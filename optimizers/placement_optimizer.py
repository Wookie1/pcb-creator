#!/usr/bin/env python3
"""Simulated Annealing placement optimizer for PCB layouts.

Two modes:
- **Repair**: Resolves overlaps and boundary violations from an invalid LLM placement.
- **Optimize**: Minimizes wire length and crossings on a valid placement.

Usage:
    python placement_optimizer.py <placement.json> <netlist.json> [--iterations N] [--seed S]
    python placement_optimizer.py <placement.json> <netlist.json> --repair

Output:
    Writes optimized/repaired placement JSON (same schema, updated coordinates).
"""

from __future__ import annotations

import argparse
import copy
import json
import math
import random
import sys
from dataclasses import dataclass
from pathlib import Path

from .ratsnest import (
    NetInfo, build_connectivity, compute_cost, total_wire_length,
    find_decoupling_associations, find_crystal_associations,
    DecouplingAssociation, CrystalAssociation,
)


# ---------- Configuration ----------

@dataclass
class SAConfig:
    """Simulated annealing configuration."""
    max_iterations: int | None = None  # None = auto-scale from component count
    initial_temperature: float = 100.0
    cooling_rate: float = 0.995
    min_temperature: float = 0.1
    wire_weight: float = 1.0
    crossing_weight: float = 5.0
    proximity_weight: float = 8.0   # decoupling cap proximity to IC
    crystal_weight: float = 10.0    # crystal proximity to MCU
    grouping_weight: float = 2.0    # functional grouping affinity
    stagnation_limit: int = 500  # early stop if no improvement in N iterations
    seed: int | None = None


# Minimum clearance between components (same as validator)
MIN_CLEARANCE_MM = 0.5

# Minimum clearance from any pad to the board edge (DFM standard)
BOARD_EDGE_CLEARANCE_MM = 1.0

# Component types that are pinned (never moved)
PINNED_TYPES = {"connector", "fiducial"}


# ---------- Helpers ----------

def _compute_iterations(n_movable: int, max_override: int | None) -> int:
    """Compute iteration count scaled to the number of movable components."""
    if max_override is not None:
        return max_override
    return max(2000, min(50000, n_movable * 1000))


def _get_bounding_box(
    x: float, y: float, w: float, h: float, rotation: int,
) -> tuple[float, float, float, float]:
    """Axis-aligned bounding box: (x_min, y_min, x_max, y_max)."""
    if rotation in (90, 270):
        w, h = h, w
    hw, hh = w / 2, h / 2
    return (x - hw, y - hh, x + hw, y + hh)


def _get_pad_extent_box(
    x: float, y: float, w: float, h: float, rotation: int,
    package: str, pin_count: int,
) -> tuple[float, float, float, float]:
    """Bounding box that includes all pad positions (not just the footprint body).

    Uses pad_geometry to get actual pin offsets, which may extend beyond the
    footprint body dimensions (e.g., PinHeader_1x8 has pins spanning 17.78mm
    but might have a footprint body of only 5mm from the LLM).
    """
    from .pad_geometry import get_footprint_def, _generate_fallback_footprint

    fp_def = get_footprint_def(package, pin_count)
    if fp_def is None:
        fp_def = _generate_fallback_footprint(w, h, pin_count)

    # Compute rotated pad positions and find extent
    rot_rad = math.radians(rotation)
    cos_r, sin_r = math.cos(rot_rad), math.sin(rot_rad)

    pw, ph = fp_def.pad_size
    x_min, y_min = x, y
    x_max, y_max = x, y

    for dx, dy in fp_def.pin_offsets.values():
        # Apply rotation
        rx = dx * cos_r - dy * sin_r
        ry = dx * sin_r + dy * cos_r
        pad_x, pad_y = x + rx, y + ry
        # Include pad size
        x_min = min(x_min, pad_x - pw / 2)
        y_min = min(y_min, pad_y - ph / 2)
        x_max = max(x_max, pad_x + pw / 2)
        y_max = max(y_max, pad_y + ph / 2)

    # Also include the footprint body
    body_box = _get_bounding_box(x, y, w, h, rotation)
    x_min = min(x_min, body_box[0])
    y_min = min(y_min, body_box[1])
    x_max = max(x_max, body_box[2])
    y_max = max(y_max, body_box[3])

    return (x_min, y_min, x_max, y_max)


def _boxes_overlap_with_clearance(
    b1: tuple[float, float, float, float],
    b2: tuple[float, float, float, float],
    clearance: float = MIN_CLEARANCE_MM,
) -> bool:
    """True if boxes overlap or are closer than clearance."""
    return not (
        b1[2] + clearance <= b2[0] or
        b2[2] + clearance <= b1[0] or
        b1[3] + clearance <= b2[1] or
        b2[3] + clearance <= b1[1]
    )


# ---------- Placement quality cost functions ----------

PROXIMITY_THRESHOLD_MM = 5.0  # decoupling caps should be within this distance of IC
CRYSTAL_THRESHOLD_MM = 5.0    # crystals should be within this distance of MCU


def _proximity_cost(
    positions: dict[str, tuple[float, float]],
    decoupling: list[DecouplingAssociation],
    threshold: float = PROXIMITY_THRESHOLD_MM,
) -> float:
    """Quadratic penalty for decoupling caps far from their IC."""
    cost = 0.0
    for assoc in decoupling:
        p_cap = positions.get(assoc.cap_designator)
        p_ic = positions.get(assoc.ic_designator)
        if p_cap and p_ic:
            d = abs(p_cap[0] - p_ic[0]) + abs(p_cap[1] - p_ic[1])
            if d > threshold:
                cost += (d - threshold) ** 2
    return cost


def _crystal_cost(
    positions: dict[str, tuple[float, float]],
    crystals: list[CrystalAssociation],
    threshold: float = CRYSTAL_THRESHOLD_MM,
) -> float:
    """Quadratic penalty for crystals far from their MCU."""
    cost = 0.0
    for assoc in crystals:
        p_xtal = positions.get(assoc.crystal_designator)
        p_ic = positions.get(assoc.ic_designator)
        if p_xtal and p_ic:
            d = abs(p_xtal[0] - p_ic[0]) + abs(p_xtal[1] - p_ic[1])
            if d > threshold:
                cost += (d - threshold) ** 2
    return cost


def _grouping_cost(
    positions: dict[str, tuple[float, float]],
    grouping_pairs: list[tuple[str, str]],
) -> float:
    """Manhattan distance penalty for functionally related components being far apart."""
    cost = 0.0
    for a, b in grouping_pairs:
        pa = positions.get(a)
        pb = positions.get(b)
        if pa and pb:
            cost += abs(pa[0] - pb[0]) + abs(pa[1] - pb[1])
    return cost


def _build_grouping_pairs(nets: list[NetInfo]) -> list[tuple[str, str]]:
    """Find component pairs that share 2+ nets (functionally related)."""
    shared_count: dict[tuple[str, str], int] = {}
    for net in nets:
        for i, d1 in enumerate(net.designators):
            for d2 in net.designators[i + 1:]:
                pair = tuple(sorted([d1, d2]))
                shared_count[pair] = shared_count.get(pair, 0) + 1
    return [(a, b) for (a, b), count in shared_count.items() if count >= 2]


def _compute_full_cost(
    positions: dict[str, tuple[float, float]],
    nets: list[NetInfo],
    config: SAConfig,
    decoupling: list[DecouplingAssociation],
    crystals: list[CrystalAssociation],
    grouping_pairs: list[tuple[str, str]],
) -> float:
    """Compute total weighted SA cost including all terms."""
    result = compute_cost(nets, positions, config.wire_weight, config.crossing_weight)
    cost = (config.wire_weight * result.total_wire_length +
            config.crossing_weight * result.crossing_count)

    if decoupling and config.proximity_weight > 0:
        cost += config.proximity_weight * _proximity_cost(positions, decoupling)
    if crystals and config.crystal_weight > 0:
        cost += config.crystal_weight * _crystal_cost(positions, crystals)
    if grouping_pairs and config.grouping_weight > 0:
        cost += config.grouping_weight * _grouping_cost(positions, grouping_pairs)

    return cost


# ---------- Core optimizer ----------

def optimize_placement(
    placement: dict,
    netlist: dict,
    config: SAConfig | None = None,
) -> dict:
    """Optimize a placement using simulated annealing.

    Args:
        placement: Valid placement dict (must pass validator).
        netlist: Netlist dict for connectivity information.
        config: SA parameters.  None uses defaults with auto-scaling.

    Returns:
        New placement dict with optimized coordinates/rotations.
        Components that moved will have placement_source="optimizer".
    """
    if config is None:
        config = SAConfig()

    rng = random.Random(config.seed)

    # Parse placement data
    board = placement.get("board", {})
    board_w = board.get("width_mm", 50.0)
    board_h = board.get("height_mm", 30.0)

    items = placement.get("placements", [])
    if not items:
        return copy.deepcopy(placement)

    # Build component data structures
    positions: dict[str, tuple[float, float]] = {}
    rotations: dict[str, int] = {}
    footprints: dict[str, tuple[float, float]] = {}  # (width, height) before rotation
    layers: dict[str, str] = {}
    pinned: set[str] = set()

    # Build port count per component from netlist
    comp_pin_counts: dict[str, int] = {}
    for elem in netlist.get("elements", []):
        if elem.get("element_type") == "port":
            cid = elem.get("component_id", "")
            comp_pin_counts[cid] = comp_pin_counts.get(cid, 0) + 1

    # Map designator -> component_id for pin count lookup
    des_to_comp_id: dict[str, str] = {}
    for elem in netlist.get("elements", []):
        if elem.get("element_type") == "component":
            des_to_comp_id[elem.get("designator", "")] = elem["component_id"]

    packages: dict[str, tuple[str, int]] = {}  # des -> (package, pin_count)

    for item in items:
        des = item["designator"]
        positions[des] = (item["x_mm"], item["y_mm"])
        rotations[des] = item.get("rotation_deg", 0)
        footprints[des] = (item["footprint_width_mm"], item["footprint_height_mm"])
        layers[des] = item.get("layer", "top")

        # Track package info for pad-aware bounds
        pkg = item.get("package", "")
        cid = des_to_comp_id.get(des, "")
        pin_count = comp_pin_counts.get(cid, 2)
        packages[des] = (pkg, pin_count)

        # Pin components that shouldn't move
        if item.get("placement_source") == "user":
            pinned.add(des)
        if item.get("component_type") in PINNED_TYPES:
            pinned.add(des)

    movable = [d for d in positions if d not in pinned]

    if len(movable) == 0:
        # Nothing to optimize
        return copy.deepcopy(placement)

    # Build connectivity from netlist
    nets = build_connectivity(netlist)

    # Build placement quality associations
    decoupling = find_decoupling_associations(netlist)
    crystal_assocs = find_crystal_associations(netlist)
    grouping_pairs = _build_grouping_pairs(nets)

    if decoupling:
        print(f"  Decoupling associations: {len(decoupling)} (cap→IC pairs)")
    if crystal_assocs:
        print(f"  Crystal associations: {len(crystal_assocs)} (crystal→IC pairs)")
    if grouping_pairs:
        print(f"  Grouping pairs: {len(grouping_pairs)} (components sharing 2+ nets)")

    # Compute iteration count
    iterations = _compute_iterations(len(movable), config.max_iterations)

    # Initial cost
    initial_result = compute_cost(nets, positions, config.wire_weight, config.crossing_weight)
    initial_cost = _compute_full_cost(
        positions, nets, config, decoupling, crystal_assocs, grouping_pairs
    )

    # SA state
    current_pos = dict(positions)
    current_rot = dict(rotations)
    current_cost = initial_cost

    best_pos = dict(current_pos)
    best_rot = dict(current_rot)
    best_cost = current_cost

    T = config.initial_temperature
    since_improvement = 0
    accepted = 0

    # Group components by package for swap moves
    package_groups: dict[str, list[str]] = {}
    for item in items:
        des = item["designator"]
        if des in pinned:
            continue
        pkg = item.get("package", "")
        if pkg not in package_groups:
            package_groups[pkg] = []
        package_groups[pkg].append(des)
    # Only keep groups with 2+ components
    swappable_groups = [g for g in package_groups.values() if len(g) >= 2]

    for iteration in range(iterations):
        # Generate a move
        new_pos, new_rot = _generate_move(
            current_pos, current_rot, movable, swappable_groups,
            footprints, layers, board_w, board_h, T, config.initial_temperature, rng,
            nets=nets,
        )

        # Fast constraint check
        if not _is_valid(new_pos, new_rot, footprints, layers, board_w, board_h, packages):
            since_improvement += 1
            if since_improvement >= config.stagnation_limit:
                break
            continue

        # Compute new cost (includes all terms)
        new_cost = _compute_full_cost(
            new_pos, nets, config, decoupling, crystal_assocs, grouping_pairs
        )

        # Accept or reject
        delta = new_cost - current_cost
        if delta < 0 or rng.random() < math.exp(-delta / max(T, 1e-10)):
            current_pos = new_pos
            current_rot = new_rot
            current_cost = new_cost
            accepted += 1

            if current_cost < best_cost:
                best_pos = dict(current_pos)
                best_rot = dict(current_rot)
                best_cost = current_cost
                since_improvement = 0
            else:
                since_improvement += 1
        else:
            since_improvement += 1

        if since_improvement >= config.stagnation_limit:
            break

        # Cool down
        T *= config.cooling_rate
        if T < config.min_temperature:
            T = config.min_temperature

    # Build output placement
    result = copy.deepcopy(placement)
    for item in result["placements"]:
        des = item["designator"]
        if des in pinned:
            continue
        old_pos = positions[des]
        old_rot = rotations[des]
        new_p = best_pos[des]
        new_r = best_rot[des]
        item["x_mm"] = round(new_p[0], 2)
        item["y_mm"] = round(new_p[1], 2)
        item["rotation_deg"] = new_r
        # Mark as optimizer-placed if position or rotation changed
        if old_pos != new_p or old_rot != new_r:
            item["placement_source"] = "optimizer"

    # Compute final metrics for logging
    final_result = compute_cost(nets, best_pos, config.wire_weight, config.crossing_weight)
    improvement = (1 - best_cost / initial_cost) * 100 if initial_cost > 0 else 0

    print(f"  SA Optimizer: {iteration + 1} iterations, {accepted} accepted moves")
    print(f"  Wire length : {initial_result.total_wire_length:.1f}mm → {final_result.total_wire_length:.1f}mm")
    print(f"  Crossings   : {initial_result.crossing_count} → {final_result.crossing_count}")
    if decoupling:
        init_prox = _proximity_cost(positions, decoupling)
        final_prox = _proximity_cost(best_pos, decoupling)
        print(f"  Decoupling  : proximity cost {init_prox:.1f} → {final_prox:.1f}")
    if crystal_assocs:
        init_xtal = _crystal_cost(positions, crystal_assocs)
        final_xtal = _crystal_cost(best_pos, crystal_assocs)
        print(f"  Crystal     : proximity cost {init_xtal:.1f} → {final_xtal:.1f}")
    print(f"  Improvement : {improvement:.1f}%")

    return result


def _generate_move(
    positions: dict[str, tuple[float, float]],
    rotations: dict[str, int],
    movable: list[str],
    swappable_groups: list[list[str]],
    footprints: dict[str, tuple[float, float]],
    layers: dict[str, str],
    board_w: float,
    board_h: float,
    temperature: float,
    initial_temperature: float,
    rng: random.Random,
    nets: list[NetInfo] | None = None,
) -> tuple[dict[str, tuple[float, float]], dict[str, int]]:
    """Generate a random perturbation.

    Returns new (positions, rotations) dicts (copies).
    """
    new_pos = dict(positions)
    new_rot = dict(rotations)

    r = rng.random()

    if r < 0.70:
        # TRANSLATE: move one component by a random delta
        des = rng.choice(movable)
        # Scale delta with temperature: large at start, small at end
        scale = (temperature / initial_temperature) * max(board_w, board_h) / 4
        scale = max(scale, 0.1)  # minimum 0.1mm moves
        dx = rng.gauss(0, scale)
        dy = rng.gauss(0, scale)
        ox, oy = positions[des]
        # Clamp to board boundaries considering footprint
        w, h = footprints[des]
        rot = new_rot[des]
        if rot in (90, 270):
            w, h = h, w
        hw, hh = w / 2, h / 2
        nx = max(hw, min(board_w - hw, ox + dx))
        ny = max(hh, min(board_h - hh, oy + dy))
        new_pos[des] = (round(nx, 2), round(ny, 2))

    elif r < 0.85 and swappable_groups:
        # SWAP: exchange positions of two same-package components
        group = rng.choice(swappable_groups)
        a, b = rng.sample(group, 2)
        new_pos[a], new_pos[b] = new_pos[b], new_pos[a]

    else:
        # SMART ROTATE: 50% evaluate best rotation, 50% random
        des = rng.choice(movable)
        all_options = [0, 90, 180, 270]
        current = new_rot[des]

        if rng.random() < 0.5 and nets:
            # Evaluate: pick rotation that minimizes connected wire length
            relevant_nets = [n for n in nets if des in n.designators]
            if relevant_nets:
                best_rot_val = current
                best_wl = float('inf')
                for rot_candidate in all_options:
                    new_rot[des] = rot_candidate
                    wl = total_wire_length(relevant_nets, new_pos)
                    if wl < best_wl:
                        best_wl = wl
                        best_rot_val = rot_candidate
                new_rot[des] = best_rot_val
            else:
                options = [rv for rv in all_options if rv != current]
                new_rot[des] = rng.choice(options)
        else:
            # Random rotation (avoid current)
            options = [rv for rv in all_options if rv != current]
            new_rot[des] = rng.choice(options)

    return new_pos, new_rot


def _is_valid(
    positions: dict[str, tuple[float, float]],
    rotations: dict[str, int],
    footprints: dict[str, tuple[float, float]],
    layers: dict[str, str],
    board_w: float,
    board_h: float,
    packages: dict[str, tuple[str, int]] | None = None,
) -> bool:
    """Fast constraint check — no JSON serialization.

    Checks:
    - All pad extents within board boundaries (with BOARD_EDGE_CLEARANCE_MM)
    - No pairwise overlaps on the same layer (with MIN_CLEARANCE_MM)
    """
    ec = BOARD_EDGE_CLEARANCE_MM
    boxes: list[tuple[str, tuple[float, float, float, float], str]] = []

    for des in positions:
        x, y = positions[des]
        w, h = footprints[des]
        rot = rotations.get(des, 0)

        # Use pad-aware bounds if package info available
        if packages and des in packages:
            pkg, pin_count = packages[des]
            box = _get_pad_extent_box(x, y, w, h, rot, pkg, pin_count)
        else:
            box = _get_bounding_box(x, y, w, h, rot)

        # Board boundary check with edge clearance
        if (box[0] < ec - 0.01 or box[1] < ec - 0.01 or
                box[2] > board_w - ec + 0.01 or box[3] > board_h - ec + 0.01):
            return False

        boxes.append((des, box, layers.get(des, "top")))

    # Pairwise overlap check (same layer only)
    for i in range(len(boxes)):
        for j in range(i + 1, len(boxes)):
            if boxes[i][2] != boxes[j][2]:
                continue  # different layers, no conflict
            if _boxes_overlap_with_clearance(boxes[i][1], boxes[j][1]):
                return False

    return True


# ---------- Repair mode ----------

def _count_violations(
    positions: dict[str, tuple[float, float]],
    rotations: dict[str, int],
    footprints: dict[str, tuple[float, float]],
    layers: dict[str, str],
    board_w: float,
    board_h: float,
    packages: dict[str, tuple[str, int]] | None = None,
) -> tuple[int, float]:
    """Count constraint violations and total overlap depth.

    Returns (violation_count, total_overlap_depth_mm).
    """
    ec = BOARD_EDGE_CLEARANCE_MM
    violations = 0
    overlap_depth = 0.0

    boxes: list[tuple[str, tuple[float, float, float, float], str]] = []
    for des in positions:
        x, y = positions[des]
        w, h = footprints[des]
        rot = rotations.get(des, 0)

        if packages and des in packages:
            pkg, pin_count = packages[des]
            box = _get_pad_extent_box(x, y, w, h, rot, pkg, pin_count)
        else:
            box = _get_bounding_box(x, y, w, h, rot)
        boxes.append((des, box, layers.get(des, "top")))

        # Board boundary violations (with edge clearance)
        if box[0] < ec - 0.01:
            violations += 1
            overlap_depth += ec - box[0]
        if box[1] < ec - 0.01:
            violations += 1
            overlap_depth += ec - box[1]
        if box[2] > board_w - ec + 0.01:
            violations += 1
            overlap_depth += box[2] - (board_w - ec)
        if box[3] > board_h - ec + 0.01:
            violations += 1
            overlap_depth += box[3] - (board_h - ec)

    # Pairwise overlap check (same layer)
    for i in range(len(boxes)):
        for j in range(i + 1, len(boxes)):
            if boxes[i][2] != boxes[j][2]:
                continue
            b1, b2 = boxes[i][1], boxes[j][1]
            # Check if they overlap (with clearance)
            if _boxes_overlap_with_clearance(b1, b2):
                violations += 1
                # Compute overlap depth (how much they need to move apart)
                ox = min(b1[2] + MIN_CLEARANCE_MM - b2[0], b2[2] + MIN_CLEARANCE_MM - b1[0])
                oy = min(b1[3] + MIN_CLEARANCE_MM - b2[1], b2[3] + MIN_CLEARANCE_MM - b1[1])
                if ox > 0 and oy > 0:
                    overlap_depth += min(ox, oy)

    return violations, overlap_depth


def repair_placement(
    placement: dict,
    netlist: dict | None = None,
    max_iterations: int = 10000,
    seed: int | None = None,
) -> dict:
    """Repair a placement with overlaps and/or boundary violations.

    Uses a modified SA where the cost function heavily penalizes violations.
    Components are pushed apart until all constraints are satisfied, then
    wire length is minimized as a secondary objective.

    Args:
        placement: Placement dict (may have overlaps).
        netlist: Optional netlist for wire-length aware repair.
        max_iterations: Maximum repair iterations.
        seed: Random seed.

    Returns:
        Repaired placement dict. Components that moved have placement_source="optimizer".
    """
    rng = random.Random(seed)

    board = placement.get("board", {})
    board_w = board.get("width_mm", 50.0)
    board_h = board.get("height_mm", 30.0)

    items = placement.get("placements", [])
    if not items:
        return copy.deepcopy(placement)

    # Build data structures
    positions: dict[str, tuple[float, float]] = {}
    rotations: dict[str, int] = {}
    footprints: dict[str, tuple[float, float]] = {}
    layers: dict[str, str] = {}
    pinned: set[str] = set()
    packages: dict[str, tuple[str, int]] = {}

    # Build port count per component from netlist
    comp_pin_counts: dict[str, int] = {}
    des_to_comp_id: dict[str, str] = {}
    if netlist:
        for elem in netlist.get("elements", []):
            if elem.get("element_type") == "port":
                cid = elem.get("component_id", "")
                comp_pin_counts[cid] = comp_pin_counts.get(cid, 0) + 1
            elif elem.get("element_type") == "component":
                des_to_comp_id[elem.get("designator", "")] = elem["component_id"]

    for item in items:
        des = item["designator"]
        positions[des] = (item["x_mm"], item["y_mm"])
        rotations[des] = item.get("rotation_deg", 0)
        footprints[des] = (item["footprint_width_mm"], item["footprint_height_mm"])
        layers[des] = item.get("layer", "top")
        if item.get("placement_source") == "user":
            pinned.add(des)

        pkg = item.get("package", "")
        cid = des_to_comp_id.get(des, "")
        pin_count = comp_pin_counts.get(cid, 2)
        packages[des] = (pkg, pin_count)

    movable = [d for d in positions if d not in pinned]
    if not movable:
        return copy.deepcopy(placement)

    # Build connectivity if netlist provided
    nets = []
    if netlist:
        from .ratsnest import build_connectivity, total_wire_length
        nets = build_connectivity(netlist)

    # Repair cost: violations dominate, wire length is secondary
    VIOLATION_WEIGHT = 1000.0
    WIRE_WEIGHT = 0.1

    def cost(pos, rot):
        v_count, v_depth = _count_violations(pos, rot, footprints, layers, board_w, board_h, packages)
        violation_cost = VIOLATION_WEIGHT * (v_count * 10 + v_depth)
        wire_cost = 0.0
        if nets and v_count == 0:
            wire_cost = WIRE_WEIGHT * total_wire_length(nets, pos)
        return violation_cost + wire_cost, v_count

    current_pos = dict(positions)
    current_rot = dict(rotations)
    current_cost, current_violations = cost(current_pos, current_rot)

    best_pos = dict(current_pos)
    best_rot = dict(current_rot)
    best_cost = current_cost
    best_violations = current_violations

    # Swappable groups
    package_groups: dict[str, list[str]] = {}
    for item in items:
        des = item["designator"]
        if des in pinned:
            continue
        pkg = item.get("package", "")
        if pkg not in package_groups:
            package_groups[pkg] = []
        package_groups[pkg].append(des)
    swappable_groups = [g for g in package_groups.values() if len(g) >= 2]

    T = 200.0  # Higher initial temp for repair — need aggressive moves
    cooling = 0.997
    stagnation = 0
    max_stagnation = 1000

    for iteration in range(max_iterations):
        new_pos, new_rot = _generate_move(
            current_pos, current_rot, movable, swappable_groups,
            footprints, layers, board_w, board_h, T, 200.0, rng,
        )

        # In repair mode, we DON'T reject invalid moves — we score them
        new_cost, new_violations = cost(new_pos, new_rot)

        delta = new_cost - current_cost
        if delta < 0 or rng.random() < math.exp(-delta / max(T, 1e-10)):
            current_pos = new_pos
            current_rot = new_rot
            current_cost = new_cost
            current_violations = new_violations

            if current_cost < best_cost:
                best_pos = dict(current_pos)
                best_rot = dict(current_rot)
                best_cost = current_cost
                best_violations = current_violations
                stagnation = 0
            else:
                stagnation += 1
        else:
            stagnation += 1

        # Early exit if we've resolved all violations and stagnated
        if best_violations == 0 and stagnation >= 500:
            break
        if stagnation >= max_stagnation:
            break

        T *= cooling
        if T < 0.01:
            T = 0.01

    # Build output
    result = copy.deepcopy(placement)
    for item in result["placements"]:
        des = item["designator"]
        if des in pinned:
            continue
        old_pos = positions[des]
        old_rot = rotations[des]
        new_p = best_pos[des]
        new_r = best_rot[des]
        item["x_mm"] = round(new_p[0], 2)
        item["y_mm"] = round(new_p[1], 2)
        item["rotation_deg"] = new_r
        if old_pos != new_p or old_rot != new_r:
            item["placement_source"] = "optimizer"

    initial_v, _ = _count_violations(positions, rotations, footprints, layers, board_w, board_h, packages)
    print(f"  Repair: {iteration + 1} iterations")
    print(f"  Violations: {initial_v} → {best_violations}")
    if best_violations == 0:
        print(f"  All overlaps resolved ✓")
    else:
        print(f"  WARNING: {best_violations} violations remain")

    return result


# ---------- CLI ----------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Optimize PCB placement via simulated annealing",
    )
    parser.add_argument("placement", type=Path, help="Path to placement JSON")
    parser.add_argument("netlist", type=Path, help="Path to netlist JSON")
    parser.add_argument("--iterations", type=int, default=None,
                        help="Max iterations (default: auto-scale)")
    parser.add_argument("--seed", type=int, default=None,
                        help="Random seed for reproducibility")
    parser.add_argument("--output", type=Path, default=None,
                        help="Output path (default: overwrite input)")

    args = parser.parse_args(argv)

    placement = json.loads(args.placement.read_text())
    netlist = json.loads(args.netlist.read_text())

    config = SAConfig(
        max_iterations=args.iterations,
        seed=args.seed,
    )

    optimized = optimize_placement(placement, netlist, config)

    output_path = args.output or args.placement
    output_path.write_text(json.dumps(optimized, indent=2))
    print(f"\n  Optimized placement written to {output_path}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
