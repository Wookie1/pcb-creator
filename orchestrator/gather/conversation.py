"""LLM-assisted requirements gathering with deterministic Python control."""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor, as_completed

from orchestrator.gather.curated_specs import lookup_footprint_dims, lookup_specs
from orchestrator.gather.schema import validate_requirements
from orchestrator.llm.base import LLMClient
from orchestrator.prompts.builder import PromptBuilder
from orchestrator.steps.step_1_schematic import extract_json


class RequirementsGatherer:
    """Gathers requirements through a Python-controlled conversation loop.

    Flow:
    1. User provides natural language description
    2. Python calls plan agent → design plan with assumptions + questions
    3. User answers questions (or skips to accept defaults)
    4. Python calls translate agent → structured JSON (with plan context)
    5. Python validates JSON against schema
    6. Python calls summarize agent → human-readable summary
    7. Python shows summary, asks user to approve
    8. If rejected → Python calls translate agent again with feedback
    9. If approved → return validated JSON
    """

    def __init__(
        self,
        llm: LLMClient,
        prompt_builder: PromptBuilder,
        *,
        cache: object | None = None,
        max_workers: int = 4,
    ):
        self.llm = llm
        self.prompt_builder = prompt_builder
        self.cache = cache
        self.max_workers = max_workers

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

        # --- Planning step: propose design and ask clarifying questions ---
        plan_context = self._run_interactive_plan(user_input)

        previous_json = None
        feedback = None

        for attempt in range(1, max_attempts + 1):
            # Translate to structured JSON (with plan context on first attempt)
            print(f"\n  Translating requirements (attempt {attempt})...")
            requirements = self._translate(
                user_input, feedback, previous_json,
                plan_context=plan_context if attempt == 1 else None,
            )

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

            # Step 2.5: Enrich components with missing specs via tiered lookup
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

    def plan(self, user_input: str) -> dict | None:
        """Public API: generate a design plan with assumptions and questions.

        Returns parsed plan dict or None on failure.
        """
        return self._plan(user_input)

    def translate(
        self,
        user_input: str,
        feedback: str | None = None,
        previous_json: str | None = None,
        plan_context: str | None = None,
    ) -> dict | None:
        """Public API for programmatic use. Returns validated requirements or None."""
        return self._translate(user_input, feedback, previous_json, plan_context=plan_context)

    def summarize(self, requirements: dict) -> str:
        """Public API: generate human-readable summary from requirements JSON."""
        return self._summarize(requirements)

    # ------------------------------------------------------------------
    # Planning step
    # ------------------------------------------------------------------

    def _plan(self, user_input: str) -> dict | None:
        """Generate a design plan with proposed assumptions and clarifying questions."""
        context = {"user_input": user_input}
        try:
            prompt = self.prompt_builder.render("gather_plan", context)
            raw = self.llm.generate(
                system_prompt="",
                user_prompt=prompt,
                max_tokens=2048,
                temperature=0.0,
            )
            return json.loads(extract_json(raw))
        except (json.JSONDecodeError, ValueError, Exception) as e:
            print(f"  Planning step failed: {e}")
            return None

    @staticmethod
    def format_plan_context(plan: dict, answers: dict[str, str] | None = None) -> str:
        """Format plan + user answers as context for the translator.

        This structured text is injected into the translate prompt so the LLM
        has a clear, disambiguated specification to work from.
        """
        lines: list[str] = []

        understanding = plan.get("understanding", "")
        if understanding:
            lines.append(f"Circuit purpose: {understanding}")

        design = plan.get("proposed_design", {})
        if design:
            lines.append("\nProposed design:")
            for key in ("power", "packages", "board"):
                if key in design:
                    lines.append(f"  - {key}: {design[key]}")
            for comp in design.get("key_components", []):
                lines.append(f"  - Component: {comp}")
            for note in design.get("notes", []):
                lines.append(f"  - Note: {note}")

        # Apply user answers (override defaults)
        questions = plan.get("questions", [])
        if questions:
            lines.append("\nDesign decisions:")
            for q in questions:
                topic = q.get("topic", "")
                default = q.get("default", "")
                answer = (answers or {}).get(topic, default)
                lines.append(f"  - {topic}: {answer}")

        return "\n".join(lines)

    def _run_interactive_plan(self, user_input: str) -> str | None:
        """Run the planning step interactively on the CLI.

        Returns formatted plan context string, or None if skipped/failed.
        """
        print("\n  Analyzing your circuit design...")
        plan = self._plan(user_input)
        if plan is None:
            print("  (Planning step skipped — proceeding directly to translation)")
            return None

        # Display the plan
        understanding = plan.get("understanding", "")
        if understanding:
            print(f"\n  Understanding: {understanding}")

        design = plan.get("proposed_design", {})
        if design:
            print("\n  Proposed design:")
            for key in ("power", "packages", "board"):
                if key in design:
                    print(f"    {key}: {design[key]}")
            for comp in design.get("key_components", []):
                print(f"    - {comp}")
            if design.get("notes"):
                print("    Notes:")
                for note in design["notes"]:
                    print(f"      - {note}")

        questions = plan.get("questions", [])
        if not questions:
            print("\n  No clarifying questions needed.")
            return self.format_plan_context(plan)

        # Ask questions
        print(f"\n  {len(questions)} question(s) — press Enter to accept default, or type your answer.")
        print("  Type 'skip' to accept all defaults.\n")

        answers: dict[str, str] = {}
        for i, q in enumerate(questions, 1):
            topic = q.get("topic", f"Q{i}")
            question = q.get("question", "")
            default = q.get("default", "")

            response = input(f"  {i}. {question}\n     [{default}]: ").strip()

            if response.lower() == "skip":
                # Accept all remaining defaults
                for remaining_q in questions[i - 1:]:
                    t = remaining_q.get("topic", "")
                    answers[t] = remaining_q.get("default", "")
                print("  Accepting defaults for remaining questions.")
                break

            answers[topic] = response if response else default

        return self.format_plan_context(plan, answers)

    # ------------------------------------------------------------------
    # Translation
    # ------------------------------------------------------------------

    def _translate(
        self,
        user_input: str,
        feedback: str | None = None,
        previous_json: str | None = None,
        plan_context: str | None = None,
    ) -> dict | None:
        """Call translate agent to convert user input to structured JSON."""
        context = {
            "user_input": user_input,
            "feedback": feedback or "",
            "previous_json": previous_json or "",
            "plan_context": plan_context or "",
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

    # ------------------------------------------------------------------
    # Spec enrichment — tiered: curated → cache → parallel LLM
    # ------------------------------------------------------------------

    @staticmethod
    def _needs_spec_lookup(ctype: str, specs: dict) -> bool:
        """Determine if a component needs spec enrichment."""
        if ctype == "led" and "vf" not in specs:
            return True
        if ctype in ("ic", "voltage_regulator") and ("pin_count" not in specs or "pinout" not in specs):
            return True
        if ctype == "capacitor" and "voltage_rating" not in specs:
            return True
        if ctype in ("transistor_npn", "transistor_pnp", "transistor_nmos", "transistor_pmos"):
            if "vce_max" not in specs and "vds_max" not in specs:
                return True
        return False

    def _enrich_component_specs(self, requirements: dict) -> dict:
        """Look up missing specs using tiered resolution.

        Tiers:
          1. Curated lookup table (instant, most trusted)
          2. Local JSON cache (instant, from prior runs)
          3. LLM datasheet query (parallel, cached for next time)
        """
        components = requirements.get("components", [])
        enriched = False
        need_llm: list[dict] = []  # components that need LLM fallback

        for comp in components:
            ctype = comp.get("type", "")
            specs = comp.get("specs", {})
            value = comp.get("value", "")
            package = comp.get("package", "")

            if not self._needs_spec_lookup(ctype, specs):
                continue

            ref = comp.get("ref", "?")
            cache_key = f"{ctype}:{value}:{package}"

            # --- Tier 1: curated table ---
            looked_up = lookup_specs(ctype, value, package)
            if looked_up:
                for k, v in looked_up.items():
                    if k not in specs:
                        specs[k] = v
                comp["specs"] = specs
                enriched = True
                print(f"  {ref} specs from curated table: {', '.join(f'{k}={v}' for k, v in looked_up.items())}")
                # Re-check if we still need more
                if not self._needs_spec_lookup(ctype, specs):
                    continue

            # --- Tier 2: cache ---
            if self.cache is not None:
                cached = self.cache.get_specs(cache_key)
                if cached:
                    for k, v in cached.items():
                        if k not in specs and k not in ("source", "resolved", "needs_review"):
                            specs[k] = v
                    comp["specs"] = specs
                    enriched = True
                    print(f"  {ref} specs from cache")
                    if not self._needs_spec_lookup(ctype, specs):
                        continue

            # --- Tier 3: queue for LLM ---
            need_llm.append(comp)

        # Fire remaining LLM calls in parallel
        if need_llm:
            enriched = self._parallel_spec_llm(need_llm) or enriched

        if enriched:
            print("  Component specs enriched.")

        return requirements

    def _parallel_spec_llm(self, components: list[dict]) -> bool:
        """Run LLM spec lookups in parallel for components that missed local tiers."""
        enriched = False

        def _lookup_one(comp: dict) -> tuple[dict, dict | None]:
            ctype = comp.get("type", "")
            value = comp.get("value", "")
            ref = comp.get("ref", "?")
            context = {
                "component_type": ctype,
                "value": value,
                "package": comp.get("package", ""),
                "purpose": comp.get("purpose", ""),
            }
            try:
                prompt = self.prompt_builder.render("gather_datasheet", context)
                lookup_tokens = 2048 if ctype in ("ic", "voltage_regulator") else 1024
                raw = self.llm.generate(
                    system_prompt="",
                    user_prompt=prompt,
                    max_tokens=lookup_tokens,
                    temperature=0.0,
                )
                return comp, json.loads(extract_json(raw))
            except (json.JSONDecodeError, ValueError, Exception) as e:
                print(f"    Spec lookup failed for {ref}: {e}")
                return comp, None

        with ThreadPoolExecutor(max_workers=self.max_workers) as pool:
            futures = {pool.submit(_lookup_one, comp): comp for comp in components}
            for future in as_completed(futures):
                comp, looked_up = future.result()
                if looked_up is None:
                    continue

                ref = comp.get("ref", "?")
                specs = comp.get("specs", {})
                for k, v in looked_up.items():
                    if k not in specs:
                        specs[k] = v
                comp["specs"] = specs
                enriched = True
                print(f"  {ref} specs from LLM: {', '.join(f'{k}={v}' for k, v in looked_up.items())}")

                # Cache result
                if self.cache is not None:
                    ctype = comp.get("type", "")
                    value = comp.get("value", "")
                    package = comp.get("package", "")
                    cache_key = f"{ctype}:{value}:{package}"
                    self.cache.put_specs(cache_key, looked_up, source="llm", needs_review=True)

        return enriched

    # ------------------------------------------------------------------
    # Footprint dimension enrichment
    # tiered: curated → cache → EasyEDA → parallel LLM
    # ------------------------------------------------------------------

    def _enrich_footprints(self, requirements: dict) -> dict:
        """Look up footprint bounding box dimensions using tiered resolution.

        Tiers:
          1. Curated footprint dimensions table
          2. Local JSON cache
          3. EasyEDA/LCSC API (if easyeda2kicad is installed)
          4. LLM footprint query (parallel, cached for next time)
        """
        components = requirements.get("components", [])
        enriched = False
        need_llm: list[dict] = []

        for comp in components:
            specs = comp.get("specs", {})
            if "footprint_width_mm" in specs:
                continue

            package = comp.get("package", "")
            if not package:
                continue

            ref = comp.get("ref", "?")

            # --- Tier 1: curated table ---
            dims = lookup_footprint_dims(package)
            if dims:
                for k in ("footprint_width_mm", "footprint_height_mm", "courtyard_margin_mm"):
                    if k in dims and k not in specs:
                        specs[k] = dims[k]
                comp["specs"] = specs
                enriched = True
                w = specs.get("footprint_width_mm", "?")
                h = specs.get("footprint_height_mm", "?")
                print(f"  {ref} footprint from curated: {w} x {h} mm")
                continue

            # --- Tier 2: cache ---
            if self.cache is not None:
                cache_key = f"footprint_dims:{package}"
                cached = self.cache.get_specs(cache_key)
                if cached:
                    for k in ("footprint_width_mm", "footprint_height_mm", "courtyard_margin_mm"):
                        if k in cached and k not in specs:
                            specs[k] = cached[k]
                    comp["specs"] = specs
                    enriched = True
                    w = specs.get("footprint_width_mm", "?")
                    h = specs.get("footprint_height_mm", "?")
                    print(f"  {ref} footprint from cache: {w} x {h} mm")
                    continue

            # --- Tier 3: EasyEDA/LCSC API ---
            easyeda_dims = self._try_easyeda_footprint(comp)
            if easyeda_dims:
                for k in ("footprint_width_mm", "footprint_height_mm", "courtyard_margin_mm"):
                    if k in easyeda_dims and k not in specs:
                        specs[k] = easyeda_dims[k]
                comp["specs"] = specs
                enriched = True
                w = specs.get("footprint_width_mm", "?")
                h = specs.get("footprint_height_mm", "?")
                print(f"  {ref} footprint from EasyEDA: {w} x {h} mm")
                # Cache EasyEDA result
                if self.cache is not None:
                    cache_key = f"footprint_dims:{package}"
                    self.cache.put_specs(cache_key, easyeda_dims, source="easyeda")
                continue

            # --- Tier 4: queue for LLM ---
            need_llm.append(comp)

        # Fire remaining LLM calls in parallel
        if need_llm:
            enriched = self._parallel_footprint_llm(need_llm) or enriched

        if enriched:
            print("  Footprint dimensions enriched.")

        return requirements

    @staticmethod
    def _try_easyeda_footprint(comp: dict) -> dict | None:
        """Try to get footprint dimensions from EasyEDA/LCSC API.

        Returns dict with footprint_width_mm/footprint_height_mm or None.
        """
        try:
            from orchestrator.gather.easyeda_lookup import fetch_footprint
        except ImportError:
            return None

        value = comp.get("value", "")
        lcsc_id = comp.get("specs", {}).get("lcsc_id", "")
        fp = fetch_footprint(value, lcsc_id=lcsc_id)
        if fp is None:
            return None

        # Convert FootprintDef pin positions to bounding box dimensions
        xs = [pos[0] for pos in fp.pin_offsets.values()]
        ys = [pos[1] for pos in fp.pin_offsets.values()]
        if not xs:
            return None

        pw, ph = fp.pad_size
        w = round((max(xs) - min(xs)) + pw + 0.5, 2)  # + courtyard
        h = round((max(ys) - min(ys)) + ph + 0.5, 2)
        return {
            "footprint_width_mm": w,
            "footprint_height_mm": h,
            "courtyard_margin_mm": 0.25,
        }

    def _parallel_footprint_llm(self, components: list[dict]) -> bool:
        """Run LLM footprint lookups in parallel."""
        enriched = False

        def _lookup_one(comp: dict) -> tuple[dict, dict | None]:
            ref = comp.get("ref", "?")
            context = {
                "component_type": comp.get("type", ""),
                "value": comp.get("value", ""),
                "package": comp.get("package", ""),
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
                return comp, json.loads(extract_json(raw))
            except (json.JSONDecodeError, ValueError, Exception) as e:
                print(f"    Footprint lookup failed for {ref}: {e}")
                return comp, None

        with ThreadPoolExecutor(max_workers=self.max_workers) as pool:
            futures = {pool.submit(_lookup_one, comp): comp for comp in components}
            for future in as_completed(futures):
                comp, looked_up = future.result()
                if looked_up is None:
                    continue

                ref = comp.get("ref", "?")
                specs = comp.get("specs", {})
                for k in ("footprint_width_mm", "footprint_height_mm", "courtyard_margin_mm"):
                    if k in looked_up and k not in specs:
                        specs[k] = looked_up[k]
                comp["specs"] = specs
                enriched = True
                w = specs.get("footprint_width_mm", "?")
                h = specs.get("footprint_height_mm", "?")
                print(f"  {ref} footprint from LLM: {w} x {h} mm")

                # Cache result
                if self.cache is not None:
                    package = comp.get("package", "")
                    cache_key = f"footprint_dims:{package}"
                    self.cache.put_specs(cache_key, looked_up, source="llm", needs_review=True)

        return enriched

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
