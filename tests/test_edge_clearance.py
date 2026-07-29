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
