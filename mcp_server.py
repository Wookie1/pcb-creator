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

mcp = FastMCP(
    "pcb-creator",
    instructions=(
        "PCB design tools, usable two ways.\n\n"
        "1) Autonomous (one shot): design_pcb runs the whole LLM-driven pipeline "
        "(requirements → schematic → BOM → placement → routing → DRC → output). "
        "It is ASYNC — it returns immediately with state 'running' and works on a "
        "background thread; poll get_project_status and read 'design_state' "
        "(running/complete/failed), 'design_progress' for the live step, then "
        "'design_result' when complete. Best when you want pcb-creator to do "
        "everything, but note it runs its own nested LLM + review loop you cannot "
        "see into.\n\n"
        "2) Granular (agent-driven, recommended when YOU are already an agent): "
        "drive the deterministic stages yourself and run your own QA between them. "
        "These tools use no LLM, return quickly, and never hide a rework loop:\n"
        "  import_kicad_netlist → optimize_placement → route_board → run_drc → export_outputs.\n"
        "route_board returns immediately and routes on a background thread; poll "
        "get_project_status and read its 'routing_state' (running/complete/failed), "
        "plus 'routing_progress' and 'routing_elapsed_s' for live progress. "
        "run_drc returns the deterministic design-rule violations as structured data "
        "for you to evaluate and decide on rework. Use get_board_image to review "
        "the board visually yourself instead of an internal vision critic."
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

# Footprint lookup globals — initialised once by _init_lookup() in main().
# Per-project custom indexes are built lazily in _get_project_custom_index().
_KICAD_INDEX: "Any | None" = None   # KiCadLibraryIndex for the system KiCad library
_CACHE: "Any | None" = None          # ComponentCache
_CUSTOM_INDICES: dict[str, "Any"] = {}   # project_name → KiCadLibraryIndex
_CUSTOM_LOCK = threading.Lock()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _init_lookup() -> None:
    """Initialise footprint resolution at MCP server startup.

    Builds the system KiCad library index (if PCB_KICAD_LIBRARY_PATH is set)
    and the component cache, then calls configure_lookup() so every subsequent
    pad-map / placement call has real footprint data.  Without this, the KiCad
    tier is silently skipped even when the env var is configured.
    """
    global _KICAD_INDEX, _CACHE
    from orchestrator.cache import ComponentCache
    from optimizers.pad_geometry import configure_lookup

    config = OrchestratorConfig.from_env(base_dir=_repo_root)
    _CACHE = ComponentCache(config.component_cache_path)

    if config.kicad_library_path:
        from exporters.kicad_mod_parser import KiCadLibraryIndex
        _KICAD_INDEX = KiCadLibraryIndex(config.kicad_library_path)

    configure_lookup(kicad_index=_KICAD_INDEX, cache=_CACHE, custom_index=None)


def _get_project_custom_index(project_name: str) -> "Any | None":
    """Return (building lazily) a KiCadLibraryIndex for the project's custom
    footprints directory, or None if it does not exist.

    The directory is ``<project_dir>/custom-footprints.pretty/``.  Agents write
    .kicad_mod files there via ``register_custom_footprint``; the index is
    invalidated on every write so new files are visible immediately.
    """
    custom_dir = _project_dir(project_name) / "custom-footprints.pretty"
    if not custom_dir.is_dir():
        return None
    with _CUSTOM_LOCK:
        if project_name not in _CUSTOM_INDICES:
            from exporters.kicad_mod_parser import KiCadLibraryIndex
            _CUSTOM_INDICES[project_name] = KiCadLibraryIndex(custom_dir)
        return _CUSTOM_INDICES[project_name]


def _activate_project_lookup(project_name: str) -> None:
    """Update the module-level footprint lookup to include this project's
    custom footprints as tier 0.

    Call this at the start of any tool that performs footprint resolution
    (optimize_placement, export_outputs, design_pcb worker thread) so that
    agent-registered footprints are visible to the placement engine.
    """
    from optimizers.pad_geometry import configure_lookup
    custom = _get_project_custom_index(project_name)
    configure_lookup(kicad_index=_KICAD_INDEX, cache=_CACHE, custom_index=custom)

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
    """Design a PCB from a circuit description or structured requirements.

    Starts the full pipeline (requirements → schematic → BOM → placement →
    routing → DRC → outputs) on a BACKGROUND THREAD and returns immediately,
    so long designs never hit the MCP client timeout. Poll
    get_project_status(project_name) and read 'design_state'
    (running → complete | failed); 'design_progress' shows the live step while
    running, and 'design_result' holds the full result (steps, routing stats,
    DRC summary, output files) when complete.

    Single-flight: calling design_pcb again for a project that is already
    running returns the in-progress job instead of launching a duplicate
    pipeline. Before re-designing after a disconnect, call get_project_status
    first — if 'design_state' is 'running' the prior run is still going.

    Two input modes (see get_requirements_schema() for the structured format,
    preferred for agents — it skips LLM translation):
      - requirements_json: structured requirements dict.
      - description: plain-English circuit description (translated via LLM).

    Args:
        description: Circuit description in plain English, or a short summary
            when using requirements_json. Used to auto-generate project_name if
            omitted.
        project_name: Optional project slug. Auto-generated from description if omitted.
        requirements_json: Structured requirements dict (schema from
            get_requirements_schema()). When provided, LLM translation is skipped.
        settings: Optional overrides: {"model","router_engine","max_rework_attempts","skip_qa"}.
        attachments: Optional file attachments (e.g. a "board_outline" DXF used by
            step 3); see get_requirements_schema() notes.

    Returns:
        {success: True, state: "running", project_name, poll}  immediately —
        or {success: False, ...} only on an immediate launch error.
    """
    import time as _time

    if not project_name:
        project_name = _slugify(description)

    # Single-flight: don't launch a duplicate pipeline for a project that is
    # already running. A second call returns the in-progress job to poll.
    with _DESIGN_LOCK:
        current = _DESIGN_JOBS.get(project_name)
        if current and current["state"] == "running":
            return {
                "success": True,
                "state": "running",
                "project_name": project_name,
                "message": "Design already in progress for this project; poll "
                           "get_project_status and read 'design_state'.",
            }
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
        # Activate project-local custom footprints (tier 0) so the pipeline
        # finds any agent-registered .kicad_mod files during placement/export.
        _activate_project_lookup(project_name)
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

    return {
        "success": True,
        "state": "running",
        "project_name": project_name,
        "poll": "Call get_project_status(project_name); read 'design_state' "
                "(running → complete | failed). 'design_result' holds the full "
                "result when complete; 'design_progress' shows the live step.",
    }


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
                print(f"  MCP auto-fix: {w}")
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
                    print(f"  MCP translate: {len(errors)} validation errors, retrying...")
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
                            print(f"  MCP auto-fix: {w}")
                        remaining = validate_requirements(requirements)
                        if remaining:
                            print(f"  MCP auto-fix: {len(remaining)} errors remain")

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
        return {"error": f"Project '{project_name}' not found"}

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

    return result


@mcp.tool()
def get_drc_report(project_name: str) -> dict:
    """Get the full DRC (Design Rule Check) report for a project.

    Args:
        project_name: The project slug/name.

    Returns:
        Full DRC report with pass/fail status, check details, and violation list.
    """
    report = _read_project_json(project_name, "_drc_report.json")
    if report is None:
        return {"error": f"No DRC report found for project '{project_name}'"}
    return report


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
        return {"error": f"No routed board found for project '{project_name}'"}
    if not netlist:
        return {"error": f"No netlist found for project '{project_name}'"}

    from exporters.kicad_exporter import export_kicad_pcb

    pdir = _project_dir(project_name)
    output_path = pdir / "output" / f"{project_name}.kicad_pcb"
    output_path.parent.mkdir(parents=True, exist_ok=True)

    try:
        result_path = export_kicad_pcb(routed, netlist, output_path)
        return {"success": True, "kicad_path": str(result_path)}
    except Exception as e:
        return {"success": False, "error": str(e)}


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
        return {"error": f"No routed board found for project '{project_name}'"}

    netlist = _read_project_json(project_name, "_netlist.json")
    bom = _read_project_json(project_name, "_bom.json")

    from orchestrator.vision_review import render_board_png

    try:
        png_bytes = render_board_png(routed, netlist, bom, width=width)
        b64 = base64.b64encode(png_bytes).decode("utf-8")
        return {
            "image_base64": b64,
            "width": width,
            "size_bytes": len(png_bytes),
            "mime_type": "image/png",
        }
    except Exception as e:
        return {"error": f"Failed to render board image: {e}"}


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
        return {
            "success": False,
            "error": (
                f"Invalid project_name '{project_name}'. "
                "Use lowercase letters, digits, and underscores only (must start with a letter)."
            ),
        }

    # Refuse to overwrite an existing project
    pdir = _project_dir(project_name)
    if pdir.exists() and any(pdir.iterdir()):
        return {
            "success": False,
            "error": (
                f"Project '{project_name}' already exists at {pdir}. "
                "Choose a different project_name or delete the existing project first."
            ),
        }

    try:
        result = convert_kicad_netlist(
            source_path=file_path,
            project_name=project_name,
            description=description,
        )
    except (FileNotFoundError, ValueError) as exc:
        return {"success": False, "error": str(exc)}
    except Exception as exc:
        return {"success": False, "error": f"Unexpected error during import: {exc}"}

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
        names = ", ".join(f"{u['designator']} ({u['package'] or 'no package'})"
                          for u in unresolved)
        next_step = (
            f"{len(unresolved)} component(s) have unresolved footprints: {names}. "
            "Placement will be BLOCKED until every footprint resolves. Fix each one "
            "by correcting its package name in the netlist, setting "
            "PCB_KICAD_LIBRARY_PATH, or calling provide_footprint(...), then call "
            f"verify_footprints(\"{project_name}\") to confirm."
        )
    else:
        next_step = (
            f"Netlist imported ({n_comp} components, {n_net} nets), all footprints "
            f"resolved. Call optimize_placement(\"{project_name}\", board_width_mm, "
            "board_height_mm) to continue."
        )

    return {
        "success":               True,
        "project_name":          project_name,
        "netlist_path":          str(netlist_path),
        "component_count":       n_comp,
        "net_count":             n_net,
        "warnings":              warnings,
        "unresolved_footprints": unresolved,
        "next_step":             next_step,
    }


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
        return {"success": False,
                "error": f"No netlist for '{project_name}'. Import one first."}

    _ensure_lookup_configured()
    from validators.verify_footprints import verify_footprints as _verify

    unresolved = _verify(netlist)
    n_comp = sum(1 for e in netlist.get("elements", [])
                 if e.get("element_type") == "component")
    return {
        "success": True,
        "resolved": not unresolved,
        "component_count": n_comp,
        "unresolved_count": len(unresolved),
        "unresolved_footprints": unresolved,
    }


@mcp.tool()
def provide_footprint(
    project_name: str,
    package: str,
    like_package: str | None = None,
    pin_offsets: dict | None = None,
    pad_size: list | None = None,
) -> dict:
    """Supply footprint geometry for a package the libraries don't know.

    Two ways to resolve an unresolved footprint (use exactly one):

    1. ``like_package`` — alias an unknown package to a recognized one. The
       geometry of ``like_package`` is resolved and cached under ``package``.
       Use this when the KiCad name is just verbose, e.g.
       provide_footprint(pn, "R_0805_2012Metric_Pad1.05x1.40mm", like_package="0805").

    2. ``pin_offsets`` + ``pad_size`` — give explicit geometry for a genuinely
       custom part. ``pin_offsets`` maps pin number → [dx_mm, dy_mm] from the
       component center at rotation 0; ``pad_size`` is [width_mm, height_mm].
       Pull these from the part's datasheet or .kicad_mod.

    The entry is written to the shared component cache (source ``agent``,
    needs_review=true) so it persists and applies to every later run. After
    calling this, run verify_footprints to confirm the gate is clear.

    Args:
        project_name: Project slug (used only to validate context).
        package:      The exact package string from the netlist to resolve.
        like_package: Recognized package whose geometry to reuse (mode 1).
        pin_offsets:  {pin_number: [dx_mm, dy_mm]} (mode 2).
        pad_size:     [width_mm, height_mm] (mode 2).

    Returns:
        {"success": True, "package": str, "source": str, "pin_count": int}
        or {"success": False, "error": str}
    """
    _ensure_lookup_configured()
    from optimizers.pad_geometry import get_footprint_def, get_default_cache

    cache = get_default_cache()
    if cache is None:
        return {"success": False,
                "error": "Component cache is not configured; cannot persist footprint."}

    if not package:
        return {"success": False, "error": "package must be a non-empty string."}

    # Mode 1: alias to a recognized package.
    if like_package:
        ref = get_footprint_def(like_package, 0)
        if ref is None:
            return {"success": False,
                    "error": (f"like_package '{like_package}' is itself unresolved. "
                              "Choose a recognized package (e.g. 0805, SOIC-8, "
                              "SOT-23, DIP-8) or use pin_offsets + pad_size.")}
        offsets = {str(k): [float(v[0]), float(v[1])]
                   for k, v in ref.pin_offsets.items()}
        cache.put_footprint(package, offsets, list(ref.pad_size),
                            source="agent", needs_review=True)
        return {"success": True, "package": package,
                "source": f"agent (alias of {like_package})",
                "pin_count": len(offsets)}

    # Mode 2: explicit geometry.
    if pin_offsets and pad_size:
        try:
            offsets = {str(k): [float(v[0]), float(v[1])]
                       for k, v in pin_offsets.items()}
            psize = [float(pad_size[0]), float(pad_size[1])]
        except (TypeError, ValueError, IndexError, KeyError) as exc:
            return {"success": False,
                    "error": f"Malformed pin_offsets/pad_size: {exc}. "
                             "pin_offsets={pin:[dx,dy]}, pad_size=[w,h]."}
        cache.put_footprint(package, offsets, psize,
                            source="agent", needs_review=True)
        return {"success": True, "package": package, "source": "agent",
                "pin_count": len(offsets)}

    return {"success": False,
            "error": "Provide either like_package, or pin_offsets + pad_size."}


# ---------------------------------------------------------------------------
# Granular deterministic stages (agent-driven flow — no LLM, no vision critic)
# ---------------------------------------------------------------------------

@mcp.tool()
def optimize_placement(
    project_name: str,
    board_width_mm: float | None = None,
    board_height_mm: float | None = None,
    seed: int | None = None,
) -> dict:
    """Place components deterministically and optimize the layout (no LLM).

    Runs deterministic grid placement → overlap repair → simulated-annealing
    optimization (wirelength + signal-net crossings). Reads the project netlist,
    writes the project placement. Returns quickly.

    Call this after import_kicad_netlist (or after design_pcb has produced a
    netlist). On the first placement you must supply board dimensions — a KiCad
    netlist carries no board outline. On a re-run, dimensions are reused from the
    existing placement if omitted.

    Args:
        project_name:    Project slug (must already have a netlist).
        board_width_mm:  Board width in mm (required on first placement).
        board_height_mm: Board height in mm (required on first placement).
        seed:            Optional RNG seed for reproducible placement.

    Returns:
        {success, component_count, wire_length_mm, crossings,
         board_width_mm, board_height_mm, placement_path}  or  {success: False, error}
    """
    from orchestrator import stages

    pdir = _project_dir(project_name)
    if not pdir.exists():
        return {"success": False, "error": f"Project '{project_name}' not found. Import a netlist first."}

    # Activate project-local custom footprints (tier 0) before placement so
    # agent-registered .kicad_mod files are visible to the placement engine.
    _activate_project_lookup(project_name)

    try:
        return stages.run_placement(
            pdir, project_name, _get_config(),
            board_width_mm=board_width_mm,
            board_height_mm=board_height_mm,
            seed=seed,
        )
    except Exception as exc:
        return {"success": False, "error": f"Placement failed: {exc}"}


@mcp.tool()
def route_board(project_name: str) -> dict:
    """Start routing the placed board (deterministic). Returns immediately.

    Routing can take seconds to minutes, so it runs on a background thread to
    avoid blocking/timeouts. This call returns right away with state "running".
    Poll get_project_status(project_name) and read 'routing_state'
    (running → complete | failed); when complete, 'routing_result' holds the
    stats (completion_pct, routed_nets, via_count, unrouted_nets, valid).

    Uses the configured engine (Freerouting by default, built-in fallback).
    Requires a placement — call optimize_placement first.

    Args:
        project_name: Project slug (must already have a placement).

    Returns:
        {success, state: "running", project_name}  or  {success: False, error}
    """
    from orchestrator import stages

    pdir = _project_dir(project_name)
    if not pdir.exists():
        return {"success": False, "error": f"Project '{project_name}' not found."}
    if not (pdir / f"{project_name}_placement.json").exists():
        return {"success": False, "error": "No placement found — call optimize_placement first."}

    import time as _time

    with _ROUTE_LOCK:
        current = _ROUTE_JOBS.get(project_name)
        if current and current["state"] == "running":
            return {"success": True, "state": "running", "project_name": project_name,
                    "message": "Routing already in progress; poll get_project_status."}
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

    def _worker() -> None:
        try:
            result = stages.run_routing(pdir, project_name, config,
                                        progress_callback=_on_progress)
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

    return {
        "success": True,
        "state": "running",
        "project_name": project_name,
        "poll": "Call get_project_status and read 'routing_state'.",
    }


@mcp.tool()
def run_drc(project_name: str) -> dict:
    """Run deterministic design-rule checks on the routed board (no LLM).

    13 manufacturability/electrical checks (clearances, trace widths, annular
    rings, copper slivers, acid traps, unrouted nets, IPC-2221, etc.). Returns
    the full report so you can decide on rework yourself.

    Requires a routed board — call route_board and wait for routing_state
    "complete" first.

    Args:
        project_name: Project slug.

    Returns:
        Full DRC report dict {success, passed, summary, checks: [...],
        statistics: {errors, warnings}}  or  {success: False, error}
    """
    from orchestrator import stages

    pdir = _project_dir(project_name)
    if not pdir.exists():
        return {"success": False, "error": f"Project '{project_name}' not found."}

    try:
        return stages.run_drc(pdir, project_name, _get_config())
    except Exception as exc:
        return {"success": False, "error": f"DRC failed: {exc}"}


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
        return {"success": False, "error": f"Project '{project_name}' not found."}

    # Activate project-local custom footprints so Gerber export uses the same
    # footprint geometry as placement/routing.
    _activate_project_lookup(project_name)

    try:
        return stages.run_export(pdir, project_name, _get_config())
    except Exception as exc:
        return {"success": False, "error": f"Export failed: {exc}"}


# ---------------------------------------------------------------------------
# Component pre-positioning (pin edge connectors before auto-placement)
# ---------------------------------------------------------------------------

@mcp.tool()
def set_component_positions(
    project_name: str,
    positions: list[dict],
    board_width_mm: float | None = None,
    board_height_mm: float | None = None,
) -> dict:
    """Pre-position components with placement_source='user' so optimize_placement
    treats them as fixed anchors and only moves everything else.

    Use this BEFORE optimize_placement to lock edge connectors (FFC ZIF,
    terminal blocks, headers, debug ports) at their spec-defined board positions.
    The SA optimizer skips components with placement_source='user', so they stay
    exactly where you put them.

    If no placement file exists yet for the project, a full grid placement is
    generated automatically from the netlist and then the specified components
    are pinned.  Call import_kicad_netlist first to ensure a netlist is available.

    Args:
        project_name:    Project slug.
        positions:       List of component position dicts, each with:
                           "designator"   (str, required) — e.g. "J1", "U3"
                           "x_mm"         (float, required) — X from board origin
                           "y_mm"         (float, required) — Y from board origin
                           "rotation_deg" (int, optional, default 0)
                           "layer"        (str, optional, "top" or "bottom", default "top")
        board_width_mm:  Board width (mm). Required when no placement exists yet.
        board_height_mm: Board height (mm). Required when no placement exists yet.

    Returns:
        {success: True, pinned_count: int, total_components: int,
         placement_path: str, notes: [str]}
        or {success: False, error: str}
    """
    pdir = _project_dir(project_name)
    if not pdir.exists():
        return {"success": False, "error": f"Project '{project_name}' not found. Run import_kicad_netlist first."}

    netlist_path = pdir / f"{project_name}_netlist.json"
    placement_path = pdir / f"{project_name}_placement.json"

    if not netlist_path.exists():
        return {"success": False, "error": "No netlist found — run import_kicad_netlist first."}

    # Load or generate placement
    if placement_path.exists():
        placement = json.loads(placement_path.read_text())
    else:
        # Need board dimensions to generate a seed placement
        bw = board_width_mm
        bh = board_height_mm
        if bw is None or bh is None:
            return {
                "success": False,
                "error": (
                    "No existing placement found and board_width_mm/board_height_mm not provided. "
                    "Supply board dimensions so a seed placement can be generated, or call "
                    "optimize_placement first and then call set_component_positions."
                ),
            }
        from optimizers.initial_placement import generate_grid_placement
        netlist = json.loads(netlist_path.read_text())
        _activate_project_lookup(project_name)
        placement = generate_grid_placement(netlist, bw, bh, project_name)
        if placement is None:
            return {"success": False, "error": "Could not generate seed placement — check that the netlist has components with resolvable footprints."}

    # Build a lookup from designator → placement item index
    des_index: dict[str, int] = {
        item["designator"]: i
        for i, item in enumerate(placement.get("placements", []))
    }

    notes = []
    pinned = []

    for pos in positions:
        des = pos.get("designator", "")
        if not des:
            notes.append("Skipped entry with no designator.")
            continue

        x = pos.get("x_mm")
        y = pos.get("y_mm")
        if x is None or y is None:
            notes.append(f"Skipped {des}: x_mm or y_mm missing.")
            continue

        if des not in des_index:
            notes.append(f"Warning: {des} not found in placement — it may not be in the netlist.")
            continue

        idx = des_index[des]
        placement["placements"][idx]["x_mm"] = float(x)
        placement["placements"][idx]["y_mm"] = float(y)
        placement["placements"][idx]["rotation_deg"] = int(pos.get("rotation_deg", 0))
        placement["placements"][idx]["layer"] = pos.get("layer", "top")
        placement["placements"][idx]["placement_source"] = "user"
        pinned.append(des)

    placement_path.write_text(json.dumps(placement, indent=2))

    return {
        "success": True,
        "pinned_count": len(pinned),
        "pinned_designators": pinned,
        "total_components": len(placement.get("placements", [])),
        "placement_path": str(placement_path),
        "notes": notes,
        "next_step": (
            "Call optimize_placement — pinned components will stay fixed; "
            "all other components will be placed around them."
        ),
    }


# ---------------------------------------------------------------------------
# Footprint coverage assessment and custom footprint registration
# ---------------------------------------------------------------------------

@mcp.tool()
def check_footprint_coverage(
    components: list[dict],
    project_name: str | None = None,
) -> dict:
    """Check footprint library coverage for a BOM before launching placement.

    Run this BEFORE design_pcb / optimize_placement to identify which components
    need custom footprints.  Components that miss all resolution tiers will cause
    placement failures or silent perimeter-approximation fallbacks that produce
    wrong pad geometry.

    Resolution tiers checked (in order):
      0. project-local custom footprints (if project_name given and has any)
      1. system KiCad library (~50 K authoritative footprints)
      2. IPC-7351B parametric (QFN, BGA, SOP, TSSOP, DFN, …)
      3. local component cache (prior EasyEDA / LLM lookups)
      4. built-in approximations

    Args:
        components:   List of component dicts, each with:
                        "reference"  (str, required) — designator, e.g. "U1"
                        "package"    (str, required) — package name, e.g. "QFN-32"
                        "pin_count"  (int, required) — number of pins
                        "value"      (str, optional) — component value / part number
        project_name: Optional project slug.  When given, project-local custom
                      footprints registered via register_custom_footprint are
                      checked as tier 0.

    Returns:
        {
          "coverage": {"total": int, "resolved": int, "custom_needed": int},
          "resolved": [
            {"reference": "R1", "package": "0402", "pin_count": 2, "tier": "kicad_library"},
            ...
          ],
          "custom_needed": [
            {"reference": "U1", "package": "QFN-48", "pin_count": 48,
             "value": "STM32F4",
             "notes": "Not found in KiCad library, IPC-7351B, cache, or built-ins. "
                      "Create a .kicad_mod and register via register_custom_footprint."},
            ...
          ],
        }
    """
    from optimizers.pad_geometry import check_footprint_tier

    custom = _get_project_custom_index(project_name) if project_name else None

    resolved = []
    custom_needed = []

    for comp in components:
        ref = comp.get("reference", "?")
        pkg = comp.get("package", "")
        pins = int(comp.get("pin_count", 0))
        val = comp.get("value", "")

        if not pkg:
            custom_needed.append({
                "reference": ref,
                "package": "",
                "pin_count": pins,
                "value": val,
                "notes": "No package specified — cannot resolve footprint.",
            })
            continue

        tier = check_footprint_tier(pkg, pins, custom_index=custom)

        if tier is not None:
            resolved.append({
                "reference": ref,
                "package": pkg,
                "pin_count": pins,
                "tier": tier,
            })
        else:
            custom_needed.append({
                "reference": ref,
                "package": pkg,
                "pin_count": pins,
                "value": val,
                "notes": (
                    f"Package '{pkg}' with {pins} pins not found in any tier "
                    "(KiCad library, IPC-7351B, cache, built-ins). "
                    "Create a .kicad_mod and call register_custom_footprint."
                ),
            })

    return {
        "coverage": {
            "total": len(components),
            "resolved": len(resolved),
            "custom_needed": len(custom_needed),
        },
        "resolved": resolved,
        "custom_needed": custom_needed,
    }


@mcp.tool()
def register_custom_footprint(
    project_name: str,
    package_name: str,
    kicad_mod_content: str,
) -> dict:
    """Register a custom .kicad_mod footprint for a project.

    Writes the footprint to the project's ``custom-footprints.pretty/``
    directory, where it is searched BEFORE the system KiCad library (tier 0).
    After registration, check_footprint_coverage, optimize_placement, and
    export_outputs will find it automatically.

    The project directory is created if it does not yet exist, so footprints
    can be pre-registered before the full pipeline runs.

    Args:
        project_name:      Project slug (lowercase letters, digits, underscores).
        package_name:      Package identifier matching what the netlist uses
                           (e.g. "QFN-48", "MY_CONNECTOR_4P").  Case-insensitive
                           during lookup.  The .kicad_mod filename is derived
                           from this (non-alphanumeric chars → underscores).
        kicad_mod_content: Full .kicad_mod file content in KiCad S-expression
                           format.  Must start with ``(footprint`` or
                           ``(module``.

    Returns:
        {success: True, path: str, package_name: str}
        or {success: False, error: str}
    """
    # Basic content sanity check
    stripped = kicad_mod_content.strip()
    if not (stripped.startswith("(footprint") or stripped.startswith("(module")):
        return {
            "success": False,
            "error": (
                "kicad_mod_content must be a valid KiCad S-expression starting "
                "with '(footprint ...' or '(module ...'. Got: "
                + stripped[:60]
            ),
        }

    # Build a filesystem-safe filename from the package name
    safe_name = re.sub(r"[^a-zA-Z0-9_\-\.]", "_", package_name).strip("_")
    if not safe_name:
        return {"success": False, "error": f"Cannot derive a safe filename from package_name '{package_name}'."}

    # Ensure the project custom-footprints.pretty directory exists
    custom_dir = _project_dir(project_name) / "custom-footprints.pretty"
    try:
        custom_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return {"success": False, "error": f"Could not create custom footprint directory: {exc}"}

    # Write the .kicad_mod file
    fp_path = custom_dir / f"{safe_name}.kicad_mod"
    try:
        fp_path.write_text(kicad_mod_content)
    except OSError as exc:
        return {"success": False, "error": f"Could not write footprint file: {exc}"}

    # Invalidate (or build) the cached index for this project so the new file
    # is visible on the next lookup without a server restart.
    with _CUSTOM_LOCK:
        if project_name in _CUSTOM_INDICES:
            _CUSTOM_INDICES[project_name].invalidate()
        else:
            from exporters.kicad_mod_parser import KiCadLibraryIndex
            _CUSTOM_INDICES[project_name] = KiCadLibraryIndex(custom_dir)

    return {
        "success": True,
        "path": str(fp_path),
        "package_name": package_name,
        "message": (
            f"Registered '{package_name}' as tier-0 custom footprint for project "
            f"'{project_name}'. It will be found by check_footprint_coverage and "
            "optimize_placement immediately."
        ),
    }


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    """Run the MCP server (stdio transport)."""
    # Ensure CWD exists — Hermes worker scratch dirs can be deleted
    # out from under us, and pathlib.Path.cwd() will raise
    # FileNotFoundError if the process CWD is gone.
    try:
        os.getcwd()
    except FileNotFoundError:
        os.chdir("/tmp")

    # Initialise footprint lookup globals so the KiCad library tier is active
    # for all placement/export calls in this server process.
    _init_lookup()

    mcp.run()


if __name__ == "__main__":
    main()
