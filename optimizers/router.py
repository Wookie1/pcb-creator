"""Copper pour, plane and silkscreen generation for a routed board.

Trace routing itself is done by Freerouting (see optimizers/freerouter.py);
this module post-processes its result:
- Copper fills with thermal relief, island removal and stitching vias
- Inner power/ground planes with via antipads (4-layer stackups)
- IPC-2221 trace width auto-calculation from copper weight + net current
- Silkscreen designator/marker placement
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass

from .pad_geometry import PadInfo, build_pad_map
from .ratsnest import NetInfo, build_connectivity

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

import logging

logger = logging.getLogger(__name__)


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
    board_edge_clearance_mm: float = 0.3   # copper-to-board-edge keepout
    # Fab's MINIMUM manufacturable via (None → unknown; DRC then floors the rule
    # at the smallest via actually on the board). Distinct from via_diameter_mm/
    # via_drill_mm, which are the sizes this board USES.
    via_diameter_min_mm: float | None = None
    via_drill_min_mm: float | None = None
    # Copper fill parameters
    fill_net_name: str = "GND"  # net name to use for fill (resolved at runtime)
    fill_clearance_mm: float = FILL_CLEARANCE_MM
    thermal_gap_mm: float = THERMAL_RELIEF_GAP_MM
    thermal_spoke_width_mm: float = THERMAL_RELIEF_SPOKE_WIDTH_MM


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

        # Voltage regulator max current — but only on the pins that actually
        # carry the load (IN/OUT/SW). Sense and control pins (FB, ADJ, EN,
        # ON/OFF, SS, COMP) see microamps; attributing the full load current
        # to them forces absurd trace widths on feedback nets.
        if ctype == "voltage_regulator":
            pin_name = str(port.get("name", "")).upper()
            is_sense_pin = bool(re.match(
                r"^(FB|FEEDBACK|SENSE|ADJ|COMP|SS|SOFT|EN|ENABLE|ON|CTRL|NC)",
                pin_name))
            if not is_sense_pin:
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


def compute_net_currents(netlist: dict) -> dict[str, float]:
    """Estimate current for every net, propagating through series elements.

    Per-net estimates (compute_net_current) only see components directly on
    the net — but the full load current flows THROUGH series inductors and
    fuses (e.g. a buck converter's L: SW node 3A → inductor → VOUT also 3A).
    Propagate the max current across 2-pin inductor/fuse components until
    stable.

    Returns {net_id: amps}.
    """
    nets = build_connectivity(netlist)
    currents = {net.net_id: compute_net_current(net, netlist)
                for net in nets}

    # Map series components (inductor/fuse) to the nets their pins touch
    elements = netlist.get("elements", [])
    series_cids = {e["component_id"] for e in elements
                   if e.get("element_type") == "component"
                   and e.get("component_type") in ("inductor", "fuse")}
    port_net: dict[str, str] = {}
    for e in elements:
        if e.get("element_type") == "net":
            for pid in e.get("connected_port_ids", []):
                port_net[pid] = e["net_id"]
    comp_nets: dict[str, set[str]] = {}
    for e in elements:
        if (e.get("element_type") == "port"
                and e.get("component_id") in series_cids
                and e.get("port_id") in port_net):
            comp_nets.setdefault(e["component_id"], set()).add(
                port_net[e["port_id"]])

    bridges = [tuple(nids) for nids in comp_nets.values() if len(nids) == 2]
    for _ in range(len(bridges) + 1):  # fixpoint over chains of series parts
        changed = False
        for a, b in bridges:
            peak = max(currents.get(a, 0.0), currents.get(b, 0.0))
            for nid in (a, b):
                if currents.get(nid, 0.0) < peak:
                    currents[nid] = peak
                    changed = True
        if not changed:
            break

    return currents


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
        pw, ph = pad.pad_width_mm, pad.pad_height_mm
        pad_radius = max(pw, ph) / 2
        clr = clearance_mm

        # Via-exclusion around EVERY pad — including no-net pads (mounting / NC /
        # unassigned). A stitching or rescue via dropped on a pad shorts it (or
        # trips hole-clearance), and the net-keyed marking below skips no-net
        # pads, so do this first and unconditionally.
        via_excl = pad_radius + clr
        grid.mark_no_via_rect(
            pad.x_mm - via_excl, pad.y_mm - via_excl,
            pad.x_mm + via_excl, pad.y_mm + via_excl,
        )

        if pad.net_id and pad.net_id in net_id_map:
            nid = net_id_map[pad.net_id]
            is_th = pad.layer == "all"

            eff_pw, eff_ph = pw, ph

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
        else:
            # No net at all — a no-connect pin, mounting pad or unassigned pad.
            # It is still real copper, and only the via exclusion above knew
            # that: the net-keyed branch skips it, so the ground pour flowed
            # straight over an unused pin and kicad-cli reported
            # "Items shorting two nets (nets  and GND)" against the blank net.
            # OBSTACLE keeps both the pour and the A* router off it. It belongs
            # to no net, so nothing may legitimately connect to it.
            th = pad.layer == "all"
            blk_layers = ("top", "bottom") if th else (pad.layer,)
            half_w = (max(pw, ph) if th else pw) / 2
            half_h = (max(pw, ph) if th else ph) / 2
            for blk_layer in blk_layers:
                grid.mark_rect(
                    pad.x_mm - half_w - clr, pad.y_mm - half_h - clr,
                    pad.x_mm + half_w + clr, pad.y_mm + half_h + clr,
                    blk_layer, OBSTACLE,
                )


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
            else:  # pragma: no cover - runs are sorted by (col_start, col_end, row); the prior branches cover every (ncs>=cs, nce>=ce) case, so this else is unreachable
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
                if not grid._in_bounds(nc, nr):  # pragma: no cover - stitch candidates are generated inset by via_radius_cells from every edge, so the ±via_radius footprint is always in bounds
                    return False
                # Respect via-exclusion zones (e.g. inner-layer signal traces a
                # through via would pierce).
                if not grid.can_place_via(nc, nr):
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
        if tc < 0 or bc < 0:  # pragma: no cover - candidates require filled_top[idx] and filled_bottom[idx], and BFS assigns a component id (>=0) to every filled cell
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
    inner_gnd_plane: bool = False,
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

        bottom_data = grid.layers["bottom"]
        for idx in cells:
            # A through via rescues the island if it reaches GND copper on
            # another layer: the bottom fill (2-layer), OR — on a 4-layer board —
            # the solid In1 GND plane directly (no bottom fill needed) (B5).
            # The plane path must still verify the BOTTOM cell: a through via
            # lands on the bottom layer too, and dropping it onto a foreign
            # signal trace there is a hard short.
            if filled_bottom[idx] or (
                    inner_gnd_plane
                    and bottom_data[idx] in (EMPTY, fill_net_int)):
                col, row = idx % cols, idx // cols
                # Skip cells where a through via would pierce an inner-layer
                # signal trace (via-exclusion zone).
                if not grid.can_place_via(col, row):
                    continue
                # Prefer candidates away from edges (at least 1 cell from island boundary)
                dist_to_center = abs(col - cx) + abs(row - cy)
                candidates.append((idx, dist_to_center))

        if not candidates:
            continue  # no reachable GND layer underneath — island can't be rescued

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
    inner_gnd_plane: bool = False,
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

    # Phase 2b: Rescue vias — connect top-layer islands through bottom fill, or
    # (4-layer) straight down to the solid In1 GND plane.
    rescue_vias = _add_rescue_vias(
        fill_bitmaps["top"], fill_bitmaps["bottom"],
        grid, fill_net_int, config,
        inner_gnd_plane=inner_gnd_plane,
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


def generate_inner_plane(
    board: dict,
    placements: list[dict],
    pad_map: dict,
    vias: list[dict],
    layer: str,
    net_id: str,
    net_name: str,
    config: "RouterConfig",
) -> dict:
    """Generate a solid copper plane on an inner layer for the given net.

    Produces a board-sized filled polygon with circular antipad cutouts
    around every through-hole pad and via that belongs to a different net.
    Thermal relief spokes are added for same-net through-hole pads.

    Args:
        board: Board dict with width_mm/height_mm.
        placements: All component placements.
        pad_map: PadInfo map from build_pad_map().
        vias: Routed via list.
        layer: Internal layer name (e.g. "inner1", "inner2").
        net_id: Net ID for the plane.
        net_name: Net name for the plane.
        config: RouterConfig for clearance/thermal parameters.

    Returns:
        Fill region dict with keys: layer, net_id, net_name, polygons.
    """
    w = board.get("width_mm", 50.0)
    h = board.get("height_mm", 50.0)

    # Solid board outline as the outer polygon
    outer = [(0.0, 0.0), (w, 0.0), (w, h), (0.0, h), (0.0, 0.0)]

    # Antipad radius = pad radius + clearance (IPC-2221 inner layer antipad)
    clearance = config.fill_clearance_mm
    thermal_gap = config.thermal_gap_mm
    thermal_spoke_w = config.thermal_spoke_width_mm
    ANTIPAD_SEGMENTS = 24  # circle approximation segments
    # The N-gon is inscribed in radius r, so its nearest edge (apothem) sits at
    # r·cos(pi/N) < r. Dividing the target radius by this factor pushes the
    # polygon edge out to the true target, so clearance holds after approximation.
    _INSCRIBE = math.cos(math.pi / ANTIPAD_SEGMENTS)

    def _circle_polygon(cx: float, cy: float, r: float) -> list[tuple[float, float]]:
        pts = []
        for i in range(ANTIPAD_SEGMENTS + 1):
            angle = 2 * math.pi * i / ANTIPAD_SEGMENTS
            pts.append((cx + r * math.cos(angle), cy + r * math.sin(angle)))
        return pts

    cutouts: list[list[tuple[float, float]]] = []

    # Through-hole pads and vias are the only features that penetrate inner layers.
    # SMD pads don't reach inner layers — skip them.
    for pad_info in pad_map.values():
        if pad_info.layer != "all":  # "all" = through-hole
            continue
        # Farthest copper of a RECTANGULAR pad is its corner (hypot(w,h)/2), not
        # max(w,h)/2 — using the latter left the corners inside the plane copper.
        # For round/oval pads this is conservative (a slightly larger void) but
        # never under-clears; tightening it needs per-pad shape data we don't carry.
        pad_r = math.hypot(pad_info.pad_width_mm, pad_info.pad_height_mm) / 2
        if pad_info.net_id == net_id:
            # Same-net pad: thermal relief — small clearance ring (no solid connection
            # on inner plane; stitching vias provide plane contact)
            r = (pad_r + thermal_gap) / _INSCRIBE
        else:
            # Foreign-net pad: full antipad clearance
            r = (pad_r + clearance) / _INSCRIBE
        cutouts.append(_circle_polygon(pad_info.x_mm, pad_info.y_mm, r))

    # Via antipads (vias are round, so radius = diameter/2 is the true reach)
    for via in vias:
        via_r = via.get("diameter_mm", 0.6) / 2
        if via.get("net_id") == net_id:
            r = (via_r + thermal_gap) / _INSCRIBE
        else:
            r = (via_r + clearance) / _INSCRIBE
        cutouts.append(_circle_polygon(via["x_mm"], via["y_mm"], r))

    # Represent as a polygon list: first entry is the outer boundary,
    # subsequent entries are holes (cutouts). KiCad zones handle holes
    # natively; Gerber fills are additive so we use the outer minus cutouts
    # approach (the gerber exporter renders each polygon as a separate region,
    # so we produce the board fill minus cutout discs via a negative fill approach).
    # For now: outer polygon first, then each cutout (exporters that support
    # holes use them; Gerber exporter paints the outer then clears cutouts).
    polygons = [outer] + cutouts

    return {
        "layer": layer,
        "net_id": net_id,
        "net_name": net_name,
        "polygons": polygons,
        "is_plane": True,  # flag for exporters: solid pour, not flood-fill
    }


def regenerate_inner_planes(routed: dict, netlist: dict,
                            config: RouterConfig | None = None) -> dict:
    """Re-cut inner-plane antipads against the CURRENT via set, in place.

    The plane antipads are cut once in `apply_copper_fills`, but `run_routing`'s
    protected-wiring union can re-add through-vias that Freerouting dropped
    *after* that — leaving them with no antipad in a power plane (the
    `inner_plane_antipad` "pad overlaps the 12V plane" error). Call this after
    any post-fill via change to refresh every `is_plane` fill. No-op when the
    board has no inner planes. Only the plane fills are rebuilt — GND outer fill
    and stitching vias are untouched (so nothing is duplicated)."""
    if config is None:
        config = RouterConfig()
    rt = routed.get("routing", {})
    fills = rt.get("copper_fills", [])
    if not any(f.get("is_plane") for f in fills):
        return routed
    board = routed.get("board", {})
    placements_list = routed.get("placements", [])
    pad_map = build_pad_map(routed, netlist)
    all_vias = rt.get("vias", [])
    rt["copper_fills"] = [
        generate_inner_plane(board, placements_list, pad_map, all_vias,
                             layer=f["layer"], net_id=f["net_id"],
                             net_name=f.get("net_name", ""), config=config)
        if f.get("is_plane") else f
        for f in fills
    ]
    return routed


def _silk_text_bbox(x: float, y: float, text: str, fh: float,
                    anchor: str = "center", angle: float = 0
                    ) -> tuple[float, float, float, float]:
    """Axis-aligned bounding box of a silk text label, accounting for anchor
    and a 0/90° rotation about its (x, y) anchor point."""
    char_w = fh * 0.6
    spacing = fh * 0.15
    total_w = len(text) * char_w + max(0, len(text) - 1) * spacing
    if anchor == "center":
        x0 = x - total_w / 2
    elif anchor == "right":
        x0 = x - total_w
    else:
        x0 = x
    # Text is centered vertically on the anchor y (matches KiCad gr_text and the
    # Gerber renderer), so a 90° rotation about (x, y) stays centered.
    y0, y1 = y - fh / 2, y + fh / 2
    corners = [(x0, y0), (x0 + total_w, y0), (x0 + total_w, y1), (x0, y1)]
    if angle:
        a = math.radians(angle)
        ca, sa = math.cos(a), math.sin(a)
        corners = [(x + (px - x) * ca - (py - y) * sa,
                    y + (px - x) * sa + (py - y) * ca) for px, py in corners]
    xs = [c[0] for c in corners]
    ys = [c[1] for c in corners]
    return (min(xs), min(ys), max(xs), max(ys))


def _boxes_overlap(a: tuple, b: tuple) -> bool:
    """True if two (x_min, y_min, x_max, y_max) boxes overlap."""
    return a[0] < b[2] and a[2] > b[0] and a[1] < b[3] and a[3] > b[1]


# Silk must sit inside the board outline with a little to spare: fabs clip
# everything outside it, so an "optimally relocated" designator that lands past
# the edge is simply MISSING from the assembled board. The bbox is the glyph
# CENTERLINE, so the margin must also cover the stroke half-width (~0.075mm at
# 1mm text) plus kicad-cli's silk-to-edge clearance (~0.2mm); 0.2 left the
# edge-most label (the title/rev block) just close enough to trip the
# "silkscreen clipped by board edge" DRC warning (#17).
SILK_EDGE_MARGIN_MM = 0.3


def _silk_on_board(bb: tuple, board_w: float, board_h: float) -> bool:
    """True if a silk bounding box is fully inside the board outline."""
    m = SILK_EDGE_MARGIN_MM
    return (bb[0] >= m and bb[1] >= m
            and bb[2] <= board_w - m and bb[3] <= board_h - m)


def _clamp_silk(x: float, y: float, bb: tuple,
                board_w: float, board_h: float) -> tuple[float, float]:
    """Shift an anchor so its bounding box lands inside the board outline.

    Used for the best-effort fallbacks: a label we could not place cleanly is
    still worth printing somewhere legible, but never off the edge where the
    fab would silently drop it.
    """
    m = SILK_EDGE_MARGIN_MM
    dx = dy = 0.0
    if bb[0] < m:
        dx = m - bb[0]
    elif bb[2] > board_w - m:
        dx = (board_w - m) - bb[2]
    if bb[1] < m:
        dy = m - bb[1]
    elif bb[3] > board_h - m:
        dy = (board_h - m) - bb[3]
    return x + dx, y + dy


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
    board = placement.get("board", {})
    board_w = board.get("width_mm", 50)
    board_h = board.get("height_mm", 30)

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

        # 1. Designator text — default above the component; the cleanup pass
        # below relocates it off pads/other silk (rotating 90° if needed).
        text_offset_y = h / 2 + 0.8  # 0.8mm above component top
        silk.append({
            "type": "text",
            "text": des,
            "x_mm": round(cx, 3),
            "y_mm": round(cy + text_offset_y, 3),
            "font_height_mm": 1.0,
            "layer": silk_layer,
            "anchor": "center",
            "purpose": "designator",
            "_box": (cx, cy, w, h),  # component extent, used by the relocator
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
                # Outward direction (centre -> pin-1 pad); the marker pass
                # below slides the dot along it until clear of copper/bodies.
                dx = pad.x_mm - cx
                dy = pad.y_mm - cy
                dist = max(0.1, math.hypot(dx, dy))
                silk.append({
                    "type": "dot",
                    "x_mm": round(pad.x_mm, 3),
                    "y_mm": round(pad.y_mm, 3),
                    "diameter_mm": 0.5,
                    "layer": silk_layer,
                    "purpose": "pin1",
                    "_marker": (pad.x_mm, pad.y_mm, pad.pad_width_mm / 2,
                                pad.pad_height_mm / 2, dx / dist, dy / dist),
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
                # Outward direction (centre -> anode pad); the marker pass
                # below places the "A" just past the pad edge, clear of copper.
                dx = pad.x_mm - cx
                dy = pad.y_mm - cy
                dist = max(0.1, math.hypot(dx, dy))
                silk.append({
                    "type": "text",
                    "text": "A",
                    "x_mm": round(pad.x_mm, 3),
                    "y_mm": round(pad.y_mm, 3),
                    "font_height_mm": 1.0,
                    "layer": silk_layer,
                    "anchor": "center",
                    "purpose": "anode",
                    "_marker": (pad.x_mm, pad.y_mm, pad.pad_width_mm / 2,
                                pad.pad_height_mm / 2, dx / dist, dy / dist),
                })

    # Build silk exclusion zones, split by severity:
    #   pad_zones  — pads + fiducial openings. Silk over an exposed pad is a
    #                real solder defect (mask sliver / poor wetting): HARD, never.
    #   body_zones — component housings. Silk here is merely hidden after
    #                assembly: SOFT, acceptable when a crowded board leaves no
    #                fully-clear spot (better a hidden label than one on a pad).
    pad_margin = 0.2
    pad_zones: list[tuple[float, float, float, float]] = []  # (x0,y0,x1,y1) HARD
    body_zones: list[tuple[float, float, float, float]] = []  # SOFT

    for pad in pad_map.values():
        pw, ph = pad.pad_width_mm, pad.pad_height_mm
        pad_zones.append((
            pad.x_mm - pw / 2 - pad_margin,
            pad.y_mm - ph / 2 - pad_margin,
            pad.x_mm + pw / 2 + pad_margin,
            pad.y_mm + ph / 2 + pad_margin,
        ))

    # Fiducial openings (full mask opening = 3mm diameter)
    for plc in placement.get("placements", []):
        if plc.get("component_type") == "fiducial":
            r = max(plc.get("footprint_width_mm", 3.0), plc.get("footprint_height_mm", 3.0)) / 2
            pad_zones.append((
                plc["x_mm"] - r - pad_margin,
                plc["y_mm"] - r - pad_margin,
                plc["x_mm"] + r + pad_margin,
                plc["y_mm"] + r + pad_margin,
            ))

    # Component bodies.
    # ponytail: layer-blind like the pad zones above — a top label also avoids
    # bottom-side bodies; conservative but simple. Split per-layer if two-sided
    # boards get crowded.
    for plc in placement.get("placements", []):
        if plc.get("component_type") == "fiducial":
            continue
        bw = plc.get("footprint_width_mm", 0) or 0
        bh = plc.get("footprint_height_mm", 0) or 0
        if plc.get("rotation_deg", 0) in (90, 270):
            bw, bh = bh, bw
        body_zones.append((
            plc["x_mm"] - bw / 2 - pad_margin,
            plc["y_mm"] - bh / 2 - pad_margin,
            plc["x_mm"] + bw / 2 + pad_margin,
            plc["y_mm"] + bh / 2 + pad_margin,
        ))

    # Markers slide off any copper/body (they must sit legibly beside their own
    # pad), so they weigh pads and bodies equally.
    exclusion_zones = pad_zones + body_zones

    # --- Marker cleanup: slide pin-1 dots / anode "A"s off copper -----------
    # A marker is meaningful only next to its pad, so it keeps its direction
    # (component centre -> pad) and slides outward, starting just past the pad
    # edge, until clear of pads/fiducials/bodies. Best effort: if nothing
    # within ~2.5mm is clear it stays at the pad-edge spot (never on the pad
    # itself — the old code offset from the pad CENTRE, which put "A" markers
    # on any pad longer than the offset).
    marker_margin = 0.15
    for item in silk:
        info = item.pop("_marker", None)
        if info is None:
            continue
        px, py, phw, phh, ux, uy = info
        if item["type"] == "dot":
            mhalf = item.get("diameter_mm", 0.5) / 2
        else:
            bb0 = _silk_text_bbox(0.0, 0.0, item["text"],
                                  item.get("font_height_mm", 1.0), "center")
            mhalf = (abs(ux) * (bb0[2] - bb0[0]) + abs(uy) * (bb0[3] - bb0[1])) / 2
        mx = my = None
        # Primary direction first; if everything within the walk cap is blocked
        # (dense board — e.g. the outward path runs into a neighbour's pad
        # field), try the two perpendicular directions, still hugging the pad.
        for vx, vy in ((ux, uy), (-uy, ux), (uy, -ux)):
            pad_ext = abs(vx) * phw + abs(vy) * phh  # pad half-extent along push
            base = pad_ext + marker_margin + mhalf
            if mx is None:                            # pad-edge fallback (primary dir)
                mx, my = px + vx * base, py + vy * base
            found = False
            for step in range(11):
                tx, ty = px + vx * (base + 0.25 * step), py + vy * (base + 0.25 * step)
                if item["type"] == "dot":
                    r = item.get("diameter_mm", 0.5) / 2
                    bb = (tx - r, ty - r, tx + r, ty + r)
                else:
                    bb = _silk_text_bbox(tx, ty, item["text"],
                                         item.get("font_height_mm", 1.0), "center")
                if (not any(_boxes_overlap(bb, z) for z in exclusion_zones)
                        and _silk_on_board(bb, board_w, board_h)):
                    mx, my = tx, ty
                    found = True
                    break
            if found:
                break
        # Fallback spot may sit past the edge on a part hard against it — pull it
        # back on-board rather than letting the fab clip the marker away.
        if item["type"] == "dot":
            r = item.get("diameter_mm", 0.5) / 2
            fb = (mx - r, my - r, mx + r, my + r)
        else:
            fb = _silk_text_bbox(mx, my, item["text"],
                                 item.get("font_height_mm", 1.0), "center")
        mx, my = _clamp_silk(mx, my, fb, board_w, board_h)
        item["x_mm"] = round(mx, 3)
        item["y_mm"] = round(my, 3)

    # --- Silkscreen cleanup: relocate designators ---------------------------
    # Each designator moves to the first position clear of pads/fiducials/
    # component bodies (exclusion_zones) AND of any already-placed silk, trying
    # upright (0°) spots all around the part first, then rotated 90°. Copper
    # traces are NOT avoided (silk over copper is allowed). Pin-1 dots and
    # anode "A" marks were finalized above and only act as obstacles here. If
    # nothing is clear the designator stays at its default spot (best effort)
    # rather than vanishing.
    out_silk: list[dict] = []

    # Pads (+ fiducials) are HARD obstacles a designator must never overlap;
    # bodies are SOFT. Pin-1 dots / anode "A"s already placed are HARD too.
    hard_zones = list(pad_zones)
    soft_zones = body_zones

    # Non-designator items: keep, and register each as a HARD obstacle.
    for item in silk:
        if item.get("purpose") == "designator":
            continue
        out_silk.append(item)
        if item["type"] == "text":
            hard_zones.append(_silk_text_bbox(
                item["x_mm"], item["y_mm"], item["text"],
                item.get("font_height_mm", 1.0), item.get("anchor", "center")))
        else:  # dot
            r = item.get("diameter_mm", 0.5) / 2
            hard_zones.append((item["x_mm"] - r, item["y_mm"] - r,
                               item["x_mm"] + r, item["y_mm"] + r))

    _GAPS = (0.8, 1.6, 2.4, 3.2, 4.0)
    # 8 directions (cardinals + diagonals): a dense passive row blocks all four
    # cardinals, but a diagonal often clears the neighbours' pads.
    _DIRS = ((0, 1), (0, -1), (1, 0), (-1, 0),
             (1, 1), (1, -1), (-1, 1), (-1, -1))
    for item in silk:
        if item.get("purpose") != "designator":
            continue
        cx0, cy0, cw, ch = item.pop("_box")
        fh = item.get("font_height_mm", 1.0)
        txt = item["text"]
        hw, hh = cw / 2, ch / 2
        chosen = None
        # Pass 1 keeps clear of pads AND bodies; pass 2 (crowded board) keeps
        # clear of pads/other silk only, accepting an unavoidable body overlap —
        # a hidden label beats one printed on an exposed pad.
        for avoid_bodies in (True, False):
            for angle in (0, 90):              # upright everywhere first, then rotate
                for gap in _GAPS:
                    for dx, dy in _DIRS:
                        tx = cx0 + dx * (hw + gap)
                        ty = cy0 + dy * (hh + gap)
                        bb = _silk_text_bbox(tx, ty, txt, fh, "center", angle)
                        if not _silk_on_board(bb, board_w, board_h):
                            continue
                        if any(_boxes_overlap(bb, z) for z in hard_zones):
                            continue
                        if avoid_bodies and any(_boxes_overlap(bb, z) for z in soft_zones):
                            continue
                        chosen = (tx, ty, angle, bb)
                        break
                    if chosen:
                        break
                if chosen:
                    break
            if chosen:
                break
        if chosen is None:
            # No spot clear of pads anywhere near the part — last resort: default
            # spot pulled inside the outline. (Rare; means every candidate hit a
            # pad or ran off-board.)
            tx, ty = cx0, cy0 + hh + 0.8
            bb = _silk_text_bbox(tx, ty, txt, fh, "center", 0)
            tx, ty = _clamp_silk(tx, ty, bb, board_w, board_h)
            chosen = (tx, ty, 0, _silk_text_bbox(tx, ty, txt, fh, "center", 0))
        tx, ty, angle, bb = chosen
        item["x_mm"] = round(tx, 3)
        item["y_mm"] = round(ty, 3)
        if angle:
            item["angle"] = angle
        hard_zones.append(bb)
        out_silk.append(item)

    silk = out_silk
    # Board-name/rev placement below must clear pads, markers, and every
    # relocated designator (all in hard_zones) plus bodies.
    exclusion_zones = hard_zones + soft_zones

    # Board name and revision label
    project_name = placement.get("project_name", "")
    if project_name:
        # Truncate long names to fit on silkscreen (max 15 chars). The ellipsis
        # must be ASCII: the stroke font has no "…" glyph and renders unknown
        # characters as ".", so "…" silently became a single dot.
        max_silk_chars = 15
        if len(project_name) > max_silk_chars:
            project_name = project_name[:max_silk_chars - 3].rstrip() + "..."

        # Try candidate positions for the board label (prefer bottom-right)
        candidates = [
            (board_w - 2.0, 2.0, "right"),       # bottom-right
            (2.0, 2.0, "left"),                   # bottom-left
            (board_w - 2.0, board_h - 4.0, "right"),  # top-right
            (2.0, board_h - 4.0, "left"),         # top-left
            (board_w / 2, 2.0, "center"),         # bottom-center
        ]

        for lx, ly, anchor in candidates:
            name_bb = _silk_text_bbox(lx, ly, project_name, 1.0, anchor)
            # fh must match the rendered item (font_height_mm=1.0 below) or the
            # on-board check underestimates the rev label's true extent (#17).
            rev_bb = _silk_text_bbox(lx, ly + 1.5, "Rev 1.0", 1.0, anchor)
            if not any(_boxes_overlap(name_bb, z) for z in exclusion_zones) and \
               not any(_boxes_overlap(rev_bb, z) for z in exclusion_zones) and \
               _silk_on_board(name_bb, board_w, board_h) and \
               _silk_on_board(rev_bb, board_w, board_h):
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


# ---------------------------------------------------------------------------
# Standalone copper fill for externally-routed boards (e.g., Freerouting)
# ---------------------------------------------------------------------------

def inner_plane_count(board: dict) -> int:
    """How many inner layers are solid PLANES (vs signal routing layers).

    Stackup convention (4-layer): In1.Cu is the first plane (GND), In2.Cu the
    second (power). board["plane_layers"] in {0,1,2} overrides; default 2 on a
    4-layer board (both inner = planes, the historical behaviour), 0 otherwise.
    plane_layers=1 frees In2.Cu for SIGNAL routing (GND plane only), roughly
    50%% more signal capacity for dense boards; power is then routed as traces.
    """
    n = int(board.get("layers", 2))
    if n < 4:
        return 0
    return max(0, min(2, int(board.get("plane_layers", 2))))


# Minimum drill-edge-to-drill-edge spacing for the hole_to_hole DRC rule. Two
# vias must be at least this far apart (edge to edge), so centre-to-centre must
# exceed drill + this.
HOLE_TO_HOLE_MIN_MM = 0.5
# Drill-edge-to-via clearance for keeping stitching vias away from mounting
# holes (the hole_clearance / hole_to_hole rules a via near an NPTH hole trips).
HOLE_TO_VIA_CLEARANCE_MM = 0.2

_MOUNTING_HOLE_DRILL_RE = re.compile(r"(\d+(?:\.\d+)?)mm", re.IGNORECASE)


def _mounting_hole_keepouts(placements: list[dict], via_diameter_mm: float
                            ) -> list[tuple[float, float, float]]:
    """(x, y, min_centre_dist) keepouts for mounting holes, so a stitching via
    never lands within hole-clearance of an NPTH hole. min_centre_dist =
    hole_radius + via_radius + clearance."""
    outs: list[tuple[float, float, float]] = []
    for p in placements:
        pkg = p.get("package", "")
        if "mountinghole" not in pkg.lower():
            continue
        m = _MOUNTING_HOLE_DRILL_RE.search(pkg)
        drill = float(m.group(1)) if m else 3.2
        min_d = drill / 2 + via_diameter_mm / 2 + HOLE_TO_VIA_CLEARANCE_MM
        outs.append((p.get("x_mm", 0.0), p.get("y_mm", 0.0), min_d))
    return outs


def _filter_via_hole_spacing(existing_vias: list[dict], new_vias: list[dict],
                             min_center_mm: float, *,
                             hole_keepouts: list[tuple[float, float, float]] = ()
                             ) -> list[dict]:
    """Keep only new (stitching/plane) vias whose centre is ≥ min_center_mm from
    every existing via and every already-kept new via (the hole_to_hole rule),
    AND outside every mounting-hole keepout (the hole_clearance rule). Existing
    routing vias are kept as-is and never dropped."""
    kept_pts = [(v.get("x_mm", 0.0), v.get("y_mm", 0.0)) for v in existing_vias]
    out: list[dict] = []
    m2 = min_center_mm * min_center_mm
    for v in new_vias:
        x, y = v.get("x_mm", 0.0), v.get("y_mm", 0.0)
        if any((x - px) ** 2 + (y - py) ** 2 < m2 for px, py in kept_pts):
            continue
        if any((x - hx) ** 2 + (y - hy) ** 2 < hd * hd
               for hx, hy, hd in hole_keepouts):
            continue
        kept_pts.append((x, y))
        out.append(v)
    return out


def _remove_dangling_traces(routing: dict, pad_map: dict,
                            tol: float = 0.06) -> int:
    """Drop trace segments with a free (unconnected) end — the `track_dangling`
    DRC warnings, leftover stubs from rip-up/reroute. Same-net aware: an endpoint
    is 'supported' only by a pad, via, or other trace OF THE SAME NET. Removing a
    stub off a free end can't disconnect the net (the free end joins nothing).
    Iterates so a chain of stubs collapses. Mutates routing['traces'] in place;
    returns how many were removed. Vias are intentionally NOT touched (plane
    stitching vias connect fills, not traces). Caller should still verify
    connectivity didn't regress."""
    traces = routing.get("traces", [])
    vias = routing.get("vias", [])
    if not traces:
        return 0

    pads_by_net: dict = {}
    for pi in pad_map.values():
        pads_by_net.setdefault(pi.net_id, []).append(
            (pi.x_mm, pi.y_mm, pi.pad_width_mm / 2 + tol, pi.pad_height_mm / 2 + tol))
    vias_by_net: dict = {}
    for v in vias:
        vias_by_net.setdefault(v.get("net_id"), []).append(
            (v.get("x_mm", 0.0), v.get("y_mm", 0.0)))

    def _near_pad(net, x, y):
        for px, py, hw, hh in pads_by_net.get(net, ()):  # bbox + tol
            if abs(x - px) <= hw and abs(y - py) <= hh:
                return True
        return False

    def _near_via(net, x, y):
        for vx, vy in vias_by_net.get(net, ()):
            if (x - vx) ** 2 + (y - vy) ** 2 <= tol * tol:
                return True
        return False

    def _on_other_trace(net, x, y, self_idx, kept):
        for j in kept:
            if j == self_idx:
                continue
            t = traces[j]
            if t.get("net_id") != net:
                continue
            # endpoint coincidence or lying on the segment
            ax, ay = t["start_x_mm"], t["start_y_mm"]
            bx, by = t["end_x_mm"], t["end_y_mm"]
            dx, dy = bx - ax, by - ay
            L2 = dx * dx + dy * dy
            if L2 == 0:
                if (x - ax) ** 2 + (y - ay) ** 2 <= tol * tol:
                    return True
                continue
            u = max(0.0, min(1.0, ((x - ax) * dx + (y - ay) * dy) / L2))
            px, py = ax + u * dx, ay + u * dy
            if (x - px) ** 2 + (y - py) ** 2 <= tol * tol:
                return True
        return False

    kept = set(range(len(traces)))
    removed = 0
    changed = True
    while changed:
        changed = False
        for i in list(kept):
            t = traces[i]
            net = t.get("net_id")
            for (x, y) in ((t["start_x_mm"], t["start_y_mm"]),
                           (t["end_x_mm"], t["end_y_mm"])):
                if not (_near_pad(net, x, y) or _near_via(net, x, y)
                        or _on_other_trace(net, x, y, i, kept)):
                    kept.discard(i)
                    removed += 1
                    changed = True
                    break
    if removed:
        routing["traces"] = [traces[i] for i in sorted(kept)]
    return removed


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

    # Remove dangling trace stubs (track_dangling DRC warnings) before pouring
    # fill — but only if it doesn't disconnect anything (revert on any new
    # incomplete net, so this can never trade a warning for a connectivity error).
    rt = routed.get("routing", {})
    if rt.get("traces"):
        try:
            from validators.validate_routing import incomplete_net_ids
            before = incomplete_net_ids(routed, netlist)
            snapshot = list(rt["traces"])
            n = _remove_dangling_traces(rt, pad_map)
            if n:
                if incomplete_net_ids(routed, netlist) - before:  # pragma: no cover - _remove_dangling_traces only drops segments with a FREE (unsupported) end, which by construction can't carry connectivity, so its removal can never add a newly-incomplete net; this is a defensive guard
                    rt["traces"] = snapshot  # regressed connectivity → revert
                else:
                    logger.info("  Removed %d dangling trace stub(s)", n)
        except Exception:  # pragma: no cover - defensive: incomplete_net_ids / dangling removal operate on already-validated dicts and don't raise in practice
            pass

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
        logger.info(f"  Copper fill: no '{fill_net_name}' net found, skipping")
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
        if nid == 0:  # pragma: no cover - a trace with an unmapped net_id has no same-net pad support, so the _remove_dangling_traces pass above always strips it before this marking loop runs
            continue

        layer = trace.get("layer", "top")
        width = trace.get("width_mm", 0.25)
        sx, sy = trace["start_x_mm"], trace["start_y_mm"]
        ex, ey = trace["end_x_mm"], trace["end_y_mm"]
        # The copper-fill grid only models the outer layers (where GND fill is
        # poured). Inner-layer signal traces (In1/In2 used as signal when
        # plane_layers<2) don't obstruct the outer fill or the inner GND plane —
        # but a THROUGH stitching/rescue via dropped on one would pierce the
        # inner layer and short it. Mark the inner trace's footprint (+ via
        # clearance) as a via-exclusion zone so fill vias steer clear of it.
        if layer not in grid.layers:
            if nid != fill_net_int:
                vmargin = (width / 2 + config.via_diameter_mm / 2
                           + config.clearance_mm)
                rad = max(1, int(math.ceil(vmargin / config.grid_resolution_mm)))
                isc, isr = grid.mm_to_grid(sx, sy)
                iec, ier = grid.mm_to_grid(ex, ey)
                nsteps = max(abs(iec - isc), abs(ier - isr), 1)
                for si in range(nsteps + 1):
                    t = si / nsteps
                    c = int(round(isc + (iec - isc) * t))
                    r = int(round(isr + (ier - isr) * t))
                    for ddc in range(-rad, rad + 1):
                        for ddr in range(-rad, rad + 1):
                            nc, nr = c + ddc, r + ddr
                            if grid._in_bounds(nc, nr):
                                grid.no_via[nr * grid.cols + nc] = True
            continue

        # Mark the outer-layer trace on the grid by walking from start to end.
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

    # Phase 4: Run copper fill (outer layers)
    # On a 4-layer board In1.Cu is a solid GND plane, so a through-via can rescue
    # an isolated outer GND-fill island by reaching that plane directly — it does
    # NOT need bottom-layer fill underneath (B5).
    fill_regions, stitch_vias = create_copper_fill(
        grid, fill_net_int, fill_net_id, fill_net_name,
        pad_map, config,
        inner_gnd_plane=inner_plane_count(board) >= 1,
    )

    pwr_net_id = fill_net_id
    pwr_stitch_vias: list[dict] = []
    pwr_plane_stubs: list[dict] = []
    # Plane-delivered SMD pads that found NO clear stitching-via site. These are
    # physically open (a plane net only reaches an SMD pad through its own via),
    # so the net must NOT be reported complete (B3). list of (designator, net_id).
    unstitched_plane_pads: list[tuple[str, str]] = []
    router_trace_w = config.trace_width_signal_mm

    # Phase 4b: Generate solid inner-layer planes for 4-layer boards.
    # Stackup convention: In1.Cu = GND plane, In2.Cu = PWR plane (if present).
    # PWR plane requires a designated power net; fall back to GND if not found.
    num_layers = board.get("layers", 2)
    pl = inner_plane_count(board)
    if pl >= 1:
        placements_list = routed.get("placements", [])

        # Identify inner2 power net first (most-connected non-GND power net).
        # Only when In2 is actually a PLANE (pl>=2); otherwise power is routed.
        pwr_net_name = fill_net_name
        best_count = 0
        for net in (all_nets if pl >= 2 else []):
            if net.get("net_class") == "power" and net.get("net_id") != fill_net_id:
                cnt = len(net.get("connected_port_ids", []))
                if cnt > best_count:
                    best_count = cnt
                    pwr_net_id = net["net_id"]
                    pwr_net_name = net.get("name", pwr_net_id)

        # Compute power stitching vias BEFORE generating inner planes so their
        # positions are known and can be included as via obstacles in the cutouts.
        via_r = config.via_diameter_mm / 2
        clearance = config.clearance_mm
        base_vias = routing.get("vias", []) + [v.to_dict() for v in stitch_vias]

        existing_via_positions: set[tuple[float, float]] = {
            (round(v.x_mm, 2), round(v.y_mm, 2)) for v in stitch_vias
        }
        # Drill hole-to-hole spacing, enforced HERE rather than only in the
        # post-filter. The obstacle lists below are copper-clearance checks that
        # skip same-net features, so two same-net stitch vias could land a drill
        # diameter apart: legal copper, undrillable board. _filter_via_hole_spacing
        # then deleted one — but its pad's stub trace stayed, leaving that pad
        # connected to nothing, the plane net reported unrouted, and export
        # blocked on a board whose plane was actually poured fine. Rejecting the
        # site here instead lets the candidate ring try the next position.
        hole_min_center = config.via_drill_mm + HOLE_TO_HOLE_MIN_MM
        pwr_hole_keepouts = _mounting_hole_keepouts(
            routed.get("placements", []), config.via_diameter_mm)
        drilled_pts: list[tuple[float, float]] = [
            (v.get("x_mm", 0.0), v.get("y_mm", 0.0)) for v in base_vias
        ]

        def _drillable(vx: float, vy: float) -> bool:
            m2 = hole_min_center * hole_min_center
            if any((vx - px) ** 2 + (vy - py) ** 2 < m2 for px, py in drilled_pts):
                return False
            return not any((vx - hx) ** 2 + (vy - hy) ** 2 < hd * hd
                           for hx, hy, hd in pwr_hole_keepouts)
        # Build foreign-obstacle list (non-pwr pads + all routed/stitch vias)
        # Round obstacles only (vias genuinely are circles). Foreign PADS are
        # checked as rectangles via pad_rects below: bounding a 0.3x1.475mm LQFP
        # pad with a max(w,h)/2 = 0.738mm circle overstates its narrow axis
        # ~5x, which walled off every legal via site around a fine-pitch part
        # and left its power pads unstitched.
        obstacles: list[tuple[float, float, float]] = []
        for ev in base_vias:
            if ev.get("net_id") == pwr_net_id:
                continue
            obstacles.append((ev["x_mm"], ev["y_mm"], ev.get("diameter_mm", config.via_diameter_mm) / 2))
        for sv in stitch_vias:
            obstacles.append((sv.x_mm, sv.y_mm, config.via_diameter_mm / 2))
        # Foreign-net routed traces are obstacles too — a through-via dropped
        # next to one shorts it (the fine-pitch via-trace clearance failure).
        # A power via crosses every layer, so any foreign trace counts.
        trace_obstacles: list[tuple[float, float, float, float, float]] = []
        for t in routing.get("traces", []):
            if t.get("net_id") == pwr_net_id:
                continue
            trace_obstacles.append((t["start_x_mm"], t["start_y_mm"],
                                    t["end_x_mm"], t["end_y_mm"],
                                    t.get("width_mm", 0.25) / 2))

        def _pt_seg_dist(px, py, ax, ay, bx, by):
            dx, dy = bx - ax, by - ay
            L2 = dx * dx + dy * dy
            tt = 0.0 if L2 == 0 else max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / L2))
            return math.hypot(px - (ax + tt * dx), py - (ay + tt * dy))

        # Foreign pads as RECTANGLES for the stub check below. The circular
        # `obstacles` list above uses max(w,h)/2, which on a 0.3x1.475mm LQFP pad
        # is a 0.738mm radius — nearly 5x the pad's narrow axis. That is fine for
        # siting a via (conservative) but would reject every stub leaving a
        # fine-pitch pad, since neighbours sit only 0.5mm away.
        pad_rects: list[tuple[float, float, float, float]] = [
            (fpi.x_mm, fpi.y_mm, fpi.pad_width_mm / 2, fpi.pad_height_mm / 2)
            for fpi in pad_map.values() if fpi.net_id != pwr_net_id
        ]

        def _seg_rect_gap(ax, ay, bx, by, cx, cy, hw, hh) -> float:
            """Distance from segment to an axis-aligned rect; 0 if it enters.

            Distance to a convex set is convex, so ternary search on t finds the
            true minimum. Projecting onto the rect CENTRE instead does not: a
            segment skimming a corner is nearest the rect well away from where it
            is nearest the centre, and that shortcut silently passed real
            overlaps.
            """
            def _pt_gap(t):
                px, py = ax + (bx - ax) * t, ay + (by - ay) * t
                return math.hypot(max(abs(px - cx) - hw, 0.0),
                                  max(abs(py - cy) - hh, 0.0))
            lo, hi = 0.0, 1.0
            for _ in range(60):
                m1 = lo + (hi - lo) / 3
                m2 = hi - (hi - lo) / 3
                if _pt_gap(m1) < _pt_gap(m2):
                    hi = m2
                else:
                    lo = m1
            return min(_pt_gap(0.0), _pt_gap(1.0), _pt_gap((lo + hi) / 2))

        def _seg_seg_gap(p1x, p1y, p2x, p2y, q1x, q1y, q2x, q2y) -> float:
            """Distance between two segments; 0 when they intersect.

            The intersection test is not optional here: for two segments meeting
            in an X, all four endpoint-to-segment distances are non-zero, so the
            usual min-of-four would report a comfortable gap across a crossing —
            exactly the case this guards.
            """
            d = (p2x - p1x) * (q2y - q1y) - (p2y - p1y) * (q2x - q1x)
            if abs(d) > 1e-12:
                t = ((q1x - p1x) * (q2y - q1y) - (q1y - p1y) * (q2x - q1x)) / d
                u = ((q1x - p1x) * (p2y - p1y) - (q1y - p1y) * (p2x - p1x)) / d
                if 0.0 <= t <= 1.0 and 0.0 <= u <= 1.0:
                    return 0.0
            return min(
                _pt_seg_dist(p1x, p1y, q1x, q1y, q2x, q2y),
                _pt_seg_dist(p2x, p2y, q1x, q1y, q2x, q2y),
                _pt_seg_dist(q1x, q1y, p1x, p1y, p2x, p2y),
                _pt_seg_dist(q2x, q2y, p1x, p1y, p2x, p2y),
            )

        def _stub_clear(px, py, vx, vy) -> bool:
            """The pad->via stub must not run into foreign copper.

            Only the VIA site was ever checked, so the stub that carries the pad
            to it crossed whatever lay between: the adjacent pin on an LQFP
            ("Items shorting two nets (nets VCC3V3 and )" once no-connect pads
            became real copper), and — since trace_obstacles was consulted for
            the via but not the stub — routed signal traces too, leaving a
            VCC3V3 stub crossing BOOT0 as the board's last DRC error.
            """
            half_w = router_trace_w / 2
            for cx, cy, hw, hh in pad_rects:
                if _seg_rect_gap(px, py, vx, vy, cx, cy, hw, hh) < half_w + clearance:
                    return False
            for ax, ay, bx, by, th in trace_obstacles:
                if _seg_seg_gap(px, py, vx, vy, ax, ay, bx, by) < half_w + th + clearance:
                    return False
            return True

        # Pads the escape fanout already dropped to the plane. Re-stitching them
        # is at best a redundant via and at worst a false "no clear via site" —
        # the escape via itself blocks every ring candidate around a crowded pad
        # (e.g. a quad pack's edge-facing power pin), so the pad is reported
        # unrouted though it is in fact connected. The power plane net is
        # EXCLUDED from Freerouting, so the only traces it can carry are the
        # protected escape stubs/fanouts — any such trace endpoint on a pad is an
        # escape delivery. (Can't key off escape_role: the Freerouting SES round-
        # trip strips it, and it is only re-attached after this stitch pass.)
        escaped_pad_keys: set[tuple[float, float]] = set()
        for t in routing.get("traces", []):
            if t.get("net_id") != pwr_net_id:
                continue
            escaped_pad_keys.add((round(t["start_x_mm"], 2), round(t["start_y_mm"], 2)))
            escaped_pad_keys.add((round(t["end_x_mm"], 2), round(t["end_y_mm"], 2)))

        for ref, pi in (pad_map.items() if pl >= 2 else []):
            if pi.net_id != pwr_net_id:
                continue
            if (round(pi.x_mm, 2), round(pi.y_mm, 2)) in escaped_pad_keys:
                continue  # already delivered to the plane by the escape fanout
            if pi.layer == "all":
                continue  # through-hole: already penetrates inner2
            placed = False
            # Try the pad CENTRE first (via-in-pad) so the pad and its plane
            # via coincide — a physical same-net connection. Fall back to a
            # widening ring of nearby positions (with a short stub trace) so a
            # crowded fine-pitch pad still finds a clear site.
            candidates = [(0.0, 0.0)]
            # Denser ring (more radii × 30° steps) so a crowded fine-pitch pad
            # tries harder to find a clear site before giving up (B3 retry).
            for radius in (0.5, 0.7, 0.9, 1.1, 1.3, 1.6, 2.0):
                for k in range(12):
                    ang = math.pi * k / 6.0
                    candidates.append((round(radius * math.cos(ang), 3),
                                       round(radius * math.sin(ang), 3)))
            for dx, dy in candidates:
                vx = round(pi.x_mm + dx, 4)
                vy = round(pi.y_mm + dy, 4)
                pos_key = (round(vx, 2), round(vy, 2))
                if pos_key in existing_via_positions:  # pragma: no cover - guards against a power-via candidate landing exactly on an already-placed GND-stitch/power via; requires two via sites to round to the identical 0.01mm key, which the spread-out pad/ring geometry doesn't produce on real boards
                    continue
                ok = True
                for fx, fy, fr in obstacles:
                    dist = math.sqrt((vx - fx) ** 2 + (vy - fy) ** 2)
                    if dist < via_r + fr + clearance:
                        ok = False
                        break
                if ok:
                    for cx, cy, hw, hh in pad_rects:
                        gap = math.hypot(max(abs(vx - cx) - hw, 0.0),
                                         max(abs(vy - cy) - hh, 0.0))
                        if gap < via_r + clearance:
                            ok = False
                            break
                if ok:
                    for ax, ay, bx, by, th in trace_obstacles:
                        if _pt_seg_dist(vx, vy, ax, ay, bx, by) < via_r + th + clearance:
                            ok = False
                            break
                if ok and not _drillable(vx, vy):
                    ok = False
                if ok and (dx, dy) != (0.0, 0.0) and not _stub_clear(
                        pi.x_mm, pi.y_mm, vx, vy):
                    ok = False
                if ok:
                    existing_via_positions.add(pos_key)
                    drilled_pts.append((vx, vy))
                    pwr_stitch_vias.append({
                        "x_mm": vx,
                        "y_mm": vy,
                        "drill_mm": config.via_drill_mm,
                        "diameter_mm": config.via_diameter_mm,
                        # Through via: it must cross inner2 to reach the power
                        # plane, and generate_inner_plane already antipads every
                        # foreign-net via on the way past inner1. Omitting the
                        # pair serialised as None, and the connectivity check
                        # then refused to credit these vias with touching the
                        # plane — the plane was poured correctly but its net was
                        # still reported unrouted, blocking export.
                        "from_layer": "top",
                        "to_layer": "bottom",
                        "net_id": pwr_net_id,
                        "net_name": pwr_net_name,
                    })
                    # When the via is offset from the pad, add a short stub on
                    # the pad's layer so the pad physically reaches the via.
                    if (dx, dy) != (0.0, 0.0):
                        pwr_plane_stubs.append({
                            "start_x_mm": round(pi.x_mm, 4),
                            "start_y_mm": round(pi.y_mm, 4),
                            "end_x_mm": vx, "end_y_mm": vy,
                            "width_mm": router_trace_w,
                            "layer": pi.layer,
                            "net_id": pwr_net_id,
                            "net_name": pwr_net_name,
                        })
                    placed = True
                    break
            if not placed:
                logger.warning("  Power plane: no clear via site for %s pad "
                               "%s.%s — unconnected to %s plane (net kept unrouted)",
                               pwr_net_name, pi.designator, pi.pin_number, pwr_net_name)
                unstitched_plane_pads.append((pi.designator, pi.net_id))

        if pwr_stitch_vias:
            logger.info(f"  Power via stitching: {len(pwr_stitch_vias)} vias for {pwr_net_name}")

        # Now generate inner planes with the complete via list (routing + pwr stitching)
        all_vias = base_vias + pwr_stitch_vias

        # Inner layer 1 → GND plane
        gnd_plane = generate_inner_plane(
            board, placements_list, pad_map, all_vias,
            layer="inner1",
            net_id=fill_net_id,
            net_name=fill_net_name,
            config=config,
        )
        fill_regions.append(gnd_plane)

        # Inner layer 2 → power plane (only when In2 is a plane, not signal)
        if pl >= 2:
            pwr_plane = generate_inner_plane(
                board, placements_list, pad_map, all_vias,
                layer="inner2",
                net_id=pwr_net_id,
                net_name=pwr_net_name,
                config=config,
            )
            fill_regions.append(pwr_plane)

    # Phase 5: Update the routed dict
    result = _copy.deepcopy(routed)

    # Add copper fills
    result["routing"]["copper_fills"] = fill_regions

    # Add stitching vias (GND outer-layer + power plane SMD stitching), but drop
    # any whose drill would sit closer than the hole-to-hole minimum to an
    # existing routing via or an already-kept stitching via — those trip the
    # hole_to_hole DRC rule (observed: two GND vias 0.25mm apart). Stitching vias
    # are redundant plane connections, so dropping a too-close one is safe; the
    # existing routing vias are never dropped.
    stitch_via_dicts = [v.to_dict() for v in stitch_vias]
    if pl >= 2:
        stitch_via_dicts.extend(pwr_stitch_vias)
    existing_vias = result["routing"].get("vias", [])
    min_center = config.via_drill_mm + HOLE_TO_HOLE_MIN_MM
    hole_keepouts = _mounting_hole_keepouts(routed.get("placements", []),
                                            config.via_diameter_mm)
    kept = _filter_via_hole_spacing(existing_vias, stitch_via_dicts, min_center,
                                    hole_keepouts=hole_keepouts)
    if len(kept) < len(stitch_via_dicts):
        logger.info("  Dropped %d stitching via(s) too close to another via or "
                    "a mounting hole", len(stitch_via_dicts) - len(kept))
    result["routing"]["vias"] = existing_vias + kept

    # Add power-plane connection stubs (pad → offset via)
    if pl >= 2 and pwr_plane_stubs:
        result["routing"]["traces"] = (
            result["routing"].get("traces", []) + pwr_plane_stubs)

    # Remove plane nets from unrouted list (copper fills connect them) — but a
    # plane net is only delivered when EVERY same-net SMD pad actually reaches the
    # plane through a stitching via. Pads with no clear via site (above) leave the
    # net physically open, so keep those nets unrouted instead of reporting the
    # board complete (B3: net-level completion masked an open power pad).
    unrouted = result["routing"].get("unrouted_nets", [])
    plane_net_ids = {fill_net_id} if pl >= 1 else set()
    if pl >= 2:
        plane_net_ids.add(pwr_net_id)
    unstitched_net_ids = {nid for _, nid in unstitched_plane_pads}
    if fill_regions:
        strip = plane_net_ids - unstitched_net_ids        # fully-connected planes
        unrouted = [n for n in unrouted if n not in strip]
        for nid in plane_net_ids & unstitched_net_ids:    # open plane → stays unrouted
            if nid not in unrouted:
                unrouted.append(nid)
        result["routing"]["unrouted_nets"] = unrouted
    if unstitched_plane_pads:
        result["routing"]["unstitched_plane_pads"] = [
            {"designator": ref, "net_id": nid}
            for ref, nid in unstitched_plane_pads
        ]

    # B6: completion must reflect ACTUAL pad connectivity, not the autorouter's
    # net-level report. Reconcile unrouted_nets against the authoritative
    # connectivity check (segment-aware union-find over traces/vias, crediting
    # copper-fill/plane delivery) so a net the router left with a pad gap — e.g. a
    # point-to-point signal it counted as done — is reported unrouted instead of
    # silently credited as 100%. incomplete_net_ids reads the current
    # unrouted_nets as its base, so the B3 plane-pad entries above are preserved.
    try:
        from validators.validate_routing import incomplete_net_ids
        unrouted = sorted(incomplete_net_ids(result, netlist))
        result["routing"]["unrouted_nets"] = unrouted
    except Exception:  # pragma: no cover - defensive: connectivity check operates on an already-built routed/netlist and doesn't raise in practice
        pass

    # Update statistics
    stats = result["routing"].get("statistics", {})
    stats["via_count"] = len(result["routing"]["vias"])
    # unrouted may now include plane nets that ses_importer never counted in
    # total_nets (B3 keeps an unstitched plane net unrouted) — grow the
    # denominator so routed_nets/completion_pct stay non-negative true fractions
    # instead of e.g. -1 nets / -100%.
    total = max(stats.get("total_nets", 0), len(unrouted))
    stats["routed_nets"] = total - len(unrouted)
    stats["unrouted_nets"] = len(unrouted)
    stats["completion_pct"] = round(
        100 * stats["routed_nets"] / total, 1) if total > 0 else 100.0
    if fill_regions:
        total_fill_polygons = sum(len(f["polygons"]) for f in fill_regions)
        stats["copper_fill_polygons"] = total_fill_polygons
        stats["copper_fill_layers"] = [f["layer"] for f in fill_regions]

    # Add config info
    result["routing"].setdefault("config", {})
    result["routing"]["config"]["fill_net"] = fill_net_name
    result["routing"]["config"]["fill_clearance_mm"] = config.fill_clearance_mm
    # Persist the copper-to-edge keepout so the exported board's DRC rule matches
    # the pour; without it kicad-cli falls back to its stricter 0.5mm default and
    # false-flags copper the fab accepts. getattr keeps this working whether or
    # not RouterConfig carries the field (0.3mm = the generic-fab safe default).
    result["routing"]["config"].setdefault(
        "board_edge_clearance_mm", getattr(config, "board_edge_clearance_mm", 0.3))

    # Generate silkscreen if not present
    if not result.get("silkscreen"):
        result["silkscreen"] = _generate_silkscreen(routed, netlist, pad_map)

    logger.info(f"  Copper fill: {len(fill_regions)} regions, "
          f"{sum(len(f['polygons']) for f in fill_regions)} polygons, "
          f"{len(stitch_vias)} stitching vias")

    return result
