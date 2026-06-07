"""End-to-end test: STM32F103 4-layer board.

Builds a synthetic netlist directly from the test requirements JSON
(bypassing the LLM schematic step), then runs placement → routing →
DRC → export and reports results.
"""
from __future__ import annotations

import json
import pathlib
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))

PROJECT_NAME = "test_stm32_4layer"
REQUIREMENTS_FILE = pathlib.Path("test/requirements/test_stm32_4layer.json")
PROJECT_DIR = pathlib.Path("projects")


def build_netlist(req: dict) -> dict:
    """Construct a netlist dict from requirements without an LLM."""
    elements: list[dict] = []

    # Components
    for comp in req.get("components", []):
        ref = comp["ref"]
        comp_id = f"comp_{ref.lower()}"
        elements.append({
            "element_type": "component",
            "component_id": comp_id,
            "designator": ref,
            "component_type": comp.get("type", ""),
            "value": comp.get("value", ""),
            "package": comp.get("package", "0402"),
            "description": comp.get("purpose", ""),
            "properties": comp.get("specs", {}),
        })

    # Ports: parse from connections
    port_map: dict[str, str] = {}  # "REF.PIN" -> port_id
    for conn in req.get("connections", []):
        for pin_ref in conn.get("pins", []):
            parts = pin_ref.split(".", 1)
            if len(parts) != 2:
                continue
            ref, pin_name = parts
            port_id = f"port_{ref.lower()}_{pin_name.lower().replace('-','_')}"
            port_map[pin_ref] = port_id
            comp_id = f"comp_{ref.lower()}"
            # Try to resolve pin number:
            # 1. Numeric pin names (Y1.1, Y1.2, C3.1, C3.2) → use directly
            # 2. Named pins from component pinout (STM32 LQFP-48)
            # 3. Fallback to 0 (dsn_exporter will auto-assign)
            pin_num = 0
            try:
                pin_num = int(pin_name)
            except ValueError:
                for comp in req.get("components", []):
                    if comp["ref"] == ref:
                        pinout = comp.get("specs", {}).get("pinout", "")
                        if pinout:
                            for entry in pinout.split():
                                if ":" in entry:
                                    n, p = entry.split(":", 1)
                                    if p == pin_name:
                                        pin_num = int(n)
                                        break
                        break
            elements.append({
                "element_type": "port",
                "port_id": port_id,
                "component_id": comp_id,
                "pin_number": pin_num,
                "name": pin_name,
                "electrical_type": _infer_electrical_type(pin_name, conn.get("net_class", "signal")),
            })

    # Nets
    for conn in req.get("connections", []):
        net_name = conn["net_name"]
        net_id = f"net_{net_name.lower().replace('-','_').replace('+','p')}"
        port_ids = [port_map[p] for p in conn.get("pins", []) if p in port_map]
        elements.append({
            "element_type": "net",
            "net_id": net_id,
            "name": net_name,
            "connected_port_ids": port_ids,
            "net_class": conn.get("net_class", "signal"),
        })

    # Board layers
    board = req.get("board", {})
    return {
        "project_name": req.get("project_name", PROJECT_NAME),
        "elements": elements,
        "board": board,
    }


def _infer_electrical_type(pin_name: str, net_class: str) -> str:
    p = pin_name.upper()
    if net_class == "ground":
        return "ground"
    if net_class == "power":
        return "power_in"
    if "VDD" in p or "VCC" in p or "VBAT" in p:
        return "power_in"
    if "VSS" in p or "GND" in p:
        return "ground"
    return "bidirectional"


def main():
    print("=" * 60)
    print(f"End-to-end test: {PROJECT_NAME}")
    print("=" * 60)

    req = json.loads(REQUIREMENTS_FILE.read_text())
    proj_dir = PROJECT_DIR / PROJECT_NAME
    proj_dir.mkdir(parents=True, exist_ok=True)

    # ----------------------------------------------------------------
    # Save requirements
    # ----------------------------------------------------------------
    req_path = PROJECT_DIR / f"{PROJECT_NAME}_requirements.json"
    req_path.write_text(json.dumps(req, indent=2))
    print(f"Requirements: {len(req.get('components',[]))} components, "
          f"{len(req.get('connections',[]))} nets, "
          f"layers={req.get('board',{}).get('layers',2)}")

    # ----------------------------------------------------------------
    # Step 1: Build synthetic netlist
    # ----------------------------------------------------------------
    print("\n--- Step 1: Synthetic netlist ---")
    t0 = time.time()
    netlist = build_netlist(req)
    comps = [e for e in netlist["elements"] if e["element_type"] == "component"]
    nets = [e for e in netlist["elements"] if e["element_type"] == "net"]
    ports = [e for e in netlist["elements"] if e["element_type"] == "port"]
    print(f"  {len(comps)} components, {len(ports)} ports, {len(nets)} nets ({time.time()-t0:.1f}s)")

    # Inject layers into netlist board block (stages.py reads from placement, but netlist may not carry it)
    netlist["board"] = req.get("board", {})

    # stages._p(project_dir, name, "netlist") → project_dir / f"{name}_netlist.json"
    # project_dir is passed as proj_dir.parent, so save at that level
    netlist_path = PROJECT_DIR / f"{PROJECT_NAME}_netlist.json"
    netlist_path.write_text(json.dumps(netlist, indent=2))
    print(f"  Saved: {netlist_path}")

    # ----------------------------------------------------------------
    # Step 2: Placement
    # ----------------------------------------------------------------
    print("\n--- Step 2: Placement ---")
    t0 = time.time()
    from orchestrator.config import OrchestratorConfig
    config = OrchestratorConfig()

    from orchestrator.stages import run_placement
    r = run_placement(
        project_dir=proj_dir.parent,
        project_name=PROJECT_NAME,
        config=config,
        board_width_mm=req["board"]["width_mm"],
        board_height_mm=req["board"]["height_mm"],
    )
    if not r.get("success"):
        print(f"  FAILED: {r.get('error')}")
        return 1
    print(f"  OK: {r['component_count']} placed, wire={r['wire_length_mm']}mm "
          f"({time.time()-t0:.1f}s)")

    # Inject layers into placement board block so routing picks it up
    placement_path = PROJECT_DIR / f"{PROJECT_NAME}_placement.json"
    placement_data = json.loads(placement_path.read_text())
    placement_data.setdefault("board", {})["layers"] = req["board"]["layers"]
    placement_path.write_text(json.dumps(placement_data, indent=2))
    print(f"  Patched placement board.layers={req['board']['layers']}")

    # ----------------------------------------------------------------
    # Step 3: Routing
    # ----------------------------------------------------------------
    print("\n--- Step 3: Routing (Freerouting) ---")
    t0 = time.time()
    from orchestrator.stages import run_routing
    r = run_routing(
        project_dir=proj_dir.parent,
        project_name=PROJECT_NAME,
        config=config,
        log=print,
    )
    if not r.get("success"):
        print(f"  FAILED: {r.get('error')}")
        return 1
    stats = r.get("routing_statistics", {})
    print(f"  OK: completion={stats.get('completion_pct')}%, "
          f"vias={stats.get('via_count')}, "
          f"engine={r.get('engine')} ({time.time()-t0:.1f}s)")
    fills = stats.get("copper_fill_layers", [])
    print(f"  Copper fills: {fills}")

    # ----------------------------------------------------------------
    # Step 4: DRC
    # ----------------------------------------------------------------
    print("\n--- Step 4: DRC ---")
    t0 = time.time()
    from orchestrator.stages import run_drc
    r = run_drc(
        project_dir=proj_dir.parent,
        project_name=PROJECT_NAME,
        config=config,
        log=print,
    )
    if not r.get("success"):
        print(f"  FAILED: {r.get('error')}")
    else:
        report = r.get("drc_report", {})
        print(f"  DRC: {report.get('summary')}")
        for chk in report.get("checks", []):
            if not chk.get("passed"):
                print(f"    FAIL [{chk['rule']}]: "
                      f"{len(chk.get('violations',[]))} violations")
    print(f"  ({time.time()-t0:.1f}s)")

    # ----------------------------------------------------------------
    # Step 5: Export
    # ----------------------------------------------------------------
    print("\n--- Step 5: Export ---")
    t0 = time.time()
    from orchestrator.stages import run_export
    r = run_export(
        project_dir=proj_dir.parent,
        project_name=PROJECT_NAME,
        config=config,
        log=print,
    )
    if not r.get("success"):
        print(f"  FAILED: {r.get('error')}")
        return 1
    print(f"  OK: files in {r.get('output_dir')} ({time.time()-t0:.1f}s)")
    for f in r.get("files", []):
        print(f"    {f}")

    print("\n" + "=" * 60)
    print("All steps completed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
