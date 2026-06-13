"""Regression tests for segment-aware connectivity in validate_routing.

The connectivity check used to union pads/vias/traces only at coincident
ENDPOINTS, so a branch trace teeing into the interior of a trunk trace (a
T-junction) or a via/pad landing mid-trace split a genuinely-routed net into
false "disconnected groups". These build real T-junction routes and assert the
net validates as connected. (Surfaced by morgan: Freerouting reported ~98%
routed while the validator flagged multi-pad nets as disconnected.)
"""

import json
import os
import tempfile

from optimizers.pad_geometry import build_pad_map
from validators.validate_routing import validate_routing


def _cap(cid, des, net_main, net_other):
    return [
        {"element_type": "component", "component_id": cid, "designator": des,
         "component_type": "capacitor", "value": "100nF", "package": "0805"},
        {"element_type": "port", "port_id": f"{cid}_1", "component_id": cid,
         "pin_number": 1, "name": "1", "electrical_type": "passive"},
        {"element_type": "port", "port_id": f"{cid}_2", "component_id": cid,
         "pin_number": 2, "name": "2", "electrical_type": "passive"},
    ]


def _net(net_id, ports, net_class="signal"):
    return {"element_type": "net", "net_id": net_id, "name": net_id,
            "net_class": net_class, "connected_port_ids": ports}


def _validate(routed, netlist):
    with tempfile.TemporaryDirectory() as td:
        rp, np_ = os.path.join(td, "r.json"), os.path.join(td, "n.json")
        json.dump(routed, open(rp, "w"))
        json.dump(netlist, open(np_, "w"))
        return validate_routing(rp, np_)


def _trace(x1, y1, x2, y2, net_id, layer="top"):
    return {"start_x_mm": x1, "start_y_mm": y1, "end_x_mm": x2, "end_y_mm": y2,
            "width_mm": 0.25, "layer": layer, "net_id": net_id}


def _build_three_pad_board():
    elements = []
    elements += _cap("c1", "C1", "net_n", "n1")
    elements += _cap("c2", "C2", "net_n", "n2")
    elements += _cap("c3", "C3", "net_n", "n3")
    elements += [
        _net("net_n", ["c1_1", "c2_1", "c3_1"]),
        _net("n1", ["c1_2"]), _net("n2", ["c2_2"]), _net("n3", ["c3_2"]),
    ]
    netlist = {"version": "1.0", "project_name": "t", "elements": elements}
    routed = {
        "version": "1.0", "project_name": "t",
        "source_netlist": "n", "source_bom": "b",
        "board": {"width_mm": 30, "height_mm": 30},
        "placements": [
            {"designator": "C1", "component_type": "capacitor", "package": "0805",
             "footprint_width_mm": 2, "footprint_height_mm": 1.25,
             "x_mm": 6, "y_mm": 15, "rotation_deg": 0, "layer": "top"},
            {"designator": "C2", "component_type": "capacitor", "package": "0805",
             "footprint_width_mm": 2, "footprint_height_mm": 1.25,
             "x_mm": 20, "y_mm": 15, "rotation_deg": 0, "layer": "top"},
            {"designator": "C3", "component_type": "capacitor", "package": "0805",
             "footprint_width_mm": 2, "footprint_height_mm": 1.25,
             "x_mm": 13, "y_mm": 25, "rotation_deg": 0, "layer": "top"},
        ],
        "routing": {"traces": [], "vias": [],
                    "statistics": {"total_nets": 4, "routed_nets": 4,
                                   "completion_pct": 100}},
        "silkscreen": [],
    }
    pm = build_pad_map(routed, netlist)
    p1, p2, p3 = pm["c1_1"], pm["c2_1"], pm["c3_1"]
    return routed, netlist, p1, p2, p3


def _disconnected_errors(result):
    return [e for e in result["errors"] if "disconnected" in e]


class TestTJunction:
    def test_branch_into_trunk_interior_is_connected(self):
        routed, netlist, p1, p2, p3 = _build_three_pad_board()
        mid = ((p1.x_mm + p2.x_mm) / 2, (p1.y_mm + p2.y_mm) / 2)
        # Trunk pad1->pad2; branch pad3 -> the MIDPOINT of the trunk (interior).
        routed["routing"]["traces"] = [
            _trace(p1.x_mm, p1.y_mm, p2.x_mm, p2.y_mm, "net_n"),
            _trace(p3.x_mm, p3.y_mm, mid[0], mid[1], "net_n"),
        ]
        assert not _disconnected_errors(_validate(routed, netlist))

    def test_genuinely_disconnected_still_flagged(self):
        """The fix must not mask a real break: pad3 left with no trace."""
        routed, netlist, p1, p2, p3 = _build_three_pad_board()
        routed["routing"]["traces"] = [
            _trace(p1.x_mm, p1.y_mm, p2.x_mm, p2.y_mm, "net_n"),
            # no trace anywhere near C3.1 → genuinely disconnected
        ]
        errs = _disconnected_errors(_validate(routed, netlist))
        assert errs, "a pad with no trace must still be flagged disconnected"


class TestViaMidTrace:
    def test_via_dropped_on_trace_interior_connects_layers(self):
        """A via landing mid-trace (not at an endpoint) bridges to a bottom
        trace; a via at the top SMD pad brings it back up. Exercises both the
        mid-trace via attachment and via-endpoint↔pad matching."""
        routed, netlist, p1, p2, p3 = _build_three_pad_board()
        mid = ((p1.x_mm + p2.x_mm) / 2, (p1.y_mm + p2.y_mm) / 2)
        routed["routing"]["traces"] = [
            _trace(p1.x_mm, p1.y_mm, p2.x_mm, p2.y_mm, "net_n", "top"),
            # bottom-layer trace from the mid-trunk via to under pad3
            _trace(mid[0], mid[1], p3.x_mm, p3.y_mm, "net_n", "bottom"),
        ]
        routed["routing"]["vias"] = [
            {"x_mm": mid[0], "y_mm": mid[1], "drill_mm": 0.3, "diameter_mm": 0.6,
             "from_layer": "top", "to_layer": "bottom", "net_id": "net_n"},
            {"x_mm": p3.x_mm, "y_mm": p3.y_mm, "drill_mm": 0.3, "diameter_mm": 0.6,
             "from_layer": "top", "to_layer": "bottom", "net_id": "net_n"},
        ]
        assert not _disconnected_errors(_validate(routed, netlist))
