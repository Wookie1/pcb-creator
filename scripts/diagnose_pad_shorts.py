#!/usr/bin/env python3
"""Diagnostic: find trace-to-pad and via-to-pad clearance violations.

Usage:
    python scripts/diagnose_pad_shorts.py <routed.json> <netlist.json>

Or with --route to route from scratch:
    python scripts/diagnose_pad_shorts.py --route <placement.json> <netlist.json>
"""

import argparse
import json
import math
import sys
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from optimizers.pad_geometry import PadInfo, build_pad_map


def point_to_segment_distance(
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


def _trace_to_rect_distance(
    ax: float, ay: float, bx: float, by: float,
    trace_half: float,
    rect_cx: float, rect_cy: float, rect_hw: float, rect_hh: float,
) -> float:
    """Minimum distance from a trace (with width) to a rectangular pad.

    Computes the distance from the trace centerline to the pad rectangle,
    then subtracts the trace half-width.  Returns the copper-to-copper gap
    (negative means overlap).
    """
    # Find closest point on trace segment to pad center
    dx, dy = bx - ax, by - ay
    length_sq = dx * dx + dy * dy
    if length_sq == 0:
        cpx, cpy = ax, ay
    else:
        t = max(0, min(1, ((rect_cx - ax) * dx + (rect_cy - ay) * dy) / length_sq))
        cpx, cpy = ax + t * dx, ay + t * dy

    # Distance from closest trace-center point to the pad rectangle boundary
    dx_to_rect = max(0.0, abs(cpx - rect_cx) - rect_hw)
    dy_to_rect = max(0.0, abs(cpy - rect_cy) - rect_hh)
    dist_to_rect = math.hypot(dx_to_rect, dy_to_rect)

    # Copper-to-copper gap = distance_to_rect - trace_half_width
    return dist_to_rect - trace_half


def check_trace_pad_clearance(routed: dict, netlist: dict, clearance_mm: float = 0.2) -> list[dict]:
    """Check trace-to-pad clearance for different nets on the same layer.

    Uses rectangular pad geometry (not circular approximation).
    Returns list of violations with details.
    """
    pad_map = build_pad_map(routed, netlist)
    routing = routed.get("routing", {})
    traces = routing.get("traces", [])

    violations = []

    for trace in traces:
        t_net = trace.get("net_id")
        t_layer = trace.get("layer", "top")
        t_width = trace.get("width_mm", 0.25)
        t_half = t_width / 2

        ax, ay = trace["start_x_mm"], trace["start_y_mm"]
        bx, by = trace["end_x_mm"], trace["end_y_mm"]

        for pad in pad_map.values():
            if pad.net_id == t_net:
                continue
            if pad.net_id is None:
                continue

            pad_on_layer = (pad.layer == t_layer or pad.layer == "all")
            if not pad_on_layer:
                continue

            is_th = pad.layer == "all"
            if is_th:
                comp_layer = "top"
                if t_layer == comp_layer:
                    pad_hw = pad.pad_width_mm / 2
                    pad_hh = pad.pad_height_mm / 2
                else:
                    drill_r = min(pad.pad_width_mm, pad.pad_height_mm) / 2
                    pad_hw = drill_r
                    pad_hh = drill_r
            else:
                pad_hw = pad.pad_width_mm / 2
                pad_hh = pad.pad_height_mm / 2

            # Rectangular copper-to-copper gap
            gap = _trace_to_rect_distance(
                ax, ay, bx, by, t_half,
                pad.x_mm, pad.y_mm, pad_hw, pad_hh,
            )

            if gap < clearance_mm - 0.01:
                severity = "SHORT" if gap < 0 else "CLEARANCE"
                violations.append({
                    "type": "trace-pad",
                    "severity": severity,
                    "layer": t_layer,
                    "trace_net": trace.get("net_name", t_net),
                    "trace_net_id": t_net,
                    "pad_net": pad.net_id,
                    "pad_designator": pad.designator,
                    "pad_pin": pad.pin_number,
                    "pad_x": pad.x_mm,
                    "pad_y": pad.y_mm,
                    "gap": gap,
                    "clearance_required": clearance_mm,
                    "trace_start": (ax, ay),
                    "trace_end": (bx, by),
                    "trace_width": t_width,
                    "is_th_pad": is_th,
                })

    return violations


def check_via_pad_clearance(routed: dict, netlist: dict, clearance_mm: float = 0.2) -> list[dict]:
    """Check via-to-pad clearance for different nets."""
    pad_map = build_pad_map(routed, netlist)
    routing = routed.get("routing", {})
    vias = routing.get("vias", [])

    violations = []

    for via in vias:
        vx, vy = via["x_mm"], via["y_mm"]
        v_radius = via.get("diameter_mm", 0.6) / 2
        v_net = via.get("net_id")
        via_layers = {via.get("from_layer", "top"), via.get("to_layer", "bottom")}

        for pad in pad_map.values():
            if pad.net_id == v_net:
                continue
            if pad.net_id is None:
                continue

            # Check if pad is on a layer the via connects
            pad_layers = {"top", "bottom"} if pad.layer == "all" else {pad.layer}
            shared_layers = via_layers & pad_layers
            if not shared_layers:
                continue

            # For TH pads, use appropriate radius per layer
            is_th = pad.layer == "all"
            for layer in shared_layers:
                if is_th:
                    if layer == "top":  # component layer
                        pad_radius = max(pad.pad_width_mm, pad.pad_height_mm) / 2
                    else:
                        pad_radius = min(pad.pad_width_mm, pad.pad_height_mm) / 2
                else:
                    pad_radius = max(pad.pad_width_mm, pad.pad_height_mm) / 2

                dist = math.hypot(vx - pad.x_mm, vy - pad.y_mm)
                min_dist = v_radius + pad_radius + clearance_mm

                if dist < min_dist - 0.01:
                    overlap_dist = v_radius + pad_radius
                    severity = "SHORT" if dist < overlap_dist else "CLEARANCE"

                    violations.append({
                        "type": "via-pad",
                        "severity": severity,
                        "layer": layer,
                        "via_net": via.get("net_name", v_net),
                        "via_net_id": v_net,
                        "pad_net": pad.net_id,
                        "pad_designator": pad.designator,
                        "pad_pin": pad.pin_number,
                        "pad_x": pad.x_mm,
                        "pad_y": pad.y_mm,
                        "pad_radius": pad_radius,
                        "via_x": vx,
                        "via_y": vy,
                        "via_radius": v_radius,
                        "distance": dist,
                        "min_required": min_dist,
                        "is_th_pad": is_th,
                    })

    return violations


def check_fill_pad_clearance(routed: dict, netlist: dict, clearance_mm: float = 0.25) -> list[dict]:
    """Check copper fill to pad clearance for different nets."""
    pad_map = build_pad_map(routed, netlist)
    routing = routed.get("routing", {})
    fills = routing.get("copper_fills", [])

    violations = []

    for fill in fills:
        fill_net = fill.get("net_id")
        fill_layer = fill.get("layer", "top")
        polygons = fill.get("polygons", [])

        # For each pad NOT on the fill net, check if any fill polygon point is too close
        for pad in pad_map.values():
            if pad.net_id == fill_net:
                continue
            if pad.net_id is None:
                continue

            pad_on_layer = (pad.layer == fill_layer or pad.layer == "all")
            if not pad_on_layer:
                continue

            is_th = pad.layer == "all"
            if is_th and fill_layer != "top":
                pad_radius = min(pad.pad_width_mm, pad.pad_height_mm) / 2
            else:
                pad_radius = max(pad.pad_width_mm, pad.pad_height_mm) / 2

            min_dist = pad_radius + clearance_mm

            # Check each polygon edge against pad center
            for poly in polygons:
                # Polygons can be list of [x,y] pairs or list of dicts
                if isinstance(poly, dict):
                    points = poly.get("points", [])
                elif isinstance(poly, list):
                    points = poly
                else:
                    continue
                n = len(points)
                for i in range(n):
                    j = (i + 1) % n
                    p_i = points[i]
                    p_j = points[j]
                    # Handle both [x,y] and {"x": x, "y": y} formats
                    if isinstance(p_i, list):
                        ix, iy = p_i[0], p_i[1]
                    else:
                        ix, iy = p_i["x"], p_i["y"]
                    if isinstance(p_j, list):
                        jx, jy = p_j[0], p_j[1]
                    else:
                        jx, jy = p_j["x"], p_j["y"]
                    dist = point_to_segment_distance(
                        pad.x_mm, pad.y_mm,
                        ix, iy, jx, jy,
                    )
                    if dist < min_dist - 0.01:
                        violations.append({
                            "type": "fill-pad",
                            "severity": "SHORT" if dist < pad_radius else "CLEARANCE",
                            "layer": fill_layer,
                            "fill_net": fill_net,
                            "pad_net": pad.net_id,
                            "pad_designator": pad.designator,
                            "pad_pin": pad.pin_number,
                            "distance": dist,
                            "min_required": min_dist,
                        })
                        break  # one violation per pad per fill is enough

    return violations


def run_diagnostics(routed: dict, netlist: dict):
    """Run all pad-related DRC checks and print results."""
    clearance = routed.get("routing", {}).get("config", {}).get("trace_clearance_mm", 0.2)

    print("=" * 70)
    print("PAD CLEARANCE DIAGNOSTIC")
    print("=" * 70)

    stats = routed.get("routing", {}).get("statistics", {})
    print(f"Routing: {stats.get('routed_nets', '?')}/{stats.get('total_nets', '?')} nets "
          f"({stats.get('completion_pct', '?')}%)")
    print(f"Traces: {len(routed.get('routing', {}).get('traces', []))}")
    print(f"Vias: {len(routed.get('routing', {}).get('vias', []))}")
    print(f"Clearance: {clearance}mm")
    print()

    # 1. Trace-to-pad
    print("-" * 40)
    print("TRACE-TO-PAD CHECK")
    print("-" * 40)
    tp_violations = check_trace_pad_clearance(routed, netlist, clearance)
    shorts = [v for v in tp_violations if v["severity"] == "SHORT"]
    clearances = [v for v in tp_violations if v["severity"] == "CLEARANCE"]
    print(f"  Shorts (copper overlap): {len(shorts)}")
    print(f"  Clearance violations: {len(clearances)}")

    if shorts:
        print("\n  SHORTS (trace copper overlaps pad copper):")
        seen = set()
        for v in shorts:
            key = (v["trace_net"], v["pad_designator"], v["pad_pin"], v["layer"])
            if key in seen:
                continue
            seen.add(key)
            print(f"    {v['layer']:6s} trace({v['trace_net']}) -> "
                  f"pad({v['pad_designator']}.{v['pad_pin']} net={v['pad_net']}) "
                  f"gap={v['gap']:.3f}mm "
                  f"{'TH' if v['is_th_pad'] else 'SMD'}")

    if clearances:
        print(f"\n  CLEARANCE VIOLATIONS (first 10):")
        seen = set()
        count = 0
        for v in clearances:
            key = (v["trace_net"], v["pad_designator"], v["pad_pin"], v["layer"])
            if key in seen:
                continue
            seen.add(key)
            print(f"    {v['layer']:6s} trace({v['trace_net']}) -> "
                  f"pad({v['pad_designator']}.{v['pad_pin']} net={v['pad_net']}) "
                  f"gap={v['gap']:.3f}mm (need {v['clearance_required']:.3f}mm)")
            count += 1
            if count >= 10:
                print(f"    ... and {len(clearances) - 10} more")
                break

    # 2. Via-to-pad
    print("\n" + "-" * 40)
    print("VIA-TO-PAD CHECK")
    print("-" * 40)
    vp_violations = check_via_pad_clearance(routed, netlist, clearance)
    vp_shorts = [v for v in vp_violations if v["severity"] == "SHORT"]
    vp_clearances = [v for v in vp_violations if v["severity"] == "CLEARANCE"]
    print(f"  Shorts: {len(vp_shorts)}")
    print(f"  Clearance violations: {len(vp_clearances)}")

    if vp_shorts:
        print("\n  SHORTS:")
        for v in vp_shorts[:20]:
            print(f"    {v['layer']:6s} via({v['via_net']} @ {v['via_x']:.2f},{v['via_y']:.2f}) -> "
                  f"pad({v['pad_designator']}.{v['pad_pin']} net={v['pad_net']}) "
                  f"dist={v['distance']:.3f}mm")

    if vp_clearances:
        print(f"\n  CLEARANCE VIOLATIONS (first 10):")
        for v in vp_clearances[:10]:
            print(f"    {v['layer']:6s} via({v['via_net']} @ {v['via_x']:.2f},{v['via_y']:.2f}) -> "
                  f"pad({v['pad_designator']}.{v['pad_pin']} net={v['pad_net']}) "
                  f"dist={v['distance']:.3f}mm (need {v['min_required']:.3f}mm)")

    # 3. Fill-to-pad
    print("\n" + "-" * 40)
    print("FILL-TO-PAD CHECK")
    print("-" * 40)
    fp_violations = check_fill_pad_clearance(routed, netlist)
    fp_shorts = [v for v in fp_violations if v["severity"] == "SHORT"]
    print(f"  Shorts: {len(fp_shorts)}")
    print(f"  Clearance violations: {len(fp_violations) - len(fp_shorts)}")

    if fp_shorts:
        print("\n  SHORTS:")
        for v in fp_shorts[:10]:
            print(f"    {v['layer']:6s} fill({v['fill_net']}) -> "
                  f"pad({v['pad_designator']}.{v['pad_pin']} net={v['pad_net']}) "
                  f"dist={v['distance']:.3f}mm")

    # Summary
    total_shorts = len(shorts) + len(vp_shorts) + len(fp_shorts)
    total_clearance = len(clearances) + len(vp_clearances) + (len(fp_violations) - len(fp_shorts))
    print("\n" + "=" * 70)
    print(f"TOTAL: {total_shorts} shorts, {total_clearance} clearance violations")
    print("=" * 70)

    return tp_violations, vp_violations, fp_violations


def main():
    parser = argparse.ArgumentParser(description="Diagnose pad clearance violations")
    parser.add_argument("file1", help="Routed JSON (or placement JSON with --route)")
    parser.add_argument("file2", help="Netlist JSON")
    parser.add_argument("--route", action="store_true", help="Route from placement+netlist first")
    args = parser.parse_args()

    if args.route:
        # Route from scratch
        from optimizers.router import route_board, RouterConfig
        placement = json.loads(Path(args.file1).read_text())
        netlist = json.loads(Path(args.file2).read_text())
        config = RouterConfig()
        print("Routing board...")
        routed = route_board(placement, netlist, config)
        print("Routing complete.\n")
    else:
        routed = json.loads(Path(args.file1).read_text())
        netlist = json.loads(Path(args.file2).read_text())

    run_diagnostics(routed, netlist)


if __name__ == "__main__":
    main()
