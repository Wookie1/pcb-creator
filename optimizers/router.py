"""Grid-based A* PCB router.

Generates copper traces between component pads using:
- Grid-based A* with Manhattan routing
- IPC-2221 trace width auto-calculation from copper weight + net current
- Net ordering: power/ground first, then signal shortest-first
- Layer strategy: top-first, via to bottom when blocked
- Rip-up-and-retry for congestion resolution
"""

from __future__ import annotations

import heapq
import math
import dataclasses
from dataclasses import dataclass, field

from .pad_geometry import PadInfo, build_pad_map
from .ratsnest import NetInfo, build_connectivity, compute_mst_edges

from collections import deque

from validators.engineering_constants import (
    TRACE_WIDTH_POWER_MM,
    TRACE_WIDTH_GROUND_MM,
    TRACE_WIDTH_SIGNAL_MM,
    TRACE_CLEARANCE_MM,
    VIA_DRILL_MM,
    VIA_DIAMETER_MM,
    ROUTING_GRID_MM,
    COPPER_WEIGHT_DEFAULT_OZ,
    FILL_CLEARANCE_MM,
    THERMAL_RELIEF_GAP_MM,
    THERMAL_RELIEF_SPOKE_WIDTH_MM,
    LED_IF_DEFAULT,
    parse_current,
)


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class RouterConfig:
    grid_resolution_mm: float = ROUTING_GRID_MM
    trace_width_power_mm: float = TRACE_WIDTH_POWER_MM
    trace_width_ground_mm: float = TRACE_WIDTH_GROUND_MM
    trace_width_signal_mm: float = TRACE_WIDTH_SIGNAL_MM
    clearance_mm: float = TRACE_CLEARANCE_MM
    via_drill_mm: float = VIA_DRILL_MM
    via_diameter_mm: float = VIA_DIAMETER_MM
    copper_weight_oz: float = COPPER_WEIGHT_DEFAULT_OZ
    max_rip_up_iterations: int = 8  # targeted rip-up passes with escalating aggression
    max_rip_up_depth: int = 5      # max blocking nets to rip per failed net per iteration
    via_cost: float = 50.0  # A* cost penalty for layer transitions (50 cells = 12.5mm detour equivalent)
    ordering_trials: int = 8  # number of net ordering permutations to try (reduced; NCR handles congestion)
    # Copper fill parameters
    fill_enabled: bool = True
    fill_net_name: str = "GND"  # net name to use for fill (resolved at runtime)
    fill_clearance_mm: float = FILL_CLEARANCE_MM
    thermal_gap_mm: float = THERMAL_RELIEF_GAP_MM
    thermal_spoke_width_mm: float = THERMAL_RELIEF_SPOKE_WIDTH_MM
    # Shove routing parameters
    shove_enabled: bool = True
    max_shove_depth: int = 5  # max recursive cascade depth for push-and-shove
    # Negotiated congestion routing (PathFinder) parameters
    ncr_enabled: bool = True
    ncr_max_iterations: int = 20  # max PathFinder iterations
    ncr_hfac_initial: float = 0.5  # initial history factor
    ncr_hfac_increment: float = 1.0  # history factor growth per iteration
    ncr_pfac: float = 1.0  # present congestion penalty factor
    ncr_stagnation_limit: int = 3  # iterations without improvement before boosting hfac
    ncr_via_cost_min: float = 10.0  # minimum via cost during aggressive NCR phases
    congestion_via_cost: float = 15.0  # reduced via cost for rip-up/shove of failed nets
    # Narrow trace parameters for congested signal nets
    trace_width_signal_narrow_mm: float = 0.20
    clearance_narrow_mm: float = 0.15
    # Fine grid parameters
    fine_grid_factor: int = 2  # integer subdivision factor (0.25 → 0.125mm)
    # Channel-aware routing parameters
    channel_pressure_weight: float = 3.0  # A* cost multiplier for channel pressure
    channel_fill_exponent: float = 2.0    # exponent for dynamic fill penalty ramp
    channel_ncr_seed_factor: float = 1.0  # scale for seeding NCR history with channel pressure
    # Pre-route GND fill parameters
    pre_fill_enabled: bool = True        # commit GND fill to grid before signal routing
    pre_fill_fallback: bool = True       # retry without pre-fill if it routes fewer nets


@dataclass
class Channel:
    """A routing channel between parallel rows of pads."""
    x_min_mm: float
    y_min_mm: float
    x_max_mm: float
    y_max_mm: float
    axis: str           # "horizontal" (pads above/below) or "vertical" (pads left/right)
    capacity: int       # max traces that fit in narrowest cross-section
    width_mm: float     # gap width in mm (the narrow dimension)


@dataclass
class TraceSegment:
    start_x_mm: float
    start_y_mm: float
    end_x_mm: float
    end_y_mm: float
    width_mm: float
    layer: str
    net_id: str
    net_name: str

    def to_dict(self) -> dict:
        return {
            "start_x_mm": round(self.start_x_mm, 4),
            "start_y_mm": round(self.start_y_mm, 4),
            "end_x_mm": round(self.end_x_mm, 4),
            "end_y_mm": round(self.end_y_mm, 4),
            "width_mm": round(self.width_mm, 4),
            "layer": self.layer,
            "net_id": self.net_id,
            "net_name": self.net_name,
        }

    def length_mm(self) -> float:
        return abs(self.end_x_mm - self.start_x_mm) + abs(self.end_y_mm - self.start_y_mm)


@dataclass
class Via:
    x_mm: float
    y_mm: float
    drill_mm: float
    diameter_mm: float
    from_layer: str
    to_layer: str
    net_id: str
    net_name: str

    def to_dict(self) -> dict:
        return {
            "x_mm": round(self.x_mm, 4),
            "y_mm": round(self.y_mm, 4),
            "drill_mm": round(self.drill_mm, 4),
            "diameter_mm": round(self.diameter_mm, 4),
            "from_layer": self.from_layer,
            "to_layer": self.to_layer,
            "net_id": self.net_id,
            "net_name": self.net_name,
        }


@dataclass
class RoutingResult:
    traces: list[TraceSegment] = field(default_factory=list)
    vias: list[Via] = field(default_factory=list)
    unrouted_nets: list[str] = field(default_factory=list)
    trace_width_overrides: dict[str, float] = field(default_factory=dict)  # net_id -> width if IPC-2221 upsized


# ---------------------------------------------------------------------------
# IPC-2221 trace width calculation
# ---------------------------------------------------------------------------

def ipc2221_trace_width(current_a: float, copper_oz: float, temp_rise_c: float = 10.0) -> float:
    """Calculate minimum trace width per IPC-2221 for external layers.

    Uses: I = k * dT^0.44 * A^0.725
    where k=0.048 (external), A = cross-sectional area in mil²

    Args:
        current_a: Maximum current in amps.
        copper_oz: Copper weight in oz/ft².
        temp_rise_c: Allowable temperature rise in °C.

    Returns:
        Minimum trace width in mm.
    """
    if current_a <= 0:
        return 0.0

    # Solve for A: A = (I / (k * dT^0.44))^(1/0.725)
    k = 0.048  # external layer constant
    a_mil2 = (current_a / (k * temp_rise_c ** 0.44)) ** (1.0 / 0.725)

    # Convert area from mil² to mm², then divide by thickness to get width
    # 1 oz copper = 1.37 mil = 0.0348 mm
    thickness_mil = copper_oz * 1.37
    width_mil = a_mil2 / thickness_mil

    # Convert mil to mm
    width_mm = width_mil * 0.0254
    return width_mm


def compute_net_current(net_info: NetInfo, netlist: dict) -> float:
    """Estimate maximum current for a net based on connected components.

    Returns current in amps.
    """
    elements = netlist.get("elements", [])

    # Build lookups
    components: dict[str, dict] = {}
    ports: dict[str, dict] = {}
    for elem in elements:
        if elem.get("element_type") == "component":
            components[elem["component_id"]] = elem
        elif elem.get("element_type") == "port":
            ports[elem["port_id"]] = elem

    # Find net element to get connected ports
    net_elem = None
    for elem in elements:
        if elem.get("element_type") == "net" and elem.get("net_id") == net_info.net_id:
            net_elem = elem
            break

    if not net_elem:
        if net_info.net_class == "power":
            return 0.5
        return 0.1

    max_current = 0.0
    for pid in net_elem.get("connected_port_ids", []):
        port = ports.get(pid, {})
        comp = components.get(port.get("component_id", ""), {})
        props = comp.get("properties", {})
        ctype = comp.get("component_type", "")

        # LED forward current
        if ctype == "led":
            try:
                max_current = max(max_current, parse_current(props.get("if", props.get("forward_current", ""))))
            except (ValueError, TypeError):
                max_current = max(max_current, LED_IF_DEFAULT)

        # Voltage regulator max current
        if ctype == "voltage_regulator":
            try:
                max_current = max(max_current, parse_current(props.get("max_current", "")))
            except (ValueError, TypeError):
                pass

    # Defaults by net class if no specific current found
    if max_current <= 0:
        if net_info.net_class in ("power", "ground"):
            return 0.5  # conservative default for power nets
        return 0.1  # signal nets

    return max_current


# ---------------------------------------------------------------------------
# Routing grid
# ---------------------------------------------------------------------------

EMPTY = 0
OBSTACLE = -1
# Positive integers represent net IDs (1-indexed)


class RoutingGrid:
    """2D occupancy grid for two-layer routing (top + bottom).

    Each cell is either EMPTY, OBSTACLE, or a positive net ID.
    Grid coords: col = int(x_mm / resolution), row = int(y_mm / resolution).
    """

    def __init__(self, board_w_mm: float, board_h_mm: float, resolution_mm: float):
        self.resolution = resolution_mm
        self.board_w = board_w_mm
        self.board_h = board_h_mm
        self.cols = int(math.ceil(board_w_mm / resolution_mm)) + 1
        self.rows = int(math.ceil(board_h_mm / resolution_mm)) + 1

        # layers["top"] and layers["bottom"] are flat lists used as 2D arrays
        self.layers: dict[str, list[int]] = {
            "top": [EMPTY] * (self.cols * self.rows),
            "bottom": [EMPTY] * (self.cols * self.rows),
        }

        # Via exclusion zone — cells where layer transitions are forbidden
        # (e.g., near through-hole pads to prevent via-pad shorts)
        self.no_via: list[bool] = [False] * (self.cols * self.rows)

    def _idx(self, col: int, row: int) -> int:
        return row * self.cols + col

    def _in_bounds(self, col: int, row: int) -> bool:
        return 0 <= col < self.cols and 0 <= row < self.rows

    def get(self, col: int, row: int, layer: str) -> int:
        if not self._in_bounds(col, row):
            return OBSTACLE
        return self.layers[layer][self._idx(col, row)]

    def set(self, col: int, row: int, layer: str, value: int) -> None:
        if self._in_bounds(col, row):
            self.layers[layer][self._idx(col, row)] = value

    def mm_to_grid(self, x_mm: float, y_mm: float) -> tuple[int, int]:
        """Convert mm coordinates to grid (col, row)."""
        col = int(round(x_mm / self.resolution))
        row = int(round(y_mm / self.resolution))
        return col, row

    def grid_to_mm(self, col: int, row: int) -> tuple[float, float]:
        """Convert grid (col, row) to mm coordinates."""
        return col * self.resolution, row * self.resolution

    def is_available(self, col: int, row: int, layer: str, net_id: int) -> bool:
        """Check if cell is usable: empty or same net."""
        val = self.get(col, row, layer)
        return val == EMPTY or val == net_id

    def mark_rect(
        self, x_min: float, y_min: float, x_max: float, y_max: float,
        layer: str, value: int,
    ) -> None:
        """Fill a rectangular region on the grid."""
        c_min, r_min = self.mm_to_grid(x_min, y_min)
        c_max, r_max = self.mm_to_grid(x_max, y_max)
        # Clamp to grid bounds
        c_min = max(0, c_min)
        r_min = max(0, r_min)
        c_max = min(self.cols - 1, c_max)
        r_max = min(self.rows - 1, r_max)
        for r in range(r_min, r_max + 1):
            for c in range(c_min, c_max + 1):
                self.set(c, r, layer, value)

    def mark_obstacle_rect(
        self, x_min: float, y_min: float, x_max: float, y_max: float,
        layer: str, clearance_mm: float = 0.0,
    ) -> None:
        """Mark a rectangular region as obstacle (with optional clearance expansion).

        Uses floor/ceil rounding to expand outward, guaranteeing the obstacle
        covers the full specified area even when coordinates fall between grid
        points.  This prevents the pad-net overwrite from eating all clearance
        on sides where round() would snap the obstacle boundary to the same
        cell as the pad boundary.
        """
        x0 = x_min - clearance_mm
        y0 = y_min - clearance_mm
        x1 = x_max + clearance_mm
        y1 = y_max + clearance_mm
        # Expand outward: floor for min, ceil for max
        c_min = int(math.floor(x0 / self.resolution))
        r_min = int(math.floor(y0 / self.resolution))
        c_max = int(math.ceil(x1 / self.resolution))
        r_max = int(math.ceil(y1 / self.resolution))
        # Clamp to grid bounds
        c_min = max(0, c_min)
        r_min = max(0, r_min)
        c_max = min(self.cols - 1, c_max)
        r_max = min(self.rows - 1, r_max)
        for r in range(r_min, r_max + 1):
            for c in range(c_min, c_max + 1):
                self.layers[layer][r * self.cols + c] = OBSTACLE

    def mark_no_via_rect(
        self, x_min: float, y_min: float, x_max: float, y_max: float,
    ) -> None:
        """Mark a rectangular region as a via exclusion zone (no layer transitions).

        Uses floor/ceil to expand outward like mark_obstacle_rect.
        """
        c_min = int(math.floor(x_min / self.resolution))
        r_min = int(math.floor(y_min / self.resolution))
        c_max = int(math.ceil(x_max / self.resolution))
        r_max = int(math.ceil(y_max / self.resolution))
        c_min = max(0, c_min)
        r_min = max(0, r_min)
        c_max = min(self.cols - 1, c_max)
        r_max = min(self.rows - 1, r_max)
        for r in range(r_min, r_max + 1):
            for c in range(c_min, c_max + 1):
                self.no_via[r * self.cols + c] = True

    def can_place_via(self, col: int, row: int) -> bool:
        """Check if a via can be placed at this grid cell."""
        if not self._in_bounds(col, row):
            return False
        return not self.no_via[row * self.cols + col]

    def clear_net(self, net_id: int) -> None:
        """Remove all trace cells for a net (for rip-up). Does NOT clear pad cells."""
        for layer_name in self.layers:
            grid = self.layers[layer_name]
            for i in range(len(grid)):
                if grid[i] == net_id:
                    grid[i] = EMPTY

    def snapshot(self) -> dict[str, list[int]]:
        """Deep copy both layer arrays for later restore."""
        return {name: list(arr) for name, arr in self.layers.items()}

    def restore(self, snap: dict[str, list[int]]) -> None:
        """Restore grid state from a snapshot."""
        for name, arr in snap.items():
            self.layers[name] = list(arr)


# ---------------------------------------------------------------------------
# A* pathfinder
# ---------------------------------------------------------------------------

# Neighbor sets for A* routing
_NEIGHBORS_4 = [
    (0, 1, 1.0), (0, -1, 1.0), (1, 0, 1.0), (-1, 0, 1.0),
]
_NEIGHBORS_8 = [
    (0, 1, 1.0), (0, -1, 1.0), (1, 0, 1.0), (-1, 0, 1.0),       # orthogonal
    (1, 1, 1.414), (1, -1, 1.414), (-1, 1, 1.414), (-1, -1, 1.414),  # diagonal (√2)
]


def astar_route(
    grid: RoutingGrid,
    start: tuple[int, int, str],     # (col, row, layer)
    end: tuple[int, int, str],       # (col, row, layer)
    net_id: int,
    half_width_cells: int = 0,       # extra cells on each side for wide traces
    via_cost: float = 10.0,
    diagonal: bool = True,
    channel_pressure: dict[str, list[float]] | None = None,
    channel_pressure_weight: float = 3.0,
) -> list[tuple[int, int, str]] | None:
    """A* search on the routing grid.

    Neighbors: 4-connected (Manhattan) or 8-connected (diagonal + orthogonal),
    plus via transition to other layer at same position.

    Heuristic: Chebyshev distance (8-conn) or Manhattan distance (4-conn).

    Returns path as list of (col, row, layer) or None if no path found.
    """
    sc, sr, sl = start
    ec, er, el = end

    # Check if start/end are accessible
    if not grid.is_available(sc, sr, sl, net_id):
        return None
    if not grid.is_available(ec, er, el, net_id):
        return None

    neighbors_set = _NEIGHBORS_8 if diagonal else _NEIGHBORS_4

    def heuristic(c: int, r: int, layer: str) -> float:
        dx, dy = abs(c - ec), abs(r - er)
        if diagonal:
            # Chebyshev: max(|dx|, |dy|) + (√2-1)*min(|dx|, |dy|)
            h = max(dx, dy) + 0.414 * min(dx, dy)
        else:
            h = dx + dy  # Manhattan
        if layer != el:
            h += via_cost
        return h

    def can_occupy(col: int, row: int, layer: str) -> bool:
        """Check if trace (including width expansion) can fit."""
        if half_width_cells == 0:
            return grid.is_available(col, row, layer, net_id)
        for dc in range(-half_width_cells, half_width_cells + 1):
            for dr in range(-half_width_cells, half_width_cells + 1):
                if not grid.is_available(col + dc, row + dr, layer, net_id):
                    return False
        return True

    other_layer = {"top": "bottom", "bottom": "top"}

    # Priority queue: (f_cost, g_cost, col, row, layer)
    open_set: list[tuple[float, float, int, int, str]] = []
    heapq.heappush(open_set, (heuristic(sc, sr, sl), 0.0, sc, sr, sl))

    came_from: dict[tuple[int, int, str], tuple[int, int, str] | None] = {
        (sc, sr, sl): None
    }
    g_score: dict[tuple[int, int, str], float] = {(sc, sr, sl): 0.0}

    while open_set:
        f, g, col, row, layer = heapq.heappop(open_set)

        if col == ec and row == er and layer == el:
            # Reconstruct path
            path = []
            node: tuple[int, int, str] | None = (col, row, layer)
            while node is not None:
                path.append(node)
                node = came_from[node]
            path.reverse()
            return path

        # Skip if we've found a better path to this node
        if g > g_score.get((col, row, layer), math.inf):
            continue

        # Generate neighbors
        neighbors: list[tuple[int, int, str, float]] = []
        for dc, dr, step_cost in neighbors_set:
            nc, nr = col + dc, row + dr
            if grid._in_bounds(nc, nr) and can_occupy(nc, nr, layer):
                neighbors.append((nc, nr, layer, step_cost))

        # Via transition (only if not in a via exclusion zone)
        other = other_layer[layer]
        if grid.can_place_via(col, row) and can_occupy(col, row, other):
            # Density-aware via cost: penalize vias in congested areas
            # to push them toward open spaces
            density = 0
            for _dc in range(-2, 3):
                for _dr in range(-2, 3):
                    _nc, _nr = col + _dc, row + _dr
                    if grid._in_bounds(_nc, _nr):
                        v = grid.layers[layer][_nr * grid.cols + _nc]
                        if v == OBSTACLE or (v > 0 and v != net_id):
                            density += 1
            effective_via = via_cost * (1.0 + density * 0.03)
            neighbors.append((col, row, other, effective_via))

        for nc, nr, nl, cost in neighbors:
            # Apply channel pressure: prefer wider channels
            if channel_pressure is not None:
                cp_layer = channel_pressure.get(nl)
                if cp_layer is not None:
                    idx = nr * grid.cols + nc
                    if idx < len(cp_layer) and cp_layer[idx] > 0:
                        cost *= (1.0 + cp_layer[idx] * channel_pressure_weight)
            new_g = g + cost
            if new_g < g_score.get((nc, nr, nl), math.inf):
                g_score[(nc, nr, nl)] = new_g
                f_new = new_g + heuristic(nc, nr, nl)
                heapq.heappush(open_set, (f_new, new_g, nc, nr, nl))
                came_from[(nc, nr, nl)] = (col, row, layer)

    return None  # no path found


def astar_route_congestion(
    grid: RoutingGrid,
    start: tuple[int, int, str],
    end: tuple[int, int, str],
    net_id: int,
    half_width_cells: int = 0,
    via_cost: float = 10.0,
    diagonal: bool = True,
    history_cost: dict[str, list[float]] | None = None,
    present_occupancy: dict[str, list[int]] | None = None,
    hfac: float = 0.5,
    pfac: float = 1.0,
    channel_pressure: dict[str, list[float]] | None = None,
    channel_pressure_weight: float = 3.0,
) -> list[tuple[int, int, str]] | None:
    """Congestion-aware A* for negotiated congestion routing (PathFinder).

    Unlike astar_route(), foreign-net cells are passable but expensive.
    Cost = base_cost × (1 + history[cell] × hfac) × (1 + present_penalty × pfac)

    OBSTACLE cells remain truly impassable.
    """
    sc, sr, sl = start
    ec, er, el = end

    if grid.get(sc, sr, sl) == OBSTACLE:
        return None
    if grid.get(ec, er, el) == OBSTACLE:
        return None

    neighbors_set = _NEIGHBORS_8 if diagonal else _NEIGHBORS_4

    def heuristic(c: int, r: int, layer: str) -> float:
        dx, dy = abs(c - ec), abs(r - er)
        if diagonal:
            h = max(dx, dy) + 0.414 * min(dx, dy)
        else:
            h = dx + dy
        if layer != el:
            h += via_cost
        return h

    def cell_passable(col: int, row: int, layer: str) -> bool:
        """Check if cell is passable (not OBSTACLE). Foreign nets are allowed."""
        if half_width_cells == 0:
            val = grid.get(col, row, layer)
            return val != OBSTACLE
        for dc in range(-half_width_cells, half_width_cells + 1):
            for dr in range(-half_width_cells, half_width_cells + 1):
                val = grid.get(col + dc, row + dr, layer)
                if val == OBSTACLE:
                    return False
        return True

    def congestion_cost(col: int, row: int, layer: str, base_cost: float) -> float:
        """Apply congestion penalties to base movement cost."""
        cost = base_cost
        # Check if any cell in the trace footprint has congestion
        if half_width_cells == 0:
            val = grid.get(col, row, layer)
            if val != EMPTY and val != net_id:
                # Foreign net cell — apply penalties
                h_cost = 0.0
                p_penalty = 0.0
                if history_cost is not None:
                    idx = row * grid.cols + col
                    if 0 <= idx < len(history_cost.get(layer, [])):
                        h_cost = history_cost[layer][idx]
                if present_occupancy is not None:
                    idx = row * grid.cols + col
                    if 0 <= idx < len(present_occupancy.get(layer, [])):
                        p_penalty = max(0, present_occupancy[layer][idx] - 1)
                cost *= (1.0 + h_cost * hfac) * (1.0 + p_penalty * pfac)
                # Base penalty for using a foreign-net cell
                cost += 50.0
        else:
            for dcc in range(-half_width_cells, half_width_cells + 1):
                for drr in range(-half_width_cells, half_width_cells + 1):
                    val = grid.get(col + dcc, row + drr, layer)
                    if val != EMPTY and val != net_id and val != OBSTACLE:
                        h_cost = 0.0
                        p_penalty = 0.0
                        idx = (row + drr) * grid.cols + (col + dcc)
                        if history_cost is not None and 0 <= idx < len(history_cost.get(layer, [])):
                            h_cost = history_cost[layer][idx]
                        if present_occupancy is not None and 0 <= idx < len(present_occupancy.get(layer, [])):
                            p_penalty = max(0, present_occupancy[layer][idx] - 1)
                        cost *= (1.0 + h_cost * hfac) * (1.0 + p_penalty * pfac)
                        cost += 50.0
                        break  # one foreign cell is enough to penalize
        # Apply channel pressure (independent of foreign-net status)
        if channel_pressure is not None:
            cp_layer = channel_pressure.get(layer)
            if cp_layer is not None:
                idx = row * grid.cols + col
                if idx < len(cp_layer) and cp_layer[idx] > 0:
                    cost *= (1.0 + cp_layer[idx] * channel_pressure_weight)
        return cost

    other_layer = {"top": "bottom", "bottom": "top"}

    open_set: list[tuple[float, float, int, int, str]] = []
    heapq.heappush(open_set, (heuristic(sc, sr, sl), 0.0, sc, sr, sl))

    came_from: dict[tuple[int, int, str], tuple[int, int, str] | None] = {
        (sc, sr, sl): None
    }
    g_score: dict[tuple[int, int, str], float] = {(sc, sr, sl): 0.0}

    while open_set:
        f, g, col, row, layer = heapq.heappop(open_set)

        if col == ec and row == er and layer == el:
            path = []
            node: tuple[int, int, str] | None = (col, row, layer)
            while node is not None:
                path.append(node)
                node = came_from[node]
            path.reverse()
            return path

        if g > g_score.get((col, row, layer), math.inf):
            continue

        neighbors: list[tuple[int, int, str, float]] = []
        for dc, dr, step_cost in neighbors_set:
            nc, nr = col + dc, row + dr
            if grid._in_bounds(nc, nr) and cell_passable(nc, nr, layer):
                cost = congestion_cost(nc, nr, layer, step_cost)
                neighbors.append((nc, nr, layer, cost))

        # Via transition with density-aware cost
        other = other_layer[layer]
        if grid.can_place_via(col, row) and cell_passable(col, row, other):
            density = 0
            for _dc in range(-2, 3):
                for _dr in range(-2, 3):
                    _nc, _nr = col + _dc, row + _dr
                    if grid._in_bounds(_nc, _nr):
                        v = grid.layers[layer][_nr * grid.cols + _nc]
                        if v == OBSTACLE or (v > 0 and v != net_id):
                            density += 1
            effective_via = via_cost * (1.0 + density * 0.03)
            cost = congestion_cost(col, row, other, effective_via)
            neighbors.append((col, row, other, cost))

        for nc, nr, nl, cost in neighbors:
            new_g = g + cost
            if new_g < g_score.get((nc, nr, nl), math.inf):
                g_score[(nc, nr, nl)] = new_g
                f_new = new_g + heuristic(nc, nr, nl)
                heapq.heappush(open_set, (f_new, new_g, nc, nr, nl))
                came_from[(nc, nr, nl)] = (col, row, layer)

    return None


def astar_find_blockers(
    grid: RoutingGrid,
    start: tuple[int, int, str],
    end: tuple[int, int, str],
    net_id: int,
    half_width_cells: int = 0,
    via_cost: float = 10.0,
    diagonal: bool = True,
    foreign_net_cost: float = 500.0,
) -> set[int] | None:
    """Find which routed nets block a path between two points.

    Like astar_route but treats foreign-net cells as passable at high cost
    instead of impassable.  OBSTACLE cells remain impassable.

    Returns the set of foreign net IDs the cheapest path crosses, or None
    if no path exists even with passthrough (e.g., surrounded by OBSTACLE).
    """
    sc, sr, sl = start
    ec, er, el = end

    neighbors_set = _NEIGHBORS_8 if diagonal else _NEIGHBORS_4

    def heuristic(c: int, r: int, layer: str) -> float:
        dx, dy = abs(c - ec), abs(r - er)
        if diagonal:
            h = max(dx, dy) + 0.414 * min(dx, dy)
        else:
            h = dx + dy
        if layer != el:
            h += via_cost
        return h

    def step_cost_and_passable(col: int, row: int, layer: str) -> tuple[float, bool]:
        """Return (extra_cost, passable) for occupying this cell."""
        if half_width_cells == 0:
            val = grid.get(col, row, layer)
            if val == OBSTACLE:
                return (0.0, False)
            if val == EMPTY or val == net_id:
                return (0.0, True)
            # Foreign net — passable at high cost
            return (foreign_net_cost, True)
        # Wide trace: check all cells in the width expansion
        extra = 0.0
        for dc in range(-half_width_cells, half_width_cells + 1):
            for dr in range(-half_width_cells, half_width_cells + 1):
                val = grid.get(col + dc, row + dr, layer)
                if val == OBSTACLE:
                    return (0.0, False)
                if val != EMPTY and val != net_id:
                    extra = max(extra, foreign_net_cost)
        return (extra, True)

    other_layer = {"top": "bottom", "bottom": "top"}

    open_set: list[tuple[float, float, int, int, str]] = []
    heapq.heappush(open_set, (heuristic(sc, sr, sl), 0.0, sc, sr, sl))
    came_from: dict[tuple[int, int, str], tuple[int, int, str] | None] = {
        (sc, sr, sl): None
    }
    g_score: dict[tuple[int, int, str], float] = {(sc, sr, sl): 0.0}

    while open_set:
        f, g, col, row, layer = heapq.heappop(open_set)

        if col == ec and row == er and layer == el:
            # Reconstruct path and collect foreign net IDs
            blockers: set[int] = set()
            node: tuple[int, int, str] | None = (col, row, layer)
            while node is not None:
                c, r, l = node
                if half_width_cells == 0:
                    val = grid.get(c, r, l)
                    if val > 0 and val != net_id:
                        blockers.add(val)
                else:
                    for dc2 in range(-half_width_cells, half_width_cells + 1):
                        for dr2 in range(-half_width_cells, half_width_cells + 1):
                            val = grid.get(c + dc2, r + dr2, l)
                            if val > 0 and val != net_id:
                                blockers.add(val)
                node = came_from[node]
            return blockers

        if g > g_score.get((col, row, layer), math.inf):
            continue

        # Generate neighbors
        for dc, dr, base_cost in neighbors_set:
            nc, nr = col + dc, row + dr
            if not grid._in_bounds(nc, nr):
                continue
            extra, passable = step_cost_and_passable(nc, nr, layer)
            if not passable:
                continue
            new_g = g + base_cost + extra
            if new_g < g_score.get((nc, nr, layer), math.inf):
                g_score[(nc, nr, layer)] = new_g
                heapq.heappush(open_set, (new_g + heuristic(nc, nr, layer), new_g, nc, nr, layer))
                came_from[(nc, nr, layer)] = (col, row, layer)

        # Via transition
        other = other_layer[layer]
        if grid.can_place_via(col, row):
            extra, passable = step_cost_and_passable(col, row, other)
            if passable:
                new_g = g + via_cost + extra
                if new_g < g_score.get((col, row, other), math.inf):
                    g_score[(col, row, other)] = new_g
                    heapq.heappush(open_set, (new_g + heuristic(col, row, other), new_g, col, row, other))
                    came_from[(col, row, other)] = (col, row, layer)

    return None


# ---------------------------------------------------------------------------
# Path processing
# ---------------------------------------------------------------------------

def simplify_path(
    path: list[tuple[int, int, str]],
    resolution_mm: float,
    trace_width: float,
    net_id: str,
    net_name: str,
) -> tuple[list[TraceSegment], list[Via]]:
    """Convert grid path to minimal trace segments and vias.

    Merges collinear consecutive points on the same layer.
    Layer transitions become via points.
    """
    if len(path) < 2:
        return [], []

    traces: list[TraceSegment] = []
    vias: list[Via] = []

    # Walk through path, grouping collinear segments
    seg_start = path[0]
    prev = path[0]

    for i in range(1, len(path)):
        curr = path[i]

        # Check for layer change
        if curr[2] != prev[2]:
            # Emit the trace segment up to prev
            if seg_start != prev:
                sx, sy = seg_start[0] * resolution_mm, seg_start[1] * resolution_mm
                ex, ey = prev[0] * resolution_mm, prev[1] * resolution_mm
                traces.append(TraceSegment(sx, sy, ex, ey, trace_width, prev[2], net_id, net_name))

            # Emit via
            vx, vy = prev[0] * resolution_mm, prev[1] * resolution_mm
            vias.append(Via(
                vx, vy, VIA_DRILL_MM, VIA_DIAMETER_MM,
                prev[2], curr[2], net_id, net_name,
            ))

            seg_start = curr
            prev = curr
            continue

        # Same layer — check if direction changed
        if i >= 2 and path[i - 2][2] == prev[2] == curr[2]:
            # Direction from prev-1 to prev
            d1c = prev[0] - path[i - 2][0]
            d1r = prev[1] - path[i - 2][1]
            # Direction from prev to curr
            d2c = curr[0] - prev[0]
            d2r = curr[1] - prev[1]

            if (d1c, d1r) != (d2c, d2r):
                # Direction changed — emit segment up to prev
                sx, sy = seg_start[0] * resolution_mm, seg_start[1] * resolution_mm
                ex, ey = prev[0] * resolution_mm, prev[1] * resolution_mm
                if (sx, sy) != (ex, ey):
                    traces.append(TraceSegment(sx, sy, ex, ey, trace_width, prev[2], net_id, net_name))
                seg_start = prev

        prev = curr

    # Emit final segment
    if seg_start != prev:
        sx, sy = seg_start[0] * resolution_mm, seg_start[1] * resolution_mm
        ex, ey = prev[0] * resolution_mm, prev[1] * resolution_mm
        traces.append(TraceSegment(sx, sy, ex, ey, trace_width, prev[2], net_id, net_name))

    return traces, vias


def mark_path_on_grid(
    grid: RoutingGrid,
    path: list[tuple[int, int, str]],
    net_id: int,
    half_width_cells: int = 0,
    via_half_width_cells: int | None = None,
) -> None:
    """Mark a routed path on the grid to block other nets.

    Via transition points (where consecutive path entries change layer)
    get a larger blocking radius because vias are wider than traces.
    """
    if via_half_width_cells is None:
        via_half_width_cells = half_width_cells

    # Identify via transition points — cells where a layer change occurs
    via_points: set[tuple[int, int]] = set()
    for i in range(len(path) - 1):
        c1, r1, l1 = path[i]
        c2, r2, l2 = path[i + 1]
        if l1 != l2 and c1 == c2 and r1 == r2:
            via_points.add((c1, r1))

    for col, row, layer in path:
        hw = via_half_width_cells if (col, row) in via_points else half_width_cells
        for dc in range(-hw, hw + 1):
            for dr in range(-hw, hw + 1):
                c, r = col + dc, row + dr
                if grid._in_bounds(c, r):
                    val = grid.get(c, r, layer)
                    if val == EMPTY:
                        grid.set(c, r, layer, net_id)


# ---------------------------------------------------------------------------
# Net ordering
# ---------------------------------------------------------------------------

def order_nets(
    nets: list[NetInfo],
    pad_map: dict[str, PadInfo],
    netlist: dict,
) -> list[NetInfo]:
    """Order nets for routing: power first, ground second, signal shortest-first.

    Within each class, sort by ascending estimated wire length.
    """
    elements = netlist.get("elements", [])

    # Map net_id -> list of pad positions
    net_pads: dict[str, list[tuple[float, float]]] = {}
    for pad in pad_map.values():
        if pad.net_id:
            net_pads.setdefault(pad.net_id, []).append((pad.x_mm, pad.y_mm))

    def wire_length_estimate(net: NetInfo) -> float:
        pads = net_pads.get(net.net_id, [])
        if len(pads) < 2:
            return 0.0
        total = 0.0
        for ia, ib, dist in compute_mst_edges(pads):
            total += dist
        return total

    # Separate by class
    power_nets = [n for n in nets if n.net_class == "power"]
    ground_nets = [n for n in nets if n.net_class == "ground"]
    signal_nets = [n for n in nets if n.net_class == "signal"]

    # Sort each group by wire length (shortest first)
    power_nets.sort(key=wire_length_estimate)
    ground_nets.sort(key=wire_length_estimate)
    signal_nets.sort(key=wire_length_estimate)

    return power_nets + ground_nets + signal_nets


def _pad_accessibility(
    net: NetInfo,
    pad_map: dict[str, PadInfo],
    grid: RoutingGrid,
    net_id_map: dict[str, int],
    netlist: dict,
    radius: int = 8,
) -> float:
    """Score how accessible a net's pads are (lower = more constrained).

    Counts EMPTY cells within `radius` grid cells of each pad.  Returns
    the minimum count across all pads — the bottleneck pad determines the
    net's escape difficulty.
    """
    pads = _get_net_pads(net, pad_map, netlist)
    if not pads:
        return 999.0

    min_score = float("inf")
    for pad in pads:
        layers = ["top", "bottom"] if pad.layer == "all" else [pad.layer]
        pad_score = 0
        for layer in layers:
            pc, pr = grid.mm_to_grid(pad.x_mm, pad.y_mm)
            count = 0
            for dc in range(-radius, radius + 1):
                for dr in range(-radius, radius + 1):
                    if dc * dc + dr * dr > radius * radius:
                        continue
                    c, r = pc + dc, pr + dr
                    if grid._in_bounds(c, r) and grid.get(c, r, layer) == EMPTY:
                        count += 1
            pad_score = max(pad_score, count)  # best layer for this pad
        min_score = min(min_score, pad_score)

    return min_score


def order_nets_by_accessibility(
    nets: list[NetInfo],
    pad_map: dict[str, PadInfo],
    grid: RoutingGrid,
    net_id_map: dict[str, int],
    netlist: dict,
) -> list[NetInfo]:
    """Order nets: power first, then signal nets by ascending pad accessibility.

    Most-constrained nets (fewest escape routes) are routed first.
    """
    power_nets = [n for n in nets if n.net_class in ("power", "ground")]
    signal_nets = [n for n in nets if n.net_class == "signal"]

    # Score each signal net's accessibility
    scored = [(n, _pad_accessibility(n, pad_map, grid, net_id_map, netlist))
              for n in signal_nets]
    scored.sort(key=lambda x: x[1])  # ascending = most constrained first

    return power_nets + [n for n, _ in scored]


# ---------------------------------------------------------------------------
# Net routing
# ---------------------------------------------------------------------------

def _get_net_pads(
    net_info: NetInfo,
    pad_map: dict[str, PadInfo],
    netlist: dict,
) -> list[PadInfo]:
    """Get all pads belonging to a net."""
    elements = netlist.get("elements", [])

    # Find the net element
    net_elem = None
    for elem in elements:
        if elem.get("element_type") == "net" and elem.get("net_id") == net_info.net_id:
            net_elem = elem
            break

    if not net_elem:
        return []

    pads = []
    for pid in net_elem.get("connected_port_ids", []):
        if pid in pad_map:
            pads.append(pad_map[pid])
    return pads


def _restore_pad_markings(
    grid: RoutingGrid,
    net_info: NetInfo,
    pad_map: dict[str, PadInfo],
    net_id_int: int,
    clearance_mm: float,
    netlist: dict,
) -> None:
    """Re-mark pad+clearance zones on the grid for a specific net.

    Called after clear_net() during rip-up to restore pad protection that
    was erased along with traces (both use the same net ID on the grid).
    """
    pads = _get_net_pads(net_info, pad_map, netlist)
    for pad in pads:
        pw, ph = pad.pad_width_mm, pad.pad_height_mm
        is_th = pad.layer == "all"
        clr = clearance_mm
        comp_layer = pad.layer if pad.layer != "all" else "top"

        if is_th:
            drill_radius = min(pw, ph) / 2
            for blk_layer in ["top", "bottom"]:
                if blk_layer == comp_layer:
                    grid.mark_rect(
                        pad.x_mm - pw / 2 - clr, pad.y_mm - ph / 2 - clr,
                        pad.x_mm + pw / 2 + clr, pad.y_mm + ph / 2 + clr,
                        blk_layer, net_id_int,
                    )
                else:
                    grid.mark_rect(
                        pad.x_mm - drill_radius - clr, pad.y_mm - drill_radius - clr,
                        pad.x_mm + drill_radius + clr, pad.y_mm + drill_radius + clr,
                        blk_layer, net_id_int,
                    )
        else:
            grid.mark_rect(
                pad.x_mm - pw / 2 - clr, pad.y_mm - ph / 2 - clr,
                pad.x_mm + pw / 2 + clr, pad.y_mm + ph / 2 + clr,
                comp_layer, net_id_int,
            )


def route_net(
    grid: RoutingGrid,
    net_info: NetInfo,
    pad_map: dict[str, PadInfo],
    net_id_int: int,
    config: RouterConfig,
    netlist: dict,
    trace_width: float,
    diagonal: bool = True,
    via_cost_override: float | None = None,
    clearance_override: float | None = None,
    channel_pressure: dict[str, list[float]] | None = None,
) -> tuple[list[TraceSegment], list[Via]] | None:
    """Route all connections within a single net using MST decomposition.

    1. Gather all pad positions
    2. Compute MST edges
    3. Route each MST edge with A*
    4. After each edge, mark path on grid so subsequent edges can connect
    """
    effective_via_cost = via_cost_override if via_cost_override is not None else config.via_cost
    effective_clearance = clearance_override if clearance_override is not None else config.clearance_mm

    pads = _get_net_pads(net_info, pad_map, netlist)
    if len(pads) < 2:
        return ([], [])

    # Two separate cell expansions:
    # 1. route_hw: cells the trace physically occupies (for A* can_occupy check)
    # 2. block_hw: cells to block for other nets (trace + clearance)
    # Use grid.resolution (not config) so this works on fine grids too
    grid_res = grid.resolution
    route_hw = int(math.ceil(trace_width / (2 * grid_res))) - 1
    route_hw = max(0, route_hw)
    block_hw = int(math.ceil((trace_width / 2 + effective_clearance) / grid_res))
    block_hw = max(route_hw, block_hw)
    # Vias are larger than traces — need bigger blocking radius at via points
    via_radius = config.via_diameter_mm / 2
    via_block_hw = int(math.ceil((via_radius + trace_width / 2 + effective_clearance) / grid_res))
    via_block_hw = max(block_hw, via_block_hw)

    # Get pad positions for MST
    pad_positions = [(p.x_mm, p.y_mm) for p in pads]
    pad_layers = [p.layer for p in pads]

    mst_edges = compute_mst_edges(pad_positions)

    all_traces: list[TraceSegment] = []
    all_vias: list[Via] = []

    for ia, ib, _ in mst_edges:
        # Source and target pads
        pad_a = pads[ia]
        pad_b = pads[ib]

        # Convert to grid coordinates
        sc, sr = grid.mm_to_grid(pad_a.x_mm, pad_a.y_mm)
        ec, er = grid.mm_to_grid(pad_b.x_mm, pad_b.y_mm)

        # Resolve "all" (through-hole) to concrete layer candidates.
        # TH pads exist on both layers, so try multiple layer combos and
        # keep the shortest path.  This lets the router use the bottom layer
        # directly instead of needing a via from the top layer.
        start_layers = ["top", "bottom"] if pad_a.layer == "all" else [pad_a.layer]
        end_layers = ["top", "bottom"] if pad_b.layer == "all" else [pad_b.layer]

        best_path = None
        for sl in start_layers:
            for el in end_layers:
                p = astar_route(
                    grid,
                    (sc, sr, sl),
                    (ec, er, el),
                    net_id_int,
                    half_width_cells=route_hw,
                    via_cost=effective_via_cost,
                    diagonal=diagonal,
                    channel_pressure=channel_pressure,
                    channel_pressure_weight=config.channel_pressure_weight,
                )
                if p is not None:
                    if best_path is None or len(p) < len(best_path):
                        best_path = p

        path = best_path
        if path is None:
            return None  # routing failed for this net

        # Mark path on grid with wider blocking zone (trace + clearance)
        # Via points get extra blocking due to larger copper diameter
        mark_path_on_grid(grid, path, net_id_int, block_hw, via_block_hw)

        # Convert to trace segments and vias
        traces, vias = simplify_path(
            path, grid_res, trace_width,
            net_info.net_id, net_info.name,
        )

        # Snap trace endpoints to exact pad positions (grid-snapped coords
        # may be off by up to 1 grid cell, causing KiCad to show airwires)
        if traces:
            t0 = traces[0]
            traces[0] = TraceSegment(
                pad_a.x_mm, pad_a.y_mm,
                t0.end_x_mm, t0.end_y_mm,
                t0.width_mm, t0.layer, t0.net_id, t0.net_name,
            )
            tN = traces[-1]
            traces[-1] = TraceSegment(
                tN.start_x_mm, tN.start_y_mm,
                pad_b.x_mm, pad_b.y_mm,
                tN.width_mm, tN.layer, tN.net_id, tN.net_name,
            )

        # Snap vias near pad endpoints to exact pad positions
        # (when a trace arrives on a different layer than the pad,
        # the via must align with the snapped trace endpoint)
        snap_tol = grid_res * 1.5
        for vi, v in enumerate(vias):
            if abs(v.x_mm - pad_a.x_mm) < snap_tol and abs(v.y_mm - pad_a.y_mm) < snap_tol:
                vias[vi] = Via(pad_a.x_mm, pad_a.y_mm, v.drill_mm, v.diameter_mm,
                               v.layer_from, v.layer_to, v.net_id, v.net_name)
            elif abs(v.x_mm - pad_b.x_mm) < snap_tol and abs(v.y_mm - pad_b.y_mm) < snap_tol:
                vias[vi] = Via(pad_b.x_mm, pad_b.y_mm, v.drill_mm, v.diameter_mm,
                               v.layer_from, v.layer_to, v.net_id, v.net_name)

        all_traces.extend(traces)
        all_vias.extend(vias)

    return (all_traces, all_vias)


def route_net_congestion(
    grid: RoutingGrid,
    net_info: NetInfo,
    pad_map: dict[str, PadInfo],
    net_id_int: int,
    config: RouterConfig,
    netlist: dict,
    trace_width: float,
    diagonal: bool = True,
    history_cost: dict[str, list[float]] | None = None,
    present_occupancy: dict[str, list[int]] | None = None,
    hfac: float = 0.5,
    pfac: float = 1.0,
    via_cost_override: float | None = None,
    clearance_override: float | None = None,
    channel_pressure: dict[str, list[float]] | None = None,
) -> tuple[list[TraceSegment], list[Via], list[tuple[int, int, str]]] | None:
    """Route a net using congestion-aware A* (for PathFinder NCR).

    Returns (traces, vias, path_cells) on success, None on failure.
    Does NOT mark path on grid — caller manages occupancy tracking.
    """
    effective_via_cost = via_cost_override if via_cost_override is not None else config.via_cost
    pads = _get_net_pads(net_info, pad_map, netlist)
    if len(pads) < 2:
        return ([], [], [])

    grid_res = grid.resolution
    route_hw = int(math.ceil(trace_width / (2 * grid_res))) - 1
    route_hw = max(0, route_hw)

    pad_positions = [(p.x_mm, p.y_mm) for p in pads]
    mst_edges = compute_mst_edges(pad_positions)

    all_traces: list[TraceSegment] = []
    all_vias: list[Via] = []
    all_path_cells: list[tuple[int, int, str]] = []

    for ia, ib, _ in mst_edges:
        pad_a = pads[ia]
        pad_b = pads[ib]
        sc, sr = grid.mm_to_grid(pad_a.x_mm, pad_a.y_mm)
        ec, er = grid.mm_to_grid(pad_b.x_mm, pad_b.y_mm)

        start_layers = ["top", "bottom"] if pad_a.layer == "all" else [pad_a.layer]
        end_layers = ["top", "bottom"] if pad_b.layer == "all" else [pad_b.layer]

        best_path = None
        for sl in start_layers:
            for el in end_layers:
                p = astar_route_congestion(
                    grid,
                    (sc, sr, sl),
                    (ec, er, el),
                    net_id_int,
                    half_width_cells=route_hw,
                    via_cost=effective_via_cost,
                    diagonal=diagonal,
                    history_cost=history_cost,
                    present_occupancy=present_occupancy,
                    hfac=hfac,
                    pfac=pfac,
                    channel_pressure=channel_pressure,
                    channel_pressure_weight=config.channel_pressure_weight,
                )
                if p is not None:
                    if best_path is None or len(p) < len(best_path):
                        best_path = p

        path = best_path
        if path is None:
            return None

        all_path_cells.extend(path)

        traces, vias = simplify_path(
            path, grid_res, trace_width,
            net_info.net_id, net_info.name,
        )

        # Snap endpoints to exact pad positions
        if traces:
            t0 = traces[0]
            traces[0] = TraceSegment(
                pad_a.x_mm, pad_a.y_mm,
                t0.end_x_mm, t0.end_y_mm,
                t0.width_mm, t0.layer, t0.net_id, t0.net_name,
            )
            tN = traces[-1]
            traces[-1] = TraceSegment(
                tN.start_x_mm, tN.start_y_mm,
                pad_b.x_mm, pad_b.y_mm,
                tN.width_mm, tN.layer, tN.net_id, tN.net_name,
            )

        # Snap vias near pad endpoints to exact pad positions
        snap_tol = grid_res * 1.5
        for vi, v in enumerate(vias):
            if abs(v.x_mm - pad_a.x_mm) < snap_tol and abs(v.y_mm - pad_a.y_mm) < snap_tol:
                vias[vi] = Via(pad_a.x_mm, pad_a.y_mm, v.drill_mm, v.diameter_mm,
                               v.layer_from, v.layer_to, v.net_id, v.net_name)
            elif abs(v.x_mm - pad_b.x_mm) < snap_tol and abs(v.y_mm - pad_b.y_mm) < snap_tol:
                vias[vi] = Via(pad_b.x_mm, pad_b.y_mm, v.drill_mm, v.diameter_mm,
                               v.layer_from, v.layer_to, v.net_id, v.net_name)

        all_traces.extend(traces)
        all_vias.extend(vias)

    return (all_traces, all_vias, all_path_cells)


# ---------------------------------------------------------------------------
# Board setup
# ---------------------------------------------------------------------------

def _setup_grid(
    grid: RoutingGrid,
    placement: dict,
    pad_map: dict[str, PadInfo],
    clearance_mm: float,
    net_id_map: dict[str, int],
) -> None:
    """Mark obstacles on the routing grid.

    SMD components: no body obstacle — traces route freely between pads.
    Through-hole components: body marked as obstacle on the component's layer
    only (pins block that layer); opposite layer remains open for routing
    underneath. Pads overwrite body obstacles so nets can reach their pads.
    """
    # Mark board edge clearance — prevent traces from routing near/outside the
    # board outline.  Standard PCB manufacturing requires ≥0.25 mm copper-to-edge
    # clearance.  We mark a band of OBSTACLE cells around all four edges on both
    # layers so the A* pathfinder cannot place traces there.
    edge_clearance_mm = 0.3  # 0.3 mm keepout from board edge
    edge_cells = max(1, int(math.ceil(edge_clearance_mm / grid.resolution)))
    for layer in ("top", "bottom"):
        # Top edge (rows 0..edge_cells-1)
        for r in range(edge_cells):
            for c in range(grid.cols):
                grid.layers[layer][r * grid.cols + c] = OBSTACLE
        # Bottom edge
        for r in range(grid.rows - edge_cells, grid.rows):
            for c in range(grid.cols):
                grid.layers[layer][r * grid.cols + c] = OBSTACLE
        # Left edge
        for r in range(grid.rows):
            for c in range(edge_cells):
                grid.layers[layer][r * grid.cols + c] = OBSTACLE
        # Right edge
        for r in range(grid.rows):
            for c in range(grid.cols - edge_cells, grid.cols):
                grid.layers[layer][r * grid.cols + c] = OBSTACLE
    # Also block vias in the edge band
    for r in range(grid.rows):
        for c in range(grid.cols):
            if (r < edge_cells or r >= grid.rows - edge_cells or
                    c < edge_cells or c >= grid.cols - edge_cells):
                grid.no_via[r * grid.cols + c] = True

    # Mark through-hole component bodies on their own layer only
    th_prefixes = ("DIP", "PinHeader", "PJ-002A", "TO-220", "HC49")
    for plc in placement.get("placements", []):
        pkg = plc.get("package", "")
        if not any(pkg.startswith(p) for p in th_prefixes):
            continue

        w = plc["footprint_width_mm"]
        h = plc["footprint_height_mm"]
        rot = plc.get("rotation_deg", 0)
        if rot in (90, 270):
            w, h = h, w

        cx, cy = plc["x_mm"], plc["y_mm"]
        layer = plc.get("layer", "top")
        grid.mark_obstacle_rect(
            cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2, layer, 0.0
        )

    # Mark fiducial exclusion zones.
    # Fiducials are 1mm copper dots with a 3mm solder mask opening. No traces
    # or vias may cross the mask opening area — the pick-and-place camera
    # needs a clean, unobstructed copper dot on bare substrate.
    for plc in placement.get("placements", []):
        if plc.get("component_type") != "fiducial":
            continue
        cx, cy = plc["x_mm"], plc["y_mm"]
        layer = plc.get("layer", "top")
        # Full footprint = mask opening (3mm)
        fw = plc.get("footprint_width_mm", 3.0)
        fh = plc.get("footprint_height_mm", 3.0)
        half_w, half_h = fw / 2, fh / 2
        grid.mark_obstacle_rect(
            cx - half_w, cy - half_h, cx + half_w, cy + half_h, layer, 0.0,
        )
        grid.mark_no_via_rect(
            cx - half_w, cy - half_h, cx + half_w, cy + half_h,
        )

    # Mark pads with clearance zone using the pad's net ID.
    #
    # The entire pad+clearance area is marked with the pad's net ID. This:
    # 1. Lets same-net A* traverse the full area (enter/exit pads freely)
    # 2. Blocks other-net A* (is_available returns False for foreign net IDs)
    # 3. Blocks the copper fill (clearance mask expands around non-fill net IDs)
    #
    # Previously this used OBSTACLE for the clearance zone, but OBSTACLE blocks
    # ALL nets including the pad's own — trapping A* inside the pad area when
    # floor/ceil rounding makes the obstacle ring wider than the pad net area.
    clearance_cells = max(1, int(math.ceil(clearance_mm / grid.resolution)))
    for pad in pad_map.values():
        if pad.net_id and pad.net_id in net_id_map:
            nid = net_id_map[pad.net_id]
            pw, ph = pad.pad_width_mm, pad.pad_height_mm
            is_th = pad.layer == "all"

            eff_pw, eff_ph = pw, ph
            pad_radius = max(pw, ph) / 2

            clr = clearance_mm
            comp_layer = pad.layer if pad.layer != "all" else "top"

            if is_th:
                # TH pads are circular on both layers in KiCad export,
                # using max(w,h) as the diameter. Use the same size here.
                th_pad_radius = max(eff_pw, eff_ph) / 2
                for blk_layer in ["top", "bottom"]:
                    if blk_layer == comp_layer:
                        # Component layer: full pad + clearance as net ID
                        grid.mark_rect(
                            pad.x_mm - eff_pw / 2 - clr, pad.y_mm - eff_ph / 2 - clr,
                            pad.x_mm + eff_pw / 2 + clr, pad.y_mm + eff_ph / 2 + clr,
                            blk_layer, nid,
                        )
                    else:
                        # Opposite layer: full circular pad + clearance as net ID
                        # Must match KiCad export: max(w,h) diameter circle
                        grid.mark_rect(
                            pad.x_mm - th_pad_radius - clr, pad.y_mm - th_pad_radius - clr,
                            pad.x_mm + th_pad_radius + clr, pad.y_mm + th_pad_radius + clr,
                            blk_layer, nid,
                        )
            else:
                # SMD: single layer — pad + clearance as net ID
                grid.mark_rect(
                    pad.x_mm - eff_pw / 2 - clr, pad.y_mm - eff_ph / 2 - clr,
                    pad.x_mm + eff_pw / 2 + clr, pad.y_mm + eff_ph / 2 + clr,
                    comp_layer, nid,
                )

            # Mark via exclusion zone around ALL pads.
            # TH pads: via drill must not overlap pad copper on either layer.
            # SMD pads: via-in-pad causes solder wicking during reflow.
            # The exclusion zone covers the pad + clearance so vias land
            # outside the pad's keepout area.
            via_excl = pad_radius + clr
            grid.mark_no_via_rect(
                pad.x_mm - via_excl, pad.y_mm - via_excl,
                pad.x_mm + via_excl, pad.y_mm + via_excl,
            )


# ---------------------------------------------------------------------------
# Channel detection and pressure
# ---------------------------------------------------------------------------


def _detect_channels(
    pad_map: dict[str, PadInfo],
    config: RouterConfig,
    board_w: float,
    board_h: float,
) -> list[Channel]:
    """Detect routing channels between parallel rows of pads.

    Groups pads by component, clusters into rows, and identifies gaps
    between rows where traces must compete for limited space.
    """
    # Group pads by component designator
    comp_pads: dict[str, list[PadInfo]] = {}
    for pad in pad_map.values():
        comp_pads.setdefault(pad.designator, []).append(pad)

    channels: list[Channel] = []
    trace_pitch = config.trace_width_signal_mm + config.clearance_mm

    for desig, pads in comp_pads.items():
        if len(pads) < 4:
            continue  # need at least 2 rows × 2 pads

        # Cluster pads into horizontal rows (same Y ± tolerance)
        h_rows: list[list[PadInfo]] = []
        sorted_by_y = sorted(pads, key=lambda p: p.y_mm)
        current_row = [sorted_by_y[0]]
        for p in sorted_by_y[1:]:
            if abs(p.y_mm - current_row[0].y_mm) < 0.2:
                current_row.append(p)
            else:
                if len(current_row) >= 3:
                    h_rows.append(current_row)
                current_row = [p]
        if len(current_row) >= 3:
            h_rows.append(current_row)

        # Cluster pads into vertical rows (same X ± tolerance)
        v_rows: list[list[PadInfo]] = []
        sorted_by_x = sorted(pads, key=lambda p: p.x_mm)
        current_row = [sorted_by_x[0]]
        for p in sorted_by_x[1:]:
            if abs(p.x_mm - current_row[0].x_mm) < 0.2:
                current_row.append(p)
            else:
                if len(current_row) >= 3:
                    v_rows.append(current_row)
                current_row = [p]
        if len(current_row) >= 3:
            v_rows.append(current_row)

        # Detect horizontal channels (between rows at different Y)
        for i in range(len(h_rows)):
            for j in range(i + 1, len(h_rows)):
                row_a, row_b = h_rows[i], h_rows[j]
                y_a = sum(p.y_mm for p in row_a) / len(row_a)
                y_b = sum(p.y_mm for p in row_b) / len(row_b)
                if y_a > y_b:
                    y_a, y_b = y_b, y_a
                    row_a, row_b = row_b, row_a

                gap = y_b - y_a
                # Compute pad radius (use max dimension for safety)
                pad_r_a = max(p.pad_width_mm for p in row_a) / 2
                pad_r_b = max(p.pad_width_mm for p in row_b) / 2
                usable = gap - (pad_r_a + config.clearance_mm) - (pad_r_b + config.clearance_mm)

                if usable < trace_pitch:
                    continue  # too narrow for even 1 trace

                capacity = int(usable / trace_pitch)
                # Channel spans the X overlap of the two rows
                x_min = max(min(p.x_mm for p in row_a), min(p.x_mm for p in row_b))
                x_max = min(max(p.x_mm for p in row_a), max(p.x_mm for p in row_b))
                if x_max <= x_min:
                    continue  # rows don't overlap in X

                channels.append(Channel(
                    x_min_mm=x_min - 1.0,  # small margin for traces entering the channel
                    y_min_mm=y_a + pad_r_a + config.clearance_mm,
                    x_max_mm=x_max + 1.0,
                    y_max_mm=y_b - pad_r_b - config.clearance_mm,
                    axis="horizontal",
                    capacity=capacity,
                    width_mm=usable,
                ))

        # Detect vertical channels (between columns at different X)
        for i in range(len(v_rows)):
            for j in range(i + 1, len(v_rows)):
                col_a, col_b = v_rows[i], v_rows[j]
                x_a = sum(p.x_mm for p in col_a) / len(col_a)
                x_b = sum(p.x_mm for p in col_b) / len(col_b)
                if x_a > x_b:
                    x_a, x_b = x_b, x_a
                    col_a, col_b = col_b, col_a

                gap = x_b - x_a
                pad_r_a = max(p.pad_height_mm for p in col_a) / 2
                pad_r_b = max(p.pad_height_mm for p in col_b) / 2
                usable = gap - (pad_r_a + config.clearance_mm) - (pad_r_b + config.clearance_mm)

                if usable < trace_pitch:
                    continue

                capacity = int(usable / trace_pitch)
                y_min = max(min(p.y_mm for p in col_a), min(p.y_mm for p in col_b))
                y_max = min(max(p.y_mm for p in col_a), max(p.y_mm for p in col_b))
                if y_max <= y_min:
                    continue

                channels.append(Channel(
                    x_min_mm=x_a + pad_r_a + config.clearance_mm,
                    y_min_mm=y_min - 1.0,
                    x_max_mm=x_b - pad_r_b - config.clearance_mm,
                    y_max_mm=y_max + 1.0,
                    axis="vertical",
                    capacity=capacity,
                    width_mm=usable,
                ))

    return channels


def _build_channel_pressure(
    channels: list[Channel],
    grid: "RoutingGrid",
    config: RouterConfig,
) -> dict[str, list[float]]:
    """Build static channel pressure grid.

    Cells inside narrow channels get higher pressure values (inversely
    proportional to capacity). A* uses this to prefer wider channels.
    """
    n_cells = grid.cols * grid.rows
    pressure: dict[str, list[float]] = {
        "top": [0.0] * n_cells,
        "bottom": [0.0] * n_cells,
    }
    res = grid.resolution

    for ch in channels:
        c_min = max(0, int(ch.x_min_mm / res))
        c_max = min(grid.cols - 1, int(ch.x_max_mm / res))
        r_min = max(0, int(ch.y_min_mm / res))
        r_max = min(grid.rows - 1, int(ch.y_max_mm / res))

        p = 1.0 / max(1, ch.capacity)
        for r in range(r_min, r_max + 1):
            base = r * grid.cols
            for c in range(c_min, c_max + 1):
                idx = base + c
                # Both layers — channel constrains routing on either layer
                for layer in ("top", "bottom"):
                    if p > pressure[layer][idx]:
                        pressure[layer][idx] = p

    return pressure


def order_nets_by_channel_pressure(
    nets: list[NetInfo],
    pad_map: dict[str, PadInfo],
    channels: list[Channel],
    net_id_map: dict[str, int],
    netlist: dict,
) -> list[NetInfo]:
    """Order nets so channel-constrained nets route first.

    For each net, compute a 'channel demand' score based on how many
    of its MST edges must cross narrow channels.
    """
    if not channels:
        return list(nets)

    # Build net_id -> list of pad positions
    net_pads: dict[str, list[tuple[float, float]]] = {}
    for pad in pad_map.values():
        if pad.net_id:
            net_pads.setdefault(pad.net_id, []).append((pad.x_mm, pad.y_mm))

    def _segment_crosses_channel(
        x1: float, y1: float, x2: float, y2: float, ch: Channel
    ) -> bool:
        """Check if line segment (x1,y1)-(x2,y2) crosses the channel bbox."""
        # Quick bbox rejection
        if max(x1, x2) < ch.x_min_mm or min(x1, x2) > ch.x_max_mm:
            return False
        if max(y1, y2) < ch.y_min_mm or min(y1, y2) > ch.y_max_mm:
            return False
        # At least one endpoint or the line crosses the channel region
        return True

    def _has_pads_both_sides(positions: list[tuple[float, float]], ch: Channel) -> bool:
        """Check if net has pads on both sides of the channel."""
        if ch.axis == "horizontal":
            above = any(y < ch.y_min_mm for _, y in positions)
            below = any(y > ch.y_max_mm for _, y in positions)
            return above and below
        else:
            left = any(x < ch.x_min_mm for x, _ in positions)
            right = any(x > ch.x_max_mm for x, _ in positions)
            return left and right

    # Score each net
    power_nets: list[NetInfo] = []
    signal_scores: list[tuple[float, NetInfo]] = []

    for net in nets:
        if net.net_class in ("power", "ground"):
            power_nets.append(net)
            continue

        positions = net_pads.get(net.net_id, [])
        if len(positions) < 2:
            signal_scores.append((0.0, net))
            continue

        # Compute MST edges
        mst_edges = compute_mst_edges(positions)
        score = 0.0
        for ia, ib, _ in mst_edges:
            x1, y1 = positions[ia]
            x2, y2 = positions[ib]
            for ch in channels:
                if _segment_crosses_channel(x1, y1, x2, y2, ch):
                    contribution = 1.0 / max(1, ch.capacity)
                    if _has_pads_both_sides(positions, ch):
                        contribution *= 2.0  # must cross — higher priority
                    score += contribution

        signal_scores.append((score, net))

    # Sort signals by descending channel demand (most constrained first)
    signal_scores.sort(key=lambda x: -x[0])
    return power_nets + [net for _, net in signal_scores]


# ---------------------------------------------------------------------------
# Push-and-shove routing
# ---------------------------------------------------------------------------

# Type alias for pad zone index: net_id_int -> list of (c_min, r_min, c_max, r_max, layer)
PadZoneIndex = dict[int, list[tuple[int, int, int, int, str]]]
# Snapshot for undo: (col, row, layer) -> old grid value
ShoveSnapshot = dict[tuple[int, int, str], int]


def _build_pad_zone_index(
    pad_map: dict[str, PadInfo],
    net_id_map: dict[str, int],
    clearance_mm: float,
    grid: RoutingGrid,
) -> PadZoneIndex:
    """Build index of pad+clearance bounding boxes per net for shove filtering.

    Returns a dict mapping each net_id_int to a list of grid-coordinate
    bounding boxes.  Used to distinguish pad cells (immovable) from trace
    cells (shovable) on the grid.
    """
    index: PadZoneIndex = {}
    for pad in pad_map.values():
        if pad.net_id is None or pad.net_id not in net_id_map:
            continue
        nid = net_id_map[pad.net_id]
        pw, ph = pad.pad_width_mm, pad.pad_height_mm
        is_th = pad.layer == "all"
        clr = clearance_mm
        comp_layer = pad.layer if pad.layer != "all" else "top"

        rects = index.setdefault(nid, [])
        if is_th:
            drill_radius = min(pw, ph) / 2
            for blk_layer in ["top", "bottom"]:
                if blk_layer == comp_layer:
                    c_min, r_min = grid.mm_to_grid(pad.x_mm - pw / 2 - clr, pad.y_mm - ph / 2 - clr)
                    c_max, r_max = grid.mm_to_grid(pad.x_mm + pw / 2 + clr, pad.y_mm + ph / 2 + clr)
                else:
                    c_min, r_min = grid.mm_to_grid(pad.x_mm - drill_radius - clr, pad.y_mm - drill_radius - clr)
                    c_max, r_max = grid.mm_to_grid(pad.x_mm + drill_radius + clr, pad.y_mm + drill_radius + clr)
                rects.append((c_min, r_min, c_max, r_max, blk_layer))
        else:
            c_min, r_min = grid.mm_to_grid(pad.x_mm - pw / 2 - clr, pad.y_mm - ph / 2 - clr)
            c_max, r_max = grid.mm_to_grid(pad.x_mm + pw / 2 + clr, pad.y_mm + ph / 2 + clr)
            rects.append((c_min, r_min, c_max, r_max, comp_layer))
    return index


def _is_pad_zone(
    col: int, row: int, layer: str, net_id_int: int, pzi: PadZoneIndex,
) -> bool:
    """Check if a grid cell is part of a pad+clearance zone (immovable)."""
    for c_min, r_min, c_max, r_max, l in pzi.get(net_id_int, []):
        if l == layer and c_min <= col <= c_max and r_min <= row <= r_max:
            return True
    return False


def _trace_segment_at(
    grid: RoutingGrid,
    col: int, row: int, layer: str,
    net_id_int: int,
    pzi: PadZoneIndex,
) -> list[tuple[int, int]]:
    """Trace a contiguous run of same-net non-pad cells through (col, row).

    Follows the dominant axis (determined by which neighbors have same-net
    cells) in both directions.  Returns ordered list of (col, row) cells
    forming the segment.  Returns empty list if the cell is a pad zone.
    """
    if _is_pad_zone(col, row, layer, net_id_int, pzi):
        return []

    # Find which neighbors are same-net trace cells
    directions: list[tuple[int, int]] = []
    for dc, dr in [(1, 0), (-1, 0), (0, 1), (0, -1),
                    (1, 1), (1, -1), (-1, 1), (-1, -1)]:
        nc, nr = col + dc, row + dr
        if (grid._in_bounds(nc, nr)
                and grid.get(nc, nr, layer) == net_id_int
                and not _is_pad_zone(nc, nr, layer, net_id_int, pzi)):
            directions.append((dc, dr))

    if not directions:
        return [(col, row)]  # isolated trace cell

    # Pick the dominant direction (first found axis; prefer orthogonal)
    dc, dr = directions[0]

    # Extend in both directions along (dc, dr)
    segment = [(col, row)]

    # Forward
    c, r = col + dc, row + dr
    while (grid._in_bounds(c, r)
           and grid.get(c, r, layer) == net_id_int
           and not _is_pad_zone(c, r, layer, net_id_int, pzi)):
        segment.append((c, r))
        c += dc
        r += dr

    # Backward
    c, r = col - dc, row - dr
    while (grid._in_bounds(c, r)
           and grid.get(c, r, layer) == net_id_int
           and not _is_pad_zone(c, r, layer, net_id_int, pzi)):
        segment.insert(0, (c, r))
        c -= dc
        r -= dr

    return segment


def _undo_shove(grid: RoutingGrid, snapshot: ShoveSnapshot) -> None:
    """Restore grid cells from a shove snapshot."""
    for (c, r, layer), old_val in snapshot.items():
        grid.layers[layer][r * grid.cols + c] = old_val


def _try_shove_segment(
    grid: RoutingGrid,
    segment: list[tuple[int, int]],
    layer: str,
    net_id_int: int,
    dc: int, dr: int,
    pzi: PadZoneIndex,
    depth: int,
    max_depth: int,
    snapshot: ShoveSnapshot,
    shoving_nets: set[int] | None = None,
) -> bool:
    """Try to shove a trace segment by (dc, dr) on the grid.

    Recursive: if the target position is occupied by another net's trace,
    tries to shove that segment too (up to max_depth).

    Applies changes directly to the grid.  On failure, caller must undo
    via snapshot.  Records first-seen old values in snapshot for undo.

    Returns True if the shove (and all cascaded shoves) succeeded.
    """
    if depth > max_depth:
        return False

    if shoving_nets is None:
        shoving_nets = set()
    shoving_nets = shoving_nets | {net_id_int}  # copy to avoid mutation across branches

    # Phase 1: check all target cells
    foreign_segments: list[tuple[int, list[tuple[int, int]], str]] = []  # (foreign_nid, segment, layer)
    seen_foreign: set[int] = set()

    for c, r in segment:
        tc, tr = c + dc, r + dr
        if not grid._in_bounds(tc, tr):
            return False
        val = grid.get(tc, tr, layer)
        if val == OBSTACLE:
            return False
        if val == EMPTY or val == net_id_int:
            continue  # ok — empty or our own net
        # Foreign net
        if val in shoving_nets:
            return False  # circular — already shoving this net
        if _is_pad_zone(tc, tr, layer, val, pzi):
            return False  # can't shove pads
        # Need to cascade-shove this foreign segment
        if val not in seen_foreign:
            seen_foreign.add(val)
            foreign_seg = _trace_segment_at(grid, tc, tr, layer, val, pzi)
            if not foreign_seg:
                return False
            foreign_segments.append((val, foreign_seg, layer))

    # Phase 2: recursively shove foreign segments first (they must move
    # BEFORE we move our segment, so our target cells become free)
    for f_nid, f_seg, f_layer in foreign_segments:
        if not _try_shove_segment(
            grid, f_seg, f_layer, f_nid, dc, dr, pzi,
            depth + 1, max_depth, snapshot, shoving_nets,
        ):
            return False

    # Phase 3: apply our shove — clear old, write new
    for c, r in segment:
        key = (c, r, layer)
        if key not in snapshot:
            snapshot[key] = grid.layers[layer][r * grid.cols + c]
        grid.layers[layer][r * grid.cols + c] = EMPTY

    for c, r in segment:
        tc, tr = c + dc, r + dr
        key = (tc, tr, layer)
        if key not in snapshot:
            snapshot[key] = grid.layers[layer][tr * grid.cols + tc]
        grid.layers[layer][tr * grid.cols + tc] = net_id_int

    return True


def astar_find_blockers_with_path(
    grid: RoutingGrid,
    start: tuple[int, int, str],
    end: tuple[int, int, str],
    net_id: int,
    half_width_cells: int = 0,
    via_cost: float = 10.0,
    diagonal: bool = True,
    foreign_net_cost: float = 500.0,
) -> tuple[list[tuple[int, int, str]], set[int]] | None:
    """Like astar_find_blockers but also returns the path.

    Returns (path, blocker_set) or None.
    """
    sc, sr, sl = start
    ec, er, el = end
    neighbors_set = _NEIGHBORS_8 if diagonal else _NEIGHBORS_4

    def heuristic(c: int, r: int, layer: str) -> float:
        dx, dy = abs(c - ec), abs(r - er)
        if diagonal:
            h = max(dx, dy) + 0.414 * min(dx, dy)
        else:
            h = dx + dy
        if layer != el:
            h += via_cost
        return h

    def step_cost_and_passable(col: int, row: int, layer: str) -> tuple[float, bool]:
        if half_width_cells == 0:
            val = grid.get(col, row, layer)
            if val == OBSTACLE:
                return (0.0, False)
            if val == EMPTY or val == net_id:
                return (0.0, True)
            return (foreign_net_cost, True)
        extra = 0.0
        for dc2 in range(-half_width_cells, half_width_cells + 1):
            for dr2 in range(-half_width_cells, half_width_cells + 1):
                val = grid.get(col + dc2, row + dr2, layer)
                if val == OBSTACLE:
                    return (0.0, False)
                if val != EMPTY and val != net_id:
                    extra = max(extra, foreign_net_cost)
        return (extra, True)

    other_layer = {"top": "bottom", "bottom": "top"}
    open_set: list[tuple[float, float, int, int, str]] = []
    heapq.heappush(open_set, (heuristic(sc, sr, sl), 0.0, sc, sr, sl))
    came_from: dict[tuple[int, int, str], tuple[int, int, str] | None] = {(sc, sr, sl): None}
    g_score: dict[tuple[int, int, str], float] = {(sc, sr, sl): 0.0}

    while open_set:
        f, g, col, row, layer = heapq.heappop(open_set)
        if col == ec and row == er and layer == el:
            # Reconstruct path and collect blockers
            path = []
            blockers: set[int] = set()
            node: tuple[int, int, str] | None = (col, row, layer)
            while node is not None:
                path.append(node)
                c, r, l = node
                if half_width_cells == 0:
                    val = grid.get(c, r, l)
                    if val > 0 and val != net_id:
                        blockers.add(val)
                else:
                    for dc2 in range(-half_width_cells, half_width_cells + 1):
                        for dr2 in range(-half_width_cells, half_width_cells + 1):
                            val = grid.get(c + dc2, r + dr2, l)
                            if val > 0 and val != net_id:
                                blockers.add(val)
                node = came_from[node]
            path.reverse()
            return (path, blockers)

        if g > g_score.get((col, row, layer), math.inf):
            continue

        for dc2, dr2, base_cost in neighbors_set:
            nc, nr = col + dc2, row + dr2
            if not grid._in_bounds(nc, nr):
                continue
            extra, passable = step_cost_and_passable(nc, nr, layer)
            if not passable:
                continue
            new_g = g + base_cost + extra
            if new_g < g_score.get((nc, nr, layer), math.inf):
                g_score[(nc, nr, layer)] = new_g
                heapq.heappush(open_set, (new_g + heuristic(nc, nr, layer), new_g, nc, nr, layer))
                came_from[(nc, nr, layer)] = (col, row, layer)

        other = other_layer[layer]
        if grid.can_place_via(col, row):
            extra, passable = step_cost_and_passable(col, row, other)
            if passable:
                new_g = g + via_cost + extra
                if new_g < g_score.get((col, row, other), math.inf):
                    g_score[(col, row, other)] = new_g
                    heapq.heappush(open_set, (new_g + heuristic(col, row, other), new_g, col, row, other))
                    came_from[(col, row, other)] = (col, row, layer)

    return None


def _shove_pass(
    grid: RoutingGrid,
    failed_nets: list[tuple[NetInfo, int]],
    result: RoutingResult,
    pad_map: dict[str, PadInfo],
    net_id_map: dict[str, int],
    int_to_net: dict[int, NetInfo],
    net_trace_widths: dict[str, float],
    pzi: PadZoneIndex,
    config: RouterConfig,
    netlist: dict,
    routed_net_ids: list[str],
    diagonal: bool,
    fill_net: "NetInfo | None" = None,
) -> list[tuple[NetInfo, int]]:
    """Push-and-shove pass: try to make room for failed nets by shifting
    blocking trace segments perpendicular by 1 grid cell.

    For each failed net:
    1. Find the blocker path (where conflicts are)
    2. For each conflict zone, try shoving blocking segments
    3. If shoves succeed, route the failed net
    4. Rebuild geometry for all affected nets

    Returns the list of nets that still failed after shoving.
    """
    still_failed: list[tuple[NetInfo, int]] = []

    for net, nid in failed_nets:
        tw = net_trace_widths[net.net_id]
        route_hw = int(math.ceil(tw / (2 * config.grid_resolution_mm))) - 1
        route_hw = max(0, route_hw)

        pads = _get_net_pads(net, pad_map, netlist)
        if len(pads) < 2:
            continue

        pad_positions = [(p.x_mm, p.y_mm) for p in pads]
        mst_edges = compute_mst_edges(pad_positions)

        # Collect all conflict zones across all MST edges
        all_conflict_cells: list[tuple[int, int, str, int]] = []  # (c, r, layer, foreign_nid)
        for ia, ib, _ in mst_edges:
            pad_a, pad_b = pads[ia], pads[ib]
            sc, sr = grid.mm_to_grid(pad_a.x_mm, pad_a.y_mm)
            ec, er = grid.mm_to_grid(pad_b.x_mm, pad_b.y_mm)
            start_layers = ["top", "bottom"] if pad_a.layer == "all" else [pad_a.layer]
            end_layers = ["top", "bottom"] if pad_b.layer == "all" else [pad_b.layer]

            best_result = None
            for sl in start_layers:
                for el in end_layers:
                    r = astar_find_blockers_with_path(
                        grid, (sc, sr, sl), (ec, er, el), nid,
                        half_width_cells=route_hw,
                        via_cost=config.via_cost, diagonal=diagonal,
                    )
                    if r is not None:
                        path, blockers = r
                        if best_result is None or len(blockers) < len(best_result[1]):
                            best_result = (path, blockers)
            if best_result is None:
                continue
            path, _ = best_result
            # Walk path, collect foreign-net cells
            for c, r, l in path:
                val = grid.get(c, r, l)
                if val > 0 and val != nid:
                    all_conflict_cells.append((c, r, l, val))

        if not all_conflict_cells:
            still_failed.append((net, nid))
            continue

        # Try shoving each conflict zone
        snapshot: ShoveSnapshot = {}
        shove_ok = True
        affected_nids: set[int] = set()

        # Group conflict cells by (layer, foreign_nid) to avoid duplicate segment traces
        seen_segments: set[tuple[int, int, str]] = set()
        for c, r, l, f_nid in all_conflict_cells:
            if (c, r, l) in seen_segments:
                continue
            if _is_pad_zone(c, r, l, f_nid, pzi):
                # Can't shove a pad — skip this conflict
                shove_ok = False
                break

            segment = _trace_segment_at(grid, c, r, l, f_nid, pzi)
            if not segment:
                shove_ok = False
                break

            for sc2, sr2 in segment:
                seen_segments.add((sc2, sr2, l))

            # Determine perpendicular directions to try
            # Segment direction: from first to last cell
            if len(segment) > 1:
                seg_dc = segment[-1][0] - segment[0][0]
                seg_dr = segment[-1][1] - segment[0][1]
            else:
                seg_dc, seg_dr = 1, 0  # default horizontal

            # Perpendicular candidates
            if seg_dc == 0:  # vertical segment → shove horizontally
                perp_dirs = [(1, 0), (-1, 0)]
            elif seg_dr == 0:  # horizontal segment → shove vertically
                perp_dirs = [(0, 1), (0, -1)]
            else:  # diagonal
                perp_dirs = [(-seg_dr, seg_dc), (seg_dr, -seg_dc)]
                # Normalize to unit steps
                perp_dirs = [(1 if d > 0 else -1 if d < 0 else 0,
                              1 if e > 0 else -1 if e < 0 else 0)
                             for d, e in perp_dirs]

            pushed = False
            for distance in range(1, 4):  # try 1, 2, 3 cell pushes
                if pushed:
                    break
                for pdc, pdr in perp_dirs:
                    trial_snapshot: ShoveSnapshot = {}
                    ok = _try_shove_segment(
                        grid, segment, l, f_nid,
                        pdc * distance, pdr * distance, pzi,
                        depth=0, max_depth=config.max_shove_depth,
                        snapshot=trial_snapshot,
                    )
                    if ok:
                        # Merge trial snapshot into main snapshot
                        for k, v in trial_snapshot.items():
                            if k not in snapshot:
                                snapshot[k] = v
                        affected_nids.add(f_nid)
                        # Also track cascaded nets
                        for (sc3, sr3, sl3), _ in trial_snapshot.items():
                            val = snapshot.get((sc3, sr3, sl3), grid.get(sc3, sr3, sl3))
                            if val > 0 and val != f_nid and val != nid:
                                affected_nids.add(val)
                        pushed = True
                        break
                    else:
                        _undo_shove(grid, trial_snapshot)

            if not pushed:
                shove_ok = False
                break

        if not shove_ok:
            _undo_shove(grid, snapshot)
            # Fallback: try ripping all conflict nets and re-routing
            conflict_nids = {f_nid for _, _, _, f_nid in all_conflict_cells}
            # Only attempt if small number of conflicts
            if len(conflict_nids) <= 3:
                ripped_for_shove: list[tuple[int, NetInfo]] = []
                for c_nid in conflict_nids:
                    c_net = int_to_net.get(c_nid)
                    if c_net and c_net.net_id in routed_net_ids:
                        if fill_net and c_net.net_id == fill_net.net_id:
                            continue
                        grid.clear_net(c_nid)
                        _restore_pad_markings(grid, c_net, pad_map, c_nid, config.clearance_mm, netlist)
                        result.traces = [t for t in result.traces if t.net_id != c_net.net_id]
                        result.vias = [v for v in result.vias if v.net_id != c_net.net_id]
                        routed_net_ids.remove(c_net.net_id)
                        ripped_for_shove.append((c_nid, c_net))

                outcome = route_net(grid, net, pad_map, nid, config, netlist, tw, diagonal=diagonal,
                                    via_cost_override=config.congestion_via_cost)
                if outcome is not None:
                    result.traces.extend(outcome[0])
                    result.vias.extend(outcome[1])
                    routed_net_ids.append(net.net_id)
                    # Re-route ripped nets
                    for r_nid, r_net in ripped_for_shove:
                        r_tw = net_trace_widths[r_net.net_id]
                        rebuild = route_net(grid, r_net, pad_map, r_nid, config, netlist, r_tw, diagonal=diagonal)
                        if rebuild is not None:
                            result.traces.extend(rebuild[0])
                            result.vias.extend(rebuild[1])
                            routed_net_ids.append(r_net.net_id)
                        else:
                            still_failed.append((r_net, r_nid))
                    continue
                else:
                    # Restore ripped nets
                    for r_nid, r_net in ripped_for_shove:
                        r_tw = net_trace_widths[r_net.net_id]
                        rebuild = route_net(grid, r_net, pad_map, r_nid, config, netlist, r_tw, diagonal=diagonal)
                        if rebuild is not None:
                            result.traces.extend(rebuild[0])
                            result.vias.extend(rebuild[1])
                            routed_net_ids.append(r_net.net_id)

            still_failed.append((net, nid))
            continue

        # Shoves succeeded — try routing the failed net
        outcome = route_net(grid, net, pad_map, nid, config, netlist, tw, diagonal=diagonal,
                            via_cost_override=config.congestion_via_cost)
        if outcome is None:
            _undo_shove(grid, snapshot)
            still_failed.append((net, nid))
            continue

        # Success! Add the new net
        result.traces.extend(outcome[0])
        result.vias.extend(outcome[1])
        routed_net_ids.append(net.net_id)

        # Rebuild geometry for all affected (shoved) nets
        for a_nid in affected_nids:
            a_net = int_to_net.get(a_nid)
            if a_net is None or a_net.net_id not in routed_net_ids:
                continue
            # Remove old traces/vias
            result.traces = [t for t in result.traces if t.net_id != a_net.net_id]
            result.vias = [v for v in result.vias if v.net_id != a_net.net_id]
            # Re-route to rebuild geometry (A* follows existing grid cells)
            a_tw = net_trace_widths[a_net.net_id]
            rebuild = route_net(grid, a_net, pad_map, a_nid, config, netlist, a_tw, diagonal=diagonal)
            if rebuild is not None:
                result.traces.extend(rebuild[0])
                result.vias.extend(rebuild[1])
            else:
                # Shoved net can't re-route — this shouldn't happen since cells
                # are still marked, but handle gracefully
                routed_net_ids.remove(a_net.net_id)
                still_failed.append((a_net, a_nid))

    return still_failed


# ---------------------------------------------------------------------------
# Copper fill (ground plane)
# ---------------------------------------------------------------------------

def _build_clearance_mask(
    grid: RoutingGrid,
    layer: str,
    fill_net_int: int,
    clearance_cells: int,
) -> list[bool]:
    """Build a boolean mask of cells that must NOT be filled due to clearance.

    Any cell occupied by a non-fill net or obstacle expands a circular clearance zone.
    """
    cols, rows = grid.cols, grid.rows
    forbidden = [False] * (cols * rows)
    r2 = clearance_cells * clearance_cells

    # Build circular kernel
    kernel: list[tuple[int, int]] = []
    for dc in range(-clearance_cells, clearance_cells + 1):
        for dr in range(-clearance_cells, clearance_cells + 1):
            if dc * dc + dr * dr <= r2:
                kernel.append((dc, dr))

    layer_data = grid.layers[layer]
    for row in range(rows):
        for col in range(cols):
            val = layer_data[row * cols + col]
            # Foreign net or obstacle → expand clearance
            if val == OBSTACLE or (val > 0 and val != fill_net_int):
                for dc, dr in kernel:
                    nc, nr = col + dc, row + dr
                    if 0 <= nc < cols and 0 <= nr < rows:
                        forbidden[nr * cols + nc] = True

    return forbidden


def _apply_thermal_relief(
    filled: list[bool],
    forbidden: list[bool],
    grid: RoutingGrid,
    layer: str,
    pad_map: dict[str, PadInfo],
    fill_net_id: str,
    config: RouterConfig,
) -> None:
    """Apply thermal relief patterns around fill-net pads.

    Clears an annular gap around each pad, then re-adds 4 cardinal spokes.
    Spokes respect the forbidden mask — they never fill cells that are in the
    clearance zone of non-fill features.  This prevents spokes from creating
    shorts with nearby pads of other nets.

    Modifies `filled` in place.
    """
    cols, rows = grid.cols, grid.rows
    res = config.grid_resolution_mm
    gap_cells = max(1, int(math.ceil(config.thermal_gap_mm / res)))
    spoke_hw = max(0, int(round(config.thermal_spoke_width_mm / (2 * res))))

    for pad in pad_map.values():
        if pad.net_id != fill_net_id:
            continue
        # TH pads ("all") exist on both layers; SMD pads only on their layer
        if pad.layer != "all" and pad.layer != layer:
            continue

        # Pad rectangle in grid coordinates
        pw, ph = pad.pad_width_mm, pad.pad_height_mm
        pc, pr = grid.mm_to_grid(pad.x_mm, pad.y_mm)
        hw = max(0, int(math.ceil(pw / (2 * res))))
        hh = max(0, int(math.ceil(ph / (2 * res))))

        # Clear annular gap (pad rect + gap, minus pad rect itself)
        outer_hw = hw + gap_cells
        outer_hh = hh + gap_cells
        for dc in range(-outer_hw, outer_hw + 1):
            for dr in range(-outer_hh, outer_hh + 1):
                nc, nr = pc + dc, pr + dr
                if 0 <= nc < cols and 0 <= nr < rows:
                    filled[nr * cols + nc] = False

        # Re-add 4 cardinal spokes extending through the gap.
        # Stop the spoke if we hit a forbidden cell (clearance zone of another
        # net's pad) to prevent copper-fill shorts.
        spoke_len = gap_cells + 2  # extend slightly beyond gap

        def _fill_spoke(dc_range, dr_range):
            for dr in dr_range:
                for dc in dc_range:
                    nc, nr = pc + dc, pr + dr
                    if 0 <= nc < cols and 0 <= nr < rows:
                        if not forbidden[nr * cols + nc]:
                            filled[nr * cols + nc] = True

        # North spoke (positive Y)
        _fill_spoke(range(-spoke_hw, spoke_hw + 1), range(-hh, hh + spoke_len + 1))
        # South spoke (negative Y)
        _fill_spoke(range(-spoke_hw, spoke_hw + 1), range(-(hh + spoke_len), hh + 1))
        # East spoke (positive X)
        _fill_spoke(range(-hw, hw + spoke_len + 1), range(-spoke_hw, spoke_hw + 1))
        # West spoke (negative X)
        _fill_spoke(range(-(hw + spoke_len), hw + 1), range(-spoke_hw, spoke_hw + 1))


def _remove_islands(
    filled: list[bool],
    grid: RoutingGrid,
    layer: str,
    fill_net_int: int,
) -> int:
    """Remove fill islands not connected to fill-net pads/traces.

    Uses BFS from cells that are both filled AND have the fill net ID on the grid.
    Returns number of island cells removed.
    """
    cols, rows = grid.cols, grid.rows
    total = cols * rows
    visited = [False] * total
    layer_data = grid.layers[layer]

    # BFS seeds: filled cells that are part of the fill net on the grid
    queue: deque[int] = deque()
    for idx in range(total):
        if filled[idx] and layer_data[idx] == fill_net_int:
            visited[idx] = True
            queue.append(idx)

    # BFS expand through filled cells
    while queue:
        idx = queue.popleft()
        col, row = idx % cols, idx // cols
        for dc, dr in [(0, 1), (0, -1), (1, 0), (-1, 0)]:
            nc, nr = col + dc, row + dr
            if 0 <= nc < cols and 0 <= nr < rows:
                nidx = nr * cols + nc
                if filled[nidx] and not visited[nidx]:
                    visited[nidx] = True
                    queue.append(nidx)

    # Remove unvisited filled cells (islands)
    removed = 0
    for idx in range(total):
        if filled[idx] and not visited[idx]:
            filled[idx] = False
            removed += 1
    return removed


def _bitmap_to_polygons(
    filled: list[bool],
    grid: RoutingGrid,
    layer: str,
) -> list[list[list[float]]]:
    """Convert fill bitmap to merged rectangles as polygon vertex arrays.

    Uses run-length encoding + vertical merging for clean output.
    Returns list of [[x1,y1], [x2,y2], [x3,y3], [x4,y4]] rectangles.
    """
    cols, rows = grid.cols, grid.rows
    res = grid.resolution

    # Phase 1: collect horizontal runs per row
    # Each run is (col_start, col_end_exclusive, row)
    runs: list[tuple[int, int, int]] = []
    for row in range(rows):
        col = 0
        while col < cols:
            if filled[row * cols + col]:
                start = col
                while col < cols and filled[row * cols + col]:
                    col += 1
                runs.append((start, col, row))
            else:
                col += 1

    # Phase 2: merge vertically adjacent runs with same x-span
    # Sort by (col_start, col_end, row) for grouping
    merged: list[tuple[int, int, int, int]] = []  # (col_start, col_end, row_start, row_end_exclusive)
    if not runs:
        return []

    runs.sort(key=lambda r: (r[0], r[1], r[2]))

    # Group runs by (col_start, col_end)
    i = 0
    while i < len(runs):
        cs, ce, row_start = runs[i]
        row_end = row_start + 1
        # Try to extend downward
        j = i + 1
        while j < len(runs):
            ncs, nce, nrow = runs[j]
            if ncs == cs and nce == ce and nrow == row_end:
                row_end = nrow + 1
                j += 1
            elif ncs == cs and nce == ce and nrow > row_end:
                break  # gap in rows
            elif ncs > cs or (ncs == cs and nce > ce):
                break  # different span
            else:
                j += 1
                continue
        merged.append((cs, ce, row_start, row_end))
        i = j

    # Phase 3: convert to polygon vertex arrays in mm coordinates
    polygons: list[list[list[float]]] = []
    for cs, ce, rs, re in merged:
        x_min = cs * res
        x_max = ce * res
        y_min = rs * res
        y_max = re * res
        polygons.append([
            [round(x_min, 3), round(y_min, 3)],
            [round(x_max, 3), round(y_min, 3)],
            [round(x_max, 3), round(y_max, 3)],
            [round(x_min, 3), round(y_max, 3)],
        ])

    return polygons


def _add_stitching_vias(
    filled_top: list[bool],
    filled_bottom: list[bool],
    grid: RoutingGrid,
    fill_net_int: int,
    config: RouterConfig,
) -> list[Via]:
    """Smart stitching vias: only place vias where they connect otherwise-
    isolated GND fill regions between layers.

    Strategy:
    1. Find fill-net pads/traces that seed connectivity on each layer
    2. BFS to find connected fill regions per layer
    3. Place stitching vias only where they bridge disconnected fill regions
    4. Space vias at least ~5mm apart in already-connected areas

    Returns list of Via objects for the output.
    """
    cols, rows = grid.cols, grid.rows
    res = config.grid_resolution_mm
    total = cols * rows

    via_radius_cells = max(1, int(math.ceil(
        config.via_diameter_mm / (2 * res)
    )))

    def _cell_clear(col: int, row: int) -> bool:
        """Check if a via can be placed at (col, row) without hitting foreign nets."""
        for dc in range(-via_radius_cells, via_radius_cells + 1):
            for dr in range(-via_radius_cells, via_radius_cells + 1):
                nc, nr = col + dc, row + dr
                if not grid._in_bounds(nc, nr):
                    return False
                for layer in ("top", "bottom"):
                    val = grid.get(nc, nr, layer)
                    if val != EMPTY and val != fill_net_int:
                        return False
        return True

    # Find candidate positions: fill on both layers and clear of foreign nets
    # Use a sparser grid (~5mm) to avoid excessive vias
    stitch_spacing = max(4, int(round(5.0 / res)))
    candidates: list[tuple[int, int]] = []
    for row in range(via_radius_cells, rows - via_radius_cells, stitch_spacing):
        for col in range(via_radius_cells, cols - via_radius_cells, stitch_spacing):
            idx = row * cols + col
            if filled_top[idx] and filled_bottom[idx] and _cell_clear(col, row):
                candidates.append((col, row))

    if not candidates:
        return []

    # BFS to find fill connectivity on each layer from fill-net features
    # A fill region is "seeded" if it touches a fill-net pad or trace
    def _bfs_fill(filled: list[bool], layer: str) -> list[int]:
        """Return component IDs for each cell. -1 = not filled, 0+ = component."""
        comp = [-1] * total
        comp_id = 0
        for idx in range(total):
            if not filled[idx] or comp[idx] >= 0:
                continue
            # BFS from this cell
            queue = deque([idx])
            comp[idx] = comp_id
            while queue:
                cidx = queue.popleft()
                cr, cc = divmod(cidx, cols)
                cc = cidx - cr * cols
                for dc, dr in ((0, 1), (0, -1), (1, 0), (-1, 0)):
                    nc, nr = cc + dc, cr + dr
                    if 0 <= nc < cols and 0 <= nr < rows:
                        nidx = nr * cols + nc
                        if filled[nidx] and comp[nidx] < 0:
                            comp[nidx] = comp_id
                            queue.append(nidx)
            comp_id += 1
        return comp

    top_comp = _bfs_fill(filled_top, "top")
    bot_comp = _bfs_fill(filled_bottom, "bottom")

    # Find which components contain fill-net seeds (pads/traces)
    seeded_top: set[int] = set()
    seeded_bot: set[int] = set()
    for idx in range(total):
        r, c = divmod(idx, cols)
        c = idx - r * cols
        for layer, comp, seeded in [("top", top_comp, seeded_top), ("bottom", bot_comp, seeded_bot)]:
            if comp[idx] >= 0:
                val = grid.get(c, r, layer)
                if val == fill_net_int:
                    seeded.add(comp[idx])

    # Place vias where they connect:
    # 1. A seeded top component to an unseeded bottom component (or vice versa)
    # 2. Two different seeded components (helps redundancy)
    # Skip if both components are already the same seeded component
    vias: list[Via] = []
    connected_pairs: set[tuple[int, int]] = set()  # (top_comp, bot_comp) already bridged

    for col, row in candidates:
        idx = row * cols + col
        tc = top_comp[idx]
        bc = bot_comp[idx]
        if tc < 0 or bc < 0:
            continue

        top_seeded = tc in seeded_top
        bot_seeded = bc in seeded_bot

        # Skip if both sides already seeded and already bridged
        pair = (tc, bc)
        if top_seeded and bot_seeded and pair in connected_pairs:
            continue

        # Place via if it bridges connectivity
        needs_via = False
        if top_seeded and not bot_seeded:
            needs_via = True  # extends connectivity to bottom
        elif bot_seeded and not top_seeded:
            needs_via = True  # extends connectivity to top
        elif top_seeded and bot_seeded and pair not in connected_pairs:
            needs_via = True  # first bridge between these two seeded components

        if needs_via:
            x_mm, y_mm = grid.grid_to_mm(col, row)
            vias.append(Via(
                x_mm=x_mm, y_mm=y_mm,
                drill_mm=config.via_drill_mm,
                diameter_mm=config.via_diameter_mm,
                from_layer="top", to_layer="bottom",
                net_id="", net_name="",  # filled in by caller
            ))
            connected_pairs.add(pair)
            # After placing a via, merge connectivity: mark bottom component as seeded
            if top_seeded:
                seeded_bot.add(bc)
            if bot_seeded:
                seeded_top.add(tc)

    return vias


def _remove_islands_cross_layer(
    filled_top: list[bool],
    filled_bottom: list[bool],
    grid: RoutingGrid,
    fill_net_int: int,
    stitch_vias: list[Via],
) -> int:
    """Remove fill islands not connected to fill-net features, with cross-layer connectivity.

    BFS seeds from fill-net pads/traces on either layer. Stitching vias provide
    cross-layer connections between top and bottom fill.

    Modifies both fill bitmaps in place. Returns total cells removed.
    """
    cols, rows = grid.cols, grid.rows
    total = cols * rows

    # Visited arrays per layer: 0=top, 1=bottom
    visited = [
        [False] * total,  # top
        [False] * total,  # bottom
    ]
    filled = [filled_top, filled_bottom]
    layer_data = [grid.layers["top"], grid.layers["bottom"]]
    layer_idx = {"top": 0, "bottom": 1}

    # BFS queue: (idx, layer_index)
    queue: deque[tuple[int, int]] = deque()

    # Seed from fill-net pads/traces on both layers
    for li in range(2):
        for idx in range(total):
            if filled[li][idx] and layer_data[li][idx] == fill_net_int:
                if not visited[li][idx]:
                    visited[li][idx] = True
                    queue.append((idx, li))

    # Build stitching via lookup: idx -> set of layers
    stitch_map: dict[int, set[int]] = {}
    for via in stitch_vias:
        vc, vr = grid.mm_to_grid(via.x_mm, via.y_mm)
        if 0 <= vc < cols and 0 <= vr < rows:
            vidx = vr * cols + vc
            stitch_map.setdefault(vidx, set()).update([0, 1])

    # BFS
    while queue:
        idx, li = queue.popleft()
        col, row = idx % cols, idx // cols

        # Same-layer neighbors
        for dc, dr in [(0, 1), (0, -1), (1, 0), (-1, 0)]:
            nc, nr = col + dc, row + dr
            if 0 <= nc < cols and 0 <= nr < rows:
                nidx = nr * cols + nc
                if filled[li][nidx] and not visited[li][nidx]:
                    visited[li][nidx] = True
                    queue.append((nidx, li))

        # Cross-layer via stitching vias
        if idx in stitch_map:
            other_li = 1 - li
            if other_li in stitch_map[idx] and filled[other_li][idx] and not visited[other_li][idx]:
                visited[other_li][idx] = True
                queue.append((idx, other_li))

    # Remove unvisited filled cells (islands)
    removed = 0
    for li in range(2):
        for idx in range(total):
            if filled[li][idx] and not visited[li][idx]:
                filled[li][idx] = False
                removed += 1
    return removed


def _add_rescue_vias(
    filled_top: list[bool],
    filled_bottom: list[bool],
    grid: RoutingGrid,
    fill_net_int: int,
    config: RouterConfig,
) -> list[Via]:
    """Find top-layer fill islands disconnected from GND and add rescue vias.

    Strategy:
    1. Find connected components (islands) on the top fill bitmap
    2. Identify which are already connected to GND (have fill_net_int cells)
    3. For disconnected islands, find the best cell that also has bottom fill
       and place a rescue via there — connecting the island through the bottom plane
    4. Only place a via if the island is large enough to be worth saving (≥4 cells)

    Returns list of rescue Via objects.
    """
    cols, rows = grid.cols, grid.rows
    total = cols * rows
    top_data = grid.layers["top"]

    # Find connected components on top layer
    component_id = [-1] * total  # which component each cell belongs to
    components: list[list[int]] = []  # list of cell indices per component

    for idx in range(total):
        if filled_top[idx] and component_id[idx] == -1:
            # BFS to find this connected component
            cid = len(components)
            cells: list[int] = []
            bfs_q: deque[int] = deque([idx])
            component_id[idx] = cid
            while bfs_q:
                cidx = bfs_q.popleft()
                cells.append(cidx)
                col, row = cidx % cols, cidx // cols
                for dc, dr in [(0, 1), (0, -1), (1, 0), (-1, 0)]:
                    nc, nr = col + dc, row + dr
                    if 0 <= nc < cols and 0 <= nr < rows:
                        nidx = nr * cols + nc
                        if filled_top[nidx] and component_id[nidx] == -1:
                            component_id[nidx] = cid
                            bfs_q.append(nidx)
            components.append(cells)

    # Identify which components are connected to GND
    connected_cids: set[int] = set()
    for idx in range(total):
        if filled_top[idx] and top_data[idx] == fill_net_int:
            cid = component_id[idx]
            if cid >= 0:
                connected_cids.add(cid)

    # For each disconnected island, try to place a rescue via
    vias: list[Via] = []
    min_island_cells = 4  # don't rescue tiny slivers

    for cid, cells in enumerate(components):
        if cid in connected_cids:
            continue  # already connected
        if len(cells) < min_island_cells:
            continue  # too small to bother

        # Find cells in this island where bottom fill also exists (via candidate)
        candidates: list[tuple[int, float]] = []
        # Compute island centroid for picking the most central candidate
        cx = sum(idx % cols for idx in cells) / len(cells)
        cy = sum(idx // cols for idx in cells) / len(cells)

        for idx in cells:
            if filled_bottom[idx]:
                col, row = idx % cols, idx // cols
                # Prefer candidates away from edges (at least 1 cell from island boundary)
                dist_to_center = abs(col - cx) + abs(row - cy)
                candidates.append((idx, dist_to_center))

        if not candidates:
            continue  # no bottom fill underneath — island can't be rescued

        # Pick the candidate closest to the island centroid
        candidates.sort(key=lambda x: x[1])
        best_idx = candidates[0][0]
        best_col, best_row = best_idx % cols, best_idx // cols
        x_mm, y_mm = grid.grid_to_mm(best_col, best_row)

        vias.append(Via(
            x_mm=x_mm, y_mm=y_mm,
            drill_mm=config.via_drill_mm,
            diameter_mm=config.via_diameter_mm,
            from_layer="top", to_layer="bottom",
            net_id="", net_name="",  # filled in by caller
        ))

    return vias


def create_copper_fill(
    grid: RoutingGrid,
    fill_net_int: int,
    fill_net_id: str,
    fill_net_name: str,
    pad_map: dict[str, PadInfo],
    config: RouterConfig,
) -> tuple[list[dict], list[Via]]:
    """Generate copper fill on both layers for the specified net.

    Called AFTER routing is complete. Fills unused grid cells, applies clearance
    from foreign nets, thermal relief around fill-net pads, adds stitching vias,
    and removes islands using cross-layer connectivity.

    Returns (fill_regions, stitching_vias) for output JSON.
    """
    res = config.grid_resolution_mm
    # Add +1 guard cell to absorb worst-case grid-quantization error (up to
    # 0.5 cells lost across the clearance zone when pad/trace coordinates
    # don't fall on grid boundaries).
    clearance_cells = max(2, int(math.ceil(config.fill_clearance_mm / res)) + 1)
    cols, rows = grid.cols, grid.rows

    # Phase 1: Build fill bitmaps for both layers
    fill_bitmaps: dict[str, list[bool]] = {}
    for layer in ["top", "bottom"]:
        layer_data = grid.layers[layer]
        forbidden = _build_clearance_mask(grid, layer, fill_net_int, clearance_cells)

        filled = [False] * (cols * rows)
        for idx in range(cols * rows):
            if not forbidden[idx]:
                val = layer_data[idx]
                if val == EMPTY or val == fill_net_int:
                    filled[idx] = True

        # Apply thermal relief (pass forbidden mask so spokes don't violate clearance)
        _apply_thermal_relief(filled, forbidden, grid, layer, pad_map, fill_net_id, config)
        fill_bitmaps[layer] = filled

    # Phase 2: Add stitching vias where fill exists on both layers
    stitch_vias = _add_stitching_vias(
        fill_bitmaps["top"], fill_bitmaps["bottom"], grid, fill_net_int, config,
    )
    # Set net info on stitching vias
    for v in stitch_vias:
        v.net_id = fill_net_id
        v.net_name = fill_net_name

    # Phase 2b: Rescue vias — connect top-layer islands through bottom fill
    rescue_vias = _add_rescue_vias(
        fill_bitmaps["top"], fill_bitmaps["bottom"],
        grid, fill_net_int, config,
    )
    for v in rescue_vias:
        v.net_id = fill_net_id
        v.net_name = fill_net_name
    stitch_vias.extend(rescue_vias)

    # Phase 3: Remove islands with cross-layer connectivity
    _remove_islands_cross_layer(
        fill_bitmaps["top"], fill_bitmaps["bottom"],
        grid, fill_net_int, stitch_vias,
    )

    # Phase 4: Convert to polygons
    results: list[dict] = []
    for layer in ["top", "bottom"]:
        polygons = _bitmap_to_polygons(fill_bitmaps[layer], grid, layer)
        if polygons:
            results.append({
                "layer": layer,
                "net_id": fill_net_id,
                "net_name": fill_net_name,
                "polygons": polygons,
            })

    return results, stitch_vias


def _apply_pre_fill(
    grid: RoutingGrid,
    fill_net_int: int,
    fill_net_id: str,
    pad_map: dict[str, PadInfo],
    config: RouterConfig,
) -> None:
    """Commit GND copper fill to the routing grid BEFORE signal routing.

    On 2-layer boards, only pre-fills the bottom layer (standard ground plane
    practice). This keeps the top layer clear for signal routing while giving
    GND pads bottom-layer connectivity via the fill plane. Signals can still
    use the bottom layer through vias when needed.

    Marks empty cells with fill_net_int so the A* router treats them as
    occupied. Applies clearance around non-GND features and thermal relief
    around GND pads.
    """
    res = config.grid_resolution_mm
    clearance_cells = max(2, int(math.ceil(config.fill_clearance_mm / res)) + 1)
    cols, rows = grid.cols, grid.rows

    # On 2-layer boards, only pre-fill bottom layer (top stays clear for signals).
    # On 4+ layer boards (future), could fill both inner layers.
    pre_fill_layers = ["bottom"]

    for layer in pre_fill_layers:
        layer_data = grid.layers[layer]

        # Build forbidden mask (clearance zones around non-GND features)
        forbidden = _build_clearance_mask(grid, layer, fill_net_int, clearance_cells)

        # Fill empty cells that aren't in forbidden zones
        for idx in range(cols * rows):
            if not forbidden[idx] and layer_data[idx] == EMPTY:
                layer_data[idx] = fill_net_int

        # Build filled bitmap from grid state (for thermal relief)
        filled = [layer_data[idx] == fill_net_int for idx in range(cols * rows)]

        # Apply thermal relief — clears annular gap, re-adds cardinal spokes
        _apply_thermal_relief(filled, forbidden, grid, layer, pad_map, fill_net_id, config)

        # Sync thermal relief changes back to grid:
        # Where thermal relief cleared fill, reset grid cell to EMPTY
        for idx in range(cols * rows):
            if layer_data[idx] == fill_net_int and not filled[idx]:
                layer_data[idx] = EMPTY


def _clear_pre_fill(
    grid: RoutingGrid,
    fill_net_int: int,
    pad_map: dict[str, PadInfo],
    fill_net_id: str,
) -> None:
    """Remove pre-filled GND cells from the routing grid (undo pre-fill).

    Resets cells marked with fill_net_int back to EMPTY, except for cells
    that correspond to actual GND pad locations.
    """
    cols = grid.cols

    # Build set of grid cells that are GND pad locations (keep these)
    pad_cells: set[tuple[str, int]] = set()
    for pad in pad_map.values():
        if pad.net_id != fill_net_id:
            continue
        layers = ["top", "bottom"] if pad.layer == "all" else [pad.layer]
        pc, pr = grid.mm_to_grid(pad.x_mm, pad.y_mm)
        for layer in layers:
            pad_cells.add((layer, pr * cols + pc))

    for layer in ["top", "bottom"]:
        layer_data = grid.layers[layer]
        for idx in range(len(layer_data)):
            if layer_data[idx] == fill_net_int and (layer, idx) not in pad_cells:
                layer_data[idx] = EMPTY


# ---------------------------------------------------------------------------
# Top-level routing
# ---------------------------------------------------------------------------

def _route_with_ordering(
    net_order: list[NetInfo],
    grid: RoutingGrid,
    pad_map: dict[str, PadInfo],
    net_id_map: dict[str, int],
    net_trace_widths: dict[str, float],
    fill_net: NetInfo | None,
    config: RouterConfig,
    netlist: dict,
    diagonal: bool = True,
    channel_pressure: dict[str, list[float]] | None = None,
) -> tuple[RoutingResult, list[str], list[tuple[NetInfo, int]]]:
    """Route all nets in the given order on a fresh grid.

    Returns (result, routed_net_ids, failed_nets).
    Does NOT do rip-up/retry or relaxed clearance — just a single ordering pass.
    """
    result = RoutingResult(trace_width_overrides={})
    routed_net_ids: list[str] = []
    failed_nets: list[tuple[NetInfo, int]] = []

    for net in net_order:
        if fill_net and net.net_id == fill_net.net_id:
            routed_net_ids.append(net.net_id)
            continue

        nid = net_id_map[net.net_id]
        tw = net_trace_widths[net.net_id]

        outcome = route_net(
            grid, net, pad_map, nid, config, netlist, tw,
            diagonal=diagonal, channel_pressure=channel_pressure,
        )
        if outcome is None:
            failed_nets.append((net, nid))
        else:
            traces, vias = outcome
            result.traces.extend(traces)
            result.vias.extend(vias)
            routed_net_ids.append(net.net_id)

    return result, routed_net_ids, failed_nets


def _negotiated_congestion_route(
    net_order: list[NetInfo],
    board_w: float,
    board_h: float,
    placement: dict,
    pad_map: dict[str, PadInfo],
    net_id_map: dict[str, int],
    net_trace_widths: dict[str, float],
    fill_net: NetInfo | None,
    config: RouterConfig,
    netlist: dict,
    diagonal: bool = True,
    channel_pressure: dict[str, list[float]] | None = None,
) -> tuple[RoutingResult, list[str], list[tuple[NetInfo, int]], RoutingGrid]:
    """Negotiated congestion routing (PathFinder algorithm).

    Iteratively routes ALL nets, allowing overlaps, then increases congestion
    costs on overused cells until the solution converges (no overlaps).

    Returns (result, routed_net_ids, failed_nets, grid).
    """
    import time as _time

    # Build a fresh base grid (pads + obstacles only)
    base_grid = RoutingGrid(board_w, board_h, config.grid_resolution_mm)
    _setup_grid(base_grid, placement, pad_map, config.clearance_mm, net_id_map)
    if config.pre_fill_enabled and fill_net and fill_net.net_id in net_id_map:
        _apply_pre_fill(base_grid, net_id_map[fill_net.net_id], fill_net.net_id, pad_map, config)
    base_snap = base_grid.snapshot()

    n_cells = base_grid.cols * base_grid.rows

    # History cost: accumulates over iterations for overused cells
    history_cost: dict[str, list[float]] = {
        "top": [0.0] * n_cells,
        "bottom": [0.0] * n_cells,
    }

    # Seed history costs with channel pressure (warm start)
    if channel_pressure is not None and config.channel_ncr_seed_factor > 0:
        seed = config.ncr_hfac_initial * config.channel_ncr_seed_factor
        for layer_name in ("top", "bottom"):
            cp = channel_pressure.get(layer_name)
            if cp is not None:
                for i in range(min(n_cells, len(cp))):
                    if cp[i] > 0:
                        history_cost[layer_name][i] += cp[i] * seed

    hfac = config.ncr_hfac_initial

    # Track best legal solution seen across iterations
    best_result: RoutingResult | None = None
    best_routed: list[str] = []
    best_failed: list[tuple[NetInfo, int]] = []
    best_grid: RoutingGrid | None = None
    best_legal_count = -1
    stagnation_count = 0

    print(f"  NCR: starting PathFinder ({config.ncr_max_iterations} max iterations, "
          f"{len(net_order)} nets)")
    t0 = _time.time()

    for iteration in range(config.ncr_max_iterations):
        # Restore grid to base state (pads + obstacles only)
        base_grid.restore(base_snap)

        # Present occupancy: tracks how many nets claim each cell this iteration
        present_occupancy: dict[str, list[int]] = {
            "top": [0] * n_cells,
            "bottom": [0] * n_cells,
        }

        # Adaptive via cost: decreases as iterations progress, drops on stagnation
        progress = iteration / max(1, config.ncr_max_iterations - 1)
        adaptive_via = max(
            config.ncr_via_cost_min,
            config.via_cost * (1.0 - 0.7 * progress),
        )
        if stagnation_count >= config.ncr_stagnation_limit:
            adaptive_via = config.ncr_via_cost_min

        # Route all nets with congestion-aware A*
        net_paths: dict[str, list[tuple[int, int, str]]] = {}
        net_results: dict[str, tuple[list[TraceSegment], list[Via]]] = {}
        routed_this_iter: list[str] = []
        failed_this_iter: list[tuple[NetInfo, int]] = []

        for net in net_order:
            if fill_net and net.net_id == fill_net.net_id:
                routed_this_iter.append(net.net_id)
                continue

            nid = net_id_map[net.net_id]
            tw = net_trace_widths[net.net_id]

            outcome = route_net_congestion(
                base_grid, net, pad_map, nid, config, netlist, tw,
                diagonal=diagonal,
                via_cost_override=adaptive_via,
                history_cost=history_cost,
                present_occupancy=present_occupancy,
                hfac=hfac,
                pfac=config.ncr_pfac,
                channel_pressure=channel_pressure,
            )

            if outcome is None:
                failed_this_iter.append((net, nid))
                continue

            traces, vias, path_cells = outcome
            net_paths[net.net_id] = path_cells
            net_results[net.net_id] = (traces, vias)
            routed_this_iter.append(net.net_id)

            # Mark path on grid so subsequent nets see this net's traces
            # (critical for congestion detection within this iteration)
            block_hw = int(math.ceil((tw / 2 + config.clearance_mm) / config.grid_resolution_mm))
            via_radius = config.via_diameter_mm / 2
            via_block_hw = int(math.ceil((via_radius + tw / 2 + config.clearance_mm) / config.grid_resolution_mm))
            via_block_hw = max(block_hw, via_block_hw)
            mark_path_on_grid(base_grid, path_cells, nid, block_hw, via_block_hw)

            # Update present occupancy — track center cells only (not expanded
            # blocking zone) to avoid false overuse from clearance overlap
            for col, row, layer in path_cells:
                if base_grid._in_bounds(col, row):
                    idx = row * base_grid.cols + col
                    present_occupancy[layer][idx] += 1

        # Count overused cells (occupancy > 1)
        overused = 0
        for layer_name in ["top", "bottom"]:
            for i in range(n_cells):
                if present_occupancy[layer_name][i] > 1:
                    overused += 1

        # Count how many nets were routed legally (no path overlaps)
        # A net is "legal" if none of its cells are overused
        legal_nets: list[str] = []
        illegal_nets: list[tuple[NetInfo, int]] = []
        for net in net_order:
            if fill_net and net.net_id == fill_net.net_id:
                legal_nets.append(net.net_id)
                continue
            if net.net_id not in net_paths:
                nid = net_id_map[net.net_id]
                illegal_nets.append((net, nid))
                continue
            # Check if any cell in this net's path is overused
            is_legal = True
            for col, row, layer in net_paths[net.net_id]:
                idx = row * base_grid.cols + col
                if base_grid._in_bounds(col, row) and present_occupancy[layer][idx] > 1:
                    is_legal = False
                    break
            if is_legal:
                legal_nets.append(net.net_id)
            else:
                nid = net_id_map[net.net_id]
                illegal_nets.append((net, nid))

        elapsed = _time.time() - t0
        print(f"  NCR iter {iteration}: {len(routed_this_iter)} routed, "
              f"{overused} overused cells, {len(legal_nets)} legal nets "
              f"[{elapsed:.1f}s]")

        if overused == 0:
            # Converged! All routes are legal.
            # Commit paths to a fresh grid
            final_grid = RoutingGrid(board_w, board_h, config.grid_resolution_mm)
            _setup_grid(final_grid, placement, pad_map, config.clearance_mm, net_id_map)
            final_result = RoutingResult(trace_width_overrides={})
            final_routed: list[str] = []
            final_failed: list[tuple[NetInfo, int]] = []

            for net in net_order:
                if fill_net and net.net_id == fill_net.net_id:
                    final_routed.append(net.net_id)
                    continue
                if net.net_id in net_results:
                    # Re-route on clean grid to get proper blocking
                    nid = net_id_map[net.net_id]
                    tw = net_trace_widths[net.net_id]
                    outcome = route_net(
                        final_grid, net, pad_map, nid, config, netlist, tw,
                        diagonal=diagonal,
                    )
                    if outcome is not None:
                        final_result.traces.extend(outcome[0])
                        final_result.vias.extend(outcome[1])
                        final_routed.append(net.net_id)
                    else:
                        final_failed.append((net, nid))
                else:
                    nid = net_id_map[net.net_id]
                    final_failed.append((net, nid))

            print(f"  NCR converged at iteration {iteration}: "
                  f"{len(final_routed)}/{len(net_order)} nets")
            return final_result, final_routed, final_failed, final_grid

        # Track best legal count for fallback
        if len(legal_nets) > best_legal_count:
            best_legal_count = len(legal_nets)
            best_routed = list(legal_nets)
            best_failed = list(illegal_nets)
            stagnation_count = 0
        else:
            stagnation_count += 1

        # Update history costs for overused cells
        overuse_boost = 1.0
        if stagnation_count >= config.ncr_stagnation_limit:
            overuse_boost = 2.0  # exponential boost when stagnating
            stagnation_count = 0
        for layer_name in ["top", "bottom"]:
            for i in range(n_cells):
                if present_occupancy[layer_name][i] > 1:
                    history_cost[layer_name][i] += hfac * overuse_boost

        hfac += config.ncr_hfac_increment

        # Dynamic reordering: route illegal (congested) nets first next iteration
        # so they get priority access to scarce routing resources
        if illegal_nets:
            illegal_ids = {n.net_id for n, _ in illegal_nets}
            # Reorder: illegal nets first (preserving relative order), then legal
            illegal_order = [n for n in net_order if n.net_id in illegal_ids]
            legal_order = [n for n in net_order if n.net_id not in illegal_ids]
            net_order = illegal_order + legal_order

    # Did not fully converge — commit best legal solution
    print(f"  NCR: did not converge after {config.ncr_max_iterations} iterations, "
          f"committing best ({best_legal_count} legal nets)")

    # Build result by routing only the legal nets on a fresh grid
    final_grid = RoutingGrid(board_w, board_h, config.grid_resolution_mm)
    _setup_grid(final_grid, placement, pad_map, config.clearance_mm, net_id_map)
    final_result = RoutingResult(trace_width_overrides={})
    final_routed: list[str] = []
    final_failed: list[tuple[NetInfo, int]] = []

    # Route in order, but only attempt nets that were in the "legal" set
    legal_set = set(best_routed)
    for net in net_order:
        if fill_net and net.net_id == fill_net.net_id:
            final_routed.append(net.net_id)
            continue
        nid = net_id_map[net.net_id]
        if net.net_id in legal_set:
            tw = net_trace_widths[net.net_id]
            outcome = route_net(
                final_grid, net, pad_map, nid, config, netlist, tw,
                diagonal=diagonal,
            )
            if outcome is not None:
                final_result.traces.extend(outcome[0])
                final_result.vias.extend(outcome[1])
                final_routed.append(net.net_id)
            else:
                final_failed.append((net, nid))
        else:
            final_failed.append((net, nid))

    # Try routing the remaining failed nets (they might fit now)
    still_failed: list[tuple[NetInfo, int]] = []
    for net, nid in final_failed:
        tw = net_trace_widths[net.net_id]
        outcome = route_net(
            final_grid, net, pad_map, nid, config, netlist, tw,
            diagonal=diagonal,
        )
        if outcome is not None:
            final_result.traces.extend(outcome[0])
            final_result.vias.extend(outcome[1])
            final_routed.append(net.net_id)
        else:
            still_failed.append((net, nid))

    return final_result, final_routed, still_failed, final_grid


def _check_net_connectivity(
    net_id: str,
    pads: list[PadInfo],
    traces: list[TraceSegment],
    vias: list[Via],
    grid_res: float,
) -> bool:
    """Check if all pads of a net are connected through traces/vias.

    Uses union-find on trace endpoints and via positions.
    Returns True if all pads are in one connected component.
    """
    if len(pads) < 2:
        return True

    # Assign indices to positions via spatial hashing
    snap = max(0.3, grid_res * 2.0)
    parent: dict[int, int] = {}

    def _pid(x: float, y: float, layer: str) -> int:
        """Quantize position to a spatial bucket."""
        qx = round(x / snap)
        qy = round(y / snap)
        lv = 0 if layer == "top" else 1
        return hash((qx, qy, lv))

    def find(a: int) -> int:
        while parent.get(a, a) != a:
            parent[a] = parent.get(parent.get(a, a), parent.get(a, a))
            a = parent[a]
        return a

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    # Union trace segment endpoints
    net_traces = [t for t in traces if t.net_id == net_id]
    for t in net_traces:
        p1 = _pid(t.start_x_mm, t.start_y_mm, t.layer)
        p2 = _pid(t.end_x_mm, t.end_y_mm, t.layer)
        parent.setdefault(p1, p1)
        parent.setdefault(p2, p2)
        union(p1, p2)

    # Union via connections (top↔bottom at same position)
    net_vias = [v for v in vias if v.net_id == net_id]
    for v in net_vias:
        pt = _pid(v.x_mm, v.y_mm, "top")
        pb = _pid(v.x_mm, v.y_mm, "bottom")
        parent.setdefault(pt, pt)
        parent.setdefault(pb, pb)
        union(pt, pb)

    # Check that all pads belong to the same component
    pad_roots: set[int] = set()
    for p in pads:
        layers = ["top", "bottom"] if p.layer == "all" else [p.layer]
        best_pid = None
        for layer in layers:
            pid = _pid(p.x_mm, p.y_mm, layer)
            if pid in parent:
                best_pid = pid
                break
        if best_pid is None:
            # Pad not connected to any trace — disconnected
            return False
        pad_roots.add(find(best_pid))

    return len(pad_roots) == 1


def _fine_grid_retry(
    coarse_grid: "RoutingGrid",
    failed_nets: list[tuple[NetInfo, int]],
    result: RoutingResult,
    placement: dict,
    pad_map: dict[str, PadInfo],
    net_id_map: dict[str, int],
    net_trace_widths: dict[str, float],
    routed_net_ids: list[str],
    config: "RouterConfig",
    netlist: dict,
    diagonal: bool,
    fill_net: NetInfo | None,
    channel_pressure: dict[str, list[float]] | None = None,
) -> list[tuple[NetInfo, int]]:
    """Re-route failed nets on a finer grid (2× resolution).

    Creates a fine grid, marks already-routed traces as obstacles, then
    attempts to route each failed signal net with narrow trace width.
    Returns the list of nets that still failed.
    """
    factor = config.fine_grid_factor
    fine_res = config.grid_resolution_mm / factor
    fine_grid = RoutingGrid(coarse_grid.board_w, coarse_grid.board_h, fine_res)

    # Set up obstacles and pads on the fine grid at its native resolution
    _setup_grid(fine_grid, placement, pad_map, config.clearance_mm, net_id_map)

    # Mark already-routed traces as obstacles on the fine grid
    # (these are fixed; failed nets must route around them)
    for trace in result.traces:
        hw = trace.width_mm / 2 + config.clearance_mm
        fine_grid.mark_obstacle_rect(
            trace.start_x_mm - hw, trace.start_y_mm - hw,
            trace.start_x_mm + hw, trace.start_y_mm + hw,
            trace.layer, 0.0,
        )
        fine_grid.mark_obstacle_rect(
            trace.end_x_mm - hw, trace.end_y_mm - hw,
            trace.end_x_mm + hw, trace.end_y_mm + hw,
            trace.layer, 0.0,
        )
        # Mark the trace body as obstacle
        # For axis-aligned traces, mark a rectangle covering the trace
        min_x = min(trace.start_x_mm, trace.end_x_mm) - hw
        max_x = max(trace.start_x_mm, trace.end_x_mm) + hw
        min_y = min(trace.start_y_mm, trace.end_y_mm) - hw
        max_y = max(trace.start_y_mm, trace.end_y_mm) + hw
        fine_grid.mark_obstacle_rect(min_x, min_y, max_x, max_y, trace.layer, 0.0)

    # Mark already-routed vias as obstacles on both layers
    for via in result.vias:
        via_hw = config.via_diameter_mm / 2 + config.clearance_mm
        for layer in ["top", "bottom"]:
            fine_grid.mark_obstacle_rect(
                via.x_mm - via_hw, via.y_mm - via_hw,
                via.x_mm + via_hw, via.y_mm + via_hw,
                layer, 0.0,
            )
        fine_grid.mark_no_via_rect(
            via.x_mm - via_hw, via.y_mm - via_hw,
            via.x_mm + via_hw, via.y_mm + via_hw,
        )

    # Now re-mark pads for the failed nets (they were overwritten by obstacle rects above)
    for net, nid in failed_nets:
        pads = _get_net_pads(net, pad_map, netlist)
        for pad in pads:
            if pad.net_id and pad.net_id in net_id_map:
                pid = net_id_map[pad.net_id]
                pw, ph = pad.pad_width_mm, pad.pad_height_mm
                clr = config.clearance_narrow_mm
                layers = ["top", "bottom"] if pad.layer == "all" else [pad.layer]
                for layer in layers:
                    fine_grid.mark_rect(
                        pad.x_mm - pw / 2 - clr, pad.y_mm - ph / 2 - clr,
                        pad.x_mm + pw / 2 + clr, pad.y_mm + ph / 2 + clr,
                        layer, pid,
                    )

    # Create a temporary config with fine grid resolution for route_net
    # (route_net uses config.grid_resolution_mm for hw calculations, but
    # the actual grid resolution is fine_res — we pass clearance_override)
    narrow_tw = config.trace_width_signal_narrow_mm
    narrow_cl = config.clearance_narrow_mm

    still_failed: list[tuple[NetInfo, int]] = []
    routed_count = 0
    for net, nid in failed_nets:
        if net.net_class != "signal":
            still_failed.append((net, nid))
            continue

        outcome = route_net(
            fine_grid, net, pad_map, nid, config, netlist, narrow_tw,
            diagonal=diagonal,
            via_cost_override=config.congestion_via_cost,
            clearance_override=narrow_cl,
            channel_pressure=channel_pressure,
        )
        if outcome is not None:
            result.traces.extend(outcome[0])
            result.vias.extend(outcome[1])
            routed_net_ids.append(net.net_id)
            result.trace_width_overrides[net.net_id] = (
                f"fine grid ({fine_res:.3f}mm) narrow trace ({narrow_tw:.2f}mm)"
            )
            routed_count += 1
        else:
            still_failed.append((net, nid))

    if routed_count:
        print(f"  Fine grid retry ({fine_res:.3f}mm): routed {routed_count} additional net(s)")
    return still_failed


def _connectivity_repair(
    grid: "RoutingGrid",
    result: RoutingResult,
    ordered_nets: list[NetInfo],
    pad_map: dict[str, PadInfo],
    net_id_map: dict[str, int],
    int_to_net: dict[int, NetInfo],
    net_trace_widths: dict[str, float],
    routed_net_ids: list[str],
    config: "RouterConfig",
    netlist: dict,
    diagonal: bool,
    fill_net: NetInfo | None,
    channel_pressure: dict[str, list[float]] | None = None,
) -> None:
    """Detect nets with disconnected groups and re-route them.

    Modifies result and grid in-place.
    """
    repaired = 0
    for net in ordered_nets:
        if net.net_id not in routed_net_ids:
            continue
        if fill_net and net.net_id == fill_net.net_id:
            continue

        pads = _get_net_pads(net, pad_map, netlist)
        if len(pads) < 2:
            continue

        connected = _check_net_connectivity(
            net.net_id, pads, result.traces, result.vias,
            config.grid_resolution_mm,
        )
        if connected:
            continue

        # Disconnected — rip and re-route
        nid = net_id_map[net.net_id]
        grid.clear_net(nid)
        _restore_pad_markings(grid, net, pad_map, nid, config.clearance_mm, netlist)
        result.traces = [t for t in result.traces if t.net_id != net.net_id]
        result.vias = [v for v in result.vias if v.net_id != net.net_id]

        tw = net_trace_widths[net.net_id]
        outcome = route_net(grid, net, pad_map, nid, config, netlist, tw, diagonal=diagonal,
                            channel_pressure=channel_pressure)
        if outcome is not None:
            result.traces.extend(outcome[0])
            result.vias.extend(outcome[1])
            repaired += 1
        else:
            # Try relaxed clearance with endpoint snapping
            relaxed_clearance = config.clearance_mm * 0.85
            route_hw = int(math.ceil(tw / (2 * config.grid_resolution_mm))) - 1
            route_hw = max(0, route_hw)
            relaxed_block_hw = int(math.ceil(
                (tw / 2 + relaxed_clearance) / config.grid_resolution_mm
            ))
            relaxed_block_hw = max(route_hw, relaxed_block_hw)
            relaxed_via_block_hw = int(math.ceil(
                (config.via_diameter_mm / 2 + tw / 2 + relaxed_clearance) / config.grid_resolution_mm
            ))
            relaxed_via_block_hw = max(relaxed_block_hw, relaxed_via_block_hw)

            pad_positions = [(p.x_mm, p.y_mm) for p in pads]
            mst_edges = compute_mst_edges(pad_positions)
            all_tr: list[TraceSegment] = []
            all_vi: list[Via] = []
            ok = True
            for ia, ib, _ in mst_edges:
                pad_a, pad_b = pads[ia], pads[ib]
                sc, sr = grid.mm_to_grid(pad_a.x_mm, pad_a.y_mm)
                ec, er = grid.mm_to_grid(pad_b.x_mm, pad_b.y_mm)
                sl_a = "top" if pad_a.layer == "all" else pad_a.layer
                sl_b = "top" if pad_b.layer == "all" else pad_b.layer
                path = astar_route(
                    grid, (sc, sr, sl_a), (ec, er, sl_b),
                    nid, half_width_cells=route_hw, via_cost=config.via_cost,
                    diagonal=diagonal,
                )
                if path is None:
                    ok = False
                    break
                mark_path_on_grid(grid, path, nid, relaxed_block_hw, relaxed_via_block_hw)
                traces, vias = simplify_path(
                    path, config.grid_resolution_mm, tw, net.net_id, net.name,
                )
                if traces:
                    t0 = traces[0]
                    traces[0] = TraceSegment(
                        pad_a.x_mm, pad_a.y_mm, t0.end_x_mm, t0.end_y_mm,
                        t0.width_mm, t0.layer, t0.net_id, t0.net_name,
                    )
                    tN = traces[-1]
                    traces[-1] = TraceSegment(
                        tN.start_x_mm, tN.start_y_mm, pad_b.x_mm, pad_b.y_mm,
                        tN.width_mm, tN.layer, tN.net_id, tN.net_name,
                    )
                all_tr.extend(traces)
                all_vi.extend(vias)
            if ok:
                result.traces.extend(all_tr)
                result.vias.extend(all_vi)
                repaired += 1
            else:
                # Could not repair — remove from routed
                routed_net_ids.remove(net.net_id)
                result.unrouted_nets.append(net.net_id)

    if repaired:
        print(f"  Connectivity repair: fixed {repaired} disconnected net(s)")


def route_board(
    placement: dict,
    netlist: dict,
    config: RouterConfig | None = None,
) -> dict:
    """Route all nets on the board.

    Uses multi-trial net ordering optimization: tries several orderings of the
    signal nets and keeps the one with the highest completion percentage.

    Args:
        placement: Validated placement JSON dict.
        netlist: Validated netlist JSON dict.
        config: Router configuration (uses defaults if None).

    Returns:
        Routed dict: placement data + routing section with traces, vias, statistics.
    """
    if config is None:
        config = RouterConfig()

    import random as _random

    # Build pad map
    pad_map = build_pad_map(placement, netlist)

    # Build connectivity (reuses ratsnest module)
    nets = build_connectivity(netlist)

    # Assign integer IDs to nets (1-indexed, 0 = EMPTY)
    net_id_map: dict[str, int] = {}
    int_to_net: dict[int, NetInfo] = {}
    for i, net in enumerate(nets, start=1):
        net_id_map[net.net_id] = i
        int_to_net[i] = net

    # Board dimensions
    board = placement.get("board", {})
    board_w = board.get("width_mm", 50.0)
    board_h = board.get("height_mm", 50.0)

    # Order nets (default: power first, then shortest-first signals)
    ordered_nets = order_nets(nets, pad_map, netlist)

    # Calculate trace widths per net (IPC-2221 + defaults)
    net_trace_widths: dict[str, float] = {}
    trace_width_overrides: dict[str, float] = {}

    for net in ordered_nets:
        if net.net_class == "power":
            default_w = config.trace_width_power_mm
        elif net.net_class == "ground":
            default_w = config.trace_width_ground_mm
        else:
            default_w = config.trace_width_signal_mm

        net_current = compute_net_current(net, netlist)
        ipc_min = ipc2221_trace_width(net_current, config.copper_weight_oz)
        final_w = max(default_w, ipc_min)
        net_trace_widths[net.net_id] = final_w
        if ipc_min > default_w:
            trace_width_overrides[net.net_id] = final_w

    # Identify fill net
    fill_net: NetInfo | None = None
    fill_net_int: int = 0
    if config.fill_enabled:
        fill_net = next((n for n in ordered_nets if n.name == config.fill_net_name), None)
        if fill_net is None:
            fill_net = next((n for n in ordered_nets if n.net_class == "ground"), None)
        if fill_net and fill_net.net_id in net_id_map:
            fill_net_int = net_id_map[fill_net.net_id]

    # Split nets into power (fixed order) and signal (order varies)
    power_nets = [n for n in ordered_nets if n.net_class in ("power", "ground")]
    signal_nets = [n for n in ordered_nets if n.net_class == "signal"]

    # Generate ordering trials
    orderings: list[list[NetInfo]] = []

    # Trial 0: default (shortest-first signals)
    orderings.append(power_nets + signal_nets)

    # Trial 1: longest-first signals (route congested nets early)
    orderings.append(power_nets + list(reversed(signal_nets)))

    # Trial 2: interleaved short/long signals
    if len(signal_nets) > 2:
        interleaved = []
        lo, hi = 0, len(signal_nets) - 1
        while lo <= hi:
            interleaved.append(signal_nets[lo])
            if lo != hi:
                interleaved.append(signal_nets[hi])
            lo += 1
            hi -= 1
        orderings.append(power_nets + interleaved)

    # Trial 3: reversed power order + default signals
    orderings.append(list(reversed(power_nets)) + signal_nets)

    # Trial 4-5: congestion-aware (most-constrained-first and reverse)
    measure_grid = RoutingGrid(board_w, board_h, config.grid_resolution_mm)
    _setup_grid(measure_grid, placement, pad_map, config.clearance_mm, net_id_map)
    if config.pre_fill_enabled and fill_net and fill_net_int > 0:
        _apply_pre_fill(measure_grid, fill_net_int, fill_net.net_id, pad_map, config)
    constrained_order = order_nets_by_accessibility(
        ordered_nets, pad_map, measure_grid, net_id_map, netlist,
    )
    # Extract just the signal portion (power prefix stays)
    constrained_power = [n for n in constrained_order if n.net_class in ("power", "ground")]
    constrained_signal = [n for n in constrained_order if n.net_class == "signal"]
    orderings.append(constrained_power + constrained_signal)
    orderings.append(constrained_power + list(reversed(constrained_signal)))

    # Trial 6-7: channel-aware ordering (most channel-constrained first)
    channels = _detect_channels(pad_map, config, board_w, board_h)
    channel_pressure: dict[str, list[float]] | None = None
    if channels:
        channel_pressure = _build_channel_pressure(channels, measure_grid, config)
        ch_order = order_nets_by_channel_pressure(
            ordered_nets, pad_map, channels, net_id_map, netlist,
        )
        ch_power = [n for n in ch_order if n.net_class in ("power", "ground")]
        ch_signal = [n for n in ch_order if n.net_class == "signal"]
        orderings.append(ch_power + ch_signal)
        orderings.append(ch_power + list(reversed(ch_signal)))
        print(f"  Detected {len(channels)} routing channel(s): "
              f"capacities {[ch.capacity for ch in channels]}")

    # Remaining trials: random shuffles with deterministic seeds
    n_deterministic = len(orderings)
    rng = _random.Random(42)
    for _ in range(max(0, config.ordering_trials - n_deterministic)):
        shuffled = list(signal_nets)
        rng.shuffle(shuffled)
        orderings.append(power_nets + shuffled)

    # Run each trial on a fresh grid, keep the best
    # Try both Manhattan (4-conn) and diagonal (8-conn) for each ordering
    best_result: RoutingResult | None = None
    best_routed: list[str] = []
    best_failed: list[tuple[NetInfo, int]] = []
    best_grid: RoutingGrid | None = None
    best_count = -1
    best_ordering: list[NetInfo] = orderings[0]
    best_diagonal: bool = True

    for ordering in orderings:
        for diag in [True, False]:
            grid = RoutingGrid(board_w, board_h, config.grid_resolution_mm)
            _setup_grid(grid, placement, pad_map, config.clearance_mm, net_id_map)
            if config.pre_fill_enabled and fill_net and fill_net_int > 0:
                _apply_pre_fill(grid, fill_net_int, fill_net.net_id, pad_map, config)

            result, routed_ids, failed = _route_with_ordering(
                ordering, grid, pad_map, net_id_map, net_trace_widths,
                fill_net, config, netlist, diagonal=diag,
                channel_pressure=channel_pressure,
            )

            routed_count = len(routed_ids)
            if routed_count > best_count:
                best_count = routed_count
                best_result = result
                best_routed = routed_ids
                best_failed = failed
                best_grid = grid
                best_ordering = ordering
                best_diagonal = diag

            # Early exit if 100% routed
            if not failed:
                break
        if best_count == len(ordered_nets):
            break

    result = best_result
    routed_net_ids = best_routed
    failed_nets = best_failed
    grid = best_grid
    use_diagonal = best_diagonal
    assert result is not None and grid is not None

    # Pre-fill fallback: if pre-fill is active but failed nets remain,
    # try one no-pre-fill trial with the best ordering to compare
    if (config.pre_fill_enabled and config.pre_fill_fallback
            and fill_net and fill_net_int > 0 and failed_nets):
        print(f"  Pre-fill routing: {best_count}/{len(ordered_nets)} nets — trying without pre-fill...")
        nf_grid = RoutingGrid(board_w, board_h, config.grid_resolution_mm)
        _setup_grid(nf_grid, placement, pad_map, config.clearance_mm, net_id_map)
        nf_result, nf_routed, nf_failed = _route_with_ordering(
            best_ordering, nf_grid, pad_map, net_id_map, net_trace_widths,
            fill_net, config, netlist, diagonal=use_diagonal,
            channel_pressure=channel_pressure,
        )
        if len(nf_routed) > best_count:
            print(f"  No-prefill routes more: {len(nf_routed)} vs {best_count} — switching off pre-fill")
            result = nf_result
            routed_net_ids = nf_routed
            failed_nets = nf_failed
            grid = nf_grid
            config = dataclasses.replace(config, pre_fill_enabled=False)
        else:
            print(f"  Pre-fill kept ({best_count} >= {len(nf_routed)})")

    # Phase 2: Negotiated congestion routing (PathFinder)
    # If initial trials didn't achieve 100%, run NCR with all nets
    if failed_nets and config.ncr_enabled:
        ncr_result, ncr_routed, ncr_failed, ncr_grid = _negotiated_congestion_route(
            best_ordering, board_w, board_h, placement, pad_map,
            net_id_map, net_trace_widths, fill_net, config, netlist,
            diagonal=use_diagonal,
            channel_pressure=channel_pressure,
        )
        # Use NCR result if it's better than initial trials
        if len(ncr_routed) > len(routed_net_ids):
            result = ncr_result
            routed_net_ids = ncr_routed
            failed_nets = ncr_failed
            grid = ncr_grid
            print(f"  NCR improved: {len(ncr_routed)}/{len(ordered_nets)} nets")
        else:
            print(f"  NCR did not improve ({len(ncr_routed)} vs {len(routed_net_ids)})")

    # Apply trace width overrides
    result.trace_width_overrides = trace_width_overrides

    # Targeted rip-up and retry for failed nets.
    # For each failed net, identify which specific routed nets block its path
    # (via astar_find_blockers), rip up only those, route the failed net,
    # then re-route the ripped nets.  Escalates aggression each iteration.

    def _can_rip_net(net: NetInfo, iteration: int) -> bool:
        """Determine if a net can be ripped based on escalation level."""
        # Never rip the fill net
        if fill_net and net.net_id == fill_net.net_id:
            return False
        if net.net_class == "signal":
            return True  # always rippable
        if net.net_class == "ground" and iteration >= 3:
            return True  # allow ground ripping at iteration 3+
        if net.net_class == "power" and iteration >= 5:
            return True  # allow power ripping at iteration 5+
        return False

    for iteration in range(config.max_rip_up_iterations):
        if not failed_nets:
            break

        max_depth = min(iteration + 1, config.max_rip_up_depth)
        still_failed: list[tuple[NetInfo, int]] = []

        # Re-order failed nets by accessibility (most-constrained-first)
        # to break ordering deadlocks
        if iteration > 0:
            constrained = order_nets_by_accessibility(
                [n for n, _ in failed_nets], pad_map, grid, net_id_map, netlist,
            )
            failed_nets = [(n, net_id_map[n.net_id]) for n in constrained]

        for net, nid in failed_nets:
            tw = net_trace_widths[net.net_id]
            route_hw = int(math.ceil(tw / (2 * config.grid_resolution_mm))) - 1
            route_hw = max(0, route_hw)

            # Find which nets block this net's MST edges
            pads = _get_net_pads(net, pad_map, netlist)
            if len(pads) < 2:
                continue
            pad_positions = [(p.x_mm, p.y_mm) for p in pads]
            mst_edges = compute_mst_edges(pad_positions)

            all_blockers: set[int] = set()
            for ia, ib, _ in mst_edges:
                pad_a, pad_b = pads[ia], pads[ib]
                sc, sr = grid.mm_to_grid(pad_a.x_mm, pad_a.y_mm)
                ec, er = grid.mm_to_grid(pad_b.x_mm, pad_b.y_mm)
                start_layers = ["top", "bottom"] if pad_a.layer == "all" else [pad_a.layer]
                end_layers = ["top", "bottom"] if pad_b.layer == "all" else [pad_b.layer]
                for sl in start_layers:
                    for el in end_layers:
                        edge_blockers = astar_find_blockers(
                            grid, (sc, sr, sl), (ec, er, el), nid,
                            half_width_cells=route_hw,
                            via_cost=config.via_cost,
                            diagonal=use_diagonal,
                        )
                        if edge_blockers is not None:
                            all_blockers |= edge_blockers
                            break  # found a path for this layer combo
                    if all_blockers:
                        break

            # Filter to nets we can rip at this escalation level
            blocker_nids = set()
            for b_int in all_blockers:
                b_net = int_to_net.get(b_int)
                if b_net and _can_rip_net(b_net, iteration) and b_net.net_id in routed_net_ids:
                    blocker_nids.add(b_int)

            if not blocker_nids or len(blocker_nids) > max_depth:
                still_failed.append((net, nid))
                continue

            # Rip up only the blocking nets
            ripped: list[tuple[int, NetInfo]] = []
            for b_int in blocker_nids:
                b_net = int_to_net[b_int]
                grid.clear_net(b_int)
                # Restore pad+clearance markings (clear_net erases them too
                # since pads use the same net ID as traces on the grid)
                _restore_pad_markings(
                    grid, b_net, pad_map, b_int, config.clearance_mm, netlist,
                )
                result.traces = [t for t in result.traces if t.net_id != b_net.net_id]
                result.vias = [v for v in result.vias if v.net_id != b_net.net_id]
                routed_net_ids.remove(b_net.net_id)
                ripped.append((b_int, b_net))

            # Route the failed net in the cleared space
            outcome = route_net(grid, net, pad_map, nid, config, netlist, tw, diagonal=use_diagonal,
                                via_cost_override=config.congestion_via_cost,
                                channel_pressure=channel_pressure)

            if outcome is None:
                # Failed even after rip-up — restore the ripped nets
                for b_int, b_net in ripped:
                    b_tw = net_trace_widths[b_net.net_id]
                    restore = route_net(grid, b_net, pad_map, b_int, config, netlist, b_tw, diagonal=use_diagonal,
                                        channel_pressure=channel_pressure)
                    if restore is not None:
                        result.traces.extend(restore[0])
                        result.vias.extend(restore[1])
                        routed_net_ids.append(b_net.net_id)
                    else:
                        still_failed.append((b_net, b_int))
                still_failed.append((net, nid))
                continue

            # Success — add the newly routed net
            result.traces.extend(outcome[0])
            result.vias.extend(outcome[1])
            routed_net_ids.append(net.net_id)

            # Re-route the ripped nets
            for b_int, b_net in ripped:
                b_tw = net_trace_widths[b_net.net_id]
                reroute = route_net(grid, b_net, pad_map, b_int, config, netlist, b_tw, diagonal=use_diagonal,
                                    channel_pressure=channel_pressure)
                if reroute is not None:
                    result.traces.extend(reroute[0])
                    result.vias.extend(reroute[1])
                    routed_net_ids.append(b_net.net_id)
                else:
                    still_failed.append((b_net, b_int))

        failed_nets = still_failed

    # Coordinated multi-net rip-up: find the worst blocker nets and rip
    # multiple simultaneously to unblock failed nets
    for coord_iter in range(5):
        if not failed_nets:
            break

        # Collect all blockers across all failed nets
        blocker_counts: dict[int, int] = {}  # blocker_nid -> count of failed nets it blocks
        for net, nid in failed_nets:
            pads = _get_net_pads(net, pad_map, netlist)
            if len(pads) < 2:
                continue
            tw = net_trace_widths[net.net_id]
            route_hw = int(math.ceil(tw / (2 * config.grid_resolution_mm))) - 1
            route_hw = max(0, route_hw)
            pad_positions = [(p.x_mm, p.y_mm) for p in pads]
            mst_edges = compute_mst_edges(pad_positions)
            net_blockers: set[int] = set()
            for ia, ib, _ in mst_edges:
                pad_a, pad_b = pads[ia], pads[ib]
                sc, sr = grid.mm_to_grid(pad_a.x_mm, pad_a.y_mm)
                ec, er = grid.mm_to_grid(pad_b.x_mm, pad_b.y_mm)
                sl = "top" if pad_a.layer == "all" else pad_a.layer
                el = "top" if pad_b.layer == "all" else pad_b.layer
                edge_blockers = astar_find_blockers(
                    grid, (sc, sr, sl), (ec, er, el), nid,
                    half_width_cells=route_hw, via_cost=config.via_cost,
                    diagonal=use_diagonal,
                )
                if edge_blockers:
                    net_blockers |= edge_blockers
            for b in net_blockers:
                b_net = int_to_net.get(b)
                if b_net and b_net.net_id in routed_net_ids:
                    if fill_net and b_net.net_id == fill_net.net_id:
                        continue
                    blocker_counts[b] = blocker_counts.get(b, 0) + 1

        if not blocker_counts:
            break

        # Rip the top N worst offenders (escalating: 1 on iter 0, up to 3)
        max_rip = min(1 + coord_iter, 3, len(blocker_counts))
        sorted_blockers = sorted(blocker_counts.keys(),
                                 key=lambda b: blocker_counts[b], reverse=True)
        rip_targets = sorted_blockers[:max_rip]

        ripped_nets: list[tuple[int, NetInfo]] = []
        for worst_b in rip_targets:
            worst_net = int_to_net[worst_b]
            grid.clear_net(worst_b)
            _restore_pad_markings(grid, worst_net, pad_map, worst_b, config.clearance_mm, netlist)
            result.traces = [t for t in result.traces if t.net_id != worst_net.net_id]
            result.vias = [v for v in result.vias if v.net_id != worst_net.net_id]
            routed_net_ids.remove(worst_net.net_id)
            ripped_nets.append((worst_b, worst_net))

        # Try routing all failed nets in the cleared space
        newly_routed: list[tuple[NetInfo, int]] = []
        still_failed_coord: list[tuple[NetInfo, int]] = []
        for net, nid in failed_nets:
            tw = net_trace_widths[net.net_id]
            outcome = route_net(grid, net, pad_map, nid, config, netlist, tw, diagonal=use_diagonal,
                                via_cost_override=config.congestion_via_cost,
                                channel_pressure=channel_pressure)
            if outcome is not None:
                result.traces.extend(outcome[0])
                result.vias.extend(outcome[1])
                routed_net_ids.append(net.net_id)
                newly_routed.append((net, nid))
            else:
                still_failed_coord.append((net, nid))

        # Re-route the ripped nets (normal via cost — they should find natural paths)
        ripped_failed: list[tuple[NetInfo, int]] = []
        for b_int, b_net in ripped_nets:
            b_tw = net_trace_widths[b_net.net_id]
            reroute = route_net(grid, b_net, pad_map, b_int, config, netlist, b_tw, diagonal=use_diagonal,
                                channel_pressure=channel_pressure)
            if reroute is not None:
                result.traces.extend(reroute[0])
                result.vias.extend(reroute[1])
                routed_net_ids.append(b_net.net_id)
            else:
                ripped_failed.append((b_net, b_int))

        total_failed = still_failed_coord + ripped_failed
        if len(total_failed) < len(failed_nets) + len(ripped_nets):
            # Net improvement — accept
            failed_nets = total_failed
        else:
            # No improvement — undo everything
            for net, nid in newly_routed:
                grid.clear_net(nid)
                _restore_pad_markings(grid, net, pad_map, nid, config.clearance_mm, netlist)
                result.traces = [t for t in result.traces if t.net_id != net.net_id]
                result.vias = [v for v in result.vias if v.net_id != net.net_id]
                routed_net_ids.remove(net.net_id)
            # Re-route ripped nets that succeeded
            for b_int, b_net in ripped_nets:
                if b_net.net_id in routed_net_ids:
                    continue  # already restored
                b_tw = net_trace_widths[b_net.net_id]
                reroute = route_net(grid, b_net, pad_map, b_int, config, netlist, b_tw, diagonal=use_diagonal,
                                    channel_pressure=channel_pressure)
                if reroute is not None:
                    result.traces.extend(reroute[0])
                    result.vias.extend(reroute[1])
                    routed_net_ids.append(b_net.net_id)
            failed_nets = still_failed_coord + [(n, nid) for n, nid in newly_routed]
            break

    # Phase 3: Push-and-shove for remaining failed nets
    if failed_nets and config.shove_enabled:
        pzi = _build_pad_zone_index(pad_map, net_id_map, config.clearance_mm, grid)
        failed_nets = _shove_pass(
            grid, failed_nets, result, pad_map, net_id_map, int_to_net,
            net_trace_widths, pzi, config, netlist,
            routed_net_ids, use_diagonal, fill_net,
        )

    # Phase 4: Narrow trace retry for congested signal nets
    if failed_nets:
        still_failed_narrow: list[tuple[NetInfo, int]] = []
        narrow_tw = config.trace_width_signal_narrow_mm
        narrow_cl = config.clearance_narrow_mm
        for net, nid in failed_nets:
            # Only narrow signal nets (never power/ground)
            if net.net_class != "signal":
                still_failed_narrow.append((net, nid))
                continue
            outcome = route_net(
                grid, net, pad_map, nid, config, netlist, narrow_tw,
                diagonal=use_diagonal,
                via_cost_override=config.congestion_via_cost,
                clearance_override=narrow_cl,
                channel_pressure=channel_pressure,
            )
            if outcome is not None:
                result.traces.extend(outcome[0])
                result.vias.extend(outcome[1])
                routed_net_ids.append(net.net_id)
                result.trace_width_overrides[net.net_id] = (
                    f"narrow trace ({narrow_tw:.2f}mm, clearance {narrow_cl:.2f}mm)"
                )
            else:
                still_failed_narrow.append((net, nid))
        if len(still_failed_narrow) < len(failed_nets):
            print(f"  Narrow trace retry: routed {len(failed_nets) - len(still_failed_narrow)} additional net(s)")
        failed_nets = still_failed_narrow

    # Phase 5: Fine grid retry — route failed nets on a 2× finer grid
    if failed_nets and config.fine_grid_factor > 1:
        failed_nets = _fine_grid_retry(
            grid, failed_nets, result, placement, pad_map, net_id_map,
            net_trace_widths, routed_net_ids, config, netlist,
            use_diagonal, fill_net, channel_pressure=channel_pressure,
        )

    # Pass 2: Relaxed clearance for remaining unrouted nets
    # Use 85% of normal clearance — enough to squeeze through tight spots
    # while staying above the DRC minimum.  The previous 0.7 factor produced
    # traces at 0.25mm spacing which violates the 0.45mm DRC requirement.
    if failed_nets:
        relaxed_clearance = config.clearance_mm * 0.85
        still_failed_relaxed: list[tuple[NetInfo, int]] = []

        for net, nid in failed_nets:
            tw = net_trace_widths[net.net_id]
            route_hw = int(math.ceil(tw / (2 * config.grid_resolution_mm))) - 1
            route_hw = max(0, route_hw)
            relaxed_block_hw = int(math.ceil(
                (tw / 2 + relaxed_clearance) / config.grid_resolution_mm
            ))
            relaxed_block_hw = max(route_hw, relaxed_block_hw)
            relaxed_via_block_hw = int(math.ceil(
                (config.via_diameter_mm / 2 + tw / 2 + relaxed_clearance) / config.grid_resolution_mm
            ))
            relaxed_via_block_hw = max(relaxed_block_hw, relaxed_via_block_hw)

            pads = _get_net_pads(net, pad_map, netlist)
            if len(pads) < 2:
                continue

            pad_positions = [(p.x_mm, p.y_mm) for p in pads]
            mst_edges = compute_mst_edges(pad_positions)

            all_traces_relaxed: list[TraceSegment] = []
            all_vias_relaxed: list[Via] = []
            net_ok = True

            for ia, ib, _ in mst_edges:
                pad_a, pad_b = pads[ia], pads[ib]
                sc, sr = grid.mm_to_grid(pad_a.x_mm, pad_a.y_mm)
                ec, er = grid.mm_to_grid(pad_b.x_mm, pad_b.y_mm)

                sl_a = "top" if pad_a.layer == "all" else pad_a.layer
                sl_b = "top" if pad_b.layer == "all" else pad_b.layer
                path = astar_route(
                    grid, (sc, sr, sl_a), (ec, er, sl_b),
                    nid, half_width_cells=route_hw, via_cost=config.via_cost,
                    diagonal=use_diagonal,
                )
                if path is None:
                    net_ok = False
                    break

                mark_path_on_grid(grid, path, nid, relaxed_block_hw, relaxed_via_block_hw)
                traces, vias = simplify_path(
                    path, config.grid_resolution_mm, tw, net.net_id, net.name,
                )
                # Snap trace endpoints to exact pad positions (same as route_net)
                if traces:
                    t0 = traces[0]
                    traces[0] = TraceSegment(
                        pad_a.x_mm, pad_a.y_mm,
                        t0.end_x_mm, t0.end_y_mm,
                        t0.width_mm, t0.layer, t0.net_id, t0.net_name,
                    )
                    tN = traces[-1]
                    traces[-1] = TraceSegment(
                        tN.start_x_mm, tN.start_y_mm,
                        pad_b.x_mm, pad_b.y_mm,
                        tN.width_mm, tN.layer, tN.net_id, tN.net_name,
                    )
                all_traces_relaxed.extend(traces)
                all_vias_relaxed.extend(vias)

            if net_ok:
                result.traces.extend(all_traces_relaxed)
                result.vias.extend(all_vias_relaxed)
                routed_net_ids.append(net.net_id)
                result.trace_width_overrides[net.net_id] = (
                    f"routed with relaxed clearance ({relaxed_clearance:.2f}mm)"
                )
            else:
                still_failed_relaxed.append((net, nid))

        failed_nets = still_failed_relaxed

    # Mark unrouted nets
    result.unrouted_nets = [net.net_id for net, _ in failed_nets]

    # Phase 5: Connectivity repair — detect and fix disconnected groups
    _connectivity_repair(
        grid, result, ordered_nets, pad_map, net_id_map, int_to_net,
        net_trace_widths, routed_net_ids, config, netlist, use_diagonal,
        fill_net, channel_pressure=channel_pressure,
    )

    # Phase 6a: Consolidate endpoints before nudging (fix MST edge gaps)
    _consolidate_endpoints(result, pad_map, config)

    # Phase 6b: Post-route nudging — fix clearance violations by shifting traces
    _post_route_nudge(result, pad_map, config)

    # Phase 6c: Re-consolidate endpoints after nudging (fix nudge-induced gaps)
    _consolidate_endpoints(result, pad_map, config)

    # Phase 7: Add short GND stub traces so KiCad fill can connect to GND pads
    # (skipped when pre-fill is active — GND pads already have fill connectivity)
    if fill_net and not config.pre_fill_enabled:
        fill_pads = [p for p in pad_map.values() if p.net_id == fill_net.net_id]
        stub_len = config.grid_resolution_mm  # tiny stub
        for fp in fill_pads:
            # Add a zero-length trace at the pad center on the pad's layer(s)
            layers = ["top", "bottom"] if fp.layer == "all" else [fp.layer]
            for layer in layers:
                result.traces.append(TraceSegment(
                    fp.x_mm, fp.y_mm,
                    fp.x_mm + stub_len, fp.y_mm,
                    config.trace_width_ground_mm, layer,
                    fill_net.net_id, fill_net.name,
                ))

    # Copper fill (after all routing is complete)
    copper_fills: list[dict] = []
    if config.fill_enabled and fill_net and fill_net_int > 0:
        copper_fills, stitch_vias = create_copper_fill(
            grid, fill_net_int, fill_net.net_id, fill_net.name, pad_map, config,
        )
        result.vias.extend(stitch_vias)

    # Build output JSON (includes silkscreen generation)
    return _build_output(placement, netlist, result, ordered_nets, config, pad_map, copper_fills)


def _consolidate_endpoints(
    result: RoutingResult,
    pad_map: dict[str, PadInfo],
    config: RouterConfig,
) -> None:
    """Consolidate trace endpoints at shared pads.

    Different MST edges meeting at the same pad may produce trace endpoints
    at slightly different positions due to grid quantization. This pass snaps
    all same-net trace endpoints near a pad to the exact pad position, and
    also snaps same-net trace endpoints near vias to the exact via position,
    ensuring the trace graph is connected.
    """
    traces = result.traces
    vias = result.vias
    snap_tol = config.grid_resolution_mm * 3.0  # generous snap radius (covers via-to-trace gaps)

    # Group pads by net
    net_pads: dict[str, list[PadInfo]] = {}
    for p in pad_map.values():
        if p.net_id:
            net_pads.setdefault(p.net_id, []).append(p)

    # Pass 1: Snap trace endpoints to exact pad positions
    for i, t in enumerate(traces):
        pads = net_pads.get(t.net_id, [])
        new_sx, new_sy = t.start_x_mm, t.start_y_mm
        new_ex, new_ey = t.end_x_mm, t.end_y_mm

        for p in pads:
            if abs(new_sx - p.x_mm) < snap_tol and abs(new_sy - p.y_mm) < snap_tol:
                new_sx, new_sy = p.x_mm, p.y_mm
            if abs(new_ex - p.x_mm) < snap_tol and abs(new_ey - p.y_mm) < snap_tol:
                new_ex, new_ey = p.x_mm, p.y_mm

        if (new_sx != t.start_x_mm or new_sy != t.start_y_mm or
                new_ex != t.end_x_mm or new_ey != t.end_y_mm):
            traces[i] = TraceSegment(
                new_sx, new_sy, new_ex, new_ey,
                t.width_mm, t.layer, t.net_id, t.net_name,
            )

    # Pass 2: Snap trace endpoints to exact via positions (cross-layer connection)
    for i, t in enumerate(traces):
        net_vias = [v for v in vias if v.net_id == t.net_id]
        new_sx, new_sy = t.start_x_mm, t.start_y_mm
        new_ex, new_ey = t.end_x_mm, t.end_y_mm

        for v in net_vias:
            if abs(new_sx - v.x_mm) < snap_tol and abs(new_sy - v.y_mm) < snap_tol:
                new_sx, new_sy = v.x_mm, v.y_mm
            if abs(new_ex - v.x_mm) < snap_tol and abs(new_ey - v.y_mm) < snap_tol:
                new_ex, new_ey = v.x_mm, v.y_mm

        if (new_sx != t.start_x_mm or new_sy != t.start_y_mm or
                new_ex != t.end_x_mm or new_ey != t.end_y_mm):
            traces[i] = TraceSegment(
                new_sx, new_sy, new_ex, new_ey,
                t.width_mm, t.layer, t.net_id, t.net_name,
            )

    # Pass 3: For same-net same-layer endpoints that are close but not at a
    # pad or via, snap them together (fixes intra-MST-edge gaps)
    merge_tol = config.grid_resolution_mm * 1.5
    for net_id in set(t.net_id for t in traces):
        for layer in ("top", "bottom"):
            net_layer_idxs = [i for i, t in enumerate(traces)
                              if t.net_id == net_id and t.layer == layer]
            # Collect all endpoints
            eps: list[tuple[int, str, float, float]] = []  # (trace_idx, 'start'|'end', x, y)
            for idx in net_layer_idxs:
                t = traces[idx]
                eps.append((idx, 'start', t.start_x_mm, t.start_y_mm))
                eps.append((idx, 'end', t.end_x_mm, t.end_y_mm))

            # For close pairs, snap to their average position
            merged = [False] * len(eps)
            for a in range(len(eps)):
                if merged[a]:
                    continue
                cluster = [a]
                for b in range(a + 1, len(eps)):
                    if merged[b]:
                        continue
                    if (abs(eps[a][2] - eps[b][2]) < merge_tol and
                            abs(eps[a][3] - eps[b][3]) < merge_tol):
                        cluster.append(b)
                        merged[b] = True
                if len(cluster) > 1:
                    # Average position
                    avg_x = sum(eps[c][2] for c in cluster) / len(cluster)
                    avg_y = sum(eps[c][3] for c in cluster) / len(cluster)
                    for c in cluster:
                        idx, which, _, _ = eps[c]
                        t = traces[idx]
                        if which == 'start':
                            traces[idx] = TraceSegment(
                                avg_x, avg_y, t.end_x_mm, t.end_y_mm,
                                t.width_mm, t.layer, t.net_id, t.net_name)
                        else:
                            traces[idx] = TraceSegment(
                                t.start_x_mm, t.start_y_mm, avg_x, avg_y,
                                t.width_mm, t.layer, t.net_id, t.net_name)


def _post_route_nudge(
    result: RoutingResult,
    pad_map: dict[str, PadInfo],
    config: RouterConfig,
    max_iterations: int = 5,
) -> None:
    """Nudge trace segments to fix clearance violations.

    After grid-based routing, trace segments are quantized to the grid.
    This pass identifies trace-trace and via-trace clearance violations
    and shifts the offending segment perpendicular to its direction by
    small increments (grid_res / 4) until the violation is resolved or
    the segment can't move without creating new violations.
    """
    traces = result.traces
    vias = result.vias
    clearance = config.clearance_mm
    nudge_step = config.grid_resolution_mm / 2  # sub-grid nudge

    def _seg_point_dist(
        sx: float, sy: float, ex: float, ey: float, px: float, py: float,
    ) -> float:
        """Distance from point (px, py) to line segment (sx,sy)-(ex,ey)."""
        dx, dy = ex - sx, ey - sy
        length_sq = dx * dx + dy * dy
        if length_sq < 1e-12:
            return math.hypot(px - sx, py - sy)
        t = max(0.0, min(1.0, ((px - sx) * dx + (py - sy) * dy) / length_sq))
        proj_x = sx + t * dx
        proj_y = sy + t * dy
        return math.hypot(px - proj_x, py - proj_y)

    def _seg_seg_dist(
        ax1: float, ay1: float, ax2: float, ay2: float,
        bx1: float, by1: float, bx2: float, by2: float,
    ) -> float:
        """Minimum distance between two line segments."""
        # Check all four endpoint-to-segment distances
        d = min(
            _seg_point_dist(ax1, ay1, ax2, ay2, bx1, by1),
            _seg_point_dist(ax1, ay1, ax2, ay2, bx2, by2),
            _seg_point_dist(bx1, by1, bx2, by2, ax1, ay1),
            _seg_point_dist(bx1, by1, bx2, by2, ax2, ay2),
        )
        return d

    for _iteration in range(max_iterations):
        violations_fixed = 0

        # Build spatial index of traces per layer
        layer_traces: dict[str, list[int]] = {"top": [], "bottom": []}
        for i, t in enumerate(traces):
            layer_traces.setdefault(t.layer, []).append(i)

        # Check trace-trace clearance violations
        for layer in ("top", "bottom"):
            idxs = layer_traces.get(layer, [])
            for ai in range(len(idxs)):
                i = idxs[ai]
                ta = traces[i]
                tw_a = ta.width_mm / 2
                for bi in range(ai + 1, len(idxs)):
                    j = idxs[bi]
                    tb = traces[j]
                    if ta.net_id == tb.net_id:
                        continue  # same net — no clearance needed
                    tw_b = tb.width_mm / 2
                    required = tw_a + tw_b + clearance

                    dist = _seg_seg_dist(
                        ta.start_x_mm, ta.start_y_mm, ta.end_x_mm, ta.end_y_mm,
                        tb.start_x_mm, tb.start_y_mm, tb.end_x_mm, tb.end_y_mm,
                    )
                    if dist >= required:
                        continue

                    # Violation found — nudge the shorter segment
                    len_a = math.hypot(ta.end_x_mm - ta.start_x_mm, ta.end_y_mm - ta.start_y_mm)
                    len_b = math.hypot(tb.end_x_mm - tb.start_x_mm, tb.end_y_mm - tb.start_y_mm)
                    nudge_idx = i if len_a <= len_b else j
                    other_idx = j if nudge_idx == i else i
                    t_nudge = traces[nudge_idx]
                    t_other = traces[other_idx]

                    # Compute perpendicular direction of the nudge segment
                    dx = t_nudge.end_x_mm - t_nudge.start_x_mm
                    dy = t_nudge.end_y_mm - t_nudge.start_y_mm
                    seg_len = math.hypot(dx, dy)
                    if seg_len < 1e-6:
                        continue

                    # Perpendicular: rotate 90 degrees
                    perp_x = -dy / seg_len
                    perp_y = dx / seg_len

                    # Determine which perpendicular direction moves away from other
                    mid_x = (t_nudge.start_x_mm + t_nudge.end_x_mm) / 2
                    mid_y = (t_nudge.start_y_mm + t_nudge.end_y_mm) / 2
                    other_mid_x = (t_other.start_x_mm + t_other.end_x_mm) / 2
                    other_mid_y = (t_other.start_y_mm + t_other.end_y_mm) / 2
                    away_dot = (mid_x - other_mid_x) * perp_x + (mid_y - other_mid_y) * perp_y
                    if away_dot < 0:
                        perp_x, perp_y = -perp_x, -perp_y

                    # Check if endpoints are pad-snapped (don't move pad endpoints)
                    is_start_pad = any(
                        abs(t_nudge.start_x_mm - p.x_mm) < 0.05 and abs(t_nudge.start_y_mm - p.y_mm) < 0.05
                        for p in pad_map.values() if p.net_id == t_nudge.net_id
                    )
                    is_end_pad = any(
                        abs(t_nudge.end_x_mm - p.x_mm) < 0.05 and abs(t_nudge.end_y_mm - p.y_mm) < 0.05
                        for p in pad_map.values() if p.net_id == t_nudge.net_id
                    )

                    # Try nudging in increasing steps
                    gap = required - dist
                    for mult in range(1, 6):
                        offset = nudge_step * mult
                        if offset < gap * 0.5:
                            continue  # skip nudges too small to fix
                        nx_s = t_nudge.start_x_mm + (0 if is_start_pad else perp_x * offset)
                        ny_s = t_nudge.start_y_mm + (0 if is_start_pad else perp_y * offset)
                        nx_e = t_nudge.end_x_mm + (0 if is_end_pad else perp_x * offset)
                        ny_e = t_nudge.end_y_mm + (0 if is_end_pad else perp_y * offset)

                        new_dist = _seg_seg_dist(
                            nx_s, ny_s, nx_e, ny_e,
                            t_other.start_x_mm, t_other.start_y_mm,
                            t_other.end_x_mm, t_other.end_y_mm,
                        )
                        if new_dist >= required:
                            old_sx = t_nudge.start_x_mm
                            old_sy = t_nudge.start_y_mm
                            old_ex = t_nudge.end_x_mm
                            old_ey = t_nudge.end_y_mm
                            traces[nudge_idx] = TraceSegment(
                                nx_s, ny_s, nx_e, ny_e,
                                t_nudge.width_mm, t_nudge.layer,
                                t_nudge.net_id, t_nudge.net_name,
                            )
                            # Propagate endpoint shifts to connected same-net segments
                            # so we don't break the trace chain
                            for ki, kt in enumerate(traces):
                                if ki == nudge_idx or kt.net_id != t_nudge.net_id:
                                    continue
                                if kt.layer != t_nudge.layer:
                                    continue
                                updated = False
                                ks_x, ks_y = kt.start_x_mm, kt.start_y_mm
                                ke_x, ke_y = kt.end_x_mm, kt.end_y_mm
                                # Check if this segment's start matches the nudged segment's old start
                                if abs(ks_x - old_sx) < 0.01 and abs(ks_y - old_sy) < 0.01:
                                    ks_x, ks_y = nx_s, ny_s
                                    updated = True
                                # Check if this segment's start matches the nudged segment's old end
                                elif abs(ks_x - old_ex) < 0.01 and abs(ks_y - old_ey) < 0.01:
                                    ks_x, ks_y = nx_e, ny_e
                                    updated = True
                                # Check if this segment's end matches the nudged segment's old start
                                if abs(ke_x - old_sx) < 0.01 and abs(ke_y - old_sy) < 0.01:
                                    ke_x, ke_y = nx_s, ny_s
                                    updated = True
                                elif abs(ke_x - old_ex) < 0.01 and abs(ke_y - old_ey) < 0.01:
                                    ke_x, ke_y = nx_e, ny_e
                                    updated = True
                                if updated:
                                    traces[ki] = TraceSegment(
                                        ks_x, ks_y, ke_x, ke_y,
                                        kt.width_mm, kt.layer,
                                        kt.net_id, kt.net_name,
                                    )
                            violations_fixed += 1
                            break

        # Check via-trace clearance violations
        for v in vias:
            if v.net_name == "GND" and v.net_id.startswith("stitch_"):
                continue  # skip stitching vias
            via_radius = config.via_diameter_mm / 2
            for layer in ("top", "bottom"):
                for ti in layer_traces.get(layer, []):
                    t = traces[ti]
                    if t.net_id == v.net_id:
                        continue
                    tw = t.width_mm / 2
                    required = via_radius + tw + clearance
                    dist = _seg_point_dist(
                        t.start_x_mm, t.start_y_mm,
                        t.end_x_mm, t.end_y_mm,
                        v.x_mm, v.y_mm,
                    )
                    if dist >= required:
                        continue

                    # Nudge the trace away from the via
                    dx = t.end_x_mm - t.start_x_mm
                    dy = t.end_y_mm - t.start_y_mm
                    seg_len = math.hypot(dx, dy)
                    if seg_len < 1e-6:
                        continue
                    perp_x = -dy / seg_len
                    perp_y = dx / seg_len

                    mid_x = (t.start_x_mm + t.end_x_mm) / 2
                    mid_y = (t.start_y_mm + t.end_y_mm) / 2
                    away_dot = (mid_x - v.x_mm) * perp_x + (mid_y - v.y_mm) * perp_y
                    if away_dot < 0:
                        perp_x, perp_y = -perp_x, -perp_y

                    is_start_pad = any(
                        abs(t.start_x_mm - p.x_mm) < 0.05 and abs(t.start_y_mm - p.y_mm) < 0.05
                        for p in pad_map.values() if p.net_id == t.net_id
                    )
                    is_end_pad = any(
                        abs(t.end_x_mm - p.x_mm) < 0.05 and abs(t.end_y_mm - p.y_mm) < 0.05
                        for p in pad_map.values() if p.net_id == t.net_id
                    )

                    gap = required - dist
                    for mult in range(1, 6):
                        offset = nudge_step * mult
                        if offset < gap * 0.5:
                            continue
                        nx_s = t.start_x_mm + (0 if is_start_pad else perp_x * offset)
                        ny_s = t.start_y_mm + (0 if is_start_pad else perp_y * offset)
                        nx_e = t.end_x_mm + (0 if is_end_pad else perp_x * offset)
                        ny_e = t.end_y_mm + (0 if is_end_pad else perp_y * offset)

                        new_dist = _seg_point_dist(
                            nx_s, ny_s, nx_e, ny_e, v.x_mm, v.y_mm,
                        )
                        if new_dist >= required:
                            traces[ti] = TraceSegment(
                                nx_s, ny_s, nx_e, ny_e,
                                t.width_mm, t.layer, t.net_id, t.net_name,
                            )
                            violations_fixed += 1
                            break

        if violations_fixed == 0:
            break

    result.traces = traces


def _generate_silkscreen(
    placement: dict,
    netlist: dict,
    pad_map: dict[str, PadInfo],
) -> list[dict]:
    """Generate silkscreen elements for all placed components.

    Produces:
    - Designator text labels (e.g., "R1", "U1") positioned above each component
    - Pin 1 dot indicators for multi-pin components (ICs, connectors, headers)
    - Anode "A" markers for LEDs and diodes near the anode pad
    """
    elements = netlist.get("elements", [])

    # Build port lookup: component_id -> list of ports
    comp_ports: dict[str, list[dict]] = {}
    for elem in elements:
        if elem.get("element_type") == "port":
            cid = elem.get("component_id", "")
            comp_ports.setdefault(cid, []).append(elem)

    # Build component lookup
    components: dict[str, dict] = {}
    for elem in elements:
        if elem.get("element_type") == "component":
            components[elem["component_id"]] = elem

    silk: list[dict] = []

    for plc in placement.get("placements", []):
        des = plc["designator"]
        ctype = plc.get("component_type", "")
        layer = plc.get("layer", "top")
        cx, cy = plc["x_mm"], plc["y_mm"]
        rot = plc.get("rotation_deg", 0)
        w = plc["footprint_width_mm"]
        h = plc["footprint_height_mm"]

        if rot in (90, 270):
            w, h = h, w

        # Skip fiducials
        if ctype == "fiducial":
            continue

        # Silkscreen layer matches component layer
        silk_layer = f"{layer}_silk"

        # 1. Designator text — positioned above the component
        text_offset_y = h / 2 + 0.8  # 0.8mm above component top
        silk.append({
            "type": "text",
            "text": des,
            "x_mm": round(cx, 3),
            "y_mm": round(cy + text_offset_y, 3),
            "font_height_mm": 1.0,
            "layer": silk_layer,
            "anchor": "center",
        })

        # Find the component in netlist
        comp = None
        for cid, c in components.items():
            if c.get("designator") == des:
                comp = c
                break

        if not comp:
            continue

        comp_id = comp["component_id"]
        ports = comp_ports.get(comp_id, [])

        # 2. Pin 1 dot — for components with 3+ pins (ICs, connectors, headers)
        if len(ports) >= 3:
            pin1_port = next((p for p in ports if p.get("pin_number") == 1), None)
            if pin1_port and pin1_port["port_id"] in pad_map:
                pad = pad_map[pin1_port["port_id"]]
                # Place dot slightly outside the component body, toward pin 1
                dx = pad.x_mm - cx
                dy = pad.y_mm - cy
                # Normalize and push 0.5mm outside the component
                dist = max(0.1, math.hypot(dx, dy))
                dot_x = pad.x_mm + dx / dist * 0.5
                dot_y = pad.y_mm + dy / dist * 0.5
                silk.append({
                    "type": "dot",
                    "x_mm": round(dot_x, 3),
                    "y_mm": round(dot_y, 3),
                    "diameter_mm": 0.5,
                    "layer": silk_layer,
                    "purpose": "pin1",
                })

        # 3. Anode "A" marker — for LEDs and diodes
        if ctype in ("led", "diode"):
            # Find the anode pin (pin named "anode" or "a", or pin 1 for LEDs)
            anode_port = None
            for p in ports:
                name = p.get("name", "").lower()
                if name in ("anode", "a"):
                    anode_port = p
                    break
            if not anode_port:
                # Default: pin 1 is anode for LEDs/diodes
                anode_port = next((p for p in ports if p.get("pin_number") == 1), None)

            if anode_port and anode_port["port_id"] in pad_map:
                pad = pad_map[anode_port["port_id"]]
                # Place "A" label 0.8mm outside the anode pad
                dx = pad.x_mm - cx
                dy = pad.y_mm - cy
                dist = max(0.1, math.hypot(dx, dy))
                a_x = pad.x_mm + dx / dist * 1.0
                a_y = pad.y_mm + dy / dist * 1.0
                silk.append({
                    "type": "text",
                    "text": "A",
                    "x_mm": round(a_x, 3),
                    "y_mm": round(a_y, 3),
                    "font_height_mm": 1.0,
                    "layer": silk_layer,
                    "anchor": "center",
                    "purpose": "anode",
                })

    # Build exclusion zones from component pads, fiducials, and vias
    # Silkscreen text must not overlap copper features
    exclusion_zones: list[tuple[float, float, float, float]] = []  # (x_min, y_min, x_max, y_max)

    # Pad exclusion zones (with 0.2mm margin)
    pad_margin = 0.2
    for pad in pad_map.values():
        pw, ph = pad.pad_width_mm, pad.pad_height_mm
        exclusion_zones.append((
            pad.x_mm - pw / 2 - pad_margin,
            pad.y_mm - ph / 2 - pad_margin,
            pad.x_mm + pw / 2 + pad_margin,
            pad.y_mm + ph / 2 + pad_margin,
        ))

    # Fiducial exclusion zones (full mask opening = 3mm diameter)
    for plc in placement.get("placements", []):
        if plc.get("component_type") == "fiducial":
            r = max(plc.get("footprint_width_mm", 3.0), plc.get("footprint_height_mm", 3.0)) / 2
            exclusion_zones.append((
                plc["x_mm"] - r - pad_margin,
                plc["y_mm"] - r - pad_margin,
                plc["x_mm"] + r + pad_margin,
                plc["y_mm"] + r + pad_margin,
            ))

    def _text_overlaps_exclusion(x: float, y: float, text: str, fh: float, anchor: str) -> bool:
        """Check if a text label overlaps any exclusion zone."""
        char_w = fh * 0.6
        spacing = fh * 0.15
        total_w = len(text) * char_w + max(0, len(text) - 1) * spacing
        if anchor == "center":
            tx_min = x - total_w / 2
        elif anchor == "right":
            tx_min = x - total_w
        else:
            tx_min = x
        tx_max = tx_min + total_w
        ty_min = y
        ty_max = y + fh

        for ex_min, ey_min, ex_max, ey_max in exclusion_zones:
            if tx_min < ex_max and tx_max > ex_min and ty_min < ey_max and ty_max > ey_min:
                return True
        return False

    # Remove silkscreen items that overlap exclusion zones
    filtered_silk = []
    for item in silk:
        if item["type"] == "text":
            if _text_overlaps_exclusion(
                item["x_mm"], item["y_mm"],
                item["text"], item.get("font_height_mm", 1.0),
                item.get("anchor", "center"),
            ):
                continue  # skip overlapping text
        elif item["type"] == "dot":
            dx, dy = item["x_mm"], item["y_mm"]
            r = item.get("diameter_mm", 0.5) / 2
            overlaps = False
            for ex_min, ey_min, ex_max, ey_max in exclusion_zones:
                if dx + r > ex_min and dx - r < ex_max and dy + r > ey_min and dy - r < ey_max:
                    overlaps = True
                    break
            if overlaps:
                continue
        filtered_silk.append(item)
    silk = filtered_silk

    # Add surviving designator/anode text bounding boxes to exclusion zones
    # so that the board name/revision label won't overlap them
    for item in silk:
        if item["type"] == "text":
            fh = item.get("font_height_mm", 1.0)
            char_w = fh * 0.6
            spacing = fh * 0.15
            txt = item["text"]
            total_w = len(txt) * char_w + max(0, len(txt) - 1) * spacing
            anchor = item.get("anchor", "center")
            ix, iy = item["x_mm"], item["y_mm"]
            if anchor == "center":
                tx_min = ix - total_w / 2
            elif anchor == "right":
                tx_min = ix - total_w
            else:
                tx_min = ix
            margin = 0.3
            exclusion_zones.append((
                tx_min - margin,
                iy - margin,
                tx_min + total_w + margin,
                iy + fh + margin,
            ))

    # Board name and revision label
    board = placement.get("board", {})
    board_w = board.get("width_mm", 50)
    board_h = board.get("height_mm", 30)
    project_name = placement.get("project_name", "")
    if project_name:
        # Truncate long names to fit on silkscreen (max 15 chars)
        max_silk_chars = 15
        if len(project_name) > max_silk_chars:
            project_name = project_name[:max_silk_chars - 1].rstrip() + "…"

        # Try candidate positions for the board label (prefer bottom-right)
        candidates = [
            (board_w - 2.0, 2.0, "right"),       # bottom-right
            (2.0, 2.0, "left"),                   # bottom-left
            (board_w - 2.0, board_h - 4.0, "right"),  # top-right
            (2.0, board_h - 4.0, "left"),         # top-left
            (board_w / 2, 2.0, "center"),         # bottom-center
        ]

        for lx, ly, anchor in candidates:
            if not _text_overlaps_exclusion(lx, ly, project_name, 1.0, anchor) and \
               not _text_overlaps_exclusion(lx, ly + 1.5, "Rev 1.0", 0.8, anchor):
                silk.append({
                    "type": "text",
                    "text": project_name,
                    "x_mm": round(lx, 3),
                    "y_mm": round(ly, 3),
                    "font_height_mm": 1.0,
                    "layer": "top_silk",
                    "anchor": anchor,
                    "purpose": "board_name",
                })
                silk.append({
                    "type": "text",
                    "text": "Rev 1.0",
                    "x_mm": round(lx, 3),
                    "y_mm": round(ly + 1.5, 3),
                    "font_height_mm": 1.0,
                    "layer": "top_silk",
                    "anchor": anchor,
                    "purpose": "revision",
                })
                break

    return silk


def _build_output(
    placement: dict,
    netlist: dict,
    result: RoutingResult,
    ordered_nets: list[NetInfo],
    config: RouterConfig,
    pad_map: dict[str, PadInfo] | None = None,
    copper_fills: list[dict] | None = None,
) -> dict:
    """Build the routed JSON output dict."""
    total_nets = len(ordered_nets)
    routed_count = total_nets - len(result.unrouted_nets)

    # Compute trace lengths per layer
    top_length = sum(t.length_mm() for t in result.traces if t.layer == "top")
    bottom_length = sum(t.length_mm() for t in result.traces if t.layer == "bottom")

    output = dict(placement)  # shallow copy of placement data

    # Generate silkscreen
    if pad_map:
        output["silkscreen"] = _generate_silkscreen(placement, netlist, pad_map)

    output["routing"] = {
        "traces": [t.to_dict() for t in result.traces],
        "vias": [v.to_dict() for v in result.vias],
        "unrouted_nets": result.unrouted_nets,
        "statistics": {
            "total_nets": total_nets,
            "routed_nets": routed_count,
            "unrouted_nets": len(result.unrouted_nets),
            "completion_pct": round(routed_count / total_nets * 100, 1) if total_nets > 0 else 100.0,
            "total_trace_length_mm": round(top_length + bottom_length, 2),
            "via_count": len(result.vias),
            "layer_usage": {
                "top_trace_length_mm": round(top_length, 2),
                "bottom_trace_length_mm": round(bottom_length, 2),
            },
        },
        "config": {
            "copper_weight_oz": config.copper_weight_oz,
            "grid_resolution_mm": config.grid_resolution_mm,
            "trace_clearance_mm": config.clearance_mm,
            "via_drill_mm": config.via_drill_mm,
            "via_diameter_mm": config.via_diameter_mm,
        },
    }

    # Add trace width overrides info
    if result.trace_width_overrides:
        overrides_out: dict[str, str] = {}
        for net_id, width in result.trace_width_overrides.items():
            if isinstance(width, str):
                overrides_out[net_id] = width
            else:
                overrides_out[net_id] = f"{width:.3f}mm (IPC-2221 upsize)"
        output["routing"]["trace_width_overrides"] = overrides_out

    # Add copper fills
    if copper_fills:
        output["routing"]["copper_fills"] = copper_fills
        total_fill_polygons = sum(len(f["polygons"]) for f in copper_fills)
        output["routing"]["statistics"]["copper_fill_polygons"] = total_fill_polygons
        output["routing"]["statistics"]["copper_fill_layers"] = [
            f["layer"] for f in copper_fills
        ]

    return output


# ---------------------------------------------------------------------------
# Standalone copper fill for externally-routed boards (e.g., Freerouting)
# ---------------------------------------------------------------------------

def apply_copper_fills(
    routed: dict,
    netlist: dict,
    config: RouterConfig | None = None,
) -> dict:
    """Add copper fills to an already-routed design.

    Rebuilds a routing grid from the routed data (traces, vias, pads), then
    runs the standard copper fill algorithm (clearance, thermal relief,
    stitching vias, island removal).

    Also generates silkscreen if not already present.

    Args:
        routed: Routed dict with routing.traces and routing.vias.
        netlist: Netlist dict for pad/net information.
        config: RouterConfig for fill parameters. Uses defaults if None.

    Returns:
        Updated routed dict with routing.copper_fills added and
        stitching vias appended to routing.vias.
    """
    import copy as _copy

    if config is None:
        config = RouterConfig()

    board = routed.get("board", {})
    board_w = board.get("width_mm", 50.0)
    board_h = board.get("height_mm", 50.0)

    # Build pad map
    pad_map = build_pad_map(routed, netlist)

    # Build net_id -> integer mapping
    elements = netlist.get("elements", [])
    net_id_map: dict[str, int] = {}
    all_nets: list[dict] = []
    for elem in elements:
        if elem.get("element_type") == "net":
            all_nets.append(elem)

    # Assign integer IDs (1-indexed, matching convention)
    for i, net in enumerate(all_nets, start=1):
        net_id_map[net["net_id"]] = i

    # Find the fill net (GND)
    fill_net_name = config.fill_net_name
    fill_net_id = ""
    fill_net_int = 0
    for net in all_nets:
        if net.get("name", "") == fill_net_name:
            fill_net_id = net["net_id"]
            fill_net_int = net_id_map.get(fill_net_id, 0)
            break

    if fill_net_int == 0:
        print(f"  Copper fill: no '{fill_net_name}' net found, skipping")
        return routed

    # Build the grid
    grid = RoutingGrid(board_w, board_h, config.grid_resolution_mm)

    # Phase 1: Mark pads and obstacles (same as normal routing setup)
    _setup_grid(grid, routed, pad_map, config.clearance_mm, net_id_map)

    # Phase 2: Mark existing traces on the grid
    routing = routed.get("routing", {})
    traces = routing.get("traces", [])
    vias = routing.get("vias", [])

    for trace in traces:
        net_id = trace.get("net_id", "")
        nid = net_id_map.get(net_id, 0)
        if nid == 0:
            continue

        layer = trace.get("layer", "top")
        width = trace.get("width_mm", 0.25)

        # Mark the trace on the grid by walking from start to end
        sx, sy = trace["start_x_mm"], trace["start_y_mm"]
        ex, ey = trace["end_x_mm"], trace["end_y_mm"]

        # Calculate half-width in grid cells for the trace + clearance
        half_w_cells = max(1, int(math.ceil(
            (width / 2 + config.clearance_mm) / config.grid_resolution_mm
        )))

        # Walk the trace using Bresenham-like stepping
        sc, sr = grid.mm_to_grid(sx, sy)
        ec, er = grid.mm_to_grid(ex, ey)

        dc = abs(ec - sc)
        dr = abs(er - sr)
        step_c = 1 if ec > sc else -1 if ec < sc else 0
        step_r = 1 if er > sr else -1 if er < sr else 0

        # Simple line rasterization
        steps = max(dc, dr, 1)
        for step_i in range(steps + 1):
            t = step_i / steps if steps > 0 else 0
            c = int(round(sc + (ec - sc) * t))
            r = int(round(sr + (er - sr) * t))
            # Mark with trace width
            for ddc in range(-half_w_cells, half_w_cells + 1):
                for ddr in range(-half_w_cells, half_w_cells + 1):
                    nc, nr = c + ddc, r + ddr
                    if grid._in_bounds(nc, nr):
                        val = grid.get(nc, nr, layer)
                        if val == EMPTY:
                            grid.set(nc, nr, layer, nid)

    # Phase 3: Mark existing vias on the grid
    via_radius_cells = max(1, int(math.ceil(
        (config.via_diameter_mm / 2 + config.clearance_mm) / config.grid_resolution_mm
    )))
    for via in vias:
        net_id = via.get("net_id", "")
        nid = net_id_map.get(net_id, 0)
        if nid == 0:
            continue

        vc, vr = grid.mm_to_grid(via["x_mm"], via["y_mm"])
        for layer in ("top", "bottom"):
            for ddc in range(-via_radius_cells, via_radius_cells + 1):
                for ddr in range(-via_radius_cells, via_radius_cells + 1):
                    nc, nr = vc + ddc, vr + ddr
                    if grid._in_bounds(nc, nr):
                        val = grid.get(nc, nr, layer)
                        if val == EMPTY:
                            grid.set(nc, nr, layer, nid)

    # Phase 4: Run copper fill
    fill_regions, stitch_vias = create_copper_fill(
        grid, fill_net_int, fill_net_id, fill_net_name,
        pad_map, config,
    )

    # Phase 5: Update the routed dict
    result = _copy.deepcopy(routed)

    # Add copper fills
    result["routing"]["copper_fills"] = fill_regions

    # Add stitching vias
    stitch_via_dicts = [v.to_dict() for v in stitch_vias]
    result["routing"]["vias"] = result["routing"].get("vias", []) + stitch_via_dicts

    # Remove fill net from unrouted list (copper fill connects it)
    unrouted = result["routing"].get("unrouted_nets", [])
    if fill_net_id in unrouted and fill_regions:
        unrouted = [n for n in unrouted if n != fill_net_id]
        result["routing"]["unrouted_nets"] = unrouted

    # Update statistics
    stats = result["routing"].get("statistics", {})
    stats["via_count"] = len(result["routing"]["vias"])
    stats["routed_nets"] = stats.get("total_nets", 0) - len(unrouted)
    stats["unrouted_nets"] = len(unrouted)
    stats["completion_pct"] = round(
        100 * stats["routed_nets"] / stats.get("total_nets", 1), 1
    ) if stats.get("total_nets", 0) > 0 else 100.0
    if fill_regions:
        total_fill_polygons = sum(len(f["polygons"]) for f in fill_regions)
        stats["copper_fill_polygons"] = total_fill_polygons
        stats["copper_fill_layers"] = [f["layer"] for f in fill_regions]

    # Add config info
    result["routing"].setdefault("config", {})
    result["routing"]["config"]["fill_net"] = fill_net_name
    result["routing"]["config"]["fill_clearance_mm"] = config.fill_clearance_mm

    # Generate silkscreen if not present
    if not result.get("silkscreen"):
        result["silkscreen"] = _generate_silkscreen(routed, netlist, pad_map)

    print(f"  Copper fill: {len(fill_regions)} regions, "
          f"{sum(len(f['polygons']) for f in fill_regions)} polygons, "
          f"{len(stitch_vias)} stitching vias")

    return result
