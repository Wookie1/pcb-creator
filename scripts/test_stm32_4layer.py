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


# Pin numbers for named pins that neither parse as an integer nor appear in a
# component's `pinout` spec. Assigning sequential numbers in port order instead
# would put the AMS1117's VIN on pad 2 and VOUT on pad 3 — the SOT-223 has them
# the other way round, so the routed board would feed the regulator's output and
# draw power from its input.
_NAMED_PINS: dict[tuple[str, str], dict[str, int]] = {
    # AMS1117 / SOT-223 LDO: 1=GND, 2=VOUT, 3=VIN (tab tied to VOUT).
    ("voltage_regulator", "SOT-223"): {"GND": 1, "OUT": 2, "IN": 3},
}


def _slug(text: str) -> str:
    """Lowercase an identifier fragment to satisfy the schema's [a-z0-9_] patterns.

    Shared by port_id and net_id: they previously sanitised separately, and the
    port version handled "-" but not "+", so a USB "D+" pin produced the
    schema-invalid port_id "port_j1_d+" while its net became "net_usb_dp".
    """
    out = []
    for ch in text.lower():
        if ch.isalnum() or ch == "_":
            out.append(ch)
        elif ch == "+":
            out.append("p")
        elif ch == "-":
            out.append("_")
    return "".join(out)


def _resolve_pin_number(pin_name: str, comp: dict) -> int | None:
    """Map a pin name to its physical pin number, or None if it can't be known.

    Order: numeric name → the component's `pinout` spec → the package pin map
    above. None means the caller must fall back to a positional number.

    The schema requires pin_number >= 1, and a 0 placeholder also silently
    misses `pad_geometry.build_pad_map`, whose lookup is
    `if pin_number in fp.pin_offsets` — footprint offsets start at 1, so a zero
    pin never lands on a real pad.
    """
    try:
        return int(pin_name)
    except ValueError:
        pass

    pinout = comp.get("specs", {}).get("pinout", "")
    for entry in pinout.split():
        if ":" in entry:
            num, name = entry.split(":", 1)
            if name == pin_name:
                return int(num)

    key = (comp.get("type", ""), comp.get("package", ""))
    return _NAMED_PINS.get(key, {}).get(pin_name.upper())


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

    # Ports: parse from connections.
    #
    # Two passes, because positional fallbacks must not steal a number a named
    # pin already owns. Processing in connection order alone, a crystal's "GND"
    # pin seen before its "1" pin takes pin 1 by fallback, and the real pin 1
    # then collides with it — two ports on one physical pin, shorting two nets.
    by_ref = {c["ref"]: c for c in req.get("components", [])}
    used_pins: dict[str, set[int]] = {}
    for conn in req.get("connections", []):
        for pin_ref in conn.get("pins", []):
            if "." not in pin_ref:
                continue
            ref, pin_name = pin_ref.split(".", 1)
            known = _resolve_pin_number(pin_name, by_ref.get(ref, {}))
            if known is not None:
                used_pins.setdefault(ref, set()).add(known)

    port_map: dict[str, str] = {}  # "REF.PIN" -> port_id
    for conn in req.get("connections", []):
        for pin_ref in conn.get("pins", []):
            parts = pin_ref.split(".", 1)
            if len(parts) != 2:
                continue
            ref, pin_name = parts
            port_id = f"port_{_slug(ref)}_{_slug(pin_name)}"
            port_map[pin_ref] = port_id
            comp_id = f"comp_{ref.lower()}"
            used = used_pins.setdefault(ref, set())
            pin_num = _resolve_pin_number(pin_name, by_ref.get(ref, {}))
            if pin_num is None:
                pin_num = 1
                while pin_num in used:
                    pin_num += 1
            used.add(pin_num)
            elements.append({
                "element_type": "port",
                "port_id": port_id,
                "component_id": comp_id,
                "pin_number": pin_num,
                "name": pin_name,
                "electrical_type": _infer_electrical_type(pin_name, conn.get("net_class", "signal")),
            })

    # Declare the pins that no connection mentions. validate_netlist requires
    # every pin of a component to exist, with unused ones marked no_connect —
    # the same rule finalize_circuit enforces on hand-built circuits. Without
    # this a 48-pin LQFP that uses 19 pins is an incomplete netlist, and the
    # missing pads never reach placement.
    from validators.pinout import expected_pin_count

    taken_ids = {e["port_id"] for e in elements if e["element_type"] == "port"}
    for comp in req.get("components", []):
        ref = comp["ref"]
        specs = comp.get("specs", {})
        total = expected_pin_count(comp.get("package", ""), specs)
        if not total:
            continue
        names = {}
        for entry in specs.get("pinout", "").split():
            if ":" in entry:
                num, name = entry.split(":", 1)
                names[int(num)] = name
        declared = used_pins.get(ref, set())
        for pin in range(1, total + 1):
            if pin in declared:
                continue
            name = names.get(pin, str(pin))
            port_id = f"port_{_slug(ref)}_{_slug(name)}"
            if port_id in taken_ids:
                port_id = f"port_{_slug(ref)}_p{pin}"
            taken_ids.add(port_id)
            elements.append({
                "element_type": "port",
                "port_id": port_id,
                "component_id": f"comp_{ref.lower()}",
                "pin_number": pin,
                "name": name,
                "electrical_type": "no_connect",
            })

    # Nets
    for conn in req.get("connections", []):
        net_name = conn["net_name"]
        net_id = f"net_{_slug(net_name)}"
        port_ids = [port_map[p] for p in conn.get("pins", []) if p in port_map]
        elements.append({
            "element_type": "net",
            "net_id": net_id,
            "name": net_name,
            "connected_port_ids": port_ids,
            "net_class": conn.get("net_class", "signal"),
        })

    # No "board" key: circuit_schema.json is additionalProperties:false at the
    # top level. Board dimensions and layer count reach placement through the
    # requirements file — stages._resolve_board_dims / _resolve_layers read
    # placement, then the circuit draft, then requirements, and never the
    # netlist.
    return {
        "version": "1.0",
        "project_name": req.get("project_name", PROJECT_NAME),
        "elements": elements,
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
    # "signal" is the schema-valid default for an MCU I/O pin, matching
    # validators/net_classes.py::infer_electrical_type. This previously
    # returned "bidirectional", which circuit_schema.json does not allow —
    # it put 50 off-enum ports into projects/ and out through the exporters.
    return "signal"


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

    # Activate footprint resolution before any placement call. The KiCad
    # library tier is OFF until configure_lookup() runs, so without this every
    # standard footprint silently misses and placement blocks on "unresolved
    # footprints" — the CLI, GUI and MCP entry points each do this at startup,
    # and this script previously did not.
    from optimizers.pad_geometry import configure_lookup
    from orchestrator.cache import ComponentCache
    from orchestrator.config import OrchestratorConfig as _Cfg

    _cfg = _Cfg.from_env(base_dir=pathlib.Path(__file__).parent.parent)
    _kicad_index = None
    if _cfg.kicad_library_path:
        from exporters.kicad_mod_parser import KiCadLibraryIndex
        _kicad_index = KiCadLibraryIndex(_cfg.kicad_library_path)
        print(f"Footprint library: {_cfg.kicad_library_path}")
    else:
        print("Footprint library: NONE FOUND — placement will block on "
              "unresolved footprints. Set PCB_KICAD_LIBRARY_PATH.")
    configure_lookup(kicad_index=_kicad_index,
                     cache=ComponentCache(_cfg.component_cache_path))

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
