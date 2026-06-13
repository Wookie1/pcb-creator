#!/usr/bin/env python3
"""Spike: verify the bottom-side mirror convention end-to-end.

Builds a tiny board with resistors on top and bottom (rotations 0 and 90),
routes it through Freerouting (DSN back-side placement), and checks that the
imported traces connect at exactly the pad positions build_pad_map predicts.
If the mirror conventions disagree, connectivity validation fails.
"""

import json
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).parent.parent
sys.path.insert(0, str(REPO))

from optimizers.pad_geometry import build_pad_map  # noqa: E402
from optimizers.freerouter import route_with_freerouting  # noqa: E402
from validators.validate_routing import validate_routing  # noqa: E402


def _comp(des, cid):
    return {"element_type": "component", "component_id": cid,
            "designator": des, "component_type": "resistor",
            "value": "1k", "package": "0805"}


def _port(cid, des, n):
    return {"element_type": "port", "port_id": f"port_{des.lower()}_{n}",
            "component_id": cid, "pin_number": n, "name": str(n),
            "electrical_type": "passive"}


def main() -> int:
    netlist = {"version": "1.0", "project_name": "spike_bottom",
               "elements": []}
    for des in ("R1", "R2", "R3", "R4"):
        cid = f"comp_{des.lower()}"
        netlist["elements"].append(_comp(des, cid))
        netlist["elements"] += [_port(cid, des, 1), _port(cid, des, 2)]
    # Nets crossing top/bottom: R1(top) -- R2(bottom rot0), R3(top) -- R4(bottom rot90)
    netlist["elements"] += [
        {"element_type": "net", "net_id": "net_a", "name": "A",
         "connected_port_ids": ["port_r1_2", "port_r2_1"],
         "net_class": "signal"},
        {"element_type": "net", "net_id": "net_b", "name": "B",
         "connected_port_ids": ["port_r3_2", "port_r4_1"],
         "net_class": "signal"},
        {"element_type": "net", "net_id": "net_c", "name": "C",
         "connected_port_ids": ["port_r1_1", "port_r3_1"],
         "net_class": "signal"},
        {"element_type": "net", "net_id": "net_d", "name": "D",
         "connected_port_ids": ["port_r2_2", "port_r4_2"],
         "net_class": "signal"},
    ]

    placement = {
        "version": "1.0", "project_name": "spike_bottom",
        "board": {"width_mm": 30, "height_mm": 20, "layers": 2},
        "placements": [
            {"designator": "R1", "package": "0805", "component_type": "resistor",
             "x_mm": 8, "y_mm": 6, "rotation_deg": 0, "layer": "top",
             "footprint_width_mm": 2.0, "footprint_height_mm": 1.25},
            {"designator": "R2", "package": "0805", "component_type": "resistor",
             "x_mm": 20, "y_mm": 6, "rotation_deg": 0, "layer": "bottom",
             "footprint_width_mm": 2.0, "footprint_height_mm": 1.25},
            {"designator": "R3", "package": "0805", "component_type": "resistor",
             "x_mm": 8, "y_mm": 14, "rotation_deg": 0, "layer": "top",
             "footprint_width_mm": 2.0, "footprint_height_mm": 1.25},
            {"designator": "R4", "package": "0805", "component_type": "resistor",
             "x_mm": 20, "y_mm": 14, "rotation_deg": 90, "layer": "bottom",
             "footprint_width_mm": 2.0, "footprint_height_mm": 1.25},
        ],
    }

    pad_map = build_pad_map(placement, netlist)
    print("predicted pad positions (build_pad_map):")
    for pid, pad in sorted(pad_map.items()):
        print(f"  {pid}: ({pad.x_mm}, {pad.y_mm}) layer={pad.layer}")

    routed = route_with_freerouting(
        placement, netlist, timeout_s=120,
        exclude_nets=[],
        dsn_config={"trace_width_mm": 0.25, "clearance_mm": 0.2,
                    "via_drill_mm": 0.3, "via_diameter_mm": 0.6},
    )
    stats = routed.get("routing", {}).get("statistics", {})
    print(f"\nrouted: {stats.get('routed_nets')}/{stats.get('total_nets')} "
          f"({stats.get('completion_pct')}%)")

    layers = sorted({t.get("layer") for t in routed["routing"]["traces"]})
    print("trace layers used:", layers)

    with tempfile.TemporaryDirectory() as td:
        rp, np_ = Path(td) / "r.json", Path(td) / "n.json"
        rp.write_text(json.dumps(routed))
        np_.write_text(json.dumps(netlist))
        result = validate_routing(str(rp), str(np_))

    print(f"\nvalidation: valid={result['valid']}")
    for e in result.get("errors", []):
        print(f"  ERROR: {e}")
    for w in result.get("warnings", [])[:5]:
        print(f"  WARN: {w}")

    ok = (result["valid"] and stats.get("completion_pct") == 100.0)
    print("\n" + ("PASS — mirror convention agrees end-to-end"
                  if ok else "FAIL — convention mismatch, see errors"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
