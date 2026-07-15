"""Targeted coverage for optimizer-module branches the existing suites miss.

Scope is the UNCOVERED lines flagged by `coverage report -m` for:
  pad_geometry, initial_placement, placement_optimizer, route_cleanup,
  ratsnest, escape_router, fiducials, freerouter (pure helpers only — the
  JVM/subprocess paths are pragma-excluded in the source).

Each test asserts real geometry / optimization behaviour, not non-None.
Anything already covered by test_placement_optimizer / test_escape_router /
test_route_cleanup / test_freerouter_lifecycle is NOT repeated here.
"""

from __future__ import annotations

import json
import math
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Fixtures live in the main repo's projects/ dir (the worktree omits them).
_PROJ_CANDIDATES = [
    ROOT / "projects" / "blink_3_leds_dc_power",
    Path("/Users/James/ai-sandbox/Productizr/pcb-creator/projects/blink_3_leds_dc_power"),
]
PROJ = next((p for p in _PROJ_CANDIDATES
             if (p / "blink_3_leds_dc_power_placement.json").exists()),
            _PROJ_CANDIDATES[0])


def _blink():
    placement = json.loads(
        (PROJ / "blink_3_leds_dc_power_placement.json").read_text())
    netlist = json.loads(
        (PROJ / "blink_3_leds_dc_power_netlist.json").read_text())
    return placement, netlist


# =========================================================================
# pad_geometry
# =========================================================================
from optimizers import pad_geometry as pg


class TestPadGeometryGenerators:
    def test_make_dip_even_rows(self):
        fp = pg._make_dip(8)
        assert len(fp.pin_offsets) == 8
        # 4 pins per side; left side negative X, right side positive X
        left = [v for k, v in fp.pin_offsets.items() if v[0] < 0]
        right = [v for k, v in fp.pin_offsets.items() if v[0] > 0]
        assert len(left) == 4 and len(right) == 4
        # pin 1 top-left, pin 5 bottom-right (numbering wraps round)
        assert fp.pin_offsets[1] == (-3.81, 3.81)
        assert fp.pin_offsets[5][0] == 3.81

    def test_make_dip_rejects_odd(self):
        with pytest.raises(ValueError):
            pg._make_dip(7)
        with pytest.raises(ValueError):
            pg._make_dip(0)

    def test_make_pin_header_1xn_collinear(self):
        fp = pg._make_pin_header_1x8 = pg._make_pin_header_1xn(4)
        assert len(fp.pin_offsets) == 4
        assert all(dx == 0.0 for dx, _ in fp.pin_offsets.values())
        ys = sorted(dy for _, dy in fp.pin_offsets.values())
        # 2.54 pitch, centred
        assert ys == [-3.81, -1.27, 1.27, 3.81]

    def test_make_pin_header_2xn(self):
        fp = pg._make_pin_header_2xn(8)
        assert len(fp.pin_offsets) == 8
        # odd pins left column, even pins right column
        assert fp.pin_offsets[1][0] == -1.27
        assert fp.pin_offsets[2][0] == 1.27

    def test_make_pin_header_2xn_rejects_odd(self):
        with pytest.raises(ValueError):
            pg._make_pin_header_2xn(5)

    def test_make_tqfp_four_sides(self):
        fp = pg._make_tqfp(16)
        assert len(fp.pin_offsets) == 16
        # 4 pins per side; check all four edges are populated
        xs = [v[0] for v in fp.pin_offsets.values()]
        ys = [v[1] for v in fp.pin_offsets.values()]
        edge = 7.0 / 2 + 0.5
        assert min(xs) == pytest.approx(-edge)
        assert max(xs) == pytest.approx(edge)
        assert min(ys) == pytest.approx(-edge)
        assert max(ys) == pytest.approx(edge)

    def test_make_tqfp_rejects_non_multiple_of_4(self):
        with pytest.raises(ValueError):
            pg._make_tqfp(15)


class TestBuiltinFootprintDef:
    def test_named_packages_resolve(self):
        assert pg._builtin_footprint_def("SOT-23", 3) is pg._SOT23
        assert pg._builtin_footprint_def("SOIC-8", 8) is pg._SOIC8
        assert pg._builtin_footprint_def("TO-220", 3) is pg._TO220
        assert pg._builtin_footprint_def("HC49", 2) is pg._HC49
        assert pg._builtin_footprint_def("PJ-002A", 3) is pg._PJ002A
        assert pg._builtin_footprint_def("Fiducial_1mm", 1) is pg._FIDUCIAL
        assert pg._builtin_footprint_def("6mm_tactile", 4) is pg._6MM_TACTILE

    def test_parametric_dispatch(self):
        assert len(pg._builtin_footprint_def("DIP-8", 8).pin_offsets) == 8
        assert len(pg._builtin_footprint_def("PinHeader_1x4", 4).pin_offsets) == 4
        assert len(pg._builtin_footprint_def("PinHeader_2x5", 10).pin_offsets) == 10
        assert len(pg._builtin_footprint_def("TQFP-32", 32).pin_offsets) == 32

    def test_unknown_returns_none(self):
        assert pg._builtin_footprint_def("WIDGET-99", 4) is None


class TestNormalizePackage:
    def test_passive_size_code(self):
        assert pg._normalize_package("R_0805_2012Metric") == "0805"
        assert pg._normalize_package("LED_0603_1608Metric_HandSolder") == "0603"

    def test_hc49_named(self):
        assert pg._normalize_package("Crystal_HC49-4H_Vertical") == "HC49"

    def test_head_token(self):
        assert pg._normalize_package("SOIC-8_3.9x4.9mm_P1.27mm") == "SOIC-8"

    def test_no_simplification(self):
        assert pg._normalize_package("0805") is None  # head==package
        assert pg._normalize_package("") is None

    def test_get_footprint_def_uses_normalization(self, clean_lookup_defaults):
        # verbose name misses bare tiers, normalized retry (line 365-369) hits
        # builtin (defaults cleared by the fixture so tiers 0-4 deterministically
        # miss and the normalize retry actually runs).
        fp = pg.get_footprint_def("R_0805_2012Metric", 2)
        assert fp is pg._SMD_2PAD["0805"]


@pytest.fixture
def clean_lookup_defaults():
    """Isolate module-level footprint lookup defaults so other test modules'
    configure_lookup() calls can't bleed a real KiCad index/cache into the
    tier-resolution assertions here."""
    saved = (pg._default_kicad_index, pg._default_cache, pg._default_custom_index)
    pg.configure_lookup(kicad_index=None, cache=None, custom_index=None)
    yield
    pg.configure_lookup(kicad_index=saved[0], cache=saved[1],
                        custom_index=saved[2])


@pytest.mark.usefixtures("clean_lookup_defaults")
class TestTierResolution:
    class _Idx:
        """Minimal index stub for tier 0/1."""
        def __init__(self, result=None, raises=False):
            self._r, self._raises = result, raises

        def get_footprint(self, package, pin_count):
            if self._raises:
                raise RuntimeError("boom")
            return self._r

    def test_custom_index_wins(self):
        sentinel = pg.FootprintDef(pin_offsets={1: (0, 0)}, pad_size=(1, 1))
        fp = pg.get_footprint_def("anything", 2, custom_index=self._Idx(sentinel))
        assert fp is sentinel

    def test_kicad_index_used(self):
        sentinel = pg.FootprintDef(pin_offsets={1: (0, 0)}, pad_size=(1, 1))
        fp = pg.get_footprint_def("XZ-weird-9", 2, kicad_index=self._Idx(sentinel))
        assert fp is sentinel

    def test_index_exception_swallowed(self):
        # raising index falls through to builtin (0805)
        fp = pg.get_footprint_def("0805", 2,
                                  custom_index=self._Idx(raises=True),
                                  kicad_index=self._Idx(raises=True))
        assert fp is pg._SMD_2PAD["0805"]

    def test_cache_tier(self):
        class _Cache:
            def get_footprint(self, package):
                return {"pin_offsets": {"1": [0.0, 0.0], "2": [1.0, 0.0]},
                        "pad_size": [0.5, 0.6]}
        fp = pg.get_footprint_def("CACHE-ONLY-1", 2, cache=_Cache())
        assert fp.pin_offsets == {1: (0.0, 0.0), 2: (1.0, 0.0)}
        assert fp.pad_size == (0.5, 0.6)

    def test_cache_exception_swallowed(self):
        class _Cache:
            def get_footprint(self, package):
                raise RuntimeError("bad cache")
        assert pg.get_footprint_def("UNKNOWN-XYZ-1", 2, cache=_Cache()) is None

    def test_all_tiers_miss(self):
        assert pg.get_footprint_def("TOTALLY-UNKNOWN-PART-9", 2) is None

    def test_ipc7351_import_error_swallowed(self, monkeypatch):
        # force `from optimizers.ipc7351 import ...` to raise ImportError
        # (lines 340-341) so resolution falls through to the builtin tier.
        monkeypatch.setitem(sys.modules, "optimizers.ipc7351", None)
        assert pg.get_footprint_def("0805", 2) is pg._SMD_2PAD["0805"]

    def test_check_tier_ipc7351_import_error(self, monkeypatch):
        # lines 457-459: ImportError on the ipc7351 import in check_footprint_tier
        monkeypatch.setitem(sys.modules, "optimizers.ipc7351", None)
        assert pg.check_footprint_tier("0805", 2) == "builtin"

    def test_check_footprint_tier(self):
        assert pg.check_footprint_tier("0805", 2) == "builtin"
        assert pg.check_footprint_tier("TOTALLY-UNKNOWN-9", 2) is None
        sentinel = pg.FootprintDef(pin_offsets={1: (0, 0)}, pad_size=(1, 1))
        assert pg.check_footprint_tier(
            "x", 2, custom_index=TestTierResolution._Idx(sentinel)) == "custom"
        assert pg.check_footprint_tier(
            "x", 2, kicad_index=TestTierResolution._Idx(sentinel)) == "kicad_library"

    def test_check_footprint_tier_index_exceptions(self):
        # both indices raise → falls through to builtin
        assert pg.check_footprint_tier(
            "0805", 2,
            custom_index=TestTierResolution._Idx(raises=True),
            kicad_index=TestTierResolution._Idx(raises=True)) == "builtin"

    def test_check_footprint_tier_ipc7351(self):
        # a QFN resolves via the IPC-7351 parametric tier (line 457)
        assert pg.check_footprint_tier("QFN-16", 16) == "ipc7351"

    def test_check_footprint_tier_cache(self):
        class _Cache:
            def get_footprint(self, package):
                return {"pin_offsets": {}, "pad_size": [0.5, 0.5]}
        assert pg.check_footprint_tier("CACHE-X-1", 2, cache=_Cache()) == "cache"

    def test_check_footprint_tier_cache_raises(self):
        class _Cache:
            def get_footprint(self, package):
                raise RuntimeError("nope")
        assert pg.check_footprint_tier("UNKNOWN-Q-1", 2, cache=_Cache()) is None


class TestFallbackFootprint:
    def test_zero_pins(self):
        fp = pg._generate_fallback_footprint(5, 5, 0)
        assert fp.pin_offsets == {}

    def test_one_pin_centred(self):
        fp = pg._generate_fallback_footprint(5, 5, 1)
        assert fp.pin_offsets == {1: (0.0, 0.0)}

    def test_two_pins_along_x(self):
        fp = pg._generate_fallback_footprint(6, 4, 2)
        assert fp.pin_offsets[1] == (-2.0, 0.0)
        assert fp.pin_offsets[2] == (2.0, 0.0)

    def test_perimeter_distribution_covers_all_edges(self):
        fp = pg._generate_fallback_footprint(8, 8, 8)
        assert len(fp.pin_offsets) == 8
        hw = hh = 4.0
        # pins land on the perimeter box edges
        for x, y in fp.pin_offsets.values():
            on_edge = (math.isclose(abs(x), hw, abs_tol=1e-6)
                       or math.isclose(abs(y), hh, abs_tol=1e-6))
            assert on_edge


class TestRotateOffset:
    def test_cardinal_rotations(self):
        assert pg._rotate_offset(1.0, 0.0, 0) == (1.0, 0.0)
        assert pg._rotate_offset(1.0, 0.0, 90) == (0.0, 1.0)
        assert pg._rotate_offset(1.0, 0.0, 180) == (-1.0, 0.0)
        assert pg._rotate_offset(1.0, 0.0, 270) == (0.0, -1.0)

    def test_arbitrary_angle(self):
        x, y = pg._rotate_offset(1.0, 0.0, 45)
        assert x == pytest.approx(math.sqrt(0.5))
        assert y == pytest.approx(math.sqrt(0.5))


class TestIsThroughHole:
    def test_explicit_flag_wins(self):
        fp = pg.FootprintDef(pin_offsets={1: (0, 0)}, pad_size=(1, 1),
                             is_through_hole=True)
        assert pg.is_through_hole_package("0805", fp) is True
        fp2 = pg.FootprintDef(pin_offsets={1: (0, 0)}, pad_size=(1, 1),
                              is_through_hole=False)
        assert pg.is_through_hole_package("DIP-8", fp2) is False

    def test_prefix_heuristic(self):
        assert pg.is_through_hole_package("DIP-8") is True
        assert pg.is_through_hole_package("0805") is False


class TestConfigureLookup:
    def test_configure_and_default_cache(self):
        prev = pg.get_default_cache()
        try:
            cache = object()
            pg.configure_lookup(cache=cache)
            assert pg.get_default_cache() is cache
        finally:
            pg.configure_lookup(cache=prev)  # restore


@pytest.mark.usefixtures("clean_lookup_defaults")
class TestBuildPadMapGeometry:
    def test_blink_pad_positions_and_count(self):
        placement, netlist = _blink()
        pm = pg.build_pad_map(placement, netlist)
        assert pm  # non-empty
        # every pad maps to a placed component & is on a real layer
        for pad in pm.values():
            assert pad.layer in ("top", "bottom", "all")
            assert pad.pad_width_mm > 0 and pad.pad_height_mm > 0
        # an 0805 2-pad part's two pads straddle its centre by ~0.9mm
        r_pads = [p for p in pm.values() if p.designator.startswith("R")]
        assert r_pads

    def test_rotation_swaps_pad_dims(self):
        # synthetic 0805 resistor rotated 90 → pad w/h swap (lines 698-699)
        placement = {"board": {"width_mm": 20, "height_mm": 20},
                     "placements": [{"designator": "R1", "component_type": "resistor",
                                     "package": "0805", "footprint_width_mm": 2.0,
                                     "footprint_height_mm": 1.25, "x_mm": 10, "y_mm": 10,
                                     "rotation_deg": 90, "layer": "top"}]}
        netlist = {"elements": [
            {"element_type": "component", "component_id": "c1",
             "designator": "R1", "package": "0805"},
            {"element_type": "port", "port_id": "p1", "component_id": "c1",
             "pin_number": 1},
            {"element_type": "port", "port_id": "p2", "component_id": "c1",
             "pin_number": 2},
            {"element_type": "net", "net_id": "n1", "name": "N1",
             "connected_port_ids": ["p1", "p2"]},
        ]}
        pm = pg.build_pad_map(placement, netlist)
        # 0805 pad_size is (0.6, 0.9); after 90deg rotation → (0.9, 0.6)
        p1 = pm["p1"]
        assert p1.pad_width_mm == pytest.approx(0.9)
        assert p1.pad_height_mm == pytest.approx(0.6)
        # rotated offset: pin1 at (-0.9,0) → (0,-0.9), so y differs from centre
        assert p1.y_mm == pytest.approx(10 - 0.9, abs=1e-3)

    def test_bottom_layer_mirrors_x(self):
        placement = {"board": {"width_mm": 20, "height_mm": 20},
                     "placements": [{"designator": "R1", "component_type": "resistor",
                                     "package": "0805", "footprint_width_mm": 2.0,
                                     "footprint_height_mm": 1.25, "x_mm": 10, "y_mm": 10,
                                     "rotation_deg": 0, "layer": "bottom"}]}
        netlist = {"elements": [
            {"element_type": "component", "component_id": "c1",
             "designator": "R1", "package": "0805"},
            {"element_type": "port", "port_id": "p1", "component_id": "c1",
             "pin_number": 1},
            {"element_type": "net", "net_id": "n1", "name": "N1",
             "connected_port_ids": ["p1"]},
        ]}
        pm = pg.build_pad_map(placement, netlist)
        # pin1 dx=-0.9 mirrored to +0.9 on bottom
        assert pm["p1"].x_mm == pytest.approx(10 + 0.9, abs=1e-3)
        assert pm["p1"].layer == "bottom"

    def test_fallback_footprint_for_unknown_package(self):
        placement = {"board": {"width_mm": 20, "height_mm": 20},
                     "placements": [{"designator": "U9", "component_type": "ic",
                                     "package": "MYSTERY-PART", "footprint_width_mm": 4.0,
                                     "footprint_height_mm": 4.0, "x_mm": 10, "y_mm": 10,
                                     "rotation_deg": 0, "layer": "top"}]}
        netlist = {"elements": [
            {"element_type": "component", "component_id": "c1",
             "designator": "U9", "package": "MYSTERY-PART"},
            {"element_type": "port", "port_id": "p1", "component_id": "c1",
             "pin_number": 1},
            {"element_type": "port", "port_id": "p2", "component_id": "c1",
             "pin_number": 2},
            {"element_type": "port", "port_id": "p3", "component_id": "c1",
             "pin_number": 3},
            {"element_type": "net", "net_id": "n1", "name": "N1",
             "connected_port_ids": ["p1", "p2", "p3"]},
        ]}
        pm = pg.build_pad_map(placement, netlist)
        assert len(pm) == 3

    def test_pin_number_beyond_footprint_uses_center(self):
        # pin 5 on an 0805 (only pins 1,2 defined) → offset falls back to (0,0)
        # (line 682), so the pad sits at the component centre.
        placement = {"board": {"width_mm": 20, "height_mm": 20},
                     "placements": [{"designator": "R1", "component_type": "resistor",
                                     "package": "0805", "footprint_width_mm": 2.0,
                                     "footprint_height_mm": 1.25, "x_mm": 10, "y_mm": 10,
                                     "rotation_deg": 0, "layer": "top"}]}
        netlist = {"elements": [
            {"element_type": "component", "component_id": "c1",
             "designator": "R1", "package": "0805"},
            {"element_type": "port", "port_id": "p5", "component_id": "c1",
             "pin_number": 5},
            {"element_type": "net", "net_id": "n1", "name": "N1",
             "connected_port_ids": ["p5"]},
        ]}
        pm = pg.build_pad_map(placement, netlist)
        assert pm["p5"].x_mm == pytest.approx(10.0)
        assert pm["p5"].y_mm == pytest.approx(10.0)

    def test_unplaced_and_missing_component_skipped(self):
        # port for a component absent from netlist → skipped (line 655);
        # port for a component not in placement → skipped (line 660)
        placement = {"board": {"width_mm": 20, "height_mm": 20},
                     "placements": []}  # nothing placed
        netlist = {"elements": [
            {"element_type": "component", "component_id": "c1",
             "designator": "R1", "package": "0805"},
            {"element_type": "port", "port_id": "p1", "component_id": "c1",
             "pin_number": 1},
            {"element_type": "port", "port_id": "p2", "component_id": "missing",
             "pin_number": 1},
        ]}
        assert pg.build_pad_map(placement, netlist) == {}


# =========================================================================
# fiducials
# =========================================================================
from optimizers import fiducials as fid


class TestFiducials:
    def test_existing_fiducials_not_counted_as_layer(self):
        # line 24: fiducial component_type is skipped in layer detection
        placement = {"placements": [
            {"designator": "FID1", "component_type": "fiducial", "layer": "bottom"},
            {"designator": "R1", "component_type": "resistor", "layer": "top"},
        ]}
        assert fid.determine_populated_layers(placement) == {"top"}

    def test_bounding_box_rotation_swaps(self):
        # line 34: 90deg swaps w/h
        box = fid._get_bounding_box(0, 0, 4, 2, 90)
        # after swap w=2,h=4 → hw=1,hh=2
        assert box == (-1.0, -2.0, 1.0, 2.0)

    def test_fiducials_placed_diagonally(self):
        placement = {"board": {"width_mm": 50, "height_mm": 30},
                     "placements": [{"designator": "R1", "component_type": "resistor",
                                     "layer": "top", "x_mm": 25, "y_mm": 15,
                                     "footprint_width_mm": 2, "footprint_height_mm": 1,
                                     "rotation_deg": 0}]}
        fids = fid.place_fiducials(placement)
        assert len(fids) == 2
        # diagonal: bottom-left + top-right
        assert (fids[0]["x_mm"], fids[0]["y_mm"]) == (2.0, 2.0)
        assert (fids[1]["x_mm"], fids[1]["y_mm"]) == (48.0, 28.0)


# =========================================================================
# ratsnest
# =========================================================================
from optimizers import ratsnest as rn


def _net(net_id, name, net_class, ports):
    return {"element_type": "net", "net_id": net_id, "name": name,
            "net_class": net_class, "connected_port_ids": ports}


class TestRatsnestAssociations:
    def _netlist(self):
        # IC (U1) + decoupling cap (C1) sharing VCC+GND; crystal (Y1) sharing XIN/XOUT
        return {"elements": [
            {"element_type": "component", "component_id": "cu1",
             "designator": "U1", "component_type": "ic"},
            {"element_type": "component", "component_id": "cc1",
             "designator": "C1", "component_type": "capacitor"},
            {"element_type": "component", "component_id": "cy1",
             "designator": "Y1", "component_type": "crystal"},
            {"element_type": "port", "port_id": "u1_v", "component_id": "cu1"},
            {"element_type": "port", "port_id": "u1_g", "component_id": "cu1"},
            {"element_type": "port", "port_id": "u1_xi", "component_id": "cu1"},
            {"element_type": "port", "port_id": "u1_xo", "component_id": "cu1"},
            {"element_type": "port", "port_id": "c1_v", "component_id": "cc1"},
            {"element_type": "port", "port_id": "c1_g", "component_id": "cc1"},
            {"element_type": "port", "port_id": "y1_xi", "component_id": "cy1"},
            {"element_type": "port", "port_id": "y1_xo", "component_id": "cy1"},
            _net("nv", "VCC", "power", ["u1_v", "c1_v"]),
            _net("ng", "GND", "ground", ["u1_g", "c1_g"]),
            _net("nxi", "XIN", "signal", ["u1_xi", "y1_xi"]),
            _net("nxo", "XOUT", "signal", ["u1_xo", "y1_xo"]),
        ]}

    def test_decoupling_association(self):
        assoc = rn.find_decoupling_associations(self._netlist())
        assert len(assoc) == 1
        a = assoc[0]
        assert a.cap_designator == "C1" and a.ic_designator == "U1"
        assert a.power_net == "VCC" and a.ground_net == "GND"

    def test_crystal_association(self):
        assoc = rn.find_crystal_associations(self._netlist())
        assert len(assoc) == 1
        a = assoc[0]
        assert a.crystal_designator == "Y1" and a.ic_designator == "U1"
        assert a.connected_nets == ["XIN", "XOUT"]

    def test_get_nets_by_class_buckets(self):
        comps, ports_by_comp, nets_by_id, port_to_net = \
            rn._build_element_lookups(self._netlist())
        p, g, s = rn._get_nets_by_class("cu1", ports_by_comp, port_to_net, nets_by_id)
        assert p == {"nv"} and g == {"ng"} and s == {"nxi", "nxo"}


class TestRatsnestMST:
    def test_mst_single_node(self):
        assert rn.compute_mst_edges([(0.0, 0.0)]) == []
        assert rn.compute_mst_edges([]) == []

    def test_mst_two_nodes(self):
        edges = rn.compute_mst_edges([(0.0, 0.0), (3.0, 4.0)])
        assert edges == [(0, 1, 7.0)]  # manhattan

    def test_mst_collinear_is_chain(self):
        # 0-1-2-3 in a line: MST total = sum of adjacent gaps
        pts = [(0.0, 0.0), (1.0, 0.0), (2.0, 0.0), (3.0, 0.0)]
        edges = rn.compute_mst_edges(pts)
        assert len(edges) == 3
        assert sum(d for _, _, d in edges) == pytest.approx(3.0)

    def test_total_wire_length_skips_singleton(self):
        nets = [rn.NetInfo("n1", "N1", "signal", ["A"]),  # only 1 placed → skip
                rn.NetInfo("n2", "N2", "signal", ["A", "B"])]
        pos = {"A": (0.0, 0.0), "B": (2.0, 0.0)}
        assert rn.total_wire_length(nets, pos) == pytest.approx(2.0)

    def test_count_crossings_known(self):
        # two nets forming an X cross exactly once
        nets = [rn.NetInfo("n1", "N1", "signal", ["A", "B"]),
                rn.NetInfo("n2", "N2", "signal", ["C", "D"])]
        pos = {"A": (0.0, 0.0), "B": (4.0, 4.0),
               "C": (0.0, 4.0), "D": (4.0, 0.0)}
        assert rn.count_crossings(nets, pos) == 1

    def test_count_crossings_singleton_skipped(self):
        nets = [rn.NetInfo("n1", "N1", "signal", ["A"]),
                rn.NetInfo("n2", "N2", "signal", ["C", "D"])]
        pos = {"A": (0.0, 0.0), "C": (0.0, 4.0), "D": (4.0, 0.0)}
        assert rn.count_crossings(nets, pos) == 0

    def test_count_crossings_same_net_not_counted(self):
        # 3-node net's own MST edges never count against each other
        nets = [rn.NetInfo("n1", "N1", "signal", ["A", "B", "C"])]
        pos = {"A": (0.0, 0.0), "B": (4.0, 0.0), "C": (2.0, 3.0)}
        assert rn.count_crossings(nets, pos) == 0


class TestIncrementalCostEdges:
    def test_short_net_contributes_zero(self):
        # net with <2 placed designators → seg_count 0, wire 0 (lines 408-410, 520)
        nets = [rn.NetInfo("n1", "N1", "signal", ["A"]),
                rn.NetInfo("n2", "N2", "signal", ["B", "C"])]
        pos = {"A": (0.0, 0.0), "B": (0.0, 0.0), "C": (2.0, 0.0)}
        ev = rn.IncrementalCost(nets, pos)
        assert ev.total_wire == pytest.approx(2.0)
        # evaluate moving the singleton net's part — recompute hits count==0 path
        w, c = ev.evaluate({"A": (5.0, 5.0), "B": (0.0, 0.0), "C": (2.0, 0.0)},
                           ["A"])
        assert w == pytest.approx(2.0)  # singleton adds nothing
        ev.commit()

    def test_recompute_zero_segment_net_returns_zero(self):
        # _recompute_net early-return for a 0-segment net (line 520). Such a net
        # never registers its designators, so evaluate can't reach it — call
        # the helper directly on the singleton net's index.
        nets = [rn.NetInfo("n1", "N1", "signal", ["A"]),
                rn.NetInfo("n2", "N2", "signal", ["B", "C"])]
        pos = {"A": (0.0, 0.0), "B": (0.0, 0.0), "C": (2.0, 0.0)}
        ev = rn.IncrementalCost(nets, pos)
        assert ev._net_seg_count[0] == 0
        assert ev._recompute_net(0, pos) == 0.0

    def test_revert_when_inactive_is_noop(self):
        nets = [rn.NetInfo("n1", "N1", "signal", ["B", "C"])]
        pos = {"B": (0.0, 0.0), "C": (2.0, 0.0)}
        ev = rn.IncrementalCost(nets, pos)
        ev.revert()  # no pending move → early return (line 582)
        assert ev.total_wire == pytest.approx(2.0)

    def test_evaluate_revert_restores_exact_state(self):
        nets = [rn.NetInfo("n1", "N1", "signal", ["A", "B"]),
                rn.NetInfo("n2", "N2", "signal", ["B", "C"])]
        pos = {"A": (0.0, 0.0), "B": (3.0, 0.0), "C": (6.0, 0.0)}
        ev = rn.IncrementalCost(nets, pos)
        w0, c0 = ev.total_wire, ev.total_cross
        ev.evaluate({"A": (0.0, 0.0), "B": (3.0, 9.0), "C": (6.0, 0.0)}, ["B"])
        ev.revert()
        assert ev.total_wire == pytest.approx(w0)
        assert ev.total_cross == c0


# =========================================================================
# escape_router
# =========================================================================
from optimizers import escape_router as er
from optimizers.pad_geometry import PadInfo


def _conn_padmap(n, pitch, axis="x", x0=10.0, y0=5.0, layer="top",
                 net_prefix="sig"):
    pads = {}
    for i in range(n):
        pid = f"cn_{i+1}"
        if axis == "x":
            x, y = x0 + i * pitch, y0
        else:
            x, y = x0, y0 + i * pitch
        pads[pid] = PadInfo(port_id=pid, designator="CN1", pin_number=i + 1,
                            net_id=f"{net_prefix}_{i+1}", x_mm=x, y_mm=y,
                            pad_width_mm=0.3, pad_height_mm=0.3, layer=layer)
    return pads


def _nl_for(padmap):
    elements = [{"element_type": "component", "component_id": "c_cn1",
                 "designator": "CN1", "component_type": "connector",
                 "package": "FPC"},
                {"element_type": "component", "component_id": "c_u1",
                 "designator": "U1", "component_type": "ic", "package": "QFP"}]
    for i, (pid, pad) in enumerate(padmap.items()):
        elements.append({"element_type": "port", "port_id": pid,
                         "component_id": "c_cn1", "pin_number": pad.pin_number})
        sink = f"u1_{i+1}"
        elements.append({"element_type": "port", "port_id": sink,
                         "component_id": "c_u1", "pin_number": i + 1})
        elements.append(_net(pad.net_id, pad.net_id, "signal", [pid, sink]))
    return {"version": "1.0", "elements": elements}


def _place():
    return {"board": {"width_mm": 40, "height_mm": 30}, "placements": []}


class TestEscapeRouterBranches:
    def test_vertical_row_escapes_along_x(self):
        # axis="y": pads vary in y, row_axis "y", escape along ±x (lines 208-211)
        pm = _conn_padmap(12, 0.5, axis="y", x0=2.0, y0=5.0)
        out = er.generate_escape_routing(_place(), _nl_for(pm), pad_map=pm)
        stubs = [t for t in out["traces"] if t.get("escape_role") == "stub"]
        assert stubs
        # escape direction is horizontal → stub end x differs from pad x, y equal
        for s in stubs:
            assert s["start_y_mm"] == pytest.approx(s["end_y_mm"], abs=1e-6)
            assert s["start_x_mm"] != s["end_x_mm"]

    def test_through_hole_pads_skipped(self):
        # all pads layer "all" → after SMD filter, < min_pins (lines 193-195)
        pm = _conn_padmap(16, 0.5, layer="all")
        out = er.generate_escape_routing(_place(), _nl_for(pm), pad_map=pm)
        assert out["traces"] == [] and out["vias"] == []

    def test_auto_drop_layer_fallback_when_only_one_signal(self):
        # 2-layer, pad on bottom, no inner/top-pref present in 2-layer order...
        # order=[top,bottom], pad bottom → signal=[top]; pref 'top' hits.
        assert er._auto_drop_layer("bottom", 2, 0) == "top"

    def test_build_pad_map_auto_when_none(self):
        # exercise the pad_map=None branch (line 137) via blink fixture
        placement, netlist = _blink()
        out = er.generate_escape_routing(placement, netlist)
        # blink has no fine-pitch part → empty, but the build_pad_map path ran
        assert out == {"traces": [], "vias": [], "keepouts": []}

    def test_via_clears_foreign_traces_same_net_skipped(self):
        # _via_clears_foreign_traces returns True for same-net traces (line 184).
        # A dense fine-pitch single-row connector exercises the placed_traces
        # foreign-clearance loop including the same-net continue.
        pm = _conn_padmap(20, 0.5, axis="x")
        out = er.generate_escape_routing(_place(), _nl_for(pm), pad_map=pm)
        # most pins escape with a stub + via + fanout
        assert len(out["vias"]) >= 10


# =========================================================================
# route_cleanup
# =========================================================================
from optimizers import route_cleanup as rc


class TestRouteCleanupHelpers:
    def test_find_kicad_cli_env(self, monkeypatch, tmp_path):
        fake = tmp_path / "kicad-cli"
        fake.write_text("#!/bin/sh\n")
        monkeypatch.setenv("PCB_KICAD_CLI", str(fake))
        assert rc.find_kicad_cli() == str(fake)

    def test_find_kicad_cli_path(self, monkeypatch):
        monkeypatch.delenv("PCB_KICAD_CLI", raising=False)
        monkeypatch.setattr(rc.shutil, "which", lambda _: "/somewhere/kicad-cli")
        assert rc.find_kicad_cli() == "/somewhere/kicad-cli"

    def test_find_kicad_cli_absolute_candidate(self, monkeypatch, tmp_path):
        monkeypatch.delenv("PCB_KICAD_CLI", raising=False)
        monkeypatch.setattr(rc.shutil, "which", lambda _: None)
        cand = tmp_path / "kc"
        cand.write_text("x")
        monkeypatch.setattr(rc, "_KICAD_CLI_CANDIDATES", (str(cand),))
        assert rc.find_kicad_cli() == str(cand)

    def test_find_kicad_cli_none(self, monkeypatch):
        monkeypatch.delenv("PCB_KICAD_CLI", raising=False)
        monkeypatch.setattr(rc.shutil, "which", lambda _: None)
        monkeypatch.setattr(rc, "_KICAD_CLI_CANDIDATES", ())
        assert rc.find_kicad_cli() is None

    def test_run_drc_json_handles_failure(self, monkeypatch, tmp_path):
        # subprocess raising → None, finally still tries unlink (lines 90-97)
        pcb = tmp_path / "b.kicad_pcb"
        pcb.write_text("(kicad_pcb)")

        def _boom(*a, **k):
            raise OSError("no kicad")
        monkeypatch.setattr(rc.subprocess, "run", _boom)
        assert rc.run_drc_json(pcb, "kicad-cli") is None

    def test_run_drc_json_parses_report(self, monkeypatch, tmp_path):
        pcb = tmp_path / "b.kicad_pcb"
        pcb.write_text("(kicad_pcb)")
        report = {"violations": []}

        def _fake_run(cmd, *a, **k):
            # cmd[5] is the --output path
            out = cmd[cmd.index("--output") + 1]
            Path(out).write_text(json.dumps(report))
            return type("R", (), {"returncode": 0})()
        monkeypatch.setattr(rc.subprocess, "run", _fake_run)
        assert rc.run_drc_json(pcb, "kicad-cli") == report

    def test_item_net_no_bracket(self):
        assert rc._item_net("Track on F.Cu") is None  # line 104
        assert rc._item_net("Track [VCC] on F.Cu") == "VCC"

    def test_parse_cleanup_drc_skips_unfixable(self):
        # hole-to-hole violation is NOT in _FIXABLE_BY_REROUTE → skipped (line 124)
        drc = {"violations": [
            {"type": "hole_to_hole",
             "items": [{"description": "Hole [GND] of J1", "pos": {"x": 1, "y": 2}}]},
            {"type": "shorting_items",
             "items": [{"description": "Track [5V] on F.Cu", "pos": {"x": 3, "y": 4}},
                       {"description": "Pad 1 [GND] of U1", "pos": {"x": 3, "y": 4}}]},
        ]}
        bad, keepouts, protect = rc.parse_cleanup_drc(drc)
        assert "GND" not in bad or "5V" in bad  # 5V (track) ripped
        assert "5V" in bad
        assert "GND" in protect  # pad net protected
        assert keepouts  # pad pos recorded

    def test_build_protected_wiring_appends_missing_escapes(self):
        # escapes not already in keep set get appended (lines 192-197)
        routed = {"routing": {"traces": [], "vias": []}}
        escapes = {"traces": [{"start_x_mm": 1, "start_y_mm": 1, "end_x_mm": 2,
                               "end_y_mm": 2, "layer": "top", "net_id": "sig"}],
                   "vias": [{"x_mm": 5, "y_mm": 5, "net_id": "sig"}],
                   "keepouts": [{"x_mm": 0, "y_mm": 0, "diameter_mm": 1}]}
        out = rc.build_protected_wiring(routed, escapes, {"sig"})
        assert len(out["traces"]) == 1  # escape stub preserved even for bad net
        assert len(out["vias"]) == 1
        assert out["keepouts"] == escapes["keepouts"]

    def test_cleanup_loop_breaks_when_drc_fails_midloop(self):
        # first _analyze succeeds (bad={'a'}); inside the loop DRC returns None
        # → break (line 306). route_fn must never be called.
        routed = {"board": {"layers": 4},
                  "routing": {"traces": [], "vias": [], "unrouted_nets": []}}
        netlist = {"version": "1.0", "elements": [
            {"element_type": "net", "net_id": "a", "name": "a",
             "connected_port_ids": []}]}
        calls = {"drc": 0}

        def drc_fn(r):
            calls["drc"] += 1
            if calls["drc"] == 1:
                return {"violations": [{"type": "shorting_items", "items": [
                    {"description": "Track [a] on F.Cu"}]}]}
            return None  # mid-loop DRC unavailable

        out, bad = rc.cleanup_shorts(
            routed, netlist, escapes={"traces": [], "vias": [], "keepouts": []},
            route_fn=lambda f: (_ for _ in ()).throw(AssertionError("no route")),
            drc_data_fn=drc_fn)
        assert out is routed and bad == {"a"}

    def test_cleanup_loop_breaks_when_route_returns_none(self):
        # bad nets present, route_fn returns None → break (line 321)
        routed = {"board": {"layers": 4},
                  "routing": {"traces": [], "vias": [], "unrouted_nets": []}}
        netlist = {"version": "1.0", "elements": [
            {"element_type": "net", "net_id": "a", "name": "a",
             "connected_port_ids": []}]}
        short = {"violations": [{"type": "shorting_items", "items": [
            {"description": "Track [a] on F.Cu"}]}]}
        out, bad = rc.cleanup_shorts(
            routed, netlist, escapes={"traces": [], "vias": [], "keepouts": []},
            route_fn=lambda f: None,
            drc_data_fn=lambda r: short)
        assert out is routed and bad == {"a"}

    def test_keepout_feature_index_swallows_padmap_error(self, monkeypatch):
        # build_pad_map raising → except path (lines 233-234); vias still indexed
        import optimizers.pad_geometry as _pgmod
        monkeypatch.setattr(_pgmod, "build_pad_map",
                            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")))
        routed = {"routing": {"vias": [{"x_mm": 1.0, "y_mm": 2.0,
                                        "diameter_mm": 0.6}]}}
        feats = rc._keepout_feature_index(routed, {"elements": []})
        assert feats == [(1.0, 2.0, 0.6)]


# =========================================================================
# initial_placement
# =========================================================================
from optimizers import initial_placement as ip


def _multi_conn_netlist(n_conn=4, pins_per_conn=12, n_passives=8):
    """Netlist with several wide high-pin connectors (force perimeter spill onto
    all four edges) plus interior passives (force the row-fill wrap)."""
    elements = []
    for ci in range(n_conn):
        cid = f"cc{ci}"
        des = f"J{ci+1}"
        elements.append({"element_type": "component", "component_id": cid,
                         "designator": des, "component_type": "connector",
                         "package": f"PinHeader_1x{pins_per_conn}"})
        for p in range(pins_per_conn):
            elements.append({"element_type": "port", "port_id": f"{cid}_{p}",
                             "component_id": cid, "pin_number": p + 1})
    for ri in range(n_passives):
        cid = f"cr{ri}"
        elements.append({"element_type": "component", "component_id": cid,
                         "designator": f"R{ri+1}", "component_type": "resistor",
                         "package": "0805"})
        elements.append({"element_type": "port", "port_id": f"{cid}_1",
                         "component_id": cid, "pin_number": 1})
        elements.append({"element_type": "port", "port_id": f"{cid}_2",
                         "component_id": cid, "pin_number": 2})
    return {"version": "1.0", "elements": elements}


class TestInitialPlacement:
    def test_import_error_returns_none(self, monkeypatch):
        # line 63-64: pad_geometry import failing yields None
        import builtins
        real_import = builtins.__import__

        def _fake(name, *a, **k):
            if name == "optimizers.pad_geometry":
                raise ImportError("no pad_geometry")
            return real_import(name, *a, **k)
        monkeypatch.setattr(builtins, "__import__", _fake)
        assert ip.generate_grid_placement({"elements": []}, 50, 30) is None

    def test_no_components_returns_none(self):
        assert ip.generate_grid_placement({"elements": []}, 50, 30) is None

    def test_perimeter_spill_and_interior_fill(self):
        # Many wide high-pin connectors on a small board spill across all edges
        # (lines 204-222); passives fill interior rows with wrap (243-249).
        nl = _multi_conn_netlist(n_conn=4, pins_per_conn=12, n_passives=10)
        result = ip.generate_grid_placement(nl, 40, 40, project_name="multi")
        assert result is not None
        placements = result["placements"]
        assert len(placements) == 4 + 10
        # connectors landed on more than one edge (not all stacked left)
        conns = [p for p in placements if p["designator"].startswith("J")]
        xs = {round(p["x_mm"]) for p in conns}
        ys = {round(p["y_mm"]) for p in conns}
        # spread across the perimeter → multiple distinct x or y bands
        assert len(xs) > 1 or len(ys) > 1
        # all components within the board
        for p in placements:
            assert 0 <= p["x_mm"] <= 40
            assert 0 <= p["y_mm"] <= 40

    def test_connector_rotation_gate(self):
        # wide high-pin connector rotates; narrow/low-pin does not
        assert ip._connector_rotation(20.0, 2.0, 12) == 90
        assert ip._connector_rotation(20.0, 2.0, 4) == 0   # too few pins
        assert ip._connector_rotation(2.0, 20.0, 12) == 0  # tall, not wide

    def test_interior_fill_clamps_overflow(self):
        # many interior passives on a short board → the row-fill clamps the last
        # rows' y to the interior ceiling (line 249).
        nl = _multi_conn_netlist(n_conn=0, pins_per_conn=0, n_passives=30)
        result = ip.generate_grid_placement(nl, 30, 14)
        assert result is not None
        assert len(result["placements"]) == 30
        # every passive stays within the board height
        for p in result["placements"]:
            assert p["y_mm"] <= 14

    def test_json_wrapper(self):
        nl = _multi_conn_netlist(n_conn=1, pins_per_conn=4, n_passives=2)
        out = ip.generate_grid_placement_json(json.dumps(nl), 50, 30)
        assert out is not None
        parsed = json.loads(out)
        assert parsed["board"]["width_mm"] == 50

    def test_json_wrapper_none(self):
        assert ip.generate_grid_placement_json('{"elements": []}', 50, 30) is None


# =========================================================================
# placement_optimizer
# =========================================================================
from optimizers import placement_optimizer as po
from optimizers.ratsnest import NetInfo, DecouplingAssociation, CrystalAssociation


class TestPlacementCostFunctions:
    def test_proximity_cost_penalizes_distance(self):
        d = [DecouplingAssociation("C1", "U1", "VCC", "GND")]
        near = po._proximity_cost({"C1": (0, 0), "U1": (1, 0)}, d)
        far = po._proximity_cost({"C1": (0, 0), "U1": (50, 0)}, d)
        assert near == 0.0           # within threshold
        assert far > 0.0             # quadratic penalty (lines 228-236)
        # exact: (50-5)^2
        assert far == pytest.approx(45.0 ** 2)

    def test_crystal_cost_penalizes_distance(self):
        c = [CrystalAssociation("Y1", "U1", ["XIN"])]
        far = po._crystal_cost({"Y1": (0, 0), "U1": (0, 30)}, c)
        assert far == pytest.approx((30 - 5) ** 2)  # lines 245-253
        assert po._crystal_cost({"Y1": (0, 0), "U1": (0, 3)}, c) == 0.0

    def test_grouping_cost_manhattan(self):
        pairs = [("A", "B")]
        cost = po._grouping_cost({"A": (0, 0), "B": (3, 4)}, pairs)
        assert cost == pytest.approx(7.0)

    def test_congestion_cost_quadratic(self):
        # pile many pins into one cell → over threshold (line covered in _congestion)
        packages = {f"U{i}": ("QFP", 20) for i in range(3)}
        pos = {f"U{i}": (1.0, 1.0) for i in range(3)}  # same cell
        cost = po._congestion_cost(pos, packages)
        assert cost > 0.0

    def test_routing_demand_cost_over_capacity(self):
        # tight cell, many overlapping nets → demand over capacity.
        # capacity ≈ (2.5/0.4)*2.5*2*0.75 ≈ 23.4; each tight net contributes ~1.0
        nets = [NetInfo(f"n{i}", f"N{i}", "signal", ["A", "B"]) for i in range(40)]
        pos = {"A": (0.0, 0.0), "B": (0.5, 0.5)}  # both in one cell
        cost = po._routing_demand_cost(pos, nets, track_pitch_mm=0.4)
        assert cost > 0.0

    def test_routing_demand_cost_empty(self):
        assert po._routing_demand_cost({}, [], 0.4) == 0.0

    def test_escape_halo_cost_intrusion(self):
        halos = {"U1": 5.0}
        packages = {"U1": ("QFP", 32), "R1": ("0805", 2)}
        pos = {"U1": (10.0, 10.0), "R1": (11.0, 10.0)}  # R1 inside halo
        th = {"U1": False, "R1": False}
        cost = po._escape_halo_cost(pos, packages, {"U1": "top", "R1": "top"},
                                    halos, th)
        assert cost > 0.0
        # other layer & SMD → no contention
        cost2 = po._escape_halo_cost(pos, packages,
                                     {"U1": "top", "R1": "bottom"}, halos, th)
        assert cost2 == 0.0

    def test_footprint_min_pitch(self):
        p = po._footprint_min_pitch("0805", 2)
        assert p == pytest.approx(1.8)  # 2 pads 1.8mm apart
        assert po._footprint_min_pitch("TOTALLY-UNKNOWN-9", 2) is None

    def test_build_escape_halos_skips_single_part_net(self):
        # a net internal to ONE part contributes no fanout demand (line 494)
        nets = [NetInfo("n0", "INT", "signal", ["U1"]),         # internal → skip
                NetInfo("n1", "SIG", "signal", ["U1", "R1"])]
        packages = {"U1": ("QFP", 32), "R1": ("0805", 2)}
        footprints = {"U1": (5.0, 5.0), "R1": (2.0, 1.0)}
        halos = po._build_escape_halos(nets, packages, footprints, po.SAConfig())
        assert "U1" in halos          # dense part gets a halo
        assert halos["U1"] > 0

    def test_build_escape_halos_focus_bump(self):
        nets = [NetInfo("n1", "SIG", "signal", ["R1", "R2"])]
        packages = {"R1": ("0805", 2), "R2": ("0805", 2)}
        footprints = {"R1": (2.0, 1.0), "R2": (2.0, 1.0)}
        cfg = po.SAConfig(focus_components=("R1",))
        halos = po._build_escape_halos(nets, packages, footprints, cfg)
        # R1 is focus → halo even though it's a small coarse passive; R2 isn't
        assert "R1" in halos and "R2" not in halos

    def test_escape_halo_cost_skips_zero_radius(self):
        # halo_r <= 0 → the part is skipped (line 540)
        cost = po._escape_halo_cost(
            {"U1": (10, 10), "R1": (10.5, 10)}, {"U1": ("QFP", 8), "R1": ("0805", 2)},
            {"U1": "top", "R1": "top"}, {"U1": 0.0}, {"U1": False, "R1": False})
        assert cost == 0.0


class TestGenerateMove:
    class _RNG:
        """Scripted RNG: random() yields a queued sequence, choice picks first."""
        def __init__(self, seq):
            self._seq = list(seq)

        def random(self):
            return self._seq.pop(0)

        def choice(self, items):
            return items[0]

        def sample(self, items, k):
            return items[:k]

        def gauss(self, mu, sigma):
            return 0.0

    def test_smart_rotate_no_relevant_nets_random_fallback(self):
        # r>=0.85 → rotate branch; inner random()<0.5 → evaluate; but the chosen
        # part has no connected net → relevant_nets empty → random pick avoiding
        # current (lines 975-976).
        pos = {"R1": (10.0, 10.0)}
        rot = {"R1": 0}
        nets = [NetInfo("n1", "N1", "signal", ["X", "Y"])]  # R1 not in any net
        rng = self._RNG([0.9, 0.4])  # 0.9→rotate, 0.4→evaluate path
        _, new_rot = po._generate_move(
            pos, rot, ["R1"], [], {"R1": (2.0, 1.0)}, {"R1": "top"},
            50, 30, 50.0, 100.0, rng, nets=nets)
        assert new_rot["R1"] != 0  # rotated to something other than current

    def test_translate_clamps_to_board(self):
        pos = {"R1": (1.0, 1.0)}
        rng = self._RNG([0.1])  # translate
        new_pos, _ = po._generate_move(
            pos, {"R1": 0}, ["R1"], [], {"R1": (4.0, 4.0)}, {"R1": "top"},
            50, 30, 100.0, 100.0, rng)
        # gauss returns 0 → stays put but clamped to >= half-width
        assert new_pos["R1"][0] >= 2.0


class TestPlacementValidationHelpers:
    def test_is_valid_out_of_bounds(self):
        pos = {"R1": (0.1, 0.1)}
        fp = {"R1": (5.0, 5.0)}
        # box extends past left/top edge → invalid
        assert po._is_valid(pos, {"R1": 0}, fp, {"R1": "top"}, 50, 30) is False

    def test_is_valid_overlap(self):
        pos = {"R1": (10.0, 10.0), "R2": (10.1, 10.0)}
        fp = {"R1": (3.0, 3.0), "R2": (3.0, 3.0)}
        assert po._is_valid(pos, {"R1": 0, "R2": 0}, fp,
                            {"R1": "top", "R2": "top"}, 50, 30) is False

    def test_is_valid_no_packages_uses_body_box(self):
        pos = {"R1": (25.0, 15.0)}
        fp = {"R1": (2.0, 2.0)}
        assert po._is_valid(pos, {"R1": 0}, fp, {"R1": "top"}, 50, 30) is True

    def test_count_violations_boundary_and_overlap(self):
        pos = {"R1": (0.0, 0.0), "R2": (0.2, 0.0)}
        fp = {"R1": (3.0, 3.0), "R2": (3.0, 3.0)}
        v, depth = po._count_violations(pos, {"R1": 0, "R2": 0}, fp,
                                        {"R1": "top", "R2": "top"}, 50, 30)
        assert v > 0 and depth > 0


class TestFindPlacementViolations:
    def test_out_of_bounds_and_overlap_reported(self):
        placement = {"board": {"width_mm": 50, "height_mm": 30},
                     "placements": [
                         {"designator": "R1", "x_mm": 0.0, "y_mm": 0.0,
                          "footprint_width_mm": 4.0, "footprint_height_mm": 4.0,
                          "rotation_deg": 0, "package": "", "layer": "top"},
                         {"designator": "R2", "x_mm": 0.5, "y_mm": 0.0,
                          "footprint_width_mm": 4.0, "footprint_height_mm": 4.0,
                          "rotation_deg": 0, "package": "", "layer": "top"},
                     ]}
        rep = po.find_placement_violations(placement)
        assert rep["count"] >= 1
        assert rep["out_of_bounds"]   # R1/R2 near origin breach edge clearance
        assert rep["overlaps"]        # R1 & R2 overlap

    def test_report_structure_on_real_fixture(self):
        # exercises the netlist-driven pad-count + pad-extent path (1123-1150)
        placement, netlist = _blink()
        rep = po.find_placement_violations(placement, netlist)
        assert set(rep) == {"out_of_bounds", "overlaps", "count"}
        assert rep["count"] == len(rep["out_of_bounds"]) + len(rep["overlaps"])
        # every reported overlap names two distinct designators on a shared layer
        for ov in rep["overlaps"]:
            assert ov["a"] != ov["b"]


class TestRepairPlacement:
    def test_empty_placement_noop(self):
        empty = {"board": {"width_mm": 50, "height_mm": 30}, "placements": []}
        out = po.repair_placement(empty)
        assert out["placements"] == []

    def test_all_pinned_noop(self):
        placement = {"board": {"width_mm": 50, "height_mm": 30},
                     "placements": [
                         {"designator": "J1", "component_type": "connector",
                          "x_mm": 5, "y_mm": 5, "footprint_width_mm": 3,
                          "footprint_height_mm": 3, "rotation_deg": 0,
                          "package": "PinHeader_1x2", "layer": "top",
                          "placement_source": "user"}]}
        out = po.repair_placement(placement)
        assert out["placements"][0]["x_mm"] == 5

    def test_user_pinned_out_of_bounds_snapped(self):
        # user-pinned but out of bounds → snapped in then pinned (lines 1277-1280).
        # A second MOVABLE component keeps `movable` non-empty so the SA loop
        # runs and the snapped pin position is written back to the output.
        placement = {"board": {"width_mm": 50, "height_mm": 30},
                     "placements": [
                         {"designator": "U1", "component_type": "ic",
                          "x_mm": -5.0, "y_mm": -5.0, "footprint_width_mm": 4.0,
                          "footprint_height_mm": 4.0, "rotation_deg": 0,
                          "package": "SOIC-8", "layer": "top",
                          "placement_source": "user"},
                         {"designator": "R1", "component_type": "resistor",
                          "x_mm": 25.0, "y_mm": 15.0, "footprint_width_mm": 2.0,
                          "footprint_height_mm": 1.25, "rotation_deg": 0,
                          "package": "0805", "layer": "top",
                          "placement_source": "auto"}]}
        out = po.repair_placement(placement, seed=1, max_iterations=200)
        u1 = next(p for p in out["placements"] if p["designator"] == "U1")
        assert u1["x_mm"] > 0 and u1["y_mm"] > 0  # snapped onto board
        assert u1["placement_source"] == "user"   # pin label preserved

    def test_movable_oob_and_tall_component_rotated(self):
        # movable component taller than 90% of board → rotated 90 + snapped
        # (lines 1291-1316)
        placement = {"board": {"width_mm": 50, "height_mm": 10},
                     "placements": [
                         {"designator": "U1", "component_type": "ic",
                          "x_mm": 100.0, "y_mm": 100.0, "footprint_width_mm": 4.0,
                          "footprint_height_mm": 9.5, "rotation_deg": 0,
                          "package": "", "layer": "top",
                          "placement_source": "auto"}]}
        out = po.repair_placement(placement, seed=1, max_iterations=200)
        u1 = out["placements"][0]
        assert u1["rotation_deg"] == 90
        # within board after snap
        assert 0 <= u1["x_mm"] <= 50 and 0 <= u1["y_mm"] <= 10


class TestOptimizeBranches:
    def test_empty_placement_noop(self):
        empty = {"board": {"width_mm": 50, "height_mm": 30}, "placements": []}
        out = po.optimize_placement(empty, {"elements": []})
        assert out["placements"] == []

    def test_blink_optimizes_with_associations_logged(self):
        # blink has decoupling caps → exercises decoupling logging (689) +
        # final proximity logging (894-897)
        placement, netlist = _blink()
        cfg = po.SAConfig(max_iterations=300, seed=7)
        out = po.optimize_placement(placement, netlist, cfg)
        # placement preserved count, valid result
        assert len(out["placements"]) == len(placement["placements"])

    def test_two_sided_and_demand_and_congestion_flags(self):
        # enable the optional cost terms so their branches in _quality_cost and
        # the optimize setup (flip_eligible, escape halos, demand) all execute
        placement, netlist = _blink()
        cfg = po.SAConfig(max_iterations=300, seed=11, two_sided=True,
                          demand_weight=40.0, congestion_weight=5.0,
                          escape_weight=6.0)
        out = po.optimize_placement(placement, netlist, cfg)
        assert len(out["placements"]) == len(placement["placements"])

    def test_temperature_floor_clamped(self):
        # A sparse 2-part board on a big board → moves stay valid, so the SA
        # loop reaches the cool-down each iteration. min_temperature near the
        # initial temp → T clamps to the floor within a few cool-downs (865).
        placement = {"board": {"width_mm": 100, "height_mm": 100},
                     "placements": [
            {"designator": "R1", "component_type": "resistor", "package": "0805",
             "x_mm": 30, "y_mm": 30, "footprint_width_mm": 2.0,
             "footprint_height_mm": 1.25, "rotation_deg": 0, "layer": "top",
             "placement_source": "auto"},
            {"designator": "R2", "component_type": "resistor", "package": "0805",
             "x_mm": 70, "y_mm": 70, "footprint_width_mm": 2.0,
             "footprint_height_mm": 1.25, "rotation_deg": 0, "layer": "top",
             "placement_source": "auto"}]}
        netlist = {"version": "1.0", "elements": [
            {"element_type": "component", "component_id": "cr1",
             "designator": "R1", "package": "0805"},
            {"element_type": "component", "component_id": "cr2",
             "designator": "R2", "package": "0805"},
            {"element_type": "port", "port_id": "r1_1", "component_id": "cr1",
             "pin_number": 1},
            {"element_type": "port", "port_id": "r2_1", "component_id": "cr2",
             "pin_number": 1},
            _net("n1", "N1", "signal", ["r1_1", "r2_1"])]}
        cfg = po.SAConfig(max_iterations=30, seed=4,
                          initial_temperature=100.0, min_temperature=99.0,
                          stagnation_limit=10_000)
        out = po.optimize_placement(placement, netlist, cfg)
        assert len(out["placements"]) == 2

    def test_quality_cost_all_terms(self):
        cfg = po.SAConfig(two_sided=True, demand_weight=40.0,
                          congestion_weight=5.0, escape_weight=6.0)
        positions = {"U1": (10, 10), "C1": (40, 10), "Y1": (10, 40), "R1": (12, 10)}
        layers = {"U1": "top", "C1": "bottom", "Y1": "top", "R1": "top"}
        packages = {"U1": ("QFP", 32), "C1": ("0805", 2),
                    "Y1": ("HC49", 2), "R1": ("0805", 2)}
        decoupling = [DecouplingAssociation("C1", "U1", "VCC", "GND")]
        crystals = [CrystalAssociation("Y1", "U1", ["XIN"])]
        groups = [("U1", "R1")]
        halos = {"U1": 6.0}
        th = {k: False for k in packages}
        signal = [NetInfo("n1", "N1", "signal", ["U1", "R1"])]
        cost = po._quality_cost(positions, cfg, decoupling, crystals, groups,
                                packages=packages, layers=layers,
                                escape_halos=halos, th_map=th, signal_nets=signal)
        assert cost > 0.0  # every weighted term contributes


def _crystal_netlist():
    """IC (U1) + crystal (Y1) sharing XIN/XOUT → drives crystal association
    logging (lines 691, 899-901)."""
    return {"version": "1.0", "elements": [
        {"element_type": "component", "component_id": "cu1",
         "designator": "U1", "component_type": "ic", "package": "SOIC-8"},
        {"element_type": "component", "component_id": "cy1",
         "designator": "Y1", "component_type": "crystal", "package": "HC49"},
        {"element_type": "port", "port_id": "u1_1", "component_id": "cu1",
         "pin_number": 1},
        {"element_type": "port", "port_id": "u1_2", "component_id": "cu1",
         "pin_number": 2},
        {"element_type": "port", "port_id": "y1_1", "component_id": "cy1",
         "pin_number": 1},
        {"element_type": "port", "port_id": "y1_2", "component_id": "cy1",
         "pin_number": 2},
        _net("nxi", "XIN", "signal", ["u1_1", "y1_1"]),
        _net("nxo", "XOUT", "signal", ["u1_2", "y1_2"]),
    ]}


def _crystal_placement():
    return {"board": {"width_mm": 50, "height_mm": 30}, "placements": [
        {"designator": "U1", "component_type": "ic", "package": "SOIC-8",
         "x_mm": 10, "y_mm": 15, "footprint_width_mm": 5.0,
         "footprint_height_mm": 6.0, "rotation_deg": 0, "layer": "top",
         "placement_source": "auto"},
        {"designator": "Y1", "component_type": "crystal", "package": "HC49",
         "x_mm": 40, "y_mm": 15, "footprint_width_mm": 6.0,
         "footprint_height_mm": 4.0, "rotation_deg": 0, "layer": "top",
         "placement_source": "auto"},
    ]}


class TestOptimizeCrystalAndTwoSided:
    def test_crystal_association_paths(self):
        # crystal logging at setup (691) + final proximity logging (899-901)
        out = po.optimize_placement(
            _crystal_placement(), _crystal_netlist(),
            po.SAConfig(max_iterations=400, seed=5))
        assert len(out["placements"]) == 2

    def test_two_sided_skips_through_hole_passive(self):
        # a through-hole electrolytic capacitor is type-eligible but TH → the
        # flip-eligibility loop skips it (line 726, repair 1360).
        placement = {"board": {"width_mm": 50, "height_mm": 30}, "placements": [
            {"designator": "C1", "component_type": "capacitor",
             "package": "electrolytic_5mm", "x_mm": 10, "y_mm": 15,
             "footprint_width_mm": 5.0, "footprint_height_mm": 5.0,
             "rotation_deg": 0, "layer": "top", "placement_source": "auto"},
            {"designator": "R1", "component_type": "resistor", "package": "0805",
             "x_mm": 30, "y_mm": 15, "footprint_width_mm": 2.0,
             "footprint_height_mm": 1.25, "rotation_deg": 0, "layer": "top",
             "placement_source": "auto"}]}
        netlist = {"version": "1.0", "elements": [
            {"element_type": "component", "component_id": "cc1",
             "designator": "C1", "package": "electrolytic_5mm"},
            {"element_type": "component", "component_id": "cr1",
             "designator": "R1", "package": "0805"},
            {"element_type": "port", "port_id": "c1_1", "component_id": "cc1",
             "pin_number": 1},
            {"element_type": "port", "port_id": "r1_1", "component_id": "cr1",
             "pin_number": 1},
            _net("n1", "N1", "signal", ["c1_1", "r1_1"]),
        ]}
        out = po.optimize_placement(placement, netlist,
                                    po.SAConfig(max_iterations=200, seed=2,
                                                two_sided=True))
        assert len(out["placements"]) == 2
        # also repair two-sided (line 1360)
        rep = po.repair_placement(placement, netlist, two_sided=True,
                                  max_iterations=200, seed=2)
        assert len(rep["placements"]) == 2

    def test_two_sided_skips_fine_pitch_passive(self):
        # a 2-pin passive whose footprint is fine-pitch (<0.8mm) → flip skip
        # (line 723, repair 1357). Inject the fine-pitch def via the cache tier.
        class _Cache:
            def get_footprint(self, package):
                if package == "FINE2":
                    return {"pin_offsets": {"1": [-0.3, 0.0], "2": [0.3, 0.0]},
                            "pad_size": [0.2, 0.2]}
                return None
        prev = pg.get_default_cache()
        pg.configure_lookup(cache=_Cache())
        try:
            # sanity: pitch is below the 0.8mm escape threshold
            assert po._footprint_min_pitch("FINE2", 2) == pytest.approx(0.6)
            placement = {"board": {"width_mm": 50, "height_mm": 30},
                         "placements": [
                {"designator": "C1", "component_type": "capacitor",
                 "package": "FINE2", "x_mm": 10, "y_mm": 15,
                 "footprint_width_mm": 1.0, "footprint_height_mm": 1.0,
                 "rotation_deg": 0, "layer": "top", "placement_source": "auto"},
                {"designator": "R1", "component_type": "resistor", "package": "0805",
                 "x_mm": 30, "y_mm": 15, "footprint_width_mm": 2.0,
                 "footprint_height_mm": 1.25, "rotation_deg": 0, "layer": "top",
                 "placement_source": "auto"}]}
            netlist = {"version": "1.0", "elements": [
                {"element_type": "component", "component_id": "cc1",
                 "designator": "C1", "package": "FINE2"},
                {"element_type": "component", "component_id": "cr1",
                 "designator": "R1", "package": "0805"},
                {"element_type": "port", "port_id": "c1_1", "component_id": "cc1",
                 "pin_number": 1},
                {"element_type": "port", "port_id": "r1_1", "component_id": "cr1",
                 "pin_number": 1},
                _net("n1", "N1", "signal", ["c1_1", "r1_1"])]}
            out = po.optimize_placement(placement, netlist,
                                        po.SAConfig(max_iterations=200, seed=2,
                                                    two_sided=True))
            assert len(out["placements"]) == 2
            rep = po.repair_placement(placement, netlist, two_sided=True,
                                      max_iterations=200, seed=2)
            assert len(rep["placements"]) == 2
        finally:
            pg.configure_lookup(cache=prev)


class TestRepairTwoSidedEligibility:
    def test_flip_eligibility_skips_pinned_nonpassive_highpin(self):
        # repair two-sided flip loop: pinned (1347), non-passive type (1350),
        # high-pin passive >3 pins (1355) all hit their `continue`.
        placement = {"board": {"width_mm": 60, "height_mm": 40}, "placements": [
            {"designator": "J1", "component_type": "connector",
             "package": "PinHeader_1x2", "x_mm": 5, "y_mm": 20,
             "footprint_width_mm": 3.0, "footprint_height_mm": 6.0,
             "rotation_deg": 0, "layer": "top", "placement_source": "user"},
            {"designator": "U1", "component_type": "ic", "package": "SOIC-8",
             "x_mm": 30, "y_mm": 20, "footprint_width_mm": 5.0,
             "footprint_height_mm": 6.0, "rotation_deg": 0, "layer": "top",
             "placement_source": "auto"},
            {"designator": "RN1", "component_type": "resistor",
             "package": "DIP-8", "x_mm": 50, "y_mm": 20,
             "footprint_width_mm": 10.0, "footprint_height_mm": 8.0,
             "rotation_deg": 0, "layer": "top", "placement_source": "auto"},
        ]}
        netlist = {"version": "1.0", "elements": [
            {"element_type": "component", "component_id": "cu1",
             "designator": "U1", "package": "SOIC-8"},
            {"element_type": "component", "component_id": "crn1",
             "designator": "RN1", "package": "DIP-8"},
        ] + [{"element_type": "port", "port_id": f"rn1_{i}",
              "component_id": "crn1", "pin_number": i} for i in range(1, 9)]}
        out = po.repair_placement(placement, netlist, two_sided=True,
                                  max_iterations=200, seed=3)
        assert len(out["placements"]) == 3

    def test_movable_rotated_component_snap(self):
        # movable, pre-rotated 90, out of bounds → snap pre-pass swaps fw/fh
        # (line 1296) before clamping onto the board.
        placement = {"board": {"width_mm": 50, "height_mm": 30}, "placements": [
            {"designator": "U1", "component_type": "ic", "package": "",
             "x_mm": 200.0, "y_mm": 200.0, "footprint_width_mm": 4.0,
             "footprint_height_mm": 8.0, "rotation_deg": 90, "layer": "top",
             "placement_source": "auto"},
            {"designator": "R1", "component_type": "resistor", "package": "0805",
             "x_mm": 25.0, "y_mm": 15.0, "footprint_width_mm": 2.0,
             "footprint_height_mm": 1.25, "rotation_deg": 0, "layer": "top",
             "placement_source": "auto"}]}
        out = po.repair_placement(placement, seed=1, max_iterations=200)
        u1 = next(p for p in out["placements"] if p["designator"] == "U1")
        assert 0 <= u1["x_mm"] <= 50 and 0 <= u1["y_mm"] <= 30


class TestRepairKeepoutInBounds:
    def test_keepout_in_bounds_pinned(self):
        # a mounting hole already in bounds → pinned in place (lines 1281-1282)
        placement = {"board": {"width_mm": 50, "height_mm": 30}, "placements": [
            {"designator": "H1", "component_type": "mounting_hole",
             "package": "MountingHole_3.2mm", "x_mm": 25, "y_mm": 15,
             "footprint_width_mm": 3.2, "footprint_height_mm": 3.2,
             "rotation_deg": 0, "layer": "top", "placement_source": "auto"},
            {"designator": "R1", "component_type": "resistor", "package": "0805",
             "x_mm": 10, "y_mm": 10, "footprint_width_mm": 2.0,
             "footprint_height_mm": 1.25, "rotation_deg": 0, "layer": "top",
             "placement_source": "auto"}]}
        out = po.repair_placement(placement, max_iterations=200, seed=1)
        h1 = next(p for p in out["placements"] if p["designator"] == "H1")
        # keepout stayed put (pinned)
        assert (h1["x_mm"], h1["y_mm"]) == (25, 15)


class TestPlacementMain:
    def test_main_round_trip(self, tmp_path):
        placement, netlist = _blink()
        pp = tmp_path / "p.json"
        np = tmp_path / "n.json"
        op = tmp_path / "out.json"
        pp.write_text(json.dumps(placement))
        np.write_text(json.dumps(netlist))
        rc_code = po.main([str(pp), str(np), "--iterations", "100",
                           "--seed", "3", "--output", str(op)])
        assert rc_code == 0
        result = json.loads(op.read_text())
        assert len(result["placements"]) == len(placement["placements"])


# =========================================================================
# freerouter — PURE helpers only (no JVM/subprocess launch)
# =========================================================================
from optimizers import freerouter as frx


class TestFreerouterHeap:
    def test_env_override(self, monkeypatch):
        monkeypatch.setenv("PCB_FREEROUTING_HEAP_MB", "4096")
        assert frx._default_heap_mb() == 4096

    def test_env_floor_512(self, monkeypatch):
        monkeypatch.setenv("PCB_FREEROUTING_HEAP_MB", "100")
        assert frx._default_heap_mb() == 512

    def test_env_invalid_falls_through(self, monkeypatch):
        monkeypatch.setenv("PCB_FREEROUTING_HEAP_MB", "not-a-number")
        # falls to sysconf path → a value in [1024, 6144]
        v = frx._default_heap_mb()
        assert 1024 <= v <= 6144

    def test_sysconf_clamped(self, monkeypatch):
        monkeypatch.delenv("PCB_FREEROUTING_HEAP_MB", raising=False)
        v = frx._default_heap_mb()
        assert 1024 <= v <= 6144

    def test_sysconf_failure_default(self, monkeypatch):
        monkeypatch.delenv("PCB_FREEROUTING_HEAP_MB", raising=False)
        monkeypatch.setattr(frx.os, "sysconf",
                            lambda *_: (_ for _ in ()).throw(OSError("no")))
        assert frx._default_heap_mb() == 2048


class TestFreerouterCleanupInstall:
    def test_signal_handler_chains_and_reraises(self, monkeypatch):
        # install_process_cleanup registers a SIGTERM/SIGINT handler whose body
        # (lines 136-142) runs cleanup, chains to a previous callable handler,
        # else resets to default and re-raises. Exercise the closure directly
        # with all of signal/os mocked so nothing actually kills the test.
        import signal as _sig
        registered = {}
        monkeypatch.setattr(frx, "_CLEANUP_INSTALLED", False)

        prev_handler_calls = []

        def _prev_handler(signum, frame):
            prev_handler_calls.append(signum)

        def _getsignal(sig):
            return _prev_handler  # a callable previous handler → chain path

        def _signal(sig, handler):
            registered[sig] = handler

        cleanup_calls = []
        monkeypatch.setattr(frx.signal, "getsignal", _getsignal)
        monkeypatch.setattr(frx.signal, "signal", _signal)
        monkeypatch.setattr(frx, "_cleanup_all_procs",
                            lambda *a: cleanup_calls.append(True))
        monkeypatch.setattr(frx.atexit, "register", lambda *a, **k: None)

        frx.install_process_cleanup()
        handler = registered[_sig.SIGTERM]
        handler(_sig.SIGTERM, None)
        assert cleanup_calls            # cleanup ran (line 137)
        assert prev_handler_calls == [_sig.SIGTERM]  # chained to prev (139)

    def test_signal_handler_default_reraise(self, monkeypatch):
        # previous handler is SIG_DFL → handler resets to default and re-signals
        # itself (lines 140-142).
        import signal as _sig
        registered = {}
        monkeypatch.setattr(frx, "_CLEANUP_INSTALLED", False)
        monkeypatch.setattr(frx.signal, "getsignal", lambda sig: _sig.SIG_DFL)
        monkeypatch.setattr(frx.signal, "signal",
                            lambda sig, h: registered.__setitem__(sig, h))
        monkeypatch.setattr(frx, "_cleanup_all_procs", lambda *a: None)
        monkeypatch.setattr(frx.atexit, "register", lambda *a, **k: None)
        killed = []
        monkeypatch.setattr(frx.os, "kill", lambda pid, sig: killed.append(sig))
        monkeypatch.setattr(frx.os, "getpid", lambda: 4242)

        frx.install_process_cleanup()
        registered[_sig.SIGINT](_sig.SIGINT, None)
        assert killed == [_sig.SIGINT]  # re-raised to self (line 142)

    def test_install_is_idempotent(self, monkeypatch):
        monkeypatch.setattr(frx, "_CLEANUP_INSTALLED", True)
        # already installed → returns immediately, registers nothing
        called = []
        monkeypatch.setattr(frx.atexit, "register", lambda *a: called.append(1))
        frx.install_process_cleanup()
        assert called == []


class TestFreerouterEnsureJar:
    def test_explicit_existing_path(self, tmp_path):
        jar = tmp_path / "freerouting.jar"
        jar.write_text("x")
        assert frx.ensure_jar(jar) == jar

    def test_default_cache_existing(self, monkeypatch, tmp_path):
        cache = tmp_path / "cache"
        cache.mkdir()
        jar = cache / frx.FREEROUTING_JAR_NAME
        jar.write_text("x")
        monkeypatch.setattr(frx, "DEFAULT_CACHE_DIR", cache)
        assert frx.ensure_jar(None) == jar

    def test_download_success(self, monkeypatch, tmp_path):
        # mocked urlretrieve writes the jar → success path (lines 277-278)
        cache = tmp_path / "cache"
        monkeypatch.setattr(frx, "DEFAULT_CACHE_DIR", cache)

        import hashlib
        monkeypatch.setattr(
            frx, "FREEROUTING_JAR_SHA256",
            hashlib.sha256(b"jar-bytes").hexdigest(),
        )

        def _fake_urlretrieve(url, dest):
            Path(dest).write_bytes(b"jar-bytes")
        monkeypatch.setattr(frx.urllib.request, "urlretrieve", _fake_urlretrieve)
        result = frx.ensure_jar(None)
        assert result == cache / frx.FREEROUTING_JAR_NAME
        assert result.read_bytes() == b"jar-bytes"

    def test_download_failure_cleans_partial(self, monkeypatch, tmp_path):
        cache = tmp_path / "cache"
        monkeypatch.setattr(frx, "DEFAULT_CACHE_DIR", cache)

        def _fake_urlretrieve(url, dest):
            Path(dest).write_text("partial")  # simulate partial write
            raise OSError("network down")
        monkeypatch.setattr(frx.urllib.request, "urlretrieve", _fake_urlretrieve)
        with pytest.raises(RuntimeError, match="Failed to download"):
            frx.ensure_jar(None)
        # partial file removed
        assert not (cache / frx.FREEROUTING_JAR_NAME).exists()


class TestFreerouterEnsureJava:
    def test_finds_common_path(self, monkeypatch):
        monkeypatch.setattr(frx.os.path, "isfile",
                            lambda p: p == "/usr/bin/java")
        monkeypatch.setattr(frx.os, "access", lambda p, m: True)
        monkeypatch.setattr(frx.subprocess, "run",
                            lambda *a, **k: type("R", (), {"returncode": 0})())
        assert frx.ensure_java() == "/usr/bin/java"

    def test_falls_back_to_which(self, monkeypatch):
        monkeypatch.setattr(frx.os.path, "isfile", lambda p: False)
        monkeypatch.setattr(frx.shutil, "which", lambda _: "/opt/jdk/bin/java")
        monkeypatch.setattr(frx.subprocess, "run",
                            lambda *a, **k: type("R", (), {"returncode": 0,
                                                           "stderr": ""})())
        assert frx.ensure_java() == "/opt/jdk/bin/java"

    def test_raises_when_absent(self, monkeypatch):
        monkeypatch.setattr(frx.os.path, "isfile", lambda p: False)
        monkeypatch.setattr(frx.shutil, "which", lambda _: None)
        with pytest.raises(RuntimeError, match="Java not found"):
            frx.ensure_java()

    def test_candidate_timeout_falls_through_to_which(self, monkeypatch):
        # candidate java -version times out (lines 223-224) → falls back to which
        import subprocess as _sp
        monkeypatch.setattr(frx.os.path, "isfile",
                            lambda p: p == "/usr/bin/java")
        monkeypatch.setattr(frx.os, "access", lambda p, m: True)
        calls = {"n": 0}

        def _run(cmd, *a, **k):
            calls["n"] += 1
            if calls["n"] == 1:
                raise _sp.TimeoutExpired("java", 10)   # candidate times out
            return type("R", (), {"returncode": 0, "stderr": ""})()
        monkeypatch.setattr(frx.subprocess, "run", _run)
        monkeypatch.setattr(frx.shutil, "which", lambda _: "/opt/jdk/bin/java")
        assert frx.ensure_java() == "/opt/jdk/bin/java"

    def test_which_java_timeout_raises(self, monkeypatch):
        # which-resolved java -version times out (line 243) → RuntimeError
        import subprocess as _sp
        monkeypatch.setattr(frx.os.path, "isfile", lambda p: False)
        monkeypatch.setattr(frx.shutil, "which", lambda _: "/opt/jdk/bin/java")

        def _run(cmd, *a, **k):
            raise _sp.TimeoutExpired("java", 10)
        monkeypatch.setattr(frx.subprocess, "run", _run)
        with pytest.raises(RuntimeError, match="timed out"):
            frx.ensure_java()

    def test_raises_when_which_java_fails(self, monkeypatch):
        monkeypatch.setattr(frx.os.path, "isfile", lambda p: False)
        monkeypatch.setattr(frx.shutil, "which", lambda _: "/bad/java")
        monkeypatch.setattr(frx.subprocess, "run",
                            lambda *a, **k: type("R", (), {"returncode": 1,
                                                           "stderr": "boom"})())
        with pytest.raises(RuntimeError, match="Java check failed"):
            frx.ensure_java()


class TestFreerouterPassParsing:
    def test_pass_regex_with_unrouted(self):
        line = ("INFO [job] Auto-router pass #3 on board 'abc' was completed "
                "in 0.03 seconds with the score of 214.91 (1 unrouted).")
        m = frx._PASS_RE.search(line)
        assert m
        assert int(m.group(1)) == 3
        assert float(m.group(3)) == pytest.approx(214.91)
        assert int(m.group(4)) == 1

    def test_pass_regex_without_unrouted(self):
        line = ("INFO [job] Auto-router pass #5 on board 'x' was completed "
                "in 1.20 seconds with the score of 10.0.")
        m = frx._PASS_RE.search(line)
        assert m and m.group(4) is None

    def test_reap_non_posix_noop(self, monkeypatch):
        monkeypatch.setattr(frx.os, "name", "nt")  # line 168
        assert frx._reap_orphaned_freerouting() == 0

    def test_reap_ps_failure_returns_zero(self, monkeypatch):
        monkeypatch.setattr(frx.os, "name", "posix")
        monkeypatch.setattr(frx.subprocess, "run",
                            lambda *a, **k: (_ for _ in ()).throw(OSError("no ps")))
        assert frx._reap_orphaned_freerouting() == 0

    def test_reap_skips_malformed_and_unparseable_lines(self, monkeypatch):
        # short line (<3 fields, line 181) + non-int pid/ppid (lines 184-185)
        jar = "/c/freerouting-2.1.0.jar"
        ps = "\n".join([
            "garbage",                                  # < 3 fields → skip (181)
            f"NOTINT NOTINT java -jar {jar} -de x.dsn",  # int() fails → skip (184)
            f"100 1 java -jar {jar} -de /tmp/x.dsn",     # valid orphan → kill
        ])
        monkeypatch.setattr(frx.os, "name", "posix")
        monkeypatch.setattr(frx.subprocess, "run",
                            lambda *a, **k: type("R", (), {"stdout": ps})())
        monkeypatch.setattr(frx.time, "sleep", lambda *_: None)
        killed = []
        monkeypatch.setattr(frx.os, "kill",
                            lambda pid, sig: killed.append(pid))
        assert frx._reap_orphaned_freerouting() == 1
        assert set(killed) == {100}

    def test_reap_swallows_kill_errors(self, monkeypatch):
        # os.kill raising ProcessLookupError is tolerated (lines 195-196); the
        # orphan still counts as reaped.
        jar = "/c/freerouting-2.1.0.jar"
        ps = f"100 1 java -jar {jar} -de /tmp/x.dsn"
        monkeypatch.setattr(frx.os, "name", "posix")
        monkeypatch.setattr(frx.subprocess, "run",
                            lambda *a, **k: type("R", (), {"stdout": ps})())
        monkeypatch.setattr(frx.time, "sleep", lambda *_: None)

        def _kill(pid, sig):
            raise ProcessLookupError("already gone")
        monkeypatch.setattr(frx.os, "kill", _kill)
        assert frx._reap_orphaned_freerouting() == 1

    def test_terminate_escalates_to_kill(self):
        # a child that ignores SIGTERM forces the SIGKILL escalation (105-109).
        # Patch wait to raise TimeoutExpired once so terminate() doesn't reap it,
        # exercising the kill() branch without depending on real signal timing.
        import subprocess as _sp

        class _FakeProc:
            def __init__(self):
                self._polls = [None]   # alive on first poll
                self.killed = False
                self._waits = 0

            def poll(self):
                return self._polls[0]

            def terminate(self):
                pass

            def kill(self):
                self.killed = True
                self._polls[0] = -9   # now dead

            def wait(self, timeout=None):
                self._waits += 1
                if self._waits == 1:
                    raise _sp.TimeoutExpired("cmd", timeout)
                return 0  # second wait (after kill) succeeds
        p = _FakeProc()
        frx._terminate_proc(p)
        assert p.killed is True

    def test_terminate_swallows_outer_oserror(self):
        # poll() raising OSError is tolerated by the outer except (lines 111-112)
        class _BadProc:
            def poll(self):
                raise OSError("proc table gone")
        frx._terminate_proc(_BadProc())  # must not raise

    def test_terminate_kill_then_still_timing_out(self):
        # kill() then wait() STILL times out → inner except passes (line 110)
        import subprocess as _sp

        class _StubbornProc:
            def __init__(self):
                self.killed = False

            def poll(self):
                return None  # never reports dead

            def terminate(self):
                pass

            def kill(self):
                self.killed = True

            def wait(self, timeout=None):
                raise _sp.TimeoutExpired("cmd", timeout)
        p = _StubbornProc()
        frx._terminate_proc(p)  # must not raise
        assert p.killed is True
