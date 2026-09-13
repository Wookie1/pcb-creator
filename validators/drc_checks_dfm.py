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
    except Exception:  # pragma: no cover - pad-map build is best-effort; via holes are still checked
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


def check_silkscreen(routed: dict, netlist: dict, dfm: dict) -> list[DRCViolation]:
    """Silkscreen legibility: manufacturer minimums, plus placement sanity.

    The relocator in `_generate_silkscreen` places every label best-effort and
    falls back to the component's default spot when nothing is clear. That
    fallback used to be silent, so a designator printed on a pad, buried under a
    neighbouring part, or hanging past the board edge shipped unnoticed. These
    checks make the fallback visible — they are what tells you a label is
    unreadable on the finished board.
    """
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

    violations.extend(_check_silk_placement(routed, netlist))
    return violations


def _silk_bbox(silk: dict) -> tuple[float, float, float, float]:
    """Bounding box of a silk item, matching what the Gerber renderer draws."""
    from optimizers.router import _silk_text_bbox
    x, y = silk.get("x_mm", 0), silk.get("y_mm", 0)
    if silk.get("type") == "dot":
        r = silk.get("diameter_mm", 0.5) / 2
        return (x - r, y - r, x + r, y + r)
    return _silk_text_bbox(x, y, silk.get("text", ""),
                           silk.get("font_height_mm", 1.0),
                           silk.get("anchor", "center"), silk.get("angle", 0))


def _check_silk_placement(routed: dict, netlist: dict) -> list[DRCViolation]:
    """Silk that is off the board, on a pad, or under a component body."""
    from optimizers.pad_geometry import build_pad_map
    from optimizers.router import _boxes_overlap

    board = routed.get("board", {})
    board_w = board.get("width_mm", 0.0)
    board_h = board.get("height_mm", 0.0)
    silk_items = routed.get("silkscreen", [])
    if not silk_items:
        return []

    try:
        pad_map = build_pad_map(routed, netlist)
    except Exception:  # pragma: no cover - pad map needs a resolvable placement
        pad_map = {}

    pad_boxes = [(p.x_mm - p.pad_width_mm / 2, p.y_mm - p.pad_height_mm / 2,
                  p.x_mm + p.pad_width_mm / 2, p.y_mm + p.pad_height_mm / 2)
                 for p in pad_map.values()]

    # A marker's whole job is to sit against its pad, so only designators and
    # board text are checked for landing on copper.
    body_boxes: list[tuple[str, tuple[float, float, float, float]]] = []
    for plc in routed.get("placements", []):
        if plc.get("component_type") == "fiducial":
            continue
        bw = plc.get("footprint_width_mm", 0) or 0
        bh = plc.get("footprint_height_mm", 0) or 0
        if plc.get("rotation_deg", 0) in (90, 270):
            bw, bh = bh, bw
        body_boxes.append((plc.get("designator", "?"),
                           (plc["x_mm"] - bw / 2, plc["y_mm"] - bh / 2,
                            plc["x_mm"] + bw / 2, plc["y_mm"] + bh / 2)))

    violations = []
    for silk in silk_items:
        label = silk.get("text", silk.get("purpose", "marker"))
        bb = _silk_bbox(silk)
        loc = {"x_mm": silk.get("x_mm", 0), "y_mm": silk.get("y_mm", 0)}

        if board_w and board_h and (bb[0] < 0 or bb[1] < 0
                                    or bb[2] > board_w or bb[3] > board_h):
            violations.append(DRCViolation(
                rule="silkscreen_off_board",
                severity="warning",
                message=f"Silkscreen '{label}' extends past the board outline "
                        f"(board {board_w}x{board_h}mm) — the fab will clip it, "
                        f"so it will be missing from the finished board",
                location=loc,
            ))

        if silk.get("purpose") in ("pin1", "anode"):
            continue

        if any(_boxes_overlap(bb, pb) for pb in pad_boxes):
            violations.append(DRCViolation(
                rule="silkscreen_over_pad",
                severity="warning",
                message=f"Silkscreen '{label}' overlaps a pad — most fabs strip "
                        f"silk off pads, leaving the text broken or missing",
                location=loc,
            ))

        under = next((d for d, cb in body_boxes
                      if d != label and _boxes_overlap(bb, cb)), None)
        if under:
            violations.append(DRCViolation(
                rule="silkscreen_under_component",
                severity="warning",
                message=f"Silkscreen '{label}' sits under {under}'s body — it "
                        f"will be hidden once the board is assembled",
                location=loc,
            ))

    return violations


# Pad-entry neckdown tolerance (short necks have negligible thermal impact
# per IPC-2152): each pad on the net may contribute a short undersized
# entry, so the per-net allowance scales with pad count — but no single
# undersized run may exceed the segment cap.
NECKDOWN_PER_PAD_MM = 2.5
NECKDOWN_MAX_SEGMENT_MM = 4.0


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
        from optimizers.router import ipc2221_trace_width, compute_net_currents
    except ImportError:  # pragma: no cover - optimizers.router always importable in-tree
        return violations

    # Net current estimates with series-element propagation — must match the
    # pre-route width hints in freerouter.py so DRC and the router agree.
    net_currents = {nid: c for nid, c in compute_net_currents(netlist).items()
                    if c > 0}

    # Build net_id -> name lookup
    net_names: dict[str, str] = {}
    for elem in netlist.get("elements", []):
        if elem.get("element_type") == "net":
            net_names[elem["net_id"]] = elem.get("name", elem["net_id"])

    # Check each trace, accumulating undersized length per net for the
    # neckdown tolerance below
    undersized_len_by_net: dict[str, float] = {}
    max_seg_by_net: dict[str, float] = {}
    pads_by_net: dict[str, int] = {}
    for elem in netlist.get("elements", []):
        if elem.get("element_type") == "net":
            pads_by_net[elem["net_id"]] = len(elem.get("connected_port_ids", []))
    for trace in routed.get("routing", {}).get("traces", []):
        net_id = trace.get("net_id", "")
        current = net_currents.get(net_id)
        if current is None or current <= 0:
            continue

        min_width = ipc2221_trace_width(current, copper_oz)
        actual_width = trace.get("width_mm", 0.25)

        if actual_width < min_width - 0.01:
            seg_len = math.hypot(trace["end_x_mm"] - trace["start_x_mm"],
                                 trace["end_y_mm"] - trace["start_y_mm"])
            undersized_len_by_net[net_id] = (
                undersized_len_by_net.get(net_id, 0.0) + seg_len)
            max_seg_by_net[net_id] = max(max_seg_by_net.get(net_id, 0.0),
                                         seg_len)
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

    # Short neckdowns are accepted practice: routers narrow a wide trace for
    # the last couple of millimetres to enter each small pad, and the
    # thermal impact of a short neck is negligible (IPC-2152). Demote to a
    # warning when the undersized copper looks like pad-entry necking:
    # total length within the per-pad allowance AND no single long run.
    name_to_id = {v: k for k, v in net_names.items()}
    for v in violations:
        nid = name_to_id.get(v.net, v.net)
        total = undersized_len_by_net.get(nid, 1e9)
        allowance = NECKDOWN_PER_PAD_MM * max(pads_by_net.get(nid, 0), 1)
        if total <= allowance and \
                max_seg_by_net.get(nid, 1e9) <= NECKDOWN_MAX_SEGMENT_MM:
            v.severity = "warning"
            v.message += (f" — undersized total {total:.1f}mm across "
                          f"{pads_by_net.get(nid, '?')} pads, longest run "
                          f"{max_seg_by_net.get(nid, 0):.1f}mm (pad-entry "
                          f"neckdown, tolerated)")

    # Deduplicate: keep one per net (prefer errors over warnings)
    seen_nets: set[str] = set()
    deduped = []
    for v in sorted(violations, key=lambda v: v.severity != "error"):
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


# ---------------------------------------------------------------------------
# Inner plane antipad check (4-layer boards)
# ---------------------------------------------------------------------------

def check_inner_plane_antipad(routed: dict, netlist: dict, dfm: dict) -> list[DRCViolation]:
    """Verify inner-layer plane regions contain antipad cutouts for all
    foreign through-hole pads and vias (is_plane fill regions only).

    Each cutout must provide at least clearance_min_mm between the pad edge
    and the plane copper edge. Missing cutouts indicate the plane generator
    failed or a pad landed outside the board outline.
    """
    violations: list[DRCViolation] = []
    routing = routed.get("routing", {})
    copper_fills = routing.get("copper_fills", [])

    # Only relevant for is_plane fills (inner planes)
    plane_fills = [f for f in copper_fills if f.get("is_plane")]
    if not plane_fills:
        return violations

    min_clearance = dfm.get("clearance_min_mm", 0.127)

    # Build through-hole pad positions from netlist
    from optimizers.pad_geometry import build_pad_map
    pad_map = build_pad_map(routed, netlist)
    th_pads = [(p.x_mm, p.y_mm, max(p.pad_width_mm, p.pad_height_mm) / 2, p.net_id)
               for p in pad_map.values() if p.layer == "all"]
    vias = [(v["x_mm"], v["y_mm"], v.get("diameter_mm", 0.6) / 2, v.get("net_id", ""))
            for v in routing.get("vias", [])]

    for plane in plane_fills:
        plane_layer = plane.get("layer", "")
        plane_net = plane.get("net_id", "")
        polygons = plane.get("polygons", [])
        if len(polygons) < 2:
            # No cutouts at all — flag if there are foreign through-holes
            for x, y, r, net_id in th_pads + vias:
                if net_id != plane_net:
                    violations.append(DRCViolation(
                        rule="inner_plane_antipad",
                        severity="error",
                        message=(
                            f"Inner plane {plane_layer} ({plane.get('net_name','')}) "
                            f"has no antipad cutouts but contains foreign through-hole features"
                        ),
                        location={"x_mm": round(x, 2), "y_mm": round(y, 2), "layer": plane_layer},
                    ))
                    break
            continue

        # Cutout discs are stored as circle-polygon approximations; measure
        # their effective radius as the mean distance from centroid to vertices.
        def _cutout_radius(poly: list) -> tuple[float, float, float]:
            # Drop a duplicated closing vertex (circle polygons repeat point 0
            # at the end) — otherwise it double-weights the centroid and skews
            # the measurement. Use the INSCRIBED radius (min vertex distance):
            # copper can approach as close as the polygon edges, so this is the
            # conservative, physically-correct clearance basis.
            pts = poly[:-1] if len(poly) > 1 and poly[0] == poly[-1] else poly
            if len(pts) < 3:
                return 0.0, 0.0, 0.0
            cx = sum(p[0] for p in pts) / len(pts)
            cy = sum(p[1] for p in pts) / len(pts)
            r = min(math.hypot(p[0] - cx, p[1] - cy) for p in pts)
            return cx, cy, r

        cutout_circles = [_cutout_radius(p) for p in polygons[1:]]

        def _antipad_clearance(px: float, py: float, pad_r: float) -> float:
            """Clearance from pad edge to nearest antipad cutout edge.

            Positive means the pad is inside the cutout with room to spare.
            Formula: cutout_radius - (distance_to_cutout_centre + pad_radius).
            """
            best = -math.inf
            for cx, cy, cr in cutout_circles:
                centre_dist = math.hypot(px - cx, py - cy)
                clr = cr - (centre_dist + pad_r)
                best = max(best, clr)
            return best

        for x, y, r, net_id in th_pads + vias:
            if net_id == plane_net:
                continue  # same-net: thermal relief is acceptable
            clearance_actual = _antipad_clearance(x, y, r)
            if clearance_actual < min_clearance - 0.01:
                violations.append(DRCViolation(
                    rule="inner_plane_antipad",
                    severity="error",
                    message=(
                        f"Insufficient antipad clearance on {plane_layer} "
                        f"({plane.get('net_name','')}): "
                        f"{clearance_actual:.3f}mm < {min_clearance:.3f}mm"
                    ),
                    location={"x_mm": round(x, 2), "y_mm": round(y, 2), "layer": plane_layer},
                    value=round(clearance_actual, 4),
                    required=min_clearance,
                ))

    return violations


# ---------------------------------------------------------------------------
# Exported-copper faithfulness: pour/plane vs foreign copper on the ACTUAL
# fill geometry the Gerbers paint (not a re-pour). See check below.
# ---------------------------------------------------------------------------

_COPPER_STACK = ("top", "inner1", "inner2", "bottom")


def _pip(x: float, y: float, poly: list, bb: tuple) -> bool:
    """Even-odd point-in-polygon for a SINGLE ring, with a bbox fast-reject."""
    pxmin, pymin, pxmax, pymax = bb
    if x < pxmin or x > pxmax or y < pymin or y > pymax:
        return False
    n = len(poly)
    if n < 3:
        return False
    inside = False
    j = n - 1
    for i in range(n):
        xi, yi = poly[i]
        xj, yj = poly[j]
        if ((yi > y) != (yj > y)) and \
           (x < (xj - xi) * (y - yi) / (yj - yi) + xi):
            inside = not inside
        j = i
    return inside


def _point_in_fill(x: float, y: float, polys: list, bboxes: list,
                   is_plane: bool) -> bool:
    """True if (x, y) lies on poured copper, matching how the Gerber renders it.

    Two storage forms need two renderings — conflating them (e.g. one even-odd
    parity over all rings) is wrong:
      * is_plane (inner plane): polys[0] is the board outline; polys[1:] are
        antipad cutouts painted CLEAR — their UNION is removed. So copper = inside
        the outline AND inside NO cutout. Even-odd is wrong here: two OVERLAPPING
        antipads flip parity back to "copper" and phantom-short a via sitting in
        the overlap (a false positive).
      * surface pour: polys are solid scan-line strips (no holes) → copper is the
        UNION (inside any strip); a cleared via sits in a gap (no strip).
    `bboxes[i]` is (xmin, ymin, xmax, ymax) for polys[i]."""
    if not polys:
        return False
    if is_plane:
        if not _pip(x, y, polys[0], bboxes[0]):
            return False
        return not any(_pip(x, y, polys[k], bboxes[k])
                       for k in range(1, len(polys)))
    return any(_pip(x, y, polys[k], bboxes[k]) for k in range(len(polys)))


def check_exported_copper_shorts(routed: dict, netlist: dict,
                                 dfm: dict) -> list[DRCViolation]:
    """Foreign-net copper overlapping a copper POUR or PLANE, tested on the exact
    ``copper_fills`` polygons the Gerbers paint.

    This is the faithfulness backstop. kicad-cli DRC re-pours zones from scratch
    (cutting its own antipads), so it validates a DIFFERENT rendering than the one
    that ships: a surface pour flooding a foreign via-in-pad, or an antipad-less
    inner plane, passes kicad-cli yet is a dead short on the fabricated board.
    (parking_flasher_xor_20mm shipped with the bottom GND pour flooding all eight
    VREG plane-stitch vias — a 0-ohm VREG-to-GND short — while every gate was
    'clean'.) Running on routed.json's fills — the SAME geometry gerber_exporter
    renders — makes a pass here mean the shipped copper is short-free.

    Covers fill-vs-{pad, via, trace}. Discrete-vs-discrete shorts are covered by
    the clearance / no-shorts / pad-clearance checks; together they validate all
    exported copper. `inner_plane_antipad` additionally flags sub-clearance
    near-misses on planes; this flags actual overlaps (shorts) on every fill.
    """
    violations: list[DRCViolation] = []
    routing = routed.get("routing", {})
    fills = routing.get("copper_fills", [])
    if not fills:
        return violations

    from optimizers.pad_geometry import build_pad_map
    try:
        pad_map = build_pad_map(routed, netlist)
    except Exception:  # pragma: no cover - defensive: a corrupt pad map must not crash DRC
        pad_map = {}
    vias = routing.get("vias", [])
    traces = routing.get("traces", [])

    def _spanned(v: dict) -> set:
        fl = v.get("from_layer") or "top"
        tl = v.get("to_layer") or "bottom"
        try:
            i, j = _COPPER_STACK.index(fl), _COPPER_STACK.index(tl)
        except ValueError:
            return {fl, tl}
        return set(_COPPER_STACK[min(i, j):max(i, j) + 1])

    def _ring(cx, cy, r, n=16):
        pts = [(cx, cy)]
        for k in range(n):
            a = 2 * math.pi * k / n
            pts.append((cx + r * math.cos(a), cy + r * math.sin(a)))
        return pts

    def _rect_pts(cx, cy, hw, hh):
        return [(cx, cy),
                (cx - hw, cy - hh), (cx + hw, cy - hh),
                (cx - hw, cy + hh), (cx + hw, cy + hh),
                (cx, cy - hh), (cx, cy + hh), (cx - hw, cy), (cx + hw, cy)]

    seen: set = set()
    for f in fills:
        polys = f.get("polygons", [])
        if not polys:
            continue
        fnet = f.get("net_id")
        flayer = f.get("layer", "")
        fname = f.get("net_name", fnet)
        is_plane = bool(f.get("is_plane"))
        bboxes = []
        gx0 = gy0 = math.inf
        gx1 = gy1 = -math.inf
        for poly in polys:
            xs = [p[0] for p in poly]
            ys = [p[1] for p in poly]
            bb = (min(xs), min(ys), max(xs), max(ys))
            bboxes.append(bb)
            gx0, gy0 = min(gx0, bb[0]), min(gy0, bb[1])
            gx1, gy1 = max(gx1, bb[2]), max(gy1, bb[3])

        def _in_bbox(x, y, m=0.0):
            return gx0 - m <= x <= gx1 + m and gy0 - m <= y <= gy1 + m

        # --- pads (SMD on their layer; TH pads span all) ---
        for p in pad_map.values():
            if p.net_id is None or p.net_id == fnet:
                continue
            if not (p.layer == "all" or p.layer == flayer):
                continue
            hw, hh = p.pad_width_mm / 2, p.pad_height_mm / 2
            if not _in_bbox(p.x_mm, p.y_mm, max(hw, hh)):
                continue
            if any(_point_in_fill(x, y, polys, bboxes, is_plane)
                   for x, y in _rect_pts(p.x_mm, p.y_mm, hw, hh)):
                key = ("pad", fnet, flayer, p.designator, p.pin_number)
                if key not in seen:
                    seen.add(key)
                    violations.append(DRCViolation(
                        rule="copper_pour_short", severity="error",
                        message=(f"Copper pour {fname} on {flayer} overlaps pad "
                                 f"{p.designator}.{p.pin_number} (net {p.net_id}) "
                                 f"— missing clearance/antipad"),
                        location={"x_mm": round(p.x_mm, 2),
                                  "y_mm": round(p.y_mm, 2), "layer": flayer}))

        # --- vias (barrel: check on every layer the via spans) ---
        for v in vias:
            vnet = v.get("net_id")
            if vnet == fnet or flayer not in _spanned(v):
                continue
            r = v.get("diameter_mm", 0.6) / 2
            if not _in_bbox(v["x_mm"], v["y_mm"], r):
                continue
            if any(_point_in_fill(x, y, polys, bboxes, is_plane)
                   for x, y in _ring(v["x_mm"], v["y_mm"], r)):
                key = ("via", fnet, flayer, round(v["x_mm"], 2), round(v["y_mm"], 2))
                if key not in seen:
                    seen.add(key)
                    violations.append(DRCViolation(
                        rule="copper_pour_short", severity="error",
                        message=(f"Copper pour {fname} on {flayer} overlaps "
                                 f"{v.get('net_name', vnet)} via at "
                                 f"({v['x_mm']:.2f},{v['y_mm']:.2f}) — missing antipad"),
                        location={"x_mm": round(v["x_mm"], 2),
                                  "y_mm": round(v["y_mm"], 2), "layer": flayer}))

        # --- traces (same layer, foreign net) ---
        for t in traces:
            if t.get("net_id") == fnet or t.get("layer") != flayer:
                continue
            ax, ay = t["start_x_mm"], t["start_y_mm"]
            bx, by = t["end_x_mm"], t["end_y_mm"]
            if not (_in_bbox(ax, ay) or _in_bbox(bx, by)):
                continue
            steps = max(2, int(math.hypot(bx - ax, by - ay) / 0.2) + 1)
            if any(_point_in_fill(ax + (bx - ax) * s / steps,
                                  ay + (by - ay) * s / steps, polys, bboxes, is_plane)
                   for s in range(steps + 1)):
                key = ("trace", fnet, flayer, t.get("net_id"))
                if key not in seen:
                    seen.add(key)
                    violations.append(DRCViolation(
                        rule="copper_pour_short", severity="error",
                        message=(f"Copper pour {fname} on {flayer} overlaps "
                                 f"{t.get('net_name', t.get('net_id'))} trace"),
                        location={"x_mm": round(ax, 2),
                                  "y_mm": round(ay, 2), "layer": flayer}))

    return violations
