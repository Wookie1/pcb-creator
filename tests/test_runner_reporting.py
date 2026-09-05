"""Honest success reporting for the runners (audit finding F3 regression).

Before the fix, run_workflow returned True and the streaming runner always
yielded success=True for steps 5/6 and the complete event — even when DRC
failed and run_export REFUSED to emit any manufacturing files, so
`run --json-output` printed success:true and exited 0. These tests pin the
truthful verdict for both runners, using the blink project as fixture data
and swapped-in DRC/export outcomes.
"""

import shutil
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from orchestrator import stages  # noqa: E402
from orchestrator import vision_review as vr_module  # noqa: E402
from orchestrator.config import OrchestratorConfig  # noqa: E402
from orchestrator.runner import (  # noqa: E402
    run_workflow,
    run_workflow_streaming,
)

_BLINK = _ROOT / "projects" / "blink_3_leds_dc_power"
_NAME = "reporttest"

_OK_STEP = SimpleNamespace(success=True, error=None,
                           output_path=Path("out.json"), qa_report=None)

_PASS_DRC = {"passed": True, "checks": [], "summary": "all clean",
             "statistics": {"errors": 0, "warnings": 0}}
_PASS_EXPORT = {"success": True, "files": []}


def _fake_step(*_a, **_k):
    return SimpleNamespace(execute=lambda **_kw: _OK_STEP,
                           progress_callback=None)


def _make_project(directory):
    class _FakeProject:
        def __init__(self, name, projects_dir):
            self.project_name = name
            self.project_dir = directory
            self.updates = []

        def get_output_path(self, fname):
            return self.project_dir / fname

        def update_status(self, *a, **k):
            self.updates.append(a)
    return _FakeProject


@pytest.fixture
def env(tmp_path, monkeypatch):
    if not _BLINK.is_dir():
        pytest.skip("blink fixture project not present")
    projects_dir = tmp_path / "projects"
    proj = projects_dir / _NAME
    proj.mkdir(parents=True)
    for suffix in ("netlist", "placement", "bom", "routed"):
        shutil.copy(_BLINK / f"blink_3_leds_dc_power_{suffix}.json",
                    proj / f"{_NAME}_{suffix}.json")

    monkeypatch.setattr("orchestrator.llm.litellm_client.LiteLLMClient",
                        lambda *a, **k: SimpleNamespace())
    monkeypatch.setattr("orchestrator.project.ProjectManager",
                        _make_project(proj))
    import orchestrator.steps.step_0_requirements as s0
    import orchestrator.steps.step_1_schematic as s1
    import orchestrator.steps.step_2_bom as s2
    import orchestrator.steps.step_3_layout as s3
    monkeypatch.setattr(s0, "RequirementsStep", _fake_step)
    monkeypatch.setattr(s1, "SchematicStep", _fake_step)
    monkeypatch.setattr(s2, "BOMStep", _fake_step)
    monkeypatch.setattr(s3, "LayoutStep", _fake_step)

    # Impl holder: tests swap outcomes via env.impl[...] = ... (the spy reads
    # the CURRENT entry, so no attribute leaks between tests).
    impl = {"drc": lambda *a, **k: dict(_PASS_DRC),
            "export": lambda *a, **k: dict(_PASS_EXPORT)}
    calls = {"log_kwargs": [], "reviews": []}

    def _spy_drc(*a, **k):
        calls["log_kwargs"].append(("drc", "log" in k))
        return impl["drc"](*a, **k)

    def _spy_export(*a, **k):
        calls["log_kwargs"].append(("export", "log" in k))
        return impl["export"](*a, **k)

    monkeypatch.setattr(stages, "run_routing",
                        lambda *a, **k: {"success": True})
    monkeypatch.setattr(stages, "run_drc", _spy_drc)
    monkeypatch.setattr(stages, "run_export", _spy_export)

    def _spy_review(*a, **k):
        calls["reviews"].append(True)
        return "approved"

    monkeypatch.setattr(vr_module, "run_vision_review", _spy_review)

    cfg = OrchestratorConfig(base_dir=_ROOT,
                             projects_dir=str(projects_dir),
                             enable_optimizer=False,
                             skip_approval=True,
                             skip_qa=False)
    return SimpleNamespace(cfg=cfg, calls=calls, impl=impl, proj=proj)


def _events(cfg):
    return list(run_workflow_streaming(Path("requirements.json"), _NAME,
                                       cfg))


def _step_done(events, step):
    return [e for e in events
            if e.get("event") == "step_done" and e.get("step") == step][-1]


def _complete(events):
    return [e for e in events if e.get("event") == "complete"][-1]


# ---------------------------------------------------------------------------
# Streaming runner (drives CLI --json-output, MCP design_pcb, and Gradio)
# ---------------------------------------------------------------------------

def test_streaming_happy_path_still_succeeds(env):
    events = _events(env.cfg)
    assert _complete(events)["success"] is True
    assert _step_done(events, 5)["success"] is True
    assert _step_done(events, 6)["success"] is True
    assert not [e for e in events if e["event"] == "error"]
    # Streaming step 6 must pass log= so a refusal prints why.
    assert ("export", True) in env.calls["log_kwargs"]
    # Vision review ran on the clean board.
    assert env.calls["reviews"]


def test_streaming_reports_drc_failure_as_failure(env):
    env.impl["drc"] = lambda *a, **k: {
        "passed": False, "checks": [],
        "summary": "12/14 checks passed, 2 errors, 0 warnings",
        "statistics": {"errors": 2, "warnings": 0}}
    events = _events(env.cfg)

    assert _step_done(events, 5)["success"] is False
    assert "DRC failed" in _step_done(events, 5)["message"]
    # Export still runs (its own gate refuses); reporting stays honest.
    assert _step_done(events, 6)["success"] is True
    assert _complete(events)["success"] is False
    # No raw "error" events: Gradio's error handler abandons the generator.
    assert not [e for e in events if e["event"] == "error"]
    # A failed DRC must not burn LLM calls on vision review.
    assert not env.calls["reviews"]


def test_streaming_reports_export_refusal_as_failure(env):
    env.impl["export"] = lambda *a, **k: {
        "success": False,
        "error": "Refusing to export: the board has 3 DRC error(s)"}
    events = _events(env.cfg)

    done6 = _step_done(events, 6)
    assert done6["success"] is False
    assert "Export refused" in done6["message"]
    assert _complete(events)["success"] is False


def test_streaming_missing_drc_dict_is_failure(env):
    # No-routed-board shape from stages.run_drc: {"success": False, "error": ...}
    env.impl["drc"] = lambda *a, **k: {
        "success": False, "error": "No routed board found"}
    events = _events(env.cfg)
    done5 = _step_done(events, 5)
    assert done5["success"] is False
    assert "No routed board" in done5["message"]
    assert _complete(events)["success"] is False


# ---------------------------------------------------------------------------
# Non-streaming runner (CLI plain output)
# ---------------------------------------------------------------------------

def test_run_workflow_success_returns_true(env):
    assert run_workflow(Path("requirements.json"), _NAME, env.cfg) is True
    assert ("export", True) in env.calls["log_kwargs"]


def test_run_workflow_drc_failure_returns_false(env):
    env.impl["drc"] = lambda *a, **k: {
        "passed": False, "checks": [], "statistics": {"errors": 2}}
    assert run_workflow(Path("requirements.json"), _NAME, env.cfg) is False


def test_run_workflow_export_refusal_returns_false(env):
    env.impl["export"] = lambda *a, **k: {
        "success": False, "error": "Refusing to export: no DRC report"}
    assert run_workflow(Path("requirements.json"), _NAME, env.cfg) is False


def test_run_workflow_missing_drc_dict_returns_false(env):
    env.impl["drc"] = lambda *a, **k: {
        "success": False, "error": "No routed board found"}
    assert run_workflow(Path("requirements.json"), _NAME, env.cfg) is False
