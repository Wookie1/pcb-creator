"""Deterministic, file-based pipeline stages — no LLM, no vision critic.

Each stage reads and writes the project directory (the same file handoff the
full pipeline uses) and returns a structured result dict.  These are the units
an external agent (e.g. Hermes) orchestrates directly: it supplies the circuit
intelligence and its own QA loop, while pcb-creator provides fast, inspectable,
deterministic placement / routing / DRC / export.

The full LLM-driven runner (`runner.run_workflow`) also calls run_routing so
there is a single routing implementation.

Conventions
-----------
project_dir : Path to the project folder (…/projects/<name>)
project_name: slug; files are <project_name>_<suffix>.json inside project_dir
config      : OrchestratorConfig (carries router engine, DFM, timeouts)
"""

from __future__ import annotations

import json
import sys
from pathlib import Path


def _p(project_dir: Path, project_name: str, suffix: str) -> Path:
    return project_dir / f"{project_name}_{suffix}.json"


def _load(path: Path) -> dict:
    return json.loads(path.read_text())


# ---------------------------------------------------------------------------
# Placement
# ---------------------------------------------------------------------------

def run_placement(
    project_dir: Path,
    project_name: str,
    config,
    board_width_mm: float | None = None,
    board_height_mm: float | None = None,
    seed: int | None = None,
) -> dict:
    """Deterministic grid placement → repair → SA optimize.

    Reads <project>_netlist.json, writes <project>_placement.json.

    Board dimensions: taken from board_width_mm/board_height_mm if given, else
    from an existing placement's board block (re-optimize case), else from the
    requirements file, else a default.  A KiCad .net import carries no board
    size, so the caller should pass dimensions on first placement.

    Returns:
        {success, component_count, wire_length_mm, crossings,
         board_width_mm, board_height_mm, placement_path}
    """
    from optimizers.initial_placement import generate_grid_placement
    from optimizers.placement_optimizer import (
        optimize_placement, repair_placement, SAConfig,
    )

    netlist_path = _p(project_dir, project_name, "netlist")
    if not netlist_path.exists():
        return {"success": False, "error": f"No netlist found at {netlist_path.name}"}
    netlist = _load(netlist_path)

    placement_path = _p(project_dir, project_name, "placement")

    # Resolve board dimensions
    bw, bh = board_width_mm, board_height_mm
    if (bw is None or bh is None) and placement_path.exists():
        try:
            existing_board = _load(placement_path).get("board", {})
            bw = bw or existing_board.get("width_mm")
            bh = bh or existing_board.get("height_mm")
        except Exception:
            pass
    if bw is None or bh is None:
        req_path = _p(project_dir, project_name, "requirements")
        if req_path.exists():
            try:
                rb = _load(req_path).get("board", {})
                bw = bw or rb.get("width_mm")
                bh = bh or rb.get("height_mm")
            except Exception:
                pass
    if bw is None:
        bw = 50.0
    if bh is None:
        bh = 50.0

    # Deterministic seed placement
    placement = generate_grid_placement(netlist, bw, bh, project_name)
    if placement is None:
        return {"success": False, "error": "No components with resolvable footprints"}

    # Repair overlaps/boundary, then optimize.  Thread the seed through both
    # so a given seed yields a fully reproducible placement.
    placement = repair_placement(placement, netlist, seed=seed)
    sa = SAConfig(seed=seed) if seed is not None else SAConfig()
    placement = optimize_placement(placement, netlist, sa)

    placement_path.write_text(json.dumps(placement, indent=2))

    # Metrics
    from optimizers.ratsnest import build_connectivity, IncrementalCost
    nets = build_connectivity(netlist)
    positions = {p["designator"]: (p["x_mm"], p["y_mm"]) for p in placement["placements"]}
    ev = IncrementalCost(nets, positions)

    return {
        "success": True,
        "component_count": len(placement["placements"]),
        "wire_length_mm": round(ev.total_wire, 1),
        "crossings": ev.total_cross,
        "board_width_mm": bw,
        "board_height_mm": bh,
        "placement_path": str(placement_path),
    }


# ---------------------------------------------------------------------------
# Routing (lifted from runner.run_workflow so there is one implementation)
# ---------------------------------------------------------------------------

def _build_router_kwargs(project_dir: Path, project_name: str, log=None) -> dict:
    """Derive router design rules from the requirements/DFM profile (if any).

    log: optional callable(str) — when provided, emits the DFM-profile line the
    CLI runner used to print.  None (default) keeps this silent for MCP callers.
    """
    _log = log or (lambda *_a: None)
    copper_oz = 0.5
    mfg_rules: dict = {}
    req_path = _p(project_dir, project_name, "requirements")
    if req_path.exists():
        try:
            req_data = _load(req_path)
            copper_oz = req_data.get("board", {}).get("copper_weight_oz", 0.5)
            mfg = req_data.get("manufacturing", {})
            if mfg:
                manufacturer = mfg.get("manufacturer", "")
                if manufacturer:
                    from validators.engineering_constants import get_dfm_profile
                    mfg_rules = get_dfm_profile(manufacturer)
                    _log(f"  DFM profile: {mfg_rules.get('description', manufacturer)}")
                for key in ("trace_width_min_mm", "clearance_min_mm",
                            "via_drill_min_mm", "via_diameter_min_mm"):
                    if key in mfg:
                        mfg_rules[key] = mfg[key]
        except Exception:
            pass

    kwargs: dict = {"copper_weight_oz": copper_oz}
    if mfg_rules:
        if "trace_width_min_mm" in mfg_rules:
            tw = mfg_rules["trace_width_min_mm"]
            kwargs["trace_width_signal_mm"] = max(0.25, tw)
            kwargs["trace_width_power_mm"] = max(0.5, tw)
            kwargs["trace_width_ground_mm"] = max(0.5, tw)
        if "clearance_min_mm" in mfg_rules:
            kwargs["clearance_mm"] = max(0.2, mfg_rules["clearance_min_mm"])
        if "via_drill_min_mm" in mfg_rules:
            kwargs["via_drill_mm"] = max(0.3, mfg_rules["via_drill_min_mm"])
        if "via_diameter_min_mm" in mfg_rules:
            kwargs["via_diameter_mm"] = max(0.6, mfg_rules["via_diameter_min_mm"])
    return kwargs


def run_routing(project_dir: Path, project_name: str, config,
                progress_callback=None, log=None) -> dict:
    """Route the board: Freerouting (if configured) or built-in A* (2-layer only).

    Reads <project>_placement.json + <project>_netlist.json, writes
    <project>_routed.json.

    progress_callback: optional callable({iteration, max_iterations,
        legal_nets, total_nets, overused_cells, elapsed_s}) fired by the
        built-in NCR router each iteration.  Ignored for Freerouting.
    log: optional callable(str) — when provided (e.g. the CLI runner passes
        print), emits the engine/fallback/stats/validation diagnostic lines.
        None (default) keeps this silent for MCP callers.

    Returns:
        {success, engine, completion_pct, routed_nets, total_nets, via_count,
         trace_length_mm, unrouted_nets, valid, validation_errors,
         validation_warnings, routed_path}
    """
    _log = log or (lambda *_a: None)
    if str(config.base_dir) not in sys.path:
        sys.path.insert(0, str(config.base_dir))
    from validators.validate_routing import validate_routing as run_routing_validation

    placement_path = _p(project_dir, project_name, "placement")
    netlist_path = _p(project_dir, project_name, "netlist")
    if not placement_path.exists():
        return {"success": False, "error": "No placement found — run placement first"}
    if not netlist_path.exists():
        return {"success": False, "error": "No netlist found"}

    placement_data = _load(placement_path)
    netlist_data = _load(netlist_path)
    router_kwargs = _build_router_kwargs(project_dir, project_name, log=log)

    routed = None
    engine = "builtin"
    num_layers = placement_data.get("board", {}).get("layers", 2)

    # 4-layer boards require Freerouting — the built-in A* is 2-layer only
    if num_layers > 2 and config.router_engine != "freerouting":
        return {
            "success": False,
            "error": f"{num_layers}-layer boards require Freerouting. "
                     "Set PCB_ROUTER_ENGINE=freerouting (default) or check Java/JAR availability.",
        }

    if config.router_engine == "freerouting":
        try:
            from optimizers.freerouter import route_with_freerouting
            engine = "freerouting"
            _log("  Engine: Freerouting")
            dsn_config = {
                "trace_width_mm": router_kwargs.get("trace_width_signal_mm", 0.25),
                "clearance_mm": router_kwargs.get("clearance_mm", 0.2),
                "via_drill_mm": router_kwargs.get("via_drill_mm", 0.3),
                "via_diameter_mm": router_kwargs.get("via_diameter_mm", 0.6),
                "num_layers": num_layers,
            }
            if num_layers > 2:
                _log(f"  Layer count: {num_layers} (inner layers routed as signal)")
            # For 4-layer boards also exclude the power plane net (inner2)
            # so Freerouting doesn't try to route it — the plane fill handles it.
            # Pick the most-connected non-GND power net for inner2 (usually VCC/3V3).
            exclude_nets = ["GND"]
            if num_layers >= 4:
                best_pwr: tuple[int, str] = (0, "")
                for elem in netlist_data.get("elements", []):
                    if (elem.get("element_type") == "net"
                            and elem.get("net_class") == "power"
                            and elem.get("name", elem.get("net_id", "")) != "GND"):
                        pin_count = len(elem.get("connected_port_ids", []))
                        if pin_count > best_pwr[0]:
                            best_pwr = (pin_count, elem.get("name", elem.get("net_id", "")))
                if best_pwr[1]:
                    exclude_nets.append(best_pwr[1])
                    _log(f"  Excluding power plane net from routing: {best_pwr[1]} ({best_pwr[0]} pins)")
            routed = route_with_freerouting(
                placement_data, netlist_data,
                jar_path=config.freerouting_jar_path,
                timeout_s=config.freerouting_timeout_s,
                exclude_nets=exclude_nets,
                dsn_config=dsn_config,
            )
            completion = routed.get("routing", {}).get("statistics", {}).get("completion_pct", 0)
            if completion < 100:
                unrouted = routed.get("routing", {}).get("unrouted_nets", [])
                _log(f"  Freerouting incomplete ({completion:.0f}%): {len(unrouted)} nets unrouted")
                _log("  Continuing with partial result (no fallback when Freerouting is the engine)")
            from optimizers.router import apply_copper_fills, RouterConfig
            routed = apply_copper_fills(routed, netlist_data, RouterConfig(**router_kwargs))
        except Exception as exc:
            _log(f"  Freerouting FAILED: {exc}")
            return {"success": False, "error": f"Freerouting failed: {exc}"}

    if routed is None:
        from optimizers.router import route_board, RouterConfig
        engine = "builtin"
        _log("  Engine: Built-in")
        rc = RouterConfig(**router_kwargs)
        rc.ncr_progress_callback = progress_callback
        routed = route_board(placement_data, netlist_data, rc)

    routed_path = _p(project_dir, project_name, "routed")
    routed_path.write_text(json.dumps(routed, indent=2))

    val_result = run_routing_validation(str(routed_path), str(netlist_path))
    stats = routed.get("routing", {}).get("statistics", {})
    unrouted = routed.get("routing", {}).get("unrouted_nets", [])

    # Diagnostic summary (mirrors the CLI runner's inline block)
    if not val_result["valid"]:
        _log("  Routing validation FAILED")
        for err in val_result.get("errors", [])[:5]:
            _log(f"    - {err}")
    else:
        _log(f"  Routed: {stats.get('routed_nets', 0)}/{stats.get('total_nets', 0)} nets "
             f"({stats.get('completion_pct', 0)}%)")
        _log(f"  Trace length: {stats.get('total_trace_length_mm', 0):.1f}mm  "
             f"Vias: {stats.get('via_count', 0)}")
        if unrouted:
            _log(f"  WARNING: {len(unrouted)} nets unrouted: {', '.join(unrouted)}")
    overrides = routed.get("routing", {}).get("trace_width_overrides", {})
    if overrides:
        _log(f"  IPC-2221 trace upsizes: {len(overrides)} nets")

    return {
        "success": True,
        "engine": engine,
        "valid": val_result["valid"],
        "validation_errors": val_result.get("errors", []) or [],
        "validation_warnings": val_result.get("warnings", []) or [],
        "completion_pct": stats.get("completion_pct", 0),
        "routed_nets": stats.get("routed_nets", 0),
        "total_nets": stats.get("total_nets", 0),
        "via_count": stats.get("via_count", 0),
        "trace_length_mm": stats.get("total_trace_length_mm", 0),
        "unrouted_nets": unrouted,
        "routed_path": str(routed_path),
    }


# ---------------------------------------------------------------------------
# DRC (deterministic — kept as a first-class stage)
# ---------------------------------------------------------------------------

def run_drc(project_dir: Path, project_name: str, config, log=None) -> dict:
    """Run the deterministic DRC checks on the routed board.

    Reads <project>_routed.json + <project>_netlist.json, writes
    <project>_drc_report.json.

    log: optional callable(str) — when provided, emits the DRC pass/fail summary
        and per-check violation lines the CLI runner printed.  None = silent.

    Returns the full DRC report dict (passed, summary, checks, statistics).
    """
    _log = log or (lambda *_a: None)
    if str(config.base_dir) not in sys.path:
        sys.path.insert(0, str(config.base_dir))
    from validators.drc_report import run_drc as _run_drc

    routed_path = _p(project_dir, project_name, "routed")
    netlist_path = _p(project_dir, project_name, "netlist")
    if not routed_path.exists():
        return {"success": False, "error": "No routed board found — run routing first"}

    routed = _load(routed_path)
    netlist_data = _load(netlist_path)

    req_data = None
    req_path = _p(project_dir, project_name, "requirements")
    if req_path.exists():
        try:
            req_data = _load(req_path)
        except Exception:
            pass

    report = _run_drc(routed, netlist_data, req_data)
    _p(project_dir, project_name, "drc_report").write_text(json.dumps(report, indent=2))

    if report.get("passed"):
        _log(f"  DRC: PASSED — {report.get('summary', '')}")
    else:
        _log(f"  DRC: FAILED — {report.get('summary', '')}")
        for check in report.get("checks", []):
            if not check.get("passed", True):
                for v in check.get("violations", [])[:3]:
                    _log(f"    {v.get('severity', '').upper()}: {v.get('message', '')}")
                remaining = len(check.get("violations", [])) - 3
                if remaining > 0:
                    _log(f"    ... and {remaining} more {check.get('rule', '')} violations")

    report["success"] = True
    return report


# ---------------------------------------------------------------------------
# Output generation (Gerbers, drill, BOM CSV, CPL, STEP, ZIP)
# ---------------------------------------------------------------------------

def run_export(project_dir: Path, project_name: str, config, log=None) -> dict:
    """Generate manufacturing outputs from the routed board.

    Reads <project>_routed.json (+ optional _netlist/_bom), writes into
    <project_dir>/output/ and produces a ZIP package.  Gerbers, drill, BOM CSV,
    pick-and-place, STEP, and assembly drawing PDF (the last two best-effort).

    log: optional callable(str) — when provided, emits the per-artifact lines the
        CLI runner printed.  None (default) keeps this silent for MCP callers.

    Returns:
        {success, output_dir, files: [...], package: <zip path>}
    """
    _log = log or (lambda *_a: None)
    if str(config.base_dir) not in sys.path:
        sys.path.insert(0, str(config.base_dir))
    from exporters.gerber_exporter import export_gerbers, export_drill, create_output_package
    from exporters.bom_csv_exporter import export_bom_csv, export_pick_and_place
    from exporters.step_exporter import export_step_populated

    routed_path = _p(project_dir, project_name, "routed")
    if not routed_path.exists():
        return {"success": False, "error": "No routed board found — run routing first"}

    routed = _load(routed_path)
    netlist_path = _p(project_dir, project_name, "netlist")
    netlist_data = _load(netlist_path) if netlist_path.exists() else {}
    bom_path = _p(project_dir, project_name, "bom")
    bom_data = _load(bom_path) if bom_path.exists() else None

    output_dir = project_dir / "output"
    output_dir.mkdir(exist_ok=True)
    produced: list[str] = []

    gerber_files = export_gerbers(routed, netlist_data, output_dir)
    produced.extend(str(f) for f in gerber_files)
    _log(f"  Gerber layers: {len(gerber_files)} files")

    drill_path = export_drill(routed, netlist_data, output_dir / f"{project_name}.drl")
    produced.append(str(drill_path))
    _log(f"  Drill file: {drill_path.name}")

    if bom_data is not None:
        bom_csv = export_bom_csv(bom_data, output_dir / f"{project_name}_bom.csv")
        produced.append(str(bom_csv))
        _log(f"  BOM: {bom_csv.name}")

    cpl_path = export_pick_and_place(
        routed, output_dir / f"{project_name}_cpl.csv", bom=bom_data
    )
    produced.append(str(cpl_path))
    _log(f"  Pick-and-place: {cpl_path.name}")

    try:
        board_thickness = {4: 1.6}.get(
            routed.get("board", {}).get("layers", 2), 1.6
        )
        step_path = export_step_populated(
            routed, netlist_data, bom_data,
            output_dir / f"{project_name}_board.step",
            board_thickness_mm=board_thickness,
        )
        produced.append(str(step_path))
        _log(f"  STEP model: {step_path.name} (populated)")
    except Exception as exc:
        _log(f"  STEP model: skipped ({exc})")  # best-effort; don't fail export

    # Assembly drawing PDF (best-effort, matches the CLI runner)
    try:
        from exporters.assembly_drawing import export_assembly_drawing
        assy_path = export_assembly_drawing(
            routed, netlist_data, bom_data,
            output_dir / f"{project_name}_assembly.pdf",
            project_name=project_name,
        )
        produced.append(str(assy_path))
        _log(f"  Assembly drawing: {assy_path.name}")
    except Exception as exc:
        _log(f"  Assembly drawing: skipped ({exc})")

    zip_path = create_output_package(output_dir, project_name)
    _log(f"  Package: {zip_path.name}")

    return {
        "success": True,
        "output_dir": str(output_dir),
        "files": [str(Path(f).relative_to(project_dir)) for f in produced],
        "package": str(zip_path),
    }
