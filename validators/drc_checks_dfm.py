"""DFM (Design for Manufacturing) rule checks.

Validates a routed PCB design against manufacturer-specific capabilities:
trace width, clearance, via specs, annular ring, hole spacing, copper-to-edge,
silkscreen, and trace current capacity.

Each check returns a list of DRCViolation dataclass instances.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field, asdict


@dataclass
class DRCViolation:
    """A single DRC rule violation."""
    rule: str
    severity: str        # "error" or "warning"
    message: str
    location: dict | None = None   # {"x_mm": ..., "y_mm": ..., "layer": ...}
    value: float | None = None     # measured value
    required: float | None = None  # required min/max
    net: str | None = None

    def to_dict(self) -> dict:
        d = asdict(self)
        # Remove None values for compact output
        return {k: v for k, v in d.items() if v is not None}


# ---------------------------------------------------------------------------
# DFM checks
# ---------------------------------------------------------------------------

def check_trace_width_min(routed: dict, dfm: dict) -> list[DRCViolation]:
    """Every trace width must meet manufacturer minimum."""
    min_w = dfm.get("trace_width_min_mm", 0.127)
    violations = []
    for trace in routed.get("routing", {}).get("traces", []):
        w = trace.get("width_mm", 0.25)
        if w < min_w - 0.001:
            violations.append(DRCViolation(
                rule="trace_width_min",
                severity="error",
                message=f"Trace width {w:.3f}mm < minimum {min_w:.3f}mm",
                location={
                    "x_mm": round(trace["start_x_mm"], 2),
                    "y_mm": round(trace["start_y_mm"], 2),
                    "layer": trace.get("layer", "top"),
                },
                value=w,
                required=min_w,
                net=trace.get("net_name"),
            ))
    return violations


def check_clearance_min(routed: dict, dfm: dict) -> list[DRCViolation]:
    """Trace-to-trace clearance must meet manufacturer minimum.

    Only checks pairs on the same layer with different nets.
    Uses segment midpoint distance as a fast approximation.
    """
    min_clr = dfm.get("clearance_min_mm", 0.127)
    violations = []
    traces = routed.get("routing", {}).get("traces", [])

    # Group traces by layer for efficiency
    by_layer: dict[str, list[dict]] = {}
    for t in traces:
        by_layer.setdefault(t.get("layer", "top"), []).append(t)

    for layer, layer_traces in by_layer.items():
        n = len(layer_traces)
        for i in range(n):
            for j in range(i + 1, n):
                t1, t2 = layer_traces[i], layer_traces[j]
                if t1.get("net_id") == t2.get("net_id"):
                    continue

                # Compute minimum distance between segments
                dist = _segment_distance(
                    t1["start_x_mm"], t1["start_y_mm"], t1["end_x_mm"], t1["end_y_mm"],
                    t2["start_x_mm"], t2["start_y_mm"], t2["end_x_mm"], t2["end_y_mm"],
                )
                required = (t1.get("width_mm", 0.25) + t2.get("width_mm", 0.25)) / 2 + min_clr
                if dist < required - 0.01:
                    actual_gap = dist - (t1.get("width_mm", 0.25) + t2.get("width_mm", 0.25)) / 2
                    violations.append(DRCViolation(
                        rule="clearance_min",
                        severity="error",
                        message=f"Clearance {actual_gap:.3f}mm < minimum {min_clr:.3f}mm "
                                f"({t1.get('net_name', '?')} <-> {t2.get('net_name', '?')})",
                        location={
                            "x_mm": round((t1["start_x_mm"] + t1["end_x_mm"]) / 2, 2),
                            "y_mm": round((t1["start_y_mm"] + t1["end_y_mm"]) / 2, 2),
                            "layer": layer,
                        },
                        value=round(actual_gap, 4),
                        required=min_clr,
                    ))
                    if len(violations) > 50:  # cap output
                        return violations

    return violations


def check_via_drill_min(routed: dict, dfm: dict) -> list[DRCViolation]:
    """Every via drill must meet manufacturer minimum."""
    min_drill = dfm.get("via_drill_min_mm", 0.3)
    violations = []
    for via in routed.get("routing", {}).get("vias", []):
        drill = via.get("drill_mm", 0.3)
        if drill < min_drill - 0.001:
            violations.append(DRCViolation(
                rule="via_drill_min",
                severity="error",
                message=f"Via drill {drill:.3f}mm < minimum {min_drill:.3f}mm",
                location={"x_mm": via["x_mm"], "y_mm": via["y_mm"]},
                value=drill,
                required=min_drill,
                net=via.get("net_name"),
            ))
    return violations


def check_annular_ring(routed: dict, dfm: dict) -> list[DRCViolation]:
    """Via annular ring must meet manufacturer minimum."""
    min_ring = dfm.get("min_annular_ring_mm", 0.13)
    violations = []
    for via in routed.get("routing", {}).get("vias", []):
        drill = via.get("drill_mm", 0.3)
        diameter = via.get("diameter_mm", 0.6)
        ring = (diameter - drill) / 2
        if ring < min_ring - 0.001:
            violations.append(DRCViolation(
                rule="annular_ring",
                severity="error",
                message=f"Via annular ring {ring:.3f}mm < minimum {min_ring:.3f}mm",
                location={"x_mm": via["x_mm"], "y_mm": via["y_mm"]},
                value=round(ring, 4),
                required=min_ring,
                net=via.get("net_name"),
            ))
    return violations


def check_hole_to_hole(routed: dict, netlist: dict, dfm: dict) -> list[DRCViolation]:
    """Minimum distance between drill holes (via-to-via, via-to-TH-pad, TH-pad-to-TH-pad)."""
    min_dist = dfm.get("min_hole_to_hole_mm", 0.5)
    violations = []

    # Collect all drill hole positions
    holes: list[tuple[float, float, str]] = []  # (x, y, label)

    for via in routed.get("routing", {}).get("vias", []):
        holes.append((via["x_mm"], via["y_mm"], f"via({via.get('net_name', '?')})"))

    # Through-hole pad positions
    try:
        from optimizers.pad_geometry import build_pad_map
        pad_map = build_pad_map(routed, netlist)
        for pad in pad_map.values():
            if pad.layer == "all":  # through-hole
                holes.append((pad.x_mm, pad.y_mm, f"{pad.designator}.{pad.pin_number}"))
    except Exception:
        pass

    # Pairwise check
    for i in range(len(holes)):
        for j in range(i + 1, len(holes)):
            dx = holes[i][0] - holes[j][0]
            dy = holes[i][1] - holes[j][1]
            dist = math.hypot(dx, dy)
            if dist < min_dist - 0.01:
                violations.append(DRCViolation(
                    rule="hole_to_hole",
                    severity="error",
                    message=f"Hole spacing {dist:.3f}mm < minimum {min_dist:.3f}mm "
                            f"({holes[i][2]} <-> {holes[j][2]})",
                    location={"x_mm": round(holes[i][0], 2), "y_mm": round(holes[i][1], 2)},
                    value=round(dist, 4),
                    required=min_dist,
                ))
                if len(violations) > 50:
                    return violations

    return violations


def check_copper_to_edge(routed: dict, netlist: dict, dfm: dict) -> list[DRCViolation]:
    """All copper features must be at least min_copper_to_edge_mm from board outline."""
    min_edge = dfm.get("min_copper_to_edge_mm", 0.2)
    board = routed.get("board", {})
    board_w = board.get("width_mm", 50.0)
    board_h = board.get("height_mm", 50.0)
    violations = []

    def edge_dist(x: float, y: float) -> float:
        return min(x, y, board_w - x, board_h - y)

    # Check trace endpoints
    for trace in routed.get("routing", {}).get("traces", []):
        for prefix in ("start_", "end_"):
            x = trace[f"{prefix}x_mm"]
            y = trace[f"{prefix}y_mm"]
            d = edge_dist(x, y)
            if d < min_edge - 0.01:
                violations.append(DRCViolation(
                    rule="copper_to_edge",
                    severity="error",
                    message=f"Copper {d:.3f}mm from board edge < minimum {min_edge:.3f}mm",
                    location={"x_mm": round(x, 2), "y_mm": round(y, 2), "layer": trace.get("layer")},
                    value=round(d, 4),
                    required=min_edge,
                    net=trace.get("net_name"),
                ))
                if len(violations) > 20:
                    return violations

    # Check vias
    for via in routed.get("routing", {}).get("vias", []):
        r = via.get("diameter_mm", 0.6) / 2
        d = edge_dist(via["x_mm"], via["y_mm"]) - r
        if d < min_edge - 0.01:
            violations.append(DRCViolation(
                rule="copper_to_edge",
                severity="error",
                message=f"Via copper {d:.3f}mm from board edge < minimum {min_edge:.3f}mm",
                location={"x_mm": via["x_mm"], "y_mm": via["y_mm"]},
                value=round(d, 4),
                required=min_edge,
                net=via.get("net_name"),
            ))

    return violations


def check_silkscreen(routed: dict, dfm: dict) -> list[DRCViolation]:
    """Silkscreen text/stroke must meet manufacturer minimums."""
    min_height = dfm.get("silkscreen_min_height_mm", 0.8)
    min_width = dfm.get("silkscreen_min_width_mm", 0.15)
    violations = []

    for silk in routed.get("silkscreen", []):
        if silk.get("type") == "text":
            fh = silk.get("font_height_mm", 1.0)
            stroke_w = fh * 0.15  # proportional stroke
            if fh < min_height - 0.01:
                violations.append(DRCViolation(
                    rule="silkscreen_height",
                    severity="warning",
                    message=f"Silkscreen text '{silk.get('text', '?')}' height {fh:.2f}mm "
                            f"< minimum {min_height:.2f}mm",
                    location={"x_mm": silk.get("x_mm", 0), "y_mm": silk.get("y_mm", 0)},
                    value=fh,
                    required=min_height,
                ))
            if stroke_w < min_width - 0.01:
                violations.append(DRCViolation(
                    rule="silkscreen_width",
                    severity="warning",
                    message=f"Silkscreen stroke {stroke_w:.3f}mm < minimum {min_width:.3f}mm",
                    location={"x_mm": silk.get("x_mm", 0), "y_mm": silk.get("y_mm", 0)},
                    value=round(stroke_w, 4),
                    required=min_width,
                ))

    return violations


def check_trace_current_capacity(
    routed: dict,
    netlist: dict,
    copper_oz: float = 0.5,
) -> list[DRCViolation]:
    """Verify each trace can carry its net's estimated current per IPC-2221."""
    violations = []

    try:
        import sys
        from pathlib import Path
        sys.path.insert(0, str(Path(__file__).parent.parent))
        from optimizers.router import ipc2221_trace_width, compute_net_current
        from optimizers.ratsnest import build_connectivity
    except ImportError:
        return violations

    nets = build_connectivity(netlist)

    # Build net current estimates
    net_currents: dict[str, float] = {}
    for net in nets:
        current = compute_net_current(net, netlist)
        if current > 0:
            net_currents[net.net_id] = current

    # Build net_id -> name lookup
    net_names: dict[str, str] = {}
    for elem in netlist.get("elements", []):
        if elem.get("element_type") == "net":
            net_names[elem["net_id"]] = elem.get("name", elem["net_id"])

    # Check each trace
    for trace in routed.get("routing", {}).get("traces", []):
        net_id = trace.get("net_id", "")
        current = net_currents.get(net_id)
        if current is None or current <= 0:
            continue

        min_width = ipc2221_trace_width(current, copper_oz)
        actual_width = trace.get("width_mm", 0.25)

        if actual_width < min_width - 0.01:
            violations.append(DRCViolation(
                rule="trace_current_capacity",
                severity="error",
                message=f"Trace width {actual_width:.3f}mm cannot carry "
                        f"{current * 1000:.0f}mA (IPC-2221 minimum: {min_width:.3f}mm)",
                location={
                    "x_mm": round(trace["start_x_mm"], 2),
                    "y_mm": round(trace["start_y_mm"], 2),
                    "layer": trace.get("layer"),
                },
                value=actual_width,
                required=round(min_width, 4),
                net=net_names.get(net_id, net_id),
            ))

    # Deduplicate: keep one per net
    seen_nets: set[str] = set()
    deduped = []
    for v in violations:
        if v.net not in seen_nets:
            deduped.append(v)
            seen_nets.add(v.net)
    return deduped


# ---------------------------------------------------------------------------
# Geometry helpers
# ---------------------------------------------------------------------------

def _point_to_segment_dist(px: float, py: float,
                           ax: float, ay: float,
                           bx: float, by: float) -> float:
    """Minimum distance from point (px, py) to segment (ax,ay)-(bx,by)."""
    dx, dy = bx - ax, by - ay
    len_sq = dx * dx + dy * dy
    if len_sq < 1e-12:
        return math.hypot(px - ax, py - ay)
    t = max(0, min(1, ((px - ax) * dx + (py - ay) * dy) / len_sq))
    proj_x = ax + t * dx
    proj_y = ay + t * dy
    return math.hypot(px - proj_x, py - proj_y)


def _segment_distance(ax1: float, ay1: float, ax2: float, ay2: float,
                       bx1: float, by1: float, bx2: float, by2: float) -> float:
    """Minimum distance between two line segments."""
    d1 = _point_to_segment_dist(ax1, ay1, bx1, by1, bx2, by2)
    d2 = _point_to_segment_dist(ax2, ay2, bx1, by1, bx2, by2)
    d3 = _point_to_segment_dist(bx1, by1, ax1, ay1, ax2, ay2)
    d4 = _point_to_segment_dist(bx2, by2, ax1, ay1, ax2, ay2)
    return min(d1, d2, d3, d4)
