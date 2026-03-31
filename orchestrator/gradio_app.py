"""Gradio web GUI for pcb-creator."""

from __future__ import annotations

import functools
import json
import logging
import os
import re
import traceback
from pathlib import Path

# Disable Gradio telemetry before importing
os.environ["GRADIO_ANALYTICS_ENABLED"] = "False"

try:
    import gradio as gr
except ImportError:
    raise SystemExit(
        "Gradio is required for the GUI. Install it with:\n"
        "  pip install gradio\n"
        "Or use the project venv:\n"
        "  .venv/bin/python -m orchestrator gui"
    )

from .config import OrchestratorConfig
from .runner import STEP_NAMES, run_workflow_streaming

logger = logging.getLogger("pcb-creator.gui")

# Number of outputs every handler must yield/return.
# status, state, chat_display, input_group, chat_input, submit, approve, steps, viewer, settings, export_row
_N_OUTPUTS = 11


# ---------------------------------------------------------------------------
# Provider presets
# ---------------------------------------------------------------------------

PROVIDER_PRESETS = {
    "OpenRouter": {
        "api_base": "",
        "model": "openrouter/x-ai/grok-4.1-fast",
        "max_tokens": 32768,
    },
    "Local (oMLX)": {
        "api_base": "http://localhost:8000/v1",
        "model": "openai/Qwen3.5-27B-MLX-7bit",
        "max_tokens": 32768,
    },
    "Custom": {
        "api_base": "",
        "model": "",
        "max_tokens": 32768,
    },
}


def _apply_preset(provider: str):
    """Return (api_base, model, max_tokens) for the selected provider."""
    preset = PROVIDER_PRESETS.get(provider, PROVIDER_PRESETS["Custom"])
    return preset["api_base"], preset["model"], preset["max_tokens"]


# ---------------------------------------------------------------------------
# Step progress rendering
# ---------------------------------------------------------------------------

_STATUS_ICONS = {
    "pending": "\u23f3",       # hourglass
    "running": "\u25b6\ufe0f", # play
    "done": "\u2705",          # check
    "failed": "\u274c",        # cross
}


def _render_steps(statuses: dict[int, str]) -> str:
    rows = []
    for step_num in range(7):
        status = statuses.get(step_num, "pending")
        icon = _STATUS_ICONS.get(status, "")
        name = STEP_NAMES.get(step_num, f"Step {step_num}")
        rows.append(
            f'<div class="step-row step-{status}">'
            f'<span class="step-icon">{icon}</span>'
            f'<span class="step-label">Step {step_num}: {name}</span>'
            f'</div>'
        )
    return (
        '<div style="font-family:monospace;font-size:14px;line-height:2;">'
        + "\n".join(rows) + "</div>"
    )


# ---------------------------------------------------------------------------
# Chat rendering — rich markdown for the conversation display
# ---------------------------------------------------------------------------

def _render_chat_markdown(messages: list[dict]) -> str:
    """Render conversation as markdown for gr.Markdown display."""
    if not messages:
        return ""
    parts = []
    for msg in messages:
        role = msg["role"]
        text = msg["content"]
        if role == "user":
            parts.append(f"**You:** {text}")
        elif role == "assistant":
            parts.append(f"**PCB Creator:**\n\n{text}")
        else:
            # System messages (status markers)
            parts.append(f"*{text}*")
        parts.append("")  # blank line between messages
    return "\n".join(parts).rstrip()


# ---------------------------------------------------------------------------
# Plan rendering
# ---------------------------------------------------------------------------

def _render_plan_message(plan: dict) -> str:
    """Format a design plan as a readable chat message."""
    lines: list[str] = []

    understanding = plan.get("understanding", "")
    if understanding:
        lines.append(f"**My understanding:** {understanding}")

    design = plan.get("proposed_design", {})
    if design:
        lines.append("\n**Proposed design:**")
        for key in ("power", "packages", "board"):
            if key in design:
                lines.append(f"- **{key.title()}:** {design[key]}")
        for comp in design.get("key_components", []):
            lines.append(f"- {comp}")
        for note in design.get("notes", []):
            lines.append(f"- *{note}*")

    questions = plan.get("questions", [])
    if questions:
        lines.append(f"\n**{len(questions)} question(s):**\n")
        for i, q in enumerate(questions, 1):
            question = q.get("question", "")
            default = q.get("default", "")
            lines.append(f"{i}. {question}")
            lines.append(f"   *Default: {default}*\n")

    lines.append("---")
    lines.append("You can **answer questions**, **suggest changes** "
                 "(e.g., *\"use WS2812B instead of regular LEDs\"*), "
                 "or click **Proceed to Design \u27a1\ufe0f** when the plan looks right.")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Error rendering
# ---------------------------------------------------------------------------

def _render_error_html(title: str, detail: str, tb: str = "",
                       hint: str = "") -> str:
    tb_section = ""
    if tb:
        esc = tb.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        tb_section = (
            '<details style="margin-top:12px;">'
            '<summary style="cursor:pointer;color:#888;">Full traceback</summary>'
            f'<pre style="background:#1e1e1e;color:#f8d7da;padding:12px;'
            f'border-radius:6px;overflow-x:auto;font-size:12px;margin-top:8px;'
            f'white-space:pre-wrap;">{esc}</pre></details>')
    hint_section = ""
    if hint:
        hint_section = (
            f'<div style="background:#cce5ff;border:1px solid #b8daff;'
            f'border-radius:6px;padding:12px;margin-top:12px;color:#004085;">'
            f'<strong>How to fix:</strong> {hint}</div>')
    esc_detail = detail.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return (
        '<div style="padding:24px;max-width:700px;margin:20px auto;">'
        f'<div style="background:#f8d7da;border:1px solid #f5c2c7;'
        f'border-radius:8px;padding:16px;">'
        f'<h3 style="color:#842029;margin:0 0 8px 0;">\u274c {title}</h3>'
        f'<p style="color:#842029;margin:0;font-family:monospace;'
        f'white-space:pre-wrap;">{esc_detail}</p>'
        f'{hint_section}{tb_section}</div></div>')


def _categorize_error(exc: Exception, tb_str: str) -> tuple[str, str, bool]:
    msg = str(exc).lower()
    if any(k in msg for k in ("connection refused", "connect call failed",
                               "connectionrefusederror", "max retries")):
        return ("Cannot Connect to Model Server",
                "Check the server is running and the API Base URL.", True)
    if any(k in msg for k in ("401", "unauthorized", "invalid api key",
                               "authentication", "api key")):
        return ("API Authentication Failed",
                "Enter a valid API key in AI Model Settings.", True)
    if any(k in msg for k in ("429", "rate limit", "too many requests")):
        return ("Rate Limited", "Wait a minute and try again.", True)
    if any(k in msg for k in ("404", "model not found", "does not exist")):
        return ("Model Not Found", "Check the Model Name in settings.", True)
    if any(k in msg for k in ("timeout", "timed out", "deadline exceeded")):
        return ("Request Timed Out", "Try a faster model or simpler circuit.", True)
    if any(k in msg for k in ("json", "expecting value", "unterminated string")):
        return ("Model Output Error", "Try again or switch models.", False)
    return ("Pipeline Error", "Check the traceback for details.", False)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_FILLER_WORDS = {
    "a", "an", "the", "with", "and", "or", "for", "to", "of", "in", "on",
    "by", "from", "that", "this", "is", "it", "my", "using", "circuit",
    "board", "pcb", "design", "please", "make", "me", "create", "build",
}


def _slugify(text: str, max_len: int = 24) -> str:
    """Convert description to a compact project slug (max 24 chars, whole words)."""
    words = re.sub(r"[^a-z0-9\s]", "", text.lower().strip()).split()
    words = [w for w in words if w not in _FILLER_WORDS]
    parts: list[str] = []
    length = 0
    for w in words:
        added = len(w) + (1 if parts else 0)  # underscore separator
        if length + added > max_len:
            break
        parts.append(w)
        length += added
    return "_".join(parts) or "pcb_project"


def _wrap_viewer_iframe(html: str) -> str:
    """Wrap viewer HTML in an iframe so JavaScript (tooltips, pan/zoom) executes.

    Gradio 6 sanitizes inline <script> tags in gr.HTML for security.
    Using srcdoc iframe preserves the full interactive viewer.
    The wrapper div with explicit height ensures Gradio doesn't collapse it.
    """
    import html as html_mod
    escaped = html_mod.escape(html)
    return (
        f'<div style="width:100%;height:75vh;min-height:500px;">'
        f'<iframe srcdoc="{escaped}" '
        f'style="width:100%;height:100%;border:none;border-radius:8px;" '
        f'sandbox="allow-scripts allow-same-origin"></iframe>'
        f'</div>'
    )


def _build_config(api_key, api_base, model, max_tokens, base_dir):
    config = OrchestratorConfig.from_env(base_dir=base_dir)
    if model and str(model).strip():
        m = str(model).strip()
        config.generate_model = m
        config.review_model = m
        config.gather_model = m
    if api_base and str(api_base).strip():
        config.api_base = str(api_base).strip()
    if api_key and str(api_key).strip():
        config.api_key = str(api_key).strip()
    if max_tokens:
        config.max_tokens = int(max_tokens)
    config.agent_mode = True
    # Disable extended thinking/reasoning for all supported model formats.
    # Different providers use different parameter names.
    config.llm_extra_body["thinking"] = False
    config.llm_extra_body["reasoning_effort"] = "none"
    config.llm_extra_body["reasoning"] = {"effort": "none"}
    return config


def _out(status="", state=None,
         chat_display=None, input_group=None, chat_input=None,
         submit_update=None, approve_update=None,
         steps_update=None, viewer_update=None, settings_update=None,
         export_row_update=None):
    """Build the standard output tuple. gr.update() for anything not specified."""
    return (
        status if status else gr.update(),
        state if state is not None else gr.update(),
        chat_display if chat_display is not None else gr.update(),
        input_group if input_group is not None else gr.update(),
        chat_input if chat_input is not None else gr.update(),
        submit_update if submit_update is not None else gr.update(),
        approve_update if approve_update is not None else gr.update(),
        steps_update if steps_update is not None else gr.update(),
        viewer_update if viewer_update is not None else gr.update(),
        settings_update if settings_update is not None else gr.update(),
        export_row_update if export_row_update is not None else gr.update(),
    )


# ---------------------------------------------------------------------------
# Phase 1: Submit / Send Feedback
# ---------------------------------------------------------------------------

def _handle_submit(
    user_text, uploaded_files,
    api_key, api_base, model, max_tokens,
    state, base_dir,
):
    """Generator: translate or re-translate with feedback. Yields output tuples."""
    phase = state.get("phase", "input")
    messages = state.get("messages", [])
    requirements = state.get("requirements")
    original_description = state.get("description", "")

    if not user_text or not str(user_text).strip():
        yield _out(status="Please enter text.", state=state)
        return

    user_text = str(user_text).strip()

    # --- Immediately disable button and show "working" status ---
    yield _out(
        status="Translating circuit description...",
        state=state,
        submit_update=gr.update(interactive=False, value="Translating..."),
        viewer_update=(
            '<div class="pcb-spinner">'
            '<span>Analyzing your circuit description...</span></div>'
        ),
    )

    try:
        config = _build_config(api_key, api_base, model, max_tokens, base_dir)
    except Exception as exc:
        yield _out(
            status=f"Config error: {exc}", state=state,
            submit_update=gr.update(interactive=True, value="Design PCB"),
            viewer_update=_render_error_html("Configuration Error", str(exc)),
            settings_update=gr.update(open=True),
        )
        return

    from .cache import ComponentCache
    from .llm.litellm_client import LiteLLMClient
    from .prompts.builder import PromptBuilder
    from .gather.conversation import RequirementsGatherer
    from .gather.schema import coerce_requirements_types, validate_requirements
    from optimizers.pad_geometry import configure_lookup

    llm = LiteLLMClient(
        config.gather_model, api_base=config.api_base,
        api_key=config.api_key, extra_body=config.llm_extra_body,
        timeout=600,  # 10 min for gather/translate calls
    )
    cache = ComponentCache(config.component_cache_path)

    # Build KiCad library index if path is configured
    kicad_index = None
    if config.kicad_library_path:
        from exporters.kicad_mod_parser import KiCadLibraryIndex
        kicad_index = KiCadLibraryIndex(config.kicad_library_path)

    # Set module-level defaults so all build_pad_map() calls benefit
    configure_lookup(kicad_index=kicad_index, cache=cache)

    gatherer = RequirementsGatherer(
        llm, PromptBuilder(config.base_dir),
        cache=cache, max_workers=config.llm_enrichment_workers,
    )

    messages.append({"role": "user", "content": user_text})

    plan_context = state.get("plan_context")
    feedback = None
    prev_json = None

    if phase == "input":
        original_description = user_text

        # --- Planning step: first round of multi-turn conversation ---
        messages.append({"role": "system", "content": "\u23f3 Analyzing your circuit..."})
        yield _out(
            chat_display=_render_chat_markdown(messages),
            status="Planning circuit design...",
            state=state,
            # Immediately clear + disable the input while planning runs so the
            # Circuit Description box doesn't linger with the original text.
            chat_input=gr.update(
                value="", interactive=False, label="Thinking...",
                placeholder="",
            ),
            submit_update=gr.update(interactive=False, value="Thinking..."),
            approve_update=gr.update(interactive=False),
        )

        planning_history: list[dict] = []
        already_looked_up: set[str] = set()

        try:
            logger.info("Running planning step (round 1)...")
            plan, planning_history = gatherer.plan_conversation_round(
                user_text, user_text, planning_history, already_looked_up,
            )
        except Exception as exc:
            logger.warning("Planning step failed: %s", exc)
            plan = None

        if plan:
            plan_msg = _render_plan_message(plan)
            messages[-1] = {"role": "assistant", "content": plan_msg}

            state.update(
                phase="planning",
                description=original_description,
                messages=messages,
                plan=plan,
                planning_history=planning_history,
                already_looked_up=list(already_looked_up),
            )
            yield _out(
                chat_display=_render_chat_markdown(messages),
                status="Review the plan. Type corrections or answer questions, then click Send. "
                       "Click Proceed to Design when ready.",
                state=state,
                chat_input=gr.update(
                    value="", label="Chat", interactive=True,
                    placeholder="Answer questions, suggest changes, or type 'proceed'...",
                    lines=2,
                ),
                submit_update=gr.update(interactive=True, value="Send"),
                approve_update=gr.update(
                    interactive=True, visible=True,
                    value="\u27a1\ufe0f Proceed to Design",
                ),
            )
            return
        else:
            # Plan failed — fall through to translate without plan context
            messages[-1] = {"role": "system",
                            "content": "\u23f3 Translating..."}

    elif phase == "planning":
        original_description = state.get("description", "")
        plan = state.get("plan", {})
        planning_history = state.get("planning_history", [])
        already_looked_up = set(state.get("already_looked_up", []))

        if gatherer._is_proceed_signal(user_text):
            # Done planning — format context and proceed to translate
            answers = gatherer._collect_default_answers(plan)
            plan_context = gatherer.format_plan_context(plan, answers)
            messages.append({"role": "system", "content": "\u23f3 Translating..."})
        else:
            # Another planning round — user is correcting/adding info
            messages.append({"role": "system", "content": "\u23f3 Updating design..."})
            yield _out(
                chat_display=_render_chat_markdown(messages),
                status="Updating design plan...",
                state=state,
                submit_update=gr.update(interactive=False, value="Thinking..."),
                approve_update=gr.update(interactive=False),
            )

            logger.info("Planning round (correction)...")
            new_plan, planning_history = gatherer.plan_conversation_round(
                original_description, user_text, planning_history, already_looked_up,
            )
            if new_plan is not None:
                plan = new_plan

            plan_msg = _render_plan_message(plan)
            messages[-1] = {"role": "assistant", "content": plan_msg}

            state.update(
                phase="planning",
                plan=plan,
                planning_history=planning_history,
                already_looked_up=list(already_looked_up),
                messages=messages,
            )
            yield _out(
                chat_display=_render_chat_markdown(messages),
                status="Review the updated plan. Type more corrections or click Proceed to Design.",
                state=state,
                chat_input=gr.update(
                    value="", label="Chat", interactive=True,
                    placeholder="Answer questions, suggest changes, or type 'proceed'...",
                    lines=2,
                ),
                submit_update=gr.update(interactive=True, value="Send"),
                approve_update=gr.update(
                    interactive=True, visible=True,
                    value="\u27a1\ufe0f Proceed to Design",
                ),
            )
            return

    elif phase == "review":
        feedback = user_text
        prev_json = json.dumps(requirements, indent=2) if requirements else None

    # Preserve previous enriched specs to avoid redundant LLM lookups
    prev_specs_by_ref: dict[str, dict] = {}
    if requirements:
        for comp in requirements.get("components", []):
            ref = comp.get("ref", "")
            if ref and comp.get("specs"):
                prev_specs_by_ref[ref] = dict(comp["specs"])

    # Ensure there's a "Translating..." status in the chat
    messages_has_translating = any(
        m.get("content", "").startswith("\u23f3 Translating")
        for m in messages[-2:]
    )
    if not messages_has_translating:
        messages.append({"role": "system", "content": "\u23f3 Translating..."})

    yield _out(
        chat_display=_render_chat_markdown(messages),
        status="Translating circuit description...",
        state=state,
        submit_update=gr.update(interactive=False, value="Translating..."),
        approve_update=gr.update(interactive=False, visible=False),
    )

    try:
        logger.info("Calling translate (feedback=%s, plan_context=%s)",
                     bool(feedback), bool(plan_context))
        new_requirements = gatherer.translate(
            original_description, feedback, prev_json,
            plan_context=plan_context,
        )
        if new_requirements is not None:
            new_requirements = coerce_requirements_types(new_requirements)

        if new_requirements is None:
            messages[-1] = {"role": "system",
                            "content": "\u274c Translation failed."}
            state.update(messages=messages)
            yield _out(
                chat_display=_render_chat_markdown(messages),
                status="Translation failed. Try rephrasing.",
                state=state,
                submit_update=gr.update(interactive=True, value="Design PCB"),
                viewer_update=_render_error_html(
                    "Translation Failed",
                    "The model could not parse your description."),
                settings_update=gr.update(open=True),
            )
            return

        requirements = new_requirements

        # Auto-fix validation errors
        logger.info("Validating requirements...")
        errors = validate_requirements(requirements)
        if errors:
            logger.info("Validation errors, retranslating: %s", errors[:3])
            yield _out(status="Fixing validation errors...", state=state)
            err_fb = "Validation errors:\n" + "\n".join(f"- {e}" for e in errors)
            requirements = gatherer.translate(
                original_description,
                f"{feedback or ''}\n\n{err_fb}".strip(),
                json.dumps(requirements, indent=2),
            )
            if requirements is not None:
                requirements = coerce_requirements_types(requirements)

        # Merge back previously enriched specs so we don't re-lookup
        if requirements and prev_specs_by_ref:
            for comp in requirements.get("components", []):
                ref = comp.get("ref", "")
                prev = prev_specs_by_ref.get(ref, {})
                if prev:
                    specs = comp.get("specs", {})
                    for k, v in prev.items():
                        if k not in specs:
                            specs[k] = v
                    comp["specs"] = specs

        # Enrich (with progress updates)
        if requirements:
            yield _out(
                status="Looking up component specs...",
                state=state,
            )
            logger.info("Enriching component specs...")
            requirements = gatherer._enrich_component_specs(requirements)

            yield _out(
                status="Looking up footprint dimensions...",
                state=state,
            )
            logger.info("Enriching footprints...")
            requirements = gatherer._enrich_footprints(requirements)

            yield _out(
                status="Generating summary for review...",
                state=state,
            )
            logger.info("Generating summary...")
            summary = gatherer.summarize(requirements)
        else:
            summary = "(Failed to generate requirements)"

        messages[-1] = {"role": "assistant", "content": summary}
        messages.append({
            "role": "system",
            "content": "\u2705 Review above. Click **Approve & Start Pipeline**, "
                       "or type corrections and click **Send Feedback**.",
        })

    except Exception as exc:
        tb = traceback.format_exc()
        logger.error("Translation error: %s\n%s", exc, tb)
        title, hint, open_settings = _categorize_error(exc, tb)
        messages[-1] = {"role": "system", "content": f"\u274c {title}: {exc}"}
        state.update(messages=messages)
        yield _out(
            chat_display=_render_chat_markdown(messages),
            status=f"{title}: {exc}",
            state=state,
            submit_update=gr.update(interactive=True, value="Design PCB"),
            viewer_update=_render_error_html(title, str(exc), tb, hint),
            settings_update=gr.update(open=open_settings),
        )
        return

    state.update(
        phase="review", messages=messages,
        requirements=requirements, description=original_description,
    )
    yield _out(
        status="Review the summary. Approve or send feedback.",
        state=state,
        chat_display=_render_chat_markdown(messages),
        chat_input=gr.update(
            value="",
            placeholder="Type corrections or additional details...",
        ),
        submit_update=gr.update(value="Send Feedback", interactive=True),
        approve_update=gr.update(interactive=True, visible=True),
    )


# ---------------------------------------------------------------------------
# Phase 2: Approve & Run Pipeline (generator)
# ---------------------------------------------------------------------------

def _approve_and_run(
    uploaded_files, api_key, api_base, model, max_tokens,
    state, base_dir,
):
    phase = state.get("phase", "")

    # If clicked during planning, treat as "proceed" → delegate to submit flow
    if phase == "planning":
        yield from _handle_submit(
            "proceed", uploaded_files,
            api_key, api_base, model, max_tokens,
            state, base_dir,
        )
        return

    messages = state.get("messages", [])
    requirements = state.get("requirements")
    description = state.get("description", "")

    if not requirements:
        messages.append({"role": "system", "content": "\u274c No requirements."})
        state.update(messages=messages)
        yield _out(
            chat_display=_render_chat_markdown(messages), status="No requirements.", state=state,
            approve_update=gr.update(interactive=False),
        )
        return

    config = _build_config(api_key, api_base, model, max_tokens, base_dir)
    project_name = _slugify(description)

    # Save requirements
    project_dir = config.resolve(config.projects_dir) / project_name
    project_dir.mkdir(parents=True, exist_ok=True)
    requirements["project_name"] = project_name
    req_path = project_dir / f"{project_name}_requirements_input.json"
    req_path.write_text(json.dumps(requirements, indent=2))

    attach_files = []
    if uploaded_files:
        for f in uploaded_files:
            src = Path(f) if isinstance(f, str) else Path(f.name)
            if src.exists():
                attach_files.append(src)

    messages.append({"role": "system", "content": "\u25b6\ufe0f Starting pipeline..."})
    state.update(phase="running", messages=messages)

    step_statuses: dict[int, str] = {}
    viewer_html = (
        '<div class="pcb-spinner">'
        '<span>Designing your PCB...</span></div>'
    )

    yield _out(
        status="Starting pipeline...", state=state,
        chat_display=_render_chat_markdown(messages),
        input_group=gr.update(visible=False),  # hide input controls
        steps_update=_render_steps(step_statuses),
        viewer_update=viewer_html,
    )

    try:
        gen = run_workflow_streaming(
            req_path, project_name, config,
            attach_files=attach_files or None,
        )

        for event in gen:
            ev = event.get("event")

            if ev == "step_start":
                step_statuses[event["step"]] = "running"
                yield _out(
                    status=f"Running Step {event['step']}: {event['name']}...",
                    state=state,
                    steps_update=_render_steps(step_statuses),
                )

            elif ev == "step_done":
                ok = event.get("success", False)
                step_statuses[event["step"]] = "done" if ok else "failed"
                yield _out(
                    status=f"Step {event['step']}: {event['name']} \u2014 "
                           f"{'complete' if ok else 'FAILED'}",
                    state=state,
                    steps_update=_render_steps(step_statuses),
                )

            elif ev == "viewer_update":
                viewer_html = _wrap_viewer_iframe(event["html"])
                yield _out(
                    status="Board updated.", state=state,
                    steps_update=_render_steps(step_statuses),
                    viewer_update=viewer_html,
                )

            elif ev == "vision_review_start":
                yield _out(
                    status="Vision AI reviewing board...", state=state,
                    steps_update=_render_steps(step_statuses),
                )

            elif ev == "vision_review_done":
                result = event.get("result", "escalated")
                if result == "approved":
                    messages.append({"role": "system",
                                     "content": "\u2705 Vision review: APPROVED"})
                    yield _out(
                        status="Vision review passed.", state=state,
                        chat_display=_render_chat_markdown(messages),
                        steps_update=_render_steps(step_statuses),
                    )
                else:
                    messages.append({"role": "system",
                                     "content": "\u26a0\ufe0f Vision review requested changes. Please review the board and approve manually."})
                    yield _out(
                        status="Vision review escalated \u2014 manual approval needed.",
                        state=state,
                        chat_display=_render_chat_markdown(messages),
                        approve_update=gr.update(interactive=True, visible=True),
                        steps_update=_render_steps(step_statuses),
                    )

            elif ev == "approval_needed":
                viewer_html = _wrap_viewer_iframe(event["html"])
                yield _out(
                    status="Manual approval needed.", state=state,
                    steps_update=_render_steps(step_statuses),
                    viewer_update=viewer_html,
                )

            elif ev == "complete":
                messages.append({"role": "system",
                                 "content": "\u2705 Pipeline complete!"})
                state.update(phase="complete", messages=messages)
                yield _out(
                    status="Pipeline complete! All outputs generated.",
                    state=state,
                    chat_display=_render_chat_markdown(messages),
                    chat_input=gr.update(
                        placeholder="Describe another circuit...",
                        interactive=True, value="",
                    ),
                    input_group=gr.update(visible=True),  # show input for new design
                    submit_update=gr.update(value="Design PCB", interactive=True),
                    approve_update=gr.update(interactive=False, visible=False),
                    steps_update=_render_steps(step_statuses),
                    viewer_update=viewer_html,
                    export_row_update=gr.update(visible=True),
                )

            elif ev == "error":
                s = event.get("step", 0)
                msg = event.get("message", "Unknown error")
                step_statuses[s] = "failed"
                sn = STEP_NAMES.get(s, "?")
                messages.append({"role": "system",
                                 "content": f"\u274c Step {s} ({sn}): {msg}"})
                state.update(messages=messages, phase="input")
                yield _out(
                    status=f"Error at Step {s}: {msg}",
                    state=state,
                    chat_display=_render_chat_markdown(messages),
                    input_group=gr.update(visible=True),  # show input to retry
                    chat_input=gr.update(value="", interactive=True),
                    submit_update=gr.update(value="Design PCB", interactive=True),
                    steps_update=_render_steps(step_statuses),
                    viewer_update=_render_error_html(f"Step {s} Failed: {sn}", msg),
                )
                return

    except Exception as exc:
        tb = traceback.format_exc()
        logger.error("Pipeline error: %s\n%s", exc, tb)
        title, hint, open_settings = _categorize_error(exc, tb)
        messages.append({"role": "system", "content": f"\u274c {title}: {exc}"})
        state.update(messages=messages, phase="input")
        for s, st in step_statuses.items():
            if st == "running":
                step_statuses[s] = "failed"
        yield _out(
            status=f"Pipeline error: {exc}",
            state=state,
            chat_display=_render_chat_markdown(messages),
            input_group=gr.update(visible=True),  # show input to retry
            chat_input=gr.update(value="", interactive=True),
            submit_update=gr.update(value="Design PCB", interactive=True),
            steps_update=_render_steps(step_statuses),
            viewer_update=_render_error_html(title, str(exc), tb, hint),
            settings_update=gr.update(open=open_settings),
        )


# ---------------------------------------------------------------------------
# Export / Import KiCad handlers
# ---------------------------------------------------------------------------

def _export_kicad(base_dir: Path):
    import sys
    sys.path.insert(0, str(base_dir))
    projects_dir = base_dir / "projects"
    if not projects_dir.exists():
        return "No projects found."
    routed_files = sorted(
        projects_dir.glob("*/*_routed.json"),
        key=lambda p: p.stat().st_mtime, reverse=True,
    )
    if not routed_files:
        return "No routed board found."
    routed_path = routed_files[0]
    project_name = routed_path.parent.name
    netlist_path = routed_path.parent / f"{project_name}_netlist.json"
    if not netlist_path.exists():
        return f"Netlist not found for {project_name}."
    try:
        from exporters.kicad_exporter import export_kicad_pcb
        routed = json.loads(routed_path.read_text())
        netlist = json.loads(netlist_path.read_text())
        output_path = routed_path.parent / f"{project_name}.kicad_pcb"
        export_kicad_pcb(routed, netlist, output_path)
        return f"\u2705 Exported: {output_path}"
    except Exception as exc:
        logger.error("KiCad export error: %s\n%s", exc, traceback.format_exc())
        return f"\u274c Export error: {exc}"


def _import_kicad(kicad_file, base_dir: Path):
    import sys
    sys.path.insert(0, str(base_dir))
    if kicad_file is None:
        return gr.update(), "Upload a .kicad_pcb file first."
    kicad_path = Path(kicad_file) if isinstance(kicad_file, str) else Path(kicad_file.name)
    projects_dir = base_dir / "projects"
    routed_files = sorted(
        projects_dir.glob("*/*_routed.json"),
        key=lambda p: p.stat().st_mtime, reverse=True,
    )
    if not routed_files:
        return gr.update(), "No routed board found."
    routed_path = routed_files[0]
    project_name = routed_path.parent.name
    netlist_path = routed_path.parent / f"{project_name}_netlist.json"
    bom_path = routed_path.parent / f"{project_name}_bom.json"
    try:
        from exporters.kicad_importer import import_kicad_pcb
        from visualizers.placement_viewer import generate_html
        original_routed = json.loads(routed_path.read_text())
        netlist = json.loads(netlist_path.read_text())
        bom = json.loads(bom_path.read_text()) if bom_path.exists() else None
        imported = import_kicad_pcb(kicad_path, original_routed, netlist)
        routed_path.write_text(json.dumps(imported, indent=2))
        html = generate_html(imported, netlist, bom, routed=imported, embed_mode=True)
        stats = imported.get("routing", {}).get("statistics", {})
        return _wrap_viewer_iframe(html), f"\u2705 Imported: {stats.get('routed_nets',0)}/{stats.get('total_nets',0)} nets"
    except Exception as exc:
        logger.error("KiCad import error: %s\n%s", exc, traceback.format_exc())
        return gr.update(), f"\u274c Import error: {exc}"


# ---------------------------------------------------------------------------
# Build & launch Gradio app
# ---------------------------------------------------------------------------

def launch_gui(base_dir: Path | None = None, port: int = 7860,
               share: bool = False) -> None:
    if base_dir is None:
        base_dir = Path.cwd()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    custom_css = """
    .step-row { padding: 4px 10px; border-radius: 4px; margin: 2px 0; color: #fff; }
    .step-pending { background: #374151; color: #9ca3af; }
    .step-running { background: #92400e; color: #fef3c7; }
    .step-done { background: #065f46; color: #d1fae5; }
    .step-failed { background: #991b1b; color: #fecaca; }
    .step-icon { margin-right: 8px; }

    /* Primary buttons — warm orange */
    button.primary {
        background: #e8751a !important; border-color: #d4680f !important;
        color: #fff !important;
    }
    button.primary:hover { background: #d4680f !important; }
    button.primary:disabled, button.primary[disabled] {
        background: #5a4a3a !important; border-color: #4a3d30 !important;
        color: #8a8078 !important;
    }
    button.secondary {
        background: #c06a1a !important; border-color: #a85c16 !important;
        color: #fff !important;
    }
    button.secondary:hover { background: #a85c16 !important; }
    button.secondary:disabled, button.secondary[disabled] {
        background: #4a3d30 !important; border-color: #3d3228 !important;
        color: #8a8078 !important;
    }
    #toast-status textarea { font-weight: 600 !important; }

    /* Spinner overlay for board viewer during pipeline */
    @keyframes pcb-spin { to { transform: rotate(360deg); } }
    .pcb-spinner {
        display: flex; align-items: center; justify-content: center;
        height: 200px; color: #e8751a;
    }
    .pcb-spinner::before {
        content: ""; width: 40px; height: 40px;
        border: 4px solid #374151; border-top-color: #e8751a;
        border-radius: 50%; animation: pcb-spin 0.8s linear infinite;
        margin-right: 12px;
    }

    /* Make board viewer fill available height */
    #board-viewer { min-height: 80vh; }
    #board-viewer > div { height: 100%; }
    #board-viewer iframe, #board-viewer > div > div {
        width: 100% !important; height: 100% !important;
    }

    /* Scrollable chat display */
    #chat-display {
        max-height: 50vh;
        overflow-y: auto;
        padding: 12px;
        border: 1px solid #374151;
        border-radius: 8px;
        background: #1e1e2e;
        font-size: 14px;
        line-height: 1.6;
    }
    #chat-display table {
        border-collapse: collapse;
        width: 100%;
        font-size: 13px;
    }
    #chat-display th, #chat-display td {
        border: 1px solid #444;
        padding: 4px 8px;
        text-align: left;
    }
    #chat-display th { background: #2a2a3e; }
    """

    with gr.Blocks(title="PCB Creator") as app:
        gr.Markdown("# PCB Creator")
        gr.Markdown("Describe your circuit, attach reference files, "
                     "and watch the AI design your PCB.")

        gui_state = gr.State({
            "phase": "input",
            "messages": [],
            "requirements": None,
            "description": "",
        })

        with gr.Row():
            # --- Left column ---
            with gr.Column(scale=3):
                # Scrollable conversation display (rich markdown)
                chat_display = gr.Markdown(
                    value="",
                    elem_id="chat-display",
                )
                # --- Input controls (hidden once pipeline starts) ---
                input_group = gr.Column(visible=True)
                with input_group:
                    chat_input = gr.Textbox(
                        label="Circuit Description",
                        placeholder="e.g. LED blink circuit with ATtiny85, "
                                    "3 LEDs (red, green, blue), powered by USB 5V...",
                        lines=4,
                        max_lines=8,
                    )
                    file_upload = gr.File(
                        label="Attach Files (drag & drop)",
                        file_count="multiple",
                        file_types=[".png", ".jpg", ".jpeg", ".gif", ".bmp", ".svg",
                                    ".pdf", ".dxf", ".txt", ".json", ".md"],
                        height=160,
                    )
                    with gr.Row():
                        submit_btn = gr.Button(
                            "Design PCB", variant="primary", size="lg",
                        )
                        approve_btn = gr.Button(
                            "\u2705 Approve & Start Pipeline",
                            variant="primary", size="lg",
                            interactive=False, visible=False,
                        )

                gr.Markdown("### Pipeline Progress")
                steps_html = gr.HTML(value=_render_steps({}))
                status_text = gr.Textbox(
                    label="Status", interactive=False, elem_id="toast-status",
                    value="Ready. Enter a circuit description and click Design PCB.",
                )

            # --- Right column: board viewer ---
            with gr.Column(scale=7):
                viewer = gr.HTML(
                    value=(
                        "<div style='height:80vh;display:flex;align-items:center;"
                        "justify-content:center;color:#888;border:1px dashed #ccc;"
                        "border-radius:8px;'>"
                        "<p>Board visualization will appear here "
                        "after the layout step completes.</p></div>"
                    ),
                    label="Board Viewer",
                    elem_id="board-viewer",
                )

        # --- Export/Import (hidden until pipeline completes) ---
        export_row = gr.Row(visible=False)
        with export_row:
            export_btn = gr.Button("Export KiCad (.kicad_pcb)")
            import_file = gr.File(
                label="Import KiCad", file_count="single",
                file_types=[".kicad_pcb"],
            )

        # --- Settings accordion ---
        settings_accordion = gr.Accordion("AI Model Settings", open=False)
        with settings_accordion:
            with gr.Row():
                provider_dropdown = gr.Dropdown(
                    label="Provider",
                    choices=list(PROVIDER_PRESETS.keys()),
                    value="OpenRouter",
                )
                _env_key_hint = (
                    " (PCB_LLM_API_KEY found in .env)"
                    if os.environ.get("PCB_LLM_API_KEY") else ""
                )
                api_key_input = gr.Textbox(
                    label=f"API Key{_env_key_hint}",
                    type="password",
                    placeholder="Leave empty to use PCB_LLM_API_KEY from .env",
                )
            with gr.Row():
                api_base_input = gr.Textbox(
                    label="API Base URL",
                    placeholder="Leave empty for default", value="",
                )
                model_input = gr.Textbox(
                    label="Model Name",
                    value="openrouter/x-ai/grok-4.1-fast",
                )
                max_tokens_input = gr.Number(
                    label="Max Tokens", value=32768, precision=0,
                )

        # ---------------------------------------------------------------
        # Event wiring
        # ---------------------------------------------------------------

        _all_outputs = [
            status_text, gui_state,
            chat_display, input_group, chat_input, submit_btn, approve_btn,
            steps_html, viewer, settings_accordion,
            export_row,
        ]

        provider_dropdown.change(
            fn=_apply_preset,
            inputs=[provider_dropdown],
            outputs=[api_base_input, model_input, max_tokens_input],
        )

        # Submit / Send Feedback (not a generator — no spinner)
        submit_btn.click(
            fn=functools.partial(_handle_submit, base_dir=base_dir),
            inputs=[chat_input, file_upload,
                    api_key_input, api_base_input, model_input, max_tokens_input,
                    gui_state],
            outputs=_all_outputs,
            show_progress="hidden",
        )

        # Approve & run pipeline (generator — shows progress via steps panel)
        approve_btn.click(
            fn=functools.partial(_approve_and_run, base_dir=base_dir),
            inputs=[file_upload,
                    api_key_input, api_base_input, model_input, max_tokens_input,
                    gui_state],
            outputs=_all_outputs,
            show_progress="hidden",
        )

        # Export KiCad
        export_btn.click(
            fn=lambda: _export_kicad(base_dir),
            outputs=[status_text],
            show_progress="hidden",
        )

        # Import KiCad
        import_file.change(
            fn=lambda f: _import_kicad(f, base_dir),
            inputs=[import_file],
            outputs=[viewer, status_text],
            show_progress="hidden",
        )

    app.queue()
    app.launch(
        server_port=port, share=share,
        theme=gr.themes.Soft(), css=custom_css,
    )
