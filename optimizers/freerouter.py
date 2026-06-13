"""Freerouting integration — route PCBs using the Freerouting autorouter.

Orchestrates: DSN export → Freerouting JAR execution → SES import.
Auto-downloads the Freerouting JAR to ~/.cache/pcb-creator/ on first use.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
import threading
import time
import urllib.request
from pathlib import Path

from validators.engineering_constants import (
    VIA_DRILL_MM,
    VIA_DIAMETER_MM,
)

import logging

logger = logging.getLogger(__name__)


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


# ---------------------------------------------------------------------------
# Environment checks
# ---------------------------------------------------------------------------

def ensure_java() -> str:
    """Check Java is available and return path to java binary.

    Raises RuntimeError if Java is not found.
    """
    java_bin = shutil.which("java")
    if not java_bin:
        raise RuntimeError(
            "Java not found. Freerouting requires Java 17+.\n"
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
    logger.info(f"  Downloading Freerouting {FREEROUTING_VERSION}...")
    logger.info(f"  From: {FREEROUTING_DOWNLOAD_URL}")

    try:
        urllib.request.urlretrieve(FREEROUTING_DOWNLOAD_URL, default_path)
    except Exception as e:
        # Clean up partial download
        if default_path.exists():
            default_path.unlink()
        raise RuntimeError(f"Failed to download Freerouting: {e}")

    logger.info(f"  Saved to: {default_path}")
    return default_path


# ---------------------------------------------------------------------------
# Main routing function
# ---------------------------------------------------------------------------

# Freerouting v2.1.0 per-pass progress line (verified via
# scripts/spike_freerouting_progress.py):
#   ... INFO [job] Auto-router pass #3 on board '<hash>' was completed in
#   0.03 seconds with the score of 214.91 (1 unrouted).
# The "(N unrouted)" suffix is omitted once nothing is unrouted.
_PASS_RE = re.compile(
    r"Auto-router pass #(\d+) .*?completed in (\d+(?:\.\d+)?) seconds "
    r"with the score of (\d+(?:\.\d+)?)(?: \((\d+) unrouted\))?"
)

_HEARTBEAT_INTERVAL_S = 10.0


def route_with_freerouting(
    placement: dict,
    netlist: dict,
    jar_path: Path | None = None,
    timeout_s: int = 300,
    exclude_nets: list[str] | None = None,
    dsn_config: dict | None = None,
    progress_callback=None,
    max_passes: int = 20,
) -> dict:
    """Route a PCB using Freerouting.

    Workflow:
    1. Export placement + netlist to DSN format
    2. Run Freerouting headlessly, streaming stdout for per-pass progress
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
        progress_callback: optional callable(dict) fired for every parsed
            auto-router pass with {phase: "freerouting", pass_num, max_passes,
            incomplete_connections, score, elapsed_s, heartbeat}, and at least
            every ~10s as a heartbeat (the 'heartbeat' counter always
            increases, so pollers see forward motion even between passes).
        max_passes: Freerouting -mp value (max optimization passes).

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
    # from the start (prevents post-routing DRC failures). Currents propagate
    # through series inductors/fuses so e.g. a buck's VOUT gets the same
    # width as its SW node.
    net_widths: dict[str, float] = {}
    try:
        from .router import ipc2221_trace_width, compute_net_currents
        currents = compute_net_currents(netlist)
        net_names = {elem["net_id"]: elem.get("name", elem["net_id"])
                     for elem in netlist.get("elements", [])
                     if elem.get("element_type") == "net"}
        for net_id, current in currents.items():
            if current > 0:
                net_widths[net_names.get(net_id, net_id)] = \
                    ipc2221_trace_width(current, copper_oz)
    except Exception as exc:
        logger.warning("IPC-2221 net width computation failed (%s) — "
                       "falling back to default widths", exc)

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
        logger.info(f"  DSN exported: {dsn_path}")

        # 2. Build command
        # Excluded nets are already omitted from the DSN, so Freerouting routes
        # all nets present in the file. The -inc flag would RESTRICT routing to
        # only those nets — the opposite of what we want.
        cmd = [java_bin, "-Djava.awt.headless=true",
               "-jar", str(jar), "-de", str(dsn_path), "-do", str(ses_path),
               "-mp", str(max_passes),
               "-mt", "1"]  # -mt 1: single-thread optimization (avoids clearance bugs)

        logger.info(f"  Running Freerouting (timeout={timeout_s}s, max_passes={max_passes})...")

        # 3. Run Freerouting with stdout streaming for live pass progress.
        t0 = time.monotonic()
        state = {"pass_num": None, "incomplete": None, "score": None,
                 "heartbeat": 0}
        state_lock = threading.Lock()
        stderr_tail: list[str] = []

        def _emit() -> None:
            if progress_callback is None:
                return
            with state_lock:
                state["heartbeat"] += 1
                snapshot = {
                    "phase": "freerouting",
                    "pass_num": state["pass_num"],
                    "max_passes": max_passes,
                    "incomplete_connections": state["incomplete"],
                    "score": state["score"],
                    "elapsed_s": round(time.monotonic() - t0, 1),
                    "heartbeat": state["heartbeat"],
                }
            try:
                progress_callback(snapshot)
            except Exception:
                pass  # progress must never kill the route

        def _read_stdout(pipe) -> None:
            for line in iter(pipe.readline, ""):
                m = _PASS_RE.search(line)
                if m:
                    with state_lock:
                        state["pass_num"] = int(m.group(1))
                        state["score"] = float(m.group(3))
                        state["incomplete"] = int(m.group(4)) if m.group(4) else 0
                    _emit()
            pipe.close()

        def _read_stderr(pipe) -> None:
            for line in iter(pipe.readline, ""):
                stderr_tail.append(line)
                if len(stderr_tail) > 50:
                    stderr_tail.pop(0)
            pipe.close()

        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True, bufsize=1)
        readers = [
            threading.Thread(target=_read_stdout, args=(proc.stdout,), daemon=True),
            threading.Thread(target=_read_stderr, args=(proc.stderr,), daemon=True),
        ]
        for r in readers:
            r.start()

        # Heartbeat loop: wait in short slices so the poller always sees the
        # heartbeat counter advance even when Freerouting emits nothing.
        deadline = t0 + timeout_s
        next_beat = t0 + _HEARTBEAT_INTERVAL_S
        timed_out = False
        while True:
            try:
                proc.wait(timeout=min(1.0, max(0.05, deadline - time.monotonic())))
                break
            except subprocess.TimeoutExpired:
                now = time.monotonic()
                if now >= deadline:
                    # Ask Freerouting to stop and flush whatever it has routed
                    # so far (SIGTERM), so a long board yields a PARTIAL route
                    # to inspect instead of nothing. Routing completes in the
                    # first pass; later passes only optimize, so the partial is
                    # usually fully or nearly routed.
                    timed_out = True
                    proc.terminate()
                    try:
                        proc.wait(timeout=20)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                        proc.wait()
                    break
                if now >= next_beat:
                    _emit()
                    next_beat = now + _HEARTBEAT_INTERVAL_S
        for r in readers:
            r.join(timeout=5)

        if timed_out:
            if ses_path.exists() and ses_path.stat().st_size > 0:
                logger.warning(
                    "Freerouting timed out after %ss — importing the partial "
                    "route it had written (some nets may be unrouted).", timeout_s)
                # Fall through to SES import below.
            else:
                raise RuntimeError(
                    f"Freerouting timed out after {timeout_s}s with no partial "
                    "result written. Lower the routing effort, give it more time "
                    "(PCB_FREEROUTING_TIMEOUT), add a signal layer, or simplify "
                    "the board."
                )
        elif proc.returncode != 0:
            stderr_snippet = "".join(stderr_tail)[-500:] or "no error output"
            raise RuntimeError(f"Freerouting failed (exit code {proc.returncode}): {stderr_snippet}")

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

        logger.info(f"  SES output: {ses_path.stat().st_size} bytes")

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
        logger.info(f"  Freerouting complete: {stats.get('routed_nets', 0)}/{stats.get('total_nets', 0)} nets "
              f"({stats.get('completion_pct', 0)}%)")

        return routed
