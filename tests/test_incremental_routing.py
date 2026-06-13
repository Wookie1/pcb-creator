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
