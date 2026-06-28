"""B1 regression: inner-plane antipad must clear ALL of a THT pad's copper.

The antipad was sized from max(w,h)/2 (half the longer side), but a rectangular
pad's farthest copper is the corner at hypot(w,h)/2 — so the circular antipad
didn't reach the corners and real clearance fell below the configured value
(observed 0.162 < 0.200), blocking export and baking under-clearance into the
Gerbers. The 24-gon approximation (inscribed radius) shaved a little more.
"""

import math

from optimizers.router import generate_inner_plane, RouterConfig
from optimizers.pad_geometry import PadInfo


def _seg_dist(px, py, ax, ay, bx, by):
    dx, dy = bx - ax, by - ay
    if dx == 0 and dy == 0:
        return math.hypot(px - ax, py - ay)
    t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)))
    return math.hypot(px - (ax + t * dx), py - (ay + t * dy))


def _min_dist_to_polygon(px, py, poly):
    return min(_seg_dist(px, py, poly[i][0], poly[i][1], poly[i + 1][0], poly[i + 1][1])
               for i in range(len(poly) - 1))


def _foreign_pad_cutout(pw, ph):
    """Generate a GND plane with one foreign-net THT pad; return (cfg, pad, cutout)."""
    cfg = RouterConfig()
    board = {"width_mm": 20.0, "height_mm": 20.0}
    pad = PadInfo(port_id="P1", designator="U1", pin_number=1, net_id="net_sig",
                  x_mm=10.0, y_mm=10.0, pad_width_mm=pw, pad_height_mm=ph, layer="all")
    plane = generate_inner_plane(board, [], {"P1": pad}, [], "inner1",
                                 "net_gnd", "GND", cfg)
    return cfg, pad, plane["polygons"][1]   # [outer] + this pad's cutout


def test_rect_tht_pad_corners_clear_antipad():
    cfg, pad, cutout = _foreign_pad_cutout(1.7, 1.7)
    corners = [(pad.x_mm + sx * pad.pad_width_mm / 2, pad.y_mm + sy * pad.pad_height_mm / 2)
               for sx in (-1, 1) for sy in (-1, 1)]
    for cx, cy in corners:
        d = _min_dist_to_polygon(cx, cy, cutout)
        assert d >= cfg.fill_clearance_mm - 1e-6, \
            f"corner clearance {d:.4f} < required {cfg.fill_clearance_mm}"


def test_oval_tht_pad_far_end_clears_antipad():
    cfg, pad, cutout = _foreign_pad_cutout(2.4, 1.2)   # long axis along X
    far = [(pad.x_mm + pad.pad_width_mm / 2, pad.y_mm),
           (pad.x_mm, pad.y_mm + pad.pad_height_mm / 2)]
    for ex, ey in far:
        d = _min_dist_to_polygon(ex, ey, cutout)
        assert d >= cfg.fill_clearance_mm - 1e-6, \
            f"edge clearance {d:.4f} < required {cfg.fill_clearance_mm}"


def test_round_tht_pad_still_clears():
    # No-regression: a round pad's edge (at w/2 along axes) must still clear.
    cfg, pad, cutout = _foreign_pad_cutout(1.0, 1.0)
    for ex, ey in [(pad.x_mm + 0.5, pad.y_mm), (pad.x_mm, pad.y_mm + 0.5)]:
        d = _min_dist_to_polygon(ex, ey, cutout)
        assert d >= cfg.fill_clearance_mm - 1e-6
