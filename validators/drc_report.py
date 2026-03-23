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
)
from .engineering_constants import get_dfm_profile


def _resolve_dfm_profile(requirements: dict | None) -> tuple[dict, str]:
    """Resolve DFM profile from requirements. Returns (profile_dict, profile_name)."""
    if not requirements:
        return get_dfm_profile("generic"), "generic"

    mfg = requirements.get("manufacturing", {})
    manufacturer = mfg.get("manufacturer", "")

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
