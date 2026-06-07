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
        "Best when you want pcb-creator to do everything.\n\n"
        "2) Granular (agent-driven, recommended when YOU are already an agent): "
        "drive the deterministic stages yourself and run your own QA between them. "
        "These tools use no LLM, return quickly, and never hide a rework loop:\n"
        "  import_kicad_netlist → optimize_placement → route_board → run_drc → export_outputs.\n"
        "route_board returns immediately and routes on a background thread; poll "
        "get_project_status and read its 'routing_state' (running/complete/failed). "
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
        for event in run_workflow_streaming(req_path, project_name, config):
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
    if not pdir.exists():
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

    # Routing job state (for the async route_board flow), reconciled with disk.
    with _ROUTE_LOCK:
        job = dict(_ROUTE_JOBS.get(project_name)) if project_name in _ROUTE_JOBS else None

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
    if job is not None:
        result["routing_state"] = job["state"]
        # Elapsed time: live during run, final after completion/failure
        started = job.get("started_at")
        if job["state"] == "running" and started is not None:
            result["routing_elapsed_s"] = round(_time.monotonic() - started, 1)
        elif job.get("elapsed_s") is not None:
            result["routing_elapsed_s"] = job["elapsed_s"]
        # Live NCR iteration progress (only meaningful while running)
        if job["state"] == "running" and job.get("progress") is not None:
            result["routing_progress"] = job["progress"]
        if job["state"] == "complete" and job.get("result"):
            result["routing_result"] = job["result"]
        elif job["state"] == "failed":
            result["routing_error"] = job.get("error")
    else:
        result["routing_state"] = "complete" if routed else "none"

    # Design job state (async design_pcb flow), reconciled with disk.
    with _DESIGN_LOCK:
        djob = dict(_DESIGN_JOBS.get(project_name)) if project_name in _DESIGN_JOBS else None
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

    return {
        "success":         True,
        "project_name":    project_name,
        "netlist_path":    str(netlist_path),
        "component_count": n_comp,
        "net_count":       n_net,
        "warnings":        warnings,
        "next_step": (
            f"Netlist imported ({n_comp} components, {n_net} nets). "
            f"Call design_pcb with requirements_json={{\"project_name\": \"{project_name}\", "
            f"\"description\": \"{description or project_name}\"}} to run placement → "
            f"routing → DRC → export, or call get_project_status(\"{project_name}\") "
            f"to check the current state."
        ),
    }


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

    try:
        return stages.run_export(pdir, project_name, _get_config())
    except Exception as exc:
        return {"success": False, "error": f"Export failed: {exc}"}


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    """Run the MCP server (stdio transport)."""
    mcp.run()


if __name__ == "__main__":
    main()
