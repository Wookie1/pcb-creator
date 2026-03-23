"""Fiducial marker placement for PCB assembly machine alignment.

Places 2 fiducial markers per populated board side in diagonally opposite
corners.  Fiducials are 1mm copper dots with 2mm clearance (3mm total
exclusion zone).
"""

from __future__ import annotations

import copy

# Fiducial physical parameters
FIDUCIAL_DOT_MM = 1.0
FIDUCIAL_CLEARANCE_MM = 2.0
FIDUCIAL_FOOTPRINT_MM = FIDUCIAL_DOT_MM + FIDUCIAL_CLEARANCE_MM  # 3.0mm
FIDUCIAL_OFFSET_MM = 2.0  # distance from board corner to fiducial center


def determine_populated_layers(placement: dict) -> set[str]:
    """Return the set of layers that have at least one component placed."""
    layers: set[str] = set()
    for item in placement.get("placements", []):
        if item.get("component_type") == "fiducial":
            continue  # don't count existing fiducials
        layers.add(item.get("layer", "top"))
    return layers


def _get_bounding_box(
    x: float, y: float, w: float, h: float, rotation: int,
) -> tuple[float, float, float, float]:
    """Get axis-aligned bounding box (x_min, y_min, x_max, y_max)."""
    if rotation in (90, 270):
        w, h = h, w
    hw, hh = w / 2, h / 2
    return (x - hw, y - hh, x + hw, y + hh)


def _boxes_overlap(
    b1: tuple[float, float, float, float],
    b2: tuple[float, float, float, float],
    clearance: float = 0.5,
) -> bool:
    """Check if two bounding boxes overlap (with clearance)."""
    return not (
        b1[2] + clearance <= b2[0] or
        b2[2] + clearance <= b1[0] or
        b1[3] + clearance <= b2[1] or
        b2[3] + clearance <= b1[1]
    )


def _fiducial_conflicts(
    fx: float, fy: float, layer: str,
    existing: list[dict], clearance: float = 0.5,
) -> bool:
    """Check if a fiducial at (fx, fy) would overlap any existing component on the same layer."""
    fid_box = _get_bounding_box(fx, fy, FIDUCIAL_FOOTPRINT_MM, FIDUCIAL_FOOTPRINT_MM, 0)
    for item in existing:
        if item.get("layer") != layer:
            continue
        comp_box = _get_bounding_box(
            item["x_mm"], item["y_mm"],
            item["footprint_width_mm"], item["footprint_height_mm"],
            item.get("rotation_deg", 0),
        )
        if _boxes_overlap(fid_box, comp_box, clearance):
            return True
    return False


def place_fiducials(placement: dict) -> list[dict]:
    """Compute fiducial marker positions for each populated layer.

    Places 2 fiducials per populated layer in diagonally opposite corners.
    Tries bottom-left + top-right first, falls back to top-left + bottom-right
    if the primary positions conflict with existing components.

    Returns:
        List of placement_item dicts for the fiducial markers.
    """
    board = placement.get("board", {})
    board_w = board.get("width_mm", 50)
    board_h = board.get("height_mm", 30)
    existing = placement.get("placements", [])
    populated = determine_populated_layers(placement)

    fiducials: list[dict] = []
    dual_layer = len(populated) > 1

    for layer in sorted(populated):
        # Candidate positions: two diagonals
        offset = FIDUCIAL_OFFSET_MM
        diag_a = [
            (offset, offset),                          # bottom-left
            (board_w - offset, board_h - offset),      # top-right
        ]
        diag_b = [
            (offset, board_h - offset),                # top-left
            (board_w - offset, offset),                # bottom-right
        ]

        # Try primary diagonal first
        chosen = diag_a
        conflicts_a = sum(
            1 for fx, fy in diag_a
            if _fiducial_conflicts(fx, fy, layer, existing)
        )
        conflicts_b = sum(
            1 for fx, fy in diag_b
            if _fiducial_conflicts(fx, fy, layer, existing)
        )
        if conflicts_a > conflicts_b:
            chosen = diag_b

        # Generate designators
        if dual_layer:
            layer_suffix = "T" if layer == "top" else "B"
            des_prefix = f"FID_{layer_suffix}"
        else:
            des_prefix = "FID"

        for i, (fx, fy) in enumerate(chosen, start=1):
            fiducials.append({
                "designator": f"{des_prefix}{i}",
                "component_type": "fiducial",
                "package": "Fiducial_1mm",
                "footprint_width_mm": FIDUCIAL_FOOTPRINT_MM,
                "footprint_height_mm": FIDUCIAL_FOOTPRINT_MM,
                "x_mm": round(fx, 2),
                "y_mm": round(fy, 2),
                "rotation_deg": 0,
                "layer": layer,
                "placement_source": "optimizer",
            })

    return fiducials


def add_fiducials_to_placement(placement: dict) -> dict:
    """Add fiducial markers to placement, removing any existing fiducials first.

    Returns a new placement dict (does not mutate the input).
    """
    result = copy.deepcopy(placement)

    # Remove any existing fiducials
    result["placements"] = [
        p for p in result.get("placements", [])
        if p.get("component_type") != "fiducial"
    ]

    # Add fresh fiducials
    fiducials = place_fiducials(result)
    result["placements"].extend(fiducials)

    return result
