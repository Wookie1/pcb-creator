"""Agent-facing electrical report for a circuit netlist.

Separated from drc_checks.py because finding a defect and explaining it to a
small model are different jobs. The shape here is built around one rule:

    no verdict value may mean "this circuit works"

The best case is `no_issues_found`, which is literally true and cannot be
misread as "verified correct". Anything we could not evaluate is named
explicitly in `not_checked`, together with the argument that would let us
evaluate it, so an agent is never left guessing what the silence meant.
"""

import drc_checks
from drc_checks import build_lookups, resolve_rails
from engineering_constants import parse_supply_voltage

# Keys an agent may supply per part via `models`. Anything else is reported
# back rather than silently ignored, so nobody assumes a value took effect.
SUPPORTED_MODEL_KEYS = frozenset({"vcc_min", "vcc_max", "vf", "if", "voltage_rating"})

# check -> whether its warnings are findings or just measurements
_INFORMATIONAL = {"check_power_budget"}

_ERROR_CHECKS = (
    drc_checks.check_circuit_integrity,
    drc_checks.check_resistor_power,
    drc_checks.check_led_current,
    drc_checks.check_rail_voltage,
    drc_checks.check_power_budget,
)


def _apply_models(components: dict, models: dict | None) -> tuple[dict, list[str]]:
    """Merge agent-supplied part data into component properties."""
    applied: dict[str, dict] = {}
    unknown: list[str] = []
    if not models:
        return applied, unknown

    by_designator = {c.get("designator"): c for c in components.values()}
    for des, spec in models.items():
        comp = by_designator.get(des)
        if comp is None or not isinstance(spec, dict):
            unknown.append(f"{des} (no such component)")
            continue
        props = dict(comp.get("properties") or {})
        for key, value in spec.items():
            if key not in SUPPORTED_MODEL_KEYS:
                unknown.append(f"{des}.{key}")
                continue
            props[key] = value
            applied.setdefault(des, {})[key] = value
        comp["properties"] = props
    return applied, unknown


def _rail_overrides(nets: dict, rails: dict | None) -> tuple[dict[str, float], list[str]]:
    """Translate agent-supplied {net_name: "5V"} into {net_id: volts}."""
    out: dict[str, float] = {}
    bad: list[str] = []
    if not rails:
        return out, bad
    by_name = {str(n.get("name", "")).upper(): nid for nid, n in nets.items()}
    for name, value in rails.items():
        nid = by_name.get(str(name).upper()) or (name if name in nets else None)
        if nid is None:
            bad.append(f"{name} (no such net)")
            continue
        volts = parse_supply_voltage(str(value))
        if volts is None:
            bad.append(f"{name}={value!r} (not a single voltage)")
            continue
        out[nid] = volts
    return out, bad


def build_report(
    elements: list[dict],
    supply_voltage: float | None = None,
    supply_source: str = "argument",
    models: dict | None = None,
    rails: dict | None = None,
) -> dict:
    """Run every electrical check and format the result for an agent."""
    components, ports, nets = build_lookups(elements)
    applied_models, unknown_keys = _apply_models(components, models)
    rail_overrides, bad_rails = _rail_overrides(nets, rails)

    _base_rails, ambiguous = resolve_rails(components, ports, nets, supply_voltage)
    ambiguous = [a for a in ambiguous if a not in rail_overrides]

    solution = drc_checks.solve_circuit(
        components, ports, nets, supply_voltage, rail_overrides
    )

    issues: list[dict] = []
    measurements: list[str] = []
    for check in _ERROR_CHECKS:
        errs, warns = check(components, ports, nets, supply_voltage, solution)
        informational = check.__name__ in _INFORMATIONAL
        for msg in errs:
            issues.append({"severity": "error", "check": check.__name__, "detail": msg})
        for msg in warns:
            if informational:
                measurements.append(msg)
            else:
                issues.append(
                    {"severity": "warning", "check": check.__name__, "detail": msg}
                )

    _e, pullup_warns = drc_checks.check_pullups(components, ports, nets)
    for msg in pullup_warns:
        issues.append({"severity": "warning", "check": "check_pullups", "detail": msg})

    not_checked = _build_not_checked(
        solution, nets, supply_voltage, ambiguous, bad_rails, unknown_keys
    )

    n_errors = sum(1 for i in issues if i["severity"] == "error")
    n_warnings = len(issues) - n_errors
    nets_evaluated = 0
    if solution is not None:
        nets_evaluated = sum(
            1
            for nid in nets
            if nid in solution.node_v and not solution.is_tainted(nid)
        )

    counts = {
        "errors": n_errors,
        "warnings": n_warnings,
        "not_checked": len(not_checked),
        "nets_total": len(nets),
        "nets_evaluated": nets_evaluated,
        "parts_total": len(components),
        "parts_unmodeled": len(solution.unmodeled) if solution else len(components),
    }

    if n_errors:
        verdict = "issues_found"
    elif not_checked:
        verdict = "not_enough_information"
    elif n_warnings:
        verdict = "issues_found"
    else:
        verdict = "no_issues_found"

    inputs_used = {}
    if supply_voltage is not None:
        inputs_used["supply_voltage"] = {
            "value": supply_voltage,
            "source": supply_source,
        }
    if applied_models:
        inputs_used["models"] = {"value": applied_models, "source": "agent"}
    if solution is not None and solution.assumptions:
        inputs_used["assumptions"] = {
            "value": solution.assumptions,
            "source": "assumed",
        }

    return {
        "verdict": verdict,
        "headline": _headline(verdict, counts),
        "issues": issues,
        "not_checked": not_checked,
        "measurements": measurements,
        "counts": counts,
        "inputs_used": inputs_used,
    }


def _build_not_checked(
    solution,
    nets: dict,
    supply_voltage: float | None,
    ambiguous: list[str],
    bad_rails: list[str],
    unknown_keys: list[str],
) -> list[dict]:
    """Everything we could not evaluate, and the argument that would fix it."""
    entries: list[dict] = []

    if supply_voltage is None:
        entries.append(
            {
                "what": "the whole circuit",
                "reason": "no usable supply voltage",
                "consequence": "No current, power, or rail-voltage check could run.",
                "to_check_this": {
                    "tool": "check_circuit",
                    "args": {"supply_voltage": "5V"},
                },
            }
        )
        return entries

    if solution is None:
        entries.append(
            {
                "what": "the whole circuit",
                "reason": "no power net could be identified to attach the supply to",
                "consequence": "Nothing drives current, so no solve-based check ran.",
                "to_check_this": {
                    "tool": "check_circuit",
                    "args": {"rails": {"VCC": "5V"}},
                },
            }
        )
        return entries

    if ambiguous:
        names = [nets.get(n, {}).get("name", n) for n in ambiguous]
        entries.append(
            {
                "what": f"supply rail assignment ({', '.join(names)})",
                "reason": "more than one power net could be the input rail",
                "consequence": "No rail was assigned, so downstream currents are unknown.",
                "to_check_this": {
                    "tool": "check_circuit",
                    "args": {"rails": {names[0]: "5V"}},
                },
            }
        )

    for part in solution.unmodeled:
        tainted = [
            nets.get(n, {}).get("name", n)
            for n in part.get("nets", [])
            if solution.is_tainted(n)
        ]
        if not tainted:
            continue
        entries.append(
            {
                "what": part["designator"],
                "value": part.get("value", ""),
                "reason": part.get("reason", "no model for this part"),
                "consequence": (
                    f"Nets {', '.join(tainted)} were not evaluated — any current "
                    f"through them depends on what this part does."
                ),
                "to_check_this": {
                    "tool": "check_circuit",
                    "args": {
                        "models": {
                            part["designator"]: {"vcc_min": "3.0V", "vcc_max": "3.6V"}
                        }
                    },
                },
            }
        )

    for bad in bad_rails:
        entries.append(
            {
                "what": f"rail override {bad}",
                "reason": "could not be applied",
                "consequence": "That rail kept its inferred value.",
                "to_check_this": {"tool": "check_circuit", "args": {}},
            }
        )

    for key in unknown_keys:
        entries.append(
            {
                "what": f"model key {key}",
                "reason": (
                    "not a supported model key; supported keys are "
                    + ", ".join(sorted(SUPPORTED_MODEL_KEYS))
                ),
                "consequence": "That value was ignored and changed no result.",
                "to_check_this": {"tool": "check_circuit", "args": {}},
            }
        )

    return entries


def _headline(verdict: str, counts: dict) -> str:
    """One quotable sentence. Small models repeat this instead of doing math."""
    bits = []
    if counts["errors"]:
        bits.append(f"{counts['errors']} error(s)")
    if counts["warnings"]:
        bits.append(f"{counts['warnings']} warning(s)")
    found = " and ".join(bits) if bits else "No issues"

    coverage = (
        f"{counts['nets_evaluated']} of {counts['nets_total']} nets were evaluated"
    )

    if verdict == "no_issues_found":
        return (
            f"No issues found, and all {counts['nets_total']} nets were evaluated. "
            f"This means no check failed — it is not proof the circuit works."
        )
    if verdict == "not_enough_information":
        return (
            f"{found} found, but only {coverage}. This circuit was NOT verified — "
            f"see not_checked for what was skipped and why."
        )
    action = "Fix the errors below" if counts["errors"] else "Review the warnings below"
    return (
        f"{found} found; {coverage}. {action}, then re-run. "
        f"Anything listed in not_checked was not verified either way."
    )
