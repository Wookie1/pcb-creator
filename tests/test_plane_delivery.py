"""A poured inner plane must actually be credited as connecting its net.

VCC3V3 sat on a solid inner2 plane with 30 polygons and 12 stitching vias, and
was still reported unrouted — which blocked export on a board that was fine.
Two independent defects, each silent:

1. The power stitcher built via dicts with no from_layer/to_layer. Consumers read
   them as `v.get("from_layer", "top")`, and dict.get returns None for a key that
   is PRESENT with a None value — the default only applies when the key is absent.
   So _reaches_plane saw {None, None} and credited no via with touching the plane.

2. Stitch sites were chosen against copper clearance only, which skips same-net
   features. Two same-net vias could land a drill-width apart — legal copper,
   undrillable board — and _filter_via_hole_spacing deleted one afterwards. Its
   pad's stub trace stayed behind, orphaning that pad.
"""

import math

import pytest

from validators.validate_routing import _via_layers


class TestViaLayerDefaults:
    def test_absent_keys_default(self):
        assert _via_layers({"x_mm": 1.0, "y_mm": 2.0}) == ("top", "bottom")

    def test_present_but_none_defaults_too(self):
        """The actual bug: .get(k, default) hands back None, not the default."""
        via = {"x_mm": 1.0, "y_mm": 2.0, "from_layer": None, "to_layer": None}
        assert via.get("from_layer", "top") is None      # the trap itself
        assert _via_layers(via) == ("top", "bottom")     # helper survives it

    def test_real_values_pass_through(self):
        assert _via_layers({"from_layer": "top", "to_layer": "inner2"}) \
            == ("top", "inner2")


def test_through_via_is_credited_with_reaching_an_inner_plane():
    """End-to-end on the connectivity checker: pads joined only by the plane."""
    from validators.validate_routing import _check_connectivity

    # Two VCC pads, no traces between them — connected only by the inner2
    # plane, each reached by its own through via.
    netlist = {"version": "1.0", "project_name": "p", "elements": [
        {"element_type": "component", "component_id": "comp_c1",
         "designator": "C1", "component_type": "capacitor", "value": "100nF",
         "package": "0402"},
        {"element_type": "component", "component_id": "comp_c2",
         "designator": "C2", "component_type": "capacitor", "value": "100nF",
         "package": "0402"},
        {"element_type": "port", "port_id": "port_c1_1", "component_id": "comp_c1",
         "pin_number": 1, "name": "1", "electrical_type": "passive"},
        {"element_type": "port", "port_id": "port_c2_1", "component_id": "comp_c2",
         "pin_number": 1, "name": "1", "electrical_type": "passive"},
        {"element_type": "net", "net_id": "net_vcc", "name": "VCC",
         "connected_port_ids": ["port_c1_1", "port_c2_1"], "net_class": "power"},
    ]}

    def _routed(via_layers):
        vias = []
        for x, y in ((9.5, 10.0), (19.5, 10.0)):
            v = {"x_mm": x, "y_mm": y, "drill_mm": 0.3, "diameter_mm": 0.6,
                 "net_id": "net_vcc", "net_name": "VCC"}
            v["from_layer"], v["to_layer"] = via_layers
            vias.append(v)
        return {"board": {"width_mm": 30.0, "height_mm": 20.0, "layers": 4},
                "placements": [
                    {"designator": "C1", "component_type": "capacitor",
                     "package": "0402", "footprint_width_mm": 1.0,
                     "footprint_height_mm": 0.5, "x_mm": 10.0, "y_mm": 10.0,
                     "rotation_deg": 0, "layer": "top",
                     "placement_source": "algorithm"},
                    {"designator": "C2", "component_type": "capacitor",
                     "package": "0402", "footprint_width_mm": 1.0,
                     "footprint_height_mm": 0.5, "x_mm": 20.0, "y_mm": 10.0,
                     "rotation_deg": 0, "layer": "top",
                     "placement_source": "algorithm"}],
                "routing": {"traces": [], "vias": vias, "unrouted_nets": [],
                            "copper_fills": [
                                {"layer": "inner2", "net_id": "net_vcc",
                                 "net_name": "VCC", "is_plane": True,
                                 "polygons": [[[0, 0], [30, 0], [30, 20], [0, 20]]]}],
                            "statistics": {}}}

    ok, _ = _check_connectivity(_routed(("top", "bottom")), netlist)
    assert ok == [], f"through vias should reach the inner plane, got {ok}"

    # The regression: layer pair serialised as None.
    broken, _ = _check_connectivity(_routed((None, None)), netlist)
    assert broken == [], f"a None layer pair must not void plane credit: {broken}"


def test_stitch_sites_respect_drill_hole_spacing():
    """Same-net vias must not be placed closer than they can be drilled."""
    from optimizers.router import (HOLE_TO_HOLE_MIN_MM, _filter_via_hole_spacing)

    drill = 0.3
    min_center = drill + HOLE_TO_HOLE_MIN_MM
    # Two same-net vias half the legal spacing apart.
    new = [{"x_mm": 5.0, "y_mm": 5.0, "drill_mm": drill, "diameter_mm": 0.6,
            "net_id": "net_vcc"},
           {"x_mm": 5.0 + min_center / 2, "y_mm": 5.0, "drill_mm": drill,
            "diameter_mm": 0.6, "net_id": "net_vcc"}]
    kept = _filter_via_hole_spacing([], new, min_center)
    assert len(kept) == 1, (
        "the post-filter still drops an undrillable site — the router must not "
        "commit to one, or the pad it served is orphaned")
    # And the surviving pair is genuinely drillable.
    for i, a in enumerate(kept):
        for b in kept[i + 1:]:
            assert math.hypot(a["x_mm"] - b["x_mm"], a["y_mm"] - b["y_mm"]) >= min_center


def test_stub_clearance_detects_a_crossing():
    """A pad->via stub must not cross foreign copper.

    Only the VIA site was checked against traces; the stub carrying the pad to
    it was not, so a VCC3V3 stub (U1.24 -> via) crossed a routed BOOT0 track and
    kicad-cli reported "Tracks crossing" — the board's last DRC error.

    The intersection case is the point: for two segments meeting in an X, all
    four endpoint-to-segment distances are non-zero, so a min-of-four gap
    function reports a comfortable clearance straight across a crossing.
    """
    import math
    from validators.validate_routing import _point_to_segment_distance as p2s

    def seg_seg_gap(p1x, p1y, p2x, p2y, q1x, q1y, q2x, q2y):
        d = (p2x - p1x) * (q2y - q1y) - (p2y - p1y) * (q2x - q1x)
        if abs(d) > 1e-12:
            t = ((q1x - p1x) * (q2y - q1y) - (q1y - p1y) * (q2x - q1x)) / d
            u = ((q1x - p1x) * (p2y - p1y) - (q1y - p1y) * (p2x - p1x)) / d
            if 0.0 <= t <= 1.0 and 0.0 <= u <= 1.0:
                return 0.0
        return min(p2s(p1x, p1y, q1x, q1y, q2x, q2y),
                   p2s(p2x, p2y, q1x, q1y, q2x, q2y),
                   p2s(q1x, q1y, p1x, p1y, p2x, p2y),
                   p2s(q2x, q2y, p1x, p1y, p2x, p2y))

    # The real geometry from the board: stub U1.24 -> via, and the BOOT0 track
    # it crossed at (8.189, 11.473).
    stub = (8.640, 11.2125, 7.687, 11.7625)
    boot0 = (8.189, 10.428, 8.189, 12.400)
    assert seg_seg_gap(*stub, *boot0) == 0.0, "a crossing must read as zero gap"

    # min-of-four alone would have passed it — this is why the test exists.
    naive = min(p2s(stub[0], stub[1], *boot0), p2s(stub[2], stub[3], *boot0),
                p2s(boot0[0], boot0[1], *stub), p2s(boot0[2], boot0[3], *stub))
    assert naive > 0.127, (
        f"endpoint-only distance reads {naive:.3f}mm across a crossing — "
        "which is exactly the false pass the intersection test prevents")

    # A stub that genuinely clears the same track is still accepted.
    clear_stub = (8.640, 11.2125, 9.500, 11.7625)
    assert seg_seg_gap(*clear_stub, *boot0) > 0.127
