"""Tests for the short-cleanup pass (optimizers.route_cleanup).

The rip/preserve/accept logic is dependency-injected (fake route_fn /
drc_net_names_fn), so it's exercised without Freerouting or KiCad.
"""

from optimizers.route_cleanup import (
    build_protected_wiring, cleanup_shorts, _parse_shorting_net_names,
)


def _routed(traces, vias=None):
    return {"board": {"layers": 4},
            "routing": {"traces": traces, "vias": vias or [], "unrouted_nets": []}}


def _t(net, sx, sy, ex, ey, layer="top", role=None):
    d = {"net_id": net, "net_name": net, "layer": layer,
         "start_x_mm": sx, "start_y_mm": sy, "end_x_mm": ex, "end_y_mm": ey,
         "width_mm": 0.127}
    if role:
        d["escape_role"] = role
    return d


def _netlist(*net_ids):
    els = []
    for n in net_ids:
        els.append({"element_type": "net", "net_id": n, "name": n,
                    "connected_port_ids": []})
    return {"version": "1.0", "elements": els}


class TestParse:
    def test_extracts_first_bracketed_token(self):
        data = {"violations": [
            {"type": "shorting_items", "items": [
                {"description": "Track [FB_DIV] on F.Cu, length 7.8 mm"},
                {"description": "PTH pad 1 [GATE_Q3] of Q3"}]}]}
        assert _parse_shorting_net_names(data) == {"FB_DIV", "GATE_Q3"}

    def test_clearance_nets_are_collected(self):
        # Clearance violations are now reroute-fixable: BOTH involved nets are
        # collected so re-routing either resolves the clearance.
        data = {"violations": [
            {"type": "clearance", "items": [
                {"description": "Via [5V] at (1, 2)"},
                {"description": "Track [GND] on F.Cu"}]},
            {"type": "via_clearance", "items": [
                {"description": "Via [3V3] at (3, 4)"}]}]}
        assert _parse_shorting_net_names(data) == {"5V", "GND", "3V3"}

    def test_non_reroutable_types_ignored(self):
        # hole_to_hole / silk are not fixable by re-routing a net.
        data = {"violations": [
            {"type": "hole_to_hole", "items": [{"description": "Via [GND] at x"}]},
            {"type": "silk_over_copper", "items": [{"description": "Text [X]"}]}]}
        assert _parse_shorting_net_names(data) == set()

    def test_no_shorts(self):
        assert _parse_shorting_net_names({"violations": []}) == set()


class TestBuildProtectedWiring:
    def test_preserves_escapes_rips_bad_onward(self):
        escapes = {
            "traces": [_t("sig1", 0, 0, 1, 0, role="stub"),
                       _t("sig1", 1, 0, 3, 0, layer="inner2", role="fanout")],
            "vias": [{"x_mm": 1, "y_mm": 0, "net_id": "sig1", "diameter_mm": 0.45}],
            "keepouts": [{"x_mm": 9, "y_mm": 9, "diameter_mm": 0.5}]}
        routed = _routed(
            traces=[
                _t("sig1", 0, 0, 1, 0, role="stub"),       # escape stub (keep)
                _t("sig1", 1, 0, 3, 0, layer="inner2", role="fanout"),  # escape (keep)
                _t("sig1", 3, 0, 9, 0, layer="inner2"),    # bad-net ONWARD (rip)
                _t("good", 5, 5, 6, 6)],                   # good net (keep)
            vias=[{"x_mm": 1, "y_mm": 0, "net_id": "sig1", "diameter_mm": 0.45}])
        fixed = build_protected_wiring(routed, escapes, {"sig1"})
        layers = [(t["net_id"], t.get("escape_role"),
                   round(t["end_x_mm"], 1)) for t in fixed["traces"]]
        # sig1's escape stub + fanout survive; its onward (end_x=9) is gone
        assert ("sig1", "stub", 1.0) in layers
        assert ("sig1", "fanout", 3.0) in layers
        assert ("sig1", None, 9.0) not in layers
        # good net preserved; keepouts carried
        assert any(t["net_id"] == "good" for t in fixed["traces"])
        assert fixed["keepouts"] == escapes["keepouts"]

    def test_no_escapes_just_rips_bad(self):
        empty = {"traces": [], "vias": [], "keepouts": []}
        routed = _routed([_t("bad", 0, 0, 1, 0), _t("ok", 2, 2, 3, 3)])
        fixed = build_protected_wiring(routed, empty, {"bad"})
        nets = {t["net_id"] for t in fixed["traces"]}
        assert nets == {"ok"}


def _drc(names, *, pos=None):
    """Minimal kicad-cli DRC json: one clearance violation per net name."""
    return {"violations": [
        {"type": "clearance",
         "items": [{"description": f"Track [{n}] on F.Cu",
                    **({"pos": pos} if pos else {})}]}
        for n in names]}


class TestCleanupLoop:
    def test_skips_when_drc_unavailable(self):
        routed = _routed([_t("a", 0, 0, 1, 0)])
        out, bad = cleanup_shorts(
            routed, _netlist("a"), escapes={"traces": [], "vias": [], "keepouts": []},
            route_fn=lambda f: (_ for _ in ()).throw(AssertionError("must not route")),
            drc_data_fn=lambda r: None)
        assert out is routed and bad == set()

    def test_reroutes_until_clean(self):
        routed0 = _routed([_t("a", 0, 0, 1, 0), _t("b", 0, 1, 1, 1)])
        routed1 = _routed([_t("a", 0, 0, 1, 0), _t("b", 5, 5, 6, 6)])  # b moved away
        drc = {id(routed0): {"a"}, id(routed1): set()}
        calls = {"n": 0}

        def route_fn(fixed):
            calls["n"] += 1
            return routed1

        out, bad = cleanup_shorts(
            routed0, _netlist("a", "b"),
            escapes={"traces": [], "vias": [], "keepouts": []},
            route_fn=route_fn,
            drc_data_fn=lambda r: _drc(drc[id(r)]))
        assert out is routed1 and bad == set() and calls["n"] == 1

    def test_keeps_previous_when_no_improvement(self):
        routed0 = _routed([_t("a", 0, 0, 1, 0)])
        routed1 = _routed([_t("a", 0, 0, 1, 0)])
        drc = {id(routed0): {"a"}, id(routed1): {"a"}}  # still shorting
        out, bad = cleanup_shorts(
            routed0, _netlist("a"),
            escapes={"traces": [], "vias": [], "keepouts": []},
            route_fn=lambda f: routed1,
            drc_data_fn=lambda r: _drc(drc[id(r)]))
        assert out is routed0  # candidate not better → keep original

    def test_excluded_nets_not_targeted(self):
        routed = _routed([_t("GND", 0, 0, 1, 0)])
        out, bad = cleanup_shorts(
            routed, _netlist("GND"),
            escapes={"traces": [], "vias": [], "keepouts": []},
            exclude_nets=("GND",),
            route_fn=lambda f: (_ for _ in ()).throw(AssertionError("no reroute")),
            drc_data_fn=lambda r: _drc({"GND"}))
        assert out is routed and bad == set()

    def test_keepout_added_at_violation_and_pad_net_protected(self):
        # A short between Track[SDA] and PAD[SCL]: SDA is ripped, SCL is PROTECTED
        # (not ripped), and a keepout is placed at the SCL pad so the re-route of
        # SDA is forced clear of it.
        routed0 = _routed([_t("SDA", 0, 0, 1, 0)])
        routed1 = _routed([_t("SDA", 5, 5, 6, 6)])
        short = {"violations": [{"type": "shorting_items", "items": [
            {"description": "Track [SDA] on B.Cu", "pos": {"x": 1.0, "y": 2.0}},
            {"description": "PTH pad 1 [SCL] of HDR1", "pos": {"x": 1.4, "y": 2.0}}]}]}
        seen = {}

        def route_fn(fixed):
            seen["keepouts"] = fixed.get("keepouts", [])
            seen["trace_nets"] = {t["net_id"] for t in fixed["traces"]}
            return routed1

        out, bad = cleanup_shorts(
            routed0, _netlist("SDA", "SCL"),
            escapes={"traces": [], "vias": [], "keepouts": []},
            route_fn=route_fn,
            drc_data_fn=lambda r: short if r is routed0 else _drc(set()))
        # keepout placed at the SCL PAD position (1.4, 2.0), not the track
        assert any(abs(k["x_mm"] - 1.4) < 1e-6 and abs(k["y_mm"] - 2.0) < 1e-6
                   for k in seen["keepouts"])
        # SCL's wiring stayed protected (it was not in the ripped set)
        assert out is routed1


# --- keepout sizing (THT-pad shorts) -------------------------------------

from optimizers.route_cleanup import (
    _sized_keepout_diameter, _keepout_feature_index,
)


def test_keepout_sized_to_tht_pad_extent():
    # A THT pad spanning 1.7mm at the violation locus must yield a keepout that
    # covers the pad + clearance, not the tiny 0.8mm default that re-clips it.
    feats = [(10.0, 10.0, 1.7)]            # (x, y, extent) — 1.7mm THT pad
    dia = _sized_keepout_diameter(10.0, 10.0, feats, clearance_mm=0.2,
                                  default_mm=0.8)
    assert dia == 1.7 + 2 * 0.2            # pad extent + clearance both sides
    assert dia > 0.8                       # strictly larger than the old default


def test_keepout_falls_back_to_default_off_feature():
    # A trace-vs-trace nick (no pad/via at the locus) keeps the default.
    feats = [(50.0, 50.0, 1.7)]            # far away
    dia = _sized_keepout_diameter(10.0, 10.0, feats, clearance_mm=0.2,
                                  default_mm=0.8)
    assert dia == 0.8


def test_keepout_feature_index_includes_vias():
    routed = {"routing": {"vias": [{"x_mm": 5, "y_mm": 6, "diameter_mm": 0.6}]}}
    feats = _keepout_feature_index(routed, {"elements": []})
    assert (5, 6, 0.6) in feats
