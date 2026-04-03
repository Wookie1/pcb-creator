"""CLI entry point for the orchestrator."""

import argparse
import json
import sys
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
except ImportError:
    # python-dotenv not installed — config.py has its own stdlib .env loader
    pass

from .config import OrchestratorConfig


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="pcb-creator",
        description="AI-driven PCB design with deterministic orchestration",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    # run command — full pipeline from requirements JSON
    run_parser = subparsers.add_parser("run", help="Run the design pipeline from a requirements JSON file")
    run_parser.add_argument(
        "--requirements",
        type=Path,
        required=True,
        help="Path to requirements JSON file",
    )
    run_parser.add_argument(
        "--project",
        type=str,
        default=None,
        help="Project name (lowercase_with_underscores). Auto-generated from requirements if omitted.",
    )
    run_parser.add_argument(
        "--base-dir",
        type=Path,
        default=None,
        help="Base directory (defaults to cwd)",
    )
    run_parser.add_argument(
        "--model",
        type=str,
        default=None,
        help="Override the LLM model for all agents",
    )
    run_parser.add_argument(
        "--attach",
        type=Path,
        action="append",
        default=[],
        help="Attach a file (DXF, sketch, photo) to copy into the project (repeatable)",
    )
    run_parser.add_argument(
        "--api-base",
        type=str,
        default=None,
        help="LLM API base URL (e.g. http://localhost:8000/v1 for local models)",
    )
    run_parser.add_argument(
        "--api-key",
        type=str,
        default=None,
        help="LLM API key (overrides PCB_LLM_API_KEY env var)",
    )
    run_parser.add_argument(
        "--no-thinking",
        action="store_true",
        default=False,
        help="Disable thinking/reasoning mode (fixes JSON parsing with Qwen thinking models)",
    )
    run_parser.add_argument(
        "--export-kicad",
        type=Path,
        nargs="?",
        const=True,  # flag present but no path = auto-generate path
        default=None,
        help="Export routed board to KiCad .kicad_pcb (optional: specify output path)",
    )
    run_parser.add_argument(
        "--agent-mode",
        action="store_true",
        default=False,
        help="Skip browser approval gate (for autonomous/agent workflows)",
    )
    run_parser.add_argument(
        "--skip-approval",
        action="store_true",
        default=False,
        help="Skip all approval gates entirely (for batch/CI runs)",
    )
    run_parser.add_argument(
        "--skip-qa",
        action="store_true",
        default=False,
        help="Skip per-step LLM QA reviews (validators still run)",
    )
    run_parser.add_argument(
        "--json-output",
        action="store_true",
        default=False,
        help="Print structured JSON result to stdout (for agent/script consumption)",
    )

    # gui command — Gradio web UI
    gui_parser = subparsers.add_parser("gui", help="Launch the Gradio web GUI")
    gui_parser.add_argument(
        "--port", type=int, default=7860, help="Port for Gradio server (default: 7860)"
    )
    gui_parser.add_argument(
        "--share", action="store_true", default=False, help="Create a public Gradio URL"
    )
    gui_parser.add_argument(
        "--base-dir", type=Path, default=None, help="Base directory (defaults to cwd)"
    )

    # design command — interactive: gather requirements then run pipeline
    design_parser = subparsers.add_parser(
        "design", help="Interactive: describe your circuit, then auto-run the pipeline"
    )
    design_parser.add_argument(
        "--project",
        type=str,
        required=True,
        help="Project name (lowercase_with_underscores)",
    )
    design_parser.add_argument(
        "--base-dir",
        type=Path,
        default=None,
        help="Base directory (defaults to cwd)",
    )
    design_parser.add_argument(
        "--model",
        type=str,
        default=None,
        help="Override the LLM model for all agents",
    )

    # import-kicad command — import a .kicad_pcb back into the pipeline
    import_parser = subparsers.add_parser(
        "import-kicad", help="Import a KiCad .kicad_pcb file back into the pipeline"
    )
    import_parser.add_argument(
        "--project",
        type=str,
        required=True,
        help="Project name (to find original routed/netlist files)",
    )
    import_parser.add_argument(
        "--kicad-file",
        type=Path,
        required=True,
        help="Path to the .kicad_pcb file to import",
    )
    import_parser.add_argument(
        "--base-dir",
        type=Path,
        default=None,
        help="Base directory (defaults to cwd)",
    )

    # validate command
    validate_parser = subparsers.add_parser(
        "validate", help="Validate an existing netlist"
    )
    validate_parser.add_argument(
        "netlist", type=Path, help="Path to netlist JSON file"
    )

    # schema command — print the requirements JSON schema
    subparsers.add_parser(
        "schema", help="Print the requirements JSON schema to stdout"
    )

    # mcp command — launch MCP server
    subparsers.add_parser(
        "mcp", help="Launch MCP server (stdio transport) for AI agent integration"
    )

    args = parser.parse_args(argv)

    if args.command == "run":
        return _run_pipeline(args)
    elif args.command == "schema":
        return _print_schema()

    elif args.command == "gui":
        return _launch_gui(args)

    elif args.command == "design":
        return _design_interactive(args)

    elif args.command == "import-kicad":
        return _import_kicad(args)

    elif args.command == "validate":
        return _validate(args.netlist)

    elif args.command == "mcp":
        from mcp_server import main as mcp_main
        mcp_main()
        return 0

    return 1


def _make_config(args) -> OrchestratorConfig:
    config = OrchestratorConfig.from_env(
        base_dir=getattr(args, "base_dir", None) or Path.cwd()
    )
    if model := getattr(args, "model", None):
        config.generate_model = model
        config.review_model = model
        config.gather_model = model
    if api_base := getattr(args, "api_base", None):
        config.api_base = api_base
    if api_key := getattr(args, "api_key", None):
        config.api_key = api_key
    if getattr(args, "no_thinking", False):
        config.llm_extra_body["thinking"] = False
    return config


def _run_pipeline(args) -> int:
    """Run the full pipeline from a requirements JSON file.

    When --json-output is set, prints a structured JSON result to stdout
    with success status, routing stats, DRC summary, and output file paths.
    """
    import re

    from .runner import run_workflow

    config = _make_config(args)
    if getattr(args, "export_kicad", None):
        config.export_kicad = args.export_kicad
    if getattr(args, "agent_mode", False):
        config.agent_mode = True
    if getattr(args, "skip_approval", False):
        config.skip_approval = True
    if getattr(args, "skip_qa", False):
        config.skip_qa = True

    # Auto-generate project name from requirements if not provided
    project_name = args.project
    if not project_name:
        try:
            req_data = json.loads(args.requirements.read_text())
            raw = req_data.get("project_name") or req_data.get("description", "pcb_project")
        except (json.JSONDecodeError, OSError):
            raw = args.requirements.stem
        project_name = re.sub(r"[^a-z0-9]+", "_", raw.lower().strip()).strip("_")[:60] or "pcb_project"

    json_output = getattr(args, "json_output", False)

    if json_output:
        # Use streaming runner to collect structured results
        from .runner import run_workflow_streaming

        req_path = args.requirements
        projects_dir = config.resolve(config.projects_dir)
        project_dir = projects_dir / project_name

        steps_completed = []
        errors = []
        last_event = None

        attach_files = getattr(args, "attach", []) or None

        try:
            for event in run_workflow_streaming(req_path, project_name, config, attach_files=attach_files):
                ev = event.get("event", "")
                if ev == "step_done":
                    steps_completed.append({
                        "step": event.get("step"),
                        "name": event.get("name"),
                        "success": event.get("success", False),
                    })
                elif ev == "error":
                    errors.append(event.get("message", "Unknown error"))
                last_event = event
        except Exception as exc:
            errors.append(f"Pipeline crashed: {exc}")

        success = last_event and last_event.get("event") == "complete" and last_event.get("success", False)

        result = {
            "success": success,
            "project_name": project_name,
            "project_dir": str(project_dir),
            "steps_completed": steps_completed,
            "errors": errors,
        }

        # Add routing stats
        routed_path = project_dir / f"{project_name}_routed.json"
        if routed_path.exists():
            routed = json.loads(routed_path.read_text())
            stats = routed.get("statistics", {})
            result["routing_stats"] = {
                "completion_pct": stats.get("completion_pct", 0),
                "total_nets": stats.get("total_nets", 0),
                "routed_nets": stats.get("routed_nets", 0),
                "via_count": stats.get("via_count", 0),
            }

        # Add DRC summary
        drc_path = project_dir / f"{project_name}_drc_report.json"
        if drc_path.exists():
            drc = json.loads(drc_path.read_text())
            result["drc_summary"] = {
                "passed": drc.get("passed", False),
                "errors": drc.get("statistics", {}).get("errors", 0),
                "warnings": drc.get("statistics", {}).get("warnings", 0),
            }

        # List output files
        output_dir = project_dir / "output"
        if output_dir.exists():
            result["output_files"] = [
                str(f) for f in sorted(output_dir.iterdir()) if f.is_file()
            ]

        print(json.dumps(result, indent=2))
        return 0 if success else 1
    else:
        success = run_workflow(
            args.requirements,
            project_name,
            config,
            attach_files=getattr(args, "attach", []) or None,
        )
        return 0 if success else 1


def _print_schema() -> int:
    """Print the requirements JSON schema to stdout."""
    from .gather.schema import REQUIREMENTS_SCHEMA
    print(json.dumps(REQUIREMENTS_SCHEMA, indent=2))
    return 0


def _launch_gui(args) -> int:
    """Launch the Gradio web GUI."""
    from .gradio_app import launch_gui

    base_dir = args.base_dir or Path.cwd()
    launch_gui(
        base_dir=base_dir,
        port=args.port,
        share=args.share,
    )
    return 0


def _design_interactive(args) -> int:
    """Interactive requirements gathering then pipeline execution."""
    from .gather.conversation import RequirementsGatherer
    from .llm.litellm_client import LiteLLMClient
    from .prompts.builder import PromptBuilder

    from .cache import ComponentCache
    from optimizers.pad_geometry import configure_lookup

    config = _make_config(args)
    llm = LiteLLMClient(config.gather_model, api_base=config.api_base, api_key=config.api_key, timeout=config.llm_timeout)
    prompt_builder = PromptBuilder(config.base_dir)
    cache = ComponentCache(config.component_cache_path)

    # Build KiCad library index if path is configured
    kicad_index = None
    if config.kicad_library_path:
        from exporters.kicad_mod_parser import KiCadLibraryIndex
        kicad_index = KiCadLibraryIndex(config.kicad_library_path)

    # Set module-level defaults so all build_pad_map() calls benefit
    configure_lookup(kicad_index=kicad_index, cache=cache)

    gatherer = RequirementsGatherer(
        llm, prompt_builder,
        cache=cache, max_workers=config.llm_enrichment_workers,
    )
    requirements = gatherer.gather_interactive()

    if requirements is None:
        print("\nRequirements gathering cancelled.")
        return 1

    # Save requirements to a temp file and run the pipeline
    projects_dir = config.resolve(config.projects_dir)
    projects_dir.mkdir(parents=True, exist_ok=True)
    req_path = projects_dir / f"{args.project}_requirements_input.json"
    req_path.write_text(json.dumps(requirements, indent=2))

    # Override project_name from the gathered requirements if present
    project_name = requirements.get("project_name", args.project)

    from .runner import run_workflow
    success = run_workflow(req_path, project_name, config)
    return 0 if success else 1


def _import_kicad(args) -> int:
    """Import a KiCad .kicad_pcb file back into the pipeline."""
    from exporters.kicad_importer import import_kicad_pcb
    from visualizers.placement_viewer import generate_html

    base_dir = args.base_dir or Path.cwd()
    project_dir = base_dir / "projects" / args.project
    kicad_file = args.kicad_file

    if not kicad_file.exists():
        print(f"Error: KiCad file not found: {kicad_file}")
        return 1

    # Find original routed and netlist files
    routed_path = project_dir / f"{args.project}_routed.json"
    netlist_path = project_dir / f"{args.project}_netlist.json"

    if not routed_path.exists():
        print(f"Error: Original routed file not found: {routed_path}")
        return 1
    if not netlist_path.exists():
        print(f"Error: Netlist file not found: {netlist_path}")
        return 1

    print(f"Importing KiCad file: {kicad_file}")
    original_routed = json.loads(routed_path.read_text())
    netlist = json.loads(netlist_path.read_text())

    # Import
    imported = import_kicad_pcb(kicad_file, original_routed, netlist)

    # Save imported routed JSON
    imported_path = project_dir / f"{args.project}_routed_imported.json"
    imported_path.write_text(json.dumps(imported, indent=2))
    print(f"  Imported routed JSON: {imported_path}")

    # Print statistics
    stats = imported["routing"]["statistics"]
    print(f"  Nets: {stats['routed_nets']}/{stats['total_nets']} ({stats['completion_pct']}%)")
    print(f"  Traces: {len(imported['routing']['traces'])}")
    print(f"  Vias: {stats['via_count']}")
    unrouted = imported["routing"]["unrouted_nets"]
    if unrouted:
        print(f"  Unrouted: {', '.join(unrouted)}")
    else:
        print("  All nets routed!")

    # Generate updated visualization
    bom_path = project_dir / f"{args.project}_bom.json"
    bom = json.loads(bom_path.read_text()) if bom_path.exists() else None
    placement = {k: v for k, v in imported.items()
                 if k in ("version", "project_name", "board", "placements")}
    html = generate_html(placement, netlist, bom, routed=imported)
    html_path = project_dir / f"{args.project}_imported_view.html"
    html_path.write_text(html)
    print(f"  Visualization: {html_path}")

    return 0


def _validate(netlist_path: Path) -> int:
    """Run the validator on an existing netlist."""
    import subprocess

    config = OrchestratorConfig.from_env()
    validator = config.resolve(config.validator_path)

    result = subprocess.run(
        [sys.executable, str(validator), str(netlist_path)],
        capture_output=True,
        text=True,
    )

    try:
        output = json.loads(result.stdout)
        print(json.dumps(output, indent=2))
        return 0 if output.get("valid") else 1
    except json.JSONDecodeError:
        print(f"Validator error: {result.stderr or result.stdout}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
