"""Placement validator — deterministic DRC checks for component placement.

Standalone CLI: python validate_placement.py <placement.json> --netlist <netlist.json>
Also importable: from validate_placement import validate_placement
"""

import argparse
import json
import sys
from pathlib import Path

import jsonschema

# Minimum component-to-component clearance in mm
MIN_CLEARANCE_MM = 0.5

# Maximum distance from board edge for connectors (warning threshold)
CONNECTOR_EDGE_MAX_MM = 5.0

# Maximum distance for decoupling cap to associated IC (warning threshold)
DECOUPLING_PROXIMITY_MAX_MM = 5.0

# Load schema
SCHEMA_PATH = Path(__file__).parent.parent / "schemas" / "placement_schema.json"


def _load_schema() -> dict:
    return json.loads(SCHEMA_PATH.read_text())


def _get_bounding_box(placement: dict) -> tuple[float, float, float, float]:
    """Get axis-aligned bounding box (x_min, y_min, x_max, y_max) for a placement.

    Accounts for rotation: 0/180 keep width/height, 90/270 swap them.
    """
    x = placement["x_mm"]
    y = placement["y_mm"]
    w = placement["footprint_width_mm"]
    h = placement["footprint_height_mm"]
    rot = placement["rotation_deg"]

    if rot in (90, 270):
        w, h = h, w

    half_w = w / 2
    half_h = h / 2
    return (x - half_w, y - half_h, x + half_w, y + half_h)


def _boxes_overlap(a: tuple, b: tuple) -> bool:
    """Check if two axis-aligned bounding boxes overlap."""
    return not (a[2] <= b[0] or b[2] <= a[0] or a[3] <= b[1] or b[3] <= a[1])


def _box_clearance(a: tuple, b: tuple) -> float:
    """Compute minimum clearance between two non-overlapping boxes.

    Returns 0 if they overlap, positive distance otherwise.
    """
    if _boxes_overlap(a, b):
        return 0.0

    dx = max(0, max(a[0] - b[2], b[0] - a[2]))
    dy = max(0, max(a[1] - b[3], b[1] - a[3]))
    return max(dx, dy) if dx == 0 or dy == 0 else (dx**2 + dy**2) ** 0.5


def _min_edge_distance(bbox: tuple, board_w: float, board_h: float) -> float:
    """Minimum distance from bounding box edge to nearest board edge."""
    return min(
        bbox[0],  # distance to left edge
        bbox[1],  # distance to bottom edge
        board_w - bbox[2],  # distance to right edge
        board_h - bbox[3],  # distance to top edge
    )


def validate_schema(placement: dict) -> list[str]:
    """Validate placement JSON against schema. Returns list of error messages."""
    schema = _load_schema()
    validator = jsonschema.Draft7Validator(schema)
    errors = []
    for error in sorted(validator.iter_errors(placement), key=lambda e: list(e.path)):
        path = ".".join(str(p) for p in error.absolute_path) or "(root)"
        errors.append(f"Schema: {path}: {error.message}")
    return errors


def validate_cross_reference(
    placement: dict, netlist: dict
) -> tuple[list[str], list[str]]:
    """Cross-reference placement entries against netlist components.

    Returns (errors, warnings).
    """
    errors = []
    warnings = []

    # Build netlist component lookup
    netlist_components = {}
    for elem in netlist.get("elements", []):
        if elem.get("element_type") == "component":
            netlist_components[elem["designator"]] = elem

    # Build placement lookup and check for duplicates
    placement_by_ref = {}
    for item in placement.get("placements", []):
        ref = item["designator"]
        if ref in placement_by_ref:
            errors.append(f"Duplicate placement for {ref}")
        placement_by_ref[ref] = item

    # Check every netlist component is placed
    for ref in netlist_components:
        if ref not in placement_by_ref:
            errors.append(f"{ref} is missing from placement (in netlist but not placed)")

    # Check no phantom placements (fiducials are exempt — not in netlist by design)
    for ref in placement_by_ref:
        if ref not in netlist_components:
            item = placement_by_ref[ref]
            if item.get("component_type") == "fiducial":
                continue
            errors.append(f"{ref} has no matching netlist component (phantom placement)")

    # Check type and package match
    for ref, item in placement_by_ref.items():
        if ref not in netlist_components:
            continue
        nc = netlist_components[ref]

        if item["component_type"].lower() != nc["component_type"].lower():
            errors.append(
                f"{ref}: component_type mismatch — "
                f"placement has '{item['component_type']}', "
                f"netlist has '{nc['component_type']}'"
            )

        if item["package"].lower() != nc["package"].lower():
            errors.append(
                f"{ref}: package mismatch — "
                f"placement has '{item['package']}', "
                f"netlist has '{nc['package']}'"
            )

    return errors, warnings


def validate_board_boundary(placement: dict) -> list[str]:
    """Check all components are within board boundary. Returns errors."""
    errors = []
    board = placement.get("board", {})
    board_w = board.get("width_mm", 0)
    board_h = board.get("height_mm", 0)

    for item in placement.get("placements", []):
        bbox = _get_bounding_box(item)
        ref = item["designator"]

        if bbox[0] < 0:
            errors.append(f"{ref}: extends {abs(bbox[0]):.2f}mm past left board edge")
        if bbox[1] < 0:
            errors.append(f"{ref}: extends {abs(bbox[1]):.2f}mm past bottom board edge")
        if bbox[2] > board_w:
            errors.append(
                f"{ref}: extends {bbox[2] - board_w:.2f}mm past right board edge"
            )
        if bbox[3] > board_h:
            errors.append(
                f"{ref}: extends {bbox[3] - board_h:.2f}mm past top board edge"
            )

    return errors


def validate_overlap_and_clearance(
    placement: dict,
) -> tuple[list[str], list[str]]:
    """Check for overlapping components and minimum clearance. Returns (errors, warnings)."""
    errors = []
    warnings = []
    items = placement.get("placements", [])

    # Pre-compute bounding boxes
    boxes = []
    for item in items:
        boxes.append((item["designator"], _get_bounding_box(item), item.get("layer", "top")))

    # Pairwise checks (only same layer)
    for i in range(len(boxes)):
        for j in range(i + 1, len(boxes)):
            ref_a, bbox_a, layer_a = boxes[i]
            ref_b, bbox_b, layer_b = boxes[j]

            # Components on different layers don't conflict
            if layer_a != layer_b:
                continue

            if _boxes_overlap(bbox_a, bbox_b):
                errors.append(f"{ref_a} and {ref_b} overlap on {layer_a} layer")
            else:
                clearance = _box_clearance(bbox_a, bbox_b)
                if clearance < MIN_CLEARANCE_MM:
                    errors.append(
                        f"{ref_a} and {ref_b}: clearance {clearance:.2f}mm "
                        f"< minimum {MIN_CLEARANCE_MM}mm"
                    )

    return errors, warnings


def validate_placement_rules(
    placement: dict, netlist: dict | None = None
) -> list[str]:
    """Advisory placement rules (warnings only).

    - Connectors should be near board edges
    - Decoupling caps should be near their ICs
    """
    warnings = []
    board = placement.get("board", {})
    board_w = board.get("width_mm", 0)
    board_h = board.get("height_mm", 0)

    items_by_ref = {}
    for item in placement.get("placements", []):
        items_by_ref[item["designator"]] = item

    for ref, item in items_by_ref.items():
        ctype = item.get("component_type", "").lower()
        bbox = _get_bounding_box(item)

        # Connector edge placement check
        if ctype == "connector":
            edge_dist = _min_edge_distance(bbox, board_w, board_h)
            if edge_dist > CONNECTOR_EDGE_MAX_MM:
                warnings.append(
                    f"{ref} (connector): {edge_dist:.1f}mm from nearest edge "
                    f"(recommended < {CONNECTOR_EDGE_MAX_MM}mm)"
                )

    # Decoupling cap proximity: look for caps near ICs via netlist connections
    if netlist:
        _check_decoupling_proximity(placement, netlist, items_by_ref, warnings)

    return warnings


def _check_decoupling_proximity(
    placement: dict,
    netlist: dict,
    items_by_ref: dict,
    warnings: list[str],
) -> None:
    """Check that decoupling/bypass caps are close to ICs they serve."""
    # Find ICs in netlist
    ic_refs = set()
    for elem in netlist.get("elements", []):
        if elem.get("element_type") == "component" and elem.get("component_type") in (
            "ic",
            "voltage_regulator",
        ):
            ic_refs.add(elem["designator"])

    # Find caps that share a power net with an IC
    # Build net membership: port_id -> net connections
    port_to_component = {}
    for elem in netlist.get("elements", []):
        if elem.get("element_type") == "port":
            port_to_component[elem["port_id"]] = elem["component_id"]

    component_id_to_ref = {}
    for elem in netlist.get("elements", []):
        if elem.get("element_type") == "component":
            component_id_to_ref[elem["component_id"]] = elem["designator"]

    for elem in netlist.get("elements", []):
        if elem.get("element_type") != "net":
            continue
        if elem.get("net_class") not in ("power", "ground"):
            continue

        # Find which components are on this power/ground net
        refs_on_net = set()
        for port_id in elem.get("connected_port_ids", []):
            comp_id = port_to_component.get(port_id)
            if comp_id:
                ref = component_id_to_ref.get(comp_id)
                if ref:
                    refs_on_net.add(ref)

        # Check cap-to-IC distances
        caps_on_net = [r for r in refs_on_net if r in items_by_ref and items_by_ref[r].get("component_type", "").lower() == "capacitor"]
        ics_on_net = [r for r in refs_on_net if r in ic_refs]

        for cap_ref in caps_on_net:
            cap = items_by_ref[cap_ref]
            for ic_ref in ics_on_net:
                if ic_ref not in items_by_ref:
                    continue
                ic = items_by_ref[ic_ref]
                dist = ((cap["x_mm"] - ic["x_mm"]) ** 2 + (cap["y_mm"] - ic["y_mm"]) ** 2) ** 0.5
                if dist > DECOUPLING_PROXIMITY_MAX_MM:
                    warnings.append(
                        f"{cap_ref} (decoupling cap): {dist:.1f}mm from {ic_ref} "
                        f"(recommended < {DECOUPLING_PROXIMITY_MAX_MM}mm)"
                    )


def validate_placement(
    placement_path: str,
    netlist_path: str | None = None,
) -> dict:
    """Run all placement validation checks.

    Returns: {"valid": bool, "errors": [], "warnings": [], "summary": "..."}
    """
    errors = []
    warnings = []

    # Load placement
    try:
        placement = json.loads(Path(placement_path).read_text())
    except (json.JSONDecodeError, FileNotFoundError) as e:
        return {
            "valid": False,
            "errors": [f"Failed to load placement: {e}"],
            "warnings": [],
            "summary": "Could not parse placement file",
        }

    # Load netlist if provided
    netlist = None
    if netlist_path:
        try:
            netlist = json.loads(Path(netlist_path).read_text())
        except (json.JSONDecodeError, FileNotFoundError) as e:
            return {
                "valid": False,
                "errors": [f"Failed to load netlist: {e}"],
                "warnings": [],
                "summary": "Could not parse netlist file",
            }

    # 1. Schema validation
    schema_errors = validate_schema(placement)
    errors.extend(schema_errors)

    # If schema fails, skip further checks
    if schema_errors:
        return {
            "valid": False,
            "errors": errors,
            "warnings": warnings,
            "summary": f"Schema validation failed with {len(schema_errors)} errors",
        }

    # 2. Cross-reference with netlist
    if netlist:
        xref_errors, xref_warnings = validate_cross_reference(placement, netlist)
        errors.extend(xref_errors)
        warnings.extend(xref_warnings)

    # 3. Board boundary
    boundary_errors = validate_board_boundary(placement)
    errors.extend(boundary_errors)

    # 4. Overlap and clearance
    overlap_errors, overlap_warnings = validate_overlap_and_clearance(placement)
    errors.extend(overlap_errors)
    warnings.extend(overlap_warnings)

    # 5. Advisory placement rules
    rule_warnings = validate_placement_rules(placement, netlist)
    warnings.extend(rule_warnings)

    valid = len(errors) == 0
    summary = (
        f"Placement valid: {len(placement.get('placements', []))} components placed"
        if valid
        else f"Placement invalid: {len(errors)} errors found"
    )

    return {
        "valid": valid,
        "errors": errors,
        "warnings": warnings,
        "summary": summary,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate PCB placement JSON")
    parser.add_argument("placement", type=str, help="Path to placement JSON file")
    parser.add_argument(
        "--netlist", type=str, default=None, help="Path to netlist JSON for cross-reference"
    )
    args = parser.parse_args()

    result = validate_placement(args.placement, args.netlist)
    print(json.dumps(result, indent=2))
    return 0 if result["valid"] else 1


if __name__ == "__main__":
    sys.exit(main())
