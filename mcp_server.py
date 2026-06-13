#!/usr/bin/env python3
"""MCP server for PCB-Creator.

Exposes the AI-driven PCB design pipeline as MCP tools so any AI agent
can design PCBs programmatically. Runs headless with vision-based approval.

Usage:
    python mcp_server.py                  # stdio transport (default)
    pcb-creator-mcp                       # via installed entry point

Configuration (environment variables):
    PCB_PROJECTS_DIR    — Persistent projects directory (default: ~/.pcb-creator/projects/)
    PCB_LLM_API_KEY         — LLM API key
    PCB_LLM_API_BASE        — LLM API base URL
    PCB_GENERATE_MODEL  — Model for generation steps
    PCB_VISION_MODEL    — Model for vision-based board review
    PCB_ROUTER_ENGINE   — "freerouting" (default) or "builtin"
"""

from __future__ import annotations

import base64
import json
import logging
import os
import re
import sys
import threading
from pathlib import Path

from fastmcp import FastMCP

# Ensure the repo root is on sys.path so orchestrator/ imports work
_repo_root = Path(__file__).resolve().parent
if str(_repo_root) not in sys.path:
    sys.path.insert(0, str(_repo_root))

from orchestrator.config import OrchestratorConfig
from mcp_envelope import ok, fail, working, next_step, option

logger = logging.getLogger(__name__)

mcp = FastMCP(
    "pcb-creator",
    instructions=(
        "PCB design tools. Call get_workflow_guide() FIRST to see the exact tool "
        "order for the three workflows: (a) build a circuit from scratch with "
        "create_circuit/add_component/connect_pins, (b) import an existing KiCad "
        "netlist with import_kicad_netlist, or (c) one-shot autonomous design_pcb. "
        "Every tool response includes 'next_step' (the call to make next) and, on "
        "failure, 'remediation' (concrete recovery options). Long operations "
        "(design_pcb, route_board) return immediately and run in the background — "
        "poll get_project_status until done; its 'status_hint' tells you what is "
        "happening. Never fall back to external CAD tools; every fix can be made "
        "through these tools."
    ),
)

# In-memory routing job registry (project_name -> job dict).  route_board runs
# routing on a background thread so the MCP call returns immediately; clients
# poll get_project_status for routing_state.  Reconciled with the on-disk
# _routed.json so state survives even if this registry is empty (e.g. restart).
_ROUTE_JOBS: dict[str, dict] = {}
_ROUTE_LOCK = threading.Lock()

# In-memory design job registry (project_name -> job dict).  design_pcb runs the
# full pipeline (requirements → schematic → BOM → placement → routing → DRC →
# outputs) on a background thread so the MCP call returns immediately and never
# hits the client timeout.  Clients poll get_project_status and read
# 'design_state' (running → complete | failed).  Single-flight: a second
# design_pcb for a project already running returns the in-progress job instead of
# launching a duplicate pipeline.  Reconciled with on-disk STATUS.json so a
# respawned server can still report design state.
_DESIGN_JOBS: dict[str, dict] = {}
_DESIGN_LOCK = threading.Lock()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_projects_dir() -> Path:
    """Resolve the persistent projects directory."""
    env = os.environ.get("PCB_PROJECTS_DIR")
    if env:
        p = Path(env).expanduser()
    else:
        p = Path.home() / ".pcb-creator" / "projects"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _get_config() -> OrchestratorConfig:
    """Build config from env vars with MCP-appropriate defaults."""
    config = OrchestratorConfig.from_env(base_dir=_repo_root)
    config.agent_mode = True
    config.skip_qa = True  # Calling agent reviews via get_project_status/get_board_image
    config.max_rework_attempts = 3  # Limit rework loops in MCP mode (agent can retry)
    config.llm_timeout = 300  # 5 min per LLM call (fail fast, agent can retry)
    # Point projects_dir to persistent location
    config.projects_dir = str(_get_projects_dir())
    return config


_LOOKUP_CONFIGURED = False
_LOOKUP_LOCK = threading.Lock()


def _ensure_lookup_configured() -> None:
    """Install the tiered footprint lookup (KiCad library + component cache).

    The CLI and Gradio entry points call ``configure_lookup`` at startup, but the
    MCP server is a separate process — without this, the KiCad-library tier and
    the component cache are disabled and verbose KiCad footprint names fall back
    to placeholders.  Idempotent and thread-safe.
    """
    global _LOOKUP_CONFIGURED
    if _LOOKUP_CONFIGURED:
        return
    with _LOOKUP_LOCK:
        if _LOOKUP_CONFIGURED:
            return
        from optimizers.pad_geometry import configure_lookup
        from orchestrator.cache import ComponentCache

        config = _get_config()
        cache = ComponentCache(config.component_cache_path)

        kicad_index = None
        if config.kicad_library_path:
            try:
                from exporters.kicad_mod_parser import KiCadLibraryIndex
                kicad_index = KiCadLibraryIndex(config.kicad_library_path)
            except Exception:
                kicad_index = None

        configure_lookup(kicad_index=kicad_index, cache=cache)
        _LOOKUP_CONFIGURED = True


def _slugify(text: str) -> str:
    """Convert description to a filesystem-safe project name."""
    slug = re.sub(r"[^a-z0-9]+", "_", text.lower().strip())
    slug = slug.strip("_")[:60]
    return slug or "pcb_project"


def _project_dir(project_name: str) -> Path:
    """Get the project directory path."""
    return _get_projects_dir() / project_name


def _read_project_json(project_name: str, suffix: str) -> dict | None:
    """Read a project JSON file by suffix (e.g. '_drc_report.json')."""
    path = _project_dir(project_name) / f"{project_name}{suffix}"
    if path.exists():
        return json.loads(path.read_text())
    return None


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

@mcp.tool()
def design_pcb(
    description: str,
    project_name: str | None = None,
    requirements_json: dict | None = None,
    settings: dict | None = None,
    attachments: list[dict] | None = None,
) -> dict:
    """Design a complete PCB autonomously from a description (async, one-shot).

    Runs the full LLM pipeline (requirements → schematic → BOM → placement →
    routing → DRC → outputs) on a background thread and returns immediately.
    Poll get_project_status until 'design_state' is 'complete' or 'failed'.
    Calling again while running returns the in-progress job (no duplicates).

    Prefer requirements_json (schema from get_requirements_schema) over plain
    description — it skips LLM translation. Minimal example:

        design_pcb(
            description="LED blinker",
            project_name="led_blinker",
            requirements_json={
                "project_name": "led_blinker",
                "description": "One red LED with resistor on 5V",
                "power": {"voltage": "5V", "source": "external_dc"},
                "board": {"width_mm": 30, "height_mm": 20, "layers": 2},
                "components": [
                    {"ref": "D1", "type": "led", "value": "red", "package": "0805"},
                    {"ref": "R1", "type": "resistor", "value": "330ohm", "package": "0805"},
                    {"ref": "J1", "type": "connector", "value": "2-pin header", "package": "PinHeader-1x2"}
                ],
                "connections": [
                    {"net_name": "VCC", "net_class": "power", "pins": ["J1.1", "R1.1"]},
                    {"net_name": "LED_DRIVE", "pins": ["R1.2", "D1.anode"]},
                    {"net_name": "GND", "net_class": "ground", "pins": ["D1.cathode", "J1.2"]}
                ]
            },
        )

    settings overrides: {"model", "router_engine", "max_rework_attempts",
    "skip_qa"}. attachments: list of {"filename", "content_base64", "type",
    "purpose", "used_by_steps"} (e.g. a "board_outline" DXF for step 3).
    """
    import time as _time

    if not project_name:
        project_name = _slugify(description)

    # Single-flight: don't launch a duplicate pipeline for a project that is
    # already running. A second call returns the in-progress job to poll.
    with _DESIGN_LOCK:
        current = _DESIGN_JOBS.get(project_name)
        if current and current["state"] == "running":
            return working(
                data={"project_name": project_name},
                poll_again_in_s=15,
                status_hint=(
                    "Design already in progress for this project. Poll "
                    "get_project_status and read 'design_state'; do not launch "
                    "another design_pcb."
                ),
            )
        _DESIGN_JOBS[project_name] = {
            "state": "running", "result": None, "error": None,
            "started_at": _time.monotonic(), "progress": None,
        }

    def _on_progress(p: dict) -> None:
        with _DESIGN_LOCK:
            job = _DESIGN_JOBS.get(project_name)
            if job and job["state"] == "running":
                job["progress"] = p

    def _worker() -> None:
        try:
            result = _design_pcb_sync(
                description, project_name, requirements_json, settings,
                attachments, progress_cb=_on_progress,
            )
            state = "complete" if result.get("success") else "failed"
            err = None if result.get("success") else (
                "; ".join(result.get("errors", [])) or "pipeline did not complete"
            )
            with _DESIGN_LOCK:
                started = _DESIGN_JOBS.get(project_name, {}).get("started_at")
                _DESIGN_JOBS[project_name] = {
                    "state": state, "result": result, "error": err,
                    "started_at": started, "progress": None,
                    "elapsed_s": round(_time.monotonic() - started, 1) if started else None,
                }
        except Exception as exc:  # noqa: BLE001 — surface any failure to the poller
            with _DESIGN_LOCK:
                started = _DESIGN_JOBS.get(project_name, {}).get("started_at")
                _DESIGN_JOBS[project_name] = {
                    "state": "failed", "result": None, "error": str(exc),
                    "started_at": started, "progress": None,
                    "elapsed_s": round(_time.monotonic() - started, 1) if started else None,
                }

    threading.Thread(target=_worker, daemon=True).start()

    return working(
        data={
            "project_name": project_name,
            "next_step": next_step(
                "get_project_status", {"project_name": project_name},
                "Poll until 'design_state' is 'complete' or 'failed'; "
                "'design_progress' shows the live step, 'design_result' the "
                "final output.",
            ),
        },
        poll_again_in_s=20,
        status_hint=(
            "Full design pipeline started in the background (this can take "
            "several minutes). Keep polling get_project_status; do not run "
            "other tools or external CLIs for this project while it runs."
        ),
    )


def _design_pcb_sync(
    description: str,
    project_name: str | None = None,
    requirements_json: dict | None = None,
    settings: dict | None = None,
    attachments: list[dict] | None = None,
    progress_cb=None,
) -> dict:
    """Synchronous design pipeline worker (run on a background thread by design_pcb).

    Design a PCB from a circuit description or structured requirements.

    Runs the full pipeline: requirements → schematic → BOM → placement →
    routing → DRC → output generation. Uses vision-based autonomous review.

    Two input modes:
    1. **Structured (preferred for agents):** Pass requirements_json directly —
       skips LLM translation entirely. Call get_requirements_schema() first to
       get the expected format.
    2. **Natural language:** Pass a plain-text description — translated to
       structured requirements via LLM automatically.

    Args:
        description: Circuit description in plain English, or a short summary
            when using requirements_json. Used for project name generation if
            project_name is omitted.

            Example: "A green LED controlled by a pushbutton, powered by 3.3V"
        project_name: Optional project slug. Auto-generated from description if omitted.
        requirements_json: Structured requirements dict matching the schema from
            get_requirements_schema(). When provided, the LLM translation step is
            skipped entirely — faster, cheaper, and more deterministic. Must include
            at minimum: components (list) and connections (list).
        settings: Optional config overrides: {"model": "...", "router_engine": "...",
            "max_rework_attempts": 5, "skip_qa": false}. QA reviews are skipped by
            default in MCP mode; set skip_qa to false to re-enable them.
        attachments: Optional list of file attachments. Each dict has:
            - "filename": Name for the file (e.g., "board_outline.dxf")
            - "content_base64": Base64-encoded file content
            - "type": Attachment type — "board_outline", "sketch", "photo", "datasheet", "other"
            - "purpose": Description of what the file is for
            - "used_by_steps": List of step numbers that use this file (e.g., [3] for layout)

            For DXF board outlines: set type to "board_outline" and used_by_steps
            to [3]. The pipeline will automatically extract the outline polygon and
            board dimensions from the DXF file — you do not need to specify
            width_mm/height_mm. If providing structured JSON requirements, set
            board.outline_type to "dxf".

    Returns:
        Dict with success status, project name, routing stats, DRC summary,
        and list of output file paths.
    """
    import tempfile

    config = _get_config()

    # Apply optional settings overrides
    if settings:
        if "model" in settings:
            config.generate_model = settings["model"]
            config.review_model = settings["model"]
        if "router_engine" in settings:
            config.router_engine = settings["router_engine"]
        if "max_rework_attempts" in settings:
            config.max_rework_attempts = int(settings["max_rework_attempts"])
        if "skip_qa" in settings:
            config.skip_qa = bool(settings["skip_qa"])

    # Generate project name if not provided
    if not project_name:
        project_name = _slugify(description)

    # Resolve requirements: structured JSON (fast path) or NL translation
    from orchestrator.gather.schema import validate_requirements, auto_fix_duplicate_pins

    if requirements_json is not None:
        # Fast path: agent provided structured requirements directly
        requirements = requirements_json
        errors = validate_requirements(requirements)
        if errors:
            requirements, fix_warnings = auto_fix_duplicate_pins(requirements)
            for w in fix_warnings:
                logger.info(f"  MCP auto-fix: {w}")
            remaining = validate_requirements(requirements)
            if remaining:
                return {
                    "success": False,
                    "errors": [f"Requirements validation failed: {e}" for e in remaining],
                }
    else:
        # Try parsing description as JSON; fall back to LLM translation
        try:
            requirements = json.loads(description)
        except (json.JSONDecodeError, TypeError):
            from orchestrator.gather.conversation import RequirementsGatherer
            from orchestrator.llm.litellm_client import LiteLLMClient
            from orchestrator.prompts.builder import PromptBuilder
            _llm = LiteLLMClient(
                config.generate_model,
                api_base=config.api_base,
                api_key=config.api_key,
                extra_body=config.llm_extra_body,
                timeout=config.llm_timeout,
            )
            _gatherer = RequirementsGatherer(_llm, PromptBuilder(config.base_dir))

            # Translate with validation + rework loop
            requirements = _gatherer.translate(description)
            if requirements is not None:
                for _retry in range(3):
                    errors = validate_requirements(requirements)
                    if not errors:
                        break
                    logger.info(f"  MCP translate: {len(errors)} validation errors, retrying...")
                    requirements = _gatherer.translate(
                        description,
                        feedback="Fix these validation errors:\n" + "\n".join(
                            f"- {e}" for e in errors
                        ),
                        previous_json=json.dumps(requirements, indent=2),
                    )
                    if requirements is None:
                        break

                # Last resort: auto-fix duplicate pins
                if requirements is not None:
                    errors = validate_requirements(requirements)
                    if errors:
                        requirements, fix_warnings = auto_fix_duplicate_pins(requirements)
                        for w in fix_warnings:
                            logger.info(f"  MCP auto-fix: {w}")
                        remaining = validate_requirements(requirements)
                        if remaining:
                            logger.info(f"  MCP auto-fix: {len(remaining)} errors remain")

            if requirements is None:
                return {
                    "success": False,
                    "errors": ["Failed to translate natural language to requirements JSON"],
                }

    projects_dir = _get_projects_dir()
    project_dir = projects_dir / project_name
    project_dir.mkdir(parents=True, exist_ok=True)

    # Handle file attachments
    if attachments:
        att_metadata = []
        for att in attachments:
            filename = att.get("filename", "attachment")
            content_b64 = att.get("content_base64", "")
            att_type = att.get("type", "other")
            purpose = att.get("purpose", "")
            used_by = att.get("used_by_steps", [3])

            # Write file to project directory
            file_path = project_dir / filename
            file_path.write_bytes(base64.b64decode(content_b64))

            att_metadata.append({
                "filename": filename,
                "type": att_type,
                "purpose": purpose,
                "used_by_steps": used_by,
            })

        # Merge attachment metadata into requirements
        existing_atts = requirements.get("attachments", [])
        existing_atts.extend(att_metadata)
        requirements["attachments"] = existing_atts

    req_path = project_dir / f"{project_name}_requirements_input.json"
    req_path.write_text(json.dumps(requirements, indent=2))

    # Run the streaming pipeline, collecting events
    from orchestrator.runner import run_workflow_streaming

    steps_completed = []
    errors = []
    last_event = None

    try:
        for event in run_workflow_streaming(
            req_path, project_name, config, progress_callback=progress_cb,
        ):
            ev = event.get("event", "")
            if ev == "step_done":
                steps_completed.append({
                    "step": event.get("step"),
                    "name": event.get("name"),
                    "success": event.get("success", False),
                })
                if progress_cb is not None:
                    progress_cb({
                        "phase": "pipeline",
                        "step": event.get("step"),
                        "name": event.get("name"),
                        "steps_done": len(steps_completed),
                    })
            elif ev == "error":
                errors.append(event.get("message", "Unknown error"))
            elif ev == "approval_needed":
                # In MCP mode with agent_mode=True, this means vision review
                # escalated. We can't do human approval in MCP, so continue.
                pass
            last_event = event
    except Exception as exc:
        errors.append(f"Pipeline crashed: {exc}")
        try:
            from orchestrator.project import ProjectManager as _PM
            _proj = _PM(project_name, projects_dir)
            _proj.update_status(-1, "ERROR")
        except Exception:
            pass

    success = last_event and last_event.get("event") == "complete" and last_event.get("success", False)

    # Annotate steps_completed with validator errors from STATUS.json
    status_data: dict = {}
    try:
        status_path = project_dir / "STATUS.json"
        if status_path.exists():
            status_data = json.loads(status_path.read_text())
    except Exception:
        pass
    step_status = status_data.get("steps", {})
    for step_entry in steps_completed:
        skey = str(step_entry["step"])
        if skey in step_status:
            v_errs = step_status[skey].get("validator_errors")
            v_warns = step_status[skey].get("validator_warnings")
            if v_errs:
                step_entry["validator_errors"] = v_errs
            if v_warns:
                step_entry["validator_warnings"] = v_warns

    # Gather output info
    result = {
        "success": success,
        "project_name": project_name,
        "project_dir": str(project_dir),
        "steps_completed": steps_completed,
        "errors": errors,
    }

    # Add routing stats if available
    routed = _read_project_json(project_name, "_routed.json")
    if routed:
        stats = routed.get("statistics", {})
        result["routing_stats"] = {
            "completion_pct": stats.get("completion_pct", 0),
            "total_nets": stats.get("total_nets", 0),
            "routed_nets": stats.get("routed_nets", 0),
            "via_count": stats.get("via_count", 0),
            "trace_length_mm": stats.get("total_trace_length_mm", 0),
        }

    # Add DRC summary
    drc = _read_project_json(project_name, "_drc_report.json")
    if drc:
        result["drc_summary"] = {
            "passed": drc.get("passed", False),
            "summary": drc.get("summary", ""),
            "errors": drc.get("statistics", {}).get("errors", 0),
            "warnings": drc.get("statistics", {}).get("warnings", 0),
        }

    # List output files
    output_dir = project_dir / "output"
    if output_dir.exists():
        result["output_files"] = [
            str(f.relative_to(project_dir)) for f in sorted(output_dir.iterdir())
            if f.is_file()
        ]

    return result


@mcp.tool()
def get_requirements_schema() -> dict:
    """Get the JSON schema for structured PCB requirements.

    Returns the full JSON Schema (Draft-7) that describes the format expected
    by design_pcb's requirements_json parameter. Call this once to understand
    the structure, then pass conforming dicts to design_pcb directly — no LLM
    translation needed.

    Key top-level fields: project_name, description, power, components,
    connections, board, manufacturing, placement_hints, calculations.
    """
    from orchestrator.gather.schema import REQUIREMENTS_SCHEMA
    return REQUIREMENTS_SCHEMA


@mcp.tool()
def get_workflow_guide() -> dict:
    """Get the step-by-step tool order for each PCB design workflow.

    Call this first if you are unsure which tool to use. Returns three
    workflows; pick ONE and follow its steps in order. Each step lists the
    tool, an args template, what to wait for, and what to do on failure.
    """
    poll_routing = {
        "then_poll": "get_project_status",
        "wait_for": "routing_state == 'complete' (poll every ~15s; "
                    "'routing_progress' and 'status_hint' show live progress)",
        "on_failure": "Read 'routing_error'; re-run optimize_placement with a "
                      "larger board, then route_board again.",
    }
    return {
        "workflows": {
            "build_from_scratch": {
                "when": "You are designing a new circuit and can describe its "
                        "components and connections.",
                "steps": [
                    {"order": 1, "tool": "create_circuit",
                     "args_template": {"project_name": "my_board",
                                       "description": "...",
                                       "board_width_mm": 50, "board_height_mm": 40}},
                    {"order": 2, "tool": "add_component",
                     "args_template": {"project_name": "my_board",
                                       "designator": "U1", "component_type": "ic",
                                       "value": "NE555", "package": "DIP-8"},
                     "note": "Repeat per component. The response lists the pins "
                             "you can connect."},
                    {"order": 3, "tool": "connect_pins",
                     "args_template": {"project_name": "my_board",
                                       "net_name": "VCC",
                                       "pins": ["U1.8", "C1.1"]},
                     "note": "Repeat per net. Unknown pins return the valid pin "
                             "list."},
                    {"order": 4, "tool": "finalize_circuit",
                     "args_template": {"project_name": "my_board"},
                     "on_failure": "Fix the reported issues with "
                                   "connect_pins/remove_component, then re-run."},
                    {"order": 5, "tool": "place_component",
                     "args_template": {"project_name": "my_board",
                                       "designator": "J1", "x_mm": 2.5,
                                       "y_mm": 20, "rotation_deg": 90},
                     "note": "OPTIONAL — only for components that must sit at "
                             "exact coordinates (edge connectors, mounting "
                             "holes). Validated immediately; pinned parts are "
                             "never moved."},
                    {"order": 6, "tool": "optimize_placement",
                     "args_template": {"project_name": "my_board",
                                       "board_width_mm": 50,
                                       "board_height_mm": 40},
                     "on_failure": "If 'violations' lists pinned components, "
                                   "adjust them with place_component / "
                                   "unplace_component; otherwise enlarge the "
                                   "board and re-run."},
                    {"order": 7, "tool": "route_board",
                     "args_template": {"project_name": "my_board"}, **poll_routing},
                    {"order": 8, "tool": "run_drc",
                     "args_template": {"project_name": "my_board"},
                     "on_failure": "Review violations; re-place on a larger "
                                   "board or re-route, then re-run."},
                    {"order": 9, "tool": "export_outputs",
                     "args_template": {"project_name": "my_board"}},
                ],
            },
            "import_kicad": {
                "when": "You already have a KiCad schematic/netlist file.",
                "steps": [
                    {"order": 1, "tool": "import_kicad_netlist",
                     "args_template": {"project_name": "my_board",
                                       "file_path": "/abs/path/board.net"}},
                    {"order": 2, "tool": "verify_footprints",
                     "args_template": {"project_name": "my_board"},
                     "on_failure": "Call provide_footprint for each unresolved "
                                   "package, then re-run."},
                    {"order": 3, "tool": "optimize_placement",
                     "args_template": {"project_name": "my_board",
                                       "board_width_mm": 50,
                                       "board_height_mm": 40}},
                    {"order": 4, "tool": "route_board",
                     "args_template": {"project_name": "my_board"}, **poll_routing},
                    {"order": 5, "tool": "run_drc",
                     "args_template": {"project_name": "my_board"}},
                    {"order": 6, "tool": "export_outputs",
                     "args_template": {"project_name": "my_board"}},
                ],
            },
            "autonomous": {
                "when": "You want pcb-creator's own LLM pipeline to do "
                        "everything from a text description (requires a "
                        "configured LLM).",
                "steps": [
                    {"order": 1, "tool": "design_pcb",
                     "args_template": {"description": "A 555 LED blinker at 1Hz "
                                                      "powered by 9V"}},
                    {"order": 2, "tool": "get_project_status",
                     "wait_for": "design_state == 'complete' (poll every ~20s)",
                     "on_failure": "Read 'design_error' and "
                                   "'step_validator_errors'; fix the description "
                                   "or switch to the build_from_scratch flow."},
                ],
            },
        },
        "rules": [
            "Pick one workflow and follow it in order; every response's "
            "'next_step' tells you the next call.",
            "While routing or designing, keep polling get_project_status — "
            "'status_hint' always reports forward progress.",
            "Never use external CAD tools or CLIs; every fix is possible "
            "through these tools.",
        ],
    }


@mcp.tool()
def list_projects() -> list[dict]:
    """List all PCB design projects with their current status.

    Returns:
        List of dicts with project_name, status info, and last modified time.
    """
    projects_dir = _get_projects_dir()
    results = []

    for entry in sorted(projects_dir.iterdir()):
        if not entry.is_dir():
            continue

        project_name = entry.name
        info: dict = {"project_name": project_name}

        # Read STATUS.json
        status_path = entry / "STATUS.json"
        if status_path.exists():
            try:
                status = json.loads(status_path.read_text())
                info["steps"] = status.get("steps", {})
                info["last_updated"] = status_path.stat().st_mtime
            except (json.JSONDecodeError, OSError):
                info["steps"] = {}
        else:
            info["steps"] = {}

        # Check for key outputs
        info["has_routed"] = (entry / f"{project_name}_routed.json").exists()
        info["has_drc"] = (entry / f"{project_name}_drc_report.json").exists()
        info["has_outputs"] = (entry / "output").exists()

        results.append(info)

    return results


@mcp.tool()
def get_project_status(project_name: str) -> dict:
    """Get detailed status for a specific PCB project.

    Args:
        project_name: The project slug/name.

    Returns:
        Dict with step status, routing statistics, and DRC pass/fail.
    """
    pdir = _project_dir(project_name)

    # Check in-memory design/route jobs BEFORE checking disk.
    # A background design_pcb thread may not have created the project
    # directory yet (or crashed before mkdir), and callers need to see
    # running/failed state instead of a misleading "not found".
    with _DESIGN_LOCK:
        djob = dict(_DESIGN_JOBS.get(project_name)) if project_name in _DESIGN_JOBS else None
    with _ROUTE_LOCK:
        rjob = dict(_ROUTE_JOBS.get(project_name)) if project_name in _ROUTE_JOBS else None

    if not pdir.exists():
        # No directory on disk yet — a background design_pcb thread may not have
        # created it (or crashed before mkdir). Report in-memory job state instead
        # of a misleading "not found".
        if djob or rjob:
            import time as _time
            result: dict = {"project_name": project_name}
            if djob is not None:
                result["design_state"] = djob["state"]
                dstarted = djob.get("started_at")
                if djob["state"] == "running" and dstarted is not None:
                    result["design_elapsed_s"] = round(_time.monotonic() - dstarted, 1)
                elif djob.get("elapsed_s") is not None:
                    result["design_elapsed_s"] = djob["elapsed_s"]
                if djob["state"] == "running" and djob.get("progress") is not None:
                    result["design_progress"] = djob["progress"]
                if djob["state"] == "complete" and djob.get("result"):
                    result["design_result"] = djob["result"]
                elif djob["state"] == "failed":
                    result["design_error"] = djob.get("error")
            if rjob is not None:
                result["routing_state"] = rjob["state"]
                if rjob["state"] == "failed":
                    result["routing_error"] = rjob.get("error")
            return result
        return fail(
            f"Project '{project_name}' not found.",
            remediation=[option("List existing projects to find the right name",
                                "list_projects", {})],
        )

    result: dict = {"project_name": project_name}

    # STATUS.json — include per-step validator errors for agent diagnostics
    status_path = pdir / "STATUS.json"
    if status_path.exists():
        try:
            status_data = json.loads(status_path.read_text())
            result["status"] = status_data
            # Surface a flat list of all step errors for easy scanning
            step_errors: dict[str, list[str]] = {}
            step_warnings: dict[str, list[str]] = {}
            for skey, sinfo in status_data.get("steps", {}).items():
                if sinfo.get("validator_errors"):
                    step_errors[skey] = sinfo["validator_errors"]
                if sinfo.get("validator_warnings"):
                    step_warnings[skey] = sinfo["validator_warnings"]
            if step_errors:
                result["step_validator_errors"] = step_errors
            if step_warnings:
                result["step_validator_warnings"] = step_warnings
        except json.JSONDecodeError:
            result["status"] = {}

    # Routing stats
    routed = _read_project_json(project_name, "_routed.json")
    if routed:
        stats = routed.get("statistics", {})
        result["routing_stats"] = {
            "completion_pct": stats.get("completion_pct", 0),
            "total_nets": stats.get("total_nets", 0),
            "routed_nets": stats.get("routed_nets", 0),
            "via_count": stats.get("via_count", 0),
            "trace_length_mm": stats.get("total_trace_length_mm", 0),
            "unrouted_nets": stats.get("unrouted_nets", []),
        }

    # routing_state: in-memory job wins; else infer from on-disk artifact.
    import time as _time
    if rjob is not None:
        result["routing_state"] = rjob["state"]
        # Elapsed time: live during run, final after completion/failure
        started = rjob.get("started_at")
        if rjob["state"] == "running" and started is not None:
            result["routing_elapsed_s"] = round(_time.monotonic() - started, 1)
        elif rjob.get("elapsed_s") is not None:
            result["routing_elapsed_s"] = rjob["elapsed_s"]
        # Live NCR iteration progress (only meaningful while running)
        if rjob["state"] == "running" and rjob.get("progress") is not None:
            result["routing_progress"] = rjob["progress"]
        if rjob["state"] == "complete" and rjob.get("result"):
            result["routing_result"] = rjob["result"]
        elif rjob["state"] == "failed":
            result["routing_error"] = rjob.get("error")
    else:
        result["routing_state"] = "complete" if routed else "none"

    # Design job state (already fetched above for early-return).
    if djob is not None:
        result["design_state"] = djob["state"]
        dstarted = djob.get("started_at")
        if djob["state"] == "running" and dstarted is not None:
            result["design_elapsed_s"] = round(_time.monotonic() - dstarted, 1)
        elif djob.get("elapsed_s") is not None:
            result["design_elapsed_s"] = djob["elapsed_s"]
        if djob["state"] == "running" and djob.get("progress") is not None:
            result["design_progress"] = djob["progress"]
        if djob["state"] == "complete" and djob.get("result"):
            result["design_result"] = djob["result"]
        elif djob["state"] == "failed":
            result["design_error"] = djob.get("error")
    else:
        # No in-memory job (e.g. server restarted). Infer from disk.
        st = result.get("status") or {}
        overall = str(
            st.get("overall_status") or st.get("overall") or st.get("state") or ""
        ).upper()
        if overall in ("COMPLETE", "DONE", "SUCCESS", "OK"):
            result["design_state"] = "complete"
        elif overall in ("ERROR", "FAILED", "FAIL"):
            result["design_state"] = "failed"
        elif (pdir / "output").exists() and any((pdir / "output").iterdir()):
            result["design_state"] = "complete"
        elif status_path.exists():
            result["design_state"] = "unknown"
        else:
            result["design_state"] = "none"

    # DRC summary
    drc = _read_project_json(project_name, "_drc_report.json")
    if drc:
        result["drc"] = {
            "passed": drc.get("passed", False),
            "summary": drc.get("summary", ""),
            "errors": drc.get("statistics", {}).get("errors", 0),
            "warnings": drc.get("statistics", {}).get("warnings", 0),
        }

    # Output files
    output_dir = pdir / "output"
    if output_dir.exists():
        result["output_files"] = [
            str(f.relative_to(pdir)) for f in sorted(output_dir.iterdir())
            if f.is_file()
        ]

    # Anti-abandonment: while a background job runs, always tell the agent
    # what is happening and to keep polling.
    if result.get("routing_state") == "running":
        prog = result.get("routing_progress") or {}
        if prog.get("pass_num") is not None:
            detail = (f"pass {prog['pass_num']}"
                      + (f", {prog['incomplete_connections']} connections "
                         f"incomplete" if prog.get("incomplete_connections")
                         is not None else ""))
        elif prog.get("iteration") is not None:
            detail = (f"iteration {prog['iteration']}"
                      + (f"/{prog['max_iterations']}"
                         if prog.get("max_iterations") else ""))
        else:
            detail = f"{result.get('routing_elapsed_s', 0)}s elapsed"
        result["poll_again_in_s"] = 15
        result["status_hint"] = (
            f"Routing in progress ({detail}). Poll get_project_status again in "
            "~15s. Do not run other tools or external CLIs for this project."
        )
    elif result.get("design_state") == "running":
        prog = result.get("design_progress") or {}
        detail = (f"step {prog.get('step')}: {prog.get('name')}"
                  if prog.get("name")
                  else f"{result.get('design_elapsed_s', 0)}s elapsed")
        result["poll_again_in_s"] = 20
        result["status_hint"] = (
            f"Design pipeline in progress ({detail}). Poll get_project_status "
            "again in ~20s. Do not run other tools or external CLIs for this "
            "project."
        )

    return result


@mcp.tool()
def get_drc_report(project_name: str, verbose: bool = False) -> dict:
    """Get the DRC (Design Rule Check) report for a project.

    By default returns the agent-friendly summary: severity-ranked top
    violations, per-rule counts, and a remediation hint per failing rule.
    Pass verbose=True for the full report (every check, every violation).

    Args:
        project_name: The project slug/name.
        verbose: Return the complete raw report instead of the summary.
    """
    report = _read_project_json(project_name, "_drc_report.json")
    if report is None:
        return fail(
            f"No DRC report found for project '{project_name}'.",
            remediation=[option("Run DRC first", "run_drc",
                                {"project_name": project_name})],
        )
    if verbose:
        return report
    from validators.drc_report import summarize_drc
    return summarize_drc(report)


@mcp.tool()
def export_kicad(project_name: str) -> dict:
    """Export a completed PCB project to KiCad format (.kicad_pcb).

    Args:
        project_name: The project slug/name.

    Returns:
        Dict with success status and path to the generated KiCad file.
    """
    routed = _read_project_json(project_name, "_routed.json")
    netlist = _read_project_json(project_name, "_netlist.json")

    if not routed:
        return fail(
            f"No routed board found for project '{project_name}'.",
            remediation=[option("Route the board first", "route_board",
                                {"project_name": project_name})],
        )
    if not netlist:
        return fail(f"No netlist found for project '{project_name}'.")

    from exporters.kicad_exporter import export_kicad_pcb

    pdir = _project_dir(project_name)
    output_path = pdir / "output" / f"{project_name}.kicad_pcb"
    output_path.parent.mkdir(parents=True, exist_ok=True)

    try:
        result_path = export_kicad_pcb(routed, netlist, output_path)
        return ok({"kicad_path": str(result_path)})
    except Exception as e:
        return fail(str(e))


@mcp.tool()
def get_board_image(project_name: str, width: int = 2048) -> dict:
    """Render the routed PCB board as a PNG image.

    Args:
        project_name: The project slug/name.
        width: Output image width in pixels (default 2048).

    Returns:
        Dict with base64-encoded PNG image data.
    """
    routed = _read_project_json(project_name, "_routed.json")
    if not routed:
        return fail(
            f"No routed board found for project '{project_name}'.",
            remediation=[option("Route the board first", "route_board",
                                {"project_name": project_name})],
        )

    netlist = _read_project_json(project_name, "_netlist.json")
    bom = _read_project_json(project_name, "_bom.json")

    from orchestrator.vision_review import render_board_png

    try:
        png_bytes = render_board_png(routed, netlist, bom, width=width)
        b64 = base64.b64encode(png_bytes).decode("utf-8")
        return ok({
            "image_base64": b64,
            "width": width,
            "size_bytes": len(png_bytes),
            "mime_type": "image/png",
        })
    except Exception as e:
        return fail(f"Failed to render board image: {e}")


# ---------------------------------------------------------------------------
# KiCad import
# ---------------------------------------------------------------------------

@mcp.tool()
def import_kicad_netlist(
    project_name: str,
    file_path: str,
    description: str = "",
) -> dict:
    """Import a KiCad schematic netlist into pcb-creator to continue a mid-stream project.

    Converts a KiCad netlist export (.net) or schematic (.kicad_sch) into
    pcb-creator's internal circuit_schema format and saves it as the project
    netlist.  After this call succeeds the project is ready for placement and
    routing — call design_pcb with skip_to="routing" or use get_project_status
    to confirm, then export_kicad / get_board_image when done.

    Accepted file types
    -------------------
    .net        KiCad netlist export.  Export from KiCad Schematic Editor:
                File → Export → Netlist → KiCad format.  This is the most
                reliable input.
    .kicad_sch  KiCad schematic file.  A sibling .net file with the same stem
                must exist in the same directory (pcb-creator uses it for
                connectivity; the schematic is used only for component metadata).

    Args:
        project_name: Slug for the project (lowercase, underscores).
                      Must be unique — a new project directory is created.
        file_path:    Absolute path to the .net or .kicad_sch file.
        description:  Optional human-readable description written into the netlist.

    Returns:
        On success:
            {
                "success": True,
                "project_name": str,
                "netlist_path": str,      # where the netlist JSON was written
                "component_count": int,
                "net_count": int,
                "warnings": [str, ...],   # non-fatal issues (empty list = clean)
                "next_step": str,         # human-readable hint
            }
        On failure:
            {"success": False, "error": str}
    """
    from exporters.kicad_netlist_importer import convert_kicad_netlist

    # Validate project name
    if not re.match(r"^[a-z][a-z0-9_]*$", project_name):
        suggested = _slugify(project_name)
        return fail(
            f"Invalid project_name '{project_name}'. "
            "Use lowercase letters, digits, and underscores only (must start with a letter).",
            remediation=[option(
                f"Retry with the corrected name '{suggested}'",
                "import_kicad_netlist",
                {"project_name": suggested, "file_path": file_path},
            )],
        )

    # Refuse to overwrite an existing project
    pdir = _project_dir(project_name)
    if pdir.exists() and any(pdir.iterdir()):
        return fail(
            f"Project '{project_name}' already exists at {pdir}.",
            remediation=[
                option("Retry with a different project_name (e.g. add a suffix)",
                       "import_kicad_netlist",
                       {"project_name": f"{project_name}_v2", "file_path": file_path}),
                option("Check the existing project's state before deciding",
                       "get_project_status", {"project_name": project_name}),
            ],
        )

    try:
        result = convert_kicad_netlist(
            source_path=file_path,
            project_name=project_name,
            description=description,
        )
    except (FileNotFoundError, ValueError) as exc:
        return fail(str(exc), remediation=[option(
            "Verify the file path and re-export the netlist from KiCad "
            "(Schematic Editor: File > Export > Netlist > KiCad format), then retry",
            "import_kicad_netlist",
            {"project_name": project_name, "file_path": "<corrected path>"},
        )])
    except Exception as exc:
        return fail(f"Unexpected error during import: {exc}")

    netlist = result["netlist"]
    warnings = result["warnings"]

    # Write netlist JSON into the project directory
    pdir.mkdir(parents=True, exist_ok=True)
    netlist_path = pdir / f"{project_name}_netlist.json"
    netlist_path.write_text(json.dumps(netlist, indent=2), encoding="utf-8")

    # Count elements for the summary
    elements = netlist.get("elements", [])
    n_comp = sum(1 for e in elements if e["element_type"] == "component")
    n_net  = sum(1 for e in elements if e["element_type"] == "net")

    # Verify every footprint resolves now, so the agent can fix packages
    # immediately instead of discovering placeholders after placement.
    _ensure_lookup_configured()
    from validators.verify_footprints import verify_footprints
    unresolved = verify_footprints(netlist)

    if unresolved:
        first = unresolved[0]
        step = next_step(
            "provide_footprint",
            {"project_name": project_name, "package": first["package"],
             "like_package": "<a recognized package, e.g. 0805, SOIC-8>"},
            f"{len(unresolved)} component(s) have unresolved footprints "
            f"(see unresolved_footprints). Placement is BLOCKED until every "
            f"footprint resolves; fix each, then call "
            f"verify_footprints('{project_name}') to confirm.",
        )
    else:
        step = next_step(
            "optimize_placement",
            {"project_name": project_name, "board_width_mm": "<width>",
             "board_height_mm": "<height>"},
            f"Netlist imported ({n_comp} components, {n_net} nets), all "
            "footprints resolved. Board dimensions are required on the first "
            "placement.",
        )

    return ok({
        "project_name":          project_name,
        "netlist_path":          str(netlist_path),
        "component_count":       n_comp,
        "net_count":             n_net,
        "warnings":              warnings,
        "unresolved_footprints": unresolved,
    }, step)


# ---------------------------------------------------------------------------
# Footprint verification + remediation (agent-driven footprint review)
# ---------------------------------------------------------------------------

@mcp.tool()
def verify_footprints(project_name: str) -> dict:
    """Check that every component's footprint resolves to real pad geometry.

    This is the deterministic gate that placement enforces. A component whose
    package cannot be resolved through any library tier (KiCad library →
    IPC-7351 → cache → built-in → normalized name) would silently become a 3mm
    placeholder — so placement refuses to run until this returns clean.

    Call after import_kicad_netlist, and again after each provide_footprint /
    package-name fix, until ``unresolved`` is empty.

    Args:
        project_name: Project slug (must already have a netlist).

    Returns:
        {
            "success": True,
            "resolved": bool,                 # True when nothing is unresolved
            "component_count": int,
            "unresolved_count": int,
            "unresolved_footprints": [        # empty when resolved
                {"designator", "package", "pin_count", "reason"}, ...
            ],
        }  or  {"success": False, "error": str}
    """
    pdir = _project_dir(project_name)
    netlist = _read_project_json(project_name, "_netlist.json")
    if netlist is None:
        return fail(
            f"No netlist for '{project_name}'.",
            remediation=[
                option("Import a KiCad netlist", "import_kicad_netlist",
                       {"project_name": project_name, "file_path": "<path to .net>"}),
                option("Build a circuit from scratch", "create_circuit",
                       {"project_name": project_name, "description": "<circuit description>"}),
            ],
        )

    _ensure_lookup_configured()
    from validators.verify_footprints import verify_footprints as _verify

    unresolved = _verify(netlist)
    n_comp = sum(1 for e in netlist.get("elements", [])
                 if e.get("element_type") == "component")
    if unresolved:
        first = unresolved[0]
        step = next_step(
            "provide_footprint",
            {"project_name": project_name, "package": first["package"],
             "like_package": "<recognized package, e.g. 0805, SOIC-8, SOT-23>"},
            f"{len(unresolved)} footprint(s) unresolved — fix each (alias via "
            "like_package, or pin_offsets + pad_size), then re-run "
            "verify_footprints.",
        )
    else:
        step = next_step(
            "optimize_placement",
            {"project_name": project_name, "board_width_mm": "<width>",
             "board_height_mm": "<height>"},
            "All footprints resolved — the placement gate is clear.",
        )
    return ok({
        "resolved": not unresolved,
        "component_count": n_comp,
        "unresolved_count": len(unresolved),
        "unresolved_footprints": unresolved,
    }, step)


@mcp.tool()
def provide_footprint(
    project_name: str,
    package: str,
    like_package: str | None = None,
    pin_offsets: dict | None = None,
    pad_size: list | None = None,
) -> dict:
    """Supply footprint geometry for a package the libraries don't know.

    Use exactly ONE of two modes:

    Mode 1 — alias a verbose/unknown name to a recognized package:

        provide_footprint("my_board", "R_0805_2012Metric_Pad1.05x1.40mm",
                          like_package="0805")

    Mode 2 — explicit geometry from the datasheet. pin_offsets maps pin number
    (string) to [dx_mm, dy_mm] from the component center at rotation 0;
    pad_size is [width_mm, height_mm]:

        provide_footprint("my_board", "CUSTOM-4",
                          pin_offsets={"1": [-1.27, 1.0], "2": [-1.27, -1.0],
                                       "3": [1.27, -1.0], "4": [1.27, 1.0]},
                          pad_size=[1.05, 1.4])

    The entry persists in the shared component cache for all later runs.
    After calling this, run verify_footprints to confirm the gate is clear.
    """
    _ensure_lookup_configured()
    from optimizers.pad_geometry import get_footprint_def, get_default_cache

    _verify_step = next_step(
        "verify_footprints", {"project_name": project_name},
        "Confirm the footprint gate is now clear.",
    )

    cache = get_default_cache()
    if cache is None:
        return fail("Component cache is not configured; cannot persist footprint.")

    if not package:
        return fail("package must be a non-empty string.")

    # Mode 1: alias to a recognized package.
    if like_package:
        ref = get_footprint_def(like_package, 0)
        if ref is None:
            return fail(
                f"like_package '{like_package}' is itself unresolved.",
                remediation=[
                    option("Alias to a recognized package instead",
                           "provide_footprint",
                           {"project_name": project_name, "package": package,
                            "like_package": "<one of: 0402, 0603, 0805, 1206, "
                                            "SOIC-8, SOT-23, DIP-8, TQFP-32>"}),
                    option("Supply explicit geometry from the datasheet",
                           "provide_footprint",
                           {"project_name": project_name, "package": package,
                            "pin_offsets": {"1": [-1.27, 0.0], "2": [1.27, 0.0]},
                            "pad_size": [1.05, 1.4]}),
                ],
            )
        offsets = {str(k): [float(v[0]), float(v[1])]
                   for k, v in ref.pin_offsets.items()}
        cache.put_footprint(package, offsets, list(ref.pad_size),
                            source="agent", needs_review=True)
        return ok({"package": package,
                   "source": f"agent (alias of {like_package})",
                   "pin_count": len(offsets)}, _verify_step)

    # Mode 2: explicit geometry.
    if pin_offsets and pad_size:
        try:
            offsets = {str(k): [float(v[0]), float(v[1])]
                       for k, v in pin_offsets.items()}
            psize = [float(pad_size[0]), float(pad_size[1])]
        except (TypeError, ValueError, IndexError, KeyError) as exc:
            return fail(
                f"Malformed pin_offsets/pad_size: {exc}.",
                remediation=[option(
                    "Retry with the exact shapes shown in args: pin_offsets maps "
                    "pin number to [dx_mm, dy_mm]; pad_size is [width_mm, height_mm]",
                    "provide_footprint",
                    {"project_name": project_name, "package": package,
                     "pin_offsets": {"1": [-1.27, 0.0], "2": [1.27, 0.0]},
                     "pad_size": [1.05, 1.4]},
                )],
            )
        cache.put_footprint(package, offsets, psize,
                            source="agent", needs_review=True)
        return ok({"package": package, "source": "agent",
                   "pin_count": len(offsets)}, _verify_step)

    return fail(
        "Provide either like_package, or pin_offsets + pad_size.",
        remediation=[
            option("Alias to a recognized package", "provide_footprint",
                   {"project_name": project_name, "package": package,
                    "like_package": "0805"}),
            option("Supply explicit geometry", "provide_footprint",
                   {"project_name": project_name, "package": package,
                    "pin_offsets": {"1": [-1.27, 0.0], "2": [1.27, 0.0]},
                    "pad_size": [1.05, 1.4]}),
        ],
    )


# ---------------------------------------------------------------------------
# Incremental circuit builder (design from scratch with small validated calls)
# ---------------------------------------------------------------------------

def _builder_fail(result: dict, project_name: str) -> dict:
    """Map a circuit_builder error result onto the failure envelope."""
    code = result.get("code", "")
    rem = []
    if code == "no_draft":
        rem.append(option("Create the circuit draft first", "create_circuit",
                          {"project_name": project_name,
                           "description": "<circuit description>",
                           "board_width_mm": 50, "board_height_mm": 40}))
    elif code == "unresolved_footprint":
        rem.append(option(
            "Supply geometry for the unknown package, then retry add_component",
            "provide_footprint",
            {"project_name": project_name, "package": result.get("package"),
             "like_package": "<recognized package, e.g. 0805, SOIC-8, SOT-23>"},
        ))
    elif code == "unknown_pin_count":
        rem.append(option(
            "Retry with an explicit pinout string", "add_component",
            {"project_name": project_name,
             "pinout": "1:GND 2:TRIG 3:OUT 4:RESET 5:CTRL 6:THRES 7:DISCH 8:VCC"},
        ))
    elif code == "unconnected_pins":
        first = (result.get("unconnected_pins") or ["U1.1"])[0]
        rem.append(option("Connect the listed pins", "connect_pins",
                          {"project_name": project_name, "net_name": "<net>",
                           "pins": [first, "<other pin>"]}))
        rem.append(option("Mark truly unused pins as no-connect",
                          "mark_no_connect",
                          {"project_name": project_name,
                           "pins": result.get("unconnected_pins", [])[:12]}))
    elif code in ("pin_conflict", "single_pin_nets"):
        rem.append(option("Review the current circuit state", "list_circuit",
                          {"project_name": project_name}))
    data = {k: v for k, v in result.items()
            if k not in ("ok", "error", "code")}
    return fail(result.get("error", "Operation failed."),
                remediation=rem or None, data=data or None)


@mcp.tool()
def create_circuit(project_name: str, description: str,
                   board_width_mm: float, board_height_mm: float,
                   layers: int = 2) -> dict:
    """Start a new circuit design from scratch (step 1 of the builder flow).

    Creates an empty draft you then fill with add_component and connect_pins,
    and compile with finalize_circuit. Each call is small and validated — no
    big JSON needed.

    Example: create_circuit("led_blinker", "555 LED blinker at 1Hz",
                            board_width_mm=40, board_height_mm=30)
    """
    from orchestrator import circuit_builder as cb
    result = cb.create_draft(_project_dir(project_name), project_name,
                             description, board_width_mm, board_height_mm,
                             layers)
    if not result.pop("ok"):
        return _builder_fail(result, project_name)
    return ok(result, next_step(
        "add_component",
        {"project_name": project_name, "designator": "U1",
         "component_type": "ic", "value": "<part>", "package": "<package>"},
        "Add each component; the response lists its pins for connect_pins.",
    ))


@mcp.tool()
def add_component(project_name: str, designator: str, component_type: str,
                  value: str, package: str, pinout: str | None = None,
                  pin_count: int | None = None) -> dict:
    """Add one component to the circuit draft. Returns its pin table.

    component_type: resistor, capacitor, inductor, led, diode, transistor_npn,
    transistor_pnp, transistor_nmos, transistor_pmos, ic, connector, switch,
    voltage_regulator, crystal, fuse, relay.

    The package is resolved to a real footprint immediately — unknown packages
    fail here (fix with provide_footprint) instead of blocking placement later.
    For ICs, pass pinout so pins get names you can use in connect_pins:

        add_component("my_board", "U1", "ic", "NE555", "DIP-8",
                      pinout="1:GND 2:TRIG 3:OUT 4:RESET 5:CTRL 6:THRES "
                             "7:DISCH 8:VCC")

    LEDs/diodes get pin names anode (1) / cathode (2) automatically;
    transistors get base/emitter/collector or gate/source/drain (SOT-23
    convention); 3-pin regulators get IN/GND/OUT. pin_count overrides the
    count derived from the package name when they disagree.
    """
    _ensure_lookup_configured()
    from orchestrator import circuit_builder as cb
    from optimizers.pad_geometry import get_footprint_def
    result = cb.add_component(_project_dir(project_name), project_name,
                              designator, component_type, value, package,
                              pinout=pinout, pin_count=pin_count,
                              footprint_lookup=get_footprint_def)
    if not result.pop("ok"):
        return _builder_fail(result, project_name)
    return ok(result, next_step(
        "connect_pins",
        {"project_name": project_name, "net_name": "<net>",
         "pins": [f"{designator}.1", "<other pin>"]},
        "Add more components, or start connecting pins into nets.",
    ))


@mcp.tool()
def connect_pins(project_name: str, net_name: str, pins: list[str],
                 net_class: str | None = None) -> dict:
    """Connect component pins into a named net (creates the net if new).

    pins use DESIGNATOR.PIN form — pin number or pin name:

        connect_pins("my_board", "VCC", ["U1.8", "C1.1", "J1.1"])
        connect_pins("my_board", "LED_DRIVE", ["R1.2", "D1.anode"])

    net_class (signal | power | ground) is auto-inferred from the net name
    (VCC/5V → power, GND → ground) — pass it only to override. Idempotent:
    re-connecting the same pin to the same net is a no-op; a pin already on a
    DIFFERENT net is an error (disconnect_pins first).
    """
    from orchestrator import circuit_builder as cb
    result = cb.connect_pins(_project_dir(project_name), project_name,
                             net_name, pins, net_class)
    if not result.pop("ok"):
        return _builder_fail(result, project_name)
    return ok(result, next_step(
        "list_circuit", {"project_name": project_name},
        "Connect remaining nets, then list_circuit to see unconnected pins, "
        "then finalize_circuit.",
    ))


@mcp.tool()
def disconnect_pins(project_name: str, net_name: str,
                    pins: list[str]) -> dict:
    """Remove pins from a net (the net is deleted when it becomes empty).

    Example: disconnect_pins("my_board", "VCC", ["U1.8"])
    """
    from orchestrator import circuit_builder as cb
    result = cb.disconnect_pins(_project_dir(project_name), project_name,
                                net_name, pins)
    if not result.pop("ok"):
        return _builder_fail(result, project_name)
    return ok(result)


@mcp.tool()
def mark_no_connect(project_name: str, pins: list[str]) -> dict:
    """Mark pins as intentionally unused (finalize_circuit requires every pin
    to be connected or explicitly no-connect).

    Example: mark_no_connect("my_board", ["U1.5", "U1.4"])
    """
    from orchestrator import circuit_builder as cb
    result = cb.mark_no_connect(_project_dir(project_name), project_name, pins)
    if not result.pop("ok"):
        return _builder_fail(result, project_name)
    return ok(result)


@mcp.tool()
def remove_component(project_name: str, designator: str) -> dict:
    """Remove a component from the draft (also detaches it from all nets).

    Example: remove_component("my_board", "R3")
    """
    from orchestrator import circuit_builder as cb
    result = cb.remove_component(_project_dir(project_name), project_name,
                                 designator)
    if not result.pop("ok"):
        return _builder_fail(result, project_name)
    return ok(result)


@mcp.tool()
def list_circuit(project_name: str) -> dict:
    """Show the current circuit draft: components, nets, no-connects, and —
    importantly — any pins still unconnected (these block finalize_circuit).
    """
    from orchestrator import circuit_builder as cb
    draft = cb.load_draft(_project_dir(project_name), project_name)
    if draft is None:
        return _builder_fail({"code": "no_draft",
                              "error": f"No circuit draft for '{project_name}'. "
                                       "Call create_circuit first."},
                             project_name)
    result = cb.list_circuit(draft)
    result.pop("ok", None)
    unconnected = result.get("unconnected_pins", [])
    if unconnected:
        step = next_step(
            "connect_pins",
            {"project_name": project_name, "net_name": "<net>",
             "pins": unconnected[:2]},
            f"{len(unconnected)} pin(s) still unconnected — connect them or "
            "mark_no_connect, then finalize_circuit.",
        )
    else:
        step = next_step("finalize_circuit", {"project_name": project_name},
                         "All pins are accounted for — compile and validate "
                         "the netlist.")
    return ok(result, step)


@mcp.tool()
def finalize_circuit(project_name: str) -> dict:
    """Compile the draft into the project netlist and validate it fully
    (schema, referential integrity, electrical DRC, footprint gate).

    On success the project is ready for optimize_placement (the next_step
    includes your board dimensions). On failure, 'errors' lists exactly what
    to fix with connect_pins / remove_component / add_component.
    """
    _ensure_lookup_configured()
    from orchestrator import circuit_builder as cb
    result = cb.finalize(_project_dir(project_name), project_name)
    if not result.pop("ok"):
        return _builder_fail(result, project_name)
    board = result.get("board", {})
    return ok(result, next_step(
        "optimize_placement",
        {"project_name": project_name,
         "board_width_mm": board.get("width_mm"),
         "board_height_mm": board.get("height_mm")},
        "Netlist is valid — place the components next.",
    ))


@mcp.tool()
def place_component(project_name: str, designator: str, x_mm: float,
                    y_mm: float, rotation_deg: int = 0,
                    layer: str = "top") -> dict:
    """Fix a component at exact board coordinates (e.g. a connector that must
    sit on an edge, or a mounting hole matching an enclosure).

    Validated immediately: the position must keep the component's PADS inside
    the board (1mm edge clearance) and clear of other pinned components —
    invalid coordinates fail here, not as silent overlaps later. Pinned
    components are never moved by optimize_placement; everything else is
    placed around them. Coordinates are mm from the top-left board corner
    (x right, y down). Re-calling replaces the pin; undo with
    unplace_component.

    Example: place_component("my_board", "J1", x_mm=2.5, y_mm=20,
                             rotation_deg=90)
    """
    from orchestrator import stages
    _ensure_lookup_configured()
    result = stages.set_placement_pin(_project_dir(project_name), project_name,
                                      designator, x_mm, y_mm, rotation_deg,
                                      layer)
    if not result.pop("ok"):
        rem = []
        if result.get("code") in ("out_of_bounds", "pin_overlap"):
            rem.append(option("Retry with adjusted coordinates",
                              "place_component",
                              {"project_name": project_name,
                               "designator": designator,
                               "x_mm": "<new x>", "y_mm": "<new y>"}))
        return fail(result.get("error", "place_component failed."),
                    remediation=rem or None)
    return ok(result, next_step(
        "optimize_placement", {"project_name": project_name},
        "Pin more components, or run placement — pinned components stay "
        "fixed and everything else is placed around them.",
    ))


@mcp.tool()
def unplace_component(project_name: str, designator: str) -> dict:
    """Remove a component's fixed-position pin so optimize_placement may move
    it again.

    Example: unplace_component("my_board", "J1")
    """
    from orchestrator import stages
    result = stages.clear_placement_pin(_project_dir(project_name),
                                        project_name, designator)
    if not result.pop("ok"):
        return fail(result.get("error", "unplace_component failed."))
    return ok(result, next_step("optimize_placement",
                                {"project_name": project_name},
                                "Re-run placement to apply the change."))


# ---------------------------------------------------------------------------
# Granular deterministic stages (agent-driven flow — no LLM, no vision critic)
# ---------------------------------------------------------------------------

@mcp.tool()
def optimize_placement(
    project_name: str,
    board_width_mm: float | None = None,
    board_height_mm: float | None = None,
    seed: int | None = None,
    two_sided: bool = False,
    plane_layers: int | None = None,
) -> dict:
    """Place components deterministically and optimize the layout (no LLM).

    Runs deterministic grid placement → overlap repair → simulated-annealing
    optimization (wirelength + signal-net crossings). Reads the project netlist,
    writes the project placement. Returns quickly.

    Call this after import_kicad_netlist (or after design_pcb has produced a
    netlist). On the first placement you must supply board dimensions — a KiCad
    netlist carries no board outline. On a re-run, dimensions are reused from the
    existing placement if omitted.

    two_sided=True lets the optimizer move small SMD passives (resistors,
    capacitors, diodes) to the BOTTOM of the board. Use it when components
    do not FIT on top (placement fails with overlap violations) — it extends
    how small a board can be. CAUTION: on 2-layer boards the bottom is the
    router's escape layer, so bottom-side parts can REDUCE routing
    completion; prefer a larger board when routing (not fit) is the problem.
    Connectors, ICs, LEDs, and through-hole parts always stay on top.

    plane_layers (4-layer boards only) sets how many inner layers are solid
    PLANES: 2 (default) = In1 GND + In2 power planes, 2 signal layers (best
    power integrity); 1 = In1 GND plane only, In2 becomes a 3rd SIGNAL layer
    (power routed as traces) — use for dense / many-signal boards (e.g. a
    fine-pitch connector with lots of GPIO) that won't route on 2 signal
    layers; 0 = all inner layers signal. Persists for re-placements.

    Example: optimize_placement("my_board", board_width_mm=45,
                                board_height_mm=18, two_sided=True,
                                plane_layers=1)
    """
    from orchestrator import stages

    pdir = _project_dir(project_name)
    if not pdir.exists():
        return fail(
            f"Project '{project_name}' not found.",
            remediation=[
                option("Import a KiCad netlist first", "import_kicad_netlist",
                       {"project_name": project_name, "file_path": "<path to .net>"}),
                option("Build a circuit from scratch", "create_circuit",
                       {"project_name": project_name, "description": "<circuit description>"}),
                option("List existing projects to find the right name", "list_projects", {}),
            ],
        )

    _ensure_lookup_configured()
    try:
        result = stages.run_placement(
            pdir, project_name, _get_config(),
            board_width_mm=board_width_mm,
            board_height_mm=board_height_mm,
            seed=seed,
            two_sided=two_sided or None,
            plane_layers=plane_layers,
        )
    except Exception as exc:
        return fail(f"Placement failed: {exc}")

    if not result.get("success"):
        rem = []
        if result.get("unresolved_footprints"):
            first = result["unresolved_footprints"][0]
            rem.append(option(
                "Resolve the blocked footprints, then re-run placement",
                "provide_footprint",
                {"project_name": project_name, "package": first.get("package"),
                 "like_package": "<recognized package>"},
            ))
        if result.get("violations"):
            v = result["violations"]
            pinned_dess = sorted({e["designator"] for e in v["out_of_bounds"]
                                  if e["pinned"]}
                                 | {d for o in v["overlaps"] if o["pinned"]
                                    for d in (o["a"], o["b"])})
            if pinned_dess:
                rem.append(option(
                    f"Adjust the fixed position of {pinned_dess[0]} (it "
                    "conflicts and is never moved automatically)",
                    "place_component",
                    {"project_name": project_name,
                     "designator": pinned_dess[0],
                     "x_mm": "<new x>", "y_mm": "<new y>"}))
                rem.append(option(
                    "Or unpin it and let the optimizer place it",
                    "unplace_component",
                    {"project_name": project_name,
                     "designator": pinned_dess[0]}))
            rem.append(option(
                "Re-place on a larger board", "optimize_placement",
                {"project_name": project_name,
                 "board_width_mm": "<larger width>",
                 "board_height_mm": "<larger height>"}))
        return fail(result.get("error", "Placement failed."),
                    remediation=rem or None, data=result)

    return ok(result, next_step(
        "route_board", {"project_name": project_name},
        f"Placement done: wire length {result.get('wire_length_mm')}mm, "
        f"{result.get('crossings')} crossings. Routing runs in the background; "
        "poll get_project_status afterwards.",
    ))


@mcp.tool()
def route_board(project_name: str, effort: str = "normal",
                max_seconds: int | None = None, auto_retry: bool = True,
                allow_grow: bool = False, keep_existing: bool = False) -> dict:
    """Start routing the placed board (deterministic). Returns immediately.

    Routing runs on a background thread. Poll get_project_status(project_name)
    and read 'routing_state' (running → complete | failed); while running,
    'routing_progress' and 'status_hint' report live pass-by-pass progress.
    When complete, 'routing_result' holds the stats (completion_pct,
    routed_nets, via_count, unrouted_nets, valid).

    effort controls routing quality vs wait time:
      "fast"   — quick first result (~2 min cap), fewer optimization passes.
      "normal" — default balance (~5 min cap).
      "best"   — maximum optimization (~15 min cap, auto-retries on timeout).
    max_seconds overrides the effort level's time cap when given.

    auto_retry (default true): if the route is incomplete, automatically
    re-place once with extra component clearance and re-route, keeping the
    better result. allow_grow additionally permits a 10% board-size increase
    for that retry.

    keep_existing=True does INCREMENTAL routing: the project's current routed
    board is kept as protected wiring and only the UNROUTED nets are routed —
    use it to finish a partly-routed board (e.g. one imported from KiCad or a
    prior incomplete route) instead of redoing it. Placement is not changed
    (so existing traces stay valid) and auto_retry is ignored.

    Requires a placement — call optimize_placement first.

    Example: route_board("my_board", effort="best", keep_existing=True)
    """
    if effort not in ("fast", "normal", "best"):
        return fail(
            f"Invalid effort '{effort}'.",
            remediation=[option(
                "Use one of: fast, normal, best", "route_board",
                {"project_name": project_name, "effort": "normal"},
            )],
        )
    from orchestrator import stages

    pdir = _project_dir(project_name)
    if not pdir.exists():
        return fail(
            f"Project '{project_name}' not found.",
            remediation=[option("List existing projects", "list_projects", {})],
        )
    if not (pdir / f"{project_name}_placement.json").exists():
        return fail(
            "No placement found.",
            remediation=[option(
                "Place the components first", "optimize_placement",
                {"project_name": project_name, "board_width_mm": "<width>",
                 "board_height_mm": "<height>"},
            )],
        )

    import time as _time

    with _ROUTE_LOCK:
        current = _ROUTE_JOBS.get(project_name)
        if current and current["state"] == "running":
            return working(
                data={"project_name": project_name},
                poll_again_in_s=15,
                status_hint=(
                    "Routing already in progress. Poll get_project_status and "
                    "read 'routing_state'; do not start another route_board."
                ),
            )
        _ROUTE_JOBS[project_name] = {
            "state": "running", "result": None, "error": None,
            "started_at": _time.monotonic(), "progress": None,
        }

    config = _get_config()

    def _on_progress(p: dict) -> None:
        with _ROUTE_LOCK:
            job = _ROUTE_JOBS.get(project_name)
            if job and job["state"] == "running":
                job["progress"] = p

    # Incremental: protect the existing routed traces/vias and route only the
    # unrouted nets. Read before the worker so a missing/empty board is caught.
    fixed_routing = None
    if keep_existing:
        existing = _read_project_json(project_name, "_routed.json")
        rt = (existing or {}).get("routing", {})
        if rt.get("traces") or rt.get("vias"):
            fixed_routing = {"traces": rt.get("traces", []),
                             "vias": rt.get("vias", [])}

    def _worker() -> None:
        try:
            if keep_existing:
                # No re-placement (would invalidate existing traces); route
                # only the remaining nets with the rest held as protected wiring.
                result = stages.run_routing(pdir, project_name, config,
                                            progress_callback=_on_progress,
                                            effort=effort, max_seconds=max_seconds,
                                            fixed_routing=fixed_routing)
            elif auto_retry:
                result = stages.run_route_with_retry(
                    pdir, project_name, config,
                    progress_callback=_on_progress,
                    effort=effort, max_seconds=max_seconds,
                    allow_grow=allow_grow)
            else:
                result = stages.run_routing(pdir, project_name, config,
                                            progress_callback=_on_progress,
                                            effort=effort, max_seconds=max_seconds)
            state = "complete" if result.get("success") else "failed"
            with _ROUTE_LOCK:
                started = _ROUTE_JOBS.get(project_name, {}).get("started_at")
                _ROUTE_JOBS[project_name] = {
                    "state": state, "result": result, "error": result.get("error"),
                    "started_at": started, "progress": None,
                    "elapsed_s": round(_time.monotonic() - started, 1) if started else None,
                }
        except Exception as exc:  # noqa: BLE001 — surface any failure to the poller
            with _ROUTE_LOCK:
                started = _ROUTE_JOBS.get(project_name, {}).get("started_at")
                _ROUTE_JOBS[project_name] = {
                    "state": "failed", "result": None, "error": str(exc),
                    "started_at": started, "progress": None,
                    "elapsed_s": round(_time.monotonic() - started, 1) if started else None,
                }

    threading.Thread(target=_worker, daemon=True).start()

    return working(
        data={
            "project_name": project_name,
            "next_step": next_step(
                "get_project_status", {"project_name": project_name},
                "Poll until 'routing_state' is 'complete' or 'failed'; "
                "'routing_progress' shows live progress.",
            ),
        },
        poll_again_in_s=15,
        status_hint=(
            "Routing started in the background (can take seconds to minutes). "
            "Keep polling get_project_status — progress is reported every pass. "
            "Do not run other tools or external CLIs for this project while "
            "routing is active."
        ),
    )


@mcp.tool()
def run_drc(project_name: str) -> dict:
    """Run deterministic design-rule checks on the routed board (no LLM).

    14 manufacturability/electrical checks (clearances, trace widths, annular
    rings, connectivity, shorts, IPC-2221 current capacity, etc.). Returns an
    agent-friendly summary: 'passed', severity-ranked 'top_violations',
    'failing_rules' each with a concrete 'remediation_hint', and a 'next_step'.
    Call get_drc_report(project_name, verbose=True) for the full raw report.

    Requires a routed board — call route_board and wait for routing_state
    "complete" first.
    """
    from orchestrator import stages

    pdir = _project_dir(project_name)
    if not pdir.exists():
        return fail(
            f"Project '{project_name}' not found.",
            remediation=[option("List existing projects", "list_projects", {})],
        )
    if not (pdir / f"{project_name}_routed.json").exists():
        return fail(
            "No routed board found.",
            remediation=[option("Route the board first", "route_board",
                                {"project_name": project_name})],
        )

    try:
        report = stages.run_drc(pdir, project_name, _get_config())
    except Exception as exc:
        return fail(f"DRC failed: {exc}")

    if report.get("error"):
        return fail(report["error"], remediation=[option(
            "Route the board first", "route_board", {"project_name": project_name})])

    from validators.drc_report import summarize_drc
    summary = summarize_drc(report)

    if report.get("passed"):
        step = next_step("export_outputs", {"project_name": project_name},
                         "DRC passed — generate manufacturing outputs.")
    else:
        first = summary["failing_rules"][0] if summary["failing_rules"] else {}
        step = next_step(
            "route_board", {"project_name": project_name, "effort": "best"},
            f"DRC failed ({summary['error_count']} errors). Each failing rule "
            "has a remediation_hint; most routing violations clear with a "
            "best-effort re-route. "
            + (first.get("remediation_hint", "") if first else ""),
        )
    return ok(summary, step)


@mcp.tool()
def export_outputs(project_name: str) -> dict:
    """Generate manufacturing outputs from the routed board (no LLM).

    Produces Gerbers, Excellon drill, BOM CSV, pick-and-place (CPL), populated
    STEP model, and a ZIP package — all written into the project's output/ dir.

    Requires a routed board.

    Args:
        project_name: Project slug.

    Returns:
        {success, output_dir, files: [...], package: <zip path>}  or  {success: False, error}
    """
    from orchestrator import stages

    pdir = _project_dir(project_name)
    if not pdir.exists():
        return fail(
            f"Project '{project_name}' not found.",
            remediation=[option("List existing projects", "list_projects", {})],
        )
    if not (pdir / f"{project_name}_routed.json").exists():
        return fail(
            "No routed board found.",
            remediation=[option("Route the board first", "route_board",
                                {"project_name": project_name})],
        )

    try:
        result = stages.run_export(pdir, project_name, _get_config())
    except Exception as exc:
        return fail(f"Export failed: {exc}")

    if not result.get("success"):
        return fail(result.get("error", "Export failed."), data=result)
    return ok(result, "Done — the ZIP package is ready for manufacturer upload. "
                      "Optionally call get_board_image for a final visual check.")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    """Run the MCP server (stdio transport)."""
    # Pipeline modules log via logging — route to stderr so the stdio
    # JSON-RPC stream on stdout is never corrupted.
    logging.basicConfig(level=logging.INFO, format="%(message)s", stream=sys.stderr)
    # Ensure CWD exists — Hermes worker scratch dirs can be deleted
    # out from under us, and pathlib.Path.cwd() will raise
    # FileNotFoundError if the process CWD is gone.
    try:
        os.getcwd()
    except FileNotFoundError:
        os.chdir("/tmp")
    _ensure_lookup_configured()
    mcp.run()


if __name__ == "__main__":
    main()
