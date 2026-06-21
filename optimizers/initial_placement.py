"""Deterministic grid placement — a zero-LLM initial placement for the SA optimizer.

Places connectors along the left edge and remaining components in size-sorted
rows (largest first for better packing).  The result is a valid placement dict
that `repair_placement` + `optimize_placement` can refine.

Used by both the step_3 layout fallback and the granular placement stage so
there is a single deterministic seeder.
"""

from __future__ import annotations

import json

import logging
import re

logger = logging.getLogger(__name__)

# Connector fanout orientation (enhancement D): rotate edge connectors so their
# long pad-span runs along the board edge. Kill-switch so it can be A/B-tested
# and disabled if it regresses a board. Only connectors with at least
# ORIENT_MIN_PINS pins are reoriented — small terminal blocks / few-pin headers
# gain nothing and reorienting them regressed routing (see the loop below).
ORIENT_CONNECTORS = True
ORIENT_MIN_PINS = 10


def _connector_rotation(w: float, h: float, pins: int) -> int:
    """Rotation for an edge connector: 90° (long pad-span vertical, along the
    left edge) for a wide, high-pin connector; 0 otherwise. Gated on pin count
    so small terminal blocks / few-pin headers are left alone (reorienting them
    regressed routing)."""
    if ORIENT_CONNECTORS and w > h and pins >= ORIENT_MIN_PINS:
        return 90
    return 0


def generate_grid_placement(
    netlist: dict,
    board_width_mm: float,
    board_height_mm: float,
    project_name: str = "",
    netlist_filename: str = "",
    bom_filename: str = "",
    layers: int = 2,
) -> dict | None:
    """Generate a grid-based deterministic placement.

    Args:
        netlist:        Parsed circuit_schema netlist dict.
        board_width_mm: Board width.
        board_height_mm:Board height.
        project_name:   Optional project slug (written into the result).
        netlist_filename / bom_filename: Optional provenance fields.

    Returns:
        A placement dict (placement_schema), or None if no components/footprints
        could be resolved.
    """
    try:
        from optimizers.pad_geometry import get_footprint_def
    except ImportError:
        return None

    # Collect components + pin counts
    comp_pin_counts: dict[str, int] = {}
    des_to_comp_id: dict[str, str] = {}
    components: list[dict] = []
    for elem in netlist.get("elements", []):
        if elem.get("element_type") == "port":
            cid = elem.get("component_id", "")
            comp_pin_counts[cid] = comp_pin_counts.get(cid, 0) + 1
        elif elem.get("element_type") == "component":
            des_to_comp_id[elem.get("designator", "")] = elem["component_id"]
            components.append(elem)

    if not components:
        return None

    # Footprint dimensions per component
    comp_dims: list[tuple[dict, float, float]] = []
    for comp in components:
        des = comp.get("designator", "")
        pkg = comp.get("package", "")
        cid = des_to_comp_id.get(des, "")
        pin_count = comp_pin_counts.get(cid, 2)

        fp = get_footprint_def(pkg, pin_count)
        if fp:
            pw, ph = fp.pad_size
            xs = [dx for dx, dy in fp.pin_offsets.values()]
            ys = [dy for dx, dy in fp.pin_offsets.values()]
            w = round(max(xs) - min(xs) + pw + 0.5, 1)
            h = round(max(ys) - min(ys) + ph + 0.5, 1)
        else:
            w = h = 3.0
        comp_dims.append((comp, w, h))

    connectors = [(c, w, h) for c, w, h in comp_dims
                  if c.get("component_type") == "connector"]

    def _pins(comp: dict) -> int:
        return comp_pin_counts.get(comp.get("component_id", ""), 0)

    # Mounting holes are mechanical keepouts pinned by the SA optimizer (by
    # package), so wherever the grid seeds them is where they STAY. Seed them at
    # the four board corners — matching how enclosures place standoffs — instead
    # of letting them fall into the size-sorted interior grid (which stacked
    # H1-H4 in a row near one edge). Only the first four go to corners; any
    # extras fall through to the normal interior fill.
    _MOUNTING_HOLE_RE = re.compile(r"MountingHole", re.IGNORECASE)
    mounting_holes = [(c, w, h) for c, w, h in comp_dims
                      if c.get("component_type") != "connector"
                      and _MOUNTING_HOLE_RE.search(c.get("package", ""))]
    corner_holes = mounting_holes[:4]
    corner_hole_des = {c["designator"] for c, _, _ in corner_holes}
    others = [(c, w, h) for c, w, h in comp_dims
              if c.get("component_type") != "connector"
              and c["designator"] not in corner_hole_des]
    # Largest first for better packing
    others.sort(key=lambda x: x[1] * x[2], reverse=True)

    placements: list[dict] = []
    margin = 1.5
    clearance = 1.0

    # Reserve the corners so edge-laid connectors don't collide with a
    # corner-seeded mounting hole (both are pinned → an unfixable overlap).
    corner_reserve = 0.0
    if corner_holes:
        corner_reserve = max(max(w, h) for _, w, h in corner_holes) + clearance

    # High-pin connectors along the left edge are oriented so their pins fan
    # into open board area (enhancement D): a connector whose pin row runs
    # along its width (long axis = x) is rotated 90° to run its pins *vertically
    # along the edge* — every pin escapes a short hop into the interior, instead
    # of the row poking horizontally into the board with deep, hard-to-escape
    # inner pins (the fanout wall on wide connectors like a 30-pin FPC).
    # GATED ON PIN COUNT (≥ ORIENT_MIN_PINS): small terminal blocks / few-pin
    # headers gain nothing from reorientation and rotating them measurably HURT
    # routing on real boards (rs485 0→3 DRC, 4ch 11→26 DRC when applied to all
    # connectors). Only genuinely wide connectors — where the deep-inner-pin
    # problem exists — are reoriented. Connectors are pinned through the SA
    # optimize pass, so the orientation sticks. footprint_width/height stay the
    # UNROTATED body dims; the layout math uses the rotated extent (ew, eh).
    # Distribute connectors around the board PERIMETER so many connectors don't
    # pile onto one edge. The old code stacked them all on the left edge and
    # collapsed any overflow onto a single right-edge point (they overlapped —
    # 8 connectors on morgan landed 3 at the same spot). Now we fill left, then
    # right, then bottom, then top, each with its own running cursor, spilling to
    # the next edge only when the current one is full. Connectors are pinned
    # through SA so the positions stick. Left/right keep the high-pin fanout
    # orientation (_connector_rotation); spill edges run the long pad-span along
    # the edge so pins still fan inward.
    from optimizers.placement_optimizer import _get_pad_extent_box

    edge_order = ["left", "right", "bottom", "top"]
    edge_cap = {"left": board_height_mm, "right": board_height_mm,
                "bottom": board_width_mm, "top": board_width_mm}

    def _conn_geom(comp: dict, w: float, h: float, edge: str):
        """Rotation + rotated pad-extent box (offsets from the placement origin)
        for a connector on *edge*. The box includes PADS, which on terminal
        blocks / FFCs extend asymmetrically past the body — positioning by it
        (not the body centre) is what keeps the pads inside the edge clearance."""
        pins = _pins(comp)
        if edge in ("left", "right"):
            r = _connector_rotation(w, h, pins)
        else:
            r = 0 if w >= h else 90        # long pad-span along the horizontal edge
        # Extent relative to a (0,0) origin so we can solve for the placement.
        bx = _get_pad_extent_box(0.0, 0.0, w, h, r, comp.get("package", ""), pins)
        return r, bx                       # bx = (x_min, y_min, x_max, y_max)

    # Seed mounting holes at the corners (clockwise from top-left). Pads inset
    # by margin from each edge; positions stick because the SA optimizer pins
    # mounting-hole packages.
    for i, (comp, w, h) in enumerate(corner_holes):
        cxs = margin + w / 2
        cys = margin + h / 2
        x = cxs if i in (0, 2) else board_width_mm - cxs
        y = cys if i in (0, 1) else board_height_mm - cys
        placements.append(_place_item(comp, w, h, x, y))

    ei = 0
    # Start each edge past the reserved corner so a connector can't land on a
    # corner hole, and stop short of the far corner (handled via edge_cap below).
    cur = margin + corner_reserve
    # Inward reach of the connectors on each edge, so the interior 'others' can
    # be inset clear of ALL of them (not just the left edge).
    left_reach = margin
    right_reach = board_width_mm - margin
    bottom_reach = margin
    top_reach = board_height_mm - margin
    for comp, w, h in connectors:
        edge = edge_order[ei]
        rot, (xmn, ymn, xmx, ymx) = _conn_geom(comp, w, h, edge)
        span = (ymx - ymn) if edge in ("left", "right") else (xmx - xmn)
        # Spill to the next edge if this one can't fit the connector (the far
        # corner is reserved too, hence the extra corner_reserve).
        while (cur + span > edge_cap[edge] - margin - corner_reserve
               and ei < len(edge_order) - 1):
            ei += 1
            edge = edge_order[ei]
            cur = margin + corner_reserve
            rot, (xmn, ymn, xmx, ymx) = _conn_geom(comp, w, h, edge)
            span = (ymx - ymn) if edge in ("left", "right") else (xmx - xmn)
        # Solve for the origin so the pad-extent box clears the edge margin and
        # stacks at the running cursor.
        if edge == "left":
            x, y = margin - xmn, cur - ymn
            left_reach = max(left_reach, x + xmx)
        elif edge == "right":
            x, y = (board_width_mm - margin) - xmx, cur - ymn
            right_reach = min(right_reach, x + xmn)
        elif edge == "bottom":
            x, y = cur - xmn, margin - ymn
            bottom_reach = max(bottom_reach, y + ymx)
        else:  # top
            x, y = cur - xmn, (board_height_mm - margin) - ymx
            top_reach = min(top_reach, y + ymn)
        item = _place_item(comp, w, h, x, y)
        item["rotation_deg"] = rot
        placements.append(item)
        cur += span + clearance

    # Remaining components fill the interior region clear of ALL edge connectors
    # (inset from each edge by that edge's connector reach), in left→right,
    # bottom→top rows. Keeping 'others' out of the pinned perimeter lets overlap
    # repair converge instead of fighting the fixed connectors — the marginal,
    # seed-dependent placement failures on sparse boards came from 'others' being
    # laid over the right/top/bottom connectors.
    x_lo = left_reach + clearance
    x_hi = right_reach - clearance
    y_lo = bottom_reach + clearance
    y_hi = top_reach - clearance
    row_x = x_lo
    row_y = y_lo
    row_height = 0.0
    for comp, w, h in others:
        if row_x + w > x_hi:
            row_x = x_lo
            row_y += row_height + clearance
            row_height = 0.0
        x = row_x + w / 2
        y = row_y + h / 2
        if y + h / 2 > y_hi:
            y = y_hi - h / 2  # clamp; repair spreads any residual overlap
        placements.append(_place_item(comp, w, h, x, y))
        row_x += w + clearance
        row_height = max(row_height, h)

    return {
        "version": "1.0",
        "project_name": project_name,
        "source_netlist": netlist_filename,
        "source_bom": bom_filename,
        "board": {
            "width_mm": board_width_mm,
            "height_mm": board_height_mm,
            "layers": layers,
            "copper_thickness_oz": 1,
        },
        "placements": placements,
    }


def _place_item(comp: dict, w: float, h: float, x: float, y: float) -> dict:
    return {
        "designator": comp["designator"],
        "component_type": comp["component_type"],
        "package": comp.get("package", ""),
        "footprint_width_mm": w,
        "footprint_height_mm": h,
        "x_mm": round(x, 2),
        "y_mm": round(y, 2),
        "rotation_deg": 0,
        "layer": "top",
        # Movable by the SA optimizer (only placement_source=="user" is pinned).
        "placement_source": "auto",
    }


def generate_grid_placement_json(
    netlist_content: str,
    board_width_mm: float,
    board_height_mm: float,
    project_name: str = "",
    netlist_filename: str = "",
    bom_filename: str = "",
) -> str | None:
    """String-in/string-out wrapper for callers that work with JSON text."""
    result = generate_grid_placement(
        json.loads(netlist_content), board_width_mm, board_height_mm,
        project_name, netlist_filename, bom_filename,
    )
    return json.dumps(result, indent=2) if result is not None else None
