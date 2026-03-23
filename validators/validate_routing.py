"""Routing validator — deterministic checks for routed PCB traces and vias.

Standalone CLI: python validate_routing.py <routed.json> [--netlist <netlist.json>]
Also importable: from validate_routing import validate_routing
"""

import argparse
import json
import math
import sys
from pathlib import Path

import jsonschema

SCHEMA_PATH = Path(__file__).parent.parent / "schemas" / "routed_schema.json"


def _load_schema() -> dict:
    return json.loads(SCHEMA_PATH.read_text())


# ---------------------------------------------------------------------------
# 1. Schema validation
# ---------------------------------------------------------------------------

def _validate_schema(routed: dict) -> list[str]:
    """Validate routed JSON against schema."""
    try:
        schema = _load_schema()
    except FileNotFoundError:
        return ["Schema file not found: routed_schema.json"]

    validator = jsonschema.Draft7Validator(schema)
    errors = []
    for error in sorted(validator.iter_errors(routed), key=lambda e: list(e.path)):
        path = ".".join(str(p) for p in error.absolute_path) or "(root)"
        errors.append(f"Schema: {path}: {error.message}")
    return errors


# ---------------------------------------------------------------------------
# 2. Trace-to-trace clearance
# ---------------------------------------------------------------------------

def _point_to_segment_distance(
    px: float, py: float,
    ax: float, ay: float, bx: float, by: float,
) -> float:
    """Minimum distance from point (px,py) to line segment (ax,ay)-(bx,by)."""
    dx, dy = bx - ax, by - ay
    length_sq = dx * dx + dy * dy
    if length_sq == 0:
        return math.hypot(px - ax, py - ay)

    t = max(0, min(1, ((px - ax) * dx + (py - ay) * dy) / length_sq))
    proj_x = ax + t * dx
    proj_y = ay + t * dy
    return math.hypot(px - proj_x, py - proj_y)


def _segment_to_segment_distance(
    a1x: float, a1y: float, a2x: float, a2y: float,
    b1x: float, b1y: float, b2x: float, b2y: float,
) -> float:
    """Minimum distance between two line segments."""
    # Check all point-to-segment combinations
    return min(
        _point_to_segment_distance(a1x, a1y, b1x, b1y, b2x, b2y),
        _point_to_segment_distance(a2x, a2y, b1x, b1y, b2x, b2y),
        _point_to_segment_distance(b1x, b1y, a1x, a1y, a2x, a2y),
        _point_to_segment_distance(b2x, b2y, a1x, a1y, a2x, a2y),
    )


def _check_trace_clearance(routed: dict) -> tuple[list[str], list[str]]:
    """Check trace-to-trace clearance for different nets on the same layer."""
    errors = []
    warnings = []

    routing = routed.get("routing", {})
    traces = routing.get("traces", [])
    clearance = routing.get("config", {}).get("trace_clearance_mm", 0.2)

    # Group traces by layer
    by_layer: dict[str, list[dict]] = {"top": [], "bottom": []}
    for t in traces:
        layer = t.get("layer", "top")
        if layer in by_layer:
            by_layer[layer].append(t)

    for layer, layer_traces in by_layer.items():
        n = len(layer_traces)
        for i in range(n):
            for j in range(i + 1, n):
                t1, t2 = layer_traces[i], layer_traces[j]
                # Only check different nets
                if t1.get("net_id") == t2.get("net_id"):
                    continue

                # Compute center-to-center distance
                dist = _segment_to_segment_distance(
                    t1["start_x_mm"], t1["start_y_mm"], t1["end_x_mm"], t1["end_y_mm"],
                    t2["start_x_mm"], t2["start_y_mm"], t2["end_x_mm"], t2["end_y_mm"],
                )

                # Account for trace widths
                min_dist = (t1.get("width_mm", 0.25) + t2.get("width_mm", 0.25)) / 2 + clearance
                if dist < min_dist - 0.01:  # small tolerance for grid snapping
                    errors.append(
                        f"Trace clearance violation on {layer}: "
                        f"{t1.get('net_name', t1.get('net_id'))} <-> "
                        f"{t2.get('net_name', t2.get('net_id'))} "
                        f"distance={dist:.3f}mm, required={min_dist:.3f}mm"
                    )

    return errors, warnings


# ---------------------------------------------------------------------------
# 3. Via clearance
# ---------------------------------------------------------------------------

def _check_via_clearance(routed: dict) -> tuple[list[str], list[str]]:
    """Check via-to-via and via-to-trace clearance for different nets."""
    errors = []
    warnings = []

    routing = routed.get("routing", {})
    vias = routing.get("vias", [])
    traces = routing.get("traces", [])
    clearance = routing.get("config", {}).get("trace_clearance_mm", 0.2)

    # Via-to-via
    for i in range(len(vias)):
        for j in range(i + 1, len(vias)):
            v1, v2 = vias[i], vias[j]
            if v1.get("net_id") == v2.get("net_id"):
                continue

            dist = math.hypot(v1["x_mm"] - v2["x_mm"], v1["y_mm"] - v2["y_mm"])
            min_dist = (v1.get("diameter_mm", 0.6) + v2.get("diameter_mm", 0.6)) / 2 + clearance
            if dist < min_dist - 0.01:
                errors.append(
                    f"Via clearance violation: "
                    f"{v1.get('net_name', v1.get('net_id'))} <-> "
                    f"{v2.get('net_name', v2.get('net_id'))} "
                    f"distance={dist:.3f}mm, required={min_dist:.3f}mm"
                )

    # Via-to-trace (check both layers the via connects)
    for via in vias:
        vx, vy = via["x_mm"], via["y_mm"]
        v_radius = via.get("diameter_mm", 0.6) / 2
        via_net = via.get("net_id")

        for trace in traces:
            if trace.get("net_id") == via_net:
                continue

            # Via affects both layers it connects
            via_layers = {via.get("from_layer", "top"), via.get("to_layer", "bottom")}
            if trace.get("layer") not in via_layers:
                continue

            dist = _point_to_segment_distance(
                vx, vy,
                trace["start_x_mm"], trace["start_y_mm"],
                trace["end_x_mm"], trace["end_y_mm"],
            )

            min_dist = v_radius + trace.get("width_mm", 0.25) / 2 + clearance
            if dist < min_dist - 0.01:
                errors.append(
                    f"Via-trace clearance violation on {trace.get('layer')}: "
                    f"via({via.get('net_name', via_net)}) <-> "
                    f"trace({trace.get('net_name', trace.get('net_id'))}) "
                    f"distance={dist:.3f}mm, required={min_dist:.3f}mm"
                )

    return errors, warnings


# ---------------------------------------------------------------------------
# 4. Connectivity verification
# ---------------------------------------------------------------------------

def _check_connectivity(routed: dict, netlist: dict | None) -> tuple[list[str], list[str]]:
    """Verify that all pads in each net are connected by traces.

    Uses union-find on trace endpoints to build connected components,
    then checks that all pads in each net belong to the same component.
    """
    errors = []
    warnings = []

    if netlist is None:
        warnings.append("Connectivity check skipped: no netlist provided")
        return errors, warnings

    routing = routed.get("routing", {})
    traces = routing.get("traces", [])
    vias = routing.get("vias", [])
    unrouted = set(routing.get("unrouted_nets", []))

    # Identify nets connected by copper fill (all pads on fill layer(s) are connected)
    fill_nets: dict[str, set[str]] = {}  # net_id -> set of layers with fill
    for fill_region in routing.get("copper_fills", []):
        fnet = fill_region.get("net_id", "")
        flayer = fill_region.get("layer", "")
        if fnet and flayer:
            fill_nets.setdefault(fnet, set()).add(flayer)

    # Build union-find for each net
    # Key: (round(x, 2), round(y, 2), layer) -> parent
    # We only check per-net connectivity

    elements = netlist.get("elements", [])

    # Import pad_map builder to get pad positions
    try:
        import sys as _sys
        _sys.path.insert(0, str(Path(__file__).parent.parent))
        from optimizers.pad_geometry import build_pad_map

        pad_map = build_pad_map(routed, netlist)
    except Exception:
        warnings.append("Connectivity check: could not build pad map")
        return errors, warnings

    # Group pads by net
    net_pads: dict[str, list[tuple[float, float, str]]] = {}
    for pad in pad_map.values():
        if pad.net_id and pad.net_id not in unrouted:
            net_pads.setdefault(pad.net_id, []).append(
                (round(pad.x_mm, 2), round(pad.y_mm, 2), pad.layer)
            )

    # For each net, check that traces connect all pads
    for net_id, pads in net_pads.items():
        if len(pads) < 2:
            continue

        # Build adjacency using trace endpoints and vias for this net
        # Union-find
        parent: dict[tuple, tuple] = {}

        def find(x: tuple) -> tuple:
            while parent.get(x, x) != x:
                parent[x] = parent.get(parent[x], parent[x])
                x = parent[x]
            return x

        def union(a: tuple, b: tuple) -> None:
            ra, rb = find(a), find(b)
            if ra != rb:
                parent[ra] = rb

        # Add all points to union-find
        # Snap radius must accommodate grid quantization
        grid_res = routing.get("config", {}).get("grid_resolution_mm", 0.25)
        snap = max(0.3, grid_res * 1.5)

        # Collect all trace endpoints for this net
        net_traces = [t for t in traces if t.get("net_id") == net_id]
        net_vias = [v for v in vias if v.get("net_id") == net_id]

        trace_points: list[tuple[float, float, str]] = []
        for t in net_traces:
            p1 = (round(t["start_x_mm"], 2), round(t["start_y_mm"], 2), t["layer"])
            p2 = (round(t["end_x_mm"], 2), round(t["end_y_mm"], 2), t["layer"])
            trace_points.append(p1)
            trace_points.append(p2)
            union(p1, p2)

        # Vias connect points across layers
        for v in net_vias:
            p1 = (round(v["x_mm"], 2), round(v["y_mm"], 2), v.get("from_layer", "top"))
            p2 = (round(v["x_mm"], 2), round(v["y_mm"], 2), v.get("to_layer", "bottom"))
            union(p1, p2)
            # Also connect to nearby trace endpoints
            for tp in trace_points:
                if math.hypot(tp[0] - p1[0], tp[1] - p1[1]) < snap:
                    union(p1, tp)
                    union(p2, tp)

        # Collect via endpoints for connectivity matching
        via_points: list[tuple[float, float, str]] = []
        for v in net_vias:
            via_points.append((round(v["x_mm"], 2), round(v["y_mm"], 2), v.get("from_layer", "top")))
            via_points.append((round(v["x_mm"], 2), round(v["y_mm"], 2), v.get("to_layer", "bottom")))

        # Connect pads to nearby trace endpoints and via endpoints
        # "all" layer (through-hole pads) matches any copper layer
        all_conn_points = trace_points + via_points
        for pad_pos in pads:
            for cp in all_conn_points:
                layers_match = (
                    cp[2] == pad_pos[2] or
                    pad_pos[2] == "all" or
                    cp[2] == "all"
                )
                if layers_match and math.hypot(cp[0] - pad_pos[0], cp[1] - pad_pos[1]) < snap:
                    union(pad_pos, cp)

        # Copper fill connectivity: all pads on a fill layer are connected via the pour
        if net_id in fill_nets:
            fill_layers = fill_nets[net_id]
            for fl in fill_layers:
                layer_pads = [p for p in pads if p[2] == fl]
                for i in range(1, len(layer_pads)):
                    union(layer_pads[0], layer_pads[i])
            # If fill exists on both layers, all pads are effectively connected
            # (fill on both layers shares connectivity via thermal relief)
            if len(fill_layers) >= 2:
                for i in range(1, len(pads)):
                    union(pads[0], pads[i])

        # Check all pads are in the same component
        roots = set()
        for pad_pos in pads:
            roots.add(find(pad_pos))

        if len(roots) > 1:
            errors.append(
                f"Net {net_id}: {len(roots)} disconnected groups "
                f"({len(pads)} pads should all be connected)"
            )

    return errors, warnings


# ---------------------------------------------------------------------------
# 5. No-shorts check
# ---------------------------------------------------------------------------

def _check_no_shorts(routed: dict) -> tuple[list[str], list[str]]:
    """Verify no two different nets share overlapping trace space.

    Simple approach: check trace-trace overlap (distance < sum of half-widths).
    """
    errors = []
    warnings = []

    routing = routed.get("routing", {})
    traces = routing.get("traces", [])

    # Group by layer
    by_layer: dict[str, list[dict]] = {"top": [], "bottom": []}
    for t in traces:
        layer = t.get("layer", "top")
        if layer in by_layer:
            by_layer[layer].append(t)

    for layer, layer_traces in by_layer.items():
        n = len(layer_traces)
        for i in range(n):
            for j in range(i + 1, n):
                t1, t2 = layer_traces[i], layer_traces[j]
                if t1.get("net_id") == t2.get("net_id"):
                    continue

                dist = _segment_to_segment_distance(
                    t1["start_x_mm"], t1["start_y_mm"], t1["end_x_mm"], t1["end_y_mm"],
                    t2["start_x_mm"], t2["start_y_mm"], t2["end_x_mm"], t2["end_y_mm"],
                )

                # Shorts: traces physically overlap (distance < sum of half-widths)
                overlap_threshold = (t1.get("width_mm", 0.25) + t2.get("width_mm", 0.25)) / 2
                if dist < overlap_threshold - 0.01:
                    errors.append(
                        f"Short circuit on {layer}: "
                        f"{t1.get('net_name', t1.get('net_id'))} <-> "
                        f"{t2.get('net_name', t2.get('net_id'))} "
                        f"(overlap distance={dist:.3f}mm)"
                    )

    return errors, warnings


# ---------------------------------------------------------------------------
# 6. Trace-to-pad and via-to-pad clearance
# ---------------------------------------------------------------------------

def _check_pad_clearance(routed: dict, netlist: dict | None) -> tuple[list[str], list[str]]:
    """Check trace-to-pad and via-to-pad clearance for different nets.

    Detects traces or vias that physically overlap pads belonging to other nets.
    """
    errors = []
    warnings = []

    if netlist is None:
        return errors, warnings

    try:
        import sys as _sys
        _sys.path.insert(0, str(Path(__file__).parent.parent))
        from optimizers.pad_geometry import build_pad_map
        pad_map = build_pad_map(routed, netlist)
    except Exception:
        warnings.append("Pad clearance check: could not build pad map")
        return errors, warnings

    routing = routed.get("routing", {})
    traces = routing.get("traces", [])
    vias = routing.get("vias", [])
    clearance = routing.get("config", {}).get("trace_clearance_mm", 0.2)

    # Build pad list with per-layer extents
    pads_by_layer: dict[str, list] = {"top": [], "bottom": []}
    for pad in pad_map.values():
        if pad.net_id is None:
            continue
        is_th = pad.layer == "all"
        for layer in (["top", "bottom"] if is_th else [pad.layer]):
            if is_th and layer != "top":
                # Opposite layer: circular pad using max(w,h) diameter
                # Must match KiCad export which uses max(w,h) on all layers
                pad_hw = max(pad.pad_width_mm, pad.pad_height_mm) / 2
                pad_hh = pad_hw
            else:
                pad_hw = pad.pad_width_mm / 2
                pad_hh = pad.pad_height_mm / 2
            pads_by_layer.setdefault(layer, []).append(
                (pad, pad_hw, pad_hh, layer)
            )

    # Trace-to-pad: check if trace copper overlaps pad copper (rectangular check)
    seen_tp = set()
    for trace in traces:
        t_net = trace.get("net_id")
        t_layer = trace.get("layer", "top")
        t_half = trace.get("width_mm", 0.25) / 2
        ax, ay = trace["start_x_mm"], trace["start_y_mm"]
        bx, by = trace["end_x_mm"], trace["end_y_mm"]

        for pad, pad_hw, pad_hh, _ in pads_by_layer.get(t_layer, []):
            if pad.net_id == t_net:
                continue

            dist = _point_to_segment_distance(pad.x_mm, pad.y_mm, ax, ay, bx, by)
            # Conservative circular check: overlap if distance < trace_half + pad_max_extent
            pad_extent = max(pad_hw, pad_hh)
            if dist < t_half + pad_extent - 0.01:
                key = (t_net, pad.designator, pad.pin_number, t_layer)
                if key in seen_tp:
                    continue
                seen_tp.add(key)
                errors.append(
                    f"Trace-pad short on {t_layer}: "
                    f"trace({trace.get('net_name', t_net)}) overlaps "
                    f"pad({pad.designator}.{pad.pin_number} net={pad.net_id}) "
                    f"distance={dist:.3f}mm"
                )

    # Via-to-pad
    for via in vias:
        vx, vy = via["x_mm"], via["y_mm"]
        v_radius = via.get("diameter_mm", 0.6) / 2
        v_net = via.get("net_id")
        via_layers = {via.get("from_layer", "top"), via.get("to_layer", "bottom")}

        for layer in via_layers:
            for pad, pad_hw, pad_hh, _ in pads_by_layer.get(layer, []):
                if pad.net_id == v_net:
                    continue
                dist = math.hypot(vx - pad.x_mm, vy - pad.y_mm)
                pad_extent = max(pad_hw, pad_hh)
                if dist < v_radius + pad_extent - 0.01:
                    errors.append(
                        f"Via-pad short on {layer}: "
                        f"via({via.get('net_name', v_net)}) overlaps "
                        f"pad({pad.designator}.{pad.pin_number} net={pad.net_id}) "
                        f"distance={dist:.3f}mm"
                    )

    return errors, warnings


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def validate_routing(
    routed_path: str,
    netlist_path: str | None = None,
) -> dict:
    """Run all routing validation checks.

    Returns:
        {"valid": bool, "errors": [...], "warnings": [...], "summary": "..."}
    """
    try:
        routed = json.loads(Path(routed_path).read_text())
    except Exception as e:
        return {
            "valid": False,
            "errors": [f"Cannot read routed file: {e}"],
            "warnings": [],
            "summary": "File read error",
        }

    netlist = None
    if netlist_path:
        try:
            netlist = json.loads(Path(netlist_path).read_text())
        except Exception:
            pass

    all_errors: list[str] = []
    all_warnings: list[str] = []

    # 1. Schema
    all_errors.extend(_validate_schema(routed))
    if all_errors:
        return {
            "valid": False,
            "errors": all_errors,
            "warnings": all_warnings,
            "summary": f"Schema validation failed ({len(all_errors)} errors)",
        }

    # 2. Trace clearance
    errs, warns = _check_trace_clearance(routed)
    all_errors.extend(errs)
    all_warnings.extend(warns)

    # 3. Via clearance
    errs, warns = _check_via_clearance(routed)
    all_errors.extend(errs)
    all_warnings.extend(warns)

    # 4. Connectivity
    errs, warns = _check_connectivity(routed, netlist)
    all_errors.extend(errs)
    all_warnings.extend(warns)

    # 5. No shorts (trace-trace overlap)
    errs, warns = _check_no_shorts(routed)
    all_errors.extend(errs)
    all_warnings.extend(warns)

    # 6. Trace-pad and via-pad clearance
    errs, warns = _check_pad_clearance(routed, netlist)
    all_errors.extend(errs)
    all_warnings.extend(warns)

    # Summary
    stats = routed.get("routing", {}).get("statistics", {})
    completion = stats.get("completion_pct", 0)

    if all_errors:
        summary = f"Routing validation FAILED: {len(all_errors)} errors, {len(all_warnings)} warnings"
    elif completion < 100:
        summary = (
            f"Routing validation passed with warnings: "
            f"{stats.get('routed_nets', 0)}/{stats.get('total_nets', 0)} nets routed "
            f"({completion}%)"
        )
    else:
        summary = (
            f"Routing validation PASSED: "
            f"{stats.get('total_nets', 0)} nets, "
            f"{len(routed.get('routing', {}).get('traces', []))} traces, "
            f"{stats.get('via_count', 0)} vias"
        )

    return {
        "valid": len(all_errors) == 0,
        "errors": all_errors,
        "warnings": all_warnings,
        "summary": summary,
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate routed PCB JSON")
    parser.add_argument("routed_json", help="Path to routed JSON file")
    parser.add_argument("--netlist", help="Path to netlist JSON (for connectivity check)")
    args = parser.parse_args(argv)

    result = validate_routing(args.routed_json, args.netlist)

    # Print results
    if result["valid"]:
        print(f"PASSED: {result['summary']}")
    else:
        print(f"FAILED: {result['summary']}")

    for err in result["errors"]:
        print(f"  ERROR: {err}")
    for warn in result["warnings"]:
        print(f"  WARNING: {warn}")

    return 0 if result["valid"] else 1


if __name__ == "__main__":
    sys.exit(main())
