"""Step 1: Schematic/Netlist — generate, validate, QA, rework loop."""

import json
import logging
import re
import subprocess
import sys
from pathlib import Path

logger = logging.getLogger(__name__)

from .base import StepBase, StepResult

# validators/ is a sibling directory — add to path for pinout import
_validators_dir = str(Path(__file__).resolve().parent.parent.parent / "validators")
if _validators_dir not in sys.path:
    sys.path.insert(0, _validators_dir)

from pinout import build_pinout_from_requirements, expected_pin_count


def _strip_markdown_fences(raw: str) -> str:
    """Remove markdown code fences (```json ... ```) if present, returning bare content."""
    stripped = raw.strip()
    if stripped.startswith("```"):
        # Remove opening fence line
        first_nl = stripped.find("\n")
        if first_nl == -1:
            return stripped  # Just a fence line, nothing inside
        stripped = stripped[first_nl + 1:]
        # Remove closing fence if present
        if stripped.rstrip().endswith("```"):
            stripped = stripped.rstrip()[:-3].rstrip()
        return stripped
    return raw


def _json_error_context(candidate: str, err: json.JSONDecodeError) -> str:
    """Build a descriptive error message that includes position and surrounding context."""
    pos = err.pos
    ctx_start = max(0, pos - 120)
    ctx_end = min(len(candidate), pos + 120)
    before = candidate[ctx_start:pos]
    after = candidate[pos:ctx_end]
    return (
        f"JSON syntax error at line {err.lineno} col {err.colno} (char {pos}): {err.msg}. "
        f"Context: ...{before!r} ← ERROR → {after!r}..."
    )


def extract_json(raw: str) -> str:
    """Extract JSON from LLM response, handling markdown fences and continuations.

    Raises ValueError with error position and context so the rework prompt can
    tell the model exactly where its JSON was broken.
    """
    if not raw:
        raise ValueError("LLM returned empty/null response")
    raw = raw.strip()

    last_err: json.JSONDecodeError | None = None

    # Try direct parse
    try:
        json.loads(raw)
        return raw
    except json.JSONDecodeError as e:
        last_err = e

    # Try stripping markdown fences (greedy — get the largest match)
    fence_pattern = re.compile(r"```(?:json)?\s*\n?(.*?)\n?\s*```", re.DOTALL)
    for match in fence_pattern.finditer(raw):
        candidate = match.group(1).strip()
        try:
            json.loads(candidate)
            return candidate
        except json.JSONDecodeError as e:
            if last_err is None or e.pos > last_err.pos:
                last_err = e  # Keep the error that got furthest (most complete)

    # Try finding first { to last } — handles continuations that may have
    # extra text between chunks
    first_brace = raw.find("{")
    last_brace = raw.rfind("}")
    if first_brace != -1 and last_brace > first_brace:
        candidate = raw[first_brace : last_brace + 1]
        try:
            json.loads(candidate)
            return candidate
        except json.JSONDecodeError as e:
            if last_err is None or e.pos > last_err.pos:
                last_err = e

    # Try programmatic repair for common model error: extra closing braces.
    # Models often generate "description":"..."}}, (double }) for objects
    # without a nested "properties":{} field.
    for candidate_src in [raw, raw[first_brace : last_brace + 1] if first_brace != -1 else ""]:
        if not candidate_src:
            continue
        opens = candidate_src.count("{")
        closes = candidate_src.count("}")
        if closes > opens:
            repaired = candidate_src
            for _ in range(closes - opens):
                try:
                    json.loads(repaired)
                    logger.info(
                        "JSON repair: removed %d extra closing brace(s) from %d-char output",
                        closes - opens, len(raw),
                    )
                    return repaired
                except json.JSONDecodeError as e2:
                    if e2.pos > 0 and repaired[e2.pos - 1] == "}":
                        repaired = repaired[: e2.pos - 1] + repaired[e2.pos :]
                    else:
                        break
            try:
                json.loads(repaired)
                logger.info(
                    "JSON repair: removed %d extra closing brace(s) from %d-char output",
                    closes - opens, len(raw),
                )
                return repaired
            except json.JSONDecodeError:
                pass

    # Build an informative error that pinpoints where the JSON broke
    if last_err is not None and last_err.pos > 200:
        # Non-trivial parse: include position + context around the error
        ctx = _json_error_context(raw, last_err)
        raise ValueError(
            f"LLM output is {len(raw)} chars but has a JSON error — {ctx}"
        )
    raise ValueError(f"Could not extract valid JSON from LLM response:\n{raw[:200]}...")


def _sanitize_net_ids(netlist_text: str) -> str:
    """Replace illegal characters in net_id values with underscores.

    The schema requires net_id to match ^net_[a-z][a-z0-9_]*$.
    Models sometimes generate net_usb_d+_ser or net_3v3 (starts with digit).
    This fixes those silently rather than burning a rework attempt on it.
    Handles collisions (e.g. d+ and d- both → d__) by appending _2, _3 etc.
    """
    try:
        data = json.loads(netlist_text)
    except (json.JSONDecodeError, ValueError):
        return netlist_text

    _illegal = re.compile(r"[^a-z0-9_]")
    _net_id_re = re.compile(r"^net_[a-z][a-z0-9_]*$")

    # First pass: collect all current valid IDs and map old→new
    existing_ids: set[str] = set()
    for elem in data.get("elements", []):
        nid = elem.get("net_id", "")
        if nid and _net_id_re.match(nid):
            existing_ids.add(nid)

    id_map: dict[str, str] = {}
    changed = False

    for elem in data.get("elements", []):
        if elem.get("element_type") != "net":
            continue
        old_id = elem.get("net_id", "")
        if not old_id or _net_id_re.match(old_id):
            continue
        # Already remapped in a previous iteration?
        if old_id in id_map:
            elem["net_id"] = id_map[old_id]
            changed = True
            continue
        # Build sanitized base
        body = old_id[4:] if old_id.startswith("net_") else old_id
        body = _illegal.sub("_", body.lower()).strip("_") or "net"
        if body and body[0].isdigit():
            body = "n" + body
        base_id = "net_" + body
        # Resolve collision
        new_id = base_id
        counter = 2
        while new_id in existing_ids and new_id != old_id:
            new_id = f"{base_id}_{counter}"
            counter += 1
        id_map[old_id] = new_id
        existing_ids.add(new_id)
        elem["net_id"] = new_id
        changed = True

    if changed:
        print(f"  Auto-sanitized {len(id_map)} net_id(s) with illegal characters")

    return json.dumps(data, separators=(",", ":")) if changed else netlist_text


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
        pin_count_table = ""
        try:
            req_json = self.project.read_requirements_json()
            pinouts = build_pinout_from_requirements(req_json)
            if pinouts:
                pinout_table = self._format_pinout_table(pinouts)
            pin_count_table = self._format_pin_count_table(req_json)
        except Exception:
            pass  # No pinout data available — not fatal

        self.project.update_status(self.step_number, "IN_PROGRESS")

        # For large circuits (expected output > 8000 chars), single-generation is
        # unreliable — small/fast models corrupt JSON mid-output. Go straight to
        # chunked (components → ports → nets) for these.
        CHUNKED_THRESHOLD = 8000
        expected_output_size = self._estimate_expected_output_length()
        use_chunked_from_start = expected_output_size > CHUNKED_THRESHOLD

        previous_output = None
        issues = None
        consecutive_short = 0  # Track consecutive short/truncated outputs
        last_validator_result: dict = {}  # Track last validator result for error reporting
        # Pre-generated response for attempt 1 (set when use_chunked_from_start)
        initial_raw_response: str | None = None

        if use_chunked_from_start:
            print(f"  Large circuit (~{expected_output_size} chars expected) — using chunked generation")
            try:
                initial_raw_response = self._generate_chunked(
                    requirements_text, project_name, pinout_table, pin_count_table
                )
            except Exception as e:
                logger.error("Chunked generation failed: %s", e)
                return StepResult(success=False, error=f"Chunked generation failed: {e}")

        for attempt in range(1, max_rework + 1):
            # 1. Generate or rework netlist
            print(f"  [Attempt {attempt}/{max_rework}] Generating netlist...")
            if attempt == 1 and initial_raw_response is not None:
                # Large circuit: use pre-generated chunked output
                raw_response = initial_raw_response
            elif attempt == 1:
                prompt = self.prompt_builder.render(
                    "schematic_generate",
                    {
                        "requirements": requirements_text,
                        "project_name": project_name,
                        "pinout_table": pinout_table,
                        "pin_count_table": pin_count_table,
                    },
                )
            else:
                # Build rework_output: pass full valid JSON, or an intelligently truncated
                # snippet of invalid JSON so the model knows what to fix.
                rework_output = previous_output
                if rework_output and len(rework_output) > 2000:
                    try:
                        json.loads(rework_output)
                        # Valid JSON — pass it through (validator/QA failure, not truncation)
                    except (json.JSONDecodeError, ValueError):
                        expected = self._estimate_expected_output_length()
                        if len(rework_output) >= expected * 0.7:
                            # Near-complete output: show beginning + tail so model knows
                            # it was almost right and can see where it went wrong.
                            head = rework_output[:800]
                            tail = rework_output[-1200:]
                            skipped = len(rework_output) - 800 - 1200
                            rework_output = (
                                head
                                + f"\n\n... [{skipped} chars omitted — only head/tail shown] ...\n\n"
                                + tail
                            )
                            issues = list(issues or [])
                            issues.insert(0, (
                                f"Previous output was {len(previous_output)} chars and structurally "
                                f"complete but had a JSON syntax error. Fix ONLY the corrupted "
                                f"element (shown in the error above). Regenerate the FULL JSON."
                            ))
                        else:
                            # Short/truncated output — just show beginning + truncation note
                            rework_output = (
                                rework_output[:2000]
                                + f"\n\n[OUTPUT TRUNCATED — model stopped at {len(rework_output)} chars]"
                            )
                            issues = list(issues or [])
                            issues.insert(0, (
                                f"Previous output was truncated at {len(previous_output)} chars. "
                                f"This board requires approximately {expected} chars. "
                                f"You MUST generate the COMPLETE JSON in one response."
                            ))
                prompt = self.prompt_builder.render(
                    "schematic_rework",
                    {
                        "requirements": requirements_text,
                        "project_name": project_name,
                        "issues": issues,
                        "previous_output": rework_output,
                        "pinout_table": pinout_table,
                        "pin_count_table": pin_count_table,
                    },
                )

            # Skip LLM call when raw_response is already set (e.g., chunked generation)
            if not (attempt == 1 and initial_raw_response is not None):
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

            # Strip markdown fences early so short-output detection and
            # generate_long can see the raw JSON (model often adds fences
            # despite "no fences" instruction in prompt).
            raw_response = _strip_markdown_fences(raw_response)

            # 1b. Detect short output and attempt forced continuation
            expected_min = self._estimate_expected_output_length()
            if len(raw_response.strip()) < expected_min:
                consecutive_short += 1
                logger.warning(
                    "Short output detected: %d chars vs expected ~%d (consecutive=%d). "
                    "Attempting forced continuation.",
                    len(raw_response.strip()), expected_min, consecutive_short,
                )
                print(f"  [Attempt {attempt}] Short output ({len(raw_response)} chars vs ~{expected_min} expected), retrying with continuation...")

                # After 2+ consecutive short outputs, switch to chunked generation
                if consecutive_short >= 2:
                    print(f"  [Attempt {attempt}] Switching to chunked generation (components → ports → nets)...")
                    try:
                        raw_response = self._generate_chunked(
                            requirements_text, project_name, pinout_table,
                            pin_count_table,
                        )
                    except Exception as e:
                        logger.error("Chunked generation failed: %s", e)
                        print(f"  [Attempt {attempt}] Chunked generation failed: {e}")
                        issues = [f"Chunked generation failed: {e}"]
                        previous_output = raw_response
                        continue
                else:
                    raw_response = self.llm.generate_long(
                        system_prompt="",
                        user_prompt=prompt,
                        max_tokens=self.config.max_tokens,
                        temperature=self.config.temperature,
                        expected_min_length=expected_min,
                        partial_output=raw_response,
                    )
            else:
                consecutive_short = 0  # Reset on successful-length output

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
                    # Debug: write full response to file with diagnostics header
                    debug_path = self.project.get_output_path(f"debug_attempt_{attempt}.txt")
                    debug_header = (
                        f"# Debug attempt {attempt} — extract_json failed\n"
                        f"# Response length: {len(raw_response)} chars\n"
                        f"# Expected min: {self._estimate_expected_output_length()} chars\n"
                        f"# Error: {issues[0] if issues else 'unknown'}\n"
                        f"# ---\n"
                    )
                    debug_path.write_text(debug_header + raw_response)
                continue

            netlist_text = _sanitize_net_ids(netlist_text)
            output_path = self.project.write_output(output_filename, netlist_text)
            print(f"  Saved {output_filename}")

            # 3. Run validator
            validator_result = self._run_validator(output_path)
            last_validator_result = validator_result
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

    def _generate_chunked(self, requirements_text: str, project_name: str, pinout_table: str, pin_count_table: str = "") -> str:
        """Generate netlist in 3 phases (components, ports, nets) for large boards.

        Each phase uses generate_long() with a minimum-length check and writes
        a phase-specific debug file if the raw output is suspiciously short.
        """
        # Phase 1: components
        print("  [Chunked] Phase 1: Generating components...")
        p1_prompt = self.prompt_builder.render(
            "schematic_generate_components",
            {
                "requirements": requirements_text,
                "project_name": project_name,
                "pinout_table": pinout_table,
            },
        )
        # Components: ~200 chars each for a 35-component board ≈ 7000 chars minimum
        p1_min = max(2000, self._estimate_component_count() * 200)
        components_raw = self.llm.generate_long(
            system_prompt="",
            user_prompt=p1_prompt,
            max_tokens=self.config.max_tokens,
            temperature=self.config.temperature,
            expected_min_length=p1_min,
        )
        components_raw = _strip_markdown_fences(components_raw)
        print(f"  [Chunked] Phase 1 raw: {len(components_raw)} chars (min expected {p1_min})")
        try:
            components_json = self._extract_json_array(components_raw)
        except ValueError:
            if len(components_raw.strip()) < p1_min:
                debug_path = self.project.get_output_path("debug_chunked_phase1.txt")
                debug_path.write_text(components_raw)
                raise ValueError(
                    f"Phase 1 (components) output too short and invalid: {len(components_raw)} chars "
                    f"(expected ≥{p1_min}). Raw saved to {debug_path.name}"
                )
            raise
        print(f"  [Chunked] Phase 1 done: {len(json.loads(components_json))} components")

        # Phase 2: ports
        print("  [Chunked] Phase 2: Generating ports...")
        p2_prompt = self.prompt_builder.render(
            "schematic_generate_ports",
            {
                "requirements": requirements_text,
                "project_name": project_name,
                "pinout_table": pinout_table,
                "pin_count_table": pin_count_table,
                "components_json": components_json,
            },
        )
        # Ports: ~4 pins per component average (passives=2, ICs=10-32), ~130 chars each
        p2_min = max(3000, self._estimate_component_count() * 4 * 130)
        ports_raw = self.llm.generate_long(
            system_prompt="",
            user_prompt=p2_prompt,
            max_tokens=self.config.max_tokens,
            temperature=self.config.temperature,
            expected_min_length=p2_min,
        )
        ports_raw = _strip_markdown_fences(ports_raw)
        print(f"  [Chunked] Phase 2 raw: {len(ports_raw)} chars (min expected {p2_min})")
        # Try parse first — valid JSON is accepted regardless of length estimate
        try:
            ports_json = self._extract_json_array(ports_raw)
        except ValueError:
            if len(ports_raw.strip()) < p2_min:
                debug_path = self.project.get_output_path("debug_chunked_phase2.txt")
                debug_path.write_text(ports_raw)
                raise ValueError(
                    f"Phase 2 (ports) output too short and invalid: {len(ports_raw)} chars "
                    f"(expected ≥{p2_min}). Raw saved to {debug_path.name}"
                )
            raise  # Re-raise if long enough but invalid
        print(f"  [Chunked] Phase 2 done: {len(json.loads(ports_json))} ports")

        # Phase 3: nets
        print("  [Chunked] Phase 3: Generating nets...")
        p3_prompt = self.prompt_builder.render(
            "schematic_generate_nets",
            {
                "requirements": requirements_text,
                "project_name": project_name,
                "components_json": components_json,
                "ports_json": ports_json,
            },
        )
        # Nets: roughly component_count nets, each with ~300 chars
        p3_min = max(2000, self._estimate_component_count() * 300)
        nets_raw = self.llm.generate_long(
            system_prompt="",
            user_prompt=p3_prompt,
            max_tokens=self.config.max_tokens,
            temperature=self.config.temperature,
            expected_min_length=p3_min,
        )
        nets_raw = _strip_markdown_fences(nets_raw)
        print(f"  [Chunked] Phase 3 raw: {len(nets_raw)} chars (min expected {p3_min})")
        try:
            nets_json = self._extract_json_array(nets_raw)
        except ValueError:
            if len(nets_raw.strip()) < p3_min:
                debug_path = self.project.get_output_path("debug_chunked_phase3.txt")
                debug_path.write_text(nets_raw)
                raise ValueError(
                    f"Phase 3 (nets) output too short and invalid: {len(nets_raw)} chars "
                    f"(expected ≥{p3_min}). Raw saved to {debug_path.name}"
                )
            raise
        print(f"  [Chunked] Phase 3 done: {len(json.loads(nets_json))} nets")

        # Assemble into final netlist structure
        components = json.loads(components_json)
        ports = json.loads(ports_json)
        nets = json.loads(nets_json)

        netlist = {
            "version": "1.0",
            "project_name": project_name,
            "description": f"Circuit netlist for {project_name} (chunked generation)",
            "elements": components + ports + nets,
        }
        return json.dumps(netlist)

    def _estimate_component_count(self) -> int:
        """Return component count from requirements JSON, or a conservative fallback."""
        try:
            req_json = self.project.read_requirements_json()
            count = len(req_json.get("components", []))
            return count if count > 0 else 10
        except Exception:
            return 10

    @staticmethod
    def _extract_json_array(raw: str) -> str:
        """Extract a JSON array from LLM response."""
        raw = raw.strip()
        # Try direct parse
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, list):
                return raw
        except json.JSONDecodeError:
            pass
        # Strip markdown fences
        fence_pattern = re.compile(r"```(?:json)?\s*\n?(.*?)\n?\s*```", re.DOTALL)
        for match in fence_pattern.finditer(raw):
            candidate = match.group(1).strip()
            try:
                parsed = json.loads(candidate)
                if isinstance(parsed, list):
                    return candidate
            except json.JSONDecodeError:
                pass
        # Find first [ to last ]
        first = raw.find("[")
        last = raw.rfind("]")
        if first != -1 and last > first:
            candidate = raw[first : last + 1]
            try:
                parsed = json.loads(candidate)
                if isinstance(parsed, list):
                    return candidate
            except json.JSONDecodeError:
                pass
        raise ValueError(f"Could not extract JSON array from response:\n{raw[:200]}...")

    def _estimate_expected_output_length(self) -> int:
        """Estimate minimum expected output length in chars based on component count."""
        component_count = self._estimate_component_count()
        # ~600 chars per component (component obj + ports + share of nets)
        return max(5000, component_count * 600)

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

    @staticmethod
    def _format_pin_count_table(req_json: dict) -> str:
        """Build a markdown table of expected port counts per component for prompt injection."""
        rows: list[tuple[str, str, int]] = []
        for comp in req_json.get("components", []):
            ref = comp.get("ref", "?")
            package = comp.get("package", "")
            specs = comp.get("specs", {})
            count = expected_pin_count(package, specs)
            if count is not None:
                rows.append((ref, package, count))
        if not rows:
            return ""
        lines = ["| Ref | Package | Required Ports |",
                 "|-----|---------|----------------|"]
        for ref, pkg, count in rows:
            lines.append(f"| {ref} | {pkg} | {count} |")
        return "\n".join(lines)
