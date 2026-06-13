"""Tests for agent-pinned placement validation: overlaps between pinned
components, pad overhang past the board edge, and mounting-hole keepouts.

Reproduces the failure modes reported from real agent runs: an agent fixes a
connector on an edge and it silently overlaps another component or a
mounting hole, or its pads hang past the board outline.
"""

import json
from pathlib import Path

import pytest

from orchestrator import circuit_builder as cb
from orchestrator.config import OrchestratorConfig
from orchestrator.stages import (
    run_placement,
    set_placement_pin,
    clear_placement_pin,
    load_placement_pins,
)
from optimizers.pad_geometry import get_footprint_def
from optimizers.placement_optimizer import find_placement_violations


@pytest.fixture()
def config():
    return OrchestratorConfig.from_env(base_dir=Path.cwd())


def _build_led_board(pdir: Path, name: str, width=30, height=20) -> None:
    """Small LED circuit via the builder (same as the agent-sim flow)."""
    assert cb.create_draft(pdir, name, "led test", width, height)["ok"]
    for des, ctype, val, pkg in [
        ("R1", "resistor", "330ohm", "0805"),
        ("D1", "led", "red", "0805"),
        ("J1", "connector", "2-pin header", "PinHeader_1x2"),
    ]:
        r = cb.add_component(pdir, name, des, ctype, val, pkg,
                             footprint_lookup=get_footprint_def)
        assert r["ok"], r
    assert cb.connect_pins(pdir, name, "VCC", ["J1.1", "R1.1"])["ok"]
    assert cb.connect_pins(pdir, name, "LED_DRIVE", ["R1.2", "D1.anode"])["ok"]
    assert cb.connect_pins(pdir, name, "GND", ["D1.cathode", "J1.2"])["ok"]
    assert cb.finalize(pdir, name)["ok"]


class TestSetPlacementPin:
    def test_pin_valid_position(self, tmp_path):
        pdir = tmp_path / "p1"
        _build_led_board(pdir, "p1")
        r = set_placement_pin(pdir, "p1", "J1", 5.0, 10.0, rotation_deg=90)
        assert r["ok"], r
        assert load_placement_pins(pdir, "p1")["J1"]["x_mm"] == 5.0

    def test_pin_unknown_designator(self, tmp_path):
        pdir = tmp_path / "p2"
        _build_led_board(pdir, "p2")
        r = set_placement_pin(pdir, "p2", "X9", 5.0, 5.0)
        assert not r["ok"]
        assert "J1" in r["error"]  # lists known designators

    def test_pin_out_of_bounds_rejected(self, tmp_path):
        """Pads hanging past the board edge are rejected at pin time."""
        pdir = tmp_path / "p3"
        _build_led_board(pdir, "p3")
        # PinHeader_1x2 pads span 2.54mm; centre at x=0.5 puts pads past the
        # left edge clearance
        r = set_placement_pin(pdir, "p3", "J1", 0.5, 10.0)
        assert not r["ok"]
        assert r["code"] == "out_of_bounds"
        assert "edge clearance" in r["error"]

    def test_pin_overlap_with_other_pin_rejected(self, tmp_path):
        pdir = tmp_path / "p4"
        _build_led_board(pdir, "p4")
        assert set_placement_pin(pdir, "p4", "J1", 10.0, 10.0)["ok"]
        r = set_placement_pin(pdir, "p4", "R1", 10.0, 10.0)
        assert not r["ok"]
        assert r["code"] == "pin_overlap"
        assert "J1" in r["error"]

    def test_bad_rotation_and_layer(self, tmp_path):
        pdir = tmp_path / "p5"
        _build_led_board(pdir, "p5")
        assert not set_placement_pin(pdir, "p5", "J1", 5, 5, rotation_deg=45)["ok"]
        assert not set_placement_pin(pdir, "p5", "J1", 5, 5, layer="middle")["ok"]

    def test_clear_pin(self, tmp_path):
        pdir = tmp_path / "p6"
        _build_led_board(pdir, "p6")
        assert set_placement_pin(pdir, "p6", "J1", 5.0, 10.0)["ok"]
        assert clear_placement_pin(pdir, "p6", "J1")["ok"]
        assert load_placement_pins(pdir, "p6") == {}
        assert not clear_placement_pin(pdir, "p6", "J1")["ok"]


class TestRunPlacementWithPins:
    def test_pins_are_applied_and_respected(self, tmp_path, config):
        pdir = tmp_path / "q1"
        _build_led_board(pdir, "q1")
        assert set_placement_pin(pdir, "q1", "J1", 6.0, 10.0, rotation_deg=90)["ok"]
        result = run_placement(pdir, "q1", config, board_width_mm=30,
                               board_height_mm=20, seed=1)
        assert result["success"], result
        placement = json.loads(
            (pdir / "q1_placement.json").read_text())
        j1 = next(p for p in placement["placements"]
                  if p["designator"] == "J1")
        assert (j1["x_mm"], j1["y_mm"]) == (6.0, 10.0)
        assert j1["rotation_deg"] == 90
        assert j1["placement_source"] == "user"

    def test_conflicting_pins_fail_with_violations(self, tmp_path, config):
        """Two components pinned on top of each other → placement FAILS with
        a structured violation report (previously it silently succeeded)."""
        pdir = tmp_path / "q2"
        _build_led_board(pdir, "q2")
        # Write conflicting pins directly (bypassing set_placement_pin's own
        # guard) — simulates an agent hand-editing placement JSON.
        pins = {"J1": {"x_mm": 10.0, "y_mm": 10.0, "rotation_deg": 0,
                       "layer": "top"},
                "R1": {"x_mm": 10.0, "y_mm": 10.0, "rotation_deg": 0,
                       "layer": "top"}}
        (pdir / "q2_placement_pins.json").write_text(json.dumps(pins))
        result = run_placement(pdir, "q2", config, board_width_mm=30,
                               board_height_mm=20, seed=1)
        assert not result["success"]
        v = result["violations"]
        assert any({o["a"], o["b"]} == {"J1", "R1"} and o["pinned"]
                   for o in v["overlaps"])
        assert "place_component" in result["error"]


class TestFindPlacementViolations:
    def _placement(self, items, w=30, h=20):
        return {"board": {"width_mm": w, "height_mm": h},
                "placements": items}

    def test_clean_placement(self):
        p = self._placement([
            {"designator": "R1", "x_mm": 8, "y_mm": 10, "rotation_deg": 0,
             "layer": "top", "package": "0805",
             "footprint_width_mm": 2.0, "footprint_height_mm": 1.25},
            {"designator": "R2", "x_mm": 20, "y_mm": 10, "rotation_deg": 0,
             "layer": "top", "package": "0805",
             "footprint_width_mm": 2.0, "footprint_height_mm": 1.25},
        ])
        assert find_placement_violations(p)["count"] == 0

    def test_edge_overhang_uses_pad_extents(self):
        """A component whose BODY is on the board but whose PADS hang past
        the edge must be flagged (the original bug)."""
        p = self._placement([
            {"designator": "J1", "x_mm": 1.0, "y_mm": 10, "rotation_deg": 90,
             "layer": "top", "package": "PinHeader_1x2",
             "footprint_width_mm": 2.5, "footprint_height_mm": 5.0},
        ])
        v = find_placement_violations(p)
        assert v["count"] >= 1
        assert v["out_of_bounds"][0]["designator"] == "J1"

    def test_pinned_pair_marked_unfixable(self):
        p = self._placement([
            {"designator": "J1", "x_mm": 10, "y_mm": 10, "rotation_deg": 0,
             "layer": "top", "package": "PinHeader_1x2",
             "placement_source": "user",
             "footprint_width_mm": 5.0, "footprint_height_mm": 2.5},
            {"designator": "J2", "x_mm": 11, "y_mm": 10, "rotation_deg": 0,
             "layer": "top", "package": "PinHeader_1x2",
             "placement_source": "user",
             "footprint_width_mm": 5.0, "footprint_height_mm": 2.5},
        ])
        v = find_placement_violations(p)
        assert any(o["pinned"] for o in v["overlaps"])

    def test_different_layers_dont_conflict(self):
        p = self._placement([
            {"designator": "R1", "x_mm": 10, "y_mm": 10, "rotation_deg": 0,
             "layer": "top", "package": "0805",
             "footprint_width_mm": 2.0, "footprint_height_mm": 1.25},
            {"designator": "R2", "x_mm": 10, "y_mm": 10, "rotation_deg": 0,
             "layer": "bottom", "package": "0805",
             "footprint_width_mm": 2.0, "footprint_height_mm": 1.25},
        ])
        assert find_placement_violations(p)["count"] == 0


class TestMountingHoles:
    def test_mounting_hole_footprint_resolves(self):
        fp = get_footprint_def("MountingHole_3.2mm_M3", 1)
        assert fp is not None
        # Keepout extent ≈ 2x the hole diameter
        assert fp.pad_size[0] == pytest.approx(6.4)

    def test_component_overlapping_hole_is_flagged(self):
        """A component sitting on a mounting hole is a violation — the hole's
        annulus is a keepout."""
        p = {"board": {"width_mm": 30, "height_mm": 20},
             "placements": [
                 {"designator": "H1", "x_mm": 4, "y_mm": 4, "rotation_deg": 0,
                  "layer": "top", "package": "MountingHole_3.2mm_M3",
                  "footprint_width_mm": 6.4, "footprint_height_mm": 6.4},
                 {"designator": "R1", "x_mm": 5, "y_mm": 4, "rotation_deg": 0,
                  "layer": "top", "package": "0805",
                  "footprint_width_mm": 2.0, "footprint_height_mm": 1.25},
             ]}
        v = find_placement_violations(p)
        assert any({o["a"], o["b"]} == {"H1", "R1"} for o in v["overlaps"])

    def test_optimizer_never_moves_mounting_holes(self, tmp_path, config):
        from optimizers.placement_optimizer import optimize_placement, SAConfig
        placement = {
            "board": {"width_mm": 40, "height_mm": 30},
            "placements": [
                {"designator": "H1", "x_mm": 4, "y_mm": 4, "rotation_deg": 0,
                 "layer": "top", "package": "MountingHole_3.2mm_M3",
                 "component_type": "ic",  # KiCad import fallback type
                 "footprint_width_mm": 6.4, "footprint_height_mm": 6.4},
                {"designator": "R1", "x_mm": 20, "y_mm": 15, "rotation_deg": 0,
                 "layer": "top", "package": "0805", "component_type": "resistor",
                 "footprint_width_mm": 2.0, "footprint_height_mm": 1.25},
                {"designator": "R2", "x_mm": 28, "y_mm": 15, "rotation_deg": 0,
                 "layer": "top", "package": "0805", "component_type": "resistor",
                 "footprint_width_mm": 2.0, "footprint_height_mm": 1.25},
            ]}
        netlist = {"version": "1.0", "project_name": "x", "elements": [
            {"element_type": "component", "component_id": "comp_r1",
             "designator": "R1", "component_type": "resistor",
             "value": "1k", "package": "0805"},
            {"element_type": "component", "component_id": "comp_r2",
             "designator": "R2", "component_type": "resistor",
             "value": "1k", "package": "0805"},
            {"element_type": "port", "port_id": "port_r1_1",
             "component_id": "comp_r1", "pin_number": 1, "name": "1",
             "electrical_type": "passive"},
            {"element_type": "port", "port_id": "port_r2_1",
             "component_id": "comp_r2", "pin_number": 1, "name": "1",
             "electrical_type": "passive"},
            {"element_type": "net", "net_id": "net_a", "name": "A",
             "connected_port_ids": ["port_r1_1", "port_r2_1"],
             "net_class": "signal"},
        ]}
        out = optimize_placement(placement, netlist, SAConfig(seed=7))
        h1 = next(p for p in out["placements"] if p["designator"] == "H1")
        assert (h1["x_mm"], h1["y_mm"]) == (4, 4)
