"""Connectivity extracted from the EXPORTED Gerbers must match the netlist.

Builds real Gerber + drill files with gerber_exporter, then verifies them with
validators.gerber_connectivity — which reads only the manufacturing files.
Three faults on parking_flasher_xor_20mm passed DRC but were visible here.
"""
import copy

from exporters.gerber_exporter import export_drill, export_gerbers
from validators.gerber_connectivity import verify_gerber_connectivity


def _board():
    netlist = {"elements": [
        {"element_type": "component", "component_id": "c1", "designator": "R1",
         "component_type": "resistor", "package": "0805"},
        {"element_type": "component", "component_id": "c2", "designator": "R2",
         "component_type": "resistor", "package": "0805"},
        {"element_type": "port", "port_id": "a1", "component_id": "c1", "pin_number": 1, "name": "1"},
        {"element_type": "port", "port_id": "a2", "component_id": "c1", "pin_number": 2, "name": "2"},
        {"element_type": "port", "port_id": "b1", "component_id": "c2", "pin_number": 1, "name": "1"},
        {"element_type": "port", "port_id": "b2", "component_id": "c2", "pin_number": 2, "name": "2"},
        {"element_type": "net", "net_id": "n_sig", "name": "SIG", "connected_port_ids": ["a2", "b1"]},
        {"element_type": "net", "net_id": "n_x", "name": "X", "connected_port_ids": ["a1"]},
        {"element_type": "net", "net_id": "n_y", "name": "Y", "connected_port_ids": ["b2"]},
    ]}
    placements = [
        {"designator": "R1", "package": "0805", "component_type": "resistor",
         "x_mm": 5.0, "y_mm": 5.0, "rotation_deg": 0, "layer": "top",
         "footprint_width_mm": 2.0, "footprint_height_mm": 1.25},
        {"designator": "R2", "package": "0805", "component_type": "resistor",
         "x_mm": 15.0, "y_mm": 5.0, "rotation_deg": 0, "layer": "top",
         "footprint_width_mm": 2.0, "footprint_height_mm": 1.25},
    ]
    sig = {"net_id": "n_sig", "net_name": "SIG", "layer": "top", "width_mm": 0.25,
           "start_x_mm": 5.9125, "start_y_mm": 5.0, "end_x_mm": 14.0875, "end_y_mm": 5.0}
    routed = {"project_name": "t",
              "board": {"width_mm": 20.0, "height_mm": 10.0, "layers": 2},
              "placements": placements,
              "routing": {"traces": [sig], "vias": [], "copper_fills": []}}
    return routed, netlist


def _verify(routed, netlist, tmp_path):
    export_gerbers(routed, netlist, tmp_path)
    export_drill(routed, netlist, tmp_path / "t.drl")
    return verify_gerber_connectivity(tmp_path, "t", routed, netlist)


def test_correct_board_verifies(tmp_path):
    routed, netlist = _board()
    rep = _verify(routed, netlist, tmp_path)
    assert rep["passed"], rep


def test_open_net_detected(tmp_path):
    routed, netlist = _board()
    routed["routing"]["traces"] = []                      # SIG never connected
    rep = _verify(routed, netlist, tmp_path)
    assert not rep["passed"] and "SIG" in rep["opens"]


def test_short_detected(tmp_path):
    routed, netlist = _board()
    bad = copy.deepcopy(routed["routing"]["traces"][0])   # X trace across SIG pad
    bad.update(net_id="n_x", net_name="X", start_x_mm=4.0875, end_x_mm=6.2,
               start_y_mm=5.0, end_y_mm=5.0)
    routed["routing"]["traces"].append(bad)
    rep = _verify(routed, netlist, tmp_path)
    assert not rep["passed"] and ["SIG", "X"] in rep["shorts"]
