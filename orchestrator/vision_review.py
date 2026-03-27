"""Vision-based autonomous board review.

Renders the routed board to PNG, sends it with DRC/routing stats to a
vision-capable LLM, and parses an APPROVE/REQUEST_CHANGES decision.
Escalates to human review after max attempts.
"""

from __future__ import annotations

import re
from pathlib import Path

import cairosvg

from .config import OrchestratorConfig
from .llm.litellm_client import LiteLLMClient
from .prompts.builder import PromptBuilder


def render_board_png(
    routed: dict,
    netlist: dict | None = None,
    bom: dict | None = None,
    width: int = 2048,
) -> bytes:
    """Render the routed board to PNG bytes via SVG -> cairosvg.

    Args:
        routed: Routed board dict (placement + traces + vias + fills).
        netlist: Optional netlist for per-net coloring.
        bom: Optional BOM for component tooltips.
        width: Output image width in pixels.

    Returns:
        PNG image as bytes.
    """
    from visualizers.placement_viewer import generate_svg

    svg_str = generate_svg(routed, netlist, bom, routed=routed)
    png_bytes = cairosvg.svg2png(
        bytestring=svg_str.encode("utf-8"),
        output_width=width,
    )
    return png_bytes


def format_review_context(drc_report: dict | None, routed: dict) -> tuple[str, str]:
    """Format routing stats and DRC summary as text for the review prompt.

    Returns:
        (routing_stats_text, drc_summary_text)
    """
    # Routing stats
    stats = routed.get("statistics", {})
    total = stats.get("total_nets", 0)
    routed_count = stats.get("routed_nets", 0)
    completion = stats.get("completion_pct", 0)
    vias = stats.get("via_count", 0)
    trace_len = stats.get("total_trace_length_mm", 0)
    unrouted = stats.get("unrouted_nets", [])

    routing_lines = [
        f"- Completion: {completion:.1f}% ({routed_count}/{total} nets routed)",
        f"- Total trace length: {trace_len:.1f} mm",
        f"- Via count: {vias}",
    ]
    if unrouted:
        routing_lines.append(f"- UNROUTED NETS: {', '.join(str(n) for n in unrouted)}")
    routing_stats = "\n".join(routing_lines)

    # DRC summary
    if drc_report:
        passed = drc_report.get("passed", False)
        summary = drc_report.get("summary", "")
        drc_stats = drc_report.get("statistics", {})
        errors = drc_stats.get("errors", 0)
        warnings = drc_stats.get("warnings", 0)

        drc_lines = [
            f"- Overall: {'PASS' if passed else 'FAIL'}",
            f"- Summary: {summary}",
            f"- Errors: {errors}, Warnings: {warnings}",
        ]

        # Include top violations
        for check in drc_report.get("checks", []):
            if not check.get("passed", True):
                violations = check.get("violations", [])
                for v in violations[:2]:
                    drc_lines.append(
                        f"- [{v.get('severity', 'error').upper()}] "
                        f"{check.get('rule', '?')}: {v.get('message', '')}"
                    )
        drc_summary = "\n".join(drc_lines)
    else:
        drc_summary = "- No DRC report available."

    return routing_stats, drc_summary


def _parse_decision(response: str) -> tuple[str, str]:
    """Parse LLM response into (decision, details).

    Returns:
        ("approve", "") or ("request_changes", "description of issues")
    """
    text = response.strip()

    if re.match(r"^APPROVE\b", text, re.IGNORECASE):
        return "approve", ""

    m = re.match(r"^REQUEST_CHANGES:\s*(.+)", text, re.IGNORECASE | re.DOTALL)
    if m:
        return "request_changes", m.group(1).strip()

    # Fallback: look for keywords anywhere
    if "APPROVE" in text.upper() and "REQUEST_CHANGES" not in text.upper():
        return "approve", ""

    return "request_changes", text


def run_vision_review(
    routed: dict,
    netlist: dict | None,
    bom: dict | None,
    drc_report: dict | None,
    config: OrchestratorConfig,
    project: object | None = None,
) -> str:
    """Run vision-based autonomous review of the routed board.

    Args:
        routed: Routed board dict.
        netlist: Netlist dict.
        bom: BOM dict.
        drc_report: DRC report dict.
        config: Orchestrator config with vision_model and vision_max_review_attempts.
        project: Project object (unused, reserved for future use).

    Returns:
        "approved" if the vision model approves.
        "escalated" if max attempts exhausted without approval.
    """
    # Quick pre-checks that don't need vision
    stats = routed.get("statistics", {})
    completion = stats.get("completion_pct", 0)
    drc_passed = drc_report.get("passed", False) if drc_report else False
    drc_errors = (drc_report or {}).get("statistics", {}).get("errors", 0)

    # Auto-approve if routing is 100% and DRC passes with no errors
    if completion >= 100.0 and drc_passed and drc_errors == 0:
        print("    Pre-check: 100% routed, 0 DRC errors — auto-approved")
        return "approved"

    # Auto-escalate if routing < 100% — no point asking vision
    if completion < 100.0:
        print(f"    Pre-check: {completion:.1f}% routed — escalating to human review")
        return "escalated"

    # Render board image
    try:
        png_bytes = render_board_png(routed, netlist, bom)
    except Exception as e:
        print(f"    Board render failed: {e} — escalating to human review")
        return "escalated"

    # Build review prompt
    routing_stats, drc_summary = format_review_context(drc_report, routed)
    prompt_builder = PromptBuilder(config.base_dir)
    review_prompt = prompt_builder.render("vision_review", {
        "routing_stats": routing_stats,
        "drc_summary": drc_summary,
    })

    # Create vision LLM client
    vision_llm = LiteLLMClient(
        model=config.vision_model,
        api_base=config.api_base,
        api_key=config.api_key,
    )

    # Review loop
    max_attempts = config.vision_max_review_attempts
    for attempt in range(1, max_attempts + 1):
        print(f"    Vision review attempt {attempt}/{max_attempts}...")
        try:
            response = vision_llm.generate_with_vision(
                system_prompt="",
                user_prompt=review_prompt,
                images=[png_bytes],
                max_tokens=512,
                temperature=0.0,
            )
        except Exception as e:
            print(f"    Vision LLM call failed: {e}")
            continue

        decision, details = _parse_decision(response)
        if decision == "approve":
            return "approved"

        print(f"    Changes requested: {details}")

    print(f"    Max review attempts ({max_attempts}) exhausted — escalating to human review")
    return "escalated"
