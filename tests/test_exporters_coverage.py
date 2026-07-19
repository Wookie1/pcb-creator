"""Targeted coverage for exporter modules — fills gaps left by the existing
exporter test suite. New tests only; nothing here duplicates
test_kicad_netlist_importer.py / test_ses_importer.py / test_gerber_inner_plane.py.

Quality bar: assert real structure of the exported artifact (apertures, coords,
CSV columns/rows, SVG designators, round-trip trace/via counts), not file
existence.
"""

from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SES_FIXTURE = Path(__file__).parent / "fixtures" / "freerouting_l298n.ses"


def _proj_dir() -> Path:
    """Project fixtures live at the main-repo projects/ dir (this is a worktree)."""
    here = REPO / "projects" / "blink_3_leds_dc_power"
    if (here / "blink_3_leds_dc_power_routed.json").exists():
        return here
    # Worktree: walk up out of .claude/worktrees/<name> to the main checkout.
    for parent in REPO.parents:
        cand = parent / "projects" / "blink_3_leds_dc_power"
        if (cand / "blink_3_leds_dc_power_routed.json").exists():
            return cand
    return here


PROJ = _proj_dir()


def _load(name: str) -> dict:
    return json.loads((PROJ / f"blink_3_leds_dc_power_{name}.json").read_text())


@pytest.fixture(scope="module")
def routed() -> dict:
    return _load("routed")


@pytest.fixture(scope="module")
def netlist() -> dict:
    return _load("netlist")


@pytest.fixture(scope="module")
def placement() -> dict:
    return _load("placement")


@pytest.fixture(scope="module")
def bom() -> dict:
    return _load("bom")


# ---------------------------------------------------------------------------
# component_heights
# ---------------------------------------------------------------------------

class TestComponentHeights:
    def test_uppercase_normalized_match(self):
        from exporters.component_heights import get_component_height
        # "sot-23" not an exact key; uppercase loop (line 128) finds "SOT-23".
        assert get_component_height("sot-23") == 1.1

    def test_prefix_match(self):
        from exporters.component_heights import get_component_height
        # "DIP-99" isn't a key but the DIP- prefix loop (134-136) returns a DIP height.
        assert get_component_height("DIP-99") == 3.5

    def test_type_default_fallback(self):
        from exporters.component_heights import get_component_height
        assert get_component_height("WeirdPkg", "relay") == 15.0

    def test_generic_default(self):
        from exporters.component_heights import get_component_height
        # No package, no type → 1.5 generic (line 142).
        assert get_component_height("totally_unknown") == 1.5


# ---------------------------------------------------------------------------
# parametric_models — bottom-layer placement
# ---------------------------------------------------------------------------

def test_parametric_bottom_layer_z_below_origin():
    from exporters.parametric_models import generate_component_model
    plc = {
        "designator": "R9", "package": "0805", "component_type": "resistor",
        "x_mm": 5.0, "y_mm": 5.0, "rotation_deg": 0, "layer": "bottom",
        "footprint_width_mm": 2.0, "footprint_height_mm": 1.25,
    }
    entities, solid_id, next_eid = generate_component_model(plc, 1.6, 0)
    assert next_eid >= solid_id > 0
    text = "\n".join(entities)
    # Bottom component sits below z=0: at least one cartesian point has a negative Z.
    assert "MANIFOLD_SOLID_BREP('R9'" in text
    assert any(",-" in e for e in entities if "CARTESIAN_POINT" in e)


# ---------------------------------------------------------------------------
# bom_csv_exporter — fiducial value override + CSV structure
# ---------------------------------------------------------------------------

def test_bom_csv_columns_and_rows(tmp_path, bom):
    from exporters.bom_csv_exporter import export_bom_csv
    out = export_bom_csv(bom, tmp_path / "bom.csv")
    rows = list(csv.reader(out.open()))
    assert rows[0] == ["Designator", "Value", "Package", "Quantity",
                       "Description", "LCSC Part #", "MPN", "Manufacturer",
                       "Specs", "Notes"]
    assert len(rows) - 1 == len(bom["bom"])


def test_cpl_fiducial_value(tmp_path):
    from exporters.bom_csv_exporter import export_pick_and_place
    placement = {"placements": [
        {"designator": "FID1", "component_type": "fiducial", "package": "Fiducial_1mm",
         "x_mm": 1.0, "y_mm": 2.0, "rotation_deg": 0, "layer": "top"},
        {"designator": "R1", "component_type": "resistor", "package": "0805",
         "x_mm": 10.0, "y_mm": 5.0, "rotation_deg": 90, "layer": "top"},
    ]}
    # BOM splits grouped designators; covers the value-lookup branch too.
    bom = {"bom": [{"designator": "R1, R2", "value": "10k"}]}
    out = export_pick_and_place(placement, tmp_path / "cpl.csv", bom=bom)
    rows = {r[0]: r for r in csv.reader(out.open())}
    assert rows["FID1"][1] == "Fiducial"   # line 108
    assert rows["R1"][1] == "10k"          # bom value lookup
    assert rows["FID1"][3] == "1.0000"     # mm formatting


# ---------------------------------------------------------------------------
# kicad_mod_parser
# ---------------------------------------------------------------------------

_MOD_2PAD = """(footprint "R_0805" (layer "F.Cu")
  (pad "1" smd roundrect (at -1.0 0.5 90) (size 1.0 1.25) (layers "F.Cu"))
  (pad "2" smd roundrect (at 1.0 0.5) (size 1.0 1.25) (layers "F.Cu"))
  (pad "A1" smd roundrect (at 0 0) (size 0.5 0.5) (layers "F.Cu"))
  (pad "" np_thru_hole circle (at 0 2) (size 1 1) (layers "*.Cu"))
)
"""


class TestKicadModParser:
    def test_parse_real_footprint(self, tmp_path):
        from exporters.kicad_mod_parser import parse_kicad_mod
        p = tmp_path / "R_0805.kicad_mod"
        p.write_text(_MOD_2PAD)
        fp = parse_kicad_mod(p)
        # Two numbered pads; lettered "A1" and empty pads skipped (61, 71-74).
        assert set(fp.pin_offsets) == {1, 2}
        # KiCad Y inverted: at 0.5 -> -0.5
        assert fp.pin_offsets[1] == (-1.0, -0.5)
        assert fp.pad_size == (1.0, 1.25)

    def test_missing_file_returns_none(self, tmp_path):
        from exporters.kicad_mod_parser import parse_kicad_mod
        assert parse_kicad_mod(tmp_path / "nope.kicad_mod") is None  # 38-39

    def test_empty_text_returns_none(self, tmp_path):
        from exporters.kicad_mod_parser import parse_kicad_mod
        p = tmp_path / "empty.kicad_mod"
        p.write_text("")
        assert parse_kicad_mod(p) is None  # 43

    def test_no_pads_returns_none(self, tmp_path):
        from exporters.kicad_mod_parser import parse_kicad_mod
        p = tmp_path / "nopads.kicad_mod"
        p.write_text('(footprint "X" (layer "F.Cu") (descr "no pads"))')
        assert parse_kicad_mod(p) is None  # 52

    def test_empty_root_returns_none(self, tmp_path):
        from exporters.kicad_mod_parser import parse_kicad_mod
        p = tmp_path / "emptyroot.kicad_mod"
        p.write_text("()")  # parses to an empty root list → line 48
        assert parse_kicad_mod(p) is None

    def test_index_lookup_and_invalidate(self, tmp_path):
        from exporters.kicad_mod_parser import KiCadLibraryIndex
        lib = tmp_path / "lib.pretty"
        lib.mkdir()
        # Index built before file exists → size 0, no match (covers _build on empty).
        idx = KiCadLibraryIndex(lib)
        assert idx.root == lib
        assert idx.size == 0
        (lib / "SOIC-8_3.9x4.9mm_P1.27mm.kicad_mod").write_text(
            '(footprint "SOIC-8" (layer "F.Cu")'
            ' (pad "1" smd rect (at -2 0) (size 0.6 1.5) (layers "F.Cu"))'
            ' (pad "2" smd rect (at 2 0) (size 0.6 1.5) (layers "F.Cu")))'
        )
        assert idx.get_footprint("SOIC-8") is None  # stale index
        idx.invalidate()  # 221-222
        fp = idx.get_footprint("SOIC-8")  # alias resolution + parse cache
        assert fp is not None and set(fp.pin_offsets) == {1, 2}
        # Second lookup hits the parsed cache.
        assert idx.get_footprint("SOIC-8") is fp
        # pin_count mismatch rejects the match.
        assert idx.get_footprint("SOIC-8", pin_count=99) is None
        assert idx.size >= 1

    def test_index_missing_dir(self, tmp_path):
        from exporters.kicad_mod_parser import KiCadLibraryIndex
        idx = KiCadLibraryIndex(tmp_path / "does_not_exist")
        assert idx.size == 0  # 162: root not a dir

    def test_pad_without_size_uses_default(self, tmp_path):
        from exporters.kicad_mod_parser import parse_kicad_mod
        p = tmp_path / "nosize.kicad_mod"
        # A short pad (<3 tokens, line 61), a pad with no (at ...) (line 80),
        # and a valid pad with (at) but NO (size) → pad_widths empty (line 103).
        p.write_text(
            '(footprint "X" (layer "F.Cu")\n'
            '  (pad "9")\n'                                          # too short -> 61
            '  (pad "8" smd rect (size 1 1) (layers "F.Cu"))\n'      # no (at) -> 80
            '  (pad "1" smd rect (at 0 0) (layers "F.Cu"))\n'        # no (size) -> 103
            ')'
        )
        fp = parse_kicad_mod(p)
        assert set(fp.pin_offsets) == {1}
        assert fp.pad_size == (0.5, 0.5)  # default when no size fields seen

    def test_pads_present_but_none_numbered(self, tmp_path):
        from exporters.kicad_mod_parser import parse_kicad_mod
        p = tmp_path / "allskipped.kicad_mod"
        # Pads exist but every one is skipped (empty/lettered) → pin_offsets
        # empty → return None (line 93), distinct from the no-pads path (52).
        p.write_text(
            '(footprint "X" (layer "F.Cu")\n'
            '  (pad "" np_thru_hole circle (at 0 0) (size 1 1) (layers "*.Cu"))\n'
            '  (pad "B2" smd rect (at 1 0) (size 0.5 0.5) (layers "F.Cu"))\n'
            ')'
        )
        assert parse_kicad_mod(p) is None

    def test_index_caches_unparseable_as_none(self, tmp_path):
        from exporters.kicad_mod_parser import KiCadLibraryIndex
        lib = tmp_path / "lib.pretty"
        lib.mkdir()
        # File matches an alias but has no usable pads → parse returns None;
        # get_footprint hits the "fp is None" guard (line 207).
        (lib / "BADFP-8_3x3mm.kicad_mod").write_text(
            '(footprint "BADFP-8" (layer "F.Cu"))')
        idx = KiCadLibraryIndex(lib)
        assert idx.get_footprint("BADFP-8") is None
        # Second call uses the cached None (line 201 branch) and still returns None.
        assert idx.get_footprint("BADFP-8") is None


# ---------------------------------------------------------------------------
# kicad_netlist_importer — .kicad_sch path, stub components, non-int pins
# ---------------------------------------------------------------------------

_SCH = """(kicad_sch
  (symbol (lib_id "Device:R")
    (property "Reference" "R1") (property "Value" "10k")
    (property "Footprint" "Resistor_SMD:R_0805_2012Metric"))
  (symbol (lib_id "Device:R")
    (property "Reference" "R1") (property "Value" "10k")
    (property "Footprint" "Resistor_SMD:R_0805_2012Metric"))
  (symbol (lib_id "power:GND")
    (property "Reference" "#PWR01") (property "Value" "GND"))
)
"""

# .net whose footprints/values are intentionally weaker than the .sch so the
# merge (446-453) must prefer the schematic data; one pin is non-numeric (305-306);
# one component (Q1) appears only in nets → stub (273-277).
_NET = """(export (version "E")
  (components
    (comp (ref "R1") (value "res") (footprint ""))
  )
  (nets
    (net (code "1") (name "/SIG")
      (node (ref "R1") (pin "1"))
      (node (ref "R1") (pin "A"))
      (node (ref "Q1") (pin "1")))
    (net (code "2") (name "GND")
      (node (ref "R1") (pin "2")))
  )
)
"""


class TestKicadNetlistImporter:
    def test_kicad_sch_with_sibling_net(self, tmp_path):
        from exporters.kicad_netlist_importer import convert_kicad_netlist
        sch = tmp_path / "board.kicad_sch"
        sch.write_text(_SCH)
        (tmp_path / "board.net").write_text(_NET)

        result = convert_kicad_netlist(sch)
        nl = result["netlist"]
        assert result["source"].endswith("board.net")
        # Schematic footprint preferred over the empty .net footprint (446-453).
        r1 = next(e for e in nl["elements"]
                  if e.get("element_type") == "component" and e["designator"] == "R1")
        assert r1["package"] == "R_0805_2012Metric"
        assert r1["value"] == "10k"
        # Q1 in nets only → stub component created (273-277) with a warning.
        designators = {e["designator"] for e in nl["elements"]
                       if e.get("element_type") == "component"}
        assert "Q1" in designators
        assert any("stub" in w.lower() for w in result["warnings"])
        # Non-numeric pin "A" mapped to a synthetic integer (305-306).
        r1_ports = [e for e in nl["elements"]
                    if e.get("element_type") == "port" and e["name"] in ("1", "A")]
        assert all(isinstance(e["pin_number"], int) for e in r1_ports)
        # GND net has <2 nodes → skipped with warning (net-skip path).
        net_names = {e["name"] for e in nl["elements"]
                     if e.get("element_type") == "net"}
        assert "/SIG" in net_names and "GND" not in net_names

    def test_parse_sch_dedup(self):
        from exporters.kicad_netlist_importer import _parse_kicad_sch_components
        comps = _parse_kicad_sch_components(_SCH)
        # Duplicate R1 collapsed (239-241); #PWR power symbol dropped (227-228).
        assert [c["ref"] for c in comps] == ["R1"]

    def test_kicad_sch_without_sibling_raises(self, tmp_path):
        from exporters.kicad_netlist_importer import convert_kicad_netlist
        sch = tmp_path / "lonely.kicad_sch"
        sch.write_text(_SCH)
        with pytest.raises(ValueError, match="net connections"):
            convert_kicad_netlist(sch)

    def test_no_nets_raises(self, tmp_path):
        from exporters.kicad_netlist_importer import convert_kicad_netlist
        p = tmp_path / "n.net"
        p.write_text('(export (components (comp (ref "R1") (value "1") '
                     '(footprint ""))) (nets))')
        with pytest.raises(ValueError, match="No nets"):
            convert_kicad_netlist(p)  # 461

    def test_project_name_leading_digit(self, tmp_path):
        from exporters.kicad_netlist_importer import convert_kicad_netlist
        p = tmp_path / "9board.net"
        p.write_text(_NET)
        result = convert_kicad_netlist(p)
        # Stem starts with a digit → prefixed (line 411).
        assert result["netlist"]["project_name"].startswith("p_")


# ---------------------------------------------------------------------------
# gerber_exporter — fiducials, plane fills, drill, paste on both sides
# ---------------------------------------------------------------------------

def _make_routed_with_features(base: dict) -> dict:
    """Augment the project routed dict to exercise rare gerber paths."""
    r = json.loads(json.dumps(base))  # deep copy
    # Add a fiducial (copper dot + mask opening: 150-155, 289-294).
    r["placements"].append({
        "designator": "FID1", "component_type": "fiducial", "package": "Fiducial",
        "x_mm": 2.0, "y_mm": 2.0, "rotation_deg": 0, "layer": "top",
        "footprint_width_mm": 1.0, "footprint_height_mm": 1.0,
    })
    # Add a silkscreen dot + left-anchored text (255-258, 218).
    r.setdefault("silkscreen", []).append(
        {"type": "dot", "layer": "top_silk", "x_mm": 3.0, "y_mm": 3.0,
         "diameter_mm": 0.4})
    r["silkscreen"].append(
        {"type": "text", "layer": "top_silk", "x_mm": 5.0, "y_mm": 5.0,
         "font_height_mm": 1.0, "text": "L1", "anchor": "left"})
    return r


class TestGerberExporter:
    def test_full_gerber_set_structure(self, tmp_path, netlist):
        from exporters.gerber_exporter import export_gerbers
        r = _make_routed_with_features(_load("routed"))
        files = export_gerbers(r, netlist, tmp_path)
        names = {p.name for p in files}
        # Expect the standard layer set.
        assert any(n.endswith("-F_Cu.gbr") for n in names)
        assert any(n.endswith("-B_Cu.gbr") for n in names)
        assert any(n.endswith("-Edge_Cuts.gbr") for n in names)
        assert any(n.endswith("-F_Paste.gbr") for n in names)
        # Copper gerber carries real flashes/draws and a proper trailer.
        fcu = next(p for p in files if p.name.endswith("-F_Cu.gbr"))
        body = fcu.read_text()
        assert body.startswith("%") or "G04" in body  # gerber header
        assert "M02*" in body  # end-of-file
        assert "D03*" in body  # at least one flash (pad/via/fiducial)

    def test_bottom_paste_when_bottom_smd(self, tmp_path, netlist):
        from exporters.gerber_exporter import export_gerbers
        r = _load("routed")
        # Force a component to the bottom so a B_Paste layer is emitted (399-403).
        smd = next(p for p in r["placements"]
                   if p.get("component_type") not in ("fiducial",))
        smd["layer"] = "bottom"
        files = export_gerbers(r, netlist, tmp_path)
        assert any(p.name.endswith("-B_Paste.gbr") for p in files)

    def test_plane_fill_with_antipads(self, tmp_path, netlist):
        from exporters.gerber_exporter import _generate_copper_layer
        from optimizers.pad_geometry import build_pad_map
        r = _load("routed")
        r["routing"]["copper_fills"] = [{
            "layer": "top", "is_plane": True,
            "polygons": [
                [[0, 0], [10, 0], [10, 10], [0, 10]],   # outer
                [[2, 2], [3, 2], [3, 3], [2, 3]],        # antipad cutout
                [[0, 0]],                                # degenerate, skipped
            ],
        }, {
            "layer": "top", "is_plane": False,
            "polygons": [[[5, 5], [6, 5], [6, 6]], [[0, 0], [1, 0]]],
        }]
        dl = _generate_copper_layer(r, netlist, "top", build_pad_map(r, netlist), 2)
        body = dl.dumps_gerber()
        # A region (G36/G37) was emitted for the plane and the non-plane fill.
        assert "G36*" in body and "G37*" in body

    def test_drill_file_structure(self, tmp_path, netlist):
        from exporters.gerber_exporter import export_drill
        r = _load("routed")
        out = export_drill(r, netlist, tmp_path / "board.drl")
        text = out.read_text()
        assert text.startswith("M48")
        assert "METRIC,TZ" in text
        assert text.rstrip().endswith("M30")
        # 5 vias in the project → at least one tool + hits.
        assert "T1" in text
        assert any(line.startswith("X") for line in text.splitlines())

    def test_drill_empty(self, tmp_path):
        from exporters.gerber_exporter import export_drill
        empty = {"board": {}, "placements": [], "routing": {"vias": []}}
        out = export_drill(empty, {"elements": []}, tmp_path / "e.drl")
        text = out.read_text()
        assert "M48" in text and "M30" in text  # 456-457
        assert not any(l.startswith("X") for l in text.splitlines())

    def test_is_through_hole_and_vertices(self):
        from exporters.gerber_exporter import _is_through_hole, _get_board_vertices
        assert _is_through_hole("DIP-8") is True
        assert _is_through_hole("0805") is False  # 65-68
        # Explicit outline_vertices branch (line 79).
        verts = _get_board_vertices({"outline_vertices": [[0, 0], [5, 0], [5, 5]]})
        assert verts == [(0, 0), (5, 0), (5, 5)]

    def test_copper_layer_function_inner(self):
        from exporters.gerber_exporter import _copper_layer_function
        assert _copper_layer_function("inner1", 4) == "Copper,L2,Inr"
        assert _copper_layer_function("inner2", 4) == "Copper,L3,Inr"
        assert _copper_layer_function("bottom", 4) == "Copper,L4,Bot"
        # Unknown layer name → top fallback (line 100).
        assert _copper_layer_function("mystery", 4) == "Copper,L1,Top"

    def test_output_package_zip(self, tmp_path, netlist):
        from exporters.gerber_exporter import export_gerbers, create_output_package
        r = _load("routed")
        export_gerbers(r, netlist, tmp_path)
        import zipfile
        zp = create_output_package(tmp_path, "blink")
        with zipfile.ZipFile(zp) as zf:
            assert any(n.endswith(".gbr") for n in zf.namelist())


# ---------------------------------------------------------------------------
# dsn_exporter — round-trip-style structure, fiducial skip, fallback paths
# ---------------------------------------------------------------------------

class TestDsnExporter:
    def test_dsn_structure(self, tmp_path, placement, netlist):
        from exporters.dsn_exporter import export_dsn
        # Add a fiducial (skipped: 219, 357) and a config-free call (507).
        p = json.loads(json.dumps(placement))
        p["placements"].append({
            "designator": "FID1", "component_type": "fiducial", "package": "Fiducial",
            "x_mm": 1.0, "y_mm": 1.0, "rotation_deg": 0, "layer": "top",
            "footprint_width_mm": 1.0, "footprint_height_mm": 1.0,
        })
        out = export_dsn(p, netlist, tmp_path / "b.dsn")
        text = out.read_text()
        assert text.startswith("(pcb")
        assert "(resolution mm 1000)" in text
        assert "(structure" in text and "(library" in text
        assert "(placement" in text and "(network" in text
        # Fiducial must not appear as a placed component image.
        assert "FID1" not in text
        # Net definitions present with pin refs.
        assert "(net " in text and "(pins " in text

    def test_dsn_placement_fallback_no_image_map(self, placement, netlist):
        from exporters.dsn_exporter import _dsn_placement
        # Calling without des_image_map exercises the rebuild path (341-351).
        text = _dsn_placement(placement["placements"], netlist, des_image_map=None)
        assert "(placement" in text
        assert "(component " in text

    def test_dsn_network_auto_pin_and_short_net(self):
        from exporters.dsn_exporter import _dsn_network
        netlist = {"elements": [
            {"element_type": "component", "component_id": "c1", "designator": "U1"},
            # Two ports both pin_number 0 → sequential auto-assign (409-412).
            {"element_type": "port", "port_id": "p1", "component_id": "c1",
             "pin_number": 0, "name": "x"},
            {"element_type": "port", "port_id": "p2", "component_id": "c1",
             "pin_number": 0, "name": "y"},
            {"element_type": "component", "component_id": "c2", "designator": "U2"},
            {"element_type": "port", "port_id": "p3", "component_id": "c2",
             "pin_number": 1, "name": "1"},
            # Net joining U1.x and U2.1 → valid (>=2 refs).
            {"element_type": "net", "net_id": "n1", "name": "SIG",
             "net_class": "signal", "connected_port_ids": ["p1", "p3"]},
            # Net with a single resolvable ref → skipped (line 435).
            {"element_type": "net", "net_id": "n2", "name": "LONELY",
             "net_class": "power", "connected_port_ids": ["p2"]},
        ]}
        text = _dsn_network(netlist)
        assert '(net "SIG"' in text
        assert "LONELY" not in text  # skipped: only one pin ref
        assert "U1-1" in text and "U2-1" in text  # auto pin numbering

    def test_dsn_network_auto_idx_collision(self):
        from exporters.dsn_exporter import _dsn_network
        # Real pin 1 first, then two zero-pins: auto_idx starts at 1 which is
        # already used → the `while auto_idx in used_pins` loop fires (line 410).
        netlist = {"elements": [
            {"element_type": "component", "component_id": "c1", "designator": "U1"},
            {"element_type": "port", "port_id": "a", "component_id": "c1",
             "pin_number": 1, "name": "1"},
            {"element_type": "port", "port_id": "b", "component_id": "c1",
             "pin_number": 0, "name": "x"},
            {"element_type": "port", "port_id": "c", "component_id": "c1",
             "pin_number": 0, "name": "y"},
            {"element_type": "component", "component_id": "c2", "designator": "U2"},
            {"element_type": "port", "port_id": "d", "component_id": "c2",
             "pin_number": 1, "name": "1"},
            {"element_type": "net", "net_id": "n1", "name": "N1",
             "net_class": "signal", "connected_port_ids": ["b", "d"]},
        ]}
        text = _dsn_network(netlist)
        # U1's first zero-pin took 2 (1 was real), so the ref is U1-2.
        assert "U1-2" in text

    def test_dsn_fallback_footprint(self, tmp_path, netlist):
        from exporters.dsn_exporter import _dsn_library
        # A placement with an unknown package forces _generate_fallback_footprint (230).
        placements = [{
            "designator": "X1", "package": "WEIRD_UNKNOWN_PKG",
            "x_mm": 0, "y_mm": 0, "rotation_deg": 0, "layer": "top",
            "footprint_width_mm": 3.0, "footprint_height_mm": 3.0,
        }]
        text, image_map = _dsn_library(placements, netlist, {"num_layers": 2})
        assert "X1" in image_map
        assert "(image " in text and "(padstack " in text


# ---------------------------------------------------------------------------
# ses_importer — full import (synthetic netlist matching the fixture's nets)
# ---------------------------------------------------------------------------

def _ses_netlist() -> dict:
    """Netlist whose net names match the freerouting_l298n.ses fixture."""
    names = ["VS", "VCC_5V", "ENA", "ENB", "IN1", "IN2", "IN3", "IN4",
             "OUT1", "OUT2", "OUT3", "OUT4", "GND"]
    elements = []
    for n in names:
        elements.append({
            "element_type": "net",
            "net_id": "net_" + n.lower(),
            "name": n,
            "connected_port_ids": [],
        })
    return {"elements": elements}


def _ses_placement() -> dict:
    return {"version": "1.0", "project_name": "l298n",
            "board": {"width_mm": 52, "height_mm": 42, "layers": 2},
            "placements": [{"designator": "U1", "x_mm": 1, "y_mm": 1}]}


class TestSesImporter:
    def test_full_import_extracts_traces_and_vias(self):
        from exporters.ses_importer import import_ses
        routed = import_ses(SES_FIXTURE, _ses_placement(), _ses_netlist(),
                            exclude_net_ids={"net_gnd"})
        routing = routed["routing"]
        # Real wires (mm-scaled) and 5 vias from the fixture.
        assert len(routing["traces"]) > 0
        assert len(routing["vias"]) == 5
        for v in routing["vias"]:
            assert v["from_layer"] == "top" and v["to_layer"] == "bottom"
            assert v["drill_mm"] == 0.3 and v["diameter_mm"] == 0.6
        # Statistics computed; GND excluded from the routable denominator.
        stats = routing["statistics"]
        assert stats["routed_nets"] > 0
        assert 0 < stats["completion_pct"] <= 100
        assert stats["via_count"] == 5
        assert stats["layer_usage"].get("top", 0) > 0
        # Placement passthrough.
        assert routed["placements"] == _ses_placement()["placements"]

    def test_resolution_units(self):
        from exporters.ses_importer import _parse_resolution
        assert _parse_resolution([["resolution", "mm", "1000"]]) == 1.0 / 1000
        assert _parse_resolution([["resolution", "um", "1"]]) == 0.001
        assert _parse_resolution([["resolution", "mil", "1"]]) == 0.0254
        assert _parse_resolution([["resolution", "inch", "1"]]) == 1.0  # else branch
        # Missing/short → default; bad value → divisor fallback.
        assert _parse_resolution([["foo"]]) == 0.001
        assert _parse_resolution([["resolution", "mm", "bad"]]) == 1.0 / 1000

    def test_find_value_helper(self):
        from exporters.ses_importer import _find_value
        assert _find_value([["x", "5"]], "x") == "5"  # 72-75
        assert _find_value([["x", "5"]], "missing", "d") == "d"

    def test_no_routes_section(self, tmp_path):
        from exporters.ses_importer import import_ses
        ses = tmp_path / "empty.ses"
        ses.write_text("(session s (base_design s))")  # no routes (line 134 return)
        routed = import_ses(ses, _ses_placement(), _ses_netlist())
        assert routed["routing"]["traces"] == []
        assert routed["routing"]["vias"] == []

    def test_routes_without_network_out(self, tmp_path):
        from exporters.ses_importer import import_ses
        ses = tmp_path / "noout.ses"
        # routes present but no network_out → line 138 return.
        ses.write_text("(session s (routes (resolution mm 1000)))")
        routed = import_ses(ses, _ses_placement(), _ses_netlist())
        assert routed["routing"]["traces"] == []

    def test_malformed_wires_and_vias_skipped(self, tmp_path):
        from exporters.ses_importer import import_ses
        ses = tmp_path / "bad.ses"
        # Short path (149), non-numeric width (156-157) + coord (164-165),
        # short via (184), non-numeric via coord (190-191), plus one good wire.
        ses.write_text(
            "(session s (routes (resolution mm 1000) (network_out\n"
            '  (net VS\n'
            "    (wire (path F.Cu 500))\n"                       # path too short -> 149
            "    (wire (path F.Cu bad 0 0 1000 1000))\n"        # bad width -> 156-157
            "    (wire (path F.Cu 500 0 0 nope 2000 3000 4000))\n"  # bad coord -> 164-165
            "    (wire (path F.Cu 500 0 0 1000 1000))\n"        # good single segment
            "    (via Via_Default 5000)\n"                       # via too short -> 184
            "    (via Via_Default oops 5000 0)\n"                # bad via coord -> 190-191
            "    (via Via_Default 6000 7000 0)\n"               # good via
            "  )\n"
            "  (net)\n"                                          # bare net -> line 138 continue
            "  )))"
        )
        routed = import_ses(ses, _ses_placement(), _ses_netlist())
        routing = routed["routing"]
        # Short path (149) produced nothing; bad-width wire fell back to the
        # default width (156-157); bad-coord wire skipped its bad token (164-165)
        # but still yielded a segment. Short/bad vias were dropped (184, 190-191);
        # only the well-formed via survives.
        widths = {t["width_mm"] for t in routing["traces"]}
        assert 0.25 in widths            # default-width fallback fired
        assert 0.5 in widths             # good wire: 500 * 0.001 = 0.5mm
        assert len(routing["vias"]) == 1
        assert routing["vias"][0]["x_mm"] == 6.0 and routing["vias"][0]["y_mm"] == 7.0


# ---------------------------------------------------------------------------
# step_exporter — bare board + populated assembly (pure-Python STEP emission)
# ---------------------------------------------------------------------------

class TestStepExporter:
    def test_bare_board_brep(self, tmp_path, routed, netlist):
        from exporters.step_exporter import export_step
        out = export_step(routed, netlist, tmp_path / "board.step")
        text = out.read_text()
        assert text.startswith("ISO-10303-21;")
        assert text.rstrip().endswith("END-ISO-10303-21;")
        # A real BREP solid + closed shell for the board.
        assert "MANIFOLD_SOLID_BREP" in text
        assert "CLOSED_SHELL" in text
        assert "SHAPE_DEFINITION_REPRESENTATION" in text
        # Rectangular board → 4 corners top + 4 bottom = 8 cartesian verts min.
        assert text.count("VERTEX_POINT") >= 8

    def test_bare_board_polygon_outline(self, tmp_path, netlist):
        from exporters.step_exporter import export_step
        r = {"board": {"outline_vertices": [[0, 0], [5, 0], [5, 5], [2, 7], [0, 5]]},
             "project_name": "poly"}
        out = export_step(r, netlist, tmp_path / "poly.step")
        text = out.read_text()
        # 5-gon → 5 side faces.
        assert text.count("ADVANCED_FACE") == 2 + 5  # top + bottom + 5 sides

    def test_populated_assembly(self, tmp_path, routed, netlist, bom):
        from exporters.step_exporter import export_step_populated
        out = export_step_populated(routed, netlist, bom, tmp_path / "asm.step")
        text = out.read_text()
        assert "PCB Assembly Model" in text
        # Board solid + one solid per non-fiducial component.
        n_components = sum(1 for p in routed["placements"]
                           if p.get("component_type") != "fiducial")
        # Each component is a box → its own MANIFOLD_SOLID_BREP, plus the board.
        assert text.count("MANIFOLD_SOLID_BREP") == 1 + n_components


# ---------------------------------------------------------------------------
# assembly_drawing — SVG content, polygon outline, pin-1 rotations, PDF
# ---------------------------------------------------------------------------

class TestAssemblyDrawing:
    def test_generate_page_svg_structure(self, routed, bom):
        from exporters.assembly_drawing import _generate_page, _build_bom_table
        board = routed["board"]
        items = [p for p in routed["placements"]
                 if p.get("component_type") != "fiducial"]
        bom_lookup = {b["designator"]: b for b in bom["bom"]
                      if b.get("designator")}
        svg = _generate_page(board, board["width_mm"], board["height_mm"],
                             items, bom_lookup, "blink", "Top", routed)
        assert svg.startswith("<svg") and svg.endswith("</svg>")
        # Every component designator appears as a label.
        for it in items:
            assert it["designator"] in svg
        assert "Assembly Drawing — Top Side" in svg
        assert "Bill of Materials" in svg

    def test_pin1_rotations(self, routed, bom):
        from exporters.assembly_drawing import _generate_page
        board = routed["board"]
        # IC at each rotation to exercise pin-1 placement branches (253-260).
        items = [
            {"designator": "U1", "component_type": "ic", "x_mm": 5, "y_mm": 5,
             "footprint_width_mm": 4, "footprint_height_mm": 4, "rotation_deg": rot}
            for rot in (0, 90, 180, 270)
        ]
        # A diode adds the polarity "+" mark.
        items.append({"designator": "D1", "component_type": "diode", "x_mm": 15,
                      "y_mm": 5, "footprint_width_mm": 2, "footprint_height_mm": 1,
                      "rotation_deg": 0})
        svg = _generate_page(board, 30, 30, items, {}, "p", "Top", routed)
        assert svg.count("<circle") >= 4  # one pin-1 dot per IC
        assert "+" in svg  # diode polarity mark

    def test_polygon_outline(self, bom):
        from exporters.assembly_drawing import _generate_page
        board = {"outline_vertices": [[0, 0], [10, 0], [10, 8], [5, 10], [0, 8]]}
        svg = _generate_page(board, 10, 10, [], {}, "p", "Top",
                             {"board": board})
        assert "<polygon" in svg  # 183-187

    def test_des_sort_key_fallback(self):
        from exporters.assembly_drawing import _des_sort_key
        assert _des_sort_key("R12") == ("R", 12)
        assert _des_sort_key("weird") == ("weird", 0)  # 391

    def test_export_pdf_single_page(self, tmp_path, routed, netlist, bom):
        from exporters.assembly_drawing import export_assembly_drawing
        out = export_assembly_drawing(routed, netlist, bom,
                                      tmp_path / "asm.pdf", "blink")
        data = out.read_bytes()
        assert data.startswith(b"%PDF")  # cairosvg produced a real PDF
        assert len(data) > 500

    def test_export_pdf_two_sided(self, tmp_path, netlist, bom):
        from exporters.assembly_drawing import export_assembly_drawing
        r = _load("routed")
        # Move one component to the bottom → second page + concat path (97, 109-119).
        comp = next(p for p in r["placements"]
                    if p.get("component_type") != "fiducial")
        comp["layer"] = "bottom"
        out = export_assembly_drawing(r, netlist, bom,
                                      tmp_path / "two.pdf", "blink")
        # pypdf is not installed → ImportError fallback copies first page (413-416).
        assert out.read_bytes().startswith(b"%PDF")
        # Temp per-page files were cleaned up.
        assert not list(tmp_path.glob("*.page*.pdf"))

    def test_concatenate_pdfs_fallback(self, tmp_path):
        from exporters.assembly_drawing import _concatenate_pdfs
        a = tmp_path / "a.pdf"
        a.write_bytes(b"%PDF-1.4 fake A")
        b = tmp_path / "b.pdf"
        b.write_bytes(b"%PDF-1.4 fake B")
        out = tmp_path / "merged.pdf"
        _concatenate_pdfs([a, b], out)
        # pypdf missing → first page copied (413-416).
        assert out.read_bytes() == a.read_bytes()
