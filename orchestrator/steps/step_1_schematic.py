"""Step 1: Schematic/Netlist — generate, validate, QA, rework loop."""

import json
import re
import subprocess
import sys
from pathlib import Path

from .base import StepBase, StepResult

# validators/ is a sibling directory — add to path for pinout import
_validators_dir = str(Path(__file__).resolve().parent.parent.parent / "validators")
if _validators_dir not in sys.path:
    sys.path.insert(0, _validators_dir)

from pinout import build_pinout_from_requirements


def extract_json(raw: str) -> str:
    """Extract JSON from LLM response, handling markdown fences and continuations."""
    if not raw:
        raise ValueError("LLM returned empty/null response")
    raw = raw.strip()

    # Try direct parse
    try:
        json.loads(raw)
        return raw
    except json.JSONDecodeError:
        pass

    # Try stripping markdown fences (greedy — get the largest match)
    fence_pattern = re.compile(r"```(?:json)?\s*\n?(.*?)\n?\s*```", re.DOTALL)
    for match in fence_pattern.finditer(raw):
        candidate = match.group(1).strip()
        try:
            json.loads(candidate)
            return candidate
        except json.JSONDecodeError:
            pass

    # Try finding first { to last } — handles continuations that may have
    # extra text between chunks
    first_brace = raw.find("{")
    last_brace = raw.rfind("}")
    if first_brace != -1 and last_brace > first_brace:
        candidate = raw[first_brace : last_brace + 1]
        try:
            json.loads(candidate)
            return candidate
        except json.JSONDecodeError:
            pass

    raise ValueError(f"Could not extract valid JSON from LLM response:\n{raw[:200]}...")


class SchematicStep(StepBase):
    @property
    def step_number(self) -> int:
        return 1

    @property
    def step_name(self) -> str:
        return "Schematic/Netlist"

    def execute(self) -> StepResult:
        requirements_text = self.project.read_requirements()
        project_name = self.project.project_name
        output_filename = f"{project_name}_netlist.json"
        max_rework = self.config.max_rework_attempts

        # Parse IC pinouts from requirements for structured prompt injection
        pinout_table = ""
        try:
            req_json = self.project.read_requirements_json()
            pinouts = build_pinout_from_requirements(req_json)
            if pinouts:
                pinout_table = self._format_pinout_table(pinouts)
        except Exception:
            pass  # No pinout data available — not fatal

        self.project.update_status(self.step_number, "IN_PROGRESS")

        previous_output = None
        issues = None

        for attempt in range(1, max_rework + 1):
            # 1. Generate or rework netlist
            print(f"  [Attempt {attempt}/{max_rework}] Generating netlist...")
            if attempt == 1:
                prompt = self.prompt_builder.render(
                    "schematic_generate",
                    {
                        "requirements": requirements_text,
                        "project_name": project_name,
                        "pinout_table": pinout_table,
                    },
                )
            else:
                prompt = self.prompt_builder.render(
                    "schematic_rework",
                    {
                        "requirements": requirements_text,
                        "project_name": project_name,
                        "issues": issues,
                        "previous_output": previous_output,
                        "pinout_table": pinout_table,
                    },
                )

            try:
                raw_response = self.llm.generate(
                    system_prompt="",
                    user_prompt=prompt,
                    max_tokens=self.config.max_tokens,
                    temperature=self.config.temperature,
                )
            except Exception as e:
                return StepResult(
                    success=False,
                    error=f"LLM call failed: {e}",
                )

            # 2. Extract and save JSON
            try:
                netlist_text = extract_json(raw_response)
            except ValueError as e:
                issues = [str(e)]
                previous_output = raw_response
                self.project.update_status(
                    self.step_number, "REWORK_IN_PROGRESS", rework_count=attempt
                )
                print(f"  [Attempt {attempt}] Failed to extract JSON from response")
                if raw_response:
                    print(f"    Response length: {len(raw_response)} chars")
                    print(f"    First 200 chars: {raw_response[:200]}")
                    print(f"    Last 200 chars: {raw_response[-200:]}")
                    # Debug: write full response to file
                    debug_path = self.project.get_output_path(f"debug_attempt_{attempt}.txt")
                    debug_path.write_text(raw_response)
                continue

            output_path = self.project.write_output(output_filename, netlist_text)
            print(f"  Saved {output_filename}")

            # 3. Run validator
            validator_result = self._run_validator(output_path)
            print(
                f"  Validator: {'VALID' if validator_result['valid'] else 'INVALID'}"
                f" ({len(validator_result.get('errors', []))} errors,"
                f" {len(validator_result.get('warnings', []))} warnings)"
            )

            # 4. If validator fails, go straight to rework (skip QA)
            if not validator_result["valid"]:
                issues = validator_result["errors"]
                previous_output = netlist_text
                self.project.update_status(
                    self.step_number, "QA_FAILED", rework_count=attempt
                )
                print(f"  Validation failed, will rework...")
                continue

            # 5. LLM QA review
            print(f"  Running QA review...")
            self.project.update_status(self.step_number, "AWAITING_QA")

            qa_prompt = self.prompt_builder.render(
                "qa_review",
                {
                    "step_number": self.step_number,
                    "step_name": self.step_name,
                    "requirements": requirements_text,
                    "validation_rules": self.prompt_builder.get_validation_rules(),
                    "output_content": netlist_text,
                    "validator_result": json.dumps(validator_result, indent=2),
                },
            )

            try:
                qa_raw = self.llm.generate(
                    system_prompt="",
                    user_prompt=qa_prompt,
                    max_tokens=4096,
                    temperature=self.config.temperature,
                )
                qa_report = json.loads(extract_json(qa_raw))
            except Exception as e:
                # If QA response parsing fails, treat validator pass as sufficient
                print(f"  QA response parsing failed ({e}), accepting validator pass")
                qa_report = {
                    "step": self.step_number,
                    "step_name": self.step_name,
                    "passed": True,
                    "issues": [],
                    "summary": "Validator passed. QA response could not be parsed.",
                }

            # 6. Handle QA result
            # If Python validator passed cleanly (0 errors), QA failures are
            # treated as warnings — the validator is the authoritative gate.
            validator_clean = not validator_result.get("errors")
            if not qa_report.get("passed", False) and validator_clean:
                print(f"  QA: OVERRIDDEN — validator passed cleanly, treating QA issues as warnings")
                qa_report["passed"] = True
                qa_report["summary"] = (
                    f"Validator passed. QA raised concerns (overridden): "
                    f"{qa_report.get('summary', '')}"
                )

            if qa_report.get("passed", False):
                self.project.write_quality(qa_report)
                self.project.update_status(self.step_number, "COMPLETE")
                print(f"  QA: PASSED — {qa_report.get('summary', '')}")
                return StepResult(
                    success=True,
                    output_path=str(output_path),
                    qa_report=qa_report,
                )

            # QA failed AND validator had errors — collect issues for rework
            issues = qa_report.get("issues", ["QA review failed (no specific issues provided)"])
            previous_output = netlist_text
            self.project.update_status(
                self.step_number, "QA_FAILED", rework_count=attempt
            )
            print(f"  QA: FAILED — {qa_report.get('summary', '')}")
            for issue in issues:
                print(f"    - {issue}")

        # Exhausted rework attempts
        self.project.update_status(self.step_number, "BLOCKED", rework_count=max_rework)
        return StepResult(
            success=False,
            error=f"Step {self.step_number} ({self.step_name}) failed after {max_rework} attempts",
            qa_report=qa_report if "qa_report" in dir() else None,
        )

    def _run_validator(self, netlist_path: Path) -> dict:
        """Run validate_netlist.py and return the result dict."""
        validator = self.config.resolve(self.config.validator_path)

        cmd = [sys.executable, str(validator), str(netlist_path)]

        # Pass requirements JSON for power-aware DRC checks
        requirements_path = self.project.get_output_path(
            f"{self.project.project_name}_requirements.json"
        )
        if requirements_path.exists():
            cmd.extend(["--requirements", str(requirements_path)])

        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            cwd=str(self.config.base_dir),
        )

        try:
            return json.loads(result.stdout)
        except json.JSONDecodeError:
            return {
                "valid": False,
                "errors": [f"Validator crashed: {result.stderr or result.stdout}"],
                "warnings": [],
                "summary": "Validator execution failed",
            }

    @staticmethod
    def _format_pinout_table(pinouts: dict) -> str:
        """Format parsed pinouts as a markdown table for LLM prompt injection."""
        lines: list[str] = []
        for ref, pin_map in sorted(pinouts.items()):
            lines.append(f"**{ref}** ({len(pin_map)} pins):")
            lines.append("| Pin | Name | Type |")
            lines.append("|-----|------|------|")
            for pin_num in sorted(pin_map.keys()):
                info = pin_map[pin_num]
                name = "/".join(info.all_names)
                lines.append(f"| {pin_num} | {name} | {info.inferred_electrical_type} |")
            lines.append("")
        return "\n".join(lines)
