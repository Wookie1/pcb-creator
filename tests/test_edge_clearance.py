"""Board-edge copper clearance: DRC rule + autorouter boundary + escape guard.

Freerouting only keeps its trace clearance from the DSN boundary, so it routes
copper right up to the board rim; and the .kicad_pro used to omit the edge rule,
so kicad-cli fell back to its conservative 0.5mm default. Both are now sourced
from the fab profile's board_edge_clearance_mm, so the DRC checks the real spec
and the autorouter keeps that far from the edge.
"""

from exporters.dsn_exporter import _dsn_structure
from exporters.kicad_exporter import build_kicad_pro


def test_dsn_boundary_inset_by_edge_clearance():
    board = {"width_mm": 50.0, "height_mm": 35.0, "layers": 4}
    dsn = _dsn_structure(board, {"trace_width_mm": 0.127, "clearance_mm": 0.127,
                                 "edge_clearance_mm": 0.3, "plane_layers": 2})
    # Inset = edge + trace_half = 0.3 + 0.0635 = 0.3635, so the boundary starts
    # there, not at 0 — Freerouting keeps the wire EDGE clear of the board rim.
    assert "0.3635 0.3635" in dsn
    assert f"{50.0 - 0.3635}" in dsn or "49.6365" in dsn
    # Without an edge clearance the boundary is the full board (no inset).
    dsn0 = _dsn_structure(board, {"trace_width_mm": 0.127, "clearance_mm": 0.127,
                                  "plane_layers": 2})
    assert "0 0 50" in dsn0


def _routed_with_edge(edge):
    cfg = {"trace_clearance_mm": 0.127, "trace_width_signal_mm": 0.127,
           "via_diameter_mm": 0.6, "via_drill_mm": 0.3}
    if edge is not None:
        cfg["board_edge_clearance_mm"] = edge
    return {"routing": {"config": cfg, "vias": []},
            "board": {"width_mm": 30, "height_mm": 30, "layers": 4}}


def test_kicad_pro_emits_edge_rule_from_profile():
    pro = build_kicad_pro(_routed_with_edge(0.3), "p")
    rules = pro["board"]["design_settings"]["rules"]
    assert rules["min_copper_edge_clearance"] == 0.3


def test_kicad_pro_omits_edge_rule_when_unknown():
    """No profile → leave KiCad's stricter default rather than inventing one."""
    pro = build_kicad_pro(_routed_with_edge(None), "p")
    assert "min_copper_edge_clearance" not in pro["board"]["design_settings"]["rules"]


# --- via-diameter rule must enforce the FAB floor, not the board's own vias ---
# Deriving min_via_diameter from the vias present is circular: a stray under-size
# via just lowers the rule to accommodate itself, so DRC never flags it. When the
# fab profile is known the rule is its minimum, independent of what's on the board.

def _routed_with_via(via_min, drill_min, board_via_dia):
    cfg = {"trace_clearance_mm": 0.127, "trace_width_signal_mm": 0.127,
           "via_diameter_mm": 0.6, "via_drill_mm": 0.3}
    if via_min is not None:
        cfg["via_diameter_min_mm"] = via_min
        cfg["via_drill_min_mm"] = drill_min
    return {"routing": {"config": cfg,
                        "vias": [{"x_mm": 1, "y_mm": 1, "diameter_mm": board_via_dia,
                                  "drill_mm": 0.2}]},
            "board": {"width_mm": 30, "height_mm": 30, "layers": 4}}


def test_via_rule_uses_fab_minimum_not_board_via():
    # A stray 0.45mm via is on the board, but the fab floor is 0.6 — the rule
    # stays 0.6 so kicad-cli flags the under-size via instead of accommodating it.
    pro = build_kicad_pro(_routed_with_via(0.6, 0.3, 0.45), "p")
    rules = pro["board"]["design_settings"]["rules"]
    assert rules["min_via_diameter"] == 0.6
    assert rules["min_through_hole_diameter"] == 0.3


def test_via_rule_falls_back_to_board_when_fab_unknown():
    # No profile → floor at the smallest via present (current behaviour, no
    # false positives when the fab minimum is unknown).
    pro = build_kicad_pro(_routed_with_via(None, None, 0.45), "p")
    assert pro["board"]["design_settings"]["rules"]["min_via_diameter"] == 0.45
