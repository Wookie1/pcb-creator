"""Main workflow runner — deterministic step execution."""

import json
from collections.abc import Generator
from pathlib import Path

from .config import OrchestratorConfig
from .project import ProjectManager

import logging

logger = logging.getLogger(__name__)


def run_workflow(
    requirements_path: Path,
    project_name: str,
    config: OrchestratorConfig,
    attach_files: list[Path] | None = None,
) -> bool:
    """Run the full workflow from requirements file to completion.

    Returns True if all steps succeeded, False otherwise.
    """
    from .llm.litellm_client import LiteLLMClient
    from .project import ProjectManager
    from .prompts.builder import PromptBuilder
    from .steps.step_0_requirements import RequirementsStep
    from .steps.step_1_schematic import SchematicStep
    from .steps.step_2_bom import BOMStep
    from .steps.step_3_layout import LayoutStep

    # Initialize shared resources
    projects_dir = config.resolve(config.projects_dir)
    project = ProjectManager(project_name, projects_dir)
    llm = LiteLLMClient(config.generate_model, api_base=config.api_base, api_key=config.api_key, extra_body=config.llm_extra_body, timeout=config.llm_timeout)
    prompt_builder = PromptBuilder(config.base_dir)

    # Step 0: Requirements
    logger.info(f"\n[Step 0: Requirements]")
    step0 = RequirementsStep(project, llm, prompt_builder, config)
    result = step0.execute(
        requirements_path=requirements_path,
        attach_files=attach_files,
    )

    if not result.success:
        logger.info(f"  FAILED: {result.error}")
        return False

    logger.info(f"  Created {result.output_path}")
    logger.info(f"  COMPLETE\n")

    # Step 1: Schematic/Netlist
    logger.info(f"[Step 1: Schematic/Netlist]")
    review_llm = LiteLLMClient(config.review_model, api_base=config.api_base, api_key=config.api_key, timeout=config.llm_timeout)
    step1 = SchematicStep(project, llm, prompt_builder, config)
    # Use review model for QA if different from generate model
    result = step1.execute()

    if not result.success:
        logger.info(f"\n  BLOCKED: {result.error}")
        _print_blocked(project_name, 1, "Schematic/Netlist", result.error)
        return False

    logger.info(f"  COMPLETE\n")

    # Step 2: Component Selection (BOM)
    logger.info(f"[Step 2: Component Selection]")
    step2 = BOMStep(project, llm, prompt_builder, config)
    result = step2.execute()

    if not result.success:
        logger.info(f"\n  BLOCKED: {result.error}")
        _print_blocked(project_name, 2, "Component Selection", result.error)
        return False

    logger.info(f"  COMPLETE\n")

    # Step 3: Board Layout (Placement)
    logger.info(f"[Step 3: Board Layout]")
    step3 = LayoutStep(project, llm, prompt_builder, config)
    result = step3.execute()

    if not result.success:
        logger.info(f"\n  BLOCKED: {result.error}")
        _print_blocked(project_name, 3, "Board Layout", result.error)
        return False

    logger.info(f"  COMPLETE\n")

    # Post-placement optimization
    if config.enable_optimizer and result.success:
        logger.info(f"[Optimizer: Placement SA]")
        import sys as _sys
        _sys.path.insert(0, str(config.base_dir))
        from optimizers.placement_optimizer import optimize_placement, SAConfig
        from optimizers.fiducials import add_fiducials_to_placement

        placement_path = project.get_output_path(
            f"{project_name}_placement.json"
        )
        netlist_path = project.get_output_path(
            f"{project_name}_netlist.json"
        )

        placement_data = json.loads(placement_path.read_text())
        netlist_data = json.loads(netlist_path.read_text())

        # Save pre-optimization backup
        pre_opt_data = json.dumps(placement_data, indent=2)

        sa_config = SAConfig(
            max_iterations=config.optimizer_iterations,
            seed=config.optimizer_seed,
        )

        optimized = optimize_placement(placement_data, netlist_data, sa_config)
        optimized = add_fiducials_to_placement(optimized)

        # Write optimized placement
        placement_path.write_text(json.dumps(optimized, indent=2))

        # Re-validate to confirm DRC compliance
        from validators.validate_placement import validate_placement as run_validation
        val_result = run_validation(str(placement_path), str(netlist_path))
        if not val_result["valid"]:
            logger.info(f"  WARNING: Optimized placement failed validation — reverting")
            for err in val_result["errors"]:
                logger.info(f"    - {err}")
            placement_path.write_text(pre_opt_data)
            logger.info(f"  Reverted to pre-optimization placement")
        else:
            n_total = len(optimized.get("placements", []))
            n_fids = sum(
                1 for p in optimized.get("placements", [])
                if p.get("component_type") == "fiducial"
            )
            logger.info(f"  Validation: PASSED ({n_total} components, {n_fids} fiducials)")

        logger.info("")

    # Step 4: Routing
    logger.info(f"[Step 4: Routing]")
    import sys as _sys
    _sys.path.insert(0, str(config.base_dir))
    from . import stages

    netlist_path = project.get_output_path(f"{project_name}_netlist.json")
    netlist_data = json.loads(netlist_path.read_text())

    route_result = stages.run_routing(
        project.project_dir, project_name, config, log=print
    )
    if not route_result.get("success"):
        logger.info(f"  Routing FAILED: {route_result.get('error')}")
        return False

    # Re-read the routed board: the approval gate, vision review, KiCad export,
    # and output generation below all need it in memory.
    routed_path = project.get_output_path(f"{project_name}_routed.json")
    routed = json.loads(routed_path.read_text())

    logger.info("")

    # Step 5: DRC
    logger.info(f"[Step 5: DRC]")
    drc_report = stages.run_drc(project.project_dir, project_name, config, log=print)

    logger.info("")

    # Post-routing approval gate
    bom_path = project.get_output_path(f"{project_name}_bom.json")
    bom_data = json.loads(bom_path.read_text()) if bom_path.exists() else None

    if config.skip_approval:
        logger.info(f"[Review & Approval] Skipped (--skip-approval)")
    elif config.agent_mode:
        # Agent mode: vision-based autonomous review
        logger.info(f"[Review & Approval] Vision-based autonomous review")
        from orchestrator.vision_review import run_vision_review

        review_result = run_vision_review(
            routed, netlist_data, bom_data, drc_report, config, project,
        )
        if review_result == "approved":
            logger.info(f"  Vision review: APPROVED")
        elif review_result == "escalated":
            logger.info(f"  Vision review: Escalated — auto-approving (agent mode)")
    else:
        # Serve the visualizer with DRC results for user review
        logger.info(f"[Review & Approval]")
        from orchestrator.approval_server import serve_approval_gate

        approval = serve_approval_gate(
            project_name,
            routed,
            netlist_data,
            bom_data,
            project.project_dir,
            drc_report=drc_report,
        )

        if approval == "continue":
            logger.info(f"  Approved")

    # KiCad export (if requested via CLI flag)
    if config.export_kicad:
        from exporters.kicad_exporter import export_kicad_pcb

        if isinstance(config.export_kicad, Path) and str(config.export_kicad) != "True":
            kicad_path = config.export_kicad
        else:
            kicad_path = project.get_output_path(f"{project_name}.kicad_pcb")

        export_kicad_pcb(routed, netlist_data, kicad_path)
        logger.info(f"  KiCad export: {kicad_path}")

    logger.info("")

    # Step 6: Output Generation
    logger.info(f"[Step 6: Output Generation]")
    stages.run_export(project.project_dir, project_name, config, log=print)

    logger.info("")

    # Final delivery
    _print_delivery(project, result)
    return True


STEP_NAMES = {
    0: "Requirements",
    1: "Schematic/Netlist",
    2: "Component Selection",
    3: "Board Layout",
    4: "Routing",
    5: "DRC",
    6: "Output Generation",
}


def run_workflow_streaming(
    requirements_path: Path,
    project_name: str,
    config: OrchestratorConfig,
    attach_files: list[Path] | None = None,
    progress_callback=None,
) -> Generator[dict, None, None]:
    """Generator version of run_workflow that yields events for Gradio UI updates.

    Yields dicts with keys:
        event: "step_start" | "step_done" | "viewer_update" | "approval_needed" | "complete" | "error"
        step: int (step number)
        name: str (step name)
        success: bool (for step_done/complete)
        html: str (for viewer_update/approval_needed)
        message: str (for error)

    progress_callback: optional callable(dict) for INTRA-step progress. The
        generator can only yield between steps, but a long step (e.g. the
        schematic's chunked components→ports→nets generation) blocks the loop
        for many minutes. Steps invoke this callback directly so a polling agent
        sees sub-phase progress in real time. None = no intra-step reporting.
    """
    from .llm.litellm_client import LiteLLMClient
    from .project import ProjectManager
    from .prompts.builder import PromptBuilder
    from .steps.step_0_requirements import RequirementsStep
    from .steps.step_1_schematic import SchematicStep
    from .steps.step_2_bom import BOMStep
    from .steps.step_3_layout import LayoutStep

    import sys as _sys
    _sys.path.insert(0, str(config.base_dir))

    projects_dir = config.resolve(config.projects_dir)
    project = ProjectManager(project_name, projects_dir)
    llm = LiteLLMClient(
        config.generate_model,
        api_base=config.api_base,
        api_key=config.api_key,
        extra_body=config.llm_extra_body,
        timeout=config.llm_timeout,
    )
    prompt_builder = PromptBuilder(config.base_dir)

    from visualizers.placement_viewer import generate_html
    from visualizers.netlist_viewer import generate_netlist_html

    # --- Step 0: Requirements ---
    yield {"event": "step_start", "step": 0, "name": STEP_NAMES[0]}
    step0 = RequirementsStep(project, llm, prompt_builder, config)
    result = step0.execute(
        requirements_path=requirements_path,
        attach_files=attach_files,
    )
    if not result.success:
        yield {"event": "step_done", "step": 0, "name": STEP_NAMES[0], "success": False}
        yield {"event": "error", "step": 0, "message": result.error or "Requirements failed"}
        return
    yield {"event": "step_done", "step": 0, "name": STEP_NAMES[0], "success": True}

    # --- Step 1: Schematic/Netlist ---
    yield {"event": "step_start", "step": 1, "name": STEP_NAMES[1]}
    step1 = SchematicStep(project, llm, prompt_builder, config)
    # Surface intra-step chunk progress (components→ports→nets) to a polling
    # agent; the generator itself can't yield while step1.execute() blocks.
    step1.progress_callback = progress_callback
    result = step1.execute()
    if not result.success:
        yield {"event": "step_done", "step": 1, "name": STEP_NAMES[1], "success": False}
        yield {"event": "error", "step": 1, "message": result.error or "Schematic failed"}
        return
    yield {"event": "step_done", "step": 1, "name": STEP_NAMES[1], "success": True}

    # Yield netlist block diagram
    netlist_path = project.get_output_path(f"{project_name}_netlist.json")
    if netlist_path.exists():
        netlist_data_for_viewer = json.loads(netlist_path.read_text())
        html = generate_netlist_html(netlist_data_for_viewer)
        yield {"event": "viewer_update", "html": html}

    # --- Step 2: Component Selection ---
    yield {"event": "step_start", "step": 2, "name": STEP_NAMES[2]}
    step2 = BOMStep(project, llm, prompt_builder, config)
    result = step2.execute()
    if not result.success:
        yield {"event": "step_done", "step": 2, "name": STEP_NAMES[2], "success": False}
        yield {"event": "error", "step": 2, "message": result.error or "BOM failed"}
        return
    yield {"event": "step_done", "step": 2, "name": STEP_NAMES[2], "success": True}

    # --- Step 3: Board Layout ---
    yield {"event": "step_start", "step": 3, "name": STEP_NAMES[3]}
    step3 = LayoutStep(project, llm, prompt_builder, config)
    result = step3.execute()
    if not result.success:
        yield {"event": "step_done", "step": 3, "name": STEP_NAMES[3], "success": False}
        yield {"event": "error", "step": 3, "message": result.error or "Layout failed"}
        return
    yield {"event": "step_done", "step": 3, "name": STEP_NAMES[3], "success": True}

    # Post-placement optimization
    if config.enable_optimizer:
        from optimizers.placement_optimizer import optimize_placement, SAConfig
        from optimizers.fiducials import add_fiducials_to_placement

        placement_path = project.get_output_path(f"{project_name}_placement.json")
        netlist_path = project.get_output_path(f"{project_name}_netlist.json")
        placement_data = json.loads(placement_path.read_text())
        netlist_data = json.loads(netlist_path.read_text())
        pre_opt_data = json.dumps(placement_data, indent=2)

        sa_config = SAConfig(
            max_iterations=config.optimizer_iterations,
            seed=config.optimizer_seed,
        )
        optimized = optimize_placement(placement_data, netlist_data, sa_config)
        optimized = add_fiducials_to_placement(optimized)
        placement_path.write_text(json.dumps(optimized, indent=2))

        from validators.validate_placement import validate_placement as run_validation
        val_result = run_validation(str(placement_path), str(netlist_path))
        if not val_result["valid"]:
            placement_path.write_text(pre_opt_data)

    # Yield placement viewer
    placement_path = project.get_output_path(f"{project_name}_placement.json")
    netlist_path = project.get_output_path(f"{project_name}_netlist.json")
    placement_data = json.loads(placement_path.read_text())
    netlist_data = json.loads(netlist_path.read_text())
    bom_path = project.get_output_path(f"{project_name}_bom.json")
    bom_data = json.loads(bom_path.read_text()) if bom_path.exists() else None

    html = generate_html(placement_data, netlist_data, bom_data, embed_mode=True)
    yield {"event": "viewer_update", "html": html}

    # --- Step 4: Routing ---
    yield {"event": "step_start", "step": 4, "name": STEP_NAMES[4]}
    from . import stages

    route_result = stages.run_routing(project.project_dir, project_name, config)
    if not route_result.get("success"):
        yield {"event": "step_done", "step": 4, "name": STEP_NAMES[4], "success": False}
        yield {"event": "error", "step": 4, "message": route_result.get("error") or "Routing failed"}
        return

    # Re-read the routed board for the viewer, DRC, vision review and exports below.
    routed_path = project.get_output_path(f"{project_name}_routed.json")
    routed = json.loads(routed_path.read_text())

    project.update_status(
        4, "COMPLETE",
        validator_errors=route_result.get("validation_errors") or None,
        validator_warnings=route_result.get("validation_warnings") or None,
    )
    yield {"event": "step_done", "step": 4, "name": STEP_NAMES[4], "success": True}

    # Yield routed viewer
    html = generate_html(routed, netlist_data, bom_data, routed=routed, embed_mode=True)
    yield {"event": "viewer_update", "html": html}

    # --- Step 5: DRC ---
    yield {"event": "step_start", "step": 5, "name": STEP_NAMES[5]}
    drc_report = stages.run_drc(project.project_dir, project_name, config)
    _drc_errors, _drc_warnings = [], []
    for _check in drc_report.get("checks", []):
        for _v in _check.get("violations", []):
            msg = _v.get("message", "")
            if _v.get("severity") == "error":
                _drc_errors.append(msg)
            elif _v.get("severity") == "warning":
                _drc_warnings.append(msg)
    project.update_status(
        5, "COMPLETE",
        validator_errors=_drc_errors or None,
        validator_warnings=_drc_warnings or None,
    )
    yield {"event": "step_done", "step": 5, "name": STEP_NAMES[5], "success": True}

    # Yield final viewer with DRC
    html = generate_html(routed, netlist_data, bom_data, routed=routed, drc_report=drc_report, embed_mode=True)
    yield {"event": "viewer_update", "html": html}

    # Vision-based autonomous review (agent_mode is always True in Gradio)
    # Skip vision review when skip_qa is set — calling agent reviews via get_board_image
    if config.skip_qa:
        logger.info("[Review] Vision review skipped (skip_qa mode)")
        yield {"event": "vision_review_start"}
        yield {"event": "vision_review_done", "result": "approved"}
    else:
        yield {"event": "vision_review_start"}
        from orchestrator.vision_review import run_vision_review

        review_result = run_vision_review(
            routed, netlist_data, bom_data, drc_report, config, project,
        )
        yield {"event": "vision_review_done", "result": review_result}

        if review_result != "approved":
            # Escalate to human — yield approval_needed to pause generator
            yield {"event": "approval_needed", "html": html}

    # --- Step 6: Output Generation ---
    yield {"event": "step_start", "step": 6, "name": STEP_NAMES[6]}
    stages.run_export(project.project_dir, project_name, config)

    if config.export_kicad:
        from exporters.kicad_exporter import export_kicad_pcb
        if isinstance(config.export_kicad, Path) and str(config.export_kicad) != "True":
            kicad_path = config.export_kicad
        else:
            kicad_path = project.get_output_path(f"{project_name}.kicad_pcb")
        export_kicad_pcb(routed, netlist_data, kicad_path)

    yield {"event": "step_done", "step": 6, "name": STEP_NAMES[6], "success": True}
    yield {"event": "complete", "success": True}


def _print_blocked(project_name: str, step: int, step_name: str, error: str) -> None:
    logger.info(f"""
WORKFLOW BLOCKED
{'=' * 46}
Project : {project_name}
Step    : {step} — {step_name}
Reason  : {error}

The workflow has stopped. Please review the issues above.
{'=' * 46}
""")




def _print_delivery(project: ProjectManager, result) -> None:
    """Print final delivery summary."""
    # Read netlist to summarize
    netlist_path = project.get_output_path(f"{project.project_name}_netlist.json")
    if netlist_path.exists():
        netlist = json.loads(netlist_path.read_text())
        elements = netlist.get("elements", [])
        components = [e for e in elements if e.get("element_type") == "component"]
        nets = [e for e in elements if e.get("element_type") == "net"]
        designators = ", ".join(e.get("designator", "?") for e in components)
        net_names = ", ".join(e.get("name", "?") for e in nets)
    else:
        components, nets = [], []
        designators, net_names = "N/A", "N/A"

    # Read BOM to summarize
    bom_path = project.get_output_path(f"{project.project_name}_bom.json")
    bom_count = 0
    if bom_path.exists():
        bom = json.loads(bom_path.read_text())
        bom_count = len(bom.get("bom", []))

    # Read placement to summarize
    placement_path = project.get_output_path(f"{project.project_name}_placement.json")
    placement_count = 0
    board_info = "N/A"
    if placement_path.exists():
        placement = json.loads(placement_path.read_text())
        placement_count = len(placement.get("placements", []))
        board = placement.get("board", {})
        board_info = f"{board.get('width_mm', '?')} x {board.get('height_mm', '?')} mm"

    qa_summary = ""
    if result.qa_report:
        qa_summary = result.qa_report.get("summary", "")

    warnings = "None"
    if result.qa_report and result.qa_report.get("issues"):
        warnings = "\n  ".join(result.qa_report["issues"])

    logger.info(f"""
DESIGN COMPLETE
{'=' * 46}
Project  : {project.project_name}
Status   : All steps completed and validated

DELIVERABLES
  Netlist    : {netlist_path}
  Components : {len(components)} ({designators})
  Nets       : {len(nets)} ({net_names})
  BOM        : {bom_path} ({bom_count} items)
  Placement  : {placement_path} ({placement_count} components, board {board_info})

QA SUMMARY
  {qa_summary}

WARNINGS
  {warnings}
{'=' * 46}
""")
