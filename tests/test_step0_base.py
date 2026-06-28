"""Tests for orchestrator/steps/step_0_requirements.py and steps/base.py.

Hermetic, zero-LLM: step_0 never touches the LLM (we pass None and assert it
stays untouched). base helpers are pure subprocess/callback plumbing.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from orchestrator.config import OrchestratorConfig
from orchestrator.project import ProjectManager
from orchestrator.steps.base import StepBase, StepResult
from orchestrator.steps.step_0_requirements import RequirementsStep

TC01 = Path(__file__).resolve().parent / "test_cases" / "tc01_2l_minimal.json"


def _valid_requirements() -> dict:
    return json.loads(TC01.read_text())


def _make_step(tmp_path: Path, project_name: str = "tc01_2l_minimal") -> RequirementsStep:
    project = ProjectManager(project_name, tmp_path / "projects")
    config = OrchestratorConfig(base_dir=tmp_path)
    # llm=None: step_0 must never invoke it. prompt_builder unused here.
    return RequirementsStep(project=project, llm=None, prompt_builder=None, config=config)


# ---------------------------------------------------------------- step_0

def test_execute_dict_input_success(tmp_path):
    step = _make_step(tmp_path)
    res = step.execute(requirements=_valid_requirements())

    assert res.success is True
    out = Path(res.output_path)
    assert out.name == "REQUIREMENTS.md"
    pdir = step.project.project_dir
    assert (pdir / "REQUIREMENTS.md").exists()
    assert (pdir / "STATUS.json").exists()
    assert (pdir / "tc01_2l_minimal_requirements.json").exists()
    status = json.loads((pdir / "STATUS.json").read_text())
    assert status["steps"]["0"]["status"] == "COMPLETE"
    # calculations were injected by calculate_requirements
    assert "calculations" in json.loads(
        (pdir / "tc01_2l_minimal_requirements.json").read_text()
    )


def test_execute_path_input_success(tmp_path):
    step = _make_step(tmp_path)
    res = step.execute(requirements_path=TC01)
    assert res.success is True
    assert (step.project.project_dir / "REQUIREMENTS.md").exists()


def test_execute_no_input_returns_error(tmp_path):
    step = _make_step(tmp_path)
    res = step.execute()
    assert res.success is False
    assert res.error == "No requirements provided"


def test_execute_invalid_schema_returns_errors(tmp_path):
    step = _make_step(tmp_path)
    # Missing required keys (description/components/connections) + bad name pattern
    res = step.execute(requirements={"project_name": "Bad Name"})
    assert res.success is False
    assert "Requirements validation failed" in res.error
    assert "  - " in res.error
    # Nothing should have been written on the failure path
    assert not step.project.project_dir.exists()


def test_attach_files_copied(tmp_path):
    step = _make_step(tmp_path)
    att = tmp_path / "datasheet.pdf"
    att.write_bytes(b"%PDF-fake")
    missing = tmp_path / "nope.pdf"  # exercises the not-found branch

    res = step.execute(requirements=_valid_requirements(), attach_files=[att, missing])
    assert res.success is True
    assert (step.project.project_dir / "datasheet.pdf").read_bytes() == b"%PDF-fake"
    assert not (step.project.project_dir / "nope.pdf").exists()


def test_referenced_attachments_copied(tmp_path):
    """attachments[] in requirements + a requirements_path → copy relative file."""
    src_dir = tmp_path / "src"
    src_dir.mkdir()
    (src_dir / "ref.pdf").write_bytes(b"ref")
    # referenced-but-missing one hits the warning branch
    req = _valid_requirements()
    req["attachments"] = [
        {"filename": "ref.pdf", "type": "datasheet", "purpose": "p"},
        {"filename": "gone.pdf", "type": "datasheet", "purpose": "p"},
    ]
    req_path = src_dir / "req.json"
    req_path.write_text(json.dumps(req))

    step = _make_step(tmp_path)
    res = step.execute(requirements_path=req_path)
    assert res.success is True
    assert (step.project.project_dir / "ref.pdf").read_bytes() == b"ref"
    assert not (step.project.project_dir / "gone.pdf").exists()


def test_referenced_attachment_already_copied_is_skipped(tmp_path):
    """When --attach already placed the file, the referenced-copy loop skips it."""
    src_dir = tmp_path / "src"
    src_dir.mkdir()
    (src_dir / "ref.pdf").write_bytes(b"original")
    attached = tmp_path / "ref.pdf"
    attached.write_bytes(b"attached-version")

    req = _valid_requirements()
    req["attachments"] = [{"filename": "ref.pdf", "type": "datasheet", "purpose": "p"}]
    req_path = src_dir / "req.json"
    req_path.write_text(json.dumps(req))

    step = _make_step(tmp_path)
    res = step.execute(requirements_path=req_path, attach_files=[attached])
    assert res.success is True
    # the --attach version wins; the dest already existed so referenced-copy is a no-op
    assert (step.project.project_dir / "ref.pdf").read_bytes() == b"attached-version"


def test_step_name_and_number(tmp_path):
    step = _make_step(tmp_path)
    assert step.step_number == 0
    assert step.step_name == "Requirements"


# ---------------------------------------------------------------- base

def test_stepresult_defaults():
    r = StepResult(success=True)
    assert r.output_path is None and r.error is None and r.qa_report is None


def test_report_progress_no_callback(tmp_path):
    step = _make_step(tmp_path)
    assert step.progress_callback is None
    step._report_progress(phase="x")  # no-op, must not raise


def test_report_progress_invokes_callback(tmp_path):
    step = _make_step(tmp_path)
    seen = {}
    step.progress_callback = lambda fields: seen.update(fields)
    step._report_progress(phase="routing", pct=42)
    assert seen == {"phase": "routing", "pct": 42}


def test_report_progress_swallows_callback_error(tmp_path):
    step = _make_step(tmp_path)

    def boom(fields):
        raise RuntimeError("boom")

    step.progress_callback = boom
    step._report_progress(phase="x")  # must not propagate


def test_run_validator_cmd_parses_json(tmp_path):
    step = _make_step(tmp_path)
    payload = {"valid": True, "errors": [], "warnings": [], "summary": "ok"}
    cmd = [sys.executable, "-c", f"print({json.dumps(json.dumps(payload))})"]
    assert step._run_validator_cmd(cmd) == payload


def test_run_validator_cmd_crash_fallback(tmp_path):
    step = _make_step(tmp_path)
    # Non-JSON on stdout + non-zero exit → crash-fallback dict.
    cmd = [sys.executable, "-c", "import sys; sys.stderr.write('kaboom'); sys.exit(1)"]
    out = step._run_validator_cmd(cmd)
    assert out["valid"] is False
    assert out["summary"] == "Validator execution failed"
    assert any("kaboom" in e for e in out["errors"])
