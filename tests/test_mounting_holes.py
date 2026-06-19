"""Mounting holes export as NPTH (non-plated through holes), never as the
SMD-placeholder pad the footprint resolver falls back to.

Regression for the recurring "H1-H4 are SMD pads, not drilled NPTH" bug: the
custom MountingHole .kicad_mod has only an unnumbered np_thru_hole pad, which
the parser skips, so the part fell back to a 3mm SMD pad placeholder.
"""

import tempfile
from pathlib import Path

from optimizers.pad_geometry import FootprintDef

from exporters.kicad_exporter import (
    _mounting_hole_drill_mm, _footprint, build_kicad_pro, _allow_mask_bridges,
)
from exporters.gerber_exporter import export_drill


class TestSolderMaskBridges:
    """Fine-pitch footprints whose mask apertures merge get
    allow_soldermask_bridges so DRC stops flagging every adjacent pad pair;
    coarse parts (where a mask sliver would be a real defect) do not."""

    def test_fine_pitch_ffc_allows(self):
        ffc = FootprintDef(
            pin_offsets={i: (round(i * 0.5, 3), 0.0) for i in range(10)},
            pad_size=(0.27, 1.3))
        assert _allow_mask_bridges(ffc) is True

    def test_soic_does_not_allow(self):
        soic = FootprintDef(
            pin_offsets={1: (0, 2.0), 2: (1.27, 2.0), 3: (2.54, 2.0),
                         4: (0, -2.0)}, pad_size=(0.6, 1.5))
        assert _allow_mask_bridges(soic) is False

    def test_0805_does_not_allow(self):
        r = FootprintDef(pin_offsets={1: (-1.0, 0), 2: (1.0, 0)},
                         pad_size=(1.0, 1.3))
        assert _allow_mask_bridges(r) is False

    def test_single_pad_never_bridges(self):
        assert _allow_mask_bridges(
            FootprintDef(pin_offsets={1: (0, 0)}, pad_size=(0.3, 0.3))) is False

    def test_attr_emitted_in_footprint(self):
        plc = {"designator": "CN1", "package": "FH35", "x_mm": 10, "y_mm": 10,
               "rotation_deg": 0, "layer": "top", "component_type": "connector"}
        fp_def = FootprintDef(
            pin_offsets={i: (round(i * 0.5, 3), 0.0) for i in range(6)},
            pad_size=(0.27, 1.3))
        # Patch resolver to return our fine-pitch def
        import exporters.kicad_exporter as ke
        orig = ke.get_footprint_def
        ke.get_footprint_def = lambda *a, **k: fp_def
        try:
            fp = _footprint(plc, {}, {}, {})
        finally:
            ke.get_footprint_def = orig
        assert "allow_soldermask_bridges" in fp


class TestFillZonesPcbnew:
    """Zone pour runs only on the KiCad-export artifact and degrades gracefully
    when pcbnew isn't installed (e.g. on a CI box / the dev Mac)."""

    def test_returns_false_when_no_pcbnew(self, monkeypatch):
        import exporters.kicad_exporter as ke

        class R:
            returncode = 1
            stdout = ""
            stderr = "ModuleNotFoundError: No module named 'pcbnew'"
        monkeypatch.setattr(ke.subprocess, "run", lambda *a, **k: R())
        assert ke.fill_zones_pcbnew("/tmp/x.kicad_pcb") is False

    def test_returns_true_on_success(self, monkeypatch):
        import exporters.kicad_exporter as ke
        seen = []

        class R:
            returncode = 0
            stdout = ""
            stderr = ""
        def fake_run(cmd, *a, **k):
            seen.append(cmd[0])
            return R()
        monkeypatch.setattr(ke.subprocess, "run", fake_run)
        assert ke.fill_zones_pcbnew("/tmp/x.kicad_pcb") is True
        assert seen  # a python candidate was invoked

    def test_honors_PCB_KICAD_PYTHON(self, monkeypatch):
        import exporters.kicad_exporter as ke
        seen = []

        class R:
            returncode = 0
            stdout = ""
            stderr = ""
        monkeypatch.setenv("PCB_KICAD_PYTHON", "/opt/kicad/bin/python")
        monkeypatch.setattr(ke.subprocess, "run",
                            lambda cmd, *a, **k: (seen.append(cmd[0]), R())[1])
        ke.fill_zones_pcbnew("/tmp/x.kicad_pcb")
        assert seen[0] == "/opt/kicad/bin/python"  # env candidate tried first


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
