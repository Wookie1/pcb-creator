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

    def test_kicad_unconnected_is_ignored(self):
        # KiCad's ratsnest disagrees with the router; connectivity is NOT taken
        # from kicad-cli. Unconnected items must not create a failing check.
        rep = build_kicad_drc_report(_drc([], unconnected=[
            {"description": "GND", "items": [{"description": "Pad", "pos": {"x": 3, "y": 4}}]}]))
        assert not any(c["rule"] == "connectivity" for c in rep["checks"])
        assert rep["passed"] is True

    def test_connectivity_comes_from_extra_checks(self):
        # The router-reconciled internal connectivity check is the gate, passed
        # in via extra_checks — and DOES fail the report when it has errors.
        conn = {"rule": "connectivity", "category": "electrical", "passed": False,
                "violations": [{"rule": "connectivity", "severity": "error",
                                "message": "Net X: 2 disconnected groups"}]}
        rep = build_kicad_drc_report(_drc([]), extra_checks=[conn])
        assert any(c["rule"] == "connectivity" for c in rep["checks"])
        assert rep["passed"] is False

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

    def test_extra_checks_appended(self):
        cc = {"rule": "trace_current_capacity", "category": "current",
              "passed": True, "violations": [], "checked_count": 5}
        rep = build_kicad_drc_report(_drc([]), extra_checks=[cc])
        assert any(c["rule"] == "trace_current_capacity" for c in rep["checks"])

    def test_unknown_type_bucketed(self):
        rep = build_kicad_drc_report(_drc([
            {"type": "some_new_kicad_rule", "severity": "error",
             "description": "x", "items": []}]))
        assert any(c["rule"] == "other" for c in rep["checks"])
        assert rep["passed"] is False
