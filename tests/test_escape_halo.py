"""Tests for the escape-halo / fanout reservation placement term (enhancement A)
and the focus-component lever the routing-feedback retry uses (enhancement C).

The term reserves a clear fanout channel around dense / fine-pitch parts by
penalizing foreign pads that intrude into a halo sized to the part's fanout
demand. It self-gates: ordinary boards produce no halos, so it is a no-op.
"""

import math

from optimizers.placement_optimizer import (
    SAConfig,
    optimize_placement,
    _build_escape_halos,
    _escape_halo_cost,
    _footprint_min_pitch,
)
from optimizers.ratsnest import build_connectivity


# ---------------------------------------------------------------------------
# Netlist / placement builders
# ---------------------------------------------------------------------------

def _resistor_chain(n):
    elements = []
    for i in range(1, n + 1):
        cid = f"comp_r{i}"
        elements.append({"element_type": "component", "component_id": cid,
                         "designator": f"R{i}", "component_type": "resistor",
                         "value": "1k", "package": "0805"})
        for p in (1, 2):
            elements.append({"element_type": "port", "port_id": f"port_r{i}_{p}",
                             "component_id": cid, "pin_number": p,
                             "name": str(p), "electrical_type": "passive"})
    for i in range(1, n):
        elements.append({"element_type": "net", "net_id": f"net_{i}",
                         "name": f"N{i}",
                         "connected_port_ids": [f"port_r{i}_2", f"port_r{i + 1}_1"],
                         "net_class": "signal"})
    return {"version": "1.0", "project_name": "t", "elements": elements}


# ---------------------------------------------------------------------------
# _build_escape_halos
# ---------------------------------------------------------------------------

class TestBuildHalos:
    def test_simple_board_has_no_halos(self):
        """A board of only 2-pin passives qualifies nothing → no-op term."""
        netlist = _resistor_chain(5)
        nets = build_connectivity(netlist)
        packages = {f"R{i}": ("0805", 2) for i in range(1, 6)}
        footprints = {f"R{i}": (2.0, 1.25) for i in range(1, 6)}
        assert _build_escape_halos(nets, packages, footprints, SAConfig()) == {}

    def test_high_pin_ic_gets_halo_bigger_than_body(self):
        packages = {"U1": ("SOIC-8", 8), "R1": ("0805", 2)}
        footprints = {"U1": (5.0, 4.0), "R1": (2.0, 1.25)}
        halos = _build_escape_halos([], packages, footprints, SAConfig())
        assert "U1" in halos and "R1" not in halos
        # Halo extends past the body half-size (max(5,4)/2 = 2.5) by the ring.
        assert halos["U1"] > 2.5

    def test_more_fanout_demand_means_bigger_halo(self):
        """The annulus grows with pin count / leaving-net count."""
        small = _build_escape_halos(
            [], {"U1": ("SOIC-8", 8)}, {"U1": (5.0, 4.0)}, SAConfig())
        big = _build_escape_halos(
            [], {"U1": ("LQFP-48", 48)}, {"U1": (5.0, 4.0)}, SAConfig())
        assert big["U1"] > small["U1"]

    def test_focus_component_forces_halo(self):
        """A low-pin part not intrinsically dense still gets a reserved halo
        when named in focus_components (the routing-feedback lever)."""
        packages = {"D1": ("SOD-123", 2)}
        footprints = {"D1": (2.0, 1.5)}
        assert _build_escape_halos([], packages, footprints, SAConfig()) == {}
        focused = _build_escape_halos(
            [], packages, footprints, SAConfig(focus_components=("D1",)))
        assert "D1" in focused and focused["D1"] >= 1.0


# ---------------------------------------------------------------------------
# _escape_halo_cost
# ---------------------------------------------------------------------------

class TestHaloCost:
    PACKAGES = {"U1": ("SOIC-8", 8), "R1": ("0805", 2)}
    HALOS = {"U1": 5.0}

    def test_intrusion_penalized_depth_scaled(self):
        th = {"U1": False, "R1": False}
        layers = {"U1": "top", "R1": "top"}
        near = _escape_halo_cost({"U1": (10, 10), "R1": (11, 10)},
                                 self.PACKAGES, layers, self.HALOS, th)
        edge = _escape_halo_cost({"U1": (10, 10), "R1": (14, 10)},
                                 self.PACKAGES, layers, self.HALOS, th)
        outside = _escape_halo_cost({"U1": (10, 10), "R1": (20, 10)},
                                    self.PACKAGES, layers, self.HALOS, th)
        assert near > edge > 0
        assert outside == 0

    def test_opposite_side_smd_does_not_contend(self):
        th = {"U1": False, "R1": False}
        layers = {"U1": "top", "R1": "bottom"}
        assert _escape_halo_cost({"U1": (10, 10), "R1": (11, 10)},
                                 self.PACKAGES, layers, self.HALOS, th) == 0

    def test_through_hole_dense_part_blocks_both_sides(self):
        th = {"U1": True, "R1": False}  # U1 is now through-hole
        layers = {"U1": "top", "R1": "bottom"}
        assert _escape_halo_cost({"U1": (10, 10), "R1": (11, 10)},
                                 self.PACKAGES, layers, self.HALOS, th) > 0


class TestFootprintPitch:
    def test_known_fine_pitch(self):
        pitch = _footprint_min_pitch("SOIC-8", 8)
        assert pitch is not None and pitch > 0


# ---------------------------------------------------------------------------
# End-to-end: the term pushes foreign parts out of a dense part's halo
# ---------------------------------------------------------------------------

class TestOptimizeBehaviour:
    def _board_with_ic_and_loose_passives(self):
        """U1 pinned at centre; 6 unconnected passives sit *inside* its escape
        halo but in valid (non-overlapping) spots. With no wire/grouping pull on
        them, only the escape term acts, so it is a clean read on whether the
        halo pushes intruders out. A wide fanout pitch (escape_track_pitch_mm
        below) inflates the annulus well past the body so there is room to move."""
        elements = [{"element_type": "component", "component_id": "comp_u1",
                     "designator": "U1", "component_type": "ic", "value": "x",
                     "package": "SOIC-8"}]
        for p in range(1, 9):
            elements.append({"element_type": "port", "port_id": f"port_u1_{p}",
                             "component_id": "comp_u1", "pin_number": p,
                             "name": str(p), "electrical_type": "passive"})
        netlist = {"version": "1.0", "project_name": "t", "elements": elements}

        placements = [{"designator": "U1", "package": "SOIC-8",
                       "component_type": "ic", "x_mm": 30, "y_mm": 30,
                       "rotation_deg": 0, "layer": "top",
                       "footprint_width_mm": 5.0, "footprint_height_mm": 4.0,
                       "placement_source": "user"}]  # pinned → fixed halo centre
        # ~6mm from centre: clear of U1's body/pads (valid) but inside the halo.
        ring = [(36, 30), (24, 30), (30, 36), (30, 24), (34.2, 34.2), (25.8, 34.2)]
        for i, (x, y) in enumerate(ring, 1):
            placements.append({"designator": f"R{i}", "package": "0805",
                               "component_type": "resistor", "x_mm": x, "y_mm": y,
                               "rotation_deg": 0, "layer": "top",
                               "footprint_width_mm": 2.0,
                               "footprint_height_mm": 1.25})
        placement = {"version": "1.0", "project_name": "t",
                     "board": {"width_mm": 60, "height_mm": 60},
                     "placements": placements}
        return placement, netlist

    @staticmethod
    def _min_dist_to_u1(result):
        u1 = next(p for p in result["placements"] if p["designator"] == "U1")
        ux, uy = u1["x_mm"], u1["y_mm"]
        return min(math.hypot(p["x_mm"] - ux, p["y_mm"] - uy)
                   for p in result["placements"] if p["designator"] != "U1")

    def test_escape_term_spreads_intruders(self):
        placement, netlist = self._board_with_ic_and_loose_passives()
        # Average over several seeds to take the stochastic edge off.
        off = on = 0.0
        seeds = range(5)
        for s in seeds:
            r_off = optimize_placement(placement, netlist,
                                       SAConfig(seed=s, escape_weight=0.0,
                                                escape_track_pitch_mm=5.0))
            r_on = optimize_placement(placement, netlist,
                                      SAConfig(seed=s, escape_weight=12.0,
                                               escape_track_pitch_mm=5.0))
            off += self._min_dist_to_u1(r_off)
            on += self._min_dist_to_u1(r_on)
        n = len(list(seeds))
        assert on / n > off / n, (
            f"escape term should push intruders out: on={on / n:.2f} "
            f"off={off / n:.2f}")
