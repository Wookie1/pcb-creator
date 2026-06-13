"""Tests for the Freerouting SES importer (exporters/ses_importer.py)."""

import json
from pathlib import Path

import pytest

from exporters.ses_importer import (
    import_ses,
    _resolve_layer,
    _via_layers,
)

FIXTURE = Path(__file__).parent / "fixtures" / "freerouting_l298n.ses"
PROJECT = Path(__file__).parent.parent / "projects" / "test_l298n_motor_driver"
# These project files are runtime artifacts (projects/ is untracked); skip if
# they aren't present rather than failing.
_PROJECT_FILES = [
    PROJECT / "test_l298n_motor_driver_placement.json",
    PROJECT / "test_l298n_motor_driver_netlist.json",
]
_HAVE_PROJECT = all(p.exists() for p in _PROJECT_FILES)


def _load_project():
    placement = json.loads(
        (PROJECT / "test_l298n_motor_driver_placement.json").read_text())
    netlist = json.loads(
        (PROJECT / "test_l298n_motor_driver_netlist.json").read_text())
    return placement, netlist


# ---------------------------------------------------------------------------
# Layer resolution
# ---------------------------------------------------------------------------

class TestLayerResolution:
    def test_layer_names(self):
        assert _resolve_layer("F.Cu", 2) == "top"
        assert _resolve_layer("B.Cu", 2) == "bottom"
        assert _resolve_layer("In1.Cu", 4) == "inner1"
        assert _resolve_layer("In2.Cu", 4) == "inner2"

    def test_numeric_indices_2layer(self):
        # On a 2-layer board index 1 is the BOTTOM (was mis-mapped to inner1)
        assert _resolve_layer("0", 2) == "top"
        assert _resolve_layer("1", 2) == "bottom"

    def test_numeric_indices_4layer(self):
        assert _resolve_layer("0", 4) == "top"
        assert _resolve_layer("1", 4) == "inner1"
        assert _resolve_layer("2", 4) == "inner2"
        assert _resolve_layer("3", 4) == "bottom"

    def test_unknown_defaults_top(self):
        assert _resolve_layer("Weird.Cu", 2) == "top"


class TestViaLayers:
    def test_through_via_default(self):
        assert _via_layers("Via_Default", 2) == ("top", "bottom")
        assert _via_layers("Via_Default", 4) == ("top", "bottom")

    def test_kicad_span_through(self):
        assert _via_layers("Via[0-3]_600:300_um", 4) == ("top", "bottom")
        assert _via_layers("Via[0-1]_600:300_um", 2) == ("top", "bottom")

    def test_kicad_span_blind_buried(self):
        assert _via_layers("Via[0-1]_600:300_um", 4) == ("top", "inner1")
        assert _via_layers("Via[1-2]_600:300_um", 4) == ("inner1", "inner2")
        assert _via_layers("Via[2-3]_600:300_um", 4) == ("inner2", "bottom")

    def test_reversed_span_normalized(self):
        assert _via_layers("Via[3-0]_600:300_um", 4) == ("top", "bottom")


# ---------------------------------------------------------------------------
# Full session import (real Freerouting v2.1.0 output)
# ---------------------------------------------------------------------------

@pytest.mark.skipif(not FIXTURE.exists() or not _HAVE_PROJECT,
                    reason="SES fixture or runtime project data missing")
class TestImportSes:
    def test_import_real_session(self):
        placement, netlist = _load_project()
        routed = import_ses(FIXTURE, placement, netlist,
                            exclude_net_ids={"net_gnd"})
        routing = routed["routing"]
        stats = routing["statistics"]

        assert len(routing["traces"]) > 0
        assert stats["routed_nets"] > 0
        assert stats["completion_pct"] > 0

        # Multi-point wire paths must split into connected segments
        # (each trace is one segment; consecutive segments share endpoints)
        t = routing["traces"][0]
        for key in ("start_x_mm", "start_y_mm", "end_x_mm", "end_y_mm",
                    "width_mm", "layer", "net_id"):
            assert key in t

        # Coordinates are mm-scale (board is ~52x42mm)
        xs = [t["start_x_mm"] for t in routing["traces"]]
        ys = [t["start_y_mm"] for t in routing["traces"]]
        assert 0 <= min(xs) and max(xs) < 100
        assert 0 <= min(ys) and max(ys) < 100

        # Layers resolved to our names, not SES tokens
        assert {t["layer"] for t in routing["traces"]} <= {
            "top", "bottom", "inner1", "inner2"}

        # Vias are through vias on this 2-layer board
        for v in routing["vias"]:
            assert v["from_layer"] == "top"
            assert v["to_layer"] == "bottom"

    def test_excluded_nets_not_counted(self):
        placement, netlist = _load_project()
        all_net_ids = {e["net_id"] for e in netlist["elements"]
                       if e.get("element_type") == "net"}
        gnd_ids = {e["net_id"] for e in netlist["elements"]
                   if e.get("element_type") == "net"
                   and e.get("name", "").upper().startswith("GND")}
        routed = import_ses(FIXTURE, placement, netlist,
                            exclude_net_ids=gnd_ids)
        stats = routed["routing"]["statistics"]
        assert stats["total_nets"] == len(all_net_ids) - len(gnd_ids)

    def test_placement_passthrough(self):
        placement, netlist = _load_project()
        routed = import_ses(FIXTURE, placement, netlist)
        assert routed["placements"] == placement.get("placements", [])
        assert routed["board"] == placement.get("board", {})
