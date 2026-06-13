"""Tests for two-sided placement: the SA layer-flip move, bottom-side pad
mirroring, and layer-conflict rules (TH parts block both sides)."""

import json
from pathlib import Path

import pytest

from optimizers.placement_optimizer import (
    optimize_placement,
    repair_placement,
    find_placement_violations,
    SAConfig,
    _layers_conflict,
    _effective_layer,
)
from optimizers.pad_geometry import build_pad_map


def _netlist(n_resistors):
    elements = []
    for i in range(1, n_resistors + 1):
        cid = f"comp_r{i}"
        elements.append({"element_type": "component", "component_id": cid,
                         "designator": f"R{i}", "component_type": "resistor",
                         "value": "1k", "package": "0805"})
        for p in (1, 2):
            elements.append({"element_type": "port",
                             "port_id": f"port_r{i}_{p}",
                             "component_id": cid, "pin_number": p,
                             "name": str(p), "electrical_type": "passive"})
    # Chain them into 2-pin nets
    for i in range(1, n_resistors):
        elements.append({"element_type": "net", "net_id": f"net_{i}",
                         "name": f"N{i}",
                         "connected_port_ids": [f"port_r{i}_2",
                                                f"port_r{i + 1}_1"],
                         "net_class": "signal"})
    return {"version": "1.0", "project_name": "t", "elements": elements}


def _placement(items, w=20, h=10):
    return {"version": "1.0", "project_name": "t",
            "board": {"width_mm": w, "height_mm": h},
            "placements": items}


def _r(des, x, y, layer="top"):
    return {"designator": des, "package": "0805",
            "component_type": "resistor", "x_mm": x, "y_mm": y,
            "rotation_deg": 0, "layer": layer,
            "footprint_width_mm": 2.0, "footprint_height_mm": 1.25}


class TestLayerRules:
    def test_layers_conflict(self):
        assert _layers_conflict("top", "top")
        assert not _layers_conflict("top", "bottom")
        assert _layers_conflict("all", "bottom")
        assert _layers_conflict("top", "all")

    def test_th_part_blocks_both_sides(self):
        assert _effective_layer("PinHeader_1x2", 2, "top") == "all"
        assert _effective_layer("0805", 2, "top") == "top"
        assert _effective_layer("0805", 2, "bottom") == "bottom"

    def test_smd_opposite_layers_dont_violate(self):
        p = _placement([_r("R1", 5, 5, "top"), _r("R2", 5, 5, "bottom")])
        assert find_placement_violations(p)["count"] == 0

    def test_smd_on_th_bottom_pads_violates(self):
        p = _placement([
            {"designator": "J1", "package": "PinHeader_1x2",
             "component_type": "connector", "x_mm": 5, "y_mm": 5,
             "rotation_deg": 0, "layer": "top",
             "footprint_width_mm": 2.5, "footprint_height_mm": 5.0},
            _r("R1", 5, 5, "bottom"),
        ])
        v = find_placement_violations(p)
        assert any({o["a"], o["b"]} == {"J1", "R1"} for o in v["overlaps"])


class TestBottomMirror:
    def test_bottom_pads_mirrored_about_y(self):
        """Bottom-side pad offsets mirror dx -> -dx (Specctra/KiCad back
        convention, verified against Freerouting end-to-end)."""
        netlist = _netlist(1)
        top = _placement([_r("R1", 10, 5, "top")])
        bot = _placement([_r("R1", 10, 5, "bottom")])
        pm_top = build_pad_map(top, netlist)
        pm_bot = build_pad_map(bot, netlist)
        # Pin 1 flips to the other side of centre
        assert pm_top["port_r1_1"].x_mm == pytest.approx(
            2 * 10 - pm_bot["port_r1_1"].x_mm)
        assert pm_bot["port_r1_1"].layer == "bottom"


class TestRepairFlip:
    def test_overfull_top_resolves_by_flipping(self):
        """A board whose passives cannot all fit on top resolves once repair
        may flip them to the bottom."""
        netlist = _netlist(8)
        # 8 resistors crammed into 9x6mm — cannot fit single-sided with
        # 0.5mm clearances (each needs ~2.5x1.75 incl. clearance), but fits
        # comfortably split across two sides
        items = [_r(f"R{i}", 2 + (i % 3) * 2.2, 2 + (i // 3) * 1.8)
                 for i in range(1, 9)]
        p = _placement(items, w=9, h=6)

        single = repair_placement(p, netlist, seed=1, two_sided=False)
        v1 = find_placement_violations(single, netlist)
        two = repair_placement(p, netlist, seed=1, two_sided=True)
        v2 = find_placement_violations(two, netlist)

        assert v2["count"] == 0, f"two-sided repair left {v2['count']} violations"
        assert v1["count"] > 0, "expected the single-sided case to be infeasible"
        flipped = [x["designator"] for x in two["placements"]
                   if x.get("layer") == "bottom"]
        assert flipped, "two-sided repair should have used the bottom side"


class TestOptimizeFlip:
    def test_optimizer_respects_two_sided_flag_off(self):
        netlist = _netlist(4)
        items = [_r(f"R{i}", 3 + i * 4, 5) for i in range(1, 5)]
        p = _placement(items, w=22, h=10)
        out = optimize_placement(p, netlist, SAConfig(seed=3, two_sided=False))
        assert all(x.get("layer") == "top" for x in out["placements"])

    def test_pinned_and_th_never_flip(self):
        netlist = _netlist(2)
        items = [_r("R1", 5, 5), _r("R2", 12, 5)]
        items[0]["placement_source"] = "user"
        p = _placement(items, w=20, h=10)
        out = optimize_placement(p, netlist,
                                 SAConfig(seed=3, two_sided=True,
                                          congestion_weight=2.0))
        r1 = next(x for x in out["placements"] if x["designator"] == "R1")
        assert r1["layer"] == "top"


class TestFourLayerPlaneConnectivity:
    """4-layer plane delivery: SMD pads on a plane net reach the inner plane
    via via-in-pad stitching, and the connectivity check credits the solid
    plane. Regression for the bug where a single-plane net (no outer fill)
    showed every SMD pad as a disconnected group."""

    def _four_layer_routed(self):
        # GND → inner1 plane, VCC → inner2 plane. VCC pads on top AND bottom
        # must each reach the inner2 plane (the regression case).
        netlist = {"version": "1.0", "project_name": "fl", "elements": [
            {"element_type": "component", "component_id": "comp_u1",
             "designator": "U1", "component_type": "ic", "value": "x",
             "package": "SOIC-8"},
            {"element_type": "component", "component_id": "comp_c1",
             "designator": "C1", "component_type": "capacitor", "value": "100nF",
             "package": "0805"},
            {"element_type": "port", "port_id": "port_u1_8",
             "component_id": "comp_u1", "pin_number": 8, "name": "VCC",
             "electrical_type": "power_in"},
            {"element_type": "port", "port_id": "port_u1_4",
             "component_id": "comp_u1", "pin_number": 4, "name": "GND",
             "electrical_type": "ground"},
            {"element_type": "port", "port_id": "port_c1_1",
             "component_id": "comp_c1", "pin_number": 1, "name": "1",
             "electrical_type": "passive"},
            {"element_type": "port", "port_id": "port_c1_2",
             "component_id": "comp_c1", "pin_number": 2, "name": "2",
             "electrical_type": "passive"},
            {"element_type": "net", "net_id": "net_vcc", "name": "VCC",
             "net_class": "power",
             "connected_port_ids": ["port_u1_8", "port_c1_1"]},
            {"element_type": "net", "net_id": "net_gnd", "name": "GND",
             "net_class": "ground",
             "connected_port_ids": ["port_u1_4", "port_c1_2"]},
        ]}
        # C1 on the BOTTOM, U1 on top — VCC pins must each reach inner2.
        routed = {"board": {"width_mm": 20, "height_mm": 16, "layers": 4},
                  "placements": [
                      {"designator": "U1", "package": "SOIC-8",
                       "component_type": "ic", "x_mm": 6, "y_mm": 8,
                       "rotation_deg": 0, "layer": "top",
                       "footprint_width_mm": 5, "footprint_height_mm": 4},
                      {"designator": "C1", "package": "0805",
                       "component_type": "capacitor", "x_mm": 14, "y_mm": 8,
                       "rotation_deg": 0, "layer": "bottom",
                       "footprint_width_mm": 2, "footprint_height_mm": 1.25}],
                  "routing": {"traces": [], "vias": [], "config": {}}}
        from optimizers.router import apply_copper_fills, RouterConfig
        return apply_copper_fills(routed, netlist, RouterConfig()), netlist

    def test_smd_plane_pads_connected(self):
        import json, tempfile, os
        routed, netlist = self._four_layer_routed()
        # Both VCC pads (one top, one bottom) must reach the inner2 plane
        vias = routed["routing"]["vias"]
        assert vias, "expected power-plane stitch vias"
        from validators.validate_routing import validate_routing
        with tempfile.TemporaryDirectory() as td:
            rp, np_ = os.path.join(td, "r.json"), os.path.join(td, "n.json")
            json.dump(routed, open(rp, "w")); json.dump(netlist, open(np_, "w"))
            result = validate_routing(rp, np_)
        conn_errors = [e for e in result["errors"] if "disconnected" in e]
        assert not conn_errors, conn_errors
