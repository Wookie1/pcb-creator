"""Silkscreen cleanup: designators relocate off pads + other silk, rotating 90°.

Replaces the old "delete any designator overlapping a pad" behavior — every part
keeps a label, placed clear of pads/fiducials and other silk (copper traces are
allowed under silk). 90° rotation is a fallback when no upright spot is clear.
"""

from optimizers.router import _silk_text_bbox, _boxes_overlap, _generate_silkscreen
from optimizers.pad_geometry import PadInfo


# --- helpers --------------------------------------------------------------

def test_text_bbox_rotation_swaps_extent():
    up = _silk_text_bbox(0, 0, "R1", 1.0, "center", 0)
    rot = _silk_text_bbox(0, 0, "R1", 1.0, "center", 90)
    up_w, up_h = up[2] - up[0], up[3] - up[1]
    rot_w, rot_h = rot[2] - rot[0], rot[3] - rot[1]
    assert up_w > up_h            # "R1" upright is wider than tall
    assert abs(rot_w - up_h) < 1e-9 and abs(rot_h - up_w) < 1e-9  # 90° swaps


def test_boxes_overlap():
    assert _boxes_overlap((0, 0, 2, 2), (1, 1, 3, 3))
    assert not _boxes_overlap((0, 0, 1, 1), (2, 2, 3, 3))


# --- scene builder --------------------------------------------------------

def _scene(comps, board=(60.0, 60.0), ctype="resistor"):
    """comps: [(des, cx, cy, w, h, [(pin, px, py, pw, ph), ...]), ...]"""
    placements, elements, pad_map = [], [], {}
    for des, cx, cy, w, h, pads in comps:
        cid = f"c_{des}"
        placements.append({
            "designator": des, "component_type": ctype, "layer": "top",
            "x_mm": cx, "y_mm": cy, "rotation_deg": 0,
            "footprint_width_mm": w, "footprint_height_mm": h,
        })
        elements.append({"element_type": "component", "component_id": cid,
                         "designator": des})
        for pin, px, py, pw, ph in pads:
            pid = f"{cid}_{pin}"
            elements.append({"element_type": "port", "component_id": cid,
                             "pin_number": pin, "name": str(pin), "port_id": pid})
            pad_map[pid] = PadInfo(port_id=pid, designator=des, pin_number=pin,
                                   net_id=None, x_mm=px, y_mm=py,
                                   pad_width_mm=pw, pad_height_mm=ph, layer="top")
    placement = {"board": {"width_mm": board[0], "height_mm": board[1]},
                 "project_name": "", "placements": placements}
    return placement, {"elements": elements}, pad_map


def _designators(silk):
    return [s for s in silk if s.get("purpose") == "designator"]


def _pad_boxes(pad_map):
    return [(p.x_mm - p.pad_width_mm / 2, p.y_mm - p.pad_height_mm / 2,
             p.x_mm + p.pad_width_mm / 2, p.y_mm + p.pad_height_mm / 2)
            for p in pad_map.values()]


def _bb(item):
    return _silk_text_bbox(item["x_mm"], item["y_mm"], item["text"],
                           item.get("font_height_mm", 1.0),
                           item.get("anchor", "center"), item.get("angle", 0))


# --- behavior -------------------------------------------------------------

def test_clear_default_position_is_kept():
    # Lone part with room above: designator stays at the default upright spot.
    pl, nl, pm = _scene([("R1", 30, 30, 2, 1, [(1, 28.8, 30, 1.0, 0.6),
                                               (2, 31.2, 30, 1.0, 0.6)])])
    d = _designators(_generate_silkscreen(pl, nl, pm))
    assert len(d) == 1
    r1 = d[0]
    assert r1.get("angle", 0) == 0
    assert abs(r1["x_mm"] - 30) < 1e-6 and r1["y_mm"] > 30  # above the part


def test_every_designator_present_and_clear_in_dense_grid():
    # 4x4 grid of parts packed 4mm apart, each with two pads — relocation must
    # keep all 16 labels and leave none overlapping a pad or another label.
    comps = []
    for i in range(4):
        for j in range(4):
            des = f"R{i}{j}"
            cx, cy = 10 + i * 4.0, 10 + j * 4.0
            comps.append((des, cx, cy, 2, 1,
                          [(1, cx - 1.0, cy, 0.9, 0.6), (2, cx + 1.0, cy, 0.9, 0.6)]))
    pl, nl, pm = _scene(comps)
    d = _designators(_generate_silkscreen(pl, nl, pm))
    assert len(d) == 16  # nothing dropped
    pads = _pad_boxes(pm)
    boxes = [_bb(x) for x in d]
    for i, b in enumerate(boxes):
        assert not any(_boxes_overlap(b, p) for p in pads), f"{d[i]['text']} over pad"
    for a in range(len(boxes)):
        for b in range(a + 1, len(boxes)):
            assert not _boxes_overlap(boxes[a], boxes[b]), "two designators overlap"


def test_rotates_90_when_only_a_narrow_vertical_slot_is_clear():
    # One part in a narrow vertical channel: wide tall pad walls leave only a
    # 1.5mm core gap (≈1.1mm after the 0.2mm pad margins). An upright "R1"
    # (≈1.35 wide) can't fit, but a 90°-rotated label (1.0 wide) can.
    pl, nl, pm = _scene([
        ("R1", 30, 30, 0.4, 0.4, [
            (1, 19.625, 30, 19.25, 40.0),   # left wall: spans x[10, 29.25], tall
            (2, 40.375, 30, 19.25, 40.0),   # right wall: spans x[30.75, 50], tall
        ]),
    ])
    d = _designators(_generate_silkscreen(pl, nl, pm))
    assert len(d) == 1
    assert d[0].get("angle", 0) == 90, "expected 90° rotation into the narrow slot"
    # and it must actually be clear of the pad walls
    pads = _pad_boxes(pm)
    assert not any(_boxes_overlap(_bb(d[0]), p) for p in pads)


def test_boxed_in_designator_kept_best_effort():
    # A part fully covered by one big pad: no clear spot anywhere → keep the label
    # at its default position rather than dropping it.
    pl, nl, pm = _scene([
        ("R1", 30, 30, 1, 1, [(1, 30, 30, 30.0, 30.0)]),  # giant pad over everything
    ])
    d = _designators(_generate_silkscreen(pl, nl, pm))
    assert len(d) == 1  # not dropped
    assert "_box" not in d[0]  # private key stripped


# --- markers (pin-1 dot / anode "A") stay off copper ----------------------

def test_anode_marker_clear_of_all_pads():
    """A DO-41-class diode: 1.7mm pads. The old fixed 1.0mm-from-pad-CENTRE
    offset put the "A" on the pad; it must now sit past the pad edge."""
    placement, netlist, pad_map = _scene([
        ("D1", 30, 30, 4.0, 2.0,
         [(1, 26.0, 30.0, 1.7, 1.7), (2, 34.0, 30.0, 1.7, 1.7)]),
    ], ctype="diode")
    silk = _generate_silkscreen(placement, netlist, pad_map)
    a = [s for s in silk if s.get("purpose") == "anode"]
    assert len(a) == 1
    for pad_box in _pad_boxes(pad_map):
        assert not _boxes_overlap(_bb(a[0]), pad_box)
    # Still meaningful: on the anode side (diode pin 2 at x=34 -> right of centre)
    assert a[0]["x_mm"] > 34.0


def test_pin1_dot_clear_of_all_pads():
    placement, netlist, pad_map = _scene([
        ("J1", 30, 30, 6.0, 6.0,
         [(1, 27.5, 27.5, 2.0, 2.0), (2, 30.0, 27.5, 2.0, 2.0),
          (3, 32.5, 27.5, 2.0, 2.0)]),
    ], ctype="connector")
    silk = _generate_silkscreen(placement, netlist, pad_map)
    dots = [s for s in silk if s.get("purpose") == "pin1"]
    assert len(dots) == 1
    r = dots[0]["diameter_mm"] / 2
    dot_bb = (dots[0]["x_mm"] - r, dots[0]["y_mm"] - r,
              dots[0]["x_mm"] + r, dots[0]["y_mm"] + r)
    for pad_box in _pad_boxes(pad_map):
        assert not _boxes_overlap(dot_bb, pad_box)


# --- designators avoid neighbouring component bodies ----------------------

def test_designator_takes_diagonal_when_cardinals_blocked():
    """Regression: four foreign pad 'bars' block all four cardinal label spots
    at every gap. The old 4-direction search fell back to its default (above)
    spot — landing on the top bar. The label must instead take an open diagonal
    and sit clear of every pad."""
    pl, nl, pm = _scene([
        ("R1", 30, 30, 0.4, 0.4,
         [(1, 29.6, 30.0, 0.4, 0.4), (2, 30.4, 30.0, 0.4, 0.4)]),
        # foreign bars N/S/E/W of R1, long enough to block all gaps; diagonals open
        ("P_N", 30, 33, 0.4, 0.4, [(1, 30.0, 33.0, 0.6, 6.0)]),
        ("P_S", 30, 27, 0.4, 0.4, [(1, 30.0, 27.0, 0.6, 6.0)]),
        ("P_E", 33, 30, 0.4, 0.4, [(1, 33.0, 30.0, 6.0, 0.6)]),
        ("P_W", 27, 30, 0.4, 0.4, [(1, 27.0, 30.0, 6.0, 0.6)]),
    ])
    r1 = [d for d in _designators(_generate_silkscreen(pl, nl, pm))
          if d["text"] == "R1"][0]
    assert not any(_boxes_overlap(_bb(r1), p) for p in _pad_boxes(pm)), \
        "R1 label must not sit on any pad"


def test_designator_over_body_beats_over_pad():
    """When every pad-clear spot also overlaps a neighbouring body, the label
    accepts the (invisible) body overlap rather than printing on an exposed
    pad."""
    big_body = (20.0, 20.0, 40.0, 40.0)  # U9 20x20 body swallowing R1's whole neighbourhood
    pl, nl, pm = _scene([
        ("R1", 30, 30, 0.4, 0.4,
         [(1, 29.6, 30.0, 0.4, 0.4), (2, 30.4, 30.0, 0.4, 0.4)]),
        ("U9", 30, 30, 20.0, 20.0, [(1, 21.0, 21.0, 0.5, 0.5)]),
    ])
    r1 = [d for d in _designators(_generate_silkscreen(pl, nl, pm))
          if d["text"] == "R1"][0]
    assert not any(_boxes_overlap(_bb(r1), p) for p in _pad_boxes(pm)), \
        "never on a pad"
    assert _boxes_overlap(_bb(r1), big_body), \
        "expected the label to accept the body overlap (pass 2)"


def test_designator_avoids_neighbor_body():
    """R1's default label spot (above) is under U9's housing — the label must
    relocate so it stays visible after assembly."""
    neighbor_body = (25.0, 31.5, 35.0, 37.5)  # U9: 10x6 body centred (30, 34.5)
    placement, netlist, pad_map = _scene([
        ("R1", 30, 30, 2.0, 1.0,
         [(1, 29.2, 30.0, 0.9, 0.9), (2, 30.8, 30.0, 0.9, 0.9)]),
        ("U9", 30, 34.5, 10.0, 6.0, [(1, 26.0, 32.5, 0.5, 0.5)]),
    ])
    silk = _generate_silkscreen(placement, netlist, pad_map)
    r1 = [d for d in _designators(silk) if d["text"] == "R1"][0]
    assert not _boxes_overlap(_bb(r1), neighbor_body)
