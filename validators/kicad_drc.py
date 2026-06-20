#!/usr/bin/env python3
"""Authoritative DRC via kicad-cli.

The internal `drc_report` is the portable, deterministic baseline, but its
geometry (pad shorts, antipads, zone connectivity) and short COUNT don't match
what the fab sees. When kicad-cli is installed we export the board (zones poured
via pcbnew, rules carried by the sibling .kicad_pro) and run KiCad's own DRC —
the same engine the manufacturer uses — and shape its result into the exact
`drc_report` report dict so agents/viewer/approval consume it unchanged.

`build_kicad_drc_report` is pure (takes a parsed kicad-cli DRC json) so it's
unit-testable; `run_kicad_drc` does the export+invoke and is a no-op (returns
None) when anything is missing.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Callable

# kicad-cli violation `type` → (drc_report rule, category)
_TYPE_TO_RULE: dict[str, tuple[str, str]] = {
    "shorting_items": ("no_shorts", "electrical"),
    "clearance": ("clearance_min", "dfm"),
    "track_clearance": ("clearance_min", "dfm"),
    "hole_clearance": ("hole_to_hole", "mechanical"),
    "hole_near_hole": ("hole_to_hole", "mechanical"),
    "hole_to_hole": ("hole_to_hole", "mechanical"),
    "track_dangling": ("dangling", "dfm"),
    "via_dangling": ("dangling", "dfm"),
    "copper_edge_clearance": ("copper_to_edge", "mechanical"),
    "edge_clearance": ("copper_to_edge", "mechanical"),
    "drill_out_of_range": ("via_drill_min", "dfm"),
    "via_diameter": ("annular_ring", "dfm"),
    "annular_width": ("annular_ring", "dfm"),
    "track_width": ("trace_width_min", "dfm"),
    "silk_over_copper": ("silkscreen", "dfm"),
    "silk_overlap": ("silkscreen", "dfm"),
    "text_height": ("silkscreen", "dfm"),
    "solder_mask_bridge": ("solder_mask_bridge", "dfm"),
    "starved_thermal": ("starved_thermal", "dfm"),
}
_UNKNOWN_RULE = ("other", "dfm")


def _first_pos(items: list[dict]) -> dict | None:
    for it in items:
        if it.get("pos"):
            p = it["pos"]
            return {"x_mm": round(p.get("x", 0), 3), "y_mm": round(p.get("y", 0), 3)}
    return None


def build_kicad_drc_report(drc_data: dict, *, project_name: str = "",
                           extra_checks: list[dict] | None = None) -> dict:
    """Shape a parsed kicad-cli DRC json into the drc_report report structure.

    KiCad's DRC is authoritative for GEOMETRY (shorts, clearance, hole/edge,
    annular, silk, dangling). It is NOT used for CONNECTIVITY: KiCad's ratsnest
    disagrees with the autorouter's own completion (it flags GND-zone fragments
    and segment/plane-credited joins the router considers connected), which made
    a "100% routed" board read as "DRC: N unconnected" and sent agents looping.
    So kicad-cli `unconnected_items` are intentionally dropped here, and the
    caller passes the router-reconciled internal checks via `extra_checks`
    (connectivity + trace_current_capacity, neither of which KiCad covers the
    way we want)."""
    by_rule: dict[str, dict] = {}

    def _bucket(rule: str, category: str) -> dict:
        return by_rule.setdefault(rule, {
            "rule": rule, "category": category, "passed": True,
            "violations": [], "checked_count": 0})

    for v in drc_data.get("violations", []):
        rule, category = _TYPE_TO_RULE.get(v.get("type", ""), _UNKNOWN_RULE)
        sev = v.get("severity", "error")
        b = _bucket(rule, category)
        b["violations"].append({
            "rule": rule, "severity": sev,
            "message": v.get("description", v.get("type", "")),
            "location": _first_pos(v.get("items", [])),
        })
        if sev == "error":
            b["passed"] = False
    # NOTE: kicad-cli `unconnected_items` are deliberately NOT mapped — the
    # router-reconciled internal connectivity check is the connectivity gate
    # (passed in via extra_checks). See the docstring.

    checks = list(by_rule.values())
    for extra in (extra_checks or []):
        if extra is not None:
            checks.append(extra)

    errors = sum(sum(1 for v in c["violations"] if v.get("severity") == "error")
                 for c in checks)
    warnings = sum(sum(1 for v in c["violations"] if v.get("severity") == "warning")
                   for c in checks)
    overall = all(c["passed"] for c in checks)
    return {
        "version": "1.0",
        "project_name": project_name,
        "engine": "kicad-cli",
        "passed": overall,
        "summary": ("DRC passed (kicad-cli)" if overall
                    else f"DRC failed (kicad-cli): {errors} error(s), {warnings} warning(s)"),
        "checks": checks,
        "statistics": {
            "total_checks": len(checks),
            "checks_passed": sum(1 for c in checks if c["passed"]),
            "errors": errors,
            "warnings": warnings,
        },
    }


def run_kicad_drc(
    routed: dict,
    netlist: dict,
    kicad_cli: str,
    *,
    export_fn: Callable[[dict, dict, Path], None],
    project_name: str = "",
    extra_checks: list[dict] | None = None,
    timeout: int = 300,
) -> dict | None:
    """Export the board and run kicad-cli DRC; return a drc_report-shaped dict,
    or None if the export/DRC couldn't be produced (caller keeps the internal
    report)."""
    import tempfile
    with tempfile.TemporaryDirectory(prefix="pcb-kicad-drc-") as td:
        pcb = Path(td) / "drc.kicad_pcb"
        out = Path(td) / "drc.json"
        try:
            export_fn(routed, netlist, pcb)
            subprocess.run(
                [kicad_cli, "pcb", "drc", "--format", "json", "--output", str(out),
                 str(pcb)],
                capture_output=True, timeout=timeout, check=False)
            data = json.loads(out.read_text())
        except (FileNotFoundError, OSError, subprocess.SubprocessError,
                json.JSONDecodeError, ValueError):
            return None
    return build_kicad_drc_report(data, project_name=project_name,
                                  extra_checks=extra_checks)
