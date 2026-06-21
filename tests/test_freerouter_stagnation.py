"""Tests for the Freerouting plateau/oscillation guard (_should_stop_for_stagnation).

Freerouting 2.1.0 has no stagnation detection, so on a board it can't fully
route it oscillates the unrouted count until the pass budget is exhausted —
minutes of churn that never converges. The guard stops it early once the best
count plateaus, preferring to stop when the current pass re-hits that best.
"""

from optimizers.freerouter import _should_stop_for_stagnation as stop

T = 8  # threshold used in these cases


def test_disabled_never_stops():
    assert stop(50, 1, 9, 9, 0) is False


def test_fully_routing_never_stops():
    # best == 0 means a complete route is in hand; keep optimizing/finishing.
    assert stop(40, 2, 0, 0, T) is False


def test_no_pass_yet_never_stops():
    assert stop(None, None, None, None, T) is False


def test_within_window_does_not_stop():
    # Only 3 passes since the best — too soon.
    assert stop(8, 5, 9, 30, T) is False


def test_stops_when_current_rehits_best():
    # 8 passes since best (pass 5) and the current pass is back at the best (9)
    # — a good moment to flush the partial.
    assert stop(13, 5, 9, 9, T) is True


def test_does_not_stop_at_window_if_current_is_worse():
    # Window reached, but the current pass is a bad oscillation peak — wait for a
    # better one rather than capturing the peak.
    assert stop(13, 5, 9, 42, T) is False


def test_force_stops_at_double_window():
    # Oscillation never re-hits the best; the 2× hard cap still terminates it.
    assert stop(21, 5, 9, 42, T) is True


def test_improving_route_resets_window():
    # If the best just improved (best_pass close to pass_num), the gap is small
    # and we keep going — the guard only fires on a genuine plateau.
    assert stop(16, 15, 5, 12, T) is False
