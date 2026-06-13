#!/usr/bin/env python3
"""Phase 0 spike: capture exactly what Freerouting v2.1.0 emits on stdout/stderr.

Streams the JAR's output line-by-line with timestamps so we can design the
progress-parsing regexes for freerouter.py. Also captures `--help` output to
confirm available CLI flags.

Usage:
    python scripts/spike_freerouting_progress.py [project_dir] [--passes N]

Default project: projects/test_l298n_motor_driver
Output: prints timestamped lines and writes raw logs to
        scripts/spike_output/freerouting_{help,run}.log
"""

import json
import subprocess
import sys
import threading
import time
from pathlib import Path

REPO = Path(__file__).parent.parent
sys.path.insert(0, str(REPO))

from optimizers.freerouter import ensure_jar, ensure_java  # noqa: E402
from exporters.dsn_exporter import export_dsn  # noqa: E402

OUT_DIR = REPO / "scripts" / "spike_output"


def stream(proc: subprocess.Popen, label: str, sink: list, t0: float) -> list[threading.Thread]:
    """Read stdout and stderr concurrently, timestamping each line."""
    def reader(pipe, tag):
        for line in iter(pipe.readline, ""):
            stamped = f"[{time.time() - t0:7.2f}s {tag}] {line.rstrip()}"
            print(stamped, flush=True)
            sink.append(stamped)
        pipe.close()

    threads = [
        threading.Thread(target=reader, args=(proc.stdout, "OUT"), daemon=True),
        threading.Thread(target=reader, args=(proc.stderr, "ERR"), daemon=True),
    ]
    for t in threads:
        t.start()
    return threads


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    passes = "20"
    if "--passes" in sys.argv:
        passes = sys.argv[sys.argv.index("--passes") + 1]

    project_dir = Path(args[0]) if args else REPO / "projects/test_l298n_motor_driver"
    name = project_dir.name

    java_bin = ensure_java()
    jar = ensure_jar()
    OUT_DIR.mkdir(exist_ok=True)

    # --- 1. Capture -h to confirm flags (non-fatal; v2.x may not support it) ---
    print("=== Freerouting -h ===")
    try:
        help_result = subprocess.run(
            [java_bin, "-Djava.awt.headless=true", "-jar", str(jar), "-h"],
            capture_output=True, text=True, timeout=20,
        )
        help_text = (help_result.stdout or "") + (help_result.stderr or "")
        print(help_text[:3000])
        (OUT_DIR / "freerouting_help.log").write_text(help_text)
    except subprocess.TimeoutExpired as e:
        help_text = ((e.stdout or b"").decode(errors="replace")
                     + (e.stderr or b"").decode(errors="replace"))
        print(f"(-h timed out; partial output below)\n{help_text[:3000]}")
        (OUT_DIR / "freerouting_help.log").write_text(help_text)

    # --- 2. Export DSN from the project ---
    placement = json.loads((project_dir / f"{name}_placement.json").read_text())
    netlist = json.loads((project_dir / f"{name}_netlist.json").read_text())

    dsn_path = OUT_DIR / "spike.dsn"
    ses_path = OUT_DIR / "spike.ses"
    if ses_path.exists():
        ses_path.unlink()
    export_dsn(placement, netlist, dsn_path, config={
        "trace_width_mm": 0.25,
        "clearance_mm": 0.2,
        "via_drill_mm": 0.3,
        "via_diameter_mm": 0.6,
        "exclude_nets": ["GND"],
    })
    print(f"\n=== DSN exported: {dsn_path} ({dsn_path.stat().st_size} bytes) ===")

    # --- 3. Run Freerouting with line-streaming ---
    cmd = [java_bin, "-Djava.awt.headless=true", "-jar", str(jar),
           "-de", str(dsn_path), "-do", str(ses_path),
           "-mp", passes, "-mt", "1"]
    print(f"=== Running: {' '.join(cmd)} ===\n")

    t0 = time.time()
    proc = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, bufsize=1,
    )
    captured: list[str] = []
    threads = stream(proc, name, captured, t0)
    rc = proc.wait(timeout=600)
    for t in threads:
        t.join(timeout=5)

    elapsed = time.time() - t0
    (OUT_DIR / "freerouting_run.log").write_text("\n".join(captured) + "\n")

    print(f"\n=== Done: exit={rc}, elapsed={elapsed:.1f}s, "
          f"ses={'exists ' + str(ses_path.stat().st_size) + 'B' if ses_path.exists() else 'MISSING'} ===")
    print(f"Raw logs: {OUT_DIR}/freerouting_run.log ({len(captured)} lines)")
    return 0 if rc == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
