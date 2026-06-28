"""Pure-logic coverage for the LLM-pipeline step modules (steps 1-3).

Only the deterministic helpers are exercised here. The execute()/_generate_chunked()
rework loops are LLM-bound and carry `# pragma: no cover` in the source — they need a
live model and mocking them would be mock theater. See test_json_extraction.py for the
JSON-extraction edge cases; this file fills the remaining pure helpers.
"""

import json
import shutil
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parent.parent
if str(_REPO / "validators") not in sys.path:
    sys.path.insert(0, str(_REPO / "validators"))

from orchestrator.config import OrchestratorConfig
from orchestrator.project import ProjectManager
from orchestrator.steps.step_1_schematic import (
    SchematicStep,
    _strip_markdown_fences,
    _auto_merge_shared_nets,
    _json_error_context,
    _normalize_netlist_structure,
    _sanitize_net_ids,
    extract_json,
)
from orchestrator.steps.step_2_bom import BOMStep
from orchestrator.steps.step_3_layout import (
    LayoutStep,
    DEFAULT_BOARD_WIDTH_MM,
    DEFAULT_BOARD_HEIGHT_MM,
)
from orchestrator.steps.step_1_schematic import _strip_reasoning

extract_array = SchematicStep._extract_json_array


# --------------------------------------------------------------------------- #
# fixtures
# --------------------------------------------------------------------------- #

def _make_step(cls, tmp_path, requirements_json: dict | None = None):
    """Build a step instance with a real ProjectManager. llm=None — only pure
    methods (those that never touch self.llm) are called."""
    cfg = OrchestratorConfig()
    cfg.base_dir = _REPO
    proj = ProjectManager("cov_proj", tmp_path)
    proj.project_dir.mkdir(parents=True, exist_ok=True)
    if requirements_json is not None:
        (proj.project_dir / f"{proj.project_name}_requirements.json").write_text(
            json.dumps(requirements_json)
        )
    return cls(project=proj, llm=None, prompt_builder=None, config=cfg)


# Minimal netlist: a 0805 resistor + an SOIC-8 IC. Both resolve in pad_geometry,
# so the footprint/board helpers do real geometry math.
_NETLIST = {
    "version": "1.0",
    "project_name": "cov_proj",
    "elements": [
        {"element_type": "component", "component_id": "comp_r1",
         "designator": "R1", "package": "0805"},
        {"element_type": "component", "component_id": "comp_u1",
         "designator": "U1", "package": "SOIC-8"},
        # R1: 2 ports
        {"element_type": "port", "port_id": "p_r1_1", "component_id": "comp_r1",
         "pin_number": "1", "name": "A", "electrical_type": "passive"},
        {"element_type": "port", "port_id": "p_r1_2", "component_id": "comp_r1",
         "pin_number": "2", "name": "B", "electrical_type": "passive"},
    ] + [
        {"element_type": "port", "port_id": f"p_u1_{i}", "component_id": "comp_u1",
         "pin_number": str(i), "name": f"P{i}", "electrical_type": "passive"}
        for i in range(1, 9)
    ],
}
_NETLIST_TEXT = json.dumps(_NETLIST)


# --------------------------------------------------------------------------- #
# step 1: _strip_markdown_fences
# --------------------------------------------------------------------------- #

class TestStripMarkdownFences:
    def test_fenced_json_block_stripped(self):
        assert _strip_markdown_fences('```json\n{"a":1}\n```') == '{"a":1}'

    def test_fence_without_lang(self):
        assert _strip_markdown_fences('```\n[1,2]\n```') == '[1,2]'

    def test_bare_fence_line_only(self):
        # No newline after the fence — nothing inside
        assert _strip_markdown_fences('```') == '```'

    def test_no_fence_passthrough(self):
        assert _strip_markdown_fences('{"x":1}') == '{"x":1}'

    def test_fence_with_reasoning_prefix(self):
        raw = '<think>plan</think>\n```json\n{"k":2}\n```'
        assert _strip_markdown_fences(raw) == '{"k":2}'


# --------------------------------------------------------------------------- #
# step 1: _auto_merge_shared_nets
# --------------------------------------------------------------------------- #

class TestAutoMergeSharedNets:
    def test_merge_nets_sharing_a_port(self):
        netlist = {"elements": [
            {"element_type": "net", "net_id": "net_a", "name": "A",
             "connected_port_ids": ["p1", "p2"]},
            {"element_type": "net", "net_id": "net_b", "name": "B",
             "connected_port_ids": ["p2", "p3"]},
        ]}
        fixed, warnings = _auto_merge_shared_nets(netlist)
        nets = [e for e in fixed["elements"] if e["element_type"] == "net"]
        assert len(nets) == 1
        assert set(nets[0]["connected_port_ids"]) == {"p1", "p2", "p3"}
        assert warnings and "Auto-merged" in warnings[0]

    def test_no_shared_port_no_merge(self):
        netlist = {"elements": [
            {"element_type": "net", "net_id": "net_a", "connected_port_ids": ["p1"]},
            {"element_type": "net", "net_id": "net_b", "connected_port_ids": ["p2"]},
        ]}
        fixed, warnings = _auto_merge_shared_nets(netlist)
        assert len([e for e in fixed["elements"] if e["element_type"] == "net"]) == 2
        assert warnings == []

    def test_merge_via_same_physical_pin_different_ports(self):
        # Two ports name the same component pin (R1.1); each is in a different net.
        netlist = {"elements": [
            {"element_type": "port", "port_id": "pa", "component_id": "R1",
             "pin_number": "1"},
            {"element_type": "port", "port_id": "pb", "component_id": "R1",
             "pin_number": "1"},
            {"element_type": "net", "net_id": "net_a", "connected_port_ids": ["pa"]},
            {"element_type": "net", "net_id": "net_b", "connected_port_ids": ["pb"]},
        ]}
        fixed, warnings = _auto_merge_shared_nets(netlist)
        assert len([e for e in fixed["elements"] if e["element_type"] == "net"]) == 1
        assert warnings

    def test_does_not_mutate_input(self):
        netlist = {"elements": [
            {"element_type": "net", "net_id": "net_a", "connected_port_ids": ["p1", "p2"]},
            {"element_type": "net", "net_id": "net_b", "connected_port_ids": ["p2"]},
        ]}
        before = json.dumps(netlist)
        _auto_merge_shared_nets(netlist)
        assert json.dumps(netlist) == before


# --------------------------------------------------------------------------- #
# step 1: _json_error_context + extract_json error paths
# --------------------------------------------------------------------------- #

class TestJsonErrorContext:
    def test_context_includes_position(self):
        bad = '{"a": 1, "b": }'
        try:
            json.loads(bad)
        except json.JSONDecodeError as e:
            msg = _json_error_context(bad, e)
        assert "JSON syntax error" in msg
        assert "ERROR" in msg

    def test_empty_raises(self):
        with pytest.raises(ValueError, match="empty"):
            extract_json("")

    def test_garbage_raises(self):
        with pytest.raises(ValueError, match="Could not extract"):
            extract_json("not json at all, just words")

    def test_long_broken_json_reports_position(self):
        # >200 chars with a genuine mid-stream error -> position-context branch
        body = ",".join(f'"k{i}":{i}' for i in range(60))
        raw = "{" + body + ', "bad": }'
        with pytest.raises(ValueError) as exc:
            extract_json(raw)
        assert "JSON error" in str(exc.value)

    def test_brace_repair_extra_closing(self):
        # double closing brace -> programmatic brace-repair branch
        out = extract_json('{"a": {"b": 1}}}')
        assert json.loads(out) == {"a": {"b": 1}}

    def test_trailing_comma_repair(self):
        out = extract_json('{"a": 1, "b": 2,}')
        assert json.loads(out) == {"a": 1, "b": 2}

    def test_single_quote_repair(self):
        out = extract_json("{'a': 1}")
        assert json.loads(out) == {"a": 1}

    def test_first_to_last_brace_extraction(self):
        out = extract_json('prefix junk {"a": 1} trailing junk')
        assert json.loads(out) == {"a": 1}


# --------------------------------------------------------------------------- #
# step 1: _normalize_netlist_structure
# --------------------------------------------------------------------------- #

class TestNormalizeNetlistStructure:
    def test_merge_sibling_arrays_into_elements(self):
        text = json.dumps({
            "elements": [{"element_type": "component", "component_id": "c1"}],
            "ports": [{"element_type": "port", "port_id": "p1"}],
            "nets": [{"element_type": "net", "net_id": "net_a"}],
        })
        out = json.loads(_normalize_netlist_structure(text))
        assert "ports" not in out and "nets" not in out
        assert len(out["elements"]) == 3

    def test_build_elements_from_separate_arrays(self):
        text = json.dumps({
            "components": [{"component_id": "c1"}],
            "ports": [{"port_id": "p1"}],
            "nets": [{"net_id": "net_a"}],
        })
        out = json.loads(_normalize_netlist_structure(text))
        assert len(out["elements"]) == 3
        assert "components" not in out

    def test_already_flat_unchanged(self):
        text = json.dumps({"elements": [{"component_id": "c1"}]})
        assert _normalize_netlist_structure(text) == text

    def test_invalid_json_passthrough(self):
        assert _normalize_netlist_structure("not json") == "not json"

    def test_non_dict_passthrough(self):
        assert _normalize_netlist_structure("[1,2,3]") == "[1,2,3]"


# --------------------------------------------------------------------------- #
# step 1: _sanitize_net_ids
# --------------------------------------------------------------------------- #

class TestSanitizeNetIds:
    def test_illegal_chars_replaced(self):
        text = json.dumps({"elements": [
            {"element_type": "net", "net_id": "net_usb_d+"},
        ]})
        out = json.loads(_sanitize_net_ids(text))
        nid = out["elements"][0]["net_id"]
        assert nid == "net_usb_d"  # trailing illegal stripped

    def test_leading_digit_prefixed(self):
        text = json.dumps({"elements": [
            {"element_type": "net", "net_id": "net_3v3"},
        ]})
        out = json.loads(_sanitize_net_ids(text))
        # body "3v3" -> "n3v3"
        assert out["elements"][0]["net_id"] == "net_n3v3"

    def test_collision_gets_suffix(self):
        text = json.dumps({"elements": [
            {"element_type": "net", "net_id": "net_d+"},
            {"element_type": "net", "net_id": "net_d-"},
        ]})
        out = json.loads(_sanitize_net_ids(text))
        ids = [e["net_id"] for e in out["elements"]]
        assert ids[0] == "net_d"
        assert ids[1] == "net_d_2"  # collision resolved

    def test_valid_ids_unchanged(self):
        text = json.dumps({"elements": [
            {"element_type": "net", "net_id": "net_gnd"},
        ]})
        assert _sanitize_net_ids(text) == text

    def test_invalid_json_passthrough(self):
        assert _sanitize_net_ids("nope") == "nope"


# --------------------------------------------------------------------------- #
# step 1: estimators, pinout-table formatters, _run_validator argv
# --------------------------------------------------------------------------- #

class TestStep1Helpers:
    def test_estimate_component_count_from_requirements(self, tmp_path):
        req = {"components": [{"ref": "R1"}, {"ref": "C1"}, {"ref": "U1"}]}
        step = _make_step(SchematicStep, tmp_path, req)
        assert step._estimate_component_count() == 3

    def test_estimate_component_count_fallback(self, tmp_path):
        step = _make_step(SchematicStep, tmp_path)  # no requirements file
        assert step._estimate_component_count() == 10

    def test_estimate_component_count_empty_list_fallback(self, tmp_path):
        step = _make_step(SchematicStep, tmp_path, {"components": []})
        assert step._estimate_component_count() == 10

    def test_estimate_expected_output_length(self, tmp_path):
        req = {"components": [{"ref": f"R{i}"} for i in range(20)]}
        step = _make_step(SchematicStep, tmp_path, req)
        # 20 * 600 = 12000 > 5000 floor
        assert step._estimate_expected_output_length() == 12000

    def test_estimate_expected_output_length_floor(self, tmp_path):
        step = _make_step(SchematicStep, tmp_path, {"components": [{"ref": "R1"}]})
        assert step._estimate_expected_output_length() == 5000

    def test_run_validator_argv(self, tmp_path):
        # Drive argv construction; subprocess actually runs the real validator.
        step = _make_step(SchematicStep, tmp_path)
        netlist_path = step.project.write_output("cov_proj_netlist.json", _NETLIST_TEXT)
        # also write requirements json so the --requirements branch is taken
        (step.project.project_dir / "cov_proj_requirements.json").write_text("{}")
        result = step._run_validator(netlist_path)
        assert isinstance(result, dict)
        assert "valid" in result

    def test_run_validator_argv_no_requirements(self, tmp_path):
        step = _make_step(SchematicStep, tmp_path)
        netlist_path = step.project.write_output("cov_proj_netlist.json", _NETLIST_TEXT)
        result = step._run_validator(netlist_path)
        assert isinstance(result, dict)

    def test_format_pinout_table(self):
        class _Info:
            all_names = ["VCC", "VDD"]
            inferred_electrical_type = "power_in"
        pinouts = {"U1": {1: _Info()}}
        out = SchematicStep._format_pinout_table(pinouts)
        assert "**U1**" in out
        assert "VCC/VDD" in out
        assert "power_in" in out

    def test_format_pin_count_table(self):
        req = {"components": [
            {"ref": "U1", "package": "DIP8", "specs": {}},
            {"ref": "R1", "package": "0805", "specs": {}},
        ]}
        out = SchematicStep._format_pin_count_table(req)
        assert "U1" in out and "DIP8" in out
        assert "| Ref |" in out

    def test_format_pin_count_table_empty(self):
        # No package resolves a count -> empty string
        assert SchematicStep._format_pin_count_table({"components": []}) == ""


# --------------------------------------------------------------------------- #
# step 2: _run_validator argv
# --------------------------------------------------------------------------- #

class TestStep2Validator:
    def test_run_validator_argv(self, tmp_path):
        step = _make_step(BOMStep, tmp_path)
        netlist_path = step.project.write_output("cov_proj_netlist.json", _NETLIST_TEXT)
        bom_path = step.project.write_output(
            "cov_proj_bom.json", json.dumps({"components": []})
        )
        result = step._run_validator(bom_path, netlist_path)
        assert isinstance(result, dict)
        assert "valid" in result


# --------------------------------------------------------------------------- #
# step 3: _correct_footprint_dimensions
# --------------------------------------------------------------------------- #

class TestCorrectFootprintDimensions:
    def test_dimensions_corrected_from_pad_geometry(self):
        # LLM guessed wildly wrong sizes; helper overrides with computed values.
        placement = {"placements": [
            {"designator": "R1", "package": "0805",
             "footprint_width_mm": 99.0, "footprint_height_mm": 99.0},
            {"designator": "U1", "package": "SOIC-8",
             "footprint_width_mm": 1.0, "footprint_height_mm": 1.0},
        ]}
        out = json.loads(
            LayoutStep._correct_footprint_dimensions(json.dumps(placement), _NETLIST_TEXT)
        )
        r1 = next(p for p in out["placements"] if p["designator"] == "R1")
        u1 = next(p for p in out["placements"] if p["designator"] == "U1")
        # Corrected to real, much-smaller-than-99 footprint
        assert r1["footprint_width_mm"] < 10
        assert u1["footprint_width_mm"] != 1.0

    def test_unknown_package_kept(self):
        placement = {"placements": [
            {"designator": "X1", "package": "TOTALLY_UNKNOWN_PKG",
             "footprint_width_mm": 7.0, "footprint_height_mm": 7.0},
        ]}
        out = json.loads(
            LayoutStep._correct_footprint_dimensions(json.dumps(placement), _NETLIST_TEXT)
        )
        assert out["placements"][0]["footprint_width_mm"] == 7.0

    def test_close_enough_not_changed(self):
        # First compute the correct size, then feed it back -> no correction
        placement = {"placements": [
            {"designator": "R1", "package": "0805",
             "footprint_width_mm": 99.0, "footprint_height_mm": 99.0},
        ]}
        corrected = json.loads(
            LayoutStep._correct_footprint_dimensions(json.dumps(placement), _NETLIST_TEXT)
        )
        w = corrected["placements"][0]["footprint_width_mm"]
        h = corrected["placements"][0]["footprint_height_mm"]
        # Feed corrected values back: should be within tolerance -> unchanged
        again = json.loads(LayoutStep._correct_footprint_dimensions(
            json.dumps({"placements": [
                {"designator": "R1", "package": "0805",
                 "footprint_width_mm": w, "footprint_height_mm": h}]}),
            _NETLIST_TEXT))
        assert again["placements"][0]["footprint_width_mm"] == w


# --------------------------------------------------------------------------- #
# step 3: _ensure_adequate_board_size
# --------------------------------------------------------------------------- #

class TestEnsureAdequateBoardSize:
    def test_tiny_default_board_grows(self):
        # 1x1mm board cannot hold the components -> auto-grown
        w, h = LayoutStep._ensure_adequate_board_size(
            1.0, 1.0, _NETLIST_TEXT, user_specified=False
        )
        assert w > 1.0 and h > 1.0

    def test_large_board_unchanged(self):
        w, h = LayoutStep._ensure_adequate_board_size(
            500.0, 500.0, _NETLIST_TEXT, user_specified=False
        )
        assert (w, h) == (500.0, 500.0)

    def test_user_specified_tight_board_respected(self):
        # Too small but user-specified -> warned, not grown
        w, h = LayoutStep._ensure_adequate_board_size(
            1.0, 1.0, _NETLIST_TEXT, user_specified=True
        )
        assert (w, h) == (1.0, 1.0)

    def test_unknown_package_fallback_area(self):
        netlist = json.dumps({"elements": [
            {"element_type": "component", "component_id": "c1",
             "designator": "X1", "package": "WEIRD_PKG"},
            {"element_type": "port", "port_id": "p1", "component_id": "c1",
             "pin_number": "1"},
        ]})
        # Just needs to run the unknown-package (w=h=3.0) branch without error
        w, h = LayoutStep._ensure_adequate_board_size(1.0, 1.0, netlist, False)
        assert w >= 1.0 and h >= 1.0


# --------------------------------------------------------------------------- #
# step 3: _run_validator argv + _parse_dxf_outline + constants
# --------------------------------------------------------------------------- #

class TestStep3MiscHelpers:
    def test_default_constants(self):
        assert DEFAULT_BOARD_WIDTH_MM == 50
        assert DEFAULT_BOARD_HEIGHT_MM == 30

    def test_run_validator_argv(self, tmp_path):
        step = _make_step(LayoutStep, tmp_path)
        netlist_path = step.project.write_output("cov_proj_netlist.json", _NETLIST_TEXT)
        placement_path = step.project.write_output(
            "cov_proj_placement.json", json.dumps({"placements": []})
        )
        result = step._run_validator(placement_path, netlist_path)
        assert isinstance(result, dict)
        assert "valid" in result

    def test_parse_dxf_no_attachment(self, tmp_path):
        step = _make_step(LayoutStep, tmp_path)
        verts, w, h = step._parse_dxf_outline({"attachments": []}, 40.0, 20.0)
        assert verts is None and (w, h) == (40.0, 20.0)

    def test_parse_dxf_missing_file(self, tmp_path):
        step = _make_step(LayoutStep, tmp_path)
        req = {"attachments": [
            {"type": "board_outline", "filename": "ghost.dxf"},
        ]}
        verts, w, h = step._parse_dxf_outline(req, 40.0, 20.0)
        assert verts is None and (w, h) == (40.0, 20.0)

    def test_parse_dxf_real_file(self, tmp_path):
        step = _make_step(LayoutStep, tmp_path)
        # Copy the real arduino outline into the project dir
        src = _REPO / "tests" / "arduino_uno_outline.dxf"
        shutil.copy(src, step.project.project_dir / "outline.dxf")
        req = {"attachments": [
            {"type": "board_outline", "filename": "outline.dxf"},
        ]}
        verts, w, h = step._parse_dxf_outline(req, 40.0, 20.0)
        assert verts is not None and len(verts) >= 3
        assert w > 0 and h > 0

    def test_parse_dxf_malformed_file(self, tmp_path):
        # Garbage .dxf -> parse_board_outline raises -> except Exception branch
        step = _make_step(LayoutStep, tmp_path)
        (step.project.project_dir / "junk.dxf").write_text("this is not a DXF")
        req = {"attachments": [
            {"type": "board_outline", "filename": "junk.dxf"},
        ]}
        verts, w, h = step._parse_dxf_outline(req, 40.0, 20.0)
        assert verts is None and (w, h) == (40.0, 20.0)


# --------------------------------------------------------------------------- #
# step properties (abstract-impl getters)
# --------------------------------------------------------------------------- #

class TestStepProperties:
    def test_step1_props(self, tmp_path):
        step = _make_step(SchematicStep, tmp_path)
        assert step.step_number == 1
        assert step.step_name == "Schematic/Netlist"

    def test_step2_props(self, tmp_path):
        step = _make_step(BOMStep, tmp_path)
        assert step.step_number == 2
        assert step.step_name == "Component Selection"

    def test_step3_props(self, tmp_path):
        step = _make_step(LayoutStep, tmp_path)
        assert step.step_number == 3
        assert step.step_name == "Board Layout"


# --------------------------------------------------------------------------- #
# step 1: remaining extract_json / repair / sanitize / array branches
# --------------------------------------------------------------------------- #

class TestStripReasoningEmptyTail:
    def test_close_with_empty_tail_falls_back_to_body(self):
        # Tail after </think> is whitespace-only -> use pre-close body, strip <think>
        raw = "<think>[{\"a\":1}]</think>   "
        assert _strip_reasoning(raw) == '[{"a":1}]'


class TestExtractJsonRepairBranches:
    def test_invalid_fence_then_valid_brace_extraction(self):
        # Fenced block is invalid JSON (updates last_err); the first{..last}
        # span then yields a parseable object.
        raw = '```json\nnot valid\n```\n{"a": 1}'
        out = extract_json(raw)
        assert json.loads(out) == {"a": 1}

    def test_brace_repair_break_when_not_brace(self):
        # closes > opens but the offending char isn't '}' -> repair loop breaks,
        # falls through to other repairs / final error
        raw = '{"a": 1]}}'  # mismatched bracket, extra braces
        with pytest.raises(ValueError):
            extract_json(raw)

    def test_unrepairable_returns_error(self):
        raw = '{"a": [1, 2, 3'  # truncated, no closing
        with pytest.raises(ValueError):
            extract_json(raw)

    def test_fence_valid_after_prefix(self):
        # Direct parse fails on the prose prefix; fenced block is valid JSON.
        out = extract_json('Here is the answer:\n```json\n{"ok": 1}\n```')
        assert json.loads(out) == {"ok": 1}

    def test_brace_in_string_naive_count_overcounts(self):
        # A '}' inside a string makes the naive brace count report 2 extra, but
        # only one real removal is needed -> brace-repair returns mid-loop.
        out = extract_json('{"a":"}"}}')
        assert json.loads(out) == {"a": "}"}

    def test_double_comma_repair_fails_gracefully(self):
        # Trailing-comma regex runs but the result is still invalid -> pass branch.
        # Also drives the first{..last} candidate-invalid (last_err update) path.
        with pytest.raises(ValueError):
            extract_json('{"a": 1,, }')

    def test_brace_substring_invalid_updates_last_err(self):
        # Prefix makes direct parse fail at char 0; the first{..last} substring
        # then parses deeper before erroring, so last_err advances (line 203).
        with pytest.raises(ValueError):
            extract_json('garbage prefix here { "a": 1, "b": 2, "c": } trailing')

    def test_invalid_fence_updates_last_err(self):
        # A fenced block that's invalid JSON deep in -> the fence except branch
        # updates last_err (e.pos progression), then everything fails to parse.
        raw = '```json\n{"a": 1, "b": [1, 2, 3, "unterminated\n```'
        with pytest.raises(ValueError):
            extract_json(raw)


class TestSanitizeNetIdsRemap:
    def test_repeated_old_id_uses_existing_map(self):
        # Same illegal net_id appears twice -> second hits the id_map cache branch
        text = json.dumps({"elements": [
            {"element_type": "net", "net_id": "net_d+"},
            {"element_type": "net", "net_id": "net_d+"},
        ]})
        out = json.loads(_sanitize_net_ids(text))
        ids = [e["net_id"] for e in out["elements"]]
        assert ids[0] == ids[1] == "net_d"

    def test_empty_net_id_skipped(self):
        text = json.dumps({"elements": [
            {"element_type": "net", "net_id": ""},
            {"element_type": "net", "net_id": "net_gnd"},
        ]})
        # nothing illegal -> unchanged
        assert _sanitize_net_ids(text) == text

    def test_non_net_element_skipped(self):
        # A component element carrying a stray net_id is left untouched; only
        # element_type == "net" is sanitized.
        text = json.dumps({"elements": [
            {"element_type": "component", "net_id": "bad+id"},
            {"element_type": "net", "net_id": "net_sig+"},
        ]})
        out = json.loads(_sanitize_net_ids(text))
        assert out["elements"][0]["net_id"] == "bad+id"  # untouched
        assert out["elements"][1]["net_id"] == "net_sig"  # sanitized


class TestExtractJsonArrayBranches:
    def test_direct_array(self):
        assert extract_array('[1, 2, 3]') == '[1, 2, 3]'

    def test_fenced_array(self):
        assert extract_array('```json\n[1,2]\n```') == '[1,2]'

    def test_brace_first_last(self):
        out = extract_array('junk [1, 2] tail')
        assert json.loads(out) == [1, 2]

    def test_invalid_fence_then_brace(self):
        # fence content not a list -> falls to first[..last]
        out = extract_array('```\n{"x":1}\n```\n[1, 2]')
        assert json.loads(out) == [1, 2]

    def test_fence_unparseable_then_brace(self):
        # Fenced block is syntactically invalid JSON (except/pass branch),
        # then the bracket-span yields the list.
        out = extract_array('```\nnot a list at all\n```\n[1, 2]')
        assert json.loads(out) == [1, 2]

    def test_trailing_comma_repair(self):
        out = extract_array('[1, 2, 3,]')
        assert json.loads(out) == [1, 2, 3]

    def test_truncated_array_salvaged_at_last_object(self):
        # Degenerate tail cut mid-object -> close at last complete '},'
        raw = '[{"a": 1}, {"b": 2}, {"c": '
        out = extract_array(raw)
        parsed = json.loads(out)
        assert parsed == [{"a": 1}, {"b": 2}]

    def test_truncated_array_salvaged_ends_with_brace(self):
        # Single complete object, no trailing '},' and missing closing ] ->
        # exercises the rstrip().endswith('}') salvage branch.
        raw = '[{"a": 1}'
        out = extract_array(raw)
        assert json.loads(out) == [{"a": 1}]

    def test_unrecoverable_array_raises(self):
        with pytest.raises(ValueError, match="Could not extract JSON array"):
            extract_array("no array characters here")


class TestAutoMergeSinglePortPin:
    def test_pin_with_single_port_skipped(self):
        # A physical pin referenced by exactly one port -> len(port_ids)<=1 continue
        netlist = {"elements": [
            {"element_type": "port", "port_id": "pa", "component_id": "R1",
             "pin_number": "1"},
            {"element_type": "net", "net_id": "net_a", "connected_port_ids": ["pa"]},
        ]}
        fixed, warnings = _auto_merge_shared_nets(netlist)
        assert warnings == []
        assert len([e for e in fixed["elements"] if e["element_type"] == "net"]) == 1
