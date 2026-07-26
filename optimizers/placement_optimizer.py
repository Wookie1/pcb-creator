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
import re
import sys
from dataclasses import dataclass
from pathlib import Path

from .ratsnest import (
    NetInfo, build_connectivity, total_wire_length,
    find_decoupling_associations, find_crystal_associations,
    DecouplingAssociation, CrystalAssociation, IncrementalCost,
    _PLANE_NET_CLASSES,
)

import logging

logger = logging.getLogger(__name__)


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
    congestion_weight: float = 0.0  # pad-density penalty (escape-route proxy);
                                    # 0 = off, enabled by the routing retry loop
    demand_weight: float = 0.0      # routing-DEMAND congestion (RUDY): spreads
                                    # each signal net's wire estimate over its
                                    # bounding box and penalizes cells whose
                                    # summed demand exceeds track capacity.
                                    # OFF by default: it is correct and self-
                                    # gating, but no board in the current suite
                                    # is channel-congestion-limited (morgan is
                                    # only ~7% over capacity and is fanout-, not
                                    # congestion-, limited), so the weight that
                                    # would make it bite is unvalidated. Kept
                                    # implemented + tested; enable explicitly
                                    # (e.g. ~40) once a congestion-limited board
                                    # is available to tune against.
    escape_weight: float = 6.0      # escape-halo penalty: keep foreign pads out
                                    # of the fanout channel a dense/fine-pitch
                                    # part needs to escape. Self-gating — only
                                    # parts that exceed the pin/pitch threshold
                                    # (or are listed in focus_components) get a
                                    # halo, so it is a no-op on simple boards.
    escape_track_pitch_mm: float = 0.4  # trace_width + clearance; sizes the
                                    # fanout annulus (set from the board's rules)
    focus_components: tuple[str, ...] = ()  # designators to give an enlarged
                                    # escape halo even if not intrinsically dense
                                    # — the routing-feedback retry lever (C):
                                    # localizes spacing to the unrouted region
                                    # instead of a blunt global clearance bump
    two_sided: bool = False         # allow flipping small SMD passives to the
                                    # bottom side (layer-flip SA move)
    bottom_penalty: float = 4.0     # cost per bottom-side component — on a
                                    # 2-layer board the bottom is the router's
                                    # escape layer, so flips must pay their way
                                    # (congestion relief > penalty)
    min_clearance_mm: float = 0.5   # component-to-component clearance enforced
                                    # during moves (raise to spread a congested board)
    stagnation_limit: int = 500  # early stop if no improvement in N iterations
    seed: int | None = None


# Minimum clearance between components (same as validator)
MIN_CLEARANCE_MM = 0.5

# Minimum clearance from any pad to the board edge (DFM standard)
BOARD_EDGE_CLEARANCE_MM = 1.0

# Component types that are pinned (never moved)
PINNED_TYPES = {"connector", "fiducial"}

# Consecutive rejected-as-invalid proposals before the SA loop gives up. This is
# NOT the stagnation limit: it only fires when the move generator cannot produce
# a single legal move for this many tries in a row, i.e. the board is packed
# solid and no further search will help. The iteration budget bounds the work
# either way, so it can be generous.
_INVALID_STREAK_LIMIT = 5000

# Area demand (parts plus their mandatory clearance, over usable board area)
# above which single-sided placement stops being a search problem. Packing
# heterogeneous rectangles with a required gap around each tops out well short
# of 100%; measured here, boards up to ~81% place reliably and arduino_nano at
# 96% never does, on any seed or iteration budget.
_PACKABLE_UTILIZATION = 0.85

# Packages that are mechanical keepouts — mounting holes and fiducials must
# stay where they were placed (they line up with the enclosure/assembly).
_KEEPOUT_PACKAGE_RE = re.compile(r"MountingHole|Fiducial", re.IGNORECASE)


def _effective_layer(package: str, pin_count: int, layer: str) -> str:
    """Through-hole parts block BOTH sides; SMD parts only their own layer."""
    from .pad_geometry import get_footprint_def, is_through_hole_package
    fp = get_footprint_def(package, pin_count)
    if is_through_hole_package(package, fp):
        return "all"
    return layer


def _layers_conflict(layer_a: str, layer_b: str) -> bool:
    return layer_a == layer_b or layer_a == "all" or layer_b == "all"


def _is_keepout_package(package: str) -> bool:
    return bool(package and _KEEPOUT_PACKAGE_RE.search(package))


# ---------- Helpers ----------

def _compute_iterations(n_movable: int, max_override: int | None) -> int:
    """Compute iteration count scaled to the number of movable components.

    The upper bound is 8 000 iterations — enough for good placement quality on
    boards up to ~70 components while keeping runtime under ~30 s on a Pi 5
    (ARM Cortex-A76, single core).  The old cap of 50 000 caused the Pi to
    hang for minutes on dense boards.

    The stagnation_limit (default 500) in SAConfig provides an early-exit if
    the optimizer hasn't improved in 500 consecutive iterations, so typical
    runs finish well under the cap.

    Override with PCB_OPTIMIZER_ITERATIONS env var or by passing an explicit
    SAConfig(max_iterations=N) for more control.
    """
    if max_override is not None:
        return max_override
    return max(2000, min(8000, n_movable * 200))


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


def _clamp_centre(
    x: float, y: float,
    w: float, h: float, rotation: int,
    board_w: float, board_h: float,
    package: str = "", pin_count: int = 2,
) -> tuple[float, float]:
    """Nearest centre that puts the part's PAD-EXTENT box inside the edge clearance.

    The single source of truth for "where may this component's centre sit". It
    must agree with `_is_valid` / `_count_violations`, which score the pad-extent
    box against BOARD_EDGE_CLEARANCE_MM — clamping on the body box instead (the
    old behaviour in three separate places) let callers propose and snap to
    positions the validator would always reject, most visibly for parts whose
    pads reach past their body outline.

    Offsets from the centre are translation-invariant, so the box is measured
    once at the origin and solved for the centre.
    """
    ec = BOARD_EDGE_CLEARANCE_MM
    if package:
        bx = _get_pad_extent_box(0.0, 0.0, w, h, rotation, package, pin_count)
    else:
        bx = _get_bounding_box(0.0, 0.0, w, h, rotation)
    lo_x, hi_x = ec - bx[0], board_w - ec - bx[2]
    lo_y, hi_y = ec - bx[1], board_h - ec - bx[3]
    # A part too big for the board leaves lo > hi; pin it at lo so the result is
    # deterministic and the violation still gets counted and reported.
    nx = max(lo_x, min(hi_x, x)) if lo_x <= hi_x else lo_x
    ny = max(lo_y, min(hi_y, y)) if lo_y <= hi_y else lo_y
    return nx, ny


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


def _build_grouping_pairs(
    nets: list[NetInfo],
    groups: dict[str, str] | None = None,
) -> list[tuple[str, str]]:
    """Component pairs that should be placed close together.

    Two sources, unioned into a single set of pairs:

      1. Shared-net heuristic (always on): components sharing 2+ nets are
         functionally related. This is the FLOOR — it is exactly the prior
         behavior, so a missing or wrong declared group can never drop the
         result below what the tool did before.
      2. Declared functional groups (optional): components the schematic
         engineer tagged with the same ``functional_group`` label (e.g. a power
         section, an MCU section, a USB block). Full mesh within each group.
         This is the explicit-hierarchy signal — the LLM knows the real circuit
         blocks, whereas the shared-net heuristic only guesses them from
         topology.

    A declared pair that is also a shared-net pair is deduped to one pair
    (weight is uniform per pair in ``_grouping_cost``), so the two sources
    reinforce without double-counting. The only downside of a mis-tag is a few
    spurious affinity pairs of bounded weight; the heuristic floor is
    untouched.
    """
    pairs: set[tuple[str, str]] = set()

    # Source 1 — shared-net floor (prior behavior).
    shared_count: dict[tuple[str, str], int] = {}
    for net in nets:
        for i, d1 in enumerate(net.designators):
            for d2 in net.designators[i + 1:]:
                pair = tuple(sorted([d1, d2]))
                shared_count[pair] = shared_count.get(pair, 0) + 1
    pairs.update(p for p, count in shared_count.items() if count >= 2)

    # Source 2 — declared functional groups (LLM-authored hierarchy).
    if groups:
        members: dict[str, list[str]] = {}
        for des, label in groups.items():
            if label:
                members.setdefault(label, []).append(des)
        total = len(groups)
        # A group that holds almost everything carries no placement signal —
        # it would just pull the whole board together. Drop a degenerate
        # "everything" tag (e.g. the LLM labelling every part "main") and fall
        # back to the shared-net floor for those parts.
        max_members = max(3, int(total * 0.6))
        for dess in members.values():
            dess = sorted(set(dess))
            if len(dess) < 2 or len(dess) > max_members:
                continue
            for i, a in enumerate(dess):
                for b in dess[i + 1:]:
                    pairs.add((a, b))

    return sorted(pairs)


def _read_functional_groups(netlist: dict) -> dict[str, str]:
    """Designator -> functional-group label, read from the netlist components.

    Reads the first-class ``functional_group`` field, falling back to
    ``properties.functional_group`` (the LLM sometimes nests it). Components
    with no label are simply absent, so the optimizer falls back to the
    shared-net heuristic for them.
    """
    groups: dict[str, str] = {}
    for elem in netlist.get("elements", []):
        if elem.get("element_type") != "component":
            continue
        des = elem.get("designator", "")
        label = elem.get("functional_group")
        if not label:
            props = elem.get("properties") or {}
            label = props.get("functional_group")
        if des and isinstance(label, str) and label.strip():
            groups[des] = label.strip()
    return groups


def _build_pin_nets(netlist: dict) -> dict[str, list[tuple[int, list[str]]]]:
    """designator -> [(pin_number, [other designators on that pin's net]), ...].

    Feeds the rotation heuristic in `_generate_move`, which needs to know where
    a specific PIN of a part sits relative to what it connects to. `NetInfo`
    only records which components a net touches, which is why the old rotation
    evaluation could not tell orientations apart.
    """
    components: dict[str, dict] = {}
    ports: dict[str, dict] = {}
    for elem in netlist.get("elements", []):
        etype = elem.get("element_type")
        if etype == "component":
            components[elem["component_id"]] = elem
        elif etype == "port":
            ports[elem["port_id"]] = elem

    port_info: dict[str, tuple[str, int]] = {}  # port_id -> (designator, pin)
    for pid, port in ports.items():
        comp = components.get(port.get("component_id", ""))
        pin = port.get("pin_number")
        if comp and isinstance(pin, int):
            port_info[pid] = (comp["designator"], pin)

    out: dict[str, list[tuple[int, list[str]]]] = {}
    for elem in netlist.get("elements", []):
        if elem.get("element_type") != "net":
            continue
        members = [port_info[pid] for pid in elem.get("connected_port_ids", [])
                   if pid in port_info]
        designators = {d for d, _ in members}
        if len(designators) < 2:
            continue  # a net inside one part tells rotation nothing
        for des, pin in members:
            others = sorted(designators - {des})
            if others:
                out.setdefault(des, []).append((pin, others))
    return out


def _rotation_pin_cost(
    des: str,
    rotation: int,
    positions: dict[str, tuple[float, float]],
    pin_nets: dict[str, list[tuple[int, list[str]]]],
    pin_offsets: dict[tuple[str, int], tuple[float, float]],
) -> float:
    """Manhattan distance from this part's PADS to what each pad connects to.

    Rotation-sensitive by construction, unlike the centre-to-centre wire length
    the SA cost uses — that is the whole point: turning a part moves its pins,
    not its centre, so centre-based wire length scores every orientation
    identically.
    """
    origin = positions.get(des)
    if origin is None:
        return 0.0
    cx, cy = origin
    rad = math.radians(rotation)
    ca, sa = math.cos(rad), math.sin(rad)
    total = 0.0
    for pin, others in pin_nets.get(des, ()):
        off = pin_offsets.get((des, pin))
        if off is None:
            continue
        px = cx + off[0] * ca - off[1] * sa
        py = cy + off[0] * sa + off[1] * ca
        for other in others:
            p = positions.get(other)
            if p is not None:
                total += abs(px - p[0]) + abs(py - p[1])
    return total


def _build_pin_offsets(
    packages: dict[str, tuple[str, int]],
) -> dict[tuple[str, int], tuple[float, float]]:
    """(designator, pin_number) -> unrotated pad offset from the part centre."""
    from .pad_geometry import get_footprint_def
    out: dict[tuple[str, int], tuple[float, float]] = {}
    for des, (pkg, pin_count) in packages.items():
        fp = get_footprint_def(pkg, pin_count)
        if fp is None:
            continue
        for pin, off in fp.pin_offsets.items():
            try:
                out[(des, int(pin))] = (float(off[0]), float(off[1]))
            except (TypeError, ValueError):  # lettered BGA pads etc.
                continue
    return out


CONGESTION_CELL_MM = 5.0     # pad-density bucket size
CONGESTION_THRESHOLD = 10.0  # pins per cell before the penalty kicks in


def _congestion_cost(
    positions: dict[str, tuple[float, float]],
    packages: dict[str, tuple[str, int]],
    cell_mm: float = CONGESTION_CELL_MM,
    threshold: float = CONGESTION_THRESHOLD,
    layers: dict[str, str] | None = None,
) -> float:
    """Quadratic penalty on pad density per grid cell (escape-route proxy).

    Cells holding more pins than the threshold are increasingly hard to
    route out of; spreading them costs a little wirelength but saves
    rip-up/retry later.
    """
    buckets: dict[tuple, float] = {}
    for des, (x, y) in positions.items():
        pin_count = packages.get(des, ("", 2))[1]
        layer = layers.get(des, "top") if layers else "top"
        key = (layer, int(x // cell_mm), int(y // cell_mm))
        buckets[key] = buckets.get(key, 0.0) + pin_count
    cost = 0.0
    for pins in buckets.values():
        if pins > threshold:
            cost += (pins - threshold) ** 2
    return cost


# ---- Routing-demand congestion / RUDY (enhancement B) -------------------
#
# The pad-density `_congestion_cost` above measures where copper *pads* sit;
# it misses the thing that actually fails on dense boards — many nets all
# wanting to route through the same channel. B models routing *demand*: each
# signal net spreads its estimated wire (half-perimeter of its pad bounding
# box) uniformly over that box (the classic RUDY estimator), the per-cell
# contributions sum into a demand heatmap, and cells whose demand exceeds the
# track capacity they could physically carry are penalized. SA then pulls
# connected parts together where there is room and away from hotspots — the
# single biggest "make placement routability-aware" lever, and it generalizes
# beyond fine-pitch. Plane (power/ground) nets are excluded: they are delivered
# by copper pours, not routed through channels.

DEMAND_CELL_MM = 2.5          # demand-heatmap bucket size — a few track-widths
                              # across; 5mm was too coarse to see channel-level
                              # congestion (morgan peaked at 56% util there).
DEMAND_SIGNAL_LAYERS = 2.0    # outer copper layers carry signal in every stackup
DEMAND_UTILIZATION_LIMIT = 0.75  # routers degrade well before 100% channel
                              # fill; penalize cells above this fraction of the
                              # nominal track capacity (standard routability rule)


def _routing_demand_cost(
    positions: dict[str, tuple[float, float]],
    signal_nets: list[NetInfo],
    track_pitch_mm: float,
    cell_mm: float = DEMAND_CELL_MM,
    signal_layers: float = DEMAND_SIGNAL_LAYERS,
) -> float:
    """RUDY routing-demand penalty (see comment above).

    `signal_nets` must already exclude plane nets. `track_pitch_mm` is
    trace+clearance — it sets how many parallel tracks a cell can carry.
    Penalty is normalized by capacity so each over-subscribed cell contributes
    an O(1) term (squared over-fraction), keeping the weight comparable to the
    other SA terms and the whole thing a no-op when no cell is over capacity.
    """
    if not signal_nets:
        return 0.0
    cell2 = cell_mm * cell_mm
    grid: dict[tuple[int, int], float] = {}
    for net in signal_nets:
        pts = [positions[d] for d in net.designators if d in positions]
        if len(pts) < 2:
            continue
        xs = [p[0] for p in pts]
        ys = [p[1] for p in pts]
        xmin, xmax = min(xs), max(xs)
        ymin, ymax = min(ys), max(ys)
        w, h = xmax - xmin, ymax - ymin
        area = max(w * h, cell2)              # floor for near-1D/degenerate boxes
        contrib = (w + h) / area * cell2       # ≈ HPWL spread, per cell
        ci0, ci1 = int(xmin // cell_mm), int(xmax // cell_mm)
        cj0, cj1 = int(ymin // cell_mm), int(ymax // cell_mm)
        for ci in range(ci0, ci1 + 1):
            for cj in range(cj0, cj1 + 1):
                grid[(ci, cj)] = grid.get((ci, cj), 0.0) + contrib
    capacity = max((cell_mm / max(track_pitch_mm, 0.1)) * cell_mm * signal_layers
                   * DEMAND_UTILIZATION_LIMIT, 1e-6)
    cost = 0.0
    for demand in grid.values():
        if demand > capacity:
            over = (demand - capacity) / capacity
            cost += over * over
    return cost


# ---- Escape-halo / fanout reservation (enhancement A) -------------------
#
# A dense or fine-pitch part needs a clear channel on its escape edges to fan
# its pins out — roughly `escapes × (trace + clearance)` of perimeter. Nothing
# reserves it today, so neighbours crowd the pin rows and the autorouter has
# nowhere to take the escapes (the morgan/CN1 plateau). We compute a per-part
# fanout demand and penalize foreign pads that intrude into an escape halo
# sized to that demand. The halo radius comes from the classic fanout-annulus
# bound: to lay `N` tracks at pitch `p` around a part you need a clear ring of
# circumference `N·p`, i.e. radius ≥ `N·p / 2π`, measured beyond the body.

ESCAPE_PIN_THRESHOLD = 8         # parts with this many pins get a reserved halo
ESCAPE_PITCH_THRESHOLD_MM = 0.8  # ...as do fine-pitch parts below this pitch


def _footprint_min_pitch(package: str, pin_count: int) -> float | None:
    """Nearest-neighbour pad pitch of a footprint, or None if unknown."""
    from .pad_geometry import get_footprint_def
    fp = get_footprint_def(package, pin_count)
    if fp is None or len(fp.pin_offsets) < 2:
        return None
    pts = list(fp.pin_offsets.values())
    return min(
        (math.hypot(pts[i][0] - pts[j][0], pts[i][1] - pts[j][1])
         for i in range(len(pts)) for j in range(i + 1, len(pts))),
        default=None)


def _build_escape_halos(
    nets: list[NetInfo],
    packages: dict[str, tuple[str, int]],
    footprints: dict[str, tuple[float, float]],
    config: SAConfig,
) -> dict[str, float]:
    """Per-component escape-halo radius (mm), only for parts that need one.

    A part qualifies when it has many pins, is fine-pitch, or is explicitly
    listed in config.focus_components (the routing-feedback lever). Returns
    {} for ordinary boards so the cost term is a pure no-op there.
    """
    # Fanout demand = number of distinct nets leaving the part to OTHER parts.
    leaving: dict[str, int] = {}
    for net in nets:
        comps = set(net.designators)
        if len(comps) < 2:
            continue  # net internal to one part needs no escape channel
        for d in comps:
            leaving[d] = leaving.get(d, 0) + 1

    focus = set(config.focus_components)
    track = max(config.escape_track_pitch_mm, 0.1)
    specs: dict[str, float] = {}
    for des, (pkg, pins) in packages.items():
        pitch = _footprint_min_pitch(pkg, pins)
        is_focus = des in focus
        dense = (pins >= ESCAPE_PIN_THRESHOLD
                 or (pitch is not None and pitch < ESCAPE_PITCH_THRESHOLD_MM))
        if not (dense or is_focus):
            continue
        demand = max(pins, leaving.get(des, 0))
        w, h = footprints.get(des, (2.0, 2.0))
        half = max(w, h) / 2.0
        ring = demand * track / (2.0 * math.pi)
        halo = half + ring
        if is_focus:
            # Localized reservation bump — replaces the old global +0.5mm
            # clearance retry with spacing applied exactly where routing failed.
            halo += max(track * 4.0, 1.0)
        specs[des] = halo
    return specs


def _escape_halo_cost(
    positions: dict[str, tuple[float, float]],
    packages: dict[str, tuple[str, int]],
    layers: dict[str, str] | None,
    escape_halos: dict[str, float],
    th_map: dict[str, bool],
) -> float:
    """Penalty for foreign pads intruding into a dense part's escape halo.

    Layer-aware: a foreign part only contends for the escape channel if it
    shares a routing layer (same side, or either part is through-hole). The
    penalty scales with intrusion depth and the foreign part's pin count
    (more pads = more copper blocking the channel), normalized by halo radius
    so parts of different demand contribute on a comparable scale.
    """
    cost = 0.0
    for hd, halo_r in escape_halos.items():
        hp = positions.get(hd)
        if hp is None or halo_r <= 0:
            continue
        hx, hy = hp
        h_th = th_map.get(hd, False)
        h_layer = layers.get(hd, "top") if layers else "top"
        for des, (x, y) in positions.items():
            if des == hd:
                continue
            f_th = th_map.get(des, False)
            f_layer = layers.get(des, "top") if layers else "top"
            if not (h_th or f_th or h_layer == f_layer):
                continue
            dist = math.hypot(x - hx, y - hy)
            if dist < halo_r:
                foreign_pins = packages.get(des, ("", 2))[1]
                cost += (halo_r - dist) / halo_r * foreign_pins
    return cost


def _quality_cost(
    positions: dict[str, tuple[float, float]],
    config: SAConfig,
    decoupling: list[DecouplingAssociation],
    crystals: list[CrystalAssociation],
    grouping_pairs: list[tuple[str, str]],
    packages: dict[str, tuple[str, int]] | None = None,
    layers: dict[str, str] | None = None,
    escape_halos: dict[str, float] | None = None,
    th_map: dict[str, bool] | None = None,
    signal_nets: list[NetInfo] | None = None,
) -> float:
    """Weighted sum of the placement-quality terms (proximity/crystal/grouping).

    These operate on small fixed association lists, so they stay full-recompute
    even in the SA hot loop — the expensive wire/crossing terms are handled
    incrementally by IncrementalCost.
    """
    cost = 0.0
    if decoupling and config.proximity_weight > 0:
        cost += config.proximity_weight * _proximity_cost(positions, decoupling)
    if crystals and config.crystal_weight > 0:
        cost += config.crystal_weight * _crystal_cost(positions, crystals)
    if grouping_pairs and config.grouping_weight > 0:
        cost += config.grouping_weight * _grouping_cost(positions, grouping_pairs)
    if packages and config.congestion_weight > 0:
        cost += config.congestion_weight * _congestion_cost(positions, packages,
                                                            layers=layers)
    if escape_halos and config.escape_weight > 0 and packages:
        cost += config.escape_weight * _escape_halo_cost(
            positions, packages, layers, escape_halos, th_map or {})
    if signal_nets and config.demand_weight > 0:
        cost += config.demand_weight * _routing_demand_cost(
            positions, signal_nets, config.escape_track_pitch_mm)
    if layers and config.two_sided and config.bottom_penalty > 0:
        cost += config.bottom_penalty * sum(
            1 for l in layers.values() if l == "bottom")
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
        if _is_keepout_package(pkg):
            pinned.add(des)

    movable = [d for d in positions if d not in pinned]

    if len(movable) == 0:
        # Nothing to optimize
        return copy.deepcopy(placement)

    # Build connectivity from netlist
    nets = build_connectivity(netlist)

    # Pin-level connectivity + pad offsets for the rotation heuristic. Built
    # once; the SA loop only does arithmetic on them.
    pin_nets = _build_pin_nets(netlist)
    pin_offsets = _build_pin_offsets(packages)

    # Build placement quality associations
    decoupling = find_decoupling_associations(netlist)
    crystal_assocs = find_crystal_associations(netlist)
    declared_groups = _read_functional_groups(netlist)
    grouping_pairs = _build_grouping_pairs(nets, declared_groups)

    if decoupling:
        logger.info(f"  Decoupling associations: {len(decoupling)} (cap→IC pairs)")
    if crystal_assocs:
        logger.info(f"  Crystal associations: {len(crystal_assocs)} (crystal→IC pairs)")
    if grouping_pairs:
        if declared_groups:
            n_labels = len(set(declared_groups.values()))
            src = (f"shared-net floor + {n_labels} declared functional "
                   f"group(s) over {len(declared_groups)} components")
        else:
            src = "components sharing 2+ nets"
        logger.info(f"  Grouping pairs: {len(grouping_pairs)} ({src})")

    # Flip-eligible components for two-sided placement: small non-polarized
    # SMD passives only. Connectors, ICs, TH parts, keepouts, and anything
    # pinned stay on their side; LEDs stay visible on top.
    flip_eligible: set[str] = set()
    if config.two_sided:
        from .pad_geometry import get_footprint_def, is_through_hole_package
        for item in items:
            des = item["designator"]
            if des in pinned:
                continue
            if item.get("component_type") not in ("resistor", "capacitor",
                                                  "diode"):
                continue
            pkg, pin_count = packages.get(des, ("", 2))
            # Guard against mis-typed high-pin / fine-pitch parts: a true
            # flip-eligible passive has ≤3 pins and a coarse pitch. A 30-pin
            # 0.5mm FPC mis-labelled "capacitor" must never be sent to the
            # bottom (real case: morgan CN1). Belt-and-braces with the type gate.
            if pin_count > 3:
                continue
            pitch = _footprint_min_pitch(pkg, pin_count)
            if pitch is not None and pitch < ESCAPE_PITCH_THRESHOLD_MM:
                continue
            fp = get_footprint_def(pkg, pin_count)
            if is_through_hole_package(pkg, fp):
                continue
            flip_eligible.add(des)
        if flip_eligible:
            logger.info(f"  Two-sided: {len(flip_eligible)} flip-eligible passives")

    # Escape halos (enhancement A): which parts get a reserved fanout channel,
    # and how big. Empty on ordinary boards → the escape term is a no-op. The
    # through-hole map is precomputed once so the layer-conflict test in the SA
    # hot loop stays a dict lookup rather than a footprint resolution.
    escape_halos: dict[str, float] = {}
    th_map: dict[str, bool] = {}
    if config.escape_weight > 0:
        from .pad_geometry import get_footprint_def, is_through_hole_package
        for des, (pkg, pc) in packages.items():
            th_map[des] = is_through_hole_package(pkg, get_footprint_def(pkg, pc))
        escape_halos = _build_escape_halos(nets, packages, footprints, config)
        if escape_halos:
            logger.info(f"  Escape halos: {len(escape_halos)} dense/fine-pitch "
                        f"part(s) reserve a fanout channel"
                        + (f" ({len(config.focus_components)} routing-focused)"
                           if config.focus_components else ""))

    # Routing-demand heatmap (enhancement B): signal nets only — plane
    # (power/ground) nets are delivered by copper pours, not channels.
    signal_nets = ([n for n in nets if n.net_class not in _PLANE_NET_CLASSES]
                   if config.demand_weight > 0 else [])

    # Compute iteration count
    iterations = _compute_iterations(len(movable), config.max_iterations)

    # Initial cost — incremental evaluator caches MST/crossing state so the SA
    # loop can update only the nets touched by each move.  Crossings exclude
    # plane (power/ground) nets, so metrics are sourced from the evaluator
    # rather than the full compute_cost to stay consistent with what SA sees.
    evaluator = IncrementalCost(nets, positions)
    initial_wire = evaluator.total_wire
    initial_cross = evaluator.total_cross
    initial_cost = (
        config.wire_weight * initial_wire
        + config.crossing_weight * initial_cross
        + _quality_cost(positions, config, decoupling, crystal_assocs, grouping_pairs, packages,
                        layers=layers, escape_halos=escape_halos, th_map=th_map,
                        signal_nets=signal_nets)
    )

    # SA state
    current_pos = dict(positions)
    current_rot = dict(rotations)
    current_layers = dict(layers)
    current_cost = initial_cost

    best_pos = dict(current_pos)
    best_rot = dict(current_rot)
    best_layers = dict(current_layers)
    best_cost = current_cost

    T = config.initial_temperature
    since_improvement = 0
    invalid_streak = 0
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
        # Generate a move: occasionally flip an eligible passive to the other
        # side (two-sided mode); otherwise translate/swap/rotate as usual.
        new_layers = current_layers
        if flip_eligible and rng.random() < 0.15:
            new_pos, new_rot = dict(current_pos), dict(current_rot)
            flip_des = rng.choice(sorted(flip_eligible))
            new_layers = dict(current_layers)
            new_layers[flip_des] = ("bottom"
                                    if current_layers[flip_des] == "top"
                                    else "top")
        else:
            new_pos, new_rot = _generate_move(
                current_pos, current_rot, movable, swappable_groups,
                footprints, current_layers, board_w, board_h, T,
                config.initial_temperature, rng,
                nets=nets, packages=packages,
                pin_nets=pin_nets, pin_offsets=pin_offsets,
            )

        # Fast constraint check. A rejected-as-invalid proposal is a move that
        # never happened — it says nothing about whether the search has stopped
        # improving, so it must NOT feed `since_improvement`. It used to, which
        # made the early-stop fire soonest on exactly the boards that need the
        # most search: on a dense board over half of all proposals are invalid,
        # so the optimizer quit at ~76% of its iteration budget while still
        # parked on a constraint violation. Invalid streaks get their own, much
        # larger cap so a genuinely stuck search still exits.
        if not _is_valid(new_pos, new_rot, footprints, new_layers, board_w, board_h, packages,
                         clearance=config.min_clearance_mm):
            invalid_streak += 1
            if invalid_streak >= _INVALID_STREAK_LIMIT:
                break
            continue
        invalid_streak = 0

        # Incremental cost: only nets touching moved components are recomputed.
        changed = [d for d in movable if new_pos[d] != current_pos[d]]
        ev_wire, ev_cross = evaluator.evaluate(new_pos, changed)
        new_cost = (
            config.wire_weight * ev_wire
            + config.crossing_weight * ev_cross
            + _quality_cost(new_pos, config, decoupling, crystal_assocs, grouping_pairs, packages,
                            layers=new_layers, escape_halos=escape_halos, th_map=th_map,
                            signal_nets=signal_nets)
        )

        # Accept or reject
        delta = new_cost - current_cost
        if delta < 0 or rng.random() < math.exp(-delta / max(T, 1e-10)):
            evaluator.commit()
            current_pos = new_pos
            current_rot = new_rot
            current_layers = new_layers
            current_cost = new_cost
            accepted += 1

            if current_cost < best_cost:
                best_pos = dict(current_pos)
                best_rot = dict(current_rot)
                best_layers = dict(current_layers)
                best_cost = current_cost
                since_improvement = 0
            else:
                since_improvement += 1
        else:
            evaluator.revert()
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
        old_layer = layers[des]
        new_p = best_pos[des]
        new_r = best_rot[des]
        new_l = best_layers.get(des, old_layer)
        item["x_mm"] = round(new_p[0], 2)
        item["y_mm"] = round(new_p[1], 2)
        item["rotation_deg"] = new_r
        item["layer"] = new_l
        # Mark as optimizer-placed if position, rotation, or side changed
        if old_pos != new_p or old_rot != new_r or old_layer != new_l:
            item["placement_source"] = "optimizer"

    # Compute final metrics for logging (signal-net crossings, plane-aware)
    final_eval = IncrementalCost(nets, best_pos)
    improvement = (1 - best_cost / initial_cost) * 100 if initial_cost > 0 else 0

    logger.info(f"  SA Optimizer: {iteration + 1} iterations, {accepted} accepted moves")
    logger.info(f"  Wire length : {initial_wire:.1f}mm → {final_eval.total_wire:.1f}mm")
    logger.info(f"  Crossings   : {initial_cross} → {final_eval.total_cross} (signal nets)")
    if decoupling:
        init_prox = _proximity_cost(positions, decoupling)
        final_prox = _proximity_cost(best_pos, decoupling)
        logger.info(f"  Decoupling  : proximity cost {init_prox:.1f} → {final_prox:.1f}")
    if crystal_assocs:
        init_xtal = _crystal_cost(positions, crystal_assocs)
        final_xtal = _crystal_cost(best_pos, crystal_assocs)
        logger.info(f"  Crystal     : proximity cost {init_xtal:.1f} → {final_xtal:.1f}")
    logger.info(f"  Improvement : {improvement:.1f}%")

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
    packages: dict[str, tuple[str, int]] | None = None,
    pin_nets: dict[str, list[tuple[int, list[str]]]] | None = None,
    pin_offsets: dict[tuple[str, int], tuple[float, float]] | None = None,
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
        # Clamp so the pad-extent box lands inside the edge clearance — the same
        # box and margin the validator scores. Clamping on the body box with no
        # margin (the old behaviour) made the reachable region a superset of the
        # legal one: SA burned proposals on positions that could never be
        # accepted, and since the stagnation counter also ticks on rejected
        # moves, dense boards quit early parked on a boundary violation.
        w, h = footprints[des]
        pkg, pin_count = (packages or {}).get(des, ("", 2))
        nx, ny = _clamp_centre(ox + dx, oy + dy, w, h, new_rot[des],
                               board_w, board_h, pkg, pin_count)
        new_pos[des] = (round(nx, 2), round(ny, 2))

    elif r < 0.85 and swappable_groups:
        # SWAP: exchange positions of two same-package components
        group = rng.choice(swappable_groups)
        a, b = rng.sample(group, 2)
        new_pos[a], new_pos[b] = new_pos[b], new_pos[a]

    else:
        # SMART ROTATE: 50% evaluate best rotation, 50% random.
        #
        # The evaluation scores candidate orientations by where this part's PADS
        # land relative to what they connect to. It used to call
        # total_wire_length, which is centre-to-centre — rotation is not even an
        # input to it, so all four candidates scored identically and the strict
        # `<` comparison always kept the first, snapping every connected part to
        # 0° and burning four MST computations to do it.
        des = rng.choice(movable)
        all_options = [0, 90, 180, 270]
        current = new_rot[des]

        if rng.random() < 0.5 and pin_nets and pin_nets.get(des):
            # Current rotation first, so an exact tie (a symmetric part, or one
            # whose pads are all equidistant) leaves the orientation alone
            # instead of drifting.
            ordered = [current] + [rv for rv in all_options if rv != current]
            new_rot[des] = min(ordered, key=lambda rv: _rotation_pin_cost(
                des, rv, new_pos, pin_nets, pin_offsets or {}))
        else:
            # Random rotation (avoid current)
            options = [rv for rv in all_options if rv != current]
            new_rot[des] = rng.choice(options)

    return new_pos, new_rot


def _legalize_pack(
    positions: dict[str, tuple[float, float]],
    rotations: dict[str, int],
    footprints: dict[str, tuple[float, float]],
    layers: dict[str, str],
    movable: list[str],
    board_w: float,
    board_h: float,
    packages: dict[str, tuple[str, int]] | None = None,
    clearance: float = MIN_CLEARANCE_MM,
) -> dict[str, tuple[float, float]] | None:
    """Deterministically pack `movable` into a non-overlapping layout.

    Annealing is a *global* placer: it finds roughly where things belong, but
    single-component random moves can never compact a board, because tightening
    a dense layout needs several parts to shift together. Past a certain density
    it stalls in a local minimum no amount of iterations escapes (measured: 10k
    and 200k iterations leave esp8266_breakout on the same violation). The
    standard fix is to stop asking the anneal for a legal answer and construct
    one — global placement, then legalization.

    Bottom-left-fill against corner candidates: each part goes at the lowest,
    then leftmost, spot where it fits, considering the board corner and the
    edges of everything already down. Simple shelf packing was tried first and
    is too wasteful — every row costs the height of its tallest member, which on
    a board needing 81% area utilization is the difference between fitting and
    not.

    Two sweep orders are attempted: the order the anneal left things (preserves
    its arrangement, so wire length survives) and tallest-first (packs denser).
    Immovable parts are obstacles. Returns None if nothing fits, leaving the
    caller its annealed best rather than a worse legal-but-scrambled layout.
    """
    ec = BOARD_EDGE_CLEARANCE_MM
    movable_set = set(movable)
    # Pack with a hair more than the required clearance. Output centres are
    # rounded to 0.01mm, so two boxes placed at exactly `clearance` can end up
    # a rounding step closer and fail the (zero-tolerance) overlap check.
    pack_gap = clearance + 0.02

    def _extent(des: str, rot: int) -> tuple[float, float, float, float]:
        """Pad-extent box measured from the part's centre."""
        w, h = footprints[des]
        pkg, pc = (packages or {}).get(des, ("", 2))
        if pkg:
            return _get_pad_extent_box(0.0, 0.0, w, h, rot, pkg, pc)
        return _get_bounding_box(0.0, 0.0, w, h, rot)

    def _layer_of(des: str) -> str:
        pkg, pc = (packages or {}).get(des, ("", 2))
        return _effective_layer(pkg, pc, layers.get(des, "top"))

    obstacles: list[tuple[tuple[float, float, float, float], str]] = []
    for des, (x, y) in positions.items():
        if des in movable_set:
            continue
        bx = _extent(des, rotations.get(des, 0))
        obstacles.append(((x + bx[0], y + bx[1], x + bx[2], y + bx[3]),
                          _layer_of(des)))

    # Each part may also be turned a quarter turn — on a tight board that is
    # often the difference between fitting and not, and repair is free to rotate
    # anyway. Both orientations of the current angle are offered; the packer
    # takes whichever seats the part lower.
    def _options(des: str) -> list[tuple[int, tuple[float, float, float, float]]]:
        rot = rotations.get(des, 0)
        opts = [(rot, _extent(des, rot))]
        alt = (rot + 90) % 360
        alt_bx = _extent(des, alt)
        if (round(alt_bx[2] - alt_bx[0], 3),
                round(alt_bx[3] - alt_bx[1], 3)) != (
                round(opts[0][1][2] - opts[0][1][0], 3),
                round(opts[0][1][3] - opts[0][1][1], 3)):
            opts.append((alt, alt_bx))
        return opts

    base = {d: _extent(d, rotations.get(d, 0)) for d in movable}
    heights = {d: base[d][3] - base[d][1] for d in movable}
    widths = {d: base[d][2] - base[d][0] for d in movable}

    anneal_order = sorted(movable, key=lambda d: (round(positions[d][1], 1),
                                                  positions[d][0], d))
    tallest_first = sorted(movable, key=lambda d: (-heights[d], -widths[d], d))

    for order in (anneal_order, tallest_first):
        out: dict[str, tuple[float, float, int]] = {}
        placed: list[tuple[tuple[float, float, float, float], str]] = list(obstacles)
        ok = True
        for des in order:
            layer = _layer_of(des)
            best: tuple[float, float, tuple, int, tuple] | None = None
            for rot, bx in _options(des):
                w_i, h_i = bx[2] - bx[0], bx[3] - bx[1]
                # Candidate corners: the board's bottom-left, plus the right and
                # top edges of everything already placed. An optimal
                # bottom-left-fill position is always at one of these.
                xs = [ec] + [b[2] + pack_gap for b, _ in placed]
                ys = [ec] + [b[3] + pack_gap for b, _ in placed]
                xs = sorted({round(v, 3) for v in xs
                             if ec <= v <= board_w - ec - w_i})
                ys = sorted({round(v, 3) for v in ys
                             if ec <= v <= board_h - ec - h_i})
                for y in ys:                   # lowest first, then leftmost
                    if best is not None and y > best[1]:
                        break                  # cannot beat the incumbent
                    hit = False
                    for x in xs:
                        cand = (x, y, x + w_i, y + h_i)
                        if any(_layers_conflict(layer, bl)
                               and _boxes_overlap_with_clearance(cand, b, pack_gap)
                               for b, bl in placed):
                            continue
                        if best is None or (y, x) < (best[1], best[0]):
                            best = (x, y, cand, rot, bx)
                        hit = True
                        break
                    if hit:
                        break
            if best is None:
                ok = False
                break
            x, y, cand, rot, bx = best
            out[des] = (round(x - bx[0], 2), round(y - bx[1], 2), rot)
            placed.append((cand, layer))
        if ok:
            return out

    return None


def _is_valid(
    positions: dict[str, tuple[float, float]],
    rotations: dict[str, int],
    footprints: dict[str, tuple[float, float]],
    layers: dict[str, str],
    board_w: float,
    board_h: float,
    packages: dict[str, tuple[str, int]] | None = None,
    clearance: float = MIN_CLEARANCE_MM,
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

        pkg, pc = (packages or {}).get(des, ("", 2))
        boxes.append((des, box,
                      _effective_layer(pkg, pc, layers.get(des, "top"))))

    # Pairwise overlap check (through-hole parts conflict on both sides)
    for i in range(len(boxes)):
        for j in range(i + 1, len(boxes)):
            if not _layers_conflict(boxes[i][2], boxes[j][2]):
                continue
            if _boxes_overlap_with_clearance(boxes[i][1], boxes[j][1], clearance):
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
    clearance: float = MIN_CLEARANCE_MM,
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
            pkg, pin_count = "", 2
            box = _get_bounding_box(x, y, w, h, rot)
        boxes.append((des, box,
                      _effective_layer(pkg, pin_count, layers.get(des, "top"))))

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

    # Pairwise overlap check (through-hole parts conflict on both sides)
    for i in range(len(boxes)):
        for j in range(i + 1, len(boxes)):
            if not _layers_conflict(boxes[i][2], boxes[j][2]):
                continue
            b1, b2 = boxes[i][1], boxes[j][1]
            # Check if they overlap (with clearance)
            if _boxes_overlap_with_clearance(b1, b2, clearance):
                violations += 1
                # Compute overlap depth (how much they need to move apart)
                ox = min(b1[2] + clearance - b2[0], b2[2] + clearance - b1[0])
                oy = min(b1[3] + clearance - b2[1], b2[3] + clearance - b1[1])
                if ox > 0 and oy > 0:
                    overlap_depth += min(ox, oy)

    return violations, overlap_depth


def find_placement_violations(
    placement: dict,
    netlist: dict | None = None,
    clearance: float = MIN_CLEARANCE_MM,
) -> dict:
    """Structured constraint report for a placement (pad-extent aware).

    Returns {"out_of_bounds": [...], "overlaps": [...], "count": int}.
    Each out_of_bounds entry: {designator, pinned, hard_pinned, detail}.
    Each overlap entry: {a, b, layer, pinned (both pinned → unfixable
    automatically), hard_pinned, detail}.

    `pinned` means `optimize_placement` will not move it (user-placed, a keepout
    package, or a PINNED_TYPES component like a connector). `hard_pinned` is the
    stricter set that `repair_placement` also refuses to move — user-placed and
    keepout packages only. The distinction matters to callers deciding whether to
    retry on a new seed: repair DOES relocate connectors, so a connector left
    overlapping is a search failure a different seed can fix, whereas a
    user-placed part in the way will never move on its own.
    """
    board = placement.get("board", {})
    board_w = board.get("width_mm", 0.0)
    board_h = board.get("height_mm", 0.0)
    items = placement.get("placements", [])

    # Pin counts per designator for pad-extent boxes
    comp_pin_counts: dict[str, int] = {}
    des_to_comp_id: dict[str, str] = {}
    if netlist:
        for elem in netlist.get("elements", []):
            if elem.get("element_type") == "port":
                cid = elem.get("component_id", "")
                comp_pin_counts[cid] = comp_pin_counts.get(cid, 0) + 1
            elif elem.get("element_type") == "component":
                des_to_comp_id[elem.get("designator", "")] = elem["component_id"]

    boxes: list[tuple[str, tuple, str, bool, bool]] = []  # +hard_pinned
    out_of_bounds: list[dict] = []
    ec = BOARD_EDGE_CLEARANCE_MM
    for item in items:
        des = item["designator"]
        x, y = item["x_mm"], item["y_mm"]
        fw, fh = item["footprint_width_mm"], item["footprint_height_mm"]
        rot = item.get("rotation_deg", 0)
        pkg = item.get("package", "")
        hard_pinned = (item.get("placement_source") == "user"
                       or _is_keepout_package(pkg))
        pinned = hard_pinned or item.get("component_type") in PINNED_TYPES
        if pkg:
            pin_count = comp_pin_counts.get(des_to_comp_id.get(des, ""), 2)
            box = _get_pad_extent_box(x, y, fw, fh, rot, pkg, pin_count)
        else:
            box = _get_bounding_box(x, y, fw, fh, rot)
        eff_layer = _effective_layer(pkg, pin_count if pkg else 2,
                                     item.get("layer", "top"))
        boxes.append((des, box, eff_layer, pinned, hard_pinned))

        parts = []
        if box[0] < ec - 0.01:
            parts.append(f"{ec - box[0]:.2f}mm past the left edge margin")
        if box[1] < ec - 0.01:
            parts.append(f"{ec - box[1]:.2f}mm past the top edge margin")
        if box[2] > board_w - ec + 0.01:
            parts.append(f"{box[2] - (board_w - ec):.2f}mm past the right edge margin")
        if box[3] > board_h - ec + 0.01:
            parts.append(f"{box[3] - (board_h - ec):.2f}mm past the bottom edge margin")
        if parts:
            out_of_bounds.append({
                "designator": des, "pinned": pinned, "hard_pinned": hard_pinned,
                "detail": f"{des} pads extend {', '.join(parts)} "
                          f"(board {board_w}x{board_h}mm, edge clearance {ec}mm)",
            })

    overlaps: list[dict] = []
    for i in range(len(boxes)):
        for j in range(i + 1, len(boxes)):
            des_a, box_a, layer_a, pin_a, hard_a = boxes[i]
            des_b, box_b, layer_b, pin_b, hard_b = boxes[j]
            if not _layers_conflict(layer_a, layer_b):
                continue
            if _boxes_overlap_with_clearance(box_a, box_b, clearance):
                overlaps.append({
                    "a": des_a, "b": des_b, "layer": layer_a,
                    "pinned": pin_a and pin_b,
                    "hard_pinned": hard_a and hard_b,
                    "detail": f"{des_a} and {des_b} overlap (or are closer "
                              f"than {clearance}mm) on {layer_a}"
                              + (" — both are pinned; move one of them"
                                 if pin_a and pin_b else ""),
                })

    return {"out_of_bounds": out_of_bounds, "overlaps": overlaps,
            "count": len(out_of_bounds) + len(overlaps)}


def placement_density(
    placement: dict,
    netlist: dict | None = None,
    clearance: float = MIN_CLEARANCE_MM,
) -> dict:
    """How full is this board, and what would make it placeable?

    "Too dense, enlarge it" is not an answer anyone can act on. This turns a
    failed placement into a number and a concrete remedy: rectangle packing with
    a mandatory gap around every part tops out well below 100%, so a board past
    roughly 85% demand is not a search failure to be retried, it needs another
    side or more room.

    Returns {utilization_pct, bare_pct, usable_mm2, demand_mm2,
             suggested_width_mm, suggested_height_mm, verdict}.
    """
    board = placement.get("board", {})
    bw = board.get("width_mm", 0.0)
    bh = board.get("height_mm", 0.0)

    comp_pin_counts: dict[str, int] = {}
    des_to_comp_id: dict[str, str] = {}
    for elem in (netlist or {}).get("elements", []):
        if elem.get("element_type") == "port":
            cid = elem.get("component_id", "")
            comp_pin_counts[cid] = comp_pin_counts.get(cid, 0) + 1
        elif elem.get("element_type") == "component":
            des_to_comp_id[elem.get("designator", "")] = elem["component_id"]

    bare = demand = 0.0
    for item in placement.get("placements", []):
        w = item.get("footprint_width_mm", 0.0)
        h = item.get("footprint_height_mm", 0.0)
        rot = item.get("rotation_deg", 0)
        pkg = item.get("package", "")
        if pkg:
            pc = comp_pin_counts.get(
                des_to_comp_id.get(item.get("designator", ""), ""), 2)
            bx = _get_pad_extent_box(0.0, 0.0, w, h, rot, pkg, pc)
        else:
            bx = _get_bounding_box(0.0, 0.0, w, h, rot)
        pw, ph = bx[2] - bx[0], bx[3] - bx[1]
        bare += pw * ph
        demand += (pw + clearance) * (ph + clearance)

    ec = BOARD_EDGE_CLEARANCE_MM
    usable = max((bw - 2 * ec) * (bh - 2 * ec), 1e-6)
    util = 100.0 * demand / usable

    # Scale the board until demand sits at a comfortable packing density,
    # keeping the aspect ratio.
    grow = max(1.0, math.sqrt(demand / (_PACKABLE_UTILIZATION * usable)))
    if util > 100:
        verdict = "impossible"
    elif util > _PACKABLE_UTILIZATION * 100:
        verdict = "impractical"
    else:
        verdict = "packable"
    return {
        "utilization_pct": round(util, 1),
        "bare_pct": round(100.0 * bare / usable, 1),
        "usable_mm2": round(usable, 1),
        "demand_mm2": round(demand, 1),
        "suggested_width_mm": round(bw * grow, 1),
        "suggested_height_mm": round(bh * grow, 1),
        "verdict": verdict,
    }


def repair_placement(
    placement: dict,
    netlist: dict | None = None,
    max_iterations: int = 10000,
    clearance: float = MIN_CLEARANCE_MM,
    seed: int | None = None,
    two_sided: bool = False,
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

        pkg = item.get("package", "")
        cid = des_to_comp_id.get(des, "")
        pin_count = comp_pin_counts.get(cid, 2)
        packages[des] = (pkg, pin_count)

    # Pin user-placed components (always) and keepout packages.
    #  - User pins: snap an out-of-bounds centre to the nearest valid in-bounds
    #    position first (same clamp as the movable pre-pass), then hold it —
    #    preserves the agent's intent even when the exact position is slightly
    #    out of bounds, and stops the SA loop treating it as free to move.
    #  - Keepouts (mounting holes / fiducials): position is mechanically fixed
    #    by the enclosure, so pin them where they sit when already in bounds.
    for item in items:
        des = item["designator"]
        pkg = packages.get(des, ("", 2))[0]
        is_user = item.get("placement_source") == "user"
        is_keepout = _is_keepout_package(pkg)
        if not (is_user or is_keepout):
            continue
        x, y = positions[des]
        fw, fh = footprints[des]
        pkg2, pc2 = packages.get(des, ("", 2))
        nx, ny = _clamp_centre(x, y, fw, fh, rotations[des],
                               board_w, board_h, pkg2, pc2)
        within = (nx == x and ny == y)
        if is_user:
            if not within:
                positions[des] = (round(nx, 2), round(ny, 2))
            pinned.add(des)
        elif within:  # keepout already in bounds → respect its fixed position
            pinned.add(des)

    movable = [d for d in positions if d not in pinned]
    if not movable:
        return copy.deepcopy(placement)

    # --- Deterministic pre-pass: snap out-of-bounds components onto the board ---
    # This gives SA a valid starting point instead of requiring it to make large jumps.
    snapped = 0
    for des in movable:
        x, y = positions[des]
        fw, fh = footprints[des]
        pkg2, pc2 = packages.get(des, ("", 2))
        nx, ny = _clamp_centre(x, y, fw, fh, rotations[des],
                               board_w, board_h, pkg2, pc2)
        if nx != x or ny != y:
            positions[des] = (round(nx, 2), round(ny, 2))
            snapped += 1
        # If component is taller than 90% of the board, rotate 90° and snap near an edge.
        # After rotation it becomes wide+short, so place it near top or bottom edge.
        rot = rotations[des]
        eh = fw if rot in (90, 270) else fh
        ew = fh if rot in (90, 270) else fw
        if eh > board_h * 0.9 and ew <= board_w * 0.9 and rot in (0, 180):
            rotations[des] = 90
            # Place near bottom or top edge — whichever is further from existing snapped pos
            cy = positions[des][1]
            nx2, ny2 = _clamp_centre(
                positions[des][0], 0.0 if cy < board_h / 2 else board_h,
                fw, fh, 90, board_w, board_h, pkg2, pc2)
            positions[des] = (round(nx2, 2), round(ny2, 2))
            snapped += 1
    if snapped:
        logger.info(f"  Repair pre-pass: snapped {snapped} out-of-bounds component(s) onto board")

    # Build connectivity if netlist provided
    nets = []
    pin_nets: dict[str, list[tuple[int, list[str]]]] = {}
    pin_offsets: dict[tuple[str, int], tuple[float, float]] = {}
    if netlist:
        from .ratsnest import build_connectivity, total_wire_length
        nets = build_connectivity(netlist)
        pin_nets = _build_pin_nets(netlist)
        pin_offsets = _build_pin_offsets(packages)

    # Repair cost: violations dominate, wire length is secondary
    VIOLATION_WEIGHT = 1000.0
    WIRE_WEIGHT = 0.1

    def cost(pos, rot, lyrs):
        v_count, v_depth = _count_violations(pos, rot, footprints, lyrs, board_w, board_h, packages,
                                              clearance=clearance)
        violation_cost = VIOLATION_WEIGHT * (v_count * 10 + v_depth)
        wire_cost = 0.0
        if nets and v_count == 0:
            wire_cost = WIRE_WEIGHT * total_wire_length(nets, pos)
        return violation_cost + wire_cost, v_count

    # Two-sided repair: flipping a passive to the bottom directly resolves
    # same-layer overlaps — exactly what an over-full top side needs.
    flip_eligible: set[str] = set()
    if two_sided:
        from .pad_geometry import get_footprint_def, is_through_hole_package
        for item in items:
            des = item["designator"]
            if des in pinned:
                continue
            if item.get("component_type") not in ("resistor", "capacitor",
                                                  "diode"):
                continue
            pkg, pc = packages.get(des, ("", 2))
            # Same guard as optimize: never flip a mis-typed high-pin /
            # fine-pitch part (e.g. a 30-pin FPC labelled "capacitor").
            if pc > 3:
                continue
            pitch = _footprint_min_pitch(pkg, pc)
            if pitch is not None and pitch < ESCAPE_PITCH_THRESHOLD_MM:
                continue
            if is_through_hole_package(pkg, get_footprint_def(pkg, pc)):
                continue
            flip_eligible.add(des)
        if flip_eligible:
            logger.info(f"  Repair two-sided: {len(flip_eligible)} flip-eligible passives")

    current_pos = dict(positions)
    current_rot = dict(rotations)
    current_layers = dict(layers)
    current_cost, current_violations = cost(current_pos, current_rot, current_layers)

    best_pos = dict(current_pos)
    best_rot = dict(current_rot)
    best_layers = dict(current_layers)
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
    max_stagnation = 3000

    for iteration in range(max_iterations):
        new_layers = current_layers
        if flip_eligible and rng.random() < 0.2:
            new_pos, new_rot = dict(current_pos), dict(current_rot)
            flip_des = rng.choice(sorted(flip_eligible))
            new_layers = dict(current_layers)
            new_layers[flip_des] = ("bottom"
                                    if current_layers[flip_des] == "top"
                                    else "top")
        else:
            new_pos, new_rot = _generate_move(
                current_pos, current_rot, movable, swappable_groups,
                footprints, current_layers, board_w, board_h, T, 200.0, rng,
                packages=packages,
                pin_nets=pin_nets, pin_offsets=pin_offsets,
            )

        # In repair mode, we DON'T reject invalid moves — we score them
        new_cost, new_violations = cost(new_pos, new_rot, new_layers)

        delta = new_cost - current_cost
        if delta < 0 or rng.random() < math.exp(-delta / max(T, 1e-10)):
            current_pos = new_pos
            current_rot = new_rot
            current_layers = new_layers
            current_cost = new_cost
            current_violations = new_violations

            if current_cost < best_cost:
                best_pos = dict(current_pos)
                best_rot = dict(current_rot)
                best_layers = dict(current_layers)
                best_cost = current_cost
                best_violations = current_violations
                stagnation = 0
            else:
                stagnation += 1
        else:
            stagnation += 1

        # Early exit if we've resolved all violations and stagnated
        if best_violations == 0 and stagnation >= 1500:
            break
        if stagnation >= max_stagnation:
            break

        T *= cooling
        if T < 0.01:
            T = 0.01

    # Legalization: the anneal is a global placer and cannot compact a dense
    # board on its own. If it finished dirty, construct a legal layout from the
    # arrangement it reached and keep whichever is better.
    def _relegalize() -> None:
        """Re-run the constructive packer against the current best state."""
        nonlocal best_pos, best_rot, best_violations
        legal = _legalize_pack(best_pos, best_rot, footprints, best_layers,
                               movable, board_w, board_h, packages,
                               clearance=clearance)
        if legal:
            cand_pos = dict(best_pos)
            cand_rot = dict(best_rot)
            for des, (lx, ly, lrot) in legal.items():
                cand_pos[des] = (lx, ly)
                cand_rot[des] = lrot
            v_legal, _ = _count_violations(cand_pos, cand_rot, footprints,
                                           best_layers, board_w, board_h,
                                           packages, clearance=clearance)
            if v_legal < best_violations:
                logger.info(f"  Repair legalizer: {best_violations} → {v_legal} "
                            f"violations (packed {len(legal)} components)")
                best_pos = cand_pos
                best_rot = cand_rot
                best_violations = v_legal

    if best_violations > 0:
        _relegalize()

    # Un-flip pass. Repair's cost counts violations, not sides, so flipping is
    # free to it and it will leave a part on the bottom that only needed to be
    # there transiently. On a 2-layer board the bottom IS the router's escape
    # layer — every part left there costs routing capacity — so bring back
    # everything that fits on top, cheapest-to-return first. Only flips that
    # actually earn their place survive.
    if flip_eligible and best_violations == 0:
        returned = 0
        for des in sorted(d for d in flip_eligible if best_layers.get(d) == "bottom"):
            trial = dict(best_layers)
            trial[des] = "top"
            if _is_valid(best_pos, best_rot, footprints, trial,
                         board_w, board_h, packages, clearance=clearance):
                best_layers = trial
                returned += 1
        if returned:
            logger.info(f"  Repair un-flip: returned {returned} component(s) to "
                        f"the top side that did not need to be on the bottom")

    # Build output
    result = copy.deepcopy(placement)
    for item in result["placements"]:
        des = item["designator"]
        if des in pinned:
            # Write back any boundary-snapping we applied to this component's
            # centre position (best_pos[des] holds the snapped value; for
            # already-in-bounds items it equals the original and this is a
            # no-op).  placement_source is intentionally left unchanged so
            # "user" pins stay "user" after a boundary snap.
            item["x_mm"] = round(best_pos[des][0], 2)
            item["y_mm"] = round(best_pos[des][1], 2)
            item["rotation_deg"] = best_rot[des]
            continue
        old_pos = positions[des]
        old_rot = rotations[des]
        old_layer = layers[des]
        new_p = best_pos[des]
        new_r = best_rot[des]
        new_l = best_layers.get(des, old_layer)
        item["x_mm"] = round(new_p[0], 2)
        item["y_mm"] = round(new_p[1], 2)
        item["rotation_deg"] = new_r
        item["layer"] = new_l
        if old_pos != new_p or old_rot != new_r or old_layer != new_l:
            item["placement_source"] = "optimizer"

    initial_v, _ = _count_violations(positions, rotations, footprints, layers, board_w, board_h, packages,
                                     clearance=clearance)
    logger.info(f"  Repair: {iteration + 1} iterations")
    logger.info(f"  Violations: {initial_v} → {best_violations}")
    if best_violations == 0:
        logger.info(f"  All overlaps resolved ✓")
    else:
        logger.info(f"  WARNING: {best_violations} violations remain")

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
    logger.info(f"\n  Optimized placement written to {output_path}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
