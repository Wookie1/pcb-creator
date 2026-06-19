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
        # ...dropped to the GND plane (In1)...
        v1 = next(v for v in out["vias"] if v["net_id"] == "sig_1")
        assert v1["to_layer"] == "inner1"
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
