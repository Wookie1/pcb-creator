"""Route-retry honesty (audit finding F2 regression).

The audit L298N board came back valid:false from BOTH the initial route and
the auto-retry, yet the step reported success and the MCP job state 'complete'
— a physically impossible board was 'kept' with no signal that attention was
due. run_route_with_retry must now FAIL such a run: keep the best artifacts
(never regress), but report success:false + a 'did not converge' error, and
hand the poller a placement-fix ladder instead of the capacity ladder.
"""

import json
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from orchestrator import stages
from orchestrator.config import OrchestratorConfig


def _cfg():
    return OrchestratorConfig.from_env(base_dir=_ROOT)


def _mk_project(tmp_path, name="f2", routed=None):
    pdir = tmp_path / "proj"
    pdir.mkdir(parents=True, exist_ok=True)
    (pdir / f"{name}_placement.json").write_text(json.dumps(
        {"board": {"width_mm": 40, "height_mm": 30, "layers": 2},
         "placements": []}))
    (pdir / f"{name}_netlist.json").write_text(json.dumps(
        {"version": "1.0", "project_name": name, "elements": []}))
    if routed is not None:
        (pdir / f"{name}_routed.json").write_text(json.dumps(routed))
    return pdir


def _route_result(completion=100, valid=False, errors=("Trace-pad short",)):
    return {"success": True, "engine": "freerouting",
            "completion_pct": completion, "valid": valid,
            "validation_errors": list(errors),
            "validation_warnings": [],
            "routed_nets": 1, "total_nets": 2, "via_count": 0,
            "trace_length_mm": 10.0, "unrouted_nets": [],
            "routed_path": "x"}


class _StubRouter:
    def __init__(self, *results):
        self.results = list(results)
        self.calls = []

    def __call__(self, *_a, **kw):
        r = self.results[min(len(self.calls), len(self.results) - 1)]
        self.calls.append(kw)
        return dict(r)


class TestRetryFailsPersistentInvalid:
    def test_replacement_retry_still_invalid_reports_failure(self, tmp_path,
                                                              monkeypatch):
        pdir = _mk_project(tmp_path)
        router = _StubRouter(_route_result(completion=50),
                             _route_result(completion=50))
        monkeypatch.setattr(stages, "run_routing", router)
        monkeypatch.setattr(stages, "run_placement",
                            lambda *_a, **_k: {"success": True})

        res = stages.run_route_with_retry(pdir, "f2", _cfg())
        assert res["success"] is False
        assert "did not converge" in res["error"]
        assert "Trace-pad short" in res["error"]
        assert res["retried"] is True
        assert len(res["attempts"]) == 2

    def test_replacement_failure_on_invalid_board_reports_failure(
            self, tmp_path, monkeypatch):
        pdir = _mk_project(tmp_path)
        monkeypatch.setattr(stages, "run_routing",
                            _StubRouter(_route_result(completion=50)))
        monkeypatch.setattr(stages, "run_placement",
                            lambda *_a, **_k: {"success": False,
                                               "error": "boom"})
        res = stages.run_route_with_retry(pdir, "f2", _cfg())
        assert res["success"] is False
        assert "did not converge" in res["error"]

    def test_incremental_finish_still_invalid_reports_failure(self, tmp_path,
                                                               monkeypatch):
        routed = {"routing": {"traces": [
            {"net_id": "n1", "layer": "bottom", "start_x_mm": 0,
             "start_y_mm": 0, "end_x_mm": 5, "end_y_mm": 0}],
            "vias": [], "unrouted_nets": []}}
        pdir = _mk_project(tmp_path, routed=routed)
        monkeypatch.setattr(stages, "run_routing",
                            _StubRouter(_route_result(), _route_result()))
        res = stages.run_route_with_retry(pdir, "f2", _cfg())
        assert len(res["attempts"]) == 2  # the incremental pass ran
        assert res["success"] is False
        assert "did not converge" in res["error"]

    def test_valid_but_incomplete_retry_still_succeeds(self, tmp_path,
                                                        monkeypatch):
        """Only INVALID retries fail: a valid, incomplete board is a normal
        intermediate the agent can finish with route_board keep_existing."""
        pdir = _mk_project(tmp_path)
        monkeypatch.setattr(stages, "run_routing",
                            _StubRouter(_route_result(completion=90,
                                                      valid=True, errors=()),
                                        _route_result(completion=95,
                                                      valid=True, errors=())))
        monkeypatch.setattr(stages, "run_placement",
                            lambda *_a, **_k: {"success": True})
        res = stages.run_route_with_retry(pdir, "f2", _cfg())
        assert res["success"] is True
        assert res["valid"] is True
        assert res["retried"] is True

    def test_valid_first_never_traded_for_invalid_second(self, tmp_path,
                                                          monkeypatch):
        pdir = _mk_project(tmp_path)
        router = _StubRouter(
            _route_result(completion=90, valid=True, errors=()),
            _route_result(completion=95, valid=False))
        monkeypatch.setattr(stages, "run_routing", router)
        monkeypatch.setattr(stages, "run_placement",
                            lambda *_a, **_k: {"success": True})
        res = stages.run_route_with_retry(pdir, "f2", _cfg())
        assert res["success"] is True          # first kept...
        assert res["valid"] is True
        assert res["completion_pct"] == 90     # despite the higher %

    def test_clean_first_returns_without_retry(self, tmp_path, monkeypatch):
        pdir = _mk_project(tmp_path)
        router = _StubRouter(_route_result(valid=True, errors=()))
        monkeypatch.setattr(stages, "run_routing", router)
        res = stages.run_route_with_retry(pdir, "f2", _cfg())
        assert res["success"] is True
        assert "retried" not in res
        assert len(res["attempts"]) == 1
        assert len(router.calls) == 1

    def test_engine_error_passes_through_unmodified(self, tmp_path,
                                                     monkeypatch):
        """A router that never finished (success:false) keeps its own error
        text — the convergence wrapper must not rewrite it."""
        pdir = _mk_project(tmp_path)
        err = {"success": False,
               "error": "Freerouting did not finish this routing run",
               "completion_pct": 0, "valid": False, "unrouted_nets": []}
        router = _StubRouter(err, err)
        monkeypatch.setattr(stages, "run_routing", router)
        monkeypatch.setattr(stages, "run_placement",
                            lambda *_a, **_k: {"success": True})
        res = stages.run_route_with_retry(pdir, "f2", _cfg())
        assert res["success"] is False
        assert "did not converge" not in res["error"]


class TestMcpFailureLadder:
    def test_convergence_failure_uses_placement_fix_ladder(self):
        import mcp_server
        step = mcp_server._route_failure_next_step(
            "nope", "Routing did not converge: even the best attempt fails "
                    "validation (3 error(s))")
        assert step["tool"] == "optimize_placement"
        assert step["args"] == {"project_name": "nope"}
        assert "not capacity-limited" in step["why"]

    def test_capacity_ladder_untouched_for_other_errors(self, tmp_path,
                                                         monkeypatch):
        import mcp_server
        pdir = tmp_path / "proj"
        pdir.mkdir()
        monkeypatch.setattr(mcp_server, "_project_dir", lambda _n: pdir)
        (pdir / "cov2_placement.json").write_text(json.dumps(
            {"board": {"layers": 2, "width_mm": 40, "height_mm": 30},
             "placements": []}))
        step = mcp_server._route_failure_next_step("cov2", "congested")
        assert step["args"].get("layers") == 4  # capacity rung unchanged