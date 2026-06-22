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


class TestMountingHoleCorners:
    """Mounting holes are pinned by the SA optimizer (by package), so the grid
    seed is where they stay — seed them at the four corners, not the interior
    row they used to land in."""

    def _netlist_with_holes(self, n_holes: int) -> dict:
        els = []
        for i in range(1, n_holes + 1):
            els.append({"element_type": "component", "component_id": f"c_h{i}",
                        "designator": f"H{i}", "component_type": "mounting_hole",
                        "package": "MountingHole_3.2mm_M3", "value": "M3"})
        # a couple of normal parts so the interior fill still runs
        for r in ("R1", "R2"):
            els.append({"element_type": "component", "component_id": f"c_{r}",
                        "designator": r, "component_type": "resistor",
                        "package": "R_0805_2012Metric", "value": "1k"})
            els.append({"element_type": "port", "port_id": f"p_{r}",
                        "component_id": f"c_{r}", "pin_number": 1, "name": "1"})
        return {"version": "1.0", "project_name": "h", "elements": els}

    def test_four_holes_go_to_corners(self):
        p = generate_grid_placement(self._netlist_with_holes(4), 100, 50, "h")
        holes = [i for i in p["placements"] if i["designator"].startswith("H")]
        assert len(holes) == 4
        # Every hole hugs a corner: near an x-edge AND near a y-edge.
        for h in holes:
            assert (h["x_mm"] < 8 or h["x_mm"] > 92)
            assert (h["y_mm"] < 8 or h["y_mm"] > 42)
        # All four corners distinct (not stacked in a row).
        corners = {(round(h["x_mm"]) < 50, round(h["y_mm"]) < 25) for h in holes}
        assert len(corners) == 4

    def test_extra_holes_beyond_four_still_placed(self):
        p = generate_grid_placement(self._netlist_with_holes(6), 100, 50, "h")
        holes = [i for i in p["placements"] if i["designator"].startswith("H")]
        assert len(holes) == 6  # 4 corners + 2 in the interior fill

    def test_no_holes_unaffected(self):
        # Boards without mounting holes are byte-identical to before.
        p = generate_grid_placement(_tiny_netlist(), 40, 30, "t")
        assert len(p["placements"]) == 4


class TestFreePositionSuggestion:
    """A rejected place_component (overlap / out-of-bounds) hands back a
    concrete free coordinate so the agent retries instead of looping."""

    def _setup(self, tmp_path) -> tuple[Path, str]:
        proj = "sug"
        pdir = tmp_path / proj
        pdir.mkdir()
        nl = {"version": "1.0", "project_name": proj, "elements": []}
        for des in ("TB3", "TB4"):
            cid = f"c_{des}"
            nl["elements"].append(
                {"element_type": "component", "component_id": cid,
                 "designator": des, "component_type": "connector",
                 "package": "TerminalBlock_Phoenix_MKDS-1,5_1x02", "value": "x"})
            for pin in (1, 2):
                nl["elements"].append(
                    {"element_type": "port", "port_id": f"{cid}_{pin}",
                     "component_id": cid, "pin_number": pin, "name": str(pin)})
        (pdir / f"{proj}_netlist.json").write_text(json.dumps(nl))
        (pdir / f"{proj}_placement.json").write_text(json.dumps(
            {"board": {"width_mm": 100, "height_mm": 50}, "placements": []}))
        return pdir, proj

    def test_overlap_suggests_a_placeable_spot(self, tmp_path):
        from orchestrator import stages
        pdir, proj = self._setup(tmp_path)
        assert stages.set_placement_pin(pdir, proj, "TB3", 62.0, 41.5)["ok"]
        r = stages.set_placement_pin(pdir, proj, "TB4", 62.5, 41.5)  # overlaps
        assert r["code"] == "pin_overlap"
        # The message must explain it's EXTENT-based, not centre distance, so an
        # agent doesn't conclude the overlap check is buggy and unpin parts.
        assert "EXTENT" in r["error"] and "span" in r["error"]
        assert r.get("suggested_x_mm") is not None
        # The suggestion must actually be free.
        ok = stages.set_placement_pin(pdir, proj, "TB4",
                                      r["suggested_x_mm"], r["suggested_y_mm"])
        assert ok["ok"]

    def test_out_of_bounds_suggests_inward_spot(self, tmp_path):
        from orchestrator import stages
        pdir, proj = self._setup(tmp_path)
        r = stages.set_placement_pin(pdir, proj, "TB3", 99.5, 48.0)  # off-edge
        assert r["code"] == "out_of_bounds"
        assert r.get("suggested_x_mm") is not None
        ok = stages.set_placement_pin(pdir, proj, "TB3",
                                      r["suggested_x_mm"], r["suggested_y_mm"])
        assert ok["ok"]


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


class TestUnpinClearsBothSources:
    """A pin lives in two places: the durable store AND the placement file's
    placement_source=="user" flag (run_placement mirrors the durable pin into
    the file, and re-scrapes it on the next run). unplace must clear BOTH or the
    pin is silently resurrected — the 'keeps treating TB3 as pinned from the old
    placement file' bug."""

    def _cfg(self):
        from orchestrator.config import OrchestratorConfig
        return OrchestratorConfig.from_env(
            base_dir=Path(__file__).resolve().parent.parent)

    def _setup(self, tmp_path):
        proj = "resur"
        pdir = tmp_path / proj
        pdir.mkdir()
        (pdir / f"{proj}_netlist.json").write_text(json.dumps(_tiny_netlist()))
        return pdir, proj

    def test_unpin_is_not_resurrected_on_next_placement(self, tmp_path):
        from orchestrator import stages
        pdir, proj = self._setup(tmp_path)
        # Pin a plain resistor (not a connector/keepout, which are type-pinned).
        assert stages.set_placement_pin(pdir, proj, "R1", 20.0, 15.0)["ok"]
        r = stages.run_placement(pdir, proj, self._cfg(),
                                 board_width_mm=40, board_height_mm=30, seed=1)
        assert r["success"]
        assert "R1" in r["pinned_components"]
        pl = json.loads((pdir / f"{proj}_placement.json").read_text())
        assert next(p for p in pl["placements"]
                    if p["designator"] == "R1")["placement_source"] == "user"

        # Unpin, then re-place. R1 must now be movable — not resurrected.
        assert stages.clear_placement_pin(pdir, proj, "R1")["ok"]
        r2 = stages.run_placement(pdir, proj, self._cfg(), seed=1)
        assert r2["success"]
        assert "R1" not in r2["pinned_components"]
        pl2 = json.loads((pdir / f"{proj}_placement.json").read_text())
        assert next(p for p in pl2["placements"]
                    if p["designator"] == "R1")["placement_source"] != "user"

    def test_unpin_unknown_lists_true_pinned_set(self, tmp_path):
        from orchestrator import stages
        pdir, proj = self._setup(tmp_path)
        stages.set_placement_pin(pdir, proj, "R1", 20.0, 15.0)
        r = stages.clear_placement_pin(pdir, proj, "R2")  # never pinned
        assert r["ok"] is False and r["code"] == "not_pinned"
        assert "R1" in r["error"]

    def test_clear_all_pins_wipes_both_sources(self, tmp_path):
        from orchestrator import stages
        pdir, proj = self._setup(tmp_path)
        stages.set_placement_pin(pdir, proj, "R1", 20.0, 15.0)
        stages.set_placement_pin(pdir, proj, "R2", 30.0, 15.0)
        stages.run_placement(pdir, proj, self._cfg(),
                             board_width_mm=40, board_height_mm=30, seed=1)
        res = stages.clear_all_placement_pins(pdir, proj)
        assert res["ok"] and set(res["cleared"]) >= {"R1", "R2"}
        # Nothing pinned in either source afterward.
        assert stages.all_pinned_designators(pdir, proj) == []
        r2 = stages.run_placement(pdir, proj, self._cfg(), seed=1)
        assert r2["pinned_components"] == []


class TestIncrementalFinishHelpers:
    """The auto-retry finishes a near-complete board incrementally (protect the
    routed wiring, route only the residual) instead of re-placing + re-routing
    the whole board — the slow path that oscillates for minutes."""

    def test_build_fixed_routing_excludes_incomplete_nets(self, monkeypatch):
        import validators.validate_routing as vr
        from orchestrator.stages import build_incremental_fixed_routing
        # Pretend net_b is still incomplete — it must be left UNprotected.
        monkeypatch.setattr(vr, "incomplete_net_ids", lambda r, nl: {"net_b"})
        routed = {"routing": {
            "traces": [{"net_id": "net_a"}, {"net_id": "net_b"}],
            "vias": [{"net_id": "net_a"}, {"net_id": "net_b"}]}}
        fixed = build_incremental_fixed_routing(routed, {})
        assert [t["net_id"] for t in fixed["traces"]] == ["net_a"]
        assert [v["net_id"] for v in fixed["vias"]] == ["net_a"]

    def test_build_fixed_routing_none_when_nothing_routed(self):
        from orchestrator.stages import build_incremental_fixed_routing
        assert build_incremental_fixed_routing(None, {}) is None
        assert build_incremental_fixed_routing(
            {"routing": {"traces": [], "vias": []}}, {}) is None

    def test_route_score_prefers_success_then_completion(self):
        from orchestrator.stages import _route_score
        clean = {"success": True, "completion_pct": 100, "valid": True}
        partial = {"success": True, "completion_pct": 95, "valid": True}
        failed = {"success": False, "completion_pct": 99}
        assert _route_score(clean) > _route_score(partial)
        assert _route_score(partial) > _route_score(failed)  # success beats %


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
