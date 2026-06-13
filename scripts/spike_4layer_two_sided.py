#!/usr/bin/env python3
"""Spike: verify two-sided placement on a 4-layer board routes end-to-end.

On a 4-layer board the inner layers are GND/power planes and both outer
layers (F.Cu/B.Cu) are signal. A bottom-side component's power/ground pins
must reach the inner planes through vias/antipads, and its signal pins must
route on B.Cu. This drives a small board with components on BOTH sides
(including a bottom-side IC-adjacent decoupling cap) through the real
Freerouting 4-layer path + plane fills + DRC.
"""

import json
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).parent.parent
sys.path.insert(0, str(REPO))

from orchestrator.config import OrchestratorConfig  # noqa: E402
from orchestrator.stages import run_routing, run_drc  # noqa: E402
from optimizers.pad_geometry import configure_lookup  # noqa: E402
from orchestrator.cache import ComponentCache  # noqa: E402


def _comp(des, cid, ctype, value, pkg):
    return {"element_type": "component", "component_id": cid, "designator": des,
            "component_type": ctype, "value": value, "package": pkg}


def _port(cid, des, n, name, etype):
    return {"element_type": "port", "port_id": f"port_{des.lower()}_{n}",
            "component_id": cid, "pin_number": n, "name": name,
            "electrical_type": etype}


def main() -> int:
    cfg = OrchestratorConfig.from_env(base_dir=REPO)
    configure_lookup(kicad_index=None,
                     cache=ComponentCache(cfg.component_cache_path))
    cfg.router_engine = "freerouting"

    # U1 SOIC-8 on top; C1/C2 decoupling caps on BOTTOM; R1 pullup on bottom;
    # J1 connector on top. Power (VCC) and ground (GND) become inner planes.
    elements = [
        _comp("U1", "comp_u1", "ic", "OPAMP", "SOIC-8"),
        _comp("C1", "comp_c1", "capacitor", "100nF", "0805"),
        _comp("C2", "comp_c2", "capacitor", "1uF", "0805"),
        _comp("R1", "comp_r1", "resistor", "10k", "0805"),
        _comp("J1", "comp_j1", "connector", "hdr", "PinHeader_1x4"),
    ]
    # U1 pins: 1 OUT, 2 IN-, 3 IN+, 4 VEE/GND, 5 IN+b, 6 IN-b, 7 OUTb, 8 VCC
    u1_pins = [(1, "OUT", "signal"), (2, "INneg", "signal"),
               (3, "INpos", "signal"), (4, "GND", "ground"),
               (5, "INposb", "signal"), (6, "INnegb", "signal"),
               (7, "OUTb", "signal"), (8, "VCC", "power_in")]
    for n, name, et in u1_pins:
        elements.append(_port("comp_u1", "U1", n, name, et))
    for cid, des in (("comp_c1", "C1"), ("comp_c2", "C2"), ("comp_r1", "R1")):
        elements.append(_port(cid, des, 1, "1", "passive"))
        elements.append(_port(cid, des, 2, "2", "passive"))
    for n, name, et in ((1, "VCC", "power_out"), (2, "GND", "ground"),
                        (3, "SIG", "signal"), (4, "SIGB", "signal")):
        elements.append(_port("comp_j1", "J1", n, name, et))

    nets = [
        ("net_vcc", "VCC", "power",
         ["port_u1_8", "port_c1_1", "port_c2_1", "port_r1_1", "port_j1_1"]),
        ("net_gnd", "GND", "ground",
         ["port_u1_4", "port_c1_2", "port_c2_2", "port_j1_2"]),
        ("net_sig", "SIG", "signal", ["port_u1_2", "port_r1_2", "port_j1_3"]),
        ("net_out", "OUT", "signal", ["port_u1_1", "port_u1_3"]),
        ("net_sigb", "SIGB", "signal", ["port_u1_6", "port_j1_4"]),
    ]
    for nid, name, ncls, pins in nets:
        elements.append({"element_type": "net", "net_id": nid, "name": name,
                         "connected_port_ids": pins, "net_class": ncls})
    netlist = {"version": "1.0", "project_name": "spike_4l", "elements": elements}

    def place(des, x, y, rot, layer, pkg, w, h, ctype):
        return {"designator": des, "package": pkg, "component_type": ctype,
                "x_mm": x, "y_mm": y, "rotation_deg": rot, "layer": layer,
                "footprint_width_mm": w, "footprint_height_mm": h}

    placement = {
        "version": "1.0", "project_name": "spike_4l",
        "board": {"width_mm": 30, "height_mm": 24, "layers": 4},
        "placements": [
            place("U1", 15, 12, 0, "top", "SOIC-8", 5.0, 4.0, "ic"),
            place("C1", 15, 6, 0, "bottom", "0805", 2.0, 1.25, "capacitor"),
            place("C2", 15, 18, 0, "bottom", "0805", 2.0, 1.25, "capacitor"),
            place("R1", 8, 12, 90, "bottom", "0805", 2.0, 1.25, "resistor"),
            place("J1", 3, 12, 90, "top", "PinHeader_1x4", 2.5, 10.16, "connector"),
        ],
    }

    name = "spike_4l"
    tmp = Path(tempfile.mkdtemp(prefix="s4l-"))
    pdir = tmp / name
    pdir.mkdir(parents=True)
    (pdir / f"{name}_netlist.json").write_text(json.dumps(netlist))
    (pdir / f"{name}_placement.json").write_text(json.dumps(placement))

    r = run_routing(pdir, name, cfg, effort="normal", log=print)
    print(f"\nrouting: success={r.get('success')} "
          f"completion={r.get('completion_pct')}% valid={r.get('valid')}")
    routed = json.loads((pdir / f"{name}_routed.json").read_text())
    fills = routed.get("routing", {}).get("copper_fills", [])
    fill_layers = sorted({f.get("layer") for f in fills})
    trace_layers = sorted({t.get("layer")
                           for t in routed["routing"]["traces"]})
    via_count = len(routed["routing"].get("vias", []))
    print(f"trace layers: {trace_layers}  fill layers: {fill_layers}  vias: {via_count}")

    rep = run_drc(pdir, name, cfg)
    stats = rep.get("statistics", {})
    print(f"DRC: passed={rep.get('passed')} errors={stats.get('errors')} "
          f"warnings={stats.get('warnings')}")
    for c in rep.get("checks", []):
        for v in c.get("violations", []):
            if v.get("severity") == "error":
                print(f"  ERROR {c['rule']}: {v.get('message','')[:90]}")

    import shutil
    ok = r.get("success") and r.get("completion_pct", 0) >= 100 and rep.get("passed")
    shutil.rmtree(tmp, ignore_errors=True)
    print("\n" + ("PASS — 4-layer two-sided routes + DRC clean"
                  if ok else "REVIEW — see results above"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
