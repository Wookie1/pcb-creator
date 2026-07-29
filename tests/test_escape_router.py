"""Tests for fine-pitch escape / fanout pre-routing (escape_router).

Geometry is exercised via an injected pad_map (a synthetic single-row 0.5mm
connector) so the tests are independent of footprint resolution.

The full breakout for each escaping pin is: a pad->via *stub* (on the pad
layer), a through via, and — for signal nets — an onward *fanout* trace on a
stackup-aware signal layer ending on a release line clear of the pad field.
Pins on a plane net (``exclude_nets``) drop straight to their plane with a via
and no fanout.
"""

import math

from optimizers.escape_router import (generate_escape_routing, EscapeConfig,
                                       _auto_drop_layer)
from optimizers.pad_geometry import PadInfo


def _connector_padmap(n=16, pitch=0.5, x0=10.0, y=5.0, layer="top",
                      net_prefix="sig", leaving=True):
    """A single-row connector: n pads along x at `pitch`, row at y=`y`."""
    pads = {}
    for i in range(n):
        pid = f"cn1_{i + 1}"
        pads[pid] = PadInfo(
            port_id=pid, designator="CN1", pin_number=i + 1,
            net_id=f"{net_prefix}_{i + 1}",
            x_mm=x0 + i * pitch, y_mm=y,
            pad_width_mm=0.3, pad_height_mm=1.0, layer=layer)
    return pads


def _netlist_for(padmap, leaving=True):
    """Build a netlist where each pad's net also touches another component
    (so the net 'leaves' CN1) unless leaving=False."""
    elements = [{"element_type": "component", "component_id": "c_cn1",
                 "designator": "CN1", "component_type": "connector",
                 "package": "FPC", "value": "x"}]
    for pid, pad in padmap.items():
        elements.append({"element_type": "port", "port_id": pid,
                         "component_id": "c_cn1", "pin_number": pad.pin_number,
                         "name": str(pad.pin_number)})
    # a sink component to receive the leaving nets
    elements.append({"element_type": "component", "component_id": "c_u1",
                     "designator": "U1", "component_type": "ic",
                     "package": "QFP", "value": "x"})
    for i, (pid, pad) in enumerate(padmap.items()):
        sink = f"u1_{i + 1}"
        elements.append({"element_type": "port", "port_id": sink,
                         "component_id": "c_u1", "pin_number": i + 1,
                         "name": str(i + 1)})
        ports = [pid, sink] if leaving else [pid]
        elements.append({"element_type": "net", "net_id": pad.net_id,
                         "name": pad.net_id, "net_class": "signal",
                         "connected_port_ids": ports})
    return {"version": "1.0", "project_name": "t", "elements": elements}


def _placement():
    return {"version": "1.0", "project_name": "t",
            "board": {"width_mm": 40, "height_mm": 30}, "placements": []}


def _stubs(out):
    return [t for t in out["traces"] if t.get("escape_role") == "stub"]


def _fanouts(out):
    return [t for t in out["traces"] if t.get("escape_role") == "fanout"]


class TestEscapeGeneration:
    def test_breakout_per_signal_pad(self):
        pm = _connector_padmap(16)
        out = generate_escape_routing(_placement(), _netlist_for(pm), pad_map=pm)
        # every signal pin → 1 via + 1 stub + 1 fanout
        assert len(out["vias"]) == 16
        assert len(_stubs(out)) == 16
        assert len(_fanouts(out)) == 16
        # stub ends at its via; fanout starts at its via
        by_net_via = {v["net_id"]: (v["x_mm"], v["y_mm"]) for v in out["vias"]}
        for s in _stubs(out):
            assert (s["end_x_mm"], s["end_y_mm"]) == by_net_via[s["net_id"]]
        for f in _fanouts(out):
            assert (f["start_x_mm"], f["start_y_mm"]) == by_net_via[f["net_id"]]

    def test_every_signal_pin_escaped(self):
        """No signal pin is silently skipped (v1 dropped shared/edge pins)."""
        pm = _connector_padmap(30, pitch=0.5)
        out = generate_escape_routing(_placement(), _netlist_for(pm), pad_map=pm)
        escaped = {v["net_id"] for v in out["vias"]}
        assert escaped == {p.net_id for p in pm.values()}

    def test_vias_are_collision_free(self):
        cfg = EscapeConfig()
        pm = _connector_padmap(16, pitch=0.5)
        out = generate_escape_routing(_placement(), _netlist_for(pm),
                                      config=cfg, pad_map=pm)
        centers = [(v["x_mm"], v["y_mm"]) for v in out["vias"]]
        min_sep = cfg.via_diameter_mm + cfg.clearance_mm
        for i in range(len(centers)):
            for j in range(i + 1, len(centers)):
                d = math.hypot(centers[i][0] - centers[j][0],
                               centers[i][1] - centers[j][1])
                assert d >= min_sep - 1e-6, f"vias {i},{j} too close: {d:.3f}"

    def test_fanout_threads_neighbour_vias_cleanly(self):
        """Near-row fanout traces must clear the far-row via bodies of OTHER
        nets (the thing v1 left to the autorouter and got shorts from)."""
        cfg = EscapeConfig()
        pm = _connector_padmap(16, pitch=0.5)
        out = generate_escape_routing(_placement(), _netlist_for(pm),
                                      config=cfg, pad_map=pm)
        via_r = cfg.via_diameter_mm / 2
        hw = cfg.trace_width_mm / 2
        for t in out["traces"]:
            ax, ay, bx, by = (t["start_x_mm"], t["start_y_mm"],
                              t["end_x_mm"], t["end_y_mm"])
            for v in out["vias"]:
                if v["net_id"] == t["net_id"]:
                    continue
                # point-segment distance
                dx, dy = bx - ax, by - ay
                L2 = dx * dx + dy * dy
                u = 0.0 if L2 == 0 else max(0, min(1, ((v["x_mm"] - ax) * dx +
                                                       (v["y_mm"] - ay) * dy) / L2))
                d = math.hypot(v["x_mm"] - (ax + u * dx), v["y_mm"] - (ay + u * dy))
                assert d - via_r - hw >= cfg.clearance_mm - 1e-3, \
                    f"fanout {t['net_id']} too close to via {v['net_id']}: {d:.3f}"

    def test_escapes_staggered_two_rows(self):
        """Adjacent pads escape to alternating distances (two via rows)."""
        pm = _connector_padmap(16, pitch=0.5, y=5.0)
        out = generate_escape_routing(_placement(), _netlist_for(pm), pad_map=pm)
        # row at y=5, board center y=15 → escape +y; vias at two distinct y's
        ys = sorted({round(v["y_mm"], 3) for v in out["vias"]})
        assert len(ys) == 2, f"expected two staggered via rows, got {ys}"


class TestDropLayer:
    def test_drop_layer_override(self):
        pm = _connector_padmap(12, layer="top")
        out = generate_escape_routing(_placement(), _netlist_for(pm),
                                      config=EscapeConfig(drop_layer="bottom"),
                                      pad_map=pm)
        assert all(v["from_layer"] == "top" for v in out["vias"])
        assert all(t["layer"] == "bottom" for t in _fanouts(out))

    def test_stackup_aware_avoids_plane(self):
        """plane_layers=1 → In1 is the GND plane; signal fanout must drop to a
        routable signal layer (In2), never a plane."""
        pm = _connector_padmap(12, layer="top")
        out = generate_escape_routing(
            _placement(), _netlist_for(pm),
            config=EscapeConfig(num_layers=4, plane_layers=1), pad_map=pm)
        assert all(t["layer"] == "inner2" for t in _fanouts(out))

    def test_auto_drop_layer_helper(self):
        # 4-layer, In1 plane only → signal pads on top fan out on In2
        assert _auto_drop_layer("top", 4, 1) == "inner2"
        # both inner planes → only the opposite outer layer is free
        assert _auto_drop_layer("top", 4, 2) == "bottom"
        # 2-layer → opposite side
        assert _auto_drop_layer("top", 2, 0) == "bottom"


class TestPlaneNets:
    def test_plane_net_emits_keepouts(self):
        """A plane-net (GND) escape — invisible to the autorouter — emits
        keepout circles so other nets don't route over its stub/via."""
        pm = _connector_padmap(16, pitch=0.5)
        out = generate_escape_routing(
            _placement(), _netlist_for(pm), pad_map=pm,
            config=EscapeConfig(num_layers=4, plane_layers=1),
            exclude_nets=("sig_1",))
        assert out["keepouts"], "expected keepouts for the plane-net escape"
        # signal-net escapes get protected fanout, not keepouts → only sig_1's
        assert all(k["diameter_mm"] > 0 for k in out["keepouts"])

    def test_no_keepouts_without_plane_nets(self):
        pm = _connector_padmap(16, pitch=0.5)
        out = generate_escape_routing(_placement(), _netlist_for(pm), pad_map=pm)
        assert out["keepouts"] == []

    def test_plane_net_drops_to_plane_no_fanout(self):
        """A pin on an excluded (plane) net still escapes — a via to the plane,
        but no onward fanout trace (the plane makes the connection)."""
        pm = _connector_padmap(16, pitch=0.5)
        out = generate_escape_routing(
            _placement(), _netlist_for(pm), pad_map=pm,
            config=EscapeConfig(num_layers=4, plane_layers=1),
            exclude_nets=("sig_1",))
        # sig_1 still gets a via + stub...
        assert any(v["net_id"] == "sig_1" for v in out["vias"])
        assert any(s["net_id"] == "sig_1" for s in _stubs(out))
        # ...as a full through-via (top→bottom) that passes In1 and connects to
        # whichever inner plane carries the net — a single-layer drop to In1
        # would miss a power net living on the In2 plane...
        v1 = next(v for v in out["vias"] if v["net_id"] == "sig_1")
        assert (v1["from_layer"], v1["to_layer"]) == ("top", "bottom")
        # ...and NO fanout trace.
        assert not any(f["net_id"] == "sig_1" for f in _fanouts(out))


class TestGuards:
    def test_coarse_pitch_part_skipped(self):
        pm = _connector_padmap(16, pitch=2.54)  # 2.54mm header — not fine-pitch
        out = generate_escape_routing(_placement(), _netlist_for(pm), pad_map=pm)
        assert out["vias"] == [] and out["traces"] == []

    def test_few_pin_part_skipped(self):
        pm = _connector_padmap(4, pitch=0.5)  # below ESCAPE_MIN_PINS
        out = generate_escape_routing(_placement(), _netlist_for(pm), pad_map=pm)
        assert out["vias"] == []

    def test_internal_net_skipped(self):
        pm = _connector_padmap(16, pitch=0.5)
        nl = _netlist_for(pm, leaving=False)  # nets touch only CN1
        out = generate_escape_routing(_placement(), nl, pad_map=pm)
        assert out["vias"] == [], "nets that don't leave the part need no escape"

    def test_multirow_part_skipped(self):
        # two rows → not a single-row part (v1 leaves these to the autorouter)
        pm = _connector_padmap(8, pitch=0.5, y=5.0)
        pm2 = _connector_padmap(8, pitch=0.5, y=6.0, net_prefix="sigb")
        for k, v in pm2.items():
            pm[k + "b"] = v
        out = generate_escape_routing(_placement(), _netlist_for(pm), pad_map=pm)
        assert out["vias"] == []


# --- quad packs (LQFP/TQFP/QFN) --------------------------------------------
# v1 classified a part as a single row or column and skipped anything else, so
# an LQFP-48 — pads on four sides, both spans large — was never fanned out at
# all. Its power pins then had no route to the inner plane: post-route stitching
# runs after Freerouting, by which time the escape channels are taken, and a
# 0.6mm via does not fit a 0.3mm pad. Sides are now decomposed and fanned out
# individually, outward from the PART centre (not the board centre — that rule
# is right for a connector on a board edge, wrong for one side of a quad pack).

def _quad_padmap(per_side=12, pitch=0.5, cx=20.0, cy=15.0, half=3.5,
                 plane_net=None):
    """An LQFP-48-alike: `per_side` pads on each of four edges, each pad long
    on the axis pointing out of its own side."""
    pads, n = {}, 0
    span = (per_side - 1) * pitch / 2.0
    for side in ("L", "B", "R", "T"):
        for i in range(per_side):
            off = i * pitch - span
            x, y = ((cx - half, cy + off) if side == "L" else
                    (cx + half, cy + off) if side == "R" else
                    (cx + off, cy - half) if side == "B" else
                    (cx + off, cy + half))
            vertical_side = side in ("L", "R")
            n += 1
            pid = f"cn1_{n}"
            pads[pid] = PadInfo(
                port_id=pid, designator="CN1", pin_number=n,
                net_id=plane_net if (plane_net and n % 6 == 0) else f"sig_{n}",
                x_mm=x, y_mm=y,
                pad_width_mm=1.475 if vertical_side else 0.3,
                pad_height_mm=0.3 if vertical_side else 1.475,
                layer="top")
    return pads


class TestQuadPack:
    def test_all_four_sides_escape(self):
        pm = _quad_padmap()
        out = generate_escape_routing(_placement(), _netlist_for(pm), pad_map=pm)
        xs = [p.x_mm for p in pm.values()]
        ys = [p.y_mm for p in pm.values()]
        minx, maxx, miny, maxy = min(xs), max(xs), min(ys), max(ys)
        left = [v for v in out["vias"] if v["x_mm"] < minx - 1e-6]
        right = [v for v in out["vias"] if v["x_mm"] > maxx + 1e-6]
        below = [v for v in out["vias"] if v["y_mm"] < miny - 1e-6]
        above = [v for v in out["vias"] if v["y_mm"] > maxy + 1e-6]
        for name, side in (("left", left), ("right", right),
                           ("below", below), ("above", above)):
            assert len(side) >= 10, f"{name} side barely escaped: {len(side)}"
        # Every pad escapes, and outward — nothing fans into the pad field.
        assert len(out["vias"]) == 48
        assert len(left) + len(right) + len(below) + len(above) == 48

    def test_quad_vias_are_collision_free(self):
        """Opposite sides must clear each other's vias — they share one list."""
        cfg = EscapeConfig()
        pm = _quad_padmap()
        out = generate_escape_routing(_placement(), _netlist_for(pm),
                                      config=cfg, pad_map=pm)
        centers = [(v["x_mm"], v["y_mm"]) for v in out["vias"]]
        min_sep = cfg.via_diameter_mm + cfg.clearance_mm
        for i in range(len(centers)):
            for j in range(i + 1, len(centers)):
                d = math.hypot(centers[i][0] - centers[j][0],
                               centers[i][1] - centers[j][1])
                assert d >= min_sep - 1e-6, f"vias {i},{j} too close: {d:.3f}"

    def test_plane_pins_drop_to_the_plane(self):
        """The reason for this work: a quad pack's power pins reach the plane."""
        pm = _quad_padmap(plane_net="VCC3V3")
        out = generate_escape_routing(
            _placement(), _netlist_for(pm), pad_map=pm,
            exclude_nets=("VCC3V3",),
            config=EscapeConfig(num_layers=4, plane_layers=2))
        plane_vias = [v for v in out["vias"] if v["net_id"] == "VCC3V3"]
        assert len(plane_vias) == 8, "every VCC3V3 pin needs its own plane via"
        # Through-via: passes both inner planes, connects to the one on its net
        # (In2=power here — a drop to In1 alone would leave the pin unrouted).
        assert all((v["from_layer"], v["to_layer"]) == ("top", "bottom")
                   for v in plane_vias)
        # Plane pins get no fanout (the plane makes the connection) but do get
        # keepouts, since the autorouter never sees this excluded net.
        assert not [t for t in _fanouts(out) if t["net_id"] == "VCC3V3"]
        assert out["keepouts"]

    def test_part_in_a_corner_skips_the_offboard_side(self):
        """No room outward → leave those pins to the autorouter, don't route
        off the board."""
        pm = _quad_padmap(cx=4.0, cy=15.0)
        out = generate_escape_routing(_placement(), _netlist_for(pm), pad_map=pm)
        assert all(v["x_mm"] > 0 for v in out["vias"])
        assert out["vias"], "the other three sides still escape"
