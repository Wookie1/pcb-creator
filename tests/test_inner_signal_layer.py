"""4-layer stackup options: inner_plane_count + DSN routing-layer selection.

plane_layers controls how many inner layers are solid planes vs signal:
  2 (default) = In1 GND + In2 power planes, route on F.Cu/B.Cu
  1           = In1 GND plane only, In2.Cu is a 3rd SIGNAL routing layer
  0           = all inner layers signal
"""

import json

import pytest

from optimizers.router import inner_plane_count
from exporters.dsn_exporter import _dsn_structure


class TestInnerPlaneCount:
    def test_two_layer_has_no_planes(self):
        assert inner_plane_count({"layers": 2}) == 0

    def test_four_layer_defaults_to_two_planes(self):
        assert inner_plane_count({"layers": 4}) == 2

    def test_explicit_one_plane(self):
        assert inner_plane_count({"layers": 4, "plane_layers": 1}) == 1

    def test_explicit_zero_planes(self):
        assert inner_plane_count({"layers": 4, "plane_layers": 0}) == 0

    def test_clamped(self):
        assert inner_plane_count({"layers": 4, "plane_layers": 5}) == 2
        assert inner_plane_count({"layers": 4, "plane_layers": -1}) == 0


class TestDsnRoutingLayers:
    def _layers_in_structure(self, num_layers, plane_layers):
        board = {"width_mm": 30, "height_mm": 20, "layers": num_layers}
        cfg = {"plane_layers": plane_layers}
        s = _dsn_structure(board, cfg)
        import re
        return [m for m in re.findall(r'\(layer "([^"]+)" \(type signal\)\)', s)]

    def test_default_4layer_routes_outer_only(self):
        # plane_layers=2: only F.Cu/B.Cu are routable signal layers
        sig = self._layers_in_structure(4, 2)
        assert sig == ["F.Cu", "B.Cu"]

    def test_one_plane_exposes_inner2_signal(self):
        # plane_layers=1: In2.Cu joins the routable signal layers
        sig = self._layers_in_structure(4, 1)
        assert "In2.Cu" in sig and "F.Cu" in sig and "B.Cu" in sig
        assert "In1.Cu" not in sig  # In1 is the GND plane

    def test_zero_planes_all_signal(self):
        sig = self._layers_in_structure(4, 0)
        assert set(sig) == {"F.Cu", "In1.Cu", "In2.Cu", "B.Cu"}

    def test_two_layer_unaffected(self):
        sig = self._layers_in_structure(2, 0)
        assert sig == ["F.Cu", "B.Cu"]


class TestInnerSignalTraceFill:
    """Regression: with plane_layers=1, Freerouting routes signal traces on
    In2.Cu (inner2). The copper-fill grid only models outer layers, so it must
    skip inner-layer traces rather than KeyError on them (the 'inner2' crash
    seen on the morgan board)."""

    def test_apply_copper_fills_tolerates_inner_signal_trace(self):
        from optimizers.router import apply_copper_fills, RouterConfig
        netlist = {"version": "1.0", "project_name": "t", "elements": [
            {"element_type": "component", "component_id": "comp_u1",
             "designator": "U1", "component_type": "ic", "value": "x",
             "package": "SOIC-8"},
            {"element_type": "port", "port_id": "port_u1_1",
             "component_id": "comp_u1", "pin_number": 1, "name": "OUT",
             "electrical_type": "signal"},
            {"element_type": "net", "net_id": "net_sig", "name": "SIG",
             "connected_port_ids": ["port_u1_1"], "net_class": "signal"},
            {"element_type": "net", "net_id": "net_gnd", "name": "GND",
             "connected_port_ids": [], "net_class": "ground"}]}
        routed = {"version": "1.0", "project_name": "t",
                  "board": {"width_mm": 30, "height_mm": 20, "layers": 4,
                            "plane_layers": 1},
                  "placements": [{"designator": "U1", "package": "SOIC-8",
                                  "component_type": "ic", "x_mm": 15, "y_mm": 10,
                                  "rotation_deg": 0, "layer": "top",
                                  "footprint_width_mm": 5, "footprint_height_mm": 4}],
                  "routing": {"traces": [
                      {"net_id": "net_sig", "net_name": "SIG", "layer": "inner2",
                       "width_mm": 0.127, "start_x_mm": 5, "start_y_mm": 5,
                       "end_x_mm": 15, "end_y_mm": 5}],
                      "vias": [], "unrouted_nets": []}}
        # Must not raise KeyError('inner2')
        out = apply_copper_fills(routed, netlist, RouterConfig())
        # inner2 trace preserved; only the In1 GND plane (not In2) generated
        assert any(t["layer"] == "inner2" for t in out["routing"]["traces"])
        fills = {f["layer"] for f in out["routing"].get("copper_fills", [])}
        assert "inner1" in fills and "inner2" not in fills


class TestLayerCountPlumbing:
    """run_placement must let the caller set the layer count and must never
    silently ignore plane_layers. Regression for morgan_carrier_v14, which was
    routed/exported as 2-layer despite plane_layers=0 because plane_layers had
    no `layers` to attach to — over-cramming a 4-layer design onto 2 layers."""

    _NETLIST = {"version": "1.0", "project_name": "t", "elements": [
        {"element_type": "component", "component_id": "comp_u1",
         "designator": "U1", "component_type": "ic", "value": "x",
         "package": "SOIC-8"},
        {"element_type": "port", "port_id": "port_u1_1",
         "component_id": "comp_u1", "pin_number": 1, "name": "OUT",
         "electrical_type": "signal"},
        {"element_type": "net", "net_id": "net_sig", "name": "SIG",
         "connected_port_ids": ["port_u1_1"], "net_class": "signal"}]}

    def _place(self, tmp_path, **kw):
        from orchestrator import stages
        (tmp_path / "t_netlist.json").write_text(json.dumps(self._NETLIST))
        r = stages.run_placement(tmp_path, "t", object(),
                                 board_width_mm=40, board_height_mm=30, **kw)
        assert r["success"], r.get("error")
        board = json.loads((tmp_path / "t_placement.json").read_text())["board"]
        return r, board

    def test_default_is_two_layer(self, tmp_path):
        r, board = self._place(tmp_path)
        assert r["layers"] == 2 and board["layers"] == 2
        assert not r["layers_promoted"]

    def test_explicit_four_layer(self, tmp_path):
        r, board = self._place(tmp_path, layers=4)
        assert r["layers"] == 4 and board["layers"] == 4
        assert board["plane_layers"] == 2  # default stackup
        assert not r["layers_promoted"]

    def test_plane_layers_promotes_to_four(self, tmp_path):
        # The morgan case: plane_layers=0 on a would-be 2-layer board.
        r, board = self._place(tmp_path, plane_layers=0)
        assert r["layers"] == 4 and board["layers"] == 4
        assert board["plane_layers"] == 0
        assert r["layers_promoted"] is True
