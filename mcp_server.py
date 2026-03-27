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
        "PCB design tools. Use design_pcb to create a PCB from a circuit description. "
        "Use list_projects, get_project_status, get_drc_report, export_kicad, and "
        "get_board_image to inspect and export completed designs."
    ),
)


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
    settings: dict | None = None,
) -> dict:
    """Design a PCB from a natural language circuit description.

    Runs the full pipeline: requirements → schematic → BOM → placement →
    routing → DRC → output generation. Uses vision-based autonomous review.

    Args:
        description: Natural language circuit description (e.g. "Arduino Uno clone
            with ATmega328P, 16MHz crystal, USB-B connector, and 6 LEDs") OR
            a JSON string of structured requirements.
        project_name: Optional project slug. Auto-generated from description if omitted.
        settings: Optional config overrides: {"model": "...", "router_engine": "...",
            "max_rework_attempts": 5}

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

    # Generate project name if not provided
    if not project_name:
        project_name = _slugify(description)

    # Write requirements to a temp file
    # Try parsing as JSON first; if it fails, wrap as natural language
    try:
        requirements = json.loads(description)
    except (json.JSONDecodeError, TypeError):
        requirements = {"description": description}

    projects_dir = _get_projects_dir()
    project_dir = projects_dir / project_name
    project_dir.mkdir(parents=True, exist_ok=True)

    req_path = project_dir / f"{project_name}_requirements_input.json"
    req_path.write_text(json.dumps(requirements, indent=2))

    # Run the streaming pipeline, collecting events
    from orchestrator.runner import run_workflow_streaming

    steps_completed = []
    errors = []
    last_event = None

    for event in run_workflow_streaming(req_path, project_name, config):
        ev = event.get("event", "")
        if ev == "step_done":
            steps_completed.append({
                "step": event.get("step"),
                "name": event.get("name"),
                "success": event.get("success", False),
            })
        elif ev == "error":
            errors.append(event.get("message", "Unknown error"))
        elif ev == "approval_needed":
            # In MCP mode with agent_mode=True, this means vision review
            # escalated. We can't do human approval in MCP, so continue.
            pass
        last_event = event

    success = last_event and last_event.get("event") == "complete" and last_event.get("success", False)

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

    # STATUS.json
    status_path = pdir / "STATUS.json"
    if status_path.exists():
        try:
            result["status"] = json.loads(status_path.read_text())
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
# Entry point
# ---------------------------------------------------------------------------

def main():
    """Run the MCP server (stdio transport)."""
    mcp.run()


if __name__ == "__main__":
    main()
