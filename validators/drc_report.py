"""Consolidated DRC report generator.

Runs all design rule checks (electrical, DFM, current capacity, mechanical)
and produces a structured JSON report with pass/fail per rule.

Usage:
    from validators.drc_report import run_drc
    report = run_drc(routed, netlist, requirements)
"""

from __future__ import annotations

import json
from pathlib import Path

from .drc_checks_dfm import (
    DRCViolation,
    check_trace_width_min,
    check_clearance_min,
    check_via_drill_min,
    check_annular_ring,
    check_hole_to_hole,
    check_copper_to_edge,
    check_silkscreen,
    check_trace_current_capacity,
    check_inner_plane_antipad,
)
from .engineering_constants import get_dfm_profile


def _resolve_dfm_profile(requirements: dict | None) -> tuple[dict, str]:
    """Resolve DFM profile from requirements. Returns (profile_dict, profile_name)."""
    if not requirements:
        return get_dfm_profile("generic"), "generic"

    # Check nested "manufacturing.manufacturer" first (LLM-generated reqs),
    # then fall back to top-level "manufacturer" (hand-written test reqs).
    mfg = requirements.get("manufacturing", {})
    manufacturer = mfg.get("manufacturer", "") or requirements.get("manufacturer", "")

    if manufacturer:
        profile = get_dfm_profile(manufacturer)
        name = manufacturer
    else:
        profile = get_dfm_profile("generic")
        name = "generic"

    # Override with explicit values from requirements
    for key in ("trace_width_min_mm", "clearance_min_mm",
                "via_drill_min_mm", "via_diameter_min_mm",
                "min_annular_ring_mm", "min_hole_to_hole_mm",
                "min_copper_to_edge_mm", "board_edge_clearance_mm",
                "silkscreen_min_width_mm", "silkscreen_min_height_mm"):
        if key in mfg:
            profile[key] = mfg[key]

    return profile, name


def _run_electrical_checks(routed: dict, netlist: dict) -> list[dict]:
    """Run existing electrical checks and return check results."""
    results = []

    # Import existing validators
    try:
        from .validate_routing import (
            _check_trace_clearance,
            _check_via_clearance,
            _check_connectivity,
            _check_no_shorts,
            _check_pad_clearance,
        )
    except ImportError:
        return results

    routing = routed.get("routing", {})
    traces = routing.get("traces", [])
    vias = routing.get("vias", [])
    config = routing.get("config", {})

    # Trace clearance
    errors, warnings = _check_trace_clearance(routed)
    results.append({
        "rule": "trace_clearance",
        "category": "electrical",
        "passed": len(errors) == 0,
        "violations": [
            DRCViolation(rule="trace_clearance", severity="error", message=e).to_dict()
            for e in errors
        ] + [
            DRCViolation(rule="trace_clearance", severity="warning", message=w).to_dict()
            for w in warnings
        ],
        "checked_count": len(traces),
    })

    # Via clearance
    errors, warnings = _check_via_clearance(routed)
    results.append({
        "rule": "via_clearance",
        "category": "electrical",
        "passed": len(errors) == 0,
        "violations": [
            DRCViolation(rule="via_clearance", severity="error", message=e).to_dict()
            for e in errors
        ],
        "checked_count": len(vias),
    })

    # Connectivity
    errors, warnings = _check_connectivity(routed, netlist)
    results.append({
        "rule": "connectivity",
        "category": "electrical",
        "passed": len(errors) == 0,
        "violations": [
            DRCViolation(rule="connectivity", severity="error", message=e).to_dict()
            for e in errors
        ],
        "checked_count": routing.get("statistics", {}).get("total_nets", 0),
    })

    # No shorts
    errors, warnings = _check_no_shorts(routed)
    results.append({
        "rule": "no_shorts",
        "category": "electrical",
        "passed": len(errors) == 0,
        "violations": [
            DRCViolation(rule="no_shorts", severity="error", message=e).to_dict()
            for e in errors
        ],
        "checked_count": len(traces),
    })

    # Pad clearance
    errors, warnings = _check_pad_clearance(routed, netlist)
    results.append({
        "rule": "pad_clearance",
        "category": "electrical",
        "passed": len(errors) == 0,
        "violations": [
            DRCViolation(rule="pad_clearance", severity="error", message=e).to_dict()
            for e in errors
        ],
        "checked_count": len(traces) + len(vias),
    })

    return results


def _run_dfm_checks(routed: dict, netlist: dict, dfm: dict) -> list[dict]:
    """Run DFM-specific checks and return check results."""
    results = []

    checks = [
        ("trace_width_min", "dfm", check_trace_width_min, (routed, dfm)),
        ("via_drill_min", "dfm", check_via_drill_min, (routed, dfm)),
        ("annular_ring", "dfm", check_annular_ring, (routed, dfm)),
        ("silkscreen", "dfm", check_silkscreen, (routed, dfm)),
        ("clearance_min", "dfm", check_clearance_min, (routed, dfm)),
        ("hole_to_hole", "mechanical", check_hole_to_hole, (routed, netlist, dfm)),
        ("copper_to_edge", "mechanical", check_copper_to_edge, (routed, netlist, dfm)),
        ("inner_plane_antipad", "dfm", check_inner_plane_antipad, (routed, netlist, dfm)),
    ]

    for rule, category, check_fn, args in checks:
        violations = check_fn(*args)
        has_errors = any(v.severity == "error" for v in violations)
        results.append({
            "rule": rule,
            "category": category,
            "passed": not has_errors,
            "violations": [v.to_dict() for v in violations],
            "checked_count": _count_checked(routed, rule),
        })

    return results


def _run_current_checks(routed: dict, netlist: dict, copper_oz: float) -> list[dict]:
    """Run trace current capacity check."""
    violations = check_trace_current_capacity(routed, netlist, copper_oz)
    has_errors = any(v.severity == "error" for v in violations)
    return [{
        "rule": "trace_current_capacity",
        "category": "current",
        "passed": not has_errors,
        "violations": [v.to_dict() for v in violations],
        "checked_count": len(set(
            t.get("net_id") for t in routed.get("routing", {}).get("traces", [])
        )),
    }]


def _count_checked(routed: dict, rule: str) -> int:
    """Estimate number of items checked for a given rule."""
    routing = routed.get("routing", {})
    if rule in ("trace_width_min", "clearance_min"):
        return len(routing.get("traces", []))
    elif rule in ("via_drill_min", "annular_ring"):
        return len(routing.get("vias", []))
    elif rule == "silkscreen":
        return len(routed.get("silkscreen", []))
    elif rule in ("hole_to_hole", "copper_to_edge"):
        return len(routing.get("vias", [])) + len(routed.get("placements", []))
    elif rule == "inner_plane_antipad":
        return sum(1 for f in routing.get("copper_fills", []) if f.get("is_plane"))
    return 0


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def run_drc(
    routed: dict,
    netlist: dict,
    requirements: dict | None = None,
) -> dict:
    """Run all DRC checks and produce a structured report.

    Args:
        routed: Routed dict with traces, vias, fills, placements.
        netlist: Netlist dict for connectivity and current estimation.
        requirements: Optional requirements dict for DFM profile selection.

    Returns:
        DRC report dict with pass/fail, check results, and statistics.
    """
    dfm, dfm_name = _resolve_dfm_profile(requirements)
    copper_oz = 0.5
    if requirements:
        copper_oz = requirements.get("board", {}).get("copper_weight_oz", 0.5)

    # Run all check categories
    all_checks: list[dict] = []
    all_checks.extend(_run_electrical_checks(routed, netlist))
    all_checks.extend(_run_dfm_checks(routed, netlist, dfm))
    all_checks.extend(_run_current_checks(routed, netlist, copper_oz))

    # Compute statistics
    total = len(all_checks)
    passed = sum(1 for c in all_checks if c["passed"])
    errors = sum(
        sum(1 for v in c["violations"] if v.get("severity") == "error")
        for c in all_checks
    )
    warnings = sum(
        sum(1 for v in c["violations"] if v.get("severity") == "warning")
        for c in all_checks
    )

    overall_pass = all(c["passed"] for c in all_checks)

    report = {
        "version": "1.0",
        "project_name": routed.get("project_name", ""),
        "manufacturer": dfm_name,
        "dfm_profile": dfm.get("description", dfm_name),
        "passed": overall_pass,
        "summary": f"{passed}/{total} checks passed, {errors} errors, {warnings} warnings",
        "checks": all_checks,
        "statistics": {
            "total_checks": total,
            "passed": passed,
            "failed": total - passed,
            "errors": errors,
            "warnings": warnings,
        },
    }

    return report


# Per-rule remediation hints for agents. Keep these concrete: which tool to
# reach for and what to change.
_RULE_REMEDIATION = {
    "trace_clearance": "Re-route with route_board(effort='best'), or re-place "
                       "on a larger board with optimize_placement and route again.",
    "via_clearance": "Re-route with route_board(effort='best') — via spacing is "
                     "decided by the router.",
    "connectivity": "Some nets are not fully connected. Re-run route_board "
                    "(higher effort), or re-place with a larger board first.",
    "no_shorts": "Traces of different nets touch. Re-route with "
                 "route_board(effort='best').",
    "pad_clearance": "Re-route with route_board(effort='best'); persistent pad "
                     "clearance violations usually mean the placement is too "
                     "dense — re-place on a larger board.",
    "trace_width_min": "Trace width is below the manufacturing minimum — pick a "
                       "less strict manufacturer profile or adjust trace widths.",
    "clearance_min": "Copper clearance below manufacturing minimum. Re-route "
                     "with route_board(effort='best') or relax the DFM profile.",
    "via_drill_min": "Via drill below manufacturing minimum — adjust the via "
                     "settings or DFM profile.",
    "annular_ring": "Via annular ring too thin — increase via diameter or "
                    "relax the DFM profile.",
    "silkscreen": "Silkscreen text below minimum legible size; usually safe to "
                  "ignore (warning) or adjust board text settings.",
    "hole_to_hole": "Drill holes too close together — re-place with more "
                    "spacing (larger board) and re-route.",
    "copper_to_edge": "Copper too close to the board edge — re-place with a "
                      "slightly larger board, then re-route.",
    "inner_plane_antipad": "Inner plane antipad clearance issue — re-route; if "
                           "persistent, increase board size.",
    "trace_current_capacity": "A trace is too narrow for its current (IPC-2221). "
                              "Increase the net's trace width or copper weight.",
}


def summarize_drc(report: dict, top_n: int = 10) -> dict:
    """Condense a full DRC report into an agent-friendly summary.

    Returns severity-ranked violations (errors first, capped at top_n),
    per-rule counts, and a concrete remediation hint per failing rule —
    so a small model can act without parsing all 14 check dicts.
    """
    failing: list[dict] = []
    flat: list[dict] = []
    for check in report.get("checks", []):
        rule = check.get("rule", "?")
        errs = [v for v in check.get("violations", [])
                if v.get("severity") == "error"]
        warns = [v for v in check.get("violations", [])
                 if v.get("severity") == "warning"]
        if errs or warns:
            entry = {
                "rule": rule,
                "category": check.get("category", ""),
                "errors": len(errs),
                "warnings": len(warns),
            }
            hint = _RULE_REMEDIATION.get(rule)
            if hint:
                entry["remediation_hint"] = hint
            failing.append(entry)
        flat.extend(errs)
        flat.extend(warns)

    # Errors first, then warnings; preserve check order within each severity.
    flat.sort(key=lambda v: 0 if v.get("severity") == "error" else 1)
    top = []
    for v in flat[:top_n]:
        item = {"rule": v.get("rule"), "severity": v.get("severity"),
                "message": v.get("message")}
        if v.get("location"):
            item["location"] = v["location"]
        top.append(item)

    stats = report.get("statistics", {})
    return {
        "passed": report.get("passed", False),
        "summary": report.get("summary", ""),
        "manufacturer": report.get("manufacturer", ""),
        "error_count": stats.get("errors", 0),
        "warning_count": stats.get("warnings", 0),
        "failing_rules": failing,
        "top_violations": top,
        "truncated": len(flat) > top_n,
        "note": (f"Showing {min(top_n, len(flat))} of {len(flat)} violations; "
                 "call get_drc_report(verbose=True) for the full report."
                 if len(flat) > top_n else None),
    }
