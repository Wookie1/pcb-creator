"""B4b regression: KiCad export silkscreen.

(1) Each designator must appear once on silk. The footprint Reference/Value go on
    the Fab layer; the visible silk designator is the curated gr_text (same text
    the Gerbers render). Previously the footprint Reference was ALSO on silk,
    stacking two copies of every designator (silk_overlap warnings).
(2) Back-side (B.*) text must be mirrored, or KiCad flags
    nonmirrored_text_on_back_layer.
"""

import re

from exporters.kicad_exporter import export_kicad_pcb


def _board():
    netlist = {"version": "1.0", "project_name": "t", "elements": [
        {"element_type": "component", "component_id": "c_u1", "designator": "U1",
         "component_type": "resistor", "value": "10k", "package": "0805"},
        {"element_type": "component", "component_id": "c_u2", "designator": "U2",
         "component_type": "resistor", "value": "1k", "package": "0805"},
        {"element_type": "port", "port_id": "p1", "component_id": "c_u1",
         "pin_number": 1, "name": "A"},
        {"element_type": "port", "port_id": "p2", "component_id": "c_u2",
         "pin_number": 1, "name": "A"},
        {"element_type": "net", "net_id": "n1", "name": "N1",
         "connected_port_ids": ["p1", "p2"]},
    ]}
    routed = {"version": "1.0", "project_name": "t",
              "board": {"width_mm": 30, "height_mm": 20, "layers": 2},
              "placements": [
                  {"designator": "U1", "package": "0805", "component_type": "resistor",
                   "x_mm": 8, "y_mm": 10, "rotation_deg": 0, "layer": "top",
                   "footprint_width_mm": 2, "footprint_height_mm": 1.25},
                  {"designator": "U2", "package": "0805", "component_type": "resistor",
                   "x_mm": 22, "y_mm": 10, "rotation_deg": 0, "layer": "bottom",
                   "footprint_width_mm": 2, "footprint_height_mm": 1.25},
              ],
              "routing": {"traces": [], "vias": [], "unrouted_nets": []},
              "silkscreen": [
                  {"type": "text", "text": "U1", "layer": "top_silk",
                   "x_mm": 8, "y_mm": 8, "font_height_mm": 1.0},
                  {"type": "text", "text": "U2", "layer": "bottom_silk",
                   "x_mm": 22, "y_mm": 8, "font_height_mm": 1.0},
              ]}
    return netlist, routed


def _export(tmp_path):
    out = tmp_path / "t.kicad_pcb"
    netlist, routed = _board()
    export_kicad_pcb(routed, netlist, out)
    return out.read_text()


def test_no_reference_or_value_on_silk(tmp_path):
    text = _export(tmp_path)
    # Every footprint Reference/Value property must sit on a Fab layer, never
    # silk — otherwise it duplicates the gr_text silk designator.
    for m in re.finditer(
        r'\(property "(?:Reference|Value)" "\w+"\s*\(at[^)]*\)\s*\(layer "([^"]+)"\)',
        text,
    ):
        assert m.group(1).endswith(".Fab"), f"property on {m.group(1)}, expected *.Fab"


def test_silk_designator_present_once_per_part(tmp_path):
    text = _export(tmp_path)
    # The designator appears as exactly one silk gr_text (and not as a silk property).
    for des in ("U1", "U2"):
        gr = re.findall(rf'\(gr_text "{des}"', text)
        assert len(gr) == 1, f"{des}: expected 1 silk gr_text, got {len(gr)}"


def test_back_side_text_is_mirrored(tmp_path):
    text = _export(tmp_path)
    # U2 footprint block (bottom) — its Fab text must be mirrored; U1 (top) must not.
    blocks = text.split("(footprint")
    u2 = next(b for b in blocks if '"Reference" "U2"' in b)
    u1 = next(b for b in blocks if '"Reference" "U1"' in b)
    assert "(justify mirror)" in u2, "back-side footprint text not mirrored"
    assert "(justify mirror)" not in u1, "top-side footprint text wrongly mirrored"

    # The back silk gr_text line must be mirrored; the front one must not.
    for line in text.splitlines():
        if "(gr_text" not in line:
            continue
        lyr = re.search(r'\(layer "([^"]+)"\)', line).group(1)
        mirrored = "(justify mirror)" in line
        if lyr.startswith("B."):
            assert mirrored, f"back silk text not mirrored: {line.strip()}"
        else:
            assert not mirrored, f"front silk text wrongly mirrored: {line.strip()}"
