"""LLM-assisted requirements gathering with deterministic Python control."""

import json

from orchestrator.gather.schema import validate_requirements
from orchestrator.llm.base import LLMClient
from orchestrator.prompts.builder import PromptBuilder
from orchestrator.steps.step_1_schematic import extract_json


class RequirementsGatherer:
    """Gathers requirements through a Python-controlled conversation loop.

    Flow:
    1. User provides natural language description
    2. Python calls translate agent → structured JSON
    3. Python validates JSON against schema
    4. Python calls summarize agent → human-readable summary
    5. Python shows summary, asks user to approve
    6. If rejected → Python calls translate agent again with feedback
    7. If approved → return validated JSON
    """

    def __init__(self, llm: LLMClient, prompt_builder: PromptBuilder):
        self.llm = llm
        self.prompt_builder = prompt_builder

    def gather_interactive(self, max_attempts: int = 5) -> dict | None:
        """Run interactive requirements gathering from CLI.

        Returns validated requirements dict, or None if user cancels.
        """
        print("\n=== PCB-Creator Requirements Gathering ===\n")
        print("Describe your circuit. Include what it does, components needed,")
        print("power supply, and any specific requirements.\n")

        user_input = input("Your circuit description:\n> ").strip()
        if not user_input:
            print("No input provided.")
            return None

        previous_json = None
        feedback = None

        for attempt in range(1, max_attempts + 1):
            # Step 1: Translate to structured JSON
            print(f"\n  Translating requirements (attempt {attempt})...")
            requirements = self._translate(user_input, feedback, previous_json)

            if requirements is None:
                print("  Failed to generate structured requirements.")
                if attempt < max_attempts:
                    feedback = input("\nProvide additional details or corrections:\n> ").strip()
                    continue
                return None

            # Step 2: Validate against schema
            errors = validate_requirements(requirements)
            if errors:
                print(f"  Schema validation failed ({len(errors)} errors):")
                for e in errors:
                    print(f"    - {e}")
                # Auto-retry with validation errors as feedback
                feedback = "The requirements JSON had validation errors:\n" + "\n".join(
                    f"- {e}" for e in errors
                )
                previous_json = json.dumps(requirements, indent=2)
                continue

            # Step 2.5: Enrich components with missing specs via LLM datasheet lookup
            requirements = self._enrich_component_specs(requirements)

            # Step 2.6: Enrich components with footprint dimensions for layout
            requirements = self._enrich_footprints(requirements)

            # Step 3: Summarize for human review
            print("  Generating summary for review...\n")
            summary = self._summarize(requirements)
            print("=" * 50)
            print(summary)
            print("=" * 50)

            # Step 4: Get approval
            response = input("\nApprove these requirements? [y/n/quit]: ").strip().lower()

            if response in ("y", "yes", "approve"):
                return requirements
            elif response in ("quit", "q", "exit"):
                return None
            else:
                feedback = input("What should be changed?\n> ").strip()
                previous_json = json.dumps(requirements, indent=2)
                continue

        print(f"\nMax attempts ({max_attempts}) reached.")
        return None

    def translate(
        self,
        user_input: str,
        feedback: str | None = None,
        previous_json: str | None = None,
    ) -> dict | None:
        """Public API for programmatic use. Returns validated requirements or None."""
        return self._translate(user_input, feedback, previous_json)

    def summarize(self, requirements: dict) -> str:
        """Public API: generate human-readable summary from requirements JSON."""
        return self._summarize(requirements)

    def _translate(
        self,
        user_input: str,
        feedback: str | None = None,
        previous_json: str | None = None,
    ) -> dict | None:
        """Call translate agent to convert user input to structured JSON."""
        context = {
            "user_input": user_input,
            "feedback": feedback or "",
            "previous_json": previous_json or "",
        }

        prompt = self.prompt_builder.render("gather_translate", context)

        try:
            raw = self.llm.generate(
                system_prompt="",
                user_prompt=prompt,
                max_tokens=4096,
                temperature=0.0,
            )
            json_str = extract_json(raw)
            return json.loads(json_str)
        except (json.JSONDecodeError, ValueError) as e:
            print(f"  Error parsing LLM response: {e}")
            return None

    def _enrich_component_specs(self, requirements: dict) -> dict:
        """Look up missing specs for components via LLM datasheet query.

        Scans components for missing critical specs and calls the LLM to
        resolve them. Results are merged into the requirements before user
        approval so they can review and correct.
        """
        components = requirements.get("components", [])
        enriched = False

        for comp in components:
            ctype = comp.get("type", "")
            specs = comp.get("specs", {})
            value = comp.get("value", "")

            # Determine if this component needs enrichment
            needs_lookup = False
            if ctype == "led" and "vf" not in specs:
                needs_lookup = True
            elif ctype in ("ic", "voltage_regulator") and ("pin_count" not in specs or "pinout" not in specs):
                needs_lookup = True
            elif ctype == "capacitor" and "voltage_rating" not in specs:
                needs_lookup = True
            elif ctype in ("transistor_npn", "transistor_pnp", "transistor_nmos", "transistor_pmos") and "vce_max" not in specs and "vds_max" not in specs:
                needs_lookup = True

            if not needs_lookup:
                continue

            ref = comp.get("ref", "?")
            print(f"  Looking up specs for {ref} ({value})...")

            context = {
                "component_type": ctype,
                "value": value,
                "package": comp.get("package", ""),
                "purpose": comp.get("purpose", ""),
            }

            try:
                prompt = self.prompt_builder.render("gather_datasheet", context)
                # ICs need more tokens for pinout data
                lookup_tokens = 2048 if ctype in ("ic", "voltage_regulator") else 1024
                raw = self.llm.generate(
                    system_prompt="",
                    user_prompt=prompt,
                    max_tokens=lookup_tokens,
                    temperature=0.0,
                )
                looked_up = json.loads(extract_json(raw))

                # Merge looked-up specs into existing specs (don't overwrite existing)
                for k, v in looked_up.items():
                    if k not in specs:
                        specs[k] = v
                comp["specs"] = specs
                enriched = True
                print(f"    Found: {', '.join(f'{k}={v}' for k, v in looked_up.items())}")

            except (json.JSONDecodeError, ValueError, Exception) as e:
                print(f"    Spec lookup failed for {ref}: {e}")

        if enriched:
            print("  Component specs enriched from datasheet lookup.")

        return requirements

    def _enrich_footprints(self, requirements: dict) -> dict:
        """Look up footprint bounding box dimensions for components missing them.

        Adds footprint_width_mm and footprint_height_mm to each component's
        specs so the placement validator can check for overlaps and boundary
        violations.
        """
        components = requirements.get("components", [])
        enriched = False

        for comp in components:
            specs = comp.get("specs", {})
            if "footprint_width_mm" in specs:
                continue  # Already has footprint dimensions

            package = comp.get("package", "")
            if not package:
                continue  # No package to look up

            ref = comp.get("ref", "?")
            value = comp.get("value", "")
            print(f"  Looking up footprint for {ref} ({package})...")

            context = {
                "component_type": comp.get("type", ""),
                "value": value,
                "package": package,
                "purpose": comp.get("purpose", ""),
            }

            try:
                prompt = self.prompt_builder.render("gather_footprint", context)
                raw = self.llm.generate(
                    system_prompt="",
                    user_prompt=prompt,
                    max_tokens=256,
                    temperature=0.0,
                )
                looked_up = json.loads(extract_json(raw))

                for k in ("footprint_width_mm", "footprint_height_mm", "courtyard_margin_mm"):
                    if k in looked_up and k not in specs:
                        specs[k] = looked_up[k]
                comp["specs"] = specs
                enriched = True
                w = specs.get("footprint_width_mm", "?")
                h = specs.get("footprint_height_mm", "?")
                print(f"    Footprint: {w} x {h} mm")

            except (json.JSONDecodeError, ValueError, Exception) as e:
                print(f"    Footprint lookup failed for {ref}: {e}")

        if enriched:
            print("  Footprint dimensions enriched from lookup.")

        return requirements

    def _summarize(self, requirements: dict) -> str:
        """Call summarize agent to produce human-readable summary."""
        context = {"requirements_json": json.dumps(requirements, indent=2)}
        prompt = self.prompt_builder.render("gather_summarize", context)

        try:
            return self.llm.generate(
                system_prompt="",
                user_prompt=prompt,
                max_tokens=2048,
                temperature=0.0,
            )
        except Exception as e:
            # Fallback: just pretty-print the JSON
            return f"(Summary generation failed: {e})\n\n{json.dumps(requirements, indent=2)}"
