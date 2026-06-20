"""Tests for the kicad-cli DRC report mapper (validators.kicad_drc).

build_kicad_drc_report is pure (parsed DRC json -> drc_report structure), so
it's tested without invoking kicad-cli.
"""

from validators.kicad_drc import build_kicad_drc_report


def _drc(violations, unconnected=None):
    return {"violations": violations, "unconnected_items": unconnected or []}


class TestBuildReport:
    def test_maps_types_to_rules(self):
        rep = build_kicad_drc_report(_drc([
            {"type": "shorting_items", "severity": "error",
             "description": "Items shorting two nets (5V and GND)",
             "items": [{"description": "Track [5V]", "pos": {"x": 1.0, "y": 2.0}}]},
            {"type": "clearance", "severity": "error",
             "description": "Clearance 0.1mm", "items": []},
        ]))
        rules = {c["rule"]: c for c in rep["checks"]}
        assert "no_shorts" in rules and "clearance_min" in rules
        assert rules["no_shorts"]["passed"] is False
        assert rules["no_shorts"]["violations"][0]["location"] == {"x_mm": 1.0, "y_mm": 2.0}
        assert rep["passed"] is False
        assert rep["engine"] == "kicad-cli"
        assert rep["statistics"]["errors"] == 2

    def test_unconnected_becomes_connectivity(self):
        rep = build_kicad_drc_report(_drc([], unconnected=[
            {"description": "GND", "items": [{"description": "Pad", "pos": {"x": 3, "y": 4}}]}]))
        conn = next(c for c in rep["checks"] if c["rule"] == "connectivity")
        assert conn["passed"] is False and len(conn["violations"]) == 1

    def test_clean_board_passes(self):
        rep = build_kicad_drc_report(_drc([]))
        assert rep["passed"] is True
        assert rep["statistics"]["errors"] == 0

    def test_warnings_dont_fail(self):
        rep = build_kicad_drc_report(_drc([
            {"type": "silk_over_copper", "severity": "warning",
             "description": "Silk over copper", "items": []}]))
        assert rep["passed"] is True
        assert rep["statistics"]["warnings"] == 1

    def test_current_check_appended(self):
        cc = {"rule": "trace_current_capacity", "category": "current",
              "passed": True, "violations": [], "checked_count": 5}
        rep = build_kicad_drc_report(_drc([]), current_check=cc)
        assert any(c["rule"] == "trace_current_capacity" for c in rep["checks"])

    def test_unknown_type_bucketed(self):
        rep = build_kicad_drc_report(_drc([
            {"type": "some_new_kicad_rule", "severity": "error",
             "description": "x", "items": []}]))
        assert any(c["rule"] == "other" for c in rep["checks"])
        assert rep["passed"] is False
