"""B3 regression: plane-net completion must be PAD-level, not net-level.

A power/ground SMD pad reaches an inner plane only through its own stitching via.
`apply_copper_fills` placed those vias but, when a pad found no clear via site, it
only logged a warning — then stripped the plane net from `unrouted_nets` anyway,
so the board reported 100% complete with a physically open power pad (observed:
5V in 3 disconnected groups). Completion must stay <100% and the net must remain
unrouted until every same-net SMD pad is actually stitched.
"""
from optimizers.router import apply_copper_fills, RouterConfig


def _board(blocked: bool):
    """4-layer (In1 GND / In2 12V plane) board with one 12V SMD pad (R1.1).

    blocked=True drops a huge foreign-net via over R1 so no clear stitching-via
    site exists for the 12V pad; blocked=False leaves it open.
    """
    netlist = {"version": "1.0", "elements": [
        {"element_type": "component", "component_id": "c_r1", "designator": "R1",
         "component_type": "resistor", "value": "1k", "package": "R_0805_2012Metric"},
        {"element_type": "port", "port_id": "p_r1_1", "component_id": "c_r1", "pin_number": 1},
        {"element_type": "port", "port_id": "p_r1_2", "component_id": "c_r1", "pin_number": 2},
        {"element_type": "net", "net_id": "net_12v", "name": "12V",
         "net_class": "power", "connected_port_ids": ["p_r1_1"]},
        {"element_type": "net", "net_id": "net_gnd", "name": "GND",
         "net_class": "power", "connected_port_ids": ["p_r1_2"]},
        {"element_type": "net", "net_id": "net_sig", "name": "SIG",
         "net_class": "signal", "connected_port_ids": []},
    ]}
    vias = []
    if blocked:
        # Foreign-net via large enough to cover every candidate site around R1.
        vias.append({"x_mm": 15.0, "y_mm": 10.0, "diameter_mm": 10.0,
                     "drill_mm": 0.3, "net_id": "net_sig", "net_name": "SIG"})
    routed = {
        "board": {"width_mm": 30.0, "height_mm": 20.0, "layers": 4, "plane_layers": 2},
        "placements": [{"designator": "R1", "package": "R_0805_2012Metric",
                        "component_type": "resistor", "x_mm": 15.0, "y_mm": 10.0,
                        "rotation_deg": 0, "layer": "top",
                        "footprint_width_mm": 2.0, "footprint_height_mm": 1.25}],
        "routing": {"traces": [], "vias": vias,
                    "unrouted_nets": ["net_gnd", "net_12v"],
                    "statistics": {"total_nets": 3}},
    }
    return routed, netlist


def test_unstitched_power_pad_keeps_net_unrouted():
    routed, netlist = _board(blocked=True)
    out = apply_copper_fills(routed, netlist, RouterConfig())
    r = out["routing"]
    assert "net_12v" in r["unrouted_nets"], "open power pad must keep its net unrouted"
    assert r["statistics"]["completion_pct"] < 100.0, "completion must reflect the open pad"
    pads = r.get("unstitched_plane_pads", [])
    assert any(p["designator"] == "R1" and p["net_id"] == "net_12v" for p in pads), \
        "the specific open pad must be surfaced"


def test_stitched_power_pad_completes_net():
    routed, netlist = _board(blocked=False)
    out = apply_copper_fills(routed, netlist, RouterConfig())
    r = out["routing"]
    assert "net_12v" not in r["unrouted_nets"], "a stitched plane net is delivered"
    assert r["statistics"]["completion_pct"] == 100.0
    assert not r.get("unstitched_plane_pads"), "no open pads expected"
    # the stitching via for 12V was actually placed
    assert any(v.get("net_id") == "net_12v" for v in r["vias"]), \
        "12V pad should have a stitching via"
