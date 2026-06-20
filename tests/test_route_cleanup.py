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
                {"description": "PTH pad 1 [GATE_Q3] of Q3"}]},
            {"type": "clearance", "items": [
                {"description": "Track [IGNORED] ..."}]}]}
        assert _parse_shorting_net_names(data) == {"FB_DIV", "GATE_Q3"}

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


class TestCleanupLoop:
    def test_skips_when_drc_unavailable(self):
        routed = _routed([_t("a", 0, 0, 1, 0)])
        out, bad = cleanup_shorts(
            routed, _netlist("a"), escapes={"traces": [], "vias": [], "keepouts": []},
            route_fn=lambda f: (_ for _ in ()).throw(AssertionError("must not route")),
            drc_net_names_fn=lambda r: None)
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
            drc_net_names_fn=lambda r: drc[id(r)])
        assert out is routed1 and bad == set() and calls["n"] == 1

    def test_keeps_previous_when_no_improvement(self):
        routed0 = _routed([_t("a", 0, 0, 1, 0)])
        routed1 = _routed([_t("a", 0, 0, 1, 0)])
        drc = {id(routed0): {"a"}, id(routed1): {"a"}}  # still shorting
        out, bad = cleanup_shorts(
            routed0, _netlist("a"),
            escapes={"traces": [], "vias": [], "keepouts": []},
            route_fn=lambda f: routed1,
            drc_net_names_fn=lambda r: drc[id(r)])
        assert out is routed0  # candidate not better → keep original

    def test_excluded_nets_not_targeted(self):
        routed = _routed([_t("GND", 0, 0, 1, 0)])
        out, bad = cleanup_shorts(
            routed, _netlist("GND"),
            escapes={"traces": [], "vias": [], "keepouts": []},
            exclude_nets=("GND",),
            route_fn=lambda f: (_ for _ in ()).throw(AssertionError("no reroute")),
            drc_net_names_fn=lambda r: {"GND"})
        assert out is routed and bad == set()
