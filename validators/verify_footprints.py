"""Footprint resolution verification — the deterministic gate that replaces the
LLM-flow's footprint review for the agent-driven (KiCad import) pipeline.

Every component's package must resolve to real pad geometry through the tiered
``pad_geometry`` lookup (KiCad library → IPC-7351 → cache → built-in → normalized
retry).  A component that resolves to ``None`` would silently become a 3 mm
placeholder during placement — that is exactly what this gate forbids.

The output is structured so an agent can act on it: correct the package name, or
supply geometry via the ``provide_footprint`` MCP tool.

Usage:
    from validators.verify_footprints import verify_footprints
    issues = verify_footprints(netlist)   # [] means every footprint resolved
"""

from __future__ import annotations


def _component_pin_counts(netlist: dict) -> dict[str, int]:
    counts: dict[str, int] = {}
    for elem in netlist.get("elements", []):
        if elem.get("element_type") == "port":
            cid = elem.get("component_id", "")
            counts[cid] = counts.get(cid, 0) + 1
    return counts


def verify_footprints(netlist: dict) -> list[dict]:
    """Return the components whose footprint cannot be resolved to real geometry.

    Args:
        netlist: Parsed circuit_schema netlist dict (flat ``elements`` array).

    Returns:
        A list of issue dicts (empty when every footprint resolves), each:
            {
                "designator": str,
                "package": str,
                "pin_count": int,
                "reason": str,
            }
        Resolution uses the module-level tiered lookup, so callers must have run
        ``configure_lookup`` first (CLI / GUI / MCP bootstrap all do).
    """
    from optimizers.pad_geometry import get_footprint_def

    pin_counts = _component_pin_counts(netlist)

    issues: list[dict] = []
    for elem in netlist.get("elements", []):
        if elem.get("element_type") != "component":
            continue
        # Fiducials carry their own geometry and are exempt from netlist lookup.
        if elem.get("component_type") == "fiducial":
            continue

        des = elem.get("designator", "")
        pkg = elem.get("package", "") or ""
        cid = elem.get("component_id", "")
        pin_count = pin_counts.get(cid, 0)

        if not pkg:
            issues.append({
                "designator": des,
                "package": pkg,
                "pin_count": pin_count,
                "reason": "component has no package string",
            })
            continue

        fp = get_footprint_def(pkg, pin_count)
        if fp is None:
            issues.append({
                "designator": des,
                "package": pkg,
                "pin_count": pin_count,
                "reason": (
                    f"package '{pkg}' did not match any footprint tier "
                    "(KiCad library, IPC-7351, cache, built-in, or normalized "
                    "name). Set PCB_KICAD_LIBRARY_PATH, correct the package "
                    "name, or supply geometry via provide_footprint."
                ),
            })
            continue

        # The footprint must cover every port's pin number. Pins without pad
        # geometry silently fall back to the component CENTER in
        # build_pad_map — phantom stacked pads that the router is forced to
        # route into, producing unroutable nets and false shorts.
        port_pins = {e.get("pin_number")
                     for e in netlist.get("elements", [])
                     if e.get("element_type") == "port"
                     and e.get("component_id") == cid}
        missing = [str(p) for p in sorted(port_pins - set(fp.pin_offsets))]
        if missing:
            issues.append({
                "designator": des,
                "package": pkg,
                "pin_count": pin_count,
                "reason": (
                    f"footprint for '{pkg}' has {len(fp.pin_offsets)} pads "
                    f"but the component has ports on pin(s) "
                    f"{', '.join(missing[:8])}"
                    + ("..." if len(missing) > 8 else "") + " with no pad. "
                    "Correct the package name (or pin numbering), or supply "
                    "full geometry via provide_footprint."
                ),
            })

    return issues
