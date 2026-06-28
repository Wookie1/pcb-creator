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


# --- anchored: assert against REAL router output, not a hand-built dict -----
# The tests above use a constructed board; if the pipeline's output shape ever
# drifts, that fixture wouldn't notice. This one routes a real board with the
# built-in router (no LLM, no Java — 2-layer) and asserts the consumer reads the
# same numbers the board actually contains. That's what makes the suite robust
# to producer/consumer drift: the producer here is the real router.

def test_summary_matches_real_router_output():
    from optimizers.router import route_board, RouterConfig

    placement = {
        "board": {"width_mm": 20.0, "height_mm": 20.0,
                  "outline_type": "rectangle", "origin": [0, 0], "layers": 2},
        "placements": [
            {"designator": "R1", "component_type": "resistor", "package": "R_0805",
             "footprint_width_mm": 2.0, "footprint_height_mm": 1.25,
             "x_mm": 5.0, "y_mm": 10.0, "rotation_deg": 0, "layer": "top"},
            {"designator": "R2", "component_type": "resistor", "package": "R_0805",
             "footprint_width_mm": 2.0, "footprint_height_mm": 1.25,
             "x_mm": 15.0, "y_mm": 10.0, "rotation_deg": 0, "layer": "top"},
        ],
    }
    netlist = {"elements": [
        {"element_type": "component", "component_id": "C1", "designator": "R1",
         "component_type": "resistor", "properties": {}},
        {"element_type": "component", "component_id": "C2", "designator": "R2",
         "component_type": "resistor", "properties": {}},
        {"element_type": "port", "port_id": "P1", "component_id": "C1",
         "designator": "R1", "pin_number": 1, "name": "1"},
        {"element_type": "port", "port_id": "P2", "component_id": "C1",
         "designator": "R1", "pin_number": 2, "name": "2"},
        {"element_type": "port", "port_id": "P3", "component_id": "C2",
         "designator": "R2", "pin_number": 1, "name": "1"},
        {"element_type": "port", "port_id": "P4", "component_id": "C2",
         "designator": "R2", "pin_number": 2, "name": "2"},
        {"element_type": "net", "net_id": "net_0", "name": "N0",
         "net_class": "signal", "connected_port_ids": ["P2", "P3"]},
    ]}

    routed = route_board(placement, netlist, RouterConfig())
    truth = routed["routing"]["statistics"]
    assert truth["total_nets"] >= 1 and truth["completion_pct"] == 100.0  # sanity

    summary = routing_stats_summary(routed)
    # The consumer must report exactly what the real router produced.
    assert summary["total_nets"] == truth["total_nets"]
    assert summary["routed_nets"] == truth["routed_nets"]
    assert summary["completion_pct"] == truth["completion_pct"]

    routing_text, _ = format_review_context(None, routed)
    assert f"{truth['routed_nets']}/{truth['total_nets']} nets routed" in routing_text
