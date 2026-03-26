"""Main workflow runner — deterministic step execution."""

import json
from collections.abc import Generator
from pathlib import Path

from .config import OrchestratorConfig


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
    llm = LiteLLMClient(config.generate_model, api_base=config.api_base, api_key=config.api_key, extra_body=config.llm_extra_body)
    prompt_builder = PromptBuilder(config.base_dir)

    # Step 0: Requirements
    print(f"\n[Step 0: Requirements]")
    step0 = RequirementsStep(project, llm, prompt_builder, config)
    result = step0.execute(
        requirements_path=requirements_path,
        attach_files=attach_files,
    )

    if not result.success:
        print(f"  FAILED: {result.error}")
        return False

    print(f"  Created {result.output_path}")
    print(f"  COMPLETE\n")

    # Step 1: Schematic/Netlist
    print(f"[Step 1: Schematic/Netlist]")
    review_llm = LiteLLMClient(config.review_model, api_base=config.api_base, api_key=config.api_key)
    step1 = SchematicStep(project, llm, prompt_builder, config)
    # Use review model for QA if different from generate model
    result = step1.execute()

    if not result.success:
        print(f"\n  BLOCKED: {result.error}")
        _print_blocked(project_name, 1, "Schematic/Netlist", result.error)
        return False

    print(f"  COMPLETE\n")

    # Step 2: Component Selection (BOM)
    print(f"[Step 2: Component Selection]")
    step2 = BOMStep(project, llm, prompt_builder, config)
    result = step2.execute()

    if not result.success:
        print(f"\n  BLOCKED: {result.error}")
        _print_blocked(project_name, 2, "Component Selection", result.error)
        return False

    print(f"  COMPLETE\n")

    # Step 3: Board Layout (Placement)
    print(f"[Step 3: Board Layout]")
    step3 = LayoutStep(project, llm, prompt_builder, config)
    result = step3.execute()

    if not result.success:
        print(f"\n  BLOCKED: {result.error}")
        _print_blocked(project_name, 3, "Board Layout", result.error)
        return False

    print(f"  COMPLETE\n")

    # Post-placement optimization
    if config.enable_optimizer and result.success:
        print(f"[Optimizer: Placement SA]")
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
            print(f"  WARNING: Optimized placement failed validation — reverting")
            for err in val_result["errors"]:
                print(f"    - {err}")
            placement_path.write_text(pre_opt_data)
            print(f"  Reverted to pre-optimization placement")
        else:
            n_total = len(optimized.get("placements", []))
            n_fids = sum(
                1 for p in optimized.get("placements", [])
                if p.get("component_type") == "fiducial"
            )
            print(f"  Validation: PASSED ({n_total} components, {n_fids} fiducials)")

        print()

    # Step 4: Routing
    print(f"[Step 4: Routing]")
    import sys as _sys
    _sys.path.insert(0, str(config.base_dir))
    from validators.validate_routing import validate_routing as run_routing_validation

    placement_path = project.get_output_path(f"{project_name}_placement.json")
    netlist_path = project.get_output_path(f"{project_name}_netlist.json")

    placement_data = json.loads(placement_path.read_text())
    netlist_data = json.loads(netlist_path.read_text())

    # Load manufacturing/DFM rules from requirements
    copper_oz = 0.5  # default
    mfg_rules: dict = {}
    req_json_path = project.get_output_path(f"{project_name}_requirements.json")
    if req_json_path.exists():
        try:
            req_data = json.loads(req_json_path.read_text())
            copper_oz = req_data.get("board", {}).get("copper_weight_oz", 0.5)
            mfg = req_data.get("manufacturing", {})
            if mfg:
                # If a manufacturer profile is specified, load it as base
                manufacturer = mfg.get("manufacturer", "")
                if manufacturer:
                    from validators.engineering_constants import get_dfm_profile
                    mfg_rules = get_dfm_profile(manufacturer)
                    print(f"  DFM profile: {mfg_rules.get('description', manufacturer)}")
                # Override with any explicit values from requirements
                for key in ("trace_width_min_mm", "clearance_min_mm",
                            "via_drill_min_mm", "via_diameter_min_mm"):
                    if key in mfg:
                        mfg_rules[key] = mfg[key]
        except Exception:
            pass

    # Build common design rules from DFM
    router_kwargs: dict = {"copper_weight_oz": copper_oz}
    if mfg_rules:
        # DFM minimums override defaults only if they're more restrictive
        # (i.e., we use whichever is LARGER: DFM minimum or electrical requirement)
        if "trace_width_min_mm" in mfg_rules:
            tw_min = mfg_rules["trace_width_min_mm"]
            router_kwargs["trace_width_signal_mm"] = max(0.25, tw_min)
            router_kwargs["trace_width_power_mm"] = max(0.5, tw_min)
            router_kwargs["trace_width_ground_mm"] = max(0.5, tw_min)
        if "clearance_min_mm" in mfg_rules:
            router_kwargs["clearance_mm"] = max(0.2, mfg_rules["clearance_min_mm"])
        if "via_drill_min_mm" in mfg_rules:
            router_kwargs["via_drill_mm"] = max(0.3, mfg_rules["via_drill_min_mm"])
        if "via_diameter_min_mm" in mfg_rules:
            router_kwargs["via_diameter_mm"] = max(0.6, mfg_rules["via_diameter_min_mm"])

    routed = None

    # Try Freerouting engine first (if configured)
    if config.router_engine == "freerouting":
        try:
            from optimizers.freerouter import route_with_freerouting
            print("  Engine: Freerouting")

            dsn_config = {
                "trace_width_mm": router_kwargs.get("trace_width_signal_mm", 0.25),
                "clearance_mm": router_kwargs.get("clearance_mm", 0.2),
                "via_drill_mm": router_kwargs.get("via_drill_mm", 0.3),
                "via_diameter_mm": router_kwargs.get("via_diameter_mm", 0.6),
            }

            routed = route_with_freerouting(
                placement_data, netlist_data,
                jar_path=config.freerouting_jar_path,
                timeout_s=config.freerouting_timeout_s,
                exclude_nets=["GND"],
                dsn_config=dsn_config,
            )

            # Apply copper fills and silkscreen
            from optimizers.router import apply_copper_fills, RouterConfig
            fill_config = RouterConfig(**router_kwargs)
            routed = apply_copper_fills(routed, netlist_data, fill_config)
        except Exception as e:
            print(f"  Freerouting FAILED: {e}")
            print(f"  Falling back to built-in router...")
            routed = None

    # Fallback to built-in router
    if routed is None:
        from optimizers.router import route_board, RouterConfig
        if config.router_engine == "freerouting":
            print("  Engine: Built-in (fallback)")
        else:
            print("  Engine: Built-in")
        router_config = RouterConfig(**router_kwargs)
        routed = route_board(placement_data, netlist_data, router_config)

    routed_path = project.get_output_path(f"{project_name}_routed.json")
    routed_path.write_text(json.dumps(routed, indent=2))

    # Validate routing
    val_result = run_routing_validation(str(routed_path), str(netlist_path))
    stats = routed.get("routing", {}).get("statistics", {})

    if not val_result["valid"]:
        print(f"  Routing validation FAILED")
        for err in val_result["errors"][:5]:
            print(f"    - {err}")
    else:
        print(f"  Routed: {stats.get('routed_nets', 0)}/{stats.get('total_nets', 0)} nets "
              f"({stats.get('completion_pct', 0)}%)")
        print(f"  Trace length: {stats.get('total_trace_length_mm', 0):.1f}mm  "
              f"Vias: {stats.get('via_count', 0)}")
        if stats.get("unrouted_nets", 0) > 0:
            unrouted = routed.get("routing", {}).get("unrouted_nets", [])
            print(f"  WARNING: {len(unrouted)} nets unrouted: {', '.join(unrouted)}")

    overrides = routed.get("routing", {}).get("trace_width_overrides", {})
    if overrides:
        print(f"  IPC-2221 trace upsizes: {len(overrides)} nets")

    print()

    # Step 5: DRC
    print(f"[Step 5: DRC]")
    from validators.drc_report import run_drc

    req_data = None
    req_json_path = project.get_output_path(f"{project_name}_requirements.json")
    if req_json_path.exists():
        try:
            req_data = json.loads(req_json_path.read_text())
        except Exception:
            pass

    drc_report = run_drc(routed, netlist_data, req_data)
    drc_path = project.get_output_path(f"{project_name}_drc_report.json")
    drc_path.write_text(json.dumps(drc_report, indent=2))

    if drc_report["passed"]:
        print(f"  DRC: PASSED — {drc_report['summary']}")
    else:
        print(f"  DRC: FAILED — {drc_report['summary']}")
        for check in drc_report["checks"]:
            if not check["passed"]:
                for v in check["violations"][:3]:
                    print(f"    {v['severity'].upper()}: {v['message']}")
                remaining = len(check["violations"]) - 3
                if remaining > 0:
                    print(f"    ... and {remaining} more {check['rule']} violations")

    print()

    # Post-routing approval gate
    bom_path = project.get_output_path(f"{project_name}_bom.json")
    bom_data = json.loads(bom_path.read_text()) if bom_path.exists() else None

    if config.agent_mode:
        # Agent mode: skip browser approval gate
        print(f"[Review & Approval] Skipped (agent mode)")
    else:
        # Serve the visualizer with DRC results for user review
        print(f"[Review & Approval]")
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
            print(f"  Approved")

    # KiCad export (if requested via CLI flag)
    if config.export_kicad:
        from exporters.kicad_exporter import export_kicad_pcb

        if isinstance(config.export_kicad, Path) and str(config.export_kicad) != "True":
            kicad_path = config.export_kicad
        else:
            kicad_path = project.get_output_path(f"{project_name}.kicad_pcb")

        export_kicad_pcb(routed, netlist_data, kicad_path)
        print(f"  KiCad export: {kicad_path}")

    print()

    # Step 6: Output Generation
    print(f"[Step 6: Output Generation]")
    from exporters.gerber_exporter import export_gerbers, export_drill, create_output_package
    from exporters.bom_csv_exporter import export_bom_csv, export_pick_and_place
    from exporters.step_exporter import export_step

    output_dir = project.project_dir / "output"
    output_dir.mkdir(exist_ok=True)

    # Gerber layers
    gerber_files = export_gerbers(routed, netlist_data, output_dir)
    print(f"  Gerber layers: {len(gerber_files)} files")

    # Excellon drill
    drill_path = export_drill(routed, netlist_data, output_dir / f"{project_name}.drl")
    print(f"  Drill file: {drill_path.name}")

    # BOM CSV
    bom_path = project.get_output_path(f"{project_name}_bom.json")
    bom_for_csv = json.loads(bom_path.read_text()) if bom_path.exists() else bom_data
    bom_csv_path = export_bom_csv(bom_for_csv, output_dir / f"{project_name}_bom.csv")
    print(f"  BOM: {bom_csv_path.name}")

    # Pick-and-place (CPL)
    cpl_path = export_pick_and_place(routed, output_dir / f"{project_name}_cpl.csv", bom=bom_for_csv)
    print(f"  Pick-and-place: {cpl_path.name}")

    # STEP 3D model (bare board)
    step_path = export_step(routed, netlist_data, output_dir / f"{project_name}_board.step")
    print(f"  STEP model: {step_path.name}")

    # Zip package
    zip_path = create_output_package(output_dir, project_name)
    print(f"  Package: {zip_path.name}")

    print()

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


def run_workflow_with_gradio(
    requirements_path: Path,
    project_name: str,
    config: OrchestratorConfig,
    attach_files: list[Path] | None = None,
) -> Generator[dict, None, None]:
    """Generator version of run_workflow that yields events for Gradio UI updates.

    Yields dicts with keys:
        event: "step_start" | "step_done" | "viewer_update" | "approval_needed" | "complete" | "error"
        step: int (step number)
        name: str (step name)
        success: bool (for step_done/complete)
        html: str (for viewer_update/approval_needed)
        message: str (for error)
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
    from validators.validate_routing import validate_routing as run_routing_validation

    # Load manufacturing/DFM rules
    copper_oz = 0.5
    mfg_rules: dict = {}
    req_json_path = project.get_output_path(f"{project_name}_requirements.json")
    if req_json_path.exists():
        try:
            req_data = json.loads(req_json_path.read_text())
            copper_oz = req_data.get("board", {}).get("copper_weight_oz", 0.5)
            mfg = req_data.get("manufacturing", {})
            if mfg:
                manufacturer = mfg.get("manufacturer", "")
                if manufacturer:
                    from validators.engineering_constants import get_dfm_profile
                    mfg_rules = get_dfm_profile(manufacturer)
                for key in ("trace_width_min_mm", "clearance_min_mm",
                            "via_drill_min_mm", "via_diameter_min_mm"):
                    if key in mfg:
                        mfg_rules[key] = mfg[key]
        except Exception:
            pass

    router_kwargs: dict = {"copper_weight_oz": copper_oz}
    if mfg_rules:
        if "trace_width_min_mm" in mfg_rules:
            tw_min = mfg_rules["trace_width_min_mm"]
            router_kwargs["trace_width_signal_mm"] = max(0.25, tw_min)
            router_kwargs["trace_width_power_mm"] = max(0.5, tw_min)
            router_kwargs["trace_width_ground_mm"] = max(0.5, tw_min)
        if "clearance_min_mm" in mfg_rules:
            router_kwargs["clearance_mm"] = max(0.2, mfg_rules["clearance_min_mm"])
        if "via_drill_min_mm" in mfg_rules:
            router_kwargs["via_drill_mm"] = max(0.3, mfg_rules["via_drill_min_mm"])
        if "via_diameter_min_mm" in mfg_rules:
            router_kwargs["via_diameter_mm"] = max(0.6, mfg_rules["via_diameter_min_mm"])

    routed = None
    if config.router_engine == "freerouting":
        try:
            from optimizers.freerouter import route_with_freerouting
            dsn_config = {
                "trace_width_mm": router_kwargs.get("trace_width_signal_mm", 0.25),
                "clearance_mm": router_kwargs.get("clearance_mm", 0.2),
                "via_drill_mm": router_kwargs.get("via_drill_mm", 0.3),
                "via_diameter_mm": router_kwargs.get("via_diameter_mm", 0.6),
            }
            routed = route_with_freerouting(
                placement_data, netlist_data,
                jar_path=config.freerouting_jar_path,
                timeout_s=config.freerouting_timeout_s,
                exclude_nets=["GND"],
                dsn_config=dsn_config,
            )
            from optimizers.router import apply_copper_fills, RouterConfig
            fill_config = RouterConfig(**router_kwargs)
            routed = apply_copper_fills(routed, netlist_data, fill_config)
        except Exception:
            routed = None

    if routed is None:
        from optimizers.router import route_board, RouterConfig
        router_config = RouterConfig(**router_kwargs)
        routed = route_board(placement_data, netlist_data, router_config)

    routed_path = project.get_output_path(f"{project_name}_routed.json")
    routed_path.write_text(json.dumps(routed, indent=2))

    val_result = run_routing_validation(str(routed_path), str(netlist_path))
    yield {"event": "step_done", "step": 4, "name": STEP_NAMES[4], "success": True}

    # Yield routed viewer
    html = generate_html(routed, netlist_data, bom_data, routed=routed, embed_mode=True)
    yield {"event": "viewer_update", "html": html}

    # --- Step 5: DRC ---
    yield {"event": "step_start", "step": 5, "name": STEP_NAMES[5]}
    from validators.drc_report import run_drc

    req_data = None
    if req_json_path.exists():
        try:
            req_data = json.loads(req_json_path.read_text())
        except Exception:
            pass

    drc_report = run_drc(routed, netlist_data, req_data)
    drc_path = project.get_output_path(f"{project_name}_drc_report.json")
    drc_path.write_text(json.dumps(drc_report, indent=2))
    yield {"event": "step_done", "step": 5, "name": STEP_NAMES[5], "success": True}

    # Yield final viewer with DRC
    html = generate_html(routed, netlist_data, bom_data, routed=routed, drc_report=drc_report, embed_mode=True)
    yield {"event": "viewer_update", "html": html}

    # Approval gate — in Gradio mode, yield and wait for UI action
    yield {"event": "approval_needed", "html": html}

    # --- Step 6: Output Generation ---
    yield {"event": "step_start", "step": 6, "name": STEP_NAMES[6]}
    from exporters.gerber_exporter import export_gerbers, export_drill, create_output_package
    from exporters.bom_csv_exporter import export_bom_csv, export_pick_and_place
    from exporters.step_exporter import export_step

    output_dir = project.project_dir / "output"
    output_dir.mkdir(exist_ok=True)

    export_gerbers(routed, netlist_data, output_dir)
    export_drill(routed, netlist_data, output_dir / f"{project_name}.drl")
    bom_for_csv = json.loads(bom_path.read_text()) if bom_path.exists() else bom_data
    export_bom_csv(bom_for_csv, output_dir / f"{project_name}_bom.csv")
    export_pick_and_place(routed, output_dir / f"{project_name}_cpl.csv", bom=bom_for_csv)
    export_step(routed, netlist_data, output_dir / f"{project_name}_board.step")
    create_output_package(output_dir, project_name)

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
    print(f"""
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

    print(f"""
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
