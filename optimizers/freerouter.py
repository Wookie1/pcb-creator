"""Freerouting integration — route PCBs using the Freerouting autorouter.

Orchestrates: DSN export → Freerouting JAR execution → SES import.
Auto-downloads the Freerouting JAR to ~/.cache/pcb-creator/ on first use.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import urllib.request
from pathlib import Path

from validators.engineering_constants import (
    VIA_DRILL_MM,
    VIA_DIAMETER_MM,
)


# ---------------------------------------------------------------------------
# Freerouting version and download
# ---------------------------------------------------------------------------

FREEROUTING_VERSION = "2.1.0"
FREEROUTING_JAR_NAME = f"freerouting-{FREEROUTING_VERSION}.jar"
FREEROUTING_DOWNLOAD_URL = (
    f"https://github.com/freerouting/freerouting/releases/download/"
    f"v{FREEROUTING_VERSION}/{FREEROUTING_JAR_NAME}"
)
DEFAULT_CACHE_DIR = Path.home() / ".cache" / "pcb-creator"


def _default_heap_mb() -> int:
    """Pick a safe JVM max-heap (MB) for Freerouting.

    Freerouting 2.x grows memory aggressively on dense boards (observed >25 GB
    on a 73-component 4-layer board).  With no cap the JVM will exhaust system
    RAM and the OS OOM-kills the process — an opaque "crash" that looks like a
    routing failure.  Capping the heap makes the JVM raise a catchable
    OutOfMemoryError instead, so route_with_freerouting can return a clear,
    actionable message (board too congested → add a routing layer / re-place).

    Default: env PCB_FREEROUTING_HEAP_MB if set, else ~55% of total RAM clamped
    to [1024, 6144] MB, leaving headroom for the OS, the MCP server, and other
    concurrent agents on small hosts (e.g. an 8 GB Raspberry Pi 5).
    """
    env = os.environ.get("PCB_FREEROUTING_HEAP_MB")
    if env:
        try:
            return max(512, int(env))
        except ValueError:
            pass
    try:
        page_size = os.sysconf("SC_PAGE_SIZE")
        phys_pages = os.sysconf("SC_PHYS_PAGES")
        total_mb = (page_size * phys_pages) // (1024 * 1024)
        return max(1024, min(6144, int(total_mb * 0.55)))
    except (ValueError, OSError, AttributeError):
        return 2048


# ---------------------------------------------------------------------------
# Environment checks
# ---------------------------------------------------------------------------

def ensure_java() -> str:
    """Check Java is available and return path to java binary.

    Raises RuntimeError if Java is not found.
    """
    # Try common Java paths first (MCP server may have restricted PATH)
    for candidate in ["/usr/bin/java", "/usr/local/bin/java", "/opt/java/bin/java"]:
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            try:
                result = subprocess.run(
                    [candidate, "-version"],
                    capture_output=True, text=True, timeout=10,
                )
                if result.returncode == 0:
                    return candidate
            except subprocess.TimeoutExpired:
                pass

    # Fall back to shutil.which
    java_bin = shutil.which("java")
    if not java_bin:
        raise RuntimeError(
            "Java not found. Freerouting requires Java 17+.\\n"
            "Install from: https://adoptium.net/ or run: brew install temurin"
        )

    # Verify it actually runs
    try:
        result = subprocess.run(
            [java_bin, "-version"],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode != 0:
            raise RuntimeError(f"Java check failed: {result.stderr[:200]}")
    except subprocess.TimeoutExpired:
        raise RuntimeError("Java version check timed out")

    return java_bin


def ensure_jar(jar_path: Path | None = None) -> Path:
    """Ensure Freerouting JAR exists, downloading if needed.

    Args:
        jar_path: Explicit path override. If None, uses default cache location.

    Returns:
        Path to the JAR file.
    """
    if jar_path and jar_path.exists():
        return jar_path

    default_path = DEFAULT_CACHE_DIR / FREEROUTING_JAR_NAME
    if default_path.exists():
        return default_path

    # Download
    DEFAULT_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    print(f"  Downloading Freerouting {FREEROUTING_VERSION}...")
    print(f"  From: {FREEROUTING_DOWNLOAD_URL}")

    try:
        urllib.request.urlretrieve(FREEROUTING_DOWNLOAD_URL, default_path)
    except Exception as e:
        # Clean up partial download
        if default_path.exists():
            default_path.unlink()
        raise RuntimeError(f"Failed to download Freerouting: {e}")

    print(f"  Saved to: {default_path}")
    return default_path


# ---------------------------------------------------------------------------
# Main routing function
# ---------------------------------------------------------------------------

def route_with_freerouting(
    placement: dict,
    netlist: dict,
    jar_path: Path | None = None,
    timeout_s: int = 300,
    exclude_nets: list[str] | None = None,
    dsn_config: dict | None = None,
    heap_mb: int | None = None,
) -> dict:
    """Route a PCB using Freerouting.

    Workflow:
    1. Export placement + netlist to DSN format
    2. Run Freerouting headlessly
    3. Import SES result
    4. Return routed dict (without copper fills)

    Args:
        placement: Placement JSON dict.
        netlist: Netlist JSON dict.
        jar_path: Override JAR location.
        timeout_s: Freerouting process timeout in seconds.
        exclude_nets: Net names to exclude from routing (e.g., ["GND"]).
        dsn_config: Design rules dict for DSN export:
            trace_width_mm, clearance_mm, via_drill_mm, via_diameter_mm
        heap_mb: JVM max-heap cap in MB.  None → auto (see _default_heap_mb).
            Prevents Freerouting's unbounded memory growth from OOM-killing the
            host on dense boards; instead it surfaces as a clear error.

    Returns:
        Routed dict compatible with route_board() output format.

    Raises:
        RuntimeError: Java not found, JAR missing, Freerouting failed/timed out.
        FileNotFoundError: SES output not produced.
    """
    java_bin = ensure_java()
    jar = ensure_jar(jar_path)

    cfg = dsn_config or {}
    via_drill = cfg.get("via_drill_mm", VIA_DRILL_MM)
    via_dia = cfg.get("via_diameter_mm", VIA_DIAMETER_MM)
    copper_oz = cfg.get("copper_weight_oz", 0.5)

    # Compute IPC-2221 trace widths per net so Freerouting uses correct widths
    # for power nets from the start (prevents post-routing DRC failures)
    net_widths: dict[str, float] = {}
    try:
        from .router import ipc2221_trace_width, compute_net_current
        from .ratsnest import build_connectivity
        nets = build_connectivity(netlist)
        for net in nets:
            current = compute_net_current(net, netlist)
            if current > 0:
                ipc_width = ipc2221_trace_width(current, copper_oz)
                # Find the net name from netlist
                for elem in netlist.get("elements", []):
                    if elem.get("element_type") == "net" and elem.get("net_id") == net.net_id:
                        net_widths[elem.get("name", net.net_id)] = ipc_width
                        break
    except Exception:
        pass  # fall back to default widths

    # Add exclude_nets and net_widths to DSN config
    dsn_cfg = dict(cfg)
    dsn_cfg["exclude_nets"] = exclude_nets or []
    dsn_cfg["net_widths"] = net_widths

    with tempfile.TemporaryDirectory(prefix="pcb-freeroute-") as tmpdir:
        dsn_path = Path(tmpdir) / "input.dsn"
        ses_path = Path(tmpdir) / "input.ses"

        # 1. Export DSN
        from exporters.dsn_exporter import export_dsn
        export_dsn(placement, netlist, dsn_path, config=dsn_cfg)
        print(f"  DSN exported: {dsn_path}")

        # 2. Build command
        # Excluded nets are already omitted from the DSN, so Freerouting routes
        # all nets present in the file. The -inc flag would RESTRICT routing to
        # only those nets — the opposite of what we want.
        # -Xmx caps the heap so a runaway board raises OutOfMemoryError (caught
        # below) instead of OOM-killing the host.
        heap = heap_mb if heap_mb is not None else _default_heap_mb()
        cmd = [java_bin, f"-Xmx{heap}m", "-Djava.awt.headless=true",
               "-jar", str(jar), "-de", str(dsn_path), "-do", str(ses_path),
               "-mp", "20", "-mt", "1"]  # -mt 1: single-thread optimization (avoids clearance bugs)

        print(f"  Running Freerouting (timeout={timeout_s}s, heap={heap}MB)...")

        # 3. Run Freerouting
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout_s,
            )
        except subprocess.TimeoutExpired:
            raise RuntimeError(
                f"Freerouting timed out after {timeout_s}s. "
                "Try increasing PCB_FREEROUTING_TIMEOUT or simplifying the board."
            )

        # Detect out-of-memory: the JVM may exit non-zero with OutOfMemoryError
        # in its output, or be killed by the OS OOM-killer (exit code -9 / 137).
        combined = ((result.stdout or "") + (result.stderr or ""))
        oom = (
            "OutOfMemoryError" in combined
            or "java.lang.OutOfMemory" in combined
            or result.returncode in (137, -9)
        )
        if oom:
            raise RuntimeError(
                f"Freerouting ran out of memory (heap cap {heap}MB). The board is "
                "too congested to route on the available signal layers. Add a "
                "routing layer (e.g. make an inner layer signal instead of a plane), "
                "loosen placement density, or raise PCB_FREEROUTING_HEAP_MB if the "
                "host has spare RAM."
            )

        if result.returncode != 0:
            stderr_snippet = result.stderr[:500] if result.stderr else "no error output"
            raise RuntimeError(f"Freerouting failed (exit code {result.returncode}): {stderr_snippet}")

        if not ses_path.exists():
            # Freerouting may name the output differently
            # Try common alternatives
            alt_names = [
                Path(tmpdir) / "input.ses",
                Path(tmpdir) / "input.scr",
            ]
            found = False
            for alt in alt_names:
                if alt.exists():
                    ses_path = alt
                    found = True
                    break

            if not found:
                # List what files were created for debugging
                created = list(Path(tmpdir).iterdir())
                raise FileNotFoundError(
                    f"Freerouting did not produce SES output. "
                    f"Files in temp dir: {[f.name for f in created]}"
                )

        print(f"  SES output: {ses_path.stat().st_size} bytes")

        # 4. Import SES — pass excluded net IDs so completion % is computed correctly
        from exporters.ses_importer import import_ses
        exclude_net_names = set(exclude_nets or [])
        exclude_net_ids: set[str] = set()
        for elem in netlist.get("elements", []):
            if elem.get("element_type") == "net":
                name = elem.get("name", elem.get("net_id", ""))
                if name in exclude_net_names:
                    exclude_net_ids.add(elem["net_id"])
        routed = import_ses(
            ses_path, placement, netlist,
            via_drill_mm=via_drill,
            via_diameter_mm=via_dia,
            exclude_net_ids=exclude_net_ids,
        )

        stats = routed.get("routing", {}).get("statistics", {})
        print(f"  Freerouting complete: {stats.get('routed_nets', 0)}/{stats.get('total_nets', 0)} nets "
              f"({stats.get('completion_pct', 0)}%)")

        return routed
