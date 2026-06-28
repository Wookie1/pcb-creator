"""Unit tests for the single-source-of-truth routed-board accessors."""

from optimizers.routed_board import routing_stats, routing_completion


def _board(**stats):
    return {"routing": {"statistics": stats}}


def test_routing_stats_reads_nested_block():
    s = _board(completion_pct=100.0, total_nets=9, routed_nets=9, via_count=3)
    assert routing_stats(s) == {
        "completion_pct": 100.0, "total_nets": 9, "routed_nets": 9, "via_count": 3,
    }


def test_routing_stats_empty_when_no_routing_block():
    assert routing_stats({}) == {}
    assert routing_stats({"statistics": {"total_nets": 9}}) == {}  # top-level ignored


def test_routing_stats_tolerates_non_dict():
    assert routing_stats(None) == {}
    assert routing_stats("nope") == {}


def test_routing_completion_reads_and_defaults():
    assert routing_completion(_board(completion_pct=87.5)) == 87.5
    assert routing_completion({}) == 0.0
    assert routing_completion(_board()) == 0.0   # stats present, key absent
