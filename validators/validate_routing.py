"""Routing validator — deterministic checks for routed PCB traces and vias.

Standalone CLI: python validate_routing.py <routed.json> [--netlist <netlist.json>]
Also importable: from validate_routing import validate_routing
"""

import argparse
import json
import math
import re
import sys
from pathlib import Path

import jsonschema

from optimizers.routed_board import routing_stats

import logging

logger = logging.getLogger(__name__)

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
    except FileNotFoundError:  # pragma: no cover - schema is shipped with the package
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

def _copper_stack(num_layers: int) -> list[str]:
    """Copper layers in physical order, top → inner1 → … → bottom."""
    if num_layers <= 2:
        return ["top", "bottom"]
    return ["top"] + [f"inner{i}" for i in range(1, num_layers - 1)] + ["bottom"]


def _via_layers(via: dict) -> tuple[str, str]:
    """A via's (from_layer, to_layer), defaulting a MISSING or None field.

    dict.get(key, default) only substitutes the default when the key is absent —
    a key present with a None value returns None. Via producers that omitted the
    layer pair (the power-plane stitcher) ended up serialised as
    "from_layer": None, so every layer test here silently failed: _reaches_plane
    saw {None, None}, refused to credit the via with touching the plane, and the
    net's pads were reported as disconnected groups even though a solid inner
    plane joined them. Stitching vias are through vias, so top/bottom are the
    right fallbacks.
    """
    return (via.get("from_layer") or "top", via.get("to_layer") or "bottom")


def _via_spanned_layers(from_layer: str, to_layer: str,
                        stack: list[str]) -> list[str]:
    """Every copper layer a via crosses, inclusive. A through-via (top↔bottom)
    spans all inner layers too, so traces on those layers connect to it."""
    try:
        i, j = stack.index(from_layer), stack.index(to_layer)
    except ValueError:
        return [from_layer, to_layer]
    lo, hi = min(i, j), max(i, j)
    return stack[lo:hi + 1]


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

    # Group traces by layer (N-layer aware)
    by_layer: dict[str, list[dict]] = {}
    for t in traces:
        layer = t.get("layer", "top")
        by_layer.setdefault(layer, []).append(t)

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
            via_layers = set(_via_layers(via))
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

def incomplete_net_ids(routed: dict, netlist: dict | None) -> set[str]:
    """Net IDs that are NOT fully connected: either fully unrouted, or routed
    but split into more than one connected group.

    Used by incremental routing to decide which nets to re-route — protecting a
    disconnected net's partial wiring would otherwise leave it disconnected
    forever (Freerouting treats protected wiring as done).
    """
    ids: set[str] = set(routed.get("routing", {}).get("unrouted_nets", []))
    if netlist is None:
        return ids
    errors, _ = _check_connectivity(routed, netlist)
    for e in errors:
        m = re.match(r"Net (\S+):", e)
        if m:
            ids.add(m.group(1))
    return ids


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

    # Copper stack order (top → inner1 → … → bottom), used to expand a via to
    # every layer it spans. Prefer the board's layer count; fall back to the
    # highest innerN actually present in the routing.
    num_layers = routed.get("board", {}).get("layers")
    if not num_layers:
        seen = ({t.get("layer") for t in traces}
                | {_via_layers(v)[0] for v in vias}
                | {_via_layers(v)[1] for v in vias})
        inner_idx = [int(s[5:]) for s in seen
                     if isinstance(s, str) and s.startswith("inner") and s[5:].isdigit()]
        num_layers = (max(inner_idx) + 2) if inner_idx else 2
    stack = _copper_stack(int(num_layers))

    # Identify nets connected by copper fill (all pads on fill layer(s) are connected)
    fill_nets: dict[str, set[str]] = {}  # net_id -> set of layers with fill
    inner_plane_nets: dict[str, set[str]] = {}  # net_id -> set of inner-plane layers
    for fill_region in routing.get("copper_fills", []):
        fnet = fill_region.get("net_id", "")
        flayer = fill_region.get("layer", "")
        if fnet and flayer:
            fill_nets.setdefault(fnet, set()).add(flayer)
            if fill_region.get("is_plane"):
                inner_plane_nets.setdefault(fnet, set()).add(flayer)

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
    except Exception:  # pragma: no cover - build_pad_map is defensive; this guards a corrupt pad map
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

        net_traces = [t for t in traces if t.get("net_id") == net_id]
        net_vias = [v for v in vias if v.get("net_id") == net_id]

        # Each trace segment: raw endpoints + layer + a canonical union node
        # (its rounded start, already unioned to its end). Connectivity is
        # SEGMENT-aware, not endpoint-only: a pad / via / other trace endpoint
        # that lands anywhere ALONG a segment (a T-junction, a mid-trace via
        # drop, a pad under the trace) is a real electrical connection. Matching
        # only coincident endpoints (the old behaviour) split genuinely-routed
        # multi-pad nets into false "disconnected groups".
        segs: list[tuple[float, float, float, float, str, tuple]] = []
        for t in net_traces:
            L = t.get("layer", "top")
            p1 = (round(t["start_x_mm"], 2), round(t["start_y_mm"], 2), L)
            p2 = (round(t["end_x_mm"], 2), round(t["end_y_mm"], 2), L)
            union(p1, p2)
            segs.append((t["start_x_mm"], t["start_y_mm"],
                         t["end_x_mm"], t["end_y_mm"], L, p1))

        def _layer_ok(layer_a: str, layer_b: str) -> bool:
            return layer_a == layer_b or layer_a == "all" or layer_b == "all"

        def _connect_to_segs(pt: tuple) -> None:
            """Union a point to every same-net trace segment it lies on/near
            (layer-matched). The point joins each such segment's component."""
            px, py, pl = pt
            for ax, ay, bx, by, sl, anchor in segs:
                if not _layer_ok(pl, sl):
                    continue
                if _point_to_segment_distance(px, py, ax, ay, bx, by) < snap:
                    union(pt, anchor)

        # Trace-trace junctions: an endpoint of one segment lying on another.
        for ax, ay, bx, by, sl, _anchor in segs:
            _connect_to_segs((round(ax, 2), round(ay, 2), sl))
            _connect_to_segs((round(bx, 2), round(by, 2), sl))

        # Vias span EVERY copper layer between from_layer and to_layer, not just
        # the two endpoints — a through-via (top↔bottom) physically passes
        # through inner1/inner2, so an inner-layer trace landing on it is
        # connected. (Missing this falsely splits nets routed on inner signal
        # layers, e.g. plane_layers=0 boards.)
        via_points: list[tuple[float, float, str]] = []
        for v in net_vias:
            vx, vy = v["x_mm"], v["y_mm"]
            spanned = _via_spanned_layers(
                *_via_layers(v), stack)
            hub = None
            for L in spanned:
                p = (round(vx, 2), round(vy, 2), L)
                if hub is None:
                    hub = p
                union(hub, p)
                _connect_to_segs(p)
                via_points.append(p)

        # Pads attach to any same-net trace segment they lie on (layer-matched,
        # "all" = through-hole matches any copper layer) and to via endpoints.
        for pad_pos in pads:
            _connect_to_segs(pad_pos)
            for vp in via_points:
                if (_layer_ok(pad_pos[2], vp[2])
                        and math.hypot(vp[0] - pad_pos[0], vp[1] - pad_pos[1]) < snap):
                    union(pad_pos, vp)

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

        # Inner-plane connectivity: a solid inner plane (is_plane fill on
        # inner1/inner2) is one continuous copper pour, so every same-net
        # feature that reaches it is mutually connected. A through via (or any
        # via spanning to the plane layer) lands on the plane; union all such
        # vias together, then union each to the pad it serves (via-in-pad or
        # the short stub trace places it on/at the pad).
        plane_layers = inner_plane_nets.get(net_id, set())
        if plane_layers:
            outer = {"top", "bottom"}

            def _reaches_plane(v: dict) -> bool:
                fl, tl = _via_layers(v)
                # A through via (top↔bottom) crosses every inner layer.
                if {fl, tl} == outer:
                    return True
                return fl in plane_layers or tl in plane_layers

            # The plane is one continuous pour; collect every same-net feature
            # that touches it into a single group: through-hole pads (penetrate
            # all layers) and vias spanning to the plane, plus the surface pads
            # those vias serve.
            anchor = None

            def _join(point):
                nonlocal anchor
                if anchor is None:
                    anchor = point
                union(anchor, point)

            for pad_pos in pads:
                if pad_pos[2] == "all":  # through-hole — penetrates the plane
                    _join(pad_pos)
            for v in net_vias:
                if not _reaches_plane(v):
                    continue
                vp = (round(v["x_mm"], 2), round(v["y_mm"], 2),
                      _via_layers(v)[0])
                _join(vp)
                for pad_pos in pads:
                    if math.hypot(vp[0] - pad_pos[0], vp[1] - pad_pos[1]) < snap:
                        _join(pad_pos)

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
# 5b. Fill shorts — foreign copper painted over by copper fills
# ---------------------------------------------------------------------------

def _poly_bbox(poly: list) -> tuple[float, float, float, float]:
    xs = [p[0] for p in poly]
    ys = [p[1] for p in poly]
    return (min(xs), min(ys), max(xs), max(ys))


def _point_in_poly(px: float, py: float, poly: list) -> bool:
    """Ray-casting point-in-polygon test."""
    inside = False
    j = len(poly) - 1
    for i in range(len(poly)):
        xi, yi = poly[i]
        xj, yj = poly[j]
        if (yi > py) != (yj > py) and \
                px < (xj - xi) * (py - yi) / (yj - yi) + xi:
            inside = not inside
        j = i
    return inside


def _segments_cross(a1x: float, a1y: float, a2x: float, a2y: float,
                    b1x: float, b1y: float, b2x: float, b2y: float) -> bool:
    """True if the two segments share a point (proper crossing or touching).

    _segment_to_segment_distance cannot detect proper crossings (its four
    point-to-segment combos stay > 0 when segments cross mid-air), so the
    fill tests use this exact intersection predicate first.
    """
    denom = (a2x - a1x) * (b2y - b1y) - (a2y - a1y) * (b2x - b1x)
    if abs(denom) < 1e-12:
        return False
    t = ((b1x - a1x) * (b2y - b1y) - (b1y - a1y) * (b2x - b1x)) / denom
    u = ((b1x - a1x) * (a2y - a1y) - (b1y - a1y) * (a2x - a1x)) / denom
    return 0.0 <= t <= 1.0 and 0.0 <= u <= 1.0


def _poly_edge_distance(ax: float, ay: float, bx: float, by: float,
                        poly: list) -> float:
    """Distance from segment to polygon BOUNDARY; 0 when touching/crossing.

    _segment_to_segment_distance cannot detect proper crossings (its four
    point-to-segment combos stay > 0 when segments cross mid-air), so the
    crossing test runs first.
    """
    n = len(poly)
    d = math.inf
    for i in range(n):
        x1, y1 = poly[i]
        x2, y2 = poly[(i + 1) % n]
        if _segments_cross(ax, ay, bx, by, x1, y1, x2, y2):
            return 0.0
        d = min(
            d,
            _point_to_segment_distance(ax, ay, x1, y1, x2, y2),
            _point_to_segment_distance(bx, by, x1, y1, x2, y2),
            _point_to_segment_distance(x1, y1, ax, ay, bx, by),
            _point_to_segment_distance(x2, y2, ax, ay, bx, by),
        )
    return d


def _segment_to_poly_gap(ax: float, ay: float, bx: float, by: float,
                         poly: list, is_copper: bool) -> float:
    """Distance from segment (a point when a == b) to a polygon.

    Returns 0.0 when the segment touches or crosses the boundary, and 0.0
    when it lies inside a COPPER polygon. Inside a hole polygon (void) it
    returns the distance to the void's edges — never more than the true
    distance to the surrounding copper, conservative by design.
    """
    if is_copper and (_point_in_poly(ax, ay, poly)
                      or _point_in_poly(bx, by, poly)):
        return 0.0
    return _poly_edge_distance(ax, ay, bx, by, poly)


def _build_fill_regions(routed: dict) -> dict[str, list[dict]]:
    """Group copper_fills into per-layer regions of (copper, hole) polygons.

    Polygon semantics follow how gerber_exporter PAINTS the board: every
    polygon of a pour (no is_plane flag) is positive copper; a plane
    (is_plane: true) paints polygons[0] minus polygons[1:] (hole cut-outs),
    so being inside the outer polygon does NOT mean being inside copper.
    The validator must judge the geometry exactly as it ships, and KiCad
    import zones emit unmarked polygons too.
    """
    by_layer: dict[str, list[dict]] = {}
    for fill in routed.get("routing", {}).get("copper_fills", []):
        layer = fill.get("layer")
        net = fill.get("net_id")
        polys = [p for p in fill.get("polygons", [])
                 if isinstance(p, (list, tuple)) and len(p) >= 3]
        if not layer or not net or not polys:
            continue
        plane = bool(fill.get("is_plane"))
        if plane:
            copper, holes = [polys[0]], polys[1:]
        else:
            copper, holes = polys, []
        by_layer.setdefault(layer, []).append({
            "net": net,
            "name": fill.get("net_name") or net,
            "plane": plane,
            "outer": polys[0] if plane else None,
            "polys": [(_poly_bbox(p), p, False) for p in copper]
                     + [(_poly_bbox(p), p, True) for p in holes],
        })
    return by_layer


def _distance_to_fill(px0: float, py0: float, px1: float, py1: float,
                      region: dict, cutoff: float) -> float:
    """Min distance from a segment (point when p0 == p1) to one fill region.

    Polygon bboxes expanded by `cutoff` are skipped, which is exact: only
    distances under the cutoff could affect a clearance verdict.
    """
    gx0 = min(px0, px1) - cutoff
    gx1 = max(px0, px1) + cutoff
    gy0 = min(py0, py1) - cutoff
    gy1 = max(py0, py1) + cutoff
    cands = [(poly, is_hole)
             for bbox, poly, is_hole in region["polys"]
             if not (gx1 < bbox[0] or gx0 > bbox[2]
                     or gy1 < bbox[1] or gy0 > bbox[3])]
    if not cands:
        return math.inf

    if region["plane"]:
        outer = region["outer"]
        holes = [poly for poly, is_hole in cands if is_hole]
        # Copper = outer minus holes: an endpoint on the copper band is a
        # short; one inside a hole void is measured against the void edges.
        for x, y in ((px0, py0), (px1, py1)):
            if (_point_in_poly(x, y, outer)
                    and not any(_point_in_poly(x, y, h) for h in holes)):
                return 0.0
        d = math.inf
        for poly, _is_hole in cands:
            dist = _poly_edge_distance(px0, py0, px1, py1, poly)
            if dist < d:
                d = dist
                if d <= 0.0:
                    return 0.0
        return d

    d = math.inf
    for poly, is_hole in cands:
        dist = _segment_to_poly_gap(px0, py0, px1, py1, poly,
                                    is_copper=not is_hole)
        if dist < d:
            d = dist
            if d <= 0.0:
                return 0.0
    return d


def _check_fill_shorts(routed: dict, netlist: dict | None) -> tuple[list[str], list[str]]:
    """Check traces, vias and pads against copper fills of OTHER nets.

    A pour painted directly on top of foreign-net copper is a real short: the
    audit LM2596 board shipped a bottom GND pour covering 28 foreign trace
    endpoints and validated clean because only trace-trace overlap was ever
    checked against fills. Same-net features are connections (the pour exists
    to carry that net), so they are skipped — same rule as _check_no_shorts.
    """
    errors: list[str] = []
    warnings: list[str] = []

    regions_by_layer = _build_fill_regions(routed)
    if not regions_by_layer:
        return errors, warnings

    routing = routed.get("routing", {})
    traces = routing.get("traces", [])
    vias = routing.get("vias", [])
    clearance = routing.get("config", {}).get("trace_clearance_mm", 0.2)

    # --- traces vs fills (same layer only) ---
    for t in traces:
        layer = t.get("layer", "top")
        regions = regions_by_layer.get(layer)
        if not regions:
            continue
        t_net = t.get("net_id")
        t_name = t.get("net_name", t_net)
        half = t.get("width_mm", 0.25) / 2
        ax, ay = t["start_x_mm"], t["start_y_mm"]
        bx, by = t["end_x_mm"], t["end_y_mm"]
        mx, my = (ax + bx) / 2, (ay + by) / 2

        for region in regions:
            if region["net"] == t_net:
                continue
            d = _distance_to_fill(ax, ay, bx, by, region, half + clearance)
            if d == math.inf:
                continue
            gap = d - half
            if gap < -0.01:
                errors.append(
                    f"Fill short on {layer}: trace({t_name}) overlaps "
                    f"{region['name']} fill at ({mx:.2f},{my:.2f}) "
                    f"(copper overlap {-gap:.3f}mm)"
                )
            elif gap < clearance - 0.05:
                warnings.append(
                    f"Fill clearance on {layer}: trace({t_name}) is {gap:.3f}mm "
                    f"from {region['name']} fill at ({mx:.2f},{my:.2f}) "
                    f"(min {clearance}mm)"
                )

    # --- vias vs fills (on every layer the via lands on) ---
    for via in vias:
        v_net = via.get("net_id")
        v_radius = via.get("diameter_mm", 0.6) / 2
        vx, vy = via["x_mm"], via["y_mm"]
        v_name = via.get("net_name", v_net)

        for layer in set(_via_layers(via)):
            regions = regions_by_layer.get(layer)
            if not regions:
                continue
            for region in regions:
                if region["net"] == v_net:
                    continue
                d = _distance_to_fill(vx, vy, vx, vy, region,
                                      v_radius + clearance)
                if d == math.inf:
                    continue
                gap = d - v_radius
                if gap < -0.01:
                    errors.append(
                        f"Fill short on {layer}: via({v_name}) overlaps "
                        f"{region['name']} fill at ({vx:.2f},{vy:.2f}) "
                        f"(copper overlap {-gap:.3f}mm)"
                    )
                elif gap < clearance - 0.05:
                    warnings.append(
                        f"Fill clearance on {layer}: via({v_name}) is "
                        f"{gap:.3f}mm from {region['name']} fill at "
                        f"({vx:.2f},{vy:.2f}) (min {clearance}mm)"
                    )

    # --- pads vs fills ---
    if netlist is None:
        return errors, warnings
    try:
        import sys as _sys
        _sys.path.insert(0, str(Path(__file__).parent.parent))
        from optimizers.pad_geometry import build_pad_map
        pad_map = build_pad_map(routed, netlist)
    except Exception:  # pragma: no cover - build_pad_map is defensive; this guards a corrupt pad map
        warnings.append("Fill short check: could not build pad map")
        return errors, warnings

    for pad in pad_map.values():
        if pad.net_id is None:
            continue
        is_th = pad.layer == "all"
        if is_th:
            radius = max(pad.pad_width_mm, pad.pad_height_mm) / 2
            # Through-hole copper penetrates both outer layers
            layers = ["top", "bottom"]
            geom = [(pad.x_mm, pad.y_mm, pad.x_mm, pad.y_mm)]
            reach = radius
        else:
            hw, hh = pad.pad_width_mm / 2, pad.pad_height_mm / 2
            x0, y0 = pad.x_mm - hw, pad.y_mm - hh
            x1, y1 = pad.x_mm + hw, pad.y_mm + hh
            layers = [pad.layer]
            # Perimeter edges + centre: a pour island swallowed whole by the
            # pad copper must count as a short even though no edge crosses it.
            geom = [(x0, y0, x1, y0), (x1, y0, x1, y1),
                    (x1, y1, x0, y1), (x0, y1, x0, y0),
                    (pad.x_mm, pad.y_mm, pad.x_mm, pad.y_mm)]
            reach = 0.0

        for layer in layers:
            regions = regions_by_layer.get(layer)
            if not regions:
                continue
            for region in regions:
                if region["net"] == pad.net_id:
                    continue
                d = math.inf
                for gx0, gy0, gx1, gy1 in geom:
                    d = min(d, _distance_to_fill(gx0, gy0, gx1, gy1, region,
                                                 reach + clearance))
                    if d <= 0.0:
                        break
                if d == math.inf:
                    continue
                gap = d - reach
                if gap < -0.01:
                    errors.append(
                        f"Fill short on {layer}: "
                        f"pad({pad.designator}.{pad.pin_number} "
                        f"net={pad.net_id}) overlaps {region['name']} fill at "
                        f"({pad.x_mm:.2f},{pad.y_mm:.2f}) "
                        f"(copper overlap {-gap:.3f}mm)"
                    )
                elif gap < clearance - 0.05:
                    warnings.append(
                        f"Fill clearance on {layer}: "
                        f"pad({pad.designator}.{pad.pin_number} "
                        f"net={pad.net_id}) is {gap:.3f}mm from "
                        f"{region['name']} fill at "
                        f"({pad.x_mm:.2f},{pad.y_mm:.2f}) (min {clearance}mm)"
                    )

    return errors, warnings


# ---------------------------------------------------------------------------
# 6. Trace-to-pad and via-to-pad clearance
# ---------------------------------------------------------------------------

def _point_to_rect_distance(px: float, py: float, cx: float, cy: float,
                            hw: float, hh: float) -> float:
    """Distance from a point to an axis-aligned rectangle (0 if inside)."""
    dx = max(abs(px - cx) - hw, 0.0)
    dy = max(abs(py - cy) - hh, 0.0)
    return math.hypot(dx, dy)


def _segment_to_rect_distance(ax: float, ay: float, bx: float, by: float,
                              cx: float, cy: float, hw: float, hh: float) -> float:
    """Minimum distance from segment (a→b) to an axis-aligned rectangle
    centred at (cx, cy) with half-extents (hw, hh). 0 if they intersect.

    Pads are true rectangles (build_pad_map swaps w/h for rotated parts), so
    this replaces the old max-extent circular approximation that falsely
    flagged traces legally passing the SHORT side of an elongated pad
    (e.g. a 1.5x0.6mm SOIC pad treated as a 0.75mm-radius circle).
    """
    # Endpoint inside the rectangle → intersecting
    if (abs(ax - cx) <= hw and abs(ay - cy) <= hh) or \
       (abs(bx - cx) <= hw and abs(by - cy) <= hh):
        return 0.0

    def seg_seg(p1x, p1y, p2x, p2y, p3x, p3y, p4x, p4y) -> float:
        denom = (p2x - p1x) * (p4y - p3y) - (p2y - p1y) * (p4x - p3x)
        if abs(denom) > 1e-12:  # not parallel — check for a true crossing
            t = ((p3x - p1x) * (p4y - p3y) - (p3y - p1y) * (p4x - p3x)) / denom
            u = ((p3x - p1x) * (p2y - p1y) - (p3y - p1y) * (p2x - p1x)) / denom
            if 0.0 <= t <= 1.0 and 0.0 <= u <= 1.0:
                return 0.0
        return min(
            _point_to_segment_distance(p1x, p1y, p3x, p3y, p4x, p4y),
            _point_to_segment_distance(p2x, p2y, p3x, p3y, p4x, p4y),
            _point_to_segment_distance(p3x, p3y, p1x, p1y, p2x, p2y),
            _point_to_segment_distance(p4x, p4y, p1x, p1y, p2x, p2y),
        )

    x0, y0, x1, y1 = cx - hw, cy - hh, cx + hw, cy + hh
    edges = [(x0, y0, x1, y0), (x1, y0, x1, y1),
             (x1, y1, x0, y1), (x0, y1, x0, y0)]
    return min(seg_seg(ax, ay, bx, by, *e) for e in edges)


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
    except Exception:  # pragma: no cover - build_pad_map is defensive; this guards a corrupt pad map
        warnings.append("Pad clearance check: could not build pad map")
        return errors, warnings

    routing = routed.get("routing", {})
    traces = routing.get("traces", [])
    vias = routing.get("vias", [])
    clearance = routing.get("config", {}).get("trace_clearance_mm", 0.2)

    # Build pad list with per-layer extents. Through-hole pads are CIRCLES
    # of diameter max(w,h) on every layer (matching both the DSN export that
    # Freerouting routes against and the KiCad export); SMD pads are true
    # rectangles on their own layer.
    pads_by_layer: dict[str, list] = {"top": [], "bottom": []}
    for pad in pad_map.values():
        if pad.net_id is None:
            continue
        is_th = pad.layer == "all"
        for layer in (["top", "bottom"] if is_th else [pad.layer]):
            if is_th:
                pad_hw = max(pad.pad_width_mm, pad.pad_height_mm) / 2
                pad_hh = pad_hw
            else:
                pad_hw = pad.pad_width_mm / 2
                pad_hh = pad.pad_height_mm / 2
            pads_by_layer.setdefault(layer, []).append(
                (pad, pad_hw, pad_hh, is_th)
            )

    # Trace-to-pad: check if trace copper overlaps pad copper (rectangular check)
    seen_tp = set()
    for trace in traces:
        t_net = trace.get("net_id")
        t_layer = trace.get("layer", "top")
        t_half = trace.get("width_mm", 0.25) / 2
        ax, ay = trace["start_x_mm"], trace["start_y_mm"]
        bx, by = trace["end_x_mm"], trace["end_y_mm"]

        for pad, pad_hw, pad_hh, is_circle in pads_by_layer.get(t_layer, []):
            if pad.net_id == t_net:
                continue

            # Cheap circular reject before the exact test
            centre_dist = _point_to_segment_distance(pad.x_mm, pad.y_mm,
                                                     ax, ay, bx, by)
            if centre_dist > t_half + max(pad_hw, pad_hh) + 0.5:
                continue

            if is_circle:
                pad_dist = centre_dist - pad_hw  # circle of radius pad_hw
            else:
                pad_dist = _segment_to_rect_distance(ax, ay, bx, by,
                                                     pad.x_mm, pad.y_mm,
                                                     pad_hw, pad_hh)
            gap = pad_dist - t_half  # copper-to-copper gap
            if gap < -0.01:
                key = (t_net, pad.designator, pad.pin_number, t_layer)
                if key in seen_tp:
                    continue
                seen_tp.add(key)
                errors.append(
                    f"Trace-pad short on {t_layer}: "
                    f"trace({trace.get('net_name', t_net)}) overlaps "
                    f"pad({pad.designator}.{pad.pin_number} net={pad.net_id}) "
                    f"by {-gap:.3f}mm"
                )
            elif gap < clearance - 0.05:
                key = ("warn", t_net, pad.designator, pad.pin_number, t_layer)
                if key in seen_tp:
                    continue
                seen_tp.add(key)
                warnings.append(
                    f"Trace-pad clearance on {t_layer}: "
                    f"trace({trace.get('net_name', t_net)}) is {gap:.3f}mm from "
                    f"pad({pad.designator}.{pad.pin_number} net={pad.net_id}) "
                    f"(min {clearance}mm)"
                )

    # Via-to-pad
    for via in vias:
        vx, vy = via["x_mm"], via["y_mm"]
        v_radius = via.get("diameter_mm", 0.6) / 2
        v_net = via.get("net_id")
        via_layers = set(_via_layers(via))

        for layer in via_layers:
            for pad, pad_hw, pad_hh, is_circle in pads_by_layer.get(layer, []):
                if pad.net_id == v_net:
                    continue
                if is_circle:
                    pad_dist = math.hypot(vx - pad.x_mm, vy - pad.y_mm) - pad_hw
                else:
                    pad_dist = _point_to_rect_distance(vx, vy,
                                                       pad.x_mm, pad.y_mm,
                                                       pad_hw, pad_hh)
                gap = pad_dist - v_radius
                if gap < -0.01:
                    errors.append(
                        f"Via-pad short on {layer}: "
                        f"via({via.get('net_name', v_net)}) overlaps "
                        f"pad({pad.designator}.{pad.pin_number} net={pad.net_id}) "
                        f"by {-gap:.3f}mm"
                    )
                elif gap < clearance - 0.05:
                    warnings.append(
                        f"Via-pad clearance on {layer}: "
                        f"via({via.get('net_name', v_net)}) is {gap:.3f}mm from "
                        f"pad({pad.designator}.{pad.pin_number} net={pad.net_id}) "
                        f"(min {clearance}mm)"
                    )

    return errors, warnings


# ---------------------------------------------------------------------------
# 7. Pad-row escape — routing threaded through a 2-row TH package's pad gap
# ---------------------------------------------------------------------------

# Minimum inner gap (mm) between the two pad rows for a footprint to be treated
# as a two-row through-hole package whose channel a trace could thread. Below
# this there is no room for copper between the rows without touching pad
# copper, which pad-clearance already catches. 1.0mm keeps narrow-pitch pin
# headers (2.54mm pitch, ~0.84mm inter-row gap) out of the rule while catching
# Multiwatt/DIP-class bodies (L298N Multiwatt-15: 1.2mm, audit finding F2).
_PAD_ROW_MIN_CHANNEL_MM = 1.0

# A trace endpoint this close (mm) to a pad centre of the package counts as
# connected TO the package: a pin-to-pin link through the channel is a
# connection, not an escape. Anything whose ends both float free and still
# crosses the channel is a dangling stub or a wrong-side escape.
_PAD_ROW_CONNECT_R_MM = 1.1


def _find_pad_row_channels(pad_map: dict) -> list[dict]:
    """Detect two-row through-hole footprints and their inter-row channel.

    A footprint qualifies when its through-hole pads (layer == "all") split at
    a clear gap into two parallel pad lines — at least two pads per line, each
    line within 1.0mm of straight (spread measured along the split axis), and
    an inner channel at least ``_PAD_ROW_MIN_CHANNEL_MM`` wide. Tries
    y-stacking then x-stacking so a 90 degree-rotated part is found too.
    Returns per package:

        {"ref": designator,
         "rect": (x0, y0, x1, y1),   # channel strip between the two pad-row
                                      # centrelines, spanning the pad field
         "pads": [(x, y), ...]}      # pad centres, for the endpoint test
    """
    by_ref: dict[str, list] = {}
    for pad in pad_map.values():
        if pad.layer == "all":
            by_ref.setdefault(pad.designator, []).append(pad)

    channels: list[dict] = []
    for ref, pads in sorted(by_ref.items()):
        if len(pads) < 4:
            continue
        for axis in (1, 0):  # split axis: y first, then x (rotated parts)
            order = sorted(pads, key=lambda p: (p.x_mm, p.y_mm)[axis])
            best_gap, split = 0.0, -1
            for i in range(len(order) - 1):
                gap = ((order[i + 1].x_mm, order[i + 1].y_mm)[axis]
                       - (order[i].x_mm, order[i].y_mm)[axis])
                if gap > best_gap:
                    best_gap, split = gap, i
            if split < 1 or split >= len(order) - 1:
                continue
            low, high = order[:split + 1], order[split + 1:]

            def _spread(group: list) -> float:
                vals = [(p.x_mm, p.y_mm)[axis] for p in group]
                return max(vals) - min(vals)

            # Each side must be a LINE of pads, not a scattered blob.
            if _spread(low) > 1.0 or _spread(high) > 1.0:
                continue

            lo_edge = max((p.x_mm, p.y_mm)[axis] for p in low)
            hi_edge = min((p.x_mm, p.y_mm)[axis] for p in high)
            # Inner channel width: centre-line gap minus each row's pad
            # half-extent along the split axis.
            half = (max(max(p.pad_width_mm, p.pad_height_mm) for p in low)
                    + max(max(p.pad_width_mm, p.pad_height_mm) for p in high)) / 2
            if (hi_edge - lo_edge) - half < _PAD_ROW_MIN_CHANNEL_MM:
                continue

            lo_c = max((p.x_mm, p.y_mm)[axis] for p in low)
            hi_c = min((p.x_mm, p.y_mm)[axis] for p in high)
            # The channel exists only where the two rows face each other: the
            # OVERLAP of their run-direction extents. Past a row end a trace is
            # leaving the package (a legal escape), not threading it.
            r_lo = max(min((p.x_mm, p.y_mm)[1 - axis] for p in low),
                       min((p.x_mm, p.y_mm)[1 - axis] for p in high))
            r_hi = min(max((p.x_mm, p.y_mm)[1 - axis] for p in low),
                       max((p.x_mm, p.y_mm)[1 - axis] for p in high))
            if r_hi <= r_lo:
                continue
            if axis == 1:  # rows run along x, channel between them in y
                rect = (r_lo, lo_c, r_hi, hi_c)
            else:          # rows run along y, channel between them in x
                rect = (lo_c, r_lo, hi_c, r_hi)
            channels.append({
                "ref": ref,
                "rect": rect,
                "pads": [(p.x_mm, p.y_mm) for p in pads],
            })
            break  # package resolved on this axis; no second channel
    return channels


def _check_pad_row_escape(routed: dict, netlist: dict | None) -> tuple[list[str], list[str]]:
    """Flag traces that cross a two-row TH package's pad-row channel without
    connecting to it.

    The channel between the pad rows is occupied by the package body and the
    pad drill/solder zone: a trace threaded through it is either a dangling
    stub or an escape that went through the footprint instead of around its
    perimeter — physically impossible boards (audit F2 routed IN2 straight
    through the L298N rows, clipping three pads; KiCad DRC saw pad_clearance
    + six dangling tracks, the internal layer saw nothing structural). Traces
    with an endpoint on a pad of the package are connections, not escapes, and
    are skipped — same rule as the other copper checks. Inner copper layers
    are NOT checked: under-package routing on an internal layer is legal.
    """
    errors: list[str] = []
    warnings: list[str] = []

    if netlist is None:
        return errors, warnings
    try:
        import sys as _sys
        _sys.path.insert(0, str(Path(__file__).parent.parent))
        from optimizers.pad_geometry import build_pad_map
        pad_map = build_pad_map(routed, netlist)
    except Exception:  # pragma: no cover - build_pad_map is defensive; this guards a corrupt pad map
        warnings.append("Pad-row escape check: could not build pad map")
        return errors, warnings

    channels = _find_pad_row_channels(pad_map)
    if not channels:
        return errors, warnings

    routing = routed.get("routing", {})
    traces = routing.get("traces", [])
    seen: set[tuple] = set()

    for ch in channels:
        x0, y0, x1, y1 = ch["rect"]
        cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
        hw, hh = (x1 - x0) / 2, (y1 - y0) / 2
        for trace in traces:
            layer = trace.get("layer", "top")
            if layer not in ("top", "bottom"):
                continue
            ax, ay = trace["start_x_mm"], trace["start_y_mm"]
            bx, by = trace["end_x_mm"], trace["end_y_mm"]
            if _segment_to_rect_distance(ax, ay, bx, by, cx, cy, hw, hh) > 0.0:
                continue

            def _free(px: float, py: float) -> bool:
                return all(math.hypot(px - qx, py - qy) > _PAD_ROW_CONNECT_R_MM
                           for qx, qy in ch["pads"])

            if not (_free(ax, ay) and _free(bx, by)):
                continue

            key = (trace.get("net_id"), ch["ref"], layer)
            if key in seen:
                continue
            seen.add(key)
            mx, my = (ax + bx) / 2, (ay + by) / 2
            errors.append(
                f"Pad-row escape on {layer}: "
                f"trace({trace.get('net_name', trace.get('net_id'))}) crosses "
                f"the pad-row gap of {ch['ref']} at ({mx:.2f},{my:.2f}) — "
                f"escapes must go around the package perimeter, not through "
                f"its pad rows"
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

    # 5b. Fill shorts (pours/planes painted over foreign copper)
    errs, warns = _check_fill_shorts(routed, netlist)
    all_errors.extend(errs)
    all_warnings.extend(warns)

    # 6. Trace-pad and via-pad clearance
    errs, warns = _check_pad_clearance(routed, netlist)
    all_errors.extend(errs)
    all_warnings.extend(warns)

    # 7. Pad-row escape (2-row TH packages: no threading through the pad rows)
    errs, warns = _check_pad_row_escape(routed, netlist)
    all_errors.extend(errs)
    all_warnings.extend(warns)

    # Summary
    stats = routing_stats(routed)
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
