"""4-layer stackup options: inner_plane_count + DSN routing-layer selection.

plane_layers controls how many inner layers are solid planes vs signal:
  2 (default) = In1 GND + In2 power planes, route on F.Cu/B.Cu
  1           = In1 GND plane only, In2.Cu is a 3rd SIGNAL routing layer
  0           = all inner layers signal
"""

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
