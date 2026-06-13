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

logger = logging.getLogger(__name__)


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
    others = [(c, w, h) for c, w, h in comp_dims
              if c.get("component_type") != "connector"]
    # Largest first for better packing
    others.sort(key=lambda x: x[1] * x[2], reverse=True)

    placements: list[dict] = []
    margin = 1.5
    clearance = 1.0

    # Connectors along the left edge
    cy = margin
    for comp, w, h in connectors:
        x = margin + w / 2
        y = cy + h / 2
        if y + h / 2 > board_height_mm - margin:
            x = board_width_mm - margin - w / 2
            y = margin + h / 2
        placements.append(_place_item(comp, w, h, x, y))
        cy += h + clearance

    # Remaining components in rows (left→right, bottom→top)
    connector_col = margin + max((w for _, w, _ in connectors), default=0) + clearance * 2
    row_x = connector_col
    row_y = margin
    row_height = 0.0
    for comp, w, h in others:
        if row_x + w + margin > board_width_mm:
            row_x = connector_col
            row_y += row_height + clearance
            row_height = 0.0
        x = row_x + w / 2
        y = row_y + h / 2
        if y + h / 2 > board_height_mm - margin:
            y = board_height_mm - margin - h / 2  # clamp
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
