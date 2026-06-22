"""Inner-plane antipads must be re-cut against the FINAL via set.

run_routing's protected-wiring union can re-add through-vias AFTER apply_copper_fills
already cut the plane antipads — leaving them overlapping a power plane
(inner_plane_antipad). regenerate_inner_planes re-cuts every is_plane fill.
"""
import math
from optimizers.router import regenerate_inner_planes, RouterConfig


def _netlist():
    return {"version": "1.0", "elements": [
        {"element_type": "component", "component_id": "c_u1", "designator": "U1",
         "component_type": "ic", "value": "x", "package": "SOIC-8"},
        {"element_type": "net", "net_id": "net_12v", "name": "12V",
         "net_class": "power", "connected_port_ids": []},
        {"element_type": "net", "net_id": "net_sig", "name": "SIG",
         "net_class": "signal", "connected_port_ids": []}]}


def _routed_with_stale_plane(board_w=30):
    # inner2 = 12V plane with NO cutouts (stale), plus a foreign SIG via on it.
    outer = [(0, 0), (board_w, 0), (board_w, 20), (0, 20), (0, 0)]
    return {"board": {"width_mm": board_w, "height_mm": 20, "layers": 4,
                      "plane_layers": 2},
            "placements": [{"designator": "U1", "package": "SOIC-8",
                            "component_type": "ic", "x_mm": 15, "y_mm": 10,
                            "rotation_deg": 0, "layer": "top"}],
            "routing": {"traces": [], "unrouted_nets": [],
                        "vias": [{"x_mm": 10, "y_mm": 10, "diameter_mm": 0.6,
                                  "net_id": "net_sig", "net_name": "SIG"}],
                        "copper_fills": [{"layer": "inner2", "net_id": "net_12v",
                                          "net_name": "12V", "is_plane": True,
                                          "polygons": [outer]}]}}


def _nearest_cutout(plane, x, y):
    best = math.inf
    for poly in plane["polygons"][1:]:
        pts = poly[:-1] if poly[0] == poly[-1] else poly
        cx = sum(p[0] for p in pts) / len(pts)
        cy = sum(p[1] for p in pts) / len(pts)
        best = min(best, math.hypot(cx - x, cy - y))
    return best


def test_regen_cuts_antipad_for_foreign_via():
    routed = _routed_with_stale_plane()
    plane = routed["routing"]["copper_fills"][0]
    assert len(plane["polygons"]) == 1  # stale: no cutouts
    regenerate_inner_planes(routed, _netlist(), RouterConfig())
    plane = next(f for f in routed["routing"]["copper_fills"]
                 if f.get("is_plane"))
    assert len(plane["polygons"]) >= 2  # now has a cutout
    assert _nearest_cutout(plane, 10, 10) < 0.05  # centred on the SIG via


def test_regen_noop_without_planes():
    routed = {"board": {}, "placements": [],
              "routing": {"vias": [], "copper_fills": [
                  {"layer": "top", "net_id": "net_gnd", "polygons": [[(0, 0)]]}]}}
    out = regenerate_inner_planes(routed, _netlist(), RouterConfig())
    assert out["routing"]["copper_fills"][0]["layer"] == "top"  # untouched
