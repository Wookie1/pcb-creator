"""Tests for orchestrator/project.py, prompts/excerpts.py, prompts/builder.py."""

import json
from pathlib import Path

import pytest

from orchestrator.project import ProjectManager
from orchestrator.prompts.builder import PromptBuilder
from orchestrator.prompts.excerpts import load_standards

REPO_ROOT = Path(__file__).resolve().parent.parent


# --------------------------------------------------------------------------- #
# ProjectManager
# --------------------------------------------------------------------------- #

FULL_REQS = {
    "project_name": "demo_board",
    "description": "A demo LED blinker.",
    "power": {"voltage": "5V", "source": "USB"},
    "components": [
        {
            "ref": "R1",
            "type": "resistor",
            "value": "220ohm",
            "package": "0805",
            "purpose": "current limit",
            "specs": {"power": "0.25W", "tol": "1%"},
        },
        {"ref": "D1", "type": "led"},  # exercises the minimal-component branch
    ],
    "connections": [
        {"net_name": "VCC", "net_class": "power", "pins": ["R1.1", "U1.8"]},
        {"net_name": "GND"},  # exercises defaults
    ],
    "calculations": {
        "r_limit": {"formula": "(5-2)/0.02", "value": "150ohm", "power": "60mW"},
        "no_power": {"formula": "x", "value": "y"},  # no power key
    },
    "packages": "Mixed SMD",
    "board": {
        "width_mm": 50,
        "height_mm": 30,
        "corner_radius_mm": 2,
        "layers": 2,
        "outline_type": "rect",
    },
    "placement_hints": [
        {"ref": "J1", "x_mm": 1, "y_mm": 2, "rotation_deg": 90, "edge": "left", "near": "U1"},
        {"ref": "C1"},  # bare hint, no positional info
    ],
    "attachments": [
        {"filename": "outline.dxf", "type": "dxf", "purpose": "board edge", "used_by_steps": [3, 4]},
        {"filename": "photo.png", "type": "image", "purpose": "ref"},  # no used_by_steps
    ],
}


@pytest.fixture
def pm(tmp_path):
    return ProjectManager("demo_board", tmp_path)


def test_initialize_creates_dir_and_files(pm):
    pm.initialize(FULL_REQS)
    assert pm.project_dir.is_dir()
    assert (pm.project_dir / "REQUIREMENTS.md").exists()
    assert (pm.project_dir / "STATUS.json").exists()
    assert (pm.project_dir / "demo_board_requirements.json").exists()


def test_initial_status_contents(pm):
    pm.initialize(FULL_REQS)
    data = json.loads((pm.project_dir / "STATUS.json").read_text())
    assert data["project_name"] == "demo_board"
    assert data["current_step"] == 0
    assert data["current_status"] == "NOT_STARTED"
    assert data["steps"] == {}


def test_requirements_md_contains_designators_and_sections(pm):
    pm.initialize(FULL_REQS)
    md = pm.read_requirements()
    # designators / component refs
    assert "**R1**" in md
    assert "resistor" in md
    assert "220ohm" in md
    assert "(0805)" in md
    assert "current limit" in md
    assert "power=0.25W" in md
    # minimal component still rendered with type
    assert "**D1**: led" in md
    # section headers
    for header in ("# Requirements: demo_board", "## Circuit Description",
                   "## Power", "## Components", "## Connections",
                   "## Calculations", "## Packages", "## Board",
                   "## Placement Hints", "## Attachments"):
        assert header in md
    # power section
    assert "Voltage: 5V" in md
    assert "Source: USB" in md
    # connection defaults
    assert "**GND** (signal):" in md
    assert "**VCC** (power): R1.1, U1.8" in md
    # calculations with and without power
    assert "r_limit: (5-2)/0.02 = 150ohm" in md
    assert "Power: 60mW" in md
    # board
    assert "Width: 50mm" in md
    assert "Corner radius: 2mm" in md
    assert "Layers: 2" in md
    # placement hints, full and bare
    assert "**J1** at (1, 2)mm rotated 90° on left edge near U1" in md
    assert "- **C1**" in md
    # attachments
    assert "**outline.dxf** (dxf): board edge [steps: 3, 4]" in md
    assert "**photo.png** (image): ref" in md


def test_requirements_md_minimal_uses_project_name(tmp_path):
    """No project_name in dict -> falls back to manager's name; optional
    sections absent -> their branches are skipped."""
    pm = ProjectManager("fallback_proj", tmp_path)
    pm.initialize({})  # empty requirements
    md = pm.read_requirements()
    assert "# Requirements: fallback_proj" in md
    assert "## Power" not in md
    assert "## Components" not in md
    assert "## Board" not in md


def test_read_requirements_json_roundtrip(pm):
    pm.initialize(FULL_REQS)
    assert pm.read_requirements_json() == FULL_REQS


def test_update_status_creates_then_updates(pm):
    pm.initialize(FULL_REQS)
    pm.update_status(1, "IN_PROGRESS")
    data = json.loads((pm.project_dir / "STATUS.json").read_text())
    assert data["current_step"] == 1
    assert data["current_status"] == "IN_PROGRESS"
    assert data["steps"]["1"]["status"] == "IN_PROGRESS"
    assert "timestamp" in data["steps"]["1"]
    assert "rework_count" not in data["steps"]["1"]

    # second update on same step with rework + errors
    pm.update_status(
        1,
        "BLOCKED",
        rework_count=2,
        validator_errors=["bad net"],
        validator_warnings=["loose pin"],
    )
    data = json.loads((pm.project_dir / "STATUS.json").read_text())
    assert data["current_status"] == "BLOCKED"
    assert data["steps"]["1"]["status"] == "BLOCKED"
    assert data["steps"]["1"]["rework_count"] == 2
    assert data["steps"]["1"]["validator_errors"] == ["bad net"]
    assert data["steps"]["1"]["validator_warnings"] == ["loose pin"]


def test_update_status_without_existing_file(pm):
    """No initialize() -> update_status builds STATUS.json from scratch."""
    pm.project_dir.mkdir(parents=True)
    pm.update_status(2, "COMPLETE")
    data = json.loads((pm.project_dir / "STATUS.json").read_text())
    assert data["project_name"] == "demo_board"
    assert data["current_step"] == 2
    assert data["current_status"] == "COMPLETE"
    assert data["steps"]["2"]["status"] == "COMPLETE"


def test_write_output_and_get_output_path_roundtrip(pm):
    pm.project_dir.mkdir(parents=True)
    path = pm.write_output("demo_board_netlist.json", '{"x": 1}')
    assert path == pm.get_output_path("demo_board_netlist.json")
    assert path.read_text() == '{"x": 1}'
    assert path.exists()


def test_write_quality_creates_then_appends(pm):
    pm.project_dir.mkdir(parents=True)
    pm.write_quality({"step": 1, "passed": True, "issues": []})
    data = json.loads((pm.project_dir / "QUALITY.json").read_text())
    assert data["project_name"] == "demo_board"
    assert len(data["reviews"]) == 1
    assert data["reviews"][0]["passed"] is True
    assert "timestamp" in data["reviews"][0]

    pm.write_quality({"step": 2, "passed": False})
    data = json.loads((pm.project_dir / "QUALITY.json").read_text())
    assert len(data["reviews"]) == 2
    assert data["reviews"][1]["step"] == 2


# --------------------------------------------------------------------------- #
# excerpts.load_standards
# --------------------------------------------------------------------------- #

def test_load_standards_real_file():
    sections = load_standards(REPO_ROOT / "STANDARDS.md")
    # numbered sections 1..7 exist and are keyed by number
    for n in (str(i) for i in range(1, 8)):
        assert n in sections
    assert sections["1"].startswith("## 1. File Format Standards")
    assert "JSON format" in sections["1"]
    assert sections["4"].startswith("## 4. Validation Rules")
    assert "validate_netlist.py" in sections["4"]
    # body of last section is captured (trailing-body branch, no following header)
    assert "IPC-2221" in sections["7"]
    # preamble is not a section
    assert "0" not in sections


def test_load_standards_handles_trailing_header_without_body(tmp_path):
    """A header with no following content still produces a (header-only) entry,
    and a non-numbered ## header is ignored."""
    p = tmp_path / "S.md"
    p.write_text(
        "preamble\n\n"
        "## 1. First\n\nbody one\n\n"
        "## Notes\n\nignored, not numbered\n\n"
        "## 2. Last\n"  # header at EOF, empty body
    )
    sections = load_standards(p)
    assert set(sections) == {"1", "2"}
    assert "body one" in sections["1"]
    assert sections["2"] == "## 2. Last"


# --------------------------------------------------------------------------- #
# PromptBuilder
# --------------------------------------------------------------------------- #

@pytest.fixture
def builder():
    return PromptBuilder(REPO_ROOT)


def test_builder_loads_standards_and_engineering_rules(builder):
    assert "4" in builder.standards
    assert builder.engineering_rules  # non-empty
    assert builder.engineering_rules == builder.engineering_rules.strip()


def test_get_validation_rules_returns_section_4(builder):
    rules = builder.get_validation_rules()
    assert rules.startswith("## 4. Validation Rules")
    assert rules == builder.standards["4"]


def test_get_validation_rules_missing_section_default():
    """get_validation_rules falls back to '' when section 4 absent."""
    b = PromptBuilder.__new__(PromptBuilder)
    b.standards = {"1": "x"}
    assert b.get_validation_rules() == ""


def test_render_substitutes_context_and_engineering_rules(builder):
    out = builder.render(
        "qa_review",
        {
            "step_number": 1,
            "step_name": "Schematic/Netlist",
            "requirements": "REQ-BODY",
            "validation_rules": "VAL-BODY",
            "output_content": "OUT-BODY",
            "validator_result": "VALID",
        },
    )
    assert "Step 1: Schematic/Netlist" in out
    assert "REQ-BODY" in out
    assert "VAL-BODY" in out
    assert "OUT-BODY" in out
    # engineering_rules auto-injected and rendered
    assert builder.engineering_rules.split("\n", 1)[0] in out
    # the {% else %} branch (step != 3) is taken
    assert "Electrical correctness" in out


def test_render_step3_branch(builder):
    out = builder.render(
        "qa_review",
        {
            "step_number": 3,
            "step_name": "Board Layout",
            "requirements": "R",
            "validation_rules": "V",
            "output_content": "O",
            "validator_result": "PASSED",
        },
    )
    assert "Step 3 (Board Layout)" in out
    assert "All components placed" in out
