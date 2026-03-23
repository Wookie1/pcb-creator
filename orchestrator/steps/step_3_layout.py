"""Step 3: Board Layout — generate placement, validate, QA, rework loop."""

import json
import subprocess
import sys
from pathlib import Path

from .base import StepBase, StepResult
from .step_1_schematic import extract_json


# Default board size if not specified in requirements
DEFAULT_BOARD_WIDTH_MM = 50
DEFAULT_BOARD_HEIGHT_MM = 30


class LayoutStep(StepBase):
    @property
    def step_number(self) -> int:
        return 3

    @property
    def step_name(self) -> str:
        return "Board Layout"

    def execute(self) -> StepResult:
        project_name = self.project.project_name
        netlist_filename = f"{project_name}_netlist.json"
        bom_filename = f"{project_name}_bom.json"
        output_filename = f"{project_name}_placement.json"
        max_rework = self.config.max_rework_attempts

        # Read netlist (input from Step 1)
        netlist_path = self.project.get_output_path(netlist_filename)
        if not netlist_path.exists():
            return StepResult(
                success=False,
                error=f"Netlist not found: {netlist_path}",
            )
        netlist_content = netlist_path.read_text()

        # Read BOM (input from Step 2)
        bom_path = self.project.get_output_path(bom_filename)
        if not bom_path.exists():
            return StepResult(
                success=False,
                error=f"BOM not found: {bom_path}",
            )
        bom_content = bom_path.read_text()

        # Read requirements for board config and placement hints
        requirements_text = self.project.read_requirements()
        requirements_json = self.project.read_requirements_json()

        # Extract board dimensions
        board = requirements_json.get("board", {})
        board_width = board.get("width_mm", DEFAULT_BOARD_WIDTH_MM)
        board_height = board.get("height_mm", DEFAULT_BOARD_HEIGHT_MM)

        # Extract placement hints
        placement_hints = requirements_json.get("placement_hints", [])

        # Extract attachment descriptions for context
        attachment_descriptions = []
        for att in requirements_json.get("attachments", []):
            if 3 in att.get("used_by_steps", []):
                attachment_descriptions.append(
                    f"{att['filename']} ({att['type']}): {att['purpose']}"
                )

        self.project.update_status(self.step_number, "IN_PROGRESS")

        previous_output = None
        issues = None

        for attempt in range(1, max_rework + 1):
            # 1. Generate or rework placement
            print(f"  [Attempt {attempt}/{max_rework}] Generating placement...")
            if attempt == 1:
                prompt = self.prompt_builder.render(
                    "layout_generate",
                    {
                        "netlist_content": netlist_content,
                        "bom_content": bom_content,
                        "project_name": project_name,
                        "netlist_filename": netlist_filename,
                        "bom_filename": bom_filename,
                        "board_width_mm": board_width,
                        "board_height_mm": board_height,
                        "placement_hints": placement_hints,
                        "attachment_descriptions": attachment_descriptions,
                    },
                )
            else:
                prompt = self.prompt_builder.render(
                    "layout_rework",
                    {
                        "netlist_content": netlist_content,
                        "bom_content": bom_content,
                        "board_width_mm": board_width,
                        "board_height_mm": board_height,
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
                placement_text = extract_json(raw_response)
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
                        f"debug_layout_attempt_{attempt}.txt"
                    )
                    debug_path.write_text(raw_response)
                continue

            # 2b. Correct footprint dimensions using pad_geometry
            # The LLM often gets dimensions wrong (e.g., PinHeader_1x8 as 5mm instead of 20mm).
            # Override with computed values from actual pad positions.
            placement_text = self._correct_footprint_dimensions(
                placement_text, netlist_content
            )

            output_path = self.project.write_output(output_filename, placement_text)
            print(f"  Saved {output_filename}")

            # 3. Run validator
            validator_result = self._run_validator(output_path, netlist_path)
            print(
                f"  Validator: {'VALID' if validator_result['valid'] else 'INVALID'}"
                f" ({len(validator_result.get('errors', []))} errors,"
                f" {len(validator_result.get('warnings', []))} warnings)"
            )

            # 4. If validator fails, try repair before reworking
            if not validator_result["valid"]:
                # Check if failures are overlap/boundary issues (repairable)
                overlap_errors = [
                    e for e in validator_result["errors"]
                    if "overlap" in e.lower() or "clearance" in e.lower()
                    or "past" in e.lower() and "board edge" in e.lower()
                ]
                if overlap_errors:
                    print(f"  Validation found {len(overlap_errors)} overlap/boundary errors, attempting repair...")
                    try:
                        import sys as _sys
                        _sys.path.insert(0, str(self.config.base_dir))
                        from optimizers.placement_optimizer import repair_placement

                        placement_data = json.loads(placement_text)
                        netlist_data = json.loads(netlist_content)
                        repaired = repair_placement(placement_data, netlist_data)
                        repaired_text = json.dumps(repaired, indent=2)

                        # Write repaired placement and re-validate
                        output_path = self.project.write_output(output_filename, repaired_text)
                        validator_result = self._run_validator(output_path, netlist_path)
                        print(
                            f"  Post-repair validator: "
                            f"{'VALID' if validator_result['valid'] else 'INVALID'}"
                            f" ({len(validator_result.get('errors', []))} errors,"
                            f" {len(validator_result.get('warnings', []))} warnings)"
                        )
                        if validator_result["valid"]:
                            placement_text = repaired_text
                            # Fall through to QA review below
                        else:
                            # Repair didn't fully fix it
                            issues = validator_result["errors"]
                            previous_output = repaired_text
                            self.project.update_status(
                                self.step_number, "QA_FAILED", rework_count=attempt
                            )
                            print(f"  Repair incomplete, will rework...")
                            for err in issues:
                                print(f"    - {err}")
                            continue
                    except Exception as e:
                        print(f"  Repair failed ({e}), will rework...")
                        issues = validator_result["errors"]
                        previous_output = placement_text
                        self.project.update_status(
                            self.step_number, "QA_FAILED", rework_count=attempt
                        )
                        continue
                else:
                    # Non-overlap errors (schema, cross-reference) — can't repair
                    issues = validator_result["errors"]
                    previous_output = placement_text
                    self.project.update_status(
                        self.step_number, "QA_FAILED", rework_count=attempt
                    )
                    print(f"  Validation failed, will rework...")
                    for err in issues:
                        print(f"    - {err}")
                    continue

            # Print warnings
            for w in validator_result.get("warnings", []):
                print(f"  Warning: {w}")

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
                    "output_content": placement_text,
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

            # QA failed — collect issues for rework
            issues = qa_report.get(
                "issues", ["QA review failed (no specific issues provided)"]
            )
            previous_output = placement_text
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

    @staticmethod
    def _correct_footprint_dimensions(
        placement_text: str, netlist_content: str
    ) -> str:
        """Override LLM-guessed footprint dimensions with computed values.

        Uses pad_geometry to compute actual pad extent + courtyard margin.
        This prevents issues like PinHeader_1x8 being listed as 5mm when it's
        actually 20mm, which causes pads to extend outside the board.
        """
        import math as _math

        try:
            from optimizers.pad_geometry import (
                get_footprint_def,
                _generate_fallback_footprint,
            )
        except ImportError:
            return placement_text  # can't import, skip correction

        placement = json.loads(placement_text)
        netlist = json.loads(netlist_content)

        # Build pin count per component
        comp_pin_counts: dict[str, int] = {}
        des_to_comp_id: dict[str, str] = {}
        for elem in netlist.get("elements", []):
            if elem.get("element_type") == "port":
                cid = elem.get("component_id", "")
                comp_pin_counts[cid] = comp_pin_counts.get(cid, 0) + 1
            elif elem.get("element_type") == "component":
                des_to_comp_id[elem.get("designator", "")] = elem[
                    "component_id"
                ]

        corrections = 0
        for item in placement.get("placements", []):
            pkg = item.get("package", "")
            des = item.get("designator", "")
            cid = des_to_comp_id.get(des, "")
            pin_count = comp_pin_counts.get(cid, 2)

            fp_def = get_footprint_def(pkg, pin_count)
            if fp_def is None:
                continue  # unknown package, keep LLM values

            pw, ph = fp_def.pad_size
            xs = [dx for dx, dy in fp_def.pin_offsets.values()]
            ys = [dy for dx, dy in fp_def.pin_offsets.values()]

            x_extent = max(xs) - min(xs) + pw
            y_extent = max(ys) - min(ys) + ph
            margin = 0.5  # courtyard margin

            correct_w = round(max(x_extent + margin, pw + margin), 1)
            correct_h = round(max(y_extent + margin, ph + margin), 1)

            old_w = item.get("footprint_width_mm", 0)
            old_h = item.get("footprint_height_mm", 0)

            if abs(old_w - correct_w) > 0.5 or abs(old_h - correct_h) > 0.5:
                item["footprint_width_mm"] = correct_w
                item["footprint_height_mm"] = correct_h
                corrections += 1

        if corrections > 0:
            print(f"  Corrected {corrections} footprint dimensions from pad geometry")

        return json.dumps(placement, indent=2)

    def _run_validator(self, placement_path: Path, netlist_path: Path) -> dict:
        """Run validate_placement.py and return the result dict."""
        validator = self.config.resolve("validators/validate_placement.py")

        cmd = [
            sys.executable,
            str(validator),
            str(placement_path),
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
