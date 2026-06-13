"""Tests for the routing-demand / RUDY congestion term (enhancement B).

B spreads each signal net's estimated wire over its bounding box and penalizes
grid cells whose summed demand exceeds the track capacity they could carry. It
is correct and self-gating (no-op below capacity) but OFF by default — see the
SAConfig.demand_weight comment for why. These tests lock in the cost function's
behaviour directly so it is ready to enable when a congestion-limited board
exists to tune against.
"""

import math

from optimizers.placement_optimizer import (
    _routing_demand_cost,
    DEMAND_CELL_MM,
    DEMAND_SIGNAL_LAYERS,
    DEMAND_UTILIZATION_LIMIT,
)
from optimizers.ratsnest import NetInfo


def _net(net_id, designators, net_class="signal"):
    return NetInfo(net_id=net_id, name=net_id, net_class=net_class,
                   designators=list(designators))


def _capacity(track_pitch):
    return (DEMAND_CELL_MM / track_pitch) * DEMAND_CELL_MM \
        * DEMAND_SIGNAL_LAYERS * DEMAND_UTILIZATION_LIMIT


class TestDemandCost:
    def test_sparse_board_is_noop(self):
        """A couple of well-separated nets never exceed capacity → cost 0."""
        positions = {"A": (0, 0), "B": (3, 0), "C": (40, 40), "D": (43, 40)}
        nets = [_net("n1", ["A", "B"]), _net("n2", ["C", "D"])]
        assert _routing_demand_cost(positions, nets, track_pitch_mm=0.4) == 0.0

    def test_many_overlapping_nets_penalized(self):
        """Many nets crammed into one cell exceed capacity → positive cost."""
        # 60 two-pad nets all spanning the same ~2mm region (one cell).
        positions = {}
        nets = []
        for i in range(60):
            a, b = f"a{i}", f"b{i}"
            positions[a] = (1.0, 1.0)
            positions[b] = (2.0, 1.0)
            nets.append(_net(f"n{i}", [a, b]))
        cost = _routing_demand_cost(positions, nets, track_pitch_mm=0.4)
        assert cost > 0.0

    def test_cost_increases_with_crowding(self):
        """Doubling the nets through one cell raises the penalty."""
        def build(n):
            pos, nets = {}, []
            for i in range(n):
                a, b = f"a{i}", f"b{i}"
                pos[a] = (1.0, 1.0); pos[b] = (2.0, 1.0)
                nets.append(_net(f"n{i}", [a, b]))
            return pos, nets
        p1, n1 = build(60)
        p2, n2 = build(120)
        c1 = _routing_demand_cost(p1, n1, track_pitch_mm=0.4)
        c2 = _routing_demand_cost(p2, n2, track_pitch_mm=0.4)
        assert c2 > c1 > 0.0

    def test_plane_nets_excluded_by_caller(self):
        """The term trusts the caller to pre-filter plane nets; given only
        signal nets it rasterizes them, given none it is a no-op."""
        positions = {f"p{i}": (1.0, 1.0) for i in range(40)}
        positions.update({f"q{i}": (2.0, 1.0) for i in range(40)})
        signal = [_net(f"n{i}", [f"p{i}", f"q{i}"]) for i in range(40)]
        assert _routing_demand_cost(positions, signal, track_pitch_mm=0.4) > 0.0
        assert _routing_demand_cost(positions, [], track_pitch_mm=0.4) == 0.0

    def test_single_pin_nets_ignored(self):
        """A net with <2 placed pads contributes no demand."""
        positions = {"A": (1.0, 1.0)}
        assert _routing_demand_cost(positions, [_net("n", ["A"])],
                                    track_pitch_mm=0.4) == 0.0
