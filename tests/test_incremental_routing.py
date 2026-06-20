"""Incremental routing: existing traces/vias become PROTECTED Specctra wiring
so Freerouting keeps them and routes only the unrouted nets (finish a
partly-routed board instead of redoing it)."""

import json
import re
import shutil
import tempfile
from pathlib import Path

import pytest

from exporters.dsn_exporter import _dsn_wiring


class TestDsnWiring:
    def test_empty_routing(self):
        assert _dsn_wiring({}, set()).strip() == "(wiring)"
        assert _dsn_wiring({"traces": [], "vias": []}, set()).strip() == "(wiring)"

    def test_traces_emitted_as_protected(self):
        routing = {"traces": [
            {"net_name": "SIG", "layer": "top", "width_mm": 0.2,
             "start_x_mm": 1.0, "start_y_mm": 2.0,
             "end_x_mm": 3.0, "end_y_mm": 2.0}],
            "vias": [{"net_name": "SIG", "x_mm": 3.0, "y_mm": 2.0}]}
        out = _dsn_wiring(routing, set())
        assert "(type protect)" in out
        assert '(net "SIG")' in out
        assert "(path F.Cu 0.2 1 2 3 2)" in out
        assert "(via Via_Default 3 2" in out

    def test_layer_mapping(self):
        routing = {"traces": [
            {"net_name": "A", "layer": "bottom", "width_mm": 0.25,
             "start_x_mm": 0, "start_y_mm": 0, "end_x_mm": 1, "end_y_mm": 0},
            {"net_name": "B", "layer": "inner2", "width_mm": 0.25,
             "start_x_mm": 0, "start_y_mm": 0, "end_x_mm": 1, "end_y_mm": 0}]}
        out = _dsn_wiring(routing, set())
        assert "path B.Cu" in out and "path In2.Cu" in out

    def test_excluded_nets_skipped(self):
        routing = {"traces": [
            {"net_name": "GND", "layer": "top", "width_mm": 0.5,
             "start_x_mm": 0, "start_y_mm": 0, "end_x_mm": 1, "end_y_mm": 0},
            {"net_name": "SIG", "layer": "top", "width_mm": 0.2,
             "start_x_mm": 0, "start_y_mm": 0, "end_x_mm": 1, "end_y_mm": 0}]}
        out = _dsn_wiring(routing, {"GND"})
        assert '(net "SIG")' in out
        assert '(net "GND")' not in out


@pytest.mark.skipif(shutil.which("java") is None, reason="Freerouting needs Java")
class TestIncrementalEndToEnd:
    """Route a small board fully, then re-route with most traces protected and
    a few nets dropped — Freerouting must finish them while preserving the
    protected wiring."""

    def _project(self, tmp):
        src = Path("projects/test_l298n_motor_driver")
        if not (src / "test_l298n_motor_driver_placement.json").exists():
            pytest.skip("l298n project data not present")
        pdir = tmp / "inc"
        pdir.mkdir(parents=True)
        for suffix in ("placement", "netlist"):
            (pdir / f"inc_{suffix}.json").write_text(
                (src / f"test_l298n_motor_driver_{suffix}.json").read_text())
        return pdir

    def test_finish_partly_routed(self, tmp_path):
        from orchestrator.config import OrchestratorConfig
        from orchestrator import stages
        from optimizers.pad_geometry import configure_lookup
        from orchestrator.cache import ComponentCache
        cfg = OrchestratorConfig.from_env(base_dir=Path.cwd())
        cfg.router_engine = "freerouting"
        configure_lookup(kicad_index=None,
                         cache=ComponentCache(cfg.component_cache_path))
        pdir = self._project(tmp_path)

        full = stages.run_routing(pdir, "inc", cfg, effort="fast")
        if full.get("completion_pct", 0) < 100:
            pytest.skip("baseline route did not complete; environment-dependent")
        traces = json.loads((pdir / "inc_routed.json").read_text())["routing"]["traces"]
        keep = traces[:len(traces) * 2 // 3]  # protect 2/3, drop the rest

        inc = stages.run_routing(pdir, "inc", cfg, effort="fast",
                                 fixed_routing={"traces": keep, "vias": []})
        assert inc.get("success")
        # Completion should be high and the protected wiring preserved
        out_traces = json.loads((pdir / "inc_routed.json").read_text())["routing"]["traces"]
        assert len(out_traces) >= len(keep)
        assert inc.get("completion_pct", 0) >= full.get("completion_pct", 0) - 5

    def test_keep_existing_complete_board_stays_100(self, tmp_path, monkeypatch):
        """Finishing an already-complete board with keep_existing protects EVERY
        net, so Freerouting writes a degenerate 'nothing to route' SES. import_ses
        then counts 0 routed nets — but the restored protected traces fully connect
        the board, so the reported completion must be recomputed to 100%, not 0%
        (otherwise the agent is told a finished board is unrouted and re-routes)."""
        import mcp_server
        from orchestrator import stages
        monkeypatch.setenv("PCB_PROJECTS_DIR", str(tmp_path / "p"))
        mcp_server._init_lookup()
        P = "kx"
        steps = [
            ("create_circuit", dict(project_name=P, description="x",
                                    board_width_mm=30, board_height_mm=20)),
            ("add_component", dict(project_name=P, designator="R1",
                                   component_type="resistor", value="330ohm",
                                   package="0805")),
            ("add_component", dict(project_name=P, designator="D1",
                                   component_type="led", value="red",
                                   package="0805")),
            ("add_component", dict(project_name=P, designator="J1",
                                   component_type="connector", value="2-pin",
                                   package="PinHeader_1x2")),
            ("connect_pins", dict(project_name=P, net_name="VCC",
                                  pins=["J1.1", "R1.1"])),
            ("connect_pins", dict(project_name=P, net_name="LED_DRIVE",
                                  pins=["R1.2", "D1.anode"])),
            ("connect_pins", dict(project_name=P, net_name="GND",
                                  pins=["D1.cathode", "J1.2"])),
            ("finalize_circuit", dict(project_name=P)),
        ]
        for tool, args in steps:
            assert getattr(mcp_server, tool)(**args)["success"]
        mcp_server.optimize_placement(P, board_width_mm=30, board_height_mm=20,
                                      seed=1)
        cfg = mcp_server._get_config()
        pdir = mcp_server._project_dir(P)

        full = stages.run_routing(pdir, P, cfg, effort="fast")
        if full.get("completion_pct", 0) < 100:
            pytest.skip("baseline route did not complete; environment-dependent")

        rt = json.loads((pdir / f"{P}_routed.json").read_text())["routing"]
        keep = {"traces": rt["traces"], "vias": rt.get("vias", [])}
        inc = stages.run_routing(pdir, P, cfg, effort="fast", fixed_routing=keep)
        assert inc["success"]
        assert inc["completion_pct"] == 100.0, inc
        assert inc["unrouted_nets"] == [], inc


class TestIncompleteNetIds:
    """incomplete_net_ids drives connectivity-aware incremental routing: it must
    flag both fully-unrouted nets and nets routed-but-split, so keep_existing
    re-routes the disconnected ones instead of protecting them forever."""

    def test_unrouted_only_when_no_netlist(self):
        from validators.validate_routing import incomplete_net_ids
        routed = {"routing": {"unrouted_nets": ["net_a", "net_b"]}}
        assert incomplete_net_ids(routed, None) == {"net_a", "net_b"}

    def test_unions_unrouted_and_disconnected(self, monkeypatch):
        import validators.validate_routing as vr
        monkeypatch.setattr(vr, "_check_connectivity", lambda r, n: (
            ["Net net_swclk: 2 disconnected groups (2 pads should all be connected)",
             "Net net_n3v3: 2 disconnected groups (6 pads should all be connected)"],
            []))
        routed = {"routing": {"unrouted_nets": ["net_gpio4"]}}
        assert vr.incomplete_net_ids(routed, {"elements": []}) == {
            "net_gpio4", "net_swclk", "net_n3v3"}

    def test_fully_connected_returns_empty(self, monkeypatch):
        import validators.validate_routing as vr
        monkeypatch.setattr(vr, "_check_connectivity", lambda r, n: ([], []))
        assert vr.incomplete_net_ids({"routing": {"unrouted_nets": []}},
                                     {"elements": []}) == set()
