"""Tests for the stitching-via hole-to-hole spacing filter.

Two vias whose drills sit closer than the hole-to-hole minimum trip a DRC
hole_to_hole error (observed: two GND stitching vias 0.25mm apart). The filter
drops redundant new stitching/plane vias that would collide, while never
dropping the existing routing vias.
"""

from optimizers.router import _filter_via_hole_spacing


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
