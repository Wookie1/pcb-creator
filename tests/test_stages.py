"""Tests for the deterministic placement seeder and the placement stage.

Routing/DRC/export stages are exercised by the manual end-to-end flow (they
need the router / pre-routed fixtures); here we cover the fast, hermetic,
zero-LLM pieces that the agent-driven flow depends on.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from optimizers.initial_placement import (
    generate_grid_placement, generate_grid_placement_json,
)


def _tiny_netlist() -> dict:
    """Two resistors, an IC, a connector — enough to exercise placement."""
    return {
        "version": "1.0",
        "project_name": "t",
        "elements": [
            {"element_type": "component", "component_id": "comp_u1", "designator": "U1",
             "component_type": "ic", "value": "ATtiny13A", "package": "SOIC-8_3.9x4.9mm_P1.27mm"},
            {"element_type": "component", "component_id": "comp_r1", "designator": "R1",
             "component_type": "resistor", "value": "470", "package": "R_0805_2012Metric"},
            {"element_type": "component", "component_id": "comp_r2", "designator": "R2",
             "component_type": "resistor", "value": "10k", "package": "R_0805_2012Metric"},
            {"element_type": "component", "component_id": "comp_j1", "designator": "J1",
             "component_type": "connector", "value": "PWR", "package": "PinHeader_1x2"},
            {"element_type": "port", "port_id": "port_u1_1", "component_id": "comp_u1",
             "pin_number": 1, "name": "1", "electrical_type": "signal"},
            {"element_type": "port", "port_id": "port_r1_1", "component_id": "comp_r1",
             "pin_number": 1, "name": "1", "electrical_type": "passive"},
            {"element_type": "port", "port_id": "port_r2_1", "component_id": "comp_r2",
             "pin_number": 1, "name": "1", "electrical_type": "passive"},
            {"element_type": "port", "port_id": "port_j1_1", "component_id": "comp_j1",
             "pin_number": 1, "name": "1", "electrical_type": "power_out"},
            {"element_type": "net", "net_id": "net_sig", "name": "SIG", "net_class": "signal",
             "connected_port_ids": ["port_u1_1", "port_r1_1"]},
            {"element_type": "net", "net_id": "net_vcc", "name": "VCC", "net_class": "power",
             "connected_port_ids": ["port_j1_1", "port_r2_1"]},
        ],
    }


class TestGridPlacement:
    def test_returns_placement_for_all_components(self):
        p = generate_grid_placement(_tiny_netlist(), 40, 30, "t")
        assert p is not None
        assert len(p["placements"]) == 4

    def test_board_dims_recorded(self):
        p = generate_grid_placement(_tiny_netlist(), 42, 27, "t")
        assert p["board"]["width_mm"] == 42
        assert p["board"]["height_mm"] == 27

    def test_all_within_board(self):
        p = generate_grid_placement(_tiny_netlist(), 40, 30, "t")
        for item in p["placements"]:
            assert 0 <= item["x_mm"] <= 40
            assert 0 <= item["y_mm"] <= 30

    def test_placement_source_is_movable(self):
        # Only placement_source == "user" is pinned by the SA optimizer.
        p = generate_grid_placement(_tiny_netlist(), 40, 30, "t")
        assert all(item["placement_source"] != "user" for item in p["placements"])

    def test_connector_on_left_edge(self):
        p = generate_grid_placement(_tiny_netlist(), 40, 30, "t")
        j1 = next(i for i in p["placements"] if i["designator"] == "J1")
        # Connector placed in the left margin column
        assert j1["x_mm"] < 10

    def test_empty_netlist_returns_none(self):
        assert generate_grid_placement({"elements": []}, 40, 30) is None

    def test_json_wrapper_roundtrips(self):
        txt = generate_grid_placement_json(json.dumps(_tiny_netlist()), 40, 30, "t")
        assert txt is not None
        parsed = json.loads(txt)
        assert len(parsed["placements"]) == 4

    def test_json_wrapper_none_on_empty(self):
        assert generate_grid_placement_json(json.dumps({"elements": []}), 40, 30) is None


class TestRunPlacementStage:
    """stages.run_placement: deterministic grid → repair → SA optimize."""

    def _setup(self, tmp_path) -> tuple[Path, str]:
        proj = "stage_test"
        pdir = tmp_path / proj
        pdir.mkdir()
        (pdir / f"{proj}_netlist.json").write_text(json.dumps(_tiny_netlist()))
        return pdir, proj

    def _config(self):
        from orchestrator.config import OrchestratorConfig
        return OrchestratorConfig.from_env(base_dir=Path(__file__).resolve().parent.parent)

    def test_writes_placement_and_returns_stats(self, tmp_path):
        from orchestrator import stages
        pdir, proj = self._setup(tmp_path)
        r = stages.run_placement(pdir, proj, self._config(),
                                 board_width_mm=40, board_height_mm=30, seed=1)
        assert r["success"]
        assert r["component_count"] == 4
        assert (pdir / f"{proj}_placement.json").exists()
        assert r["board_width_mm"] == 40

    def test_missing_netlist_errors(self, tmp_path):
        from orchestrator import stages
        empty = tmp_path / "empty"
        empty.mkdir()
        r = stages.run_placement(empty, "empty", self._config(),
                                 board_width_mm=40, board_height_mm=30)
        assert r["success"] is False
        assert "netlist" in r["error"].lower()

    def test_reuses_board_dims_on_rerun(self, tmp_path):
        from orchestrator import stages
        pdir, proj = self._setup(tmp_path)
        stages.run_placement(pdir, proj, self._config(),
                             board_width_mm=44, board_height_mm=33, seed=1)
        # Second call without dims should reuse 44x33 from the existing placement
        r2 = stages.run_placement(pdir, proj, self._config(), seed=2)
        assert r2["success"]
        assert r2["board_width_mm"] == 44
        assert r2["board_height_mm"] == 33

    def test_deterministic_with_seed(self, tmp_path):
        from orchestrator import stages
        pdir, proj = self._setup(tmp_path)
        r1 = stages.run_placement(pdir, proj, self._config(),
                                  board_width_mm=40, board_height_mm=30, seed=7)
        pos1 = json.loads((pdir / f"{proj}_placement.json").read_text())
        r2 = stages.run_placement(pdir, proj, self._config(),
                                  board_width_mm=40, board_height_mm=30, seed=7)
        pos2 = json.loads((pdir / f"{proj}_placement.json").read_text())
        coords1 = [(p["designator"], p["x_mm"], p["y_mm"]) for p in pos1["placements"]]
        coords2 = [(p["designator"], p["x_mm"], p["y_mm"]) for p in pos2["placements"]]
        assert coords1 == coords2
