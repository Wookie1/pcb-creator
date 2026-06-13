#!/usr/bin/env python3
"""Board eval scoreboard: run the deterministic granular flow over every
test/requirements/*.json and measure routing completion, DRC, and wall time.

The requirements' components/connections are compiled through the incremental
circuit builder (no LLM), so this measures placement+routing+DRC quality —
the regression gate for optimizer/router tuning.

Usage:
    python scripts/eval_boards.py [--effort fast|normal|best]
                                  [--engine freerouting|builtin]
                                  [--boards name1,name2] [--no-retry]
Output:
    scripts/eval_output/scoreboard.md and scoreboard.json
"""

from __future__ import annotations

import argparse
import json
import logging
import shutil
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).parent.parent
sys.path.insert(0, str(REPO))

from orchestrator import circuit_builder as cb  # noqa: E402
from orchestrator.config import OrchestratorConfig  # noqa: E402
from orchestrator.stages import run_placement, run_routing, run_route_with_retry, run_drc  # noqa: E402

OUT_DIR = REPO / "scripts" / "eval_output"

# Requirements "type" values that differ from the circuit schema enum
_TYPE_ALIASES = {
    "regulator": "voltage_regulator",
    "transistor": "transistor_npn",
    "mosfet": "transistor_nmos",
    "button": "switch",
    "pushbutton": "switch",
    "module": "ic",
    "oscillator": "crystal",
}


def _infer_pin_count(comp: dict, req: dict) -> int | None:
    """Best-effort pin count for a custom package: specs, then the highest
    numeric pin referenced in connections for this ref."""
    specs = comp.get("specs", {}) or {}
    if specs.get("pin_count"):
        try:
            return int(specs["pin_count"])
        except (TypeError, ValueError):
            pass
    ref = comp.get("ref", "")
    pins = set()
    for conn in req.get("connections", []):
        for p in conn.get("pins", []):
            des, _, pin = p.partition(".")
            if des == ref and pin.isdigit():
                pins.add(int(pin))
    return max(pins) if pins else None


def _provide_fallback_footprint(package: str, pin_count: int) -> bool:
    """Cache a synthesized footprint for a custom package — the eval
    equivalent of an agent calling provide_footprint."""
    import re as _re
    from optimizers.pad_geometry import get_default_cache, _generate_fallback_footprint
    cache = get_default_cache()
    if cache is None:
        return False
    m = _re.search(r"(\d+(?:\.\d+)?)x(\d+(?:\.\d+)?)", package)
    w, h = (float(m.group(1)), float(m.group(2))) if m else (10.0, 10.0)
    fp = _generate_fallback_footprint(w, h, pin_count)
    offsets = {str(k): [float(v[0]), float(v[1])] for k, v in fp.pin_offsets.items()}
    cache.put_footprint(package, offsets, list(fp.pad_size),
                        source="eval-fallback", needs_review=True)
    return True


def _build_netlist(req: dict, project_dir: Path, name: str,
                   row: dict) -> tuple[bool, str]:
    """Compile requirements components/connections into a netlist via the
    circuit builder. Returns (ok, message)."""
    from optimizers.pad_geometry import get_footprint_def

    board = req.get("board", {})
    r = cb.create_draft(project_dir, name, req.get("description", ""),
                        board.get("width_mm", 50), board.get("height_mm", 40),
                        layers=board.get("layers", 2))
    if not r["ok"]:
        return False, f"create_draft: {r['error']}"

    for comp in req.get("components", []):
        ctype = comp.get("type", "ic").lower()
        ctype = _TYPE_ALIASES.get(ctype, ctype)
        if ctype not in cb.COMPONENT_TYPES:
            ctype = "ic"
        specs = comp.get("specs", {}) or {}
        pinout = specs.get("pinout")
        if not pinout:
            # Mirror Step 0's curated-spec enrichment (zero-I/O lookup table)
            from orchestrator.gather.curated_specs import lookup_specs
            curated = lookup_specs(ctype, str(comp.get("value", "")),
                                   comp.get("package", "") or "")
            if curated:
                pinout = curated.get("pinout")
        args = (project_dir, name, comp.get("ref", ""), ctype,
                str(comp.get("value", "") or "-"), comp.get("package", "") or "")
        spec_pc = specs.get("pin_count")
        spec_pc = int(spec_pc) if str(spec_pc or "").isdigit() else None
        r = cb.add_component(*args, pinout=pinout or None, pin_count=spec_pc,
                             footprint_lookup=get_footprint_def)
        if not r["ok"] and r.get("code") in ("unresolved_footprint",
                                             "unknown_pin_count"):
            # Mirror an agent's provide_footprint remediation, then retry once.
            pc = r.get("pin_count") or _infer_pin_count(comp, req)
            if pc and _provide_fallback_footprint(comp.get("package", ""), pc):
                row["fallback_fps"] = row.get("fallback_fps", 0) + 1
                r = cb.add_component(*args, pinout=pinout or None,
                                     pin_count=spec_pc,
                                     footprint_lookup=get_footprint_def)
        if not r["ok"]:
            return False, f"add_component {comp.get('ref')}: {r['error']}"

    for conn in req.get("connections", []):
        r = cb.connect_pins(project_dir, name, conn.get("net_name", ""),
                            conn.get("pins", []),
                            net_class=conn.get("net_class"))
        if not r["ok"]:
            return False, f"connect_pins {conn.get('net_name')}: {r['error']}"

    r = cb.finalize(project_dir, name)
    if not r["ok"] and r.get("code") == "unconnected_pins":
        # Eval convenience: an agent would review these; here we mark them
        # no-connect (requirements list only the intended connections).
        cb.mark_no_connect(project_dir, name, r["unconnected_pins"])
        r = cb.finalize(project_dir, name)
    if not r["ok"]:
        errs = "; ".join(r.get("errors", [])[:3]) or r.get("error", "?")
        return False, f"finalize: {errs}"
    return True, "ok"


def eval_board(req_path: Path, config, effort: str, auto_retry: bool) -> dict:
    name = req_path.stem.lower()
    req = json.loads(req_path.read_text())
    row = {"board": name, "components": len(req.get("components", [])),
           "nets": len(req.get("connections", []))}

    tmp = Path(tempfile.mkdtemp(prefix=f"eval-{name}-"))
    pdir = tmp / name
    try:
        t0 = time.monotonic()
        ok, msg = _build_netlist(req, pdir, name, row)
        if not ok:
            row.update(status="build_failed", detail=msg)
            return row

        # Write requirements next to the netlist so DFM profiles apply
        (pdir / f"{name}_requirements.json").write_text(json.dumps(req))

        place = run_placement(pdir, name, config,
                              board_width_mm=req.get("board", {}).get("width_mm"),
                              board_height_mm=req.get("board", {}).get("height_mm"),
                              seed=42)
        if not place.get("success"):
            row.update(status="place_failed", detail=place.get("error", "?"))
            return row
        row["wire_mm"] = place.get("wire_length_mm")

        t_route = time.monotonic()
        router = run_route_with_retry if auto_retry else run_routing
        route = router(pdir, name, config, effort=effort)
        row["route_s"] = round(time.monotonic() - t_route, 1)
        row["engine"] = route.get("engine")
        row["completion"] = route.get("completion_pct", 0)
        row["vias"] = route.get("via_count")
        row["retried"] = bool(route.get("retried"))
        if not route.get("success"):
            row.update(status="route_failed", detail=route.get("error", "?"))
            return row

        drc = run_drc(pdir, name, config)
        stats = drc.get("statistics", {})
        row["drc_errors"] = stats.get("errors", "?")
        row["drc_warnings"] = stats.get("warnings", "?")
        row["total_s"] = round(time.monotonic() - t0, 1)
        row["status"] = ("PASS" if row["completion"] == 100
                         and stats.get("errors", 1) == 0 else "ISSUES")
        return row
    except Exception as exc:  # noqa: BLE001 — one bad board must not kill the run
        row.update(status="crashed", detail=str(exc)[:200])
        return row
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--effort", default="normal",
                    choices=["fast", "normal", "best"])
    ap.add_argument("--engine", default="freerouting",
                    choices=["freerouting", "builtin"])
    ap.add_argument("--boards", default="",
                    help="comma-separated stems to run (default: all)")
    ap.add_argument("--no-retry", action="store_true",
                    help="disable the placement->routing feedback retry")
    args = ap.parse_args()

    logging.basicConfig(level=logging.WARNING, stream=sys.stderr)

    config = OrchestratorConfig.from_env(base_dir=REPO)
    config.router_engine = args.engine

    # Install the tiered footprint lookup (KiCad library + component cache),
    # exactly like the MCP server does — without it only the IPC-7351 and
    # built-in tiers resolve. The cache is redirected to a throwaway file so
    # eval fallback footprints never pollute the user's real cache.
    from optimizers.pad_geometry import configure_lookup
    from orchestrator.cache import ComponentCache
    eval_cache_dir = Path(tempfile.mkdtemp(prefix="eval-cache-"))
    cache = ComponentCache(str(eval_cache_dir / "component_cache.json"))
    kicad_index = None
    if config.kicad_library_path:
        try:
            from exporters.kicad_mod_parser import KiCadLibraryIndex
            kicad_index = KiCadLibraryIndex(config.kicad_library_path)
        except Exception:
            kicad_index = None
    configure_lookup(kicad_index=kicad_index, cache=cache)

    req_dir = REPO / "test" / "requirements"
    paths = sorted(req_dir.glob("*.json"))
    if args.boards:
        wanted = {b.strip() for b in args.boards.split(",")}
        paths = [p for p in paths if p.stem in wanted]
    if not paths:
        print(f"No requirements found in {req_dir}")
        return 1

    rows = []
    for p in paths:
        print(f"  {p.stem} ...", flush=True)
        row = eval_board(p, config, args.effort, not args.no_retry)
        print(f"    -> {row.get('status')} "
              f"(completion={row.get('completion', '-')}% "
              f"drc_err={row.get('drc_errors', '-')} "
              f"route={row.get('route_s', '-')}s)"
              + (f" [{row.get('detail')}]" if row.get("detail") else ""))
        rows.append(row)

    OUT_DIR.mkdir(exist_ok=True)
    meta = {"engine": args.engine, "effort": args.effort,
            "auto_retry": not args.no_retry}
    (OUT_DIR / "scoreboard.json").write_text(
        json.dumps({"meta": meta, "rows": rows}, indent=2))

    cols = ["board", "components", "nets", "status", "completion", "vias",
            "drc_errors", "drc_warnings", "wire_mm", "route_s", "retried", "fallback_fps"]
    lines = [f"# Board eval — engine={args.engine} effort={args.effort} "
             f"retry={'on' if not args.no_retry else 'off'}", "",
             "| " + " | ".join(cols) + " |",
             "|" + "---|" * len(cols)]
    for r in rows:
        lines.append("| " + " | ".join(str(r.get(c, "-")) for c in cols) + " |")
    fails = [r for r in rows if r.get("status") not in ("PASS",)]
    if fails:
        lines += ["", "## Issues", ""]
        for r in fails:
            lines.append(f"- **{r['board']}** ({r.get('status')}): "
                         f"{r.get('detail', 'see row above')}")
    (OUT_DIR / "scoreboard.md").write_text("\n".join(lines) + "\n")

    n_pass = sum(1 for r in rows if r.get("status") == "PASS")
    print(f"\n{n_pass}/{len(rows)} PASS — scoreboard at {OUT_DIR}/scoreboard.md")
    return 0 if n_pass == len(rows) else 1


if __name__ == "__main__":
    sys.exit(main())
