"""B6 regression: completion must reflect ACTUAL pad connectivity.

The router credited a net as routed from its own net-level count even when a pad
gap remained (observed: a point-to-point signal reported done while kicad-cli
found its pads in separate groups). apply_copper_fills now reconciles
unrouted_nets against the authoritative connectivity check, so such a net is
reported unrouted and completion drops below 100 instead of a false 100%.
"""
from optimizers.router import apply_copper_fills, RouterConfig


def _board(sig_connected: bool):
    """2-layer board: GND (R1.2,R2.2) + SIG (R1.1,R2.1). SIG is joined by a trace
    only when sig_connected; otherwise its two pads have no copper between them."""
    netlist = {"version": "1.0", "elements": [
        {"element_type": "component", "component_id": "c_r1", "designator": "R1",
         "component_type": "resistor", "value": "1k", "package": "R_0805_2012Metric"},
        {"element_type": "component", "component_id": "c_r2", "designator": "R2",
         "component_type": "resistor", "value": "1k", "package": "R_0805_2012Metric"},
        {"element_type": "port", "port_id": "p_r1_1", "component_id": "c_r1", "pin_number": 1},
        {"element_type": "port", "port_id": "p_r1_2", "component_id": "c_r1", "pin_number": 2},
        {"element_type": "port", "port_id": "p_r2_1", "component_id": "c_r2", "pin_number": 1},
        {"element_type": "port", "port_id": "p_r2_2", "component_id": "c_r2", "pin_number": 2},
        {"element_type": "net", "net_id": "net_sig", "name": "SIG",
         "net_class": "signal", "connected_port_ids": ["p_r1_1", "p_r2_1"]},
        {"element_type": "net", "net_id": "net_gnd", "name": "GND",
         "net_class": "power", "connected_port_ids": ["p_r1_2", "p_r2_2"]},
    ]}
    place = [
        {"designator": "R1", "package": "R_0805_2012Metric", "component_type": "resistor",
         "x_mm": 8.0, "y_mm": 10.0, "rotation_deg": 0, "layer": "top",
         "footprint_width_mm": 2.0, "footprint_height_mm": 1.25},
        {"designator": "R2", "package": "R_0805_2012Metric", "component_type": "resistor",
         "x_mm": 22.0, "y_mm": 10.0, "rotation_deg": 0, "layer": "top",
         "footprint_width_mm": 2.0, "footprint_height_mm": 1.25},
    ]
    traces = []
    if sig_connected:
        # Join R1.1 (~7.0,10) to R2.1 (~21.0,10) along y=10 on F.Cu.
        traces.append({"start_x_mm": 7.0, "start_y_mm": 10.0,
                       "end_x_mm": 21.0, "end_y_mm": 10.0,
                       "width_mm": 0.25, "layer": "top",
                       "net_id": "net_sig", "net_name": "SIG"})
    routed = {
        "board": {"width_mm": 30.0, "height_mm": 20.0, "layers": 2},
        "placements": place,
        "routing": {"traces": traces, "vias": [], "unrouted_nets": [],
                    "statistics": {"total_nets": 2}},
    }
    return routed, netlist


def test_signal_pad_gap_is_reported_unrouted():
    routed, netlist = _board(sig_connected=False)
    out = apply_copper_fills(routed, netlist, RouterConfig())
    r = out["routing"]
    assert "net_sig" in r["unrouted_nets"], "a pad gap must be reported, not credited"
    assert r["statistics"]["completion_pct"] < 100.0


def test_connected_signal_completes():
    routed, netlist = _board(sig_connected=True)
    out = apply_copper_fills(routed, netlist, RouterConfig())
    r = out["routing"]
    assert "net_sig" not in r["unrouted_nets"], "a fully-routed signal must complete"
    assert r["statistics"]["completion_pct"] == 100.0
