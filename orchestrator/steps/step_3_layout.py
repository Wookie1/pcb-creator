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

        # Auto-size: ensure board is large enough for all components
        board_width, board_height = self._ensure_adequate_board_size(
            board_width, board_height, netlist_content,
            user_specified="width_mm" in board or "height_mm" in board,
        )

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
        last_validator_result: dict = {}

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
            last_validator_result = validator_result
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
                        last_validator_result = validator_result
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

        # Last resort fallbacks for persistent overlap failures
        if last_validator_result.get("errors"):
            overlap_errors = [
                e for e in last_validator_result["errors"]
                if "overlap" in e.lower() or "clearance" in e.lower()
                or "past" in e.lower() and "board edge" in e.lower()
            ]
            if overlap_errors:
                import sys as _sys
                _sys.path.insert(0, str(self.config.base_dir))
                from optimizers.placement_optimizer import repair_placement

                netlist_data = json.loads(netlist_content)

                # Fallback 1: Grow board 20% and re-run repair on LLM placement
                if previous_output:
                    try:
                        print(f"  Fallback 1: growing board by 20% and re-running repair...")
                        placement_data = json.loads(previous_output)
                        if "board" in placement_data:
                            placement_data["board"]["width_mm"] = round(
                                placement_data["board"]["width_mm"] * 1.2, 1
                            )
                            placement_data["board"]["height_mm"] = round(
                                placement_data["board"]["height_mm"] * 1.2, 1
                            )
                        repaired = repair_placement(placement_data, netlist_data)
                        repaired_text = json.dumps(repaired, indent=2)
                        output_path = self.project.write_output(output_filename, repaired_text)
                        validator_result = self._run_validator(output_path, netlist_path)
                        print(
                            f"  Post-grow repair: "
                            f"{'VALID' if validator_result['valid'] else 'INVALID'}"
                            f" ({len(validator_result.get('errors', []))} errors)"
                        )
                        if validator_result["valid"]:
                            self.project.update_status(self.step_number, "COMPLETE")
                            return StepResult(success=True, output_path=str(output_path))
                    except Exception as e:
                        print(f"  Board grow fallback failed: {e}")

                # Fallback 2: Deterministic grid placement + SA repair
                try:
                    grown_w = round(board_width * 1.3, 1)
                    grown_h = round(board_height * 1.3, 1)
                    print(f"  Fallback 2: deterministic grid placement ({grown_w}x{grown_h}mm)...")
                    det_text = self._generate_deterministic_placement(
                        netlist_content, grown_w, grown_h,
                        project_name, netlist_filename, bom_filename,
                    )
                    if det_text:
                        # Correct footprint dimensions
                        det_text = self._correct_footprint_dimensions(det_text, netlist_content)
                        det_data = json.loads(det_text)
                        repaired = repair_placement(det_data, netlist_data)
                        repaired_text = json.dumps(repaired, indent=2)
                        output_path = self.project.write_output(output_filename, repaired_text)
                        validator_result = self._run_validator(output_path, netlist_path)
                        print(
                            f"  Deterministic + repair: "
                            f"{'VALID' if validator_result['valid'] else 'INVALID'}"
                            f" ({len(validator_result.get('errors', []))} errors)"
                        )
                        if validator_result["valid"]:
                            self.project.update_status(self.step_number, "COMPLETE")
                            return StepResult(success=True, output_path=str(output_path))
                except Exception as e:
                    print(f"  Deterministic fallback failed: {e}")

        # Exhausted rework attempts
        self.project.update_status(
            self.step_number, "BLOCKED", rework_count=max_rework,
            validator_errors=last_validator_result.get("errors"),
            validator_warnings=last_validator_result.get("warnings"),
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

    @staticmethod
    def _ensure_adequate_board_size(
        width: float, height: float, netlist_content: str,
        user_specified: bool = False,
    ) -> tuple[float, float]:
        """Ensure board is large enough for all components.

        Computes total component footprint area + 2.5x margin and expands
        the board if needed. Only expands defaults; user-specified sizes
        get a warning but are respected.
        """
        import math as _math

        try:
            from optimizers.pad_geometry import get_footprint_def
        except ImportError:
            return width, height

        netlist = json.loads(netlist_content)

        # Collect component areas
        comp_pin_counts: dict[str, int] = {}
        des_to_comp_id: dict[str, str] = {}
        components: list[dict] = []
        for elem in netlist.get("elements", []):
            if elem.get("element_type") == "port":
                cid = elem.get("component_id", "")
                comp_pin_counts[cid] = comp_pin_counts.get(cid, 0) + 1
            elif elem.get("element_type") == "component":
                des_to_comp_id[elem.get("designator", "")] = elem["component_id"]
                components.append(elem)

        total_area = 0.0
        max_dim = 0.0
        for comp in components:
            des = comp.get("designator", "")
            pkg = comp.get("package", "")
            cid = des_to_comp_id.get(des, "")
            pin_count = comp_pin_counts.get(cid, 2)

            fp = get_footprint_def(pkg, pin_count)
            if fp:
                pw, ph = fp.pad_size
                xs = [dx for dx, dy in fp.pin_offsets.values()]
                ys = [dy for dx, dy in fp.pin_offsets.values()]
                w = max(xs) - min(xs) + pw + 0.5
                h = max(ys) - min(ys) + ph + 0.5
            else:
                # Fallback estimate for unknown packages
                w = h = 3.0  # conservative default

            total_area += w * h
            max_dim = max(max_dim, w, h)

        # Target: component area * 2.5 margin (allows routing channels + clearances)
        target_area = total_area * 2.5
        current_area = width * height

        if current_area >= target_area:
            return width, height

        if user_specified:
            print(
                f"  Warning: user-specified board ({width}x{height}mm = {current_area:.0f}mm²) "
                f"may be tight for {len(components)} components "
                f"(component area: {total_area:.0f}mm², recommended: {target_area:.0f}mm²)"
            )
            return width, height

        # Compute new dimensions maintaining aspect ratio
        scale = _math.sqrt(target_area / current_area)
        new_w = round(width * scale, 1)
        new_h = round(height * scale, 1)

        # Ensure minimum dimension accommodates largest component + edge margins
        min_dim = max_dim + 4.0  # 2mm edge margin each side
        new_w = max(new_w, min_dim)
        new_h = max(new_h, min_dim)

        print(
            f"  Auto-sized board: {width}x{height}mm → {new_w}x{new_h}mm "
            f"({len(components)} components, {total_area:.0f}mm² footprint area)"
        )
        return new_w, new_h

    @staticmethod
    def _generate_deterministic_placement(
        netlist_content: str, board_width: float, board_height: float,
        project_name: str, netlist_filename: str, bom_filename: str,
    ) -> str | None:
        """Generate a grid-based deterministic placement as a fallback.

        Places components in rows sorted by size (largest first), with
        connectors on edges. Returns placement JSON string or None on failure.
        """
        import math as _math

        try:
            from optimizers.pad_geometry import get_footprint_def
        except ImportError:
            return None

        netlist = json.loads(netlist_content)

        # Collect components with dimensions
        comp_pin_counts: dict[str, int] = {}
        des_to_comp_id: dict[str, str] = {}
        components: list[dict] = []
        for elem in netlist.get("elements", []):
            if elem.get("element_type") == "port":
                cid = elem.get("component_id", "")
                comp_pin_counts[cid] = comp_pin_counts.get(cid, 0) + 1
            elif elem.get("element_type") == "component":
                des_to_comp_id[elem.get("designator", "")] = elem["component_id"]
                components.append(elem)

        if not components:
            return None

        # Compute footprint dimensions for each component
        comp_dims: list[tuple[dict, float, float]] = []
        for comp in components:
            des = comp.get("designator", "")
            pkg = comp.get("package", "")
            cid = des_to_comp_id.get(des, "")
            pin_count = comp_pin_counts.get(cid, 2)

            fp = get_footprint_def(pkg, pin_count)
            if fp:
                pw, ph = fp.pad_size
                xs = [dx for dx, dy in fp.pin_offsets.values()]
                ys = [dy for dx, dy in fp.pin_offsets.values()]
                w = round(max(xs) - min(xs) + pw + 0.5, 1)
                h = round(max(ys) - min(ys) + ph + 0.5, 1)
            else:
                w = h = 3.0

            comp_dims.append((comp, w, h))

        # Separate connectors (edges) from others
        connectors = [(c, w, h) for c, w, h in comp_dims
                      if c.get("component_type") == "connector"]
        others = [(c, w, h) for c, w, h in comp_dims
                  if c.get("component_type") != "connector"]

        # Sort non-connectors by area (largest first for better packing)
        others.sort(key=lambda x: x[1] * x[2], reverse=True)

        placements = []
        margin = 1.5  # edge margin
        clearance = 1.0  # between components

        # Place connectors along left edge
        cy = margin
        for comp, w, h in connectors:
            x = margin + w / 2
            y = cy + h / 2
            if y + h / 2 > board_height - margin:
                # Overflow — place on right edge
                x = board_width - margin - w / 2
                y = margin + h / 2
            placements.append({
                "designator": comp["designator"],
                "component_type": comp["component_type"],
                "package": comp.get("package", ""),
                "footprint_width_mm": w,
                "footprint_height_mm": h,
                "x_mm": round(x, 2),
                "y_mm": round(y, 2),
                "rotation_deg": 0,
                "layer": "top",
                "placement_source": "llm",
            })
            cy += h + clearance

        # Place remaining components in rows (left to right, bottom to top)
        row_x = margin + max((w for _, w, _ in connectors), default=0) + clearance * 2
        row_y = margin
        row_height = 0.0

        for comp, w, h in others:
            # Check if component fits in current row
            if row_x + w + margin > board_width:
                # Move to next row
                row_x = margin + max((cw for _, cw, _ in connectors), default=0) + clearance * 2
                row_y += row_height + clearance
                row_height = 0.0

            x = row_x + w / 2
            y = row_y + h / 2

            # If off board vertically, we've run out of space
            if y + h / 2 > board_height - margin:
                y = board_height - margin - h / 2  # clamp

            placements.append({
                "designator": comp["designator"],
                "component_type": comp["component_type"],
                "package": comp.get("package", ""),
                "footprint_width_mm": w,
                "footprint_height_mm": h,
                "x_mm": round(x, 2),
                "y_mm": round(y, 2),
                "rotation_deg": 0,
                "layer": "top",
                "placement_source": "llm",
            })

            row_x += w + clearance
            row_height = max(row_height, h)

        result = {
            "version": "1.0",
            "project_name": project_name,
            "source_netlist": netlist_filename,
            "source_bom": bom_filename,
            "board": {
                "width_mm": board_width,
                "height_mm": board_height,
                "layers": 2,
                "copper_thickness_oz": 1,
            },
            "placements": placements,
        }

        return json.dumps(result, indent=2)
