"""Tests for connector fanout orientation (enhancement D) and the two-sided
flip guard that the morgan/CN1 mis-typing exposed.

D orients edge connectors so their pin row runs *along* the board edge (pins
fan into open area) rather than poking into the board. The flip guard prevents
a mis-typed high-pin / fine-pitch part (e.g. a 30-pin FPC labelled "capacitor")
from ever being sent to the bottom side.
"""

from optimizers.initial_placement import _connector_rotation, ORIENT_MIN_PINS
from optimizers.placement_optimizer import optimize_placement, SAConfig


def _comp(cid, des, ctype, pkg, n):
    elems = [{"element_type": "component", "component_id": cid,
              "designator": des, "component_type": ctype, "value": "x",
              "package": pkg}]
    for p in range(1, n + 1):
        elems.append({"element_type": "port", "port_id": f"{cid}_{p}",
                      "component_id": cid, "pin_number": p, "name": str(p),
                      "electrical_type": "passive"})
    return elems


class TestConnectorOrientation:
    def test_wide_high_pin_connector_rotated_vertical(self):
        """A wide (long-axis-x) connector with enough pins is rotated 90° so its
        pad row runs vertically along the left edge."""
        assert _connector_rotation(w=20.0, h=4.0, pins=ORIENT_MIN_PINS) == 90
        assert _connector_rotation(w=20.0, h=4.0, pins=30) == 90

    def test_small_connector_left_alone(self):
        """Few-pin connectors are NOT reoriented even when wide — reorienting
        terminal blocks regressed routing (rs485, 4ch)."""
        assert _connector_rotation(w=15.0, h=2.0, pins=6) == 0
        assert _connector_rotation(w=15.0, h=2.0, pins=ORIENT_MIN_PINS - 1) == 0

    def test_tall_connector_not_rotated(self):
        """A connector already taller than wide stays at 0 (long axis already
        vertical), regardless of pin count."""
        assert _connector_rotation(w=4.0, h=20.0, pins=30) == 0


class TestFlipGuard:
    def _board_with_mistyped_fpc(self):
        # A 30-pin 0.5mm FPC mislabelled "capacitor" + a couple of real 0402s.
        elems = _comp("c_cn1", "CN1", "capacitor",
                      "Connector_FFC-FPC:Hirose_FH35-30S-0.5SV_1x30-1MP", 30)
        elems += _comp("c_c1", "C1", "capacitor", "C_0402_1005Metric", 2)
        elems += _comp("c_c2", "C2", "capacitor", "C_0402_1005Metric", 2)
        netlist = {"version": "1.0", "project_name": "t", "elements": elems}
        placements = [
            {"designator": "CN1", "component_type": "capacitor",
             "package": "Connector_FFC-FPC:Hirose_FH35-30S-0.5SV_1x30-1MP",
             "x_mm": 20, "y_mm": 20, "rotation_deg": 0, "layer": "top",
             "footprint_width_mm": 18, "footprint_height_mm": 4},
            {"designator": "C1", "component_type": "capacitor",
             "package": "C_0402_1005Metric", "x_mm": 5, "y_mm": 40,
             "rotation_deg": 0, "layer": "top",
             "footprint_width_mm": 1, "footprint_height_mm": 0.5},
            {"designator": "C2", "component_type": "capacitor",
             "package": "C_0402_1005Metric", "x_mm": 8, "y_mm": 40,
             "rotation_deg": 0, "layer": "top",
             "footprint_width_mm": 1, "footprint_height_mm": 0.5},
        ]
        placement = {"version": "1.0", "project_name": "t",
                     "board": {"width_mm": 50, "height_mm": 50},
                     "placements": placements}
        return placement, netlist

    def test_mistyped_high_pin_part_never_flips(self):
        placement, netlist = self._board_with_mistyped_fpc()
        for seed in range(4):
            out = optimize_placement(placement, netlist,
                                     SAConfig(seed=seed, two_sided=True,
                                              congestion_weight=2.0))
            cn1 = next(p for p in out["placements"] if p["designator"] == "CN1")
            assert cn1["layer"] == "top", (
                f"30-pin fine-pitch FPC was flipped to {cn1['layer']} (seed {seed})")
