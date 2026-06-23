"""Inner-plane gerbers must SUBTRACT antipad cutouts, not ship a solid plane.

Regression for the bug where _generate_copper_layer painted only polygons[0]
(the board-sized outer boundary) of an is_plane fill and dropped polygons[1:]
(the antipad cutouts) — shipping a solid copper plane that shorts every foreign
pad/via on every 4-layer board, while kicad-cli DRC (a pcbnew re-pour) masked it.
"""
import math
from exporters.gerber_exporter import export_gerbers


def _circle(cx, cy, r, n=24):
    pts = [(cx + r * math.cos(2 * math.pi * i / n),
            cy + r * math.sin(2 * math.pi * i / n)) for i in range(n)]
    pts.append(pts[0])
    return pts


def _routed_4layer(tmp_name="t"):
    outer = [(0.3, 0.3), (49.7, 0.3), (49.7, 29.7), (0.3, 29.7), (0.3, 0.3)]
    # inner1 GND plane: outer boundary + 3 antipad cutouts around foreign vias.
    cutouts = [_circle(10, 10, 0.5), _circle(20, 15, 0.5), _circle(30, 20, 0.5)]
    return {
        "project_name": tmp_name,
        "board": {"width_mm": 50, "height_mm": 30, "layers": 4},
        "placements": [],
        "routing": {
            "traces": [],
            "vias": [{"x_mm": 10, "y_mm": 10, "diameter_mm": 0.6, "net_id": "n_sig"},
                     {"x_mm": 20, "y_mm": 15, "diameter_mm": 0.6, "net_id": "n_sig"},
                     {"x_mm": 30, "y_mm": 20, "diameter_mm": 0.6, "net_id": "n_sig"}],
            "copper_fills": [{
                "layer": "inner1", "net_id": "n_gnd", "net_name": "GND",
                "is_plane": True, "polygons": [outer, *cutouts]}],
        },
    }


def test_inner_plane_gerber_subtracts_antipads(tmp_path):
    routed = _routed_4layer()
    export_gerbers(routed, {"elements": []}, tmp_path)
    in1 = (tmp_path / "t-In1_Cu.gbr").read_text()

    # The plane must NOT be a single solid region. Outer + 3 cutouts = 4 regions,
    # and the cutouts must be CLEAR (LPC) so they remove copper.
    assert in1.count("G36") == 4, "expected outer boundary + 3 antipad cutouts"
    assert "%LPC*%" in in1, "antipad cutouts must clear copper (negative polarity)"
    assert "%LPD*%" in in1, "plane outer must add copper (positive polarity)"


def test_inner_plane_not_solid_rectangle(tmp_path):
    """The shipped failure mode: a single board-sized solid region, no clears."""
    routed = _routed_4layer()
    export_gerbers(routed, {"elements": []}, tmp_path)
    in1 = (tmp_path / "t-In1_Cu.gbr").read_text()
    # A solid plane is exactly 1 region with no clear ops — must never happen
    # when the fill carries cutouts.
    assert not (in1.count("G36") == 1 and "%LPC*%" not in in1)
