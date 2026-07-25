"""Regression tests: routing stats must be read from routed["routing"]["statistics"].

A live pipeline run surfaced a reporting bug — a 100%-routed board was reported
as 0% because three call sites read the TOP level (routed["statistics"], always
absent) instead of the nested routing block. Symptoms: `cli run --json-output`
showed total_nets=0, and the vision pre-check logged "0.0% routed — escalating
to human review" on a fully-routed board.

These tests pin the nesting using small constructed boards (no LLM, no routing
engine), so they're fast and won't go stale: they assert the value flows from
the canonical location, not any log text or end-to-end output.
"""

from orchestrator.cli import routing_stats_summary
from orchestrator.vision_review import format_review_context, run_vision_review
from orchestrator.config import OrchestratorConfig


def _routed(completion=100.0, total=9, routed=9, vias=3):
    """A routed-board dict shaped like what the pipeline actually writes."""
    return {
        "routing": {
            "traces": [],
            "vias": [],
            "unrouted_nets": [],
            "statistics": {
                "completion_pct": completion,
                "total_nets": total,
                "routed_nets": routed,
                "via_count": vias,
                "total_trace_length_mm": 171.3,
            },
        }
    }


# --- cli routing_stats_summary --------------------------------------------

def test_summary_reads_nested_stats():
    assert routing_stats_summary(_routed(100.0, 9, 9, 3)) == {
        "completion_pct": 100.0, "total_nets": 9, "routed_nets": 9, "via_count": 3,
    }


def test_summary_zero_on_board_without_routing_block():
    # Missing/empty routing wrapper -> zeros, no crash (the bug returned this
    # for EVERY board because it looked one level too high).
    assert routing_stats_summary({}) == {
        "completion_pct": 0, "total_nets": 0, "routed_nets": 0, "via_count": 0,
    }
    assert routing_stats_summary({"statistics": {"total_nets": 9}})["total_nets"] == 0


# --- vision_review.format_review_context -----------------------------------

def test_review_context_reports_real_counts():
    routing_text, _ = format_review_context(None, _routed(100.0, 9, 9, 3))
    assert "100.0%" in routing_text
    assert "9/9 nets routed" in routing_text
    assert "0/0" not in routing_text          # the bug rendered 0/0


# --- vision_review.run_vision_review pre-check (returns before any vision) --

def test_precheck_approves_fully_routed_clean_board():
    drc = {"passed": True, "statistics": {"errors": 0}}
    decision = run_vision_review(_routed(100.0, 9, 9, 3), None, None, drc,
                                 OrchestratorConfig())
    assert decision == "approved"             # was wrongly "escalated"


def test_precheck_still_escalates_genuinely_unrouted_board():
    drc = {"passed": True, "statistics": {"errors": 0}}
    decision = run_vision_review(_routed(50.0, 9, 4, 1), None, None, drc,
                                 OrchestratorConfig())
    assert decision == "escalated"            # real <100% must still escalate
