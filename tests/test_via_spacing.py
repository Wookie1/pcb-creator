"""Tests for the stitching-via hole-to-hole spacing filter.

Two vias whose drills sit closer than the hole-to-hole minimum trip a DRC
hole_to_hole error (observed: two GND stitching vias 0.25mm apart). The filter
drops redundant new stitching/plane vias that would collide, while never
dropping the existing routing vias.
"""

from optimizers.router import (
    _filter_via_hole_spacing, _mounting_hole_keepouts, _remove_dangling_traces,
)
from optimizers.pad_geometry import PadInfo


def _v(x, y):
    return {"x_mm": x, "y_mm": y, "net_name": "GND"}


def test_drops_new_via_too_close_to_existing():
    existing = [_v(10.0, 10.0)]
    new = [_v(10.3, 10.0)]  # 0.3mm away — closer than 0.8mm min
    assert _filter_via_hole_spacing(existing, new, 0.8) == []


def test_keeps_new_via_far_enough():
    existing = [_v(10.0, 10.0)]
    new = [_v(11.0, 10.0)]  # 1.0mm away — fine
    assert _filter_via_hole_spacing(existing, new, 0.8) == new


def test_dedups_among_new_vias():
    # Two new vias 0.2mm apart: keep the first, drop the second.
    new = [_v(5.0, 5.0), _v(5.2, 5.0), _v(9.0, 9.0)]
    kept = _filter_via_hole_spacing([], new, 0.8)
    assert kept == [_v(5.0, 5.0), _v(9.0, 9.0)]


def test_never_drops_existing():
    # Existing routing vias may themselves be close; they are inputs, not
    # candidates — the function only ever filters `new`.
    existing = [_v(1.0, 1.0), _v(1.1, 1.0)]
    new = [_v(50.0, 50.0)]
    kept = _filter_via_hole_spacing(existing, new, 0.8)
    assert kept == new  # existing untouched, new far away kept


class TestMountingHoleKeepout:
    """Stitching vias must keep clear of mounting holes (NPTH) or they trip the
    hole_clearance / hole_to_hole rules — the regression where a GND via landed
    0.7mm from a corner-seeded M3 hole."""

    def test_keepout_radius_from_package(self):
        ko = _mounting_hole_keepouts(
            [{"package": "MountingHole_3.2mm_M3", "x_mm": 5.0, "y_mm": 5.0}],
            via_diameter_mm=0.6)
        assert len(ko) == 1
        x, y, d = ko[0]
        assert (x, y) == (5.0, 5.0)
        assert d == 3.2 / 2 + 0.6 / 2 + 0.2  # hole_r + via_r + clearance

    def test_via_near_mounting_hole_dropped(self):
        ko = _mounting_hole_keepouts(
            [{"package": "MountingHole_3.2mm_M3", "x_mm": 5.0, "y_mm": 5.0}], 0.6)
        # via 0.7mm from the hole centre — inside the ~2.1mm keepout → dropped
        assert _filter_via_hole_spacing([], [_v(5.5, 5.5)], 0.8,
                                        hole_keepouts=ko) == []
        # a via well clear of the hole survives
        assert _filter_via_hole_spacing([], [_v(50.0, 50.0)], 0.8,
                                        hole_keepouts=ko) == [_v(50.0, 50.0)]

    def test_non_mounting_components_ignored(self):
        assert _mounting_hole_keepouts(
            [{"package": "R_0805_2012Metric", "x_mm": 1, "y_mm": 1}], 0.6) == []


class TestDanglingTraceRemoval:
    def _pad(self, net, x, y):
        return PadInfo(port_id="p", designator="D", pin_number=1, net_id=net,
                       x_mm=x, y_mm=y, pad_width_mm=1.0, pad_height_mm=1.0,
                       layer="top")

    def _t(self, net, sx, sy, ex, ey):
        return {"net_id": net, "start_x_mm": sx, "start_y_mm": sy,
                "end_x_mm": ex, "end_y_mm": ey, "layer": "top"}

    def test_removes_stub_with_free_end(self):
        # pad at (0,0); trace goes (0,0)->(5,0) where (5,0) connects to nothing.
        pad_map = {"a": self._pad("n1", 0.0, 0.0)}
        routing = {"traces": [self._t("n1", 0.0, 0.0, 5.0, 0.0)], "vias": []}
        removed = _remove_dangling_traces(routing, pad_map)
        assert removed == 1 and routing["traces"] == []

    def test_keeps_trace_between_two_pads(self):
        # pad->pad trace is fully supported, never removed.
        pad_map = {"a": self._pad("n1", 0.0, 0.0), "b": self._pad("n1", 5.0, 0.0)}
        routing = {"traces": [self._t("n1", 0.0, 0.0, 5.0, 0.0)], "vias": []}
        assert _remove_dangling_traces(routing, pad_map) == 0
        assert len(routing["traces"]) == 1

    def test_collapses_chain_of_stubs(self):
        # pad->A->B where B's far end is free: both A and B are dangling.
        pad_map = {"a": self._pad("n1", 0.0, 0.0)}
        routing = {"traces": [self._t("n1", 0.0, 0.0, 5.0, 0.0),
                              self._t("n1", 5.0, 0.0, 9.0, 0.0)], "vias": []}
        assert _remove_dangling_traces(routing, pad_map) == 2
        assert routing["traces"] == []

    def test_different_net_does_not_support(self):
        # a pad of a DIFFERENT net at the free end must not rescue the stub.
        pad_map = {"a": self._pad("n1", 0.0, 0.0), "b": self._pad("n2", 5.0, 0.0)}
        routing = {"traces": [self._t("n1", 0.0, 0.0, 5.0, 0.0)], "vias": []}
        assert _remove_dangling_traces(routing, pad_map) == 1
