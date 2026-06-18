"""Mounting holes export as NPTH (non-plated through holes), never as the
SMD-placeholder pad the footprint resolver falls back to.

Regression for the recurring "H1-H4 are SMD pads, not drilled NPTH" bug: the
custom MountingHole .kicad_mod has only an unnumbered np_thru_hole pad, which
the parser skips, so the part fell back to a 3mm SMD pad placeholder.
"""

import tempfile
from pathlib import Path

from exporters.kicad_exporter import (
    _mounting_hole_drill_mm, _footprint, build_kicad_pro,
)
from exporters.gerber_exporter import export_drill


class TestKicadProDesignRules:
    """The exported .kicad_pro must carry the ACTUAL routed clearance/track
    width so kicad-cli DRC stops using its 0.2mm defaults (which falsely flag
    every fine-pitch trace on a board routed at 0.127mm)."""

    def _pro(self, clearance=0.127, width=0.127):
        routed = {"routing": {"config": {
            "trace_clearance_mm": clearance, "trace_width_signal_mm": width,
            "via_diameter_mm": 0.6, "via_drill_mm": 0.3}}}
        return build_kicad_pro(routed, "t")

    def test_default_netclass_uses_routed_clearance(self):
        cls = self._pro()["net_settings"]["classes"][0]
        assert cls["name"] == "Default"
        assert cls["clearance"] == 0.127
        assert cls["track_width"] == 0.127

    def test_board_min_clearance_matches(self):
        rules = self._pro()["board"]["design_settings"]["rules"]
        assert rules["min_clearance"] == 0.127
        assert rules["min_track_width"] == 0.127

    def test_falls_back_to_conservative_defaults(self):
        cls = build_kicad_pro({}, "t")["net_settings"]["classes"][0]
        assert cls["clearance"] == 0.2  # default when no routing config


class TestMountingHoleDetection:
    def test_parses_drill_from_name(self):
        assert _mounting_hole_drill_mm("MountingHole_3.2mm_M3") == 3.2

    def test_underscore_variant(self):
        assert _mounting_hole_drill_mm("mounting_hole_2.7mm") == 2.7

    def test_component_type(self):
        assert _mounting_hole_drill_mm("Whatever", "mounting_hole") == 3.2

    def test_default_drill_when_no_dimension(self):
        assert _mounting_hole_drill_mm("MountingHole_M3") == 3.2

    def test_non_mounting_hole_is_none(self):
        assert _mounting_hole_drill_mm("R_0805") is None
        assert _mounting_hole_drill_mm("SOIC-8") is None


class TestMountingHoleFootprint:
    PLC = {"designator": "H1", "package": "MountingHole_3.2mm_M3",
           "x_mm": 5.0, "y_mm": 5.0, "rotation_deg": 0, "layer": "top",
           "component_type": "ic"}  # mis-typed as ic, like morgan — detect by package

    def test_emits_npth_not_smd(self):
        fp = _footprint(self.PLC, {}, {}, {})
        assert "np_thru_hole" in fp
        assert "(drill 3.2)" in fp
        assert "smd" not in fp           # never an SMD placeholder pad
        assert '(net ' not in fp         # NPTH carries no net

    def test_hole_at_placement_origin(self):
        fp = _footprint(self.PLC, {}, {}, {})
        assert "(at 5.0 5.0)" in fp


class TestDrillIncludesMountingHole:
    def test_npth_in_drill_file(self):
        routed = {"board": {"width_mm": 30, "height_mm": 20, "layers": 2},
                  "placements": [
                      {"designator": "H1", "package": "MountingHole_3.2mm_M3",
                       "x_mm": 5.0, "y_mm": 5.0, "rotation_deg": 0,
                       "layer": "top", "component_type": "ic"}],
                  "routing": {"traces": [], "vias": [], "unrouted_nets": []}}
        netlist = {"version": "1.0", "project_name": "t", "elements": []}
        with tempfile.TemporaryDirectory() as d:
            out = export_drill(routed, netlist, Path(d) / "t.drl")
            txt = out.read_text()
        # 3.200mm tool present and one hit
        assert "C3.200" in txt
        assert "X5000Y5000" in txt
