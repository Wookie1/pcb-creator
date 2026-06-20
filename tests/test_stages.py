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


class TestEscapeFanoutGating:
    """Escape fanout is tri-state: AUTO (None) enables it when the board has a
    fine-pitch part; PCB_ESCAPE_FANOUT forces it on/off."""

    def _cfg(self, base_dir):
        from orchestrator.config import OrchestratorConfig
        return OrchestratorConfig.from_env(base_dir=base_dir)

    def test_config_tristate_from_env(self, monkeypatch):
        base = Path(__file__).resolve().parent.parent
        monkeypatch.delenv("PCB_ESCAPE_FANOUT", raising=False)
        assert self._cfg(base).escape_fanout is None          # unset → AUTO
        monkeypatch.setenv("PCB_ESCAPE_FANOUT", "true")
        assert self._cfg(base).escape_fanout is True           # forced on
        monkeypatch.setenv("PCB_ESCAPE_FANOUT", "false")
        assert self._cfg(base).escape_fanout is False          # forced off

    def test_coarse_board_not_fine_pitch(self, tmp_path):
        # A 2.54mm header must NOT trip the threshold, so AUTO leaves ordinary
        # boards untouched (no escape vias on non-fine-pitch designs).
        from orchestrator.stages import _min_pad_pitch, FINE_PITCH_THRESHOLD_MM
        proj = "fp_test"
        pdir = tmp_path / proj
        pdir.mkdir()
        elements = [{"element_type": "component", "component_id": "c_cn1",
                     "designator": "CN1", "component_type": "connector",
                     "package": "PinHeader_1x16", "value": "x"}]
        for i in range(1, 17):
            elements.append({"element_type": "port", "port_id": f"p{i}",
                             "component_id": "c_cn1", "pin_number": i, "name": str(i)})
        (pdir / f"{proj}_netlist.json").write_text(
            json.dumps({"version": "1.0", "project_name": proj, "elements": elements}))
        pitch = _min_pad_pitch(pdir, proj)
        assert pitch is not None and pitch >= FINE_PITCH_THRESHOLD_MM

    def test_min_pad_pitch_none_without_netlist(self, tmp_path):
        from orchestrator.stages import _min_pad_pitch
        assert _min_pad_pitch(tmp_path, "nope") is None


class TestPinDurability:
    """set_component_positions must persist to the DURABLE pin store
    (placement_pins.json) — not only placement.json's placement_source flags —
    so batch pins survive a full placement regeneration. Regression for the
    'silent no-op' where optimize_placement scattered set_component_positions
    pins after the placement was rebuilt."""

    def test_batch_pins_survive_placement_regen(self, tmp_path, monkeypatch):
        import mcp_server
        from orchestrator import stages
        from orchestrator.config import OrchestratorConfig

        monkeypatch.setenv("PCB_PROJECTS_DIR", str(tmp_path))
        proj = "pintest"
        pdir = tmp_path / proj
        pdir.mkdir()
        (pdir / f"{proj}_netlist.json").write_text(json.dumps(_tiny_netlist()))
        mcp_server._init_lookup()

        # Pin comfortably in-bounds so the edge-clearance repair won't nudge it.
        r = mcp_server.set_component_positions(
            proj,
            [{"designator": "J1", "x_mm": 20.0, "y_mm": 15.0, "rotation_deg": 90}],
            board_width_mm=40, board_height_mm=30)
        assert r["success"] and "J1" in r["pinned_designators"]

        # The durable pin store now carries J1 (the fix).
        pins = stages.load_placement_pins(pdir, proj)
        assert "J1" in pins
        assert pins["J1"]["x_mm"] == 20.0 and pins["J1"]["rotation_deg"] == 90

        # Drop placement.json to simulate a full regen (loses placement_source
        # flags) — the exact scenario that used to scatter the pin. Only the
        # durable store remains.
        (pdir / f"{proj}_placement.json").unlink()
        cfg = OrchestratorConfig.from_env(
            base_dir=Path(__file__).resolve().parent.parent)
        res = stages.run_placement(pdir, proj, cfg,
                                   board_width_mm=40, board_height_mm=30, seed=1)
        assert res.get("success")
        placement = json.loads((pdir / f"{proj}_placement.json").read_text())
        j1 = next(p for p in placement["placements"] if p["designator"] == "J1")
        assert j1["placement_source"] == "user"
        assert abs(j1["x_mm"] - 20.0) < 0.01 and abs(j1["y_mm"] - 15.0) < 0.01


class TestSetPositionsNoOpGuard:
    """set_component_positions must NOT silently succeed when it pins nothing.

    The documented failure mode: an agent passed a typo'd designator, the tool
    skipped it with a buried 'notes' entry but returned success:True /
    pinned_count:0 and a next_step saying the pins would hold — so the agent
    proceeded and burned 30+ tool calls discovering its anchors were gone.
    A request that pins nothing is now a failure with remediation; a partial
    request surfaces the unpinned designators at the TOP level."""

    def _setup(self, tmp_path, monkeypatch):
        import mcp_server
        monkeypatch.setenv("PCB_PROJECTS_DIR", str(tmp_path))
        proj = "noop"
        pdir = tmp_path / proj
        pdir.mkdir()
        (pdir / f"{proj}_netlist.json").write_text(json.dumps(_tiny_netlist()))
        mcp_server._init_lookup()
        # A real placement so the placement-exists branch is taken.
        ok = mcp_server.set_component_positions(
            proj, [{"designator": "J1", "x_mm": 20.0, "y_mm": 15.0}],
            board_width_mm=40, board_height_mm=30)
        assert ok["success"]
        return mcp_server, proj

    def test_all_typo_fails_with_remediation(self, tmp_path, monkeypatch):
        m, proj = self._setup(tmp_path, monkeypatch)
        r = m.set_component_positions(proj, [{"designator": "J9", "x_mm": 5, "y_mm": 5}])
        assert r["success"] is False
        assert r["pinned_count"] == 0
        # Names the valid designators so the agent can correct the typo.
        assert "J1" in r["error"] and "R1" in r["error"]
        assert any(o["tool"] == "list_circuit" for o in r["remediation"])
        assert "J9" in r["known_designators"] or r["known_designators"]

    def test_missing_coords_fails(self, tmp_path, monkeypatch):
        m, proj = self._setup(tmp_path, monkeypatch)
        r = m.set_component_positions(proj, [{"designator": "R1"}])
        assert r["success"] is False and r["pinned_count"] == 0
        assert r["unpinned"][0]["reason"].startswith("missing")

    def test_partial_surfaces_unpinned_at_top_level(self, tmp_path, monkeypatch):
        m, proj = self._setup(tmp_path, monkeypatch)
        r = m.set_component_positions(
            proj, [{"designator": "R1", "x_mm": 12, "y_mm": 8},
                   {"designator": "J9", "x_mm": 1, "y_mm": 1}])
        assert r["success"] is True
        assert r["pinned_designators"] == ["R1"]
        # The failed designator is NOT buried — it is a top-level field and the
        # next_step.why warns about it.
        assert r["unpinned"][0]["designator"] == "J9"
        assert "J9" in r["warning"]
        assert "WARNING" in r["next_step"]["why"]

    def test_uncompiled_draft_steers_to_finalize(self, tmp_path, monkeypatch):
        import mcp_server
        monkeypatch.setenv("PCB_PROJECTS_DIR", str(tmp_path))
        mcp_server._init_lookup()
        r = mcp_server.create_circuit("draftonly", "x", 40, 30)
        assert r["success"]
        # Draft exists but no netlist yet — must point at finalize_circuit, not
        # the misleading "import a netlist".
        r = mcp_server.set_component_positions(
            "draftonly", [{"designator": "J1", "x_mm": 5, "y_mm": 5}],
            board_width_mm=40, board_height_mm=30)
        assert r["success"] is False
        assert any(o["tool"] == "finalize_circuit" for o in r["remediation"])
