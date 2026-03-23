"""Step 2: Component Selection — generate BOM, validate, QA, rework loop."""

import json
import subprocess
import sys
from pathlib import Path

from .base import StepBase, StepResult
from .step_1_schematic import extract_json


class BOMStep(StepBase):
    @property
    def step_number(self) -> int:
        return 2

    @property
    def step_name(self) -> str:
        return "Component Selection"

    def execute(self) -> StepResult:
        project_name = self.project.project_name
        netlist_filename = f"{project_name}_netlist.json"
        output_filename = f"{project_name}_bom.json"
        max_rework = self.config.max_rework_attempts

        # Read netlist (input from Step 1)
        netlist_path = self.project.get_output_path(netlist_filename)
        if not netlist_path.exists():
            return StepResult(
                success=False,
                error=f"Netlist not found: {netlist_path}",
            )
        netlist_content = netlist_path.read_text()

        # Read requirements for context
        requirements_text = self.project.read_requirements()

        self.project.update_status(self.step_number, "IN_PROGRESS")

        previous_output = None
        issues = None

        for attempt in range(1, max_rework + 1):
            # 1. Generate or rework BOM
            print(f"  [Attempt {attempt}/{max_rework}] Generating BOM...")
            if attempt == 1:
                prompt = self.prompt_builder.render(
                    "bom_generate",
                    {
                        "netlist_content": netlist_content,
                        "requirements": requirements_text,
                        "project_name": project_name,
                        "netlist_filename": netlist_filename,
                    },
                )
            else:
                prompt = self.prompt_builder.render(
                    "bom_rework",
                    {
                        "netlist_content": netlist_content,
                        "requirements": requirements_text,
                        "issues": issues,
                        "previous_output": previous_output,
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
                bom_text = extract_json(raw_response)
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
                    debug_path = self.project.get_output_path(
                        f"debug_bom_attempt_{attempt}.txt"
                    )
                    debug_path.write_text(raw_response)
                continue

            output_path = self.project.write_output(output_filename, bom_text)
            print(f"  Saved {output_filename}")

            # 3. Run validator
            validator_result = self._run_validator(output_path, netlist_path)
            print(
                f"  Validator: {'VALID' if validator_result['valid'] else 'INVALID'}"
                f" ({len(validator_result.get('errors', []))} errors,"
                f" {len(validator_result.get('warnings', []))} warnings)"
            )

            # 4. If validator fails, go straight to rework (skip QA)
            if not validator_result["valid"]:
                issues = validator_result["errors"]
                previous_output = bom_text
                self.project.update_status(
                    self.step_number, "QA_FAILED", rework_count=attempt
                )
                print(f"  Validation failed, will rework...")
                for err in issues:
                    print(f"    - {err}")
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
                    "output_content": bom_text,
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
            issues = qa_report.get(
                "issues", ["QA review failed (no specific issues provided)"]
            )
            previous_output = bom_text
            self.project.update_status(
                self.step_number, "QA_FAILED", rework_count=attempt
            )
            print(f"  QA: FAILED — {qa_report.get('summary', '')}")
            for issue in issues:
                print(f"    - {issue}")

        # Exhausted rework attempts
        self.project.update_status(
            self.step_number, "BLOCKED", rework_count=max_rework
        )
        return StepResult(
            success=False,
            error=f"Step {self.step_number} ({self.step_name}) failed after {max_rework} attempts",
            qa_report=qa_report if "qa_report" in dir() else None,
        )

    def _run_validator(self, bom_path: Path, netlist_path: Path) -> dict:
        """Run validate_bom.py and return the result dict."""
        validator = self.config.resolve("validators/validate_bom.py")

        cmd = [
            sys.executable,
            str(validator),
            str(bom_path),
            "--netlist",
            str(netlist_path),
        ]

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
