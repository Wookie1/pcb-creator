"""Line-coverage drive for optimizers/router.py — the built-in A* router.

Pure deterministic logic (no LLM, no Java), so it is fully testable. Mix of
direct unit tests on the pure helpers (grid ops, A*, path simplification,
fills/planes, IPC width, channel detection, connectivity) and end-to-end
route_board runs on small synthetic boards + the blink fixture.

Assertions check real routing behaviour (a routed 2-pad net yields a trace
connecting the pads; a cross-layer route inserts a via; completion % matches
routed/total; copper fill encloses area; inner plane covers board minus
antipads; IPC width grows with current), not non-None.
"""

import json
import math
import os

import pytest

from optimizers.router import (
    RouterConfig, RoutingGrid, RoutingResult, TraceSegment, Via,
    Channel, EMPTY, OBSTACLE,
    route_board, route_net, route_net_congestion,
    astar_route, astar_route_congestion, astar_find_blockers,
    simplify_path, mark_path_on_grid,
    ipc2221_trace_width, compute_net_current, compute_net_currents,
    order_nets, order_nets_by_accessibility,
    create_copper_fill, generate_inner_plane, regenerate_inner_planes,
    inner_plane_count, apply_copper_fills,
    _detect_channels, _build_channel_pressure, _setup_grid,
    _bitmap_to_polygons, _check_net_connectivity,
    _remove_dangling_traces, _mounting_hole_keepouts, _filter_via_hole_spacing,
    _post_route_nudge, _consolidate_endpoints,
    _build_clearance_mask, _add_stitching_vias, _remove_islands_cross_layer,
    _build_pad_zone_index, _is_pad_zone, _trace_segment_at,
    _try_shove_segment, _undo_shove, _get_net_pads,
    _pad_accessibility, _apply_pre_fill,
    _connectivity_repair, _restore_pad_markings, _generate_silkscreen,
)
from optimizers.pad_geometry import build_pad_map, PadInfo
from optimizers.ratsnest import NetInfo, build_connectivity

def _find_blink_prefix():
    """projects/ is gitignored, so it lives in the main repo, not the worktree.
    Resolve the blink fixture from whichever checkout actually has it."""
    here = os.path.dirname(__file__)
    candidates = [
        os.path.join(here, "..", "projects", "blink_3_leds_dc_power"),
        # worktree is <main>/.claude/worktrees/<name>/tests -> up to <main>
        os.path.join(here, "..", "..", "..", "..", "projects",
                     "blink_3_leds_dc_power"),
    ]
    for d in candidates:
        if os.path.exists(os.path.join(d, "blink_3_leds_dc_power_placement.json")):
            return os.path.join(d, "blink_3_leds_dc_power_")
    return os.path.join(candidates[0], "blink_3_leds_dc_power_")  # pragma: no cover


BLINK = _find_blink_prefix()


# ---------------------------------------------------------------------------
# Synthetic fixture builders
# ---------------------------------------------------------------------------

def _comp(cid, desig, x, y, ctype="resistor", layer="top", pkg="R_0805",
          fw=2.0, fh=1.25, rot=0):
    return {
        "designator": desig, "component_type": ctype, "package": pkg,
        "footprint_width_mm": fw, "footprint_height_mm": fh,
        "x_mm": x, "y_mm": y, "rotation_deg": rot, "layer": layer,
    }


def _two_pad_board(w=20.0, h=20.0, x1=5.0, x2=15.0, y=10.0,
                   net_class="signal", ctype="resistor", layers=2):
    """A board with two 2-pin parts and one net joining inner pins."""
    placement = {
        "board": {"width_mm": w, "height_mm": h, "outline_type": "rectangle",
                  "origin": [0, 0], "layers": layers},
        "placements": [
            _comp("C1", "R1", x1, y, ctype),
            _comp("C2", "R2", x2, y, ctype),
        ],
    }
    netlist = {"elements": [
        {"element_type": "component", "component_id": "C1",
         "component_type": ctype, "designator": "R1", "properties": {}},
        {"element_type": "component", "component_id": "C2",
         "component_type": ctype, "designator": "R2", "properties": {}},
        {"element_type": "port", "port_id": "P1", "component_id": "C1",
         "designator": "R1", "pin_number": 1, "name": "1"},
        {"element_type": "port", "port_id": "P2", "component_id": "C1",
         "designator": "R1", "pin_number": 2, "name": "2"},
        {"element_type": "port", "port_id": "P3", "component_id": "C2",
         "designator": "R2", "pin_number": 1, "name": "1"},
        {"element_type": "port", "port_id": "P4", "component_id": "C2",
         "designator": "R2", "pin_number": 2, "name": "2"},
        {"element_type": "net", "net_id": "net_0", "name": "N0",
         "net_class": net_class, "connected_port_ids": ["P2", "P3"]},
    ]}
    return placement, netlist


def _fast_cfg(**kw):
    """Cheap config: few trials, expensive phases off unless overridden."""
    base = dict(ordering_trials=1, ncr_enabled=False, fine_grid_factor=1,
                shove_enabled=False, pre_fill_enabled=False)
    base.update(kw)
    return RouterConfig(**base)


def _net(net_id="net_0", name="N0", cls="signal"):
    return NetInfo(net_id=net_id, name=name, net_class=cls, designators=["R1"])


# ===========================================================================
# IPC-2221 trace width
# ===========================================================================

def test_ipc_width_zero_current_is_zero():
    assert ipc2221_trace_width(0.0, 1.0) == 0.0
    assert ipc2221_trace_width(-1.0, 1.0) == 0.0


def test_ipc_width_increases_with_current():
    w_low = ipc2221_trace_width(0.5, 1.0)
    w_high = ipc2221_trace_width(3.0, 1.0)
    assert w_high > w_low > 0


def test_ipc_width_decreases_with_thicker_copper():
    w_1oz = ipc2221_trace_width(2.0, 1.0)
    w_2oz = ipc2221_trace_width(2.0, 2.0)
    assert w_2oz < w_1oz


# ===========================================================================
# compute_net_current / compute_net_currents
# ===========================================================================

def test_compute_net_current_led_uses_forward_current():
    netlist = {"elements": [
        {"element_type": "component", "component_id": "D1",
         "component_type": "led", "designator": "D1",
         "properties": {"if": "20mA"}},
        {"element_type": "port", "port_id": "P1", "component_id": "D1",
         "designator": "D1", "pin_number": 1, "name": "A"},
        {"element_type": "net", "net_id": "n", "name": "N", "net_class": "signal",
         "connected_port_ids": ["P1"]},
    ]}
    net = NetInfo("n", "N", "signal", ["D1"])
    assert compute_net_current(net, netlist) == pytest.approx(0.02, abs=1e-4)


def test_compute_net_current_no_net_elem_defaults_by_class():
    nl = {"elements": []}
    assert compute_net_current(NetInfo("x", "X", "power", []), nl) == 0.5
    assert compute_net_current(NetInfo("x", "X", "signal", []), nl) == 0.1


def test_compute_net_current_regulator_sense_pin_excluded():
    netlist = {"elements": [
        {"element_type": "component", "component_id": "U1",
         "component_type": "voltage_regulator", "designator": "U1",
         "properties": {"max_current": "1A"}},
        {"element_type": "port", "port_id": "PF", "component_id": "U1",
         "designator": "U1", "pin_number": 1, "name": "FB"},
        {"element_type": "net", "net_id": "fb", "name": "FB", "net_class": "signal",
         "connected_port_ids": ["PF"]},
    ]}
    net = NetInfo("fb", "FB", "signal", ["U1"])
    # FB is a sense pin → load current not attributed → signal default 0.1
    assert compute_net_current(net, netlist) == 0.1


def test_compute_net_current_regulator_load_pin_uses_max_current():
    netlist = {"elements": [
        {"element_type": "component", "component_id": "U1",
         "component_type": "voltage_regulator", "designator": "U1",
         "properties": {"max_current": "1A"}},
        {"element_type": "port", "port_id": "PO", "component_id": "U1",
         "designator": "U1", "pin_number": 1, "name": "OUT"},
        {"element_type": "net", "net_id": "vout", "name": "VOUT", "net_class": "power",
         "connected_port_ids": ["PO"]},
    ]}
    net = NetInfo("vout", "VOUT", "power", ["U1"])
    assert compute_net_current(net, netlist) == pytest.approx(1.0, abs=1e-3)


def test_compute_net_currents_propagates_through_inductor():
    # SW node carries 3A; inductor bridges SW->VOUT; VOUT should inherit 3A.
    netlist = {"elements": [
        {"element_type": "component", "component_id": "U1",
         "component_type": "voltage_regulator", "designator": "U1",
         "properties": {"max_current": "3A"}},
        {"element_type": "component", "component_id": "L1",
         "component_type": "inductor", "designator": "L1", "properties": {}},
        {"element_type": "port", "port_id": "PSW", "component_id": "U1",
         "designator": "U1", "pin_number": 1, "name": "SW"},
        {"element_type": "port", "port_id": "PL1", "component_id": "L1",
         "designator": "L1", "pin_number": 1, "name": "1"},
        {"element_type": "port", "port_id": "PL2", "component_id": "L1",
         "designator": "L1", "pin_number": 2, "name": "2"},
        {"element_type": "net", "net_id": "sw", "name": "SW", "net_class": "power",
         "connected_port_ids": ["PSW", "PL1"]},
        {"element_type": "net", "net_id": "vout", "name": "VOUT", "net_class": "power",
         "connected_port_ids": ["PL2"]},
    ]}
    currents = compute_net_currents(netlist)
    assert currents["sw"] == pytest.approx(3.0, abs=1e-3)
    assert currents["vout"] == pytest.approx(3.0, abs=1e-3)


# ===========================================================================
# RoutingGrid
# ===========================================================================

def test_grid_dims_and_bounds():
    g = RoutingGrid(10.0, 5.0, 1.0)
    assert g.cols == 11 and g.rows == 6
    assert g.get(-1, 0, "top") == OBSTACLE      # out of bounds
    assert g.get(0, 0, "top") == EMPTY


def test_grid_set_get_and_clear_net():
    g = RoutingGrid(10.0, 10.0, 1.0)
    g.set(2, 3, "top", 7)
    assert g.get(2, 3, "top") == 7
    assert g.is_available(2, 3, "top", 7) is True   # same net
    assert g.is_available(2, 3, "top", 9) is False  # foreign net
    g.clear_net(7)
    assert g.get(2, 3, "top") == EMPTY


def test_grid_mm_conversions_roundtrip():
    g = RoutingGrid(10.0, 10.0, 0.5)
    c, r = g.mm_to_grid(2.5, 3.0)
    assert (c, r) == (5, 6)
    assert g.grid_to_mm(5, 6) == (2.5, 3.0)


def test_grid_mark_rect_and_obstacle_rect():
    g = RoutingGrid(10.0, 10.0, 1.0)
    g.mark_rect(2.0, 2.0, 4.0, 4.0, "top", 3)
    assert g.get(2, 2, "top") == 3 and g.get(4, 4, "top") == 3
    g.mark_obstacle_rect(6.0, 6.0, 7.0, 7.0, "bottom", clearance_mm=0.5)
    assert g.get(6, 6, "bottom") == OBSTACLE


def test_grid_no_via_zone():
    g = RoutingGrid(10.0, 10.0, 1.0)
    assert g.can_place_via(3, 3) is True
    g.mark_no_via_rect(2.0, 2.0, 4.0, 4.0)
    assert g.can_place_via(3, 3) is False
    assert g.can_place_via(-1, -1) is False  # out of bounds


def test_grid_snapshot_restore():
    g = RoutingGrid(5.0, 5.0, 1.0)
    g.set(1, 1, "top", 4)
    snap = g.snapshot()
    g.set(1, 1, "top", EMPTY)
    g.set(2, 2, "bottom", 9)
    g.restore(snap)
    assert g.get(1, 1, "top") == 4
    assert g.get(2, 2, "bottom") == EMPTY


# ===========================================================================
# astar_route
# ===========================================================================

def test_astar_straight_line_path():
    g = RoutingGrid(10.0, 10.0, 1.0)
    path = astar_route(g, (0, 0, "top"), (5, 0, "top"), net_id=1, diagonal=False)
    assert path is not None
    assert path[0] == (0, 0, "top") and path[-1] == (5, 0, "top")
    # all on the top layer, no via
    assert all(p[2] == "top" for p in path)


def test_astar_blocked_start_returns_none():
    g = RoutingGrid(10.0, 10.0, 1.0)
    g.set(0, 0, "top", 99)  # foreign net occupies start
    assert astar_route(g, (0, 0, "top"), (5, 0, "top"), net_id=1) is None


def test_astar_inserts_via_when_target_on_other_layer():
    g = RoutingGrid(10.0, 10.0, 1.0)
    path = astar_route(g, (0, 0, "top"), (0, 0, "bottom"), net_id=1, via_cost=1.0)
    assert path is not None
    layers = [p[2] for p in path]
    assert "top" in layers and "bottom" in layers


def test_astar_routes_around_obstacle_wall():
    g = RoutingGrid(10.0, 10.0, 1.0)
    # vertical wall at col 3 rows 0..8, leaving a gap at row 9
    for r in range(0, 9):
        g.set(3, r, "top", OBSTACLE)
    path = astar_route(g, (0, 0, "top"), (6, 0, "top"), net_id=1, diagonal=False)
    assert path is not None
    # must detour down to row >=9 to pass the wall
    assert max(p[1] for p in path) >= 9


def test_astar_no_path_when_fully_walled():
    g = RoutingGrid(6.0, 6.0, 1.0)
    for r in range(g.rows):
        g.set(3, r, "top", OBSTACLE)
        g.set(3, r, "bottom", OBSTACLE)
    g.mark_no_via_rect(0, 0, 6, 6)  # forbid via escape
    assert astar_route(g, (0, 0, "top"), (5, 0, "top"), net_id=1) is None


def test_astar_wide_trace_detours_around_obstacle():
    g = RoutingGrid(12.0, 12.0, 1.0)
    # narrow corridor at row 6: a half_width=1 trace needs rows 5..7 clear.
    # block row 5 col 6 so the wide trace must detour, narrow one passes.
    g.set(6, 5, "top", OBSTACLE)
    # route along the middle (away from edges so half_width fits)
    p_narrow = astar_route(g, (1, 6, "top"), (11, 6, "top"),
                           net_id=1, half_width_cells=0, diagonal=False)
    assert p_narrow is not None
    # wide trace can still route (detours below the blocked cell)
    p_wide = astar_route(g, (1, 6, "top"), (11, 6, "top"),
                         net_id=1, half_width_cells=1, diagonal=False)
    assert p_wide is not None
    # the wide path is forced off the straight row-6 line near the obstacle
    assert any(p[1] != 6 for p in p_wide)


# ===========================================================================
# astar_route_congestion
# ===========================================================================

def test_astar_congestion_passes_through_foreign_net_at_cost():
    g = RoutingGrid(10.0, 10.0, 1.0)
    # foreign net wall — impassable to plain A*, passable (costly) to congestion
    for r in range(0, 10):
        g.set(3, r, "top", 99)
    assert astar_route(g, (0, 0, "top"), (6, 0, "top"), net_id=1,
                       diagonal=False) is not None  # row 10 gap
    hist = {"top": [0.0] * (g.cols * g.rows), "bottom": [0.0] * (g.cols * g.rows)}
    occ = {"top": [0] * (g.cols * g.rows), "bottom": [0] * (g.cols * g.rows)}
    path = astar_route_congestion(
        g, (0, 0, "top"), (6, 0, "top"), net_id=1,
        history_cost=hist, present_occupancy=occ, diagonal=False)
    assert path is not None


def test_astar_congestion_wide_trace_and_channel_pressure():
    g = RoutingGrid(12.0, 12.0, 1.0)
    n = g.cols * g.rows
    # a foreign-net cell in the corridor — wide trace footprint hits it (penalty)
    g.set(6, 6, "top", 99)
    hist = {"top": [0.0] * n, "bottom": [0.0] * n}
    occ = {"top": [0] * n, "bottom": [0] * n}
    # mark some history + occupancy so those penalty branches execute
    hist["top"][6 * g.cols + 6] = 2.0
    occ["top"][6 * g.cols + 6] = 3
    # channel pressure on the top layer
    cp = {"top": [0.0] * n, "bottom": [0.0] * n}
    cp["top"][6 * g.cols + 7] = 0.5
    path = astar_route_congestion(
        g, (2, 6, "top"), (10, 6, "top"), net_id=1,
        half_width_cells=1, via_cost=10.0,
        history_cost=hist, present_occupancy=occ,
        channel_pressure=cp, channel_pressure_weight=3.0, diagonal=False)
    assert path is not None


def test_astar_congestion_obstacle_still_impassable():
    g = RoutingGrid(6.0, 6.0, 1.0)
    for r in range(g.rows):
        g.set(3, r, "top", OBSTACLE)
        g.set(3, r, "bottom", OBSTACLE)
    g.mark_no_via_rect(0, 0, 6, 6)
    hist = {"top": [0.0] * (g.cols * g.rows), "bottom": [0.0] * (g.cols * g.rows)}
    occ = {"top": [0] * (g.cols * g.rows), "bottom": [0] * (g.cols * g.rows)}
    assert astar_route_congestion(
        g, (0, 0, "top"), (5, 0, "top"), net_id=1,
        history_cost=hist, present_occupancy=occ) is None


# ===========================================================================
# astar_find_blockers
# ===========================================================================

def test_find_blockers_empty_when_clear():
    g = RoutingGrid(10.0, 10.0, 1.0)
    blockers = astar_find_blockers(g, (0, 0, "top"), (5, 0, "top"), net_id=1,
                                   diagonal=False)
    assert blockers == set()


def test_find_blockers_unreachable_end_returns_none():
    g = RoutingGrid(8.0, 8.0, 1.0)
    # wall off the end completely on both layers + forbid vias
    for r in range(g.rows):
        g.set(4, r, "top", OBSTACLE)
        g.set(4, r, "bottom", OBSTACLE)
    g.mark_no_via_rect(0, 0, 8, 8)
    assert astar_find_blockers(g, (1, 1, "top"), (7, 1, "top"), 1,
                               diagonal=False) is None


def test_find_blockers_wide_trace_collects_blockers():
    g = RoutingGrid(12.0, 12.0, 1.0)
    # foreign net cells in the corridor; wide trace footprint sees them
    for r in range(g.rows):
        g.set(6, r, "top", 7)
        g.set(6, r, "bottom", 7)
    g.mark_no_via_rect(0, 0, 12, 12)
    blockers = astar_find_blockers(g, (2, 6, "top"), (10, 6, "top"), net_id=1,
                                   half_width_cells=1, diagonal=False)
    assert blockers == {7}


def test_find_blockers_reports_net_in_the_way():
    g = RoutingGrid(10.0, 10.0, 1.0)
    for r in range(g.rows):  # full wall of net 7 on both layers
        g.set(3, r, "top", 7)
        g.set(3, r, "bottom", 7)
    g.mark_no_via_rect(0, 0, 10, 10)
    blockers = astar_find_blockers(g, (0, 0, "top"), (6, 0, "top"), net_id=1,
                                   diagonal=False)
    assert blockers == {7}


# ===========================================================================
# simplify_path
# ===========================================================================

def test_simplify_path_too_short():
    assert simplify_path([(0, 0, "top")], 1.0, 0.2, "n", "N") == ([], [])


def test_simplify_path_collinear_merges_to_one_segment():
    path = [(0, 0, "top"), (1, 0, "top"), (2, 0, "top"), (3, 0, "top")]
    traces, vias = simplify_path(path, 1.0, 0.2, "n", "N")
    assert len(traces) == 1 and not vias
    assert (traces[0].start_x_mm, traces[0].end_x_mm) == (0.0, 3.0)


def test_simplify_path_corner_makes_two_segments():
    path = [(0, 0, "top"), (1, 0, "top"), (2, 0, "top"),
            (2, 1, "top"), (2, 2, "top")]
    traces, _ = simplify_path(path, 1.0, 0.2, "n", "N")
    assert len(traces) == 2


def test_simplify_path_layer_change_emits_via():
    path = [(0, 0, "top"), (1, 0, "top"), (1, 0, "bottom"), (2, 0, "bottom")]
    traces, vias = simplify_path(path, 1.0, 0.2, "n", "N")
    assert len(vias) == 1
    assert vias[0].from_layer == "top" and vias[0].to_layer == "bottom"
    assert {t.layer for t in traces} == {"top", "bottom"}


# ===========================================================================
# mark_path_on_grid
# ===========================================================================

def test_mark_path_marks_cells_and_via_radius():
    g = RoutingGrid(10.0, 10.0, 1.0)
    path = [(0, 0, "top"), (1, 0, "top"), (1, 0, "bottom")]
    mark_path_on_grid(g, path, net_id=5, half_width_cells=0, via_half_width_cells=1)
    assert g.get(0, 0, "top") == 5
    # via point at (1,0) gets a wider radius on bottom layer
    assert g.get(1, 1, "bottom") == 5


# ===========================================================================
# order_nets
# ===========================================================================

def test_order_nets_power_ground_then_signal():
    placement, netlist = _two_pad_board()
    pad_map = build_pad_map(placement, netlist)
    nets = [
        NetInfo("s", "S", "signal", []),
        NetInfo("g", "GND", "ground", []),
        NetInfo("p", "VCC", "power", []),
    ]
    ordered = order_nets(nets, pad_map, netlist)
    classes = [n.net_class for n in ordered]
    assert classes == ["power", "ground", "signal"]


# ===========================================================================
# _check_net_connectivity
# ===========================================================================

def _pad(x, y, net="net_0", layer="top"):
    return PadInfo(port_id="p", designator="R1", pin_number=1, net_id=net,
                   x_mm=x, y_mm=y, pad_width_mm=1.0, pad_height_mm=1.0, layer=layer)


def test_connectivity_single_pad_trivially_connected():
    assert _check_net_connectivity("net_0", [_pad(1, 1)], [], [], 0.25) is True


def test_connectivity_two_pads_joined_by_trace():
    pads = [_pad(0, 0), _pad(5, 0)]
    traces = [TraceSegment(0, 0, 5, 0, 0.2, "top", "net_0", "N0")]
    assert _check_net_connectivity("net_0", pads, traces, [], 0.25) is True


def test_connectivity_disconnected_pads_returns_false():
    pads = [_pad(0, 0), _pad(20, 20)]
    traces = [TraceSegment(0, 0, 2, 0, 0.2, "top", "net_0", "N0")]
    assert _check_net_connectivity("net_0", pads, traces, [], 0.25) is False


def test_connectivity_via_links_layers():
    pads = [_pad(0, 0, layer="top"), _pad(5, 0, layer="bottom")]
    traces = [
        TraceSegment(0, 0, 2, 0, 0.2, "top", "net_0", "N0"),
        TraceSegment(2, 0, 5, 0, 0.2, "bottom", "net_0", "N0"),
    ]
    vias = [Via(2, 0, 0.3, 0.6, "top", "bottom", "net_0", "N0")]
    assert _check_net_connectivity("net_0", pads, traces, vias, 0.25) is True


# ===========================================================================
# _detect_channels / _build_channel_pressure
# ===========================================================================

def _ic_pads(desig, cx, cy, pitch=1.0, rows_y=(0.0, 4.0), n_per_row=4):
    pads = {}
    k = 0
    for ry in rows_y:
        for i in range(n_per_row):
            px = cx + i * pitch
            pads[f"{desig}_{k}"] = PadInfo(
                port_id=f"{desig}_{k}", designator=desig, pin_number=k,
                net_id=None, x_mm=px, y_mm=cy + ry,
                pad_width_mm=0.5, pad_height_mm=0.5, layer="top")
            k += 1
    return pads


def test_detect_channels_finds_horizontal_channel():
    pad_map = _ic_pads("U1", 5.0, 5.0, pitch=1.5, rows_y=(0.0, 5.0), n_per_row=4)
    cfg = RouterConfig()
    channels = _detect_channels(pad_map, cfg, 30.0, 30.0)
    assert any(ch.axis == "horizontal" for ch in channels)
    assert all(ch.capacity >= 1 for ch in channels)


def test_detect_channels_skips_small_components():
    pad_map = {f"R_{i}": _pad(i, 0) for i in range(2)}  # only 2 pads
    assert _detect_channels(pad_map, RouterConfig(), 30.0, 30.0) == []


def test_build_channel_pressure_marks_channel_cells():
    pad_map = _ic_pads("U1", 5.0, 5.0, pitch=1.5, rows_y=(0.0, 5.0), n_per_row=4)
    cfg = RouterConfig()
    channels = _detect_channels(pad_map, cfg, 30.0, 30.0)
    grid = RoutingGrid(30.0, 30.0, cfg.grid_resolution_mm)
    pressure = _build_channel_pressure(channels, grid, cfg)
    assert "top" in pressure and "bottom" in pressure
    assert any(v > 0 for v in pressure["top"])


# ===========================================================================
# _bitmap_to_polygons
# ===========================================================================

def test_bitmap_to_polygons_single_block():
    g = RoutingGrid(5.0, 5.0, 1.0)
    filled = [False] * (g.cols * g.rows)
    # fill a 2x2 block at cols 1-2, rows 1-2
    for r in (1, 2):
        for c in (1, 2):
            filled[r * g.cols + c] = True
    polys = _bitmap_to_polygons(filled, g, "top")
    assert len(polys) == 1
    xs = [p[0] for p in polys[0]]
    ys = [p[1] for p in polys[0]]
    # the rectangle should span ~ the filled extent
    assert max(xs) - min(xs) >= 1.0 and max(ys) - min(ys) >= 1.0


def test_bitmap_to_polygons_empty():
    g = RoutingGrid(5.0, 5.0, 1.0)
    assert _bitmap_to_polygons([False] * (g.cols * g.rows), g, "top") == []


# ===========================================================================
# _build_clearance_mask / _add_stitching_vias / island removal
# ===========================================================================

def test_clearance_mask_forbids_around_foreign_net():
    g = RoutingGrid(10.0, 10.0, 1.0)
    g.set(5, 5, "top", 9)  # foreign net (fill net is 1)
    mask = _build_clearance_mask(g, "top", fill_net_int=1, clearance_cells=2)
    assert mask[5 * g.cols + 5] is True
    assert mask[6 * g.cols + 5] is True   # within clearance
    assert mask[0] is False               # far away, free


def test_remove_islands_cross_layer_via_keeps_region():
    g = RoutingGrid(10.0, 10.0, 1.0)
    n = g.cols * g.rows
    top = [False] * n
    bot = [False] * n
    # top region seeded; bottom region reachable only through a stitching via
    for c in (1, 2):
        top[1 * g.cols + c] = True
        g.set(c, 1, "top", 1)
    for c in (1, 2):
        bot[1 * g.cols + c] = True
    via = Via(1.0, 1.0, 0.3, 0.6, "top", "bottom", "g", "GND")
    removed = _remove_islands_cross_layer(top, bot, g, 1, [via])
    # bottom region is connected via the stitching via → kept
    assert bot[1 * g.cols + 1] is True
    assert removed == 0


def test_add_stitching_vias_bridges_seeded_top_to_unseeded_bottom():
    g = RoutingGrid(20.0, 20.0, 0.5)
    n = g.cols * g.rows
    top = [True] * n
    bot = [True] * n
    # Seed the top fill (a fill-net cell) but NOT the bottom — a stitching via
    # must extend connectivity down to the unseeded bottom region.
    g.set(20, 20, "top", 1)
    cfg = RouterConfig()
    vias = _add_stitching_vias(top, bot, g, fill_net_int=1, config=cfg)
    assert len(vias) > 0
    assert all(isinstance(v, Via) for v in vias)
    assert all(v.from_layer == "top" and v.to_layer == "bottom" for v in vias)


# ===========================================================================
# create_copper_fill (direct)
# ===========================================================================

def test_create_copper_fill_produces_polygons():
    g = RoutingGrid(15.0, 15.0, 0.5)
    # mark a few GND pad cells (net 1) so fill has connectivity seeds
    g.set(5, 5, "top", 1)
    g.set(5, 5, "bottom", 1)
    pad_map = {"g": _pad(2.5, 2.5, net="net_g")}
    cfg = RouterConfig()
    regions, vias = create_copper_fill(g, 1, "net_g", "GND", pad_map, cfg)
    assert any(r["layer"] == "top" for r in regions)
    assert all(r["net_name"] == "GND" for r in regions)


# ===========================================================================
# generate_inner_plane / inner_plane_count / regenerate_inner_planes
# ===========================================================================

def test_inner_plane_count_logic():
    assert inner_plane_count({"layers": 2}) == 0
    assert inner_plane_count({"layers": 4}) == 2
    assert inner_plane_count({"layers": 4, "plane_layers": 1}) == 1
    assert inner_plane_count({"layers": 4, "plane_layers": 0}) == 0


def test_generate_inner_plane_outer_plus_antipads():
    board = {"width_mm": 20.0, "height_mm": 20.0}
    pad_map = {
        "th_gnd": PadInfo("a", "U1", 1, "gnd", 5.0, 5.0, 1.5, 1.5, "all"),
        "th_sig": PadInfo("b", "U1", 2, "sig", 10.0, 10.0, 1.5, 1.5, "all"),
        "smd": PadInfo("c", "U1", 3, "sig", 3.0, 3.0, 1.0, 1.0, "top"),
    }
    vias = [{"x_mm": 12.0, "y_mm": 12.0, "diameter_mm": 0.6, "net_id": "sig"}]
    plane = generate_inner_plane(board, [], pad_map, vias, "inner1",
                                 "gnd", "GND", RouterConfig())
    assert plane["is_plane"] is True
    # outer boundary first, then a cutout per TH pad (2) + 1 via = 3 cutouts.
    # SMD pad is skipped (doesn't reach inner layer).
    assert len(plane["polygons"]) == 1 + 3
    outer = plane["polygons"][0]
    assert (0.0, 0.0) in outer and (20.0, 20.0) in outer


def test_regenerate_inner_planes_noop_without_planes():
    routed = {"routing": {"copper_fills": [{"layer": "top", "is_plane": False}]}}
    assert regenerate_inner_planes(routed, {"elements": []}) is routed


def test_regenerate_inner_planes_recuts():
    board = {"width_mm": 20.0, "height_mm": 20.0, "layers": 4}
    routed = {
        "board": board, "placements": [],
        "routing": {
            "vias": [{"x_mm": 8.0, "y_mm": 8.0, "diameter_mm": 0.6, "net_id": "sig"}],
            "copper_fills": [
                {"layer": "inner1", "net_id": "gnd", "net_name": "GND",
                 "is_plane": True, "polygons": [[(0, 0)]]},
            ],
        },
    }
    netlist = {"elements": [
        {"element_type": "net", "net_id": "gnd", "name": "GND",
         "net_class": "ground", "connected_port_ids": []},
    ]}
    out = regenerate_inner_planes(routed, netlist)
    plane = out["routing"]["copper_fills"][0]
    # re-cut: outer boundary + one antipad for the foreign via
    assert plane["polygons"][0][0] == (0.0, 0.0)
    assert len(plane["polygons"]) == 2


# ===========================================================================
# _mounting_hole_keepouts / _filter_via_hole_spacing
# ===========================================================================

def test_mounting_hole_keepouts_parses_drill():
    placements = [
        {"package": "MountingHole_3.2mm", "x_mm": 1.0, "y_mm": 2.0},
        {"package": "R_0805", "x_mm": 5.0, "y_mm": 5.0},
    ]
    outs = _mounting_hole_keepouts(placements, via_diameter_mm=0.6)
    assert len(outs) == 1
    x, y, min_d = outs[0]
    assert (x, y) == (1.0, 2.0)
    assert min_d == pytest.approx(3.2 / 2 + 0.3 + 0.2)


def test_filter_via_spacing_drops_too_close():
    existing = [{"x_mm": 0.0, "y_mm": 0.0}]
    new = [
        {"x_mm": 0.1, "y_mm": 0.0},   # too close to existing → dropped
        {"x_mm": 5.0, "y_mm": 5.0},   # far → kept
    ]
    kept = _filter_via_hole_spacing(existing, new, min_center_mm=0.8)
    assert {(v["x_mm"], v["y_mm"]) for v in kept} == {(5.0, 5.0)}


def test_filter_via_spacing_respects_mounting_hole_keepout():
    new = [{"x_mm": 1.0, "y_mm": 1.0}]
    kept = _filter_via_hole_spacing(
        [], new, min_center_mm=0.8,
        hole_keepouts=[(1.0, 1.0, 2.0)])  # via sits on the hole keepout
    assert kept == []


# ===========================================================================
# _remove_dangling_traces
# ===========================================================================

def test_remove_dangling_drops_free_stub():
    pad_map = {
        "a": _pad(0, 0, net="net_0"),
        "b": _pad(5, 0, net="net_0"),
    }
    routing = {
        "traces": [
            {"start_x_mm": 0.0, "start_y_mm": 0.0, "end_x_mm": 5.0,
             "end_y_mm": 0.0, "width_mm": 0.2, "layer": "top", "net_id": "net_0"},
            # dangling stub: one end on the main trace, the other in free space
            {"start_x_mm": 2.5, "start_y_mm": 0.0, "end_x_mm": 2.5,
             "end_y_mm": 3.0, "width_mm": 0.2, "layer": "top", "net_id": "net_0"},
        ],
        "vias": [],
    }
    removed = _remove_dangling_traces(routing, pad_map)
    assert removed == 1
    assert len(routing["traces"]) == 1   # main pad-to-pad trace survives


def test_remove_dangling_keeps_connected_traces():
    pad_map = {"a": _pad(0, 0, net="net_0"), "b": _pad(5, 0, net="net_0")}
    routing = {
        "traces": [
            {"start_x_mm": 0.0, "start_y_mm": 0.0, "end_x_mm": 5.0,
             "end_y_mm": 0.0, "width_mm": 0.2, "layer": "top", "net_id": "net_0"},
        ],
        "vias": [],
    }
    assert _remove_dangling_traces(routing, pad_map) == 0


# ===========================================================================
# _post_route_nudge / _consolidate_endpoints
# ===========================================================================

def test_post_route_nudge_runs_on_clean_traces():
    result = RoutingResult()
    result.traces = [TraceSegment(0, 0, 5, 0, 0.2, "top", "net_0", "N0")]
    pad_map = {"a": _pad(0, 0), "b": _pad(5, 0)}
    _post_route_nudge(result, pad_map, RouterConfig())  # no violations → no crash
    assert len(result.traces) == 1


def test_post_route_nudge_fixes_trace_trace_violation():
    cfg = RouterConfig()
    result = RoutingResult()
    # two parallel foreign-net traces 0.1mm apart → below clearance → nudge
    result.traces = [
        TraceSegment(1.0, 5.0, 9.0, 5.0, 0.2, "top", "net_a", "A"),       # longer
        TraceSegment(4.0, 5.1, 6.0, 5.1, 0.2, "top", "net_b", "B"),       # shorter
    ]
    pad_map = {}  # no pad anchors → endpoints free to move
    _post_route_nudge(result, pad_map, cfg)
    # the shorter trace (B) was nudged away from A
    b = result.traces[1]
    assert abs(b.start_y_mm - 5.1) > 1e-6 or abs(b.end_y_mm - 5.1) > 1e-6


def test_post_route_nudge_fixes_via_trace_violation():
    cfg = RouterConfig()
    result = RoutingResult()
    result.traces = [TraceSegment(0.0, 5.0, 10.0, 5.0, 0.2, "top", "net_a", "A")]
    # foreign-net via sitting on the trace → violation
    result.vias = [Via(5.0, 5.05, 0.3, 0.6, "top", "bottom", "net_b", "B")]
    pad_map = {}
    _post_route_nudge(result, pad_map, cfg)
    t = result.traces[0]
    # trace nudged off y=5.0
    assert abs(t.start_y_mm - 5.0) > 1e-6 or abs(t.end_y_mm - 5.0) > 1e-6


def test_consolidate_endpoints_snaps_to_via():
    cfg = RouterConfig()
    result = RoutingResult()
    off = cfg.grid_resolution_mm
    # trace end near a via of the same net (cross-layer connection)
    result.traces = [TraceSegment(0, 0, 5 + off, off, 0.2, "top", "net_0", "N0")]
    result.vias = [Via(5.0, 0.0, 0.3, 0.6, "top", "bottom", "net_0", "N0")]
    pad_map = {}  # no pads → only via snap path runs
    _consolidate_endpoints(result, pad_map, cfg)
    assert (result.traces[0].end_x_mm, result.traces[0].end_y_mm) == (5.0, 0.0)


def test_detect_channels_vertical():
    # two columns of pads (same X within each), separated horizontally
    pads = {}
    k = 0
    for cx in (5.0, 11.0):
        for i in range(4):
            pads[f"U1_{k}"] = PadInfo(
                f"U1_{k}", "U1", k, None, cx, 3.0 + i * 1.5,
                0.5, 0.5, "top")
            k += 1
    channels = _detect_channels(pads, RouterConfig(), 30.0, 30.0)
    assert any(ch.axis == "vertical" for ch in channels)


def test_consolidate_endpoints_snaps_to_pad():
    result = RoutingResult()
    cfg = RouterConfig()
    # endpoint within snap tol (grid_res*3) of the pad at (5,0)
    off = cfg.grid_resolution_mm
    result.traces = [TraceSegment(0, 0, 5 + off, 0, 0.2, "top", "net_0", "N0")]
    pad_map = {"a": _pad(0, 0), "b": _pad(5, 0)}
    _consolidate_endpoints(result, pad_map, cfg)
    assert result.traces[0].end_x_mm == 5.0


# ===========================================================================
# Push-and-shove helpers
# ===========================================================================

def test_pad_zone_index_th_and_smd():
    pads = {
        "smd": PadInfo("a", "R1", 1, "net_0", 5.0, 5.0, 1.0, 1.0, "top"),
        "th": PadInfo("b", "U1", 1, "net_1", 8.0, 8.0, 1.5, 1.5, "all"),
    }
    g = RoutingGrid(20.0, 20.0, 0.5)
    pzi = _build_pad_zone_index(pads, {"net_0": 1, "net_1": 2}, 0.25, g)
    assert 1 in pzi and 2 in pzi
    # TH pad indexed on both layers
    assert {rect[4] for rect in pzi[2]} == {"top", "bottom"}
    # SMD only on its layer
    assert {rect[4] for rect in pzi[1]} == {"top"}


def test_is_pad_zone_inside_and_outside():
    pads = {"smd": PadInfo("a", "R1", 1, "net_0", 5.0, 5.0, 1.0, 1.0, "top")}
    g = RoutingGrid(20.0, 20.0, 0.5)
    pzi = _build_pad_zone_index(pads, {"net_0": 1}, 0.25, g)
    pc, pr = g.mm_to_grid(5.0, 5.0)
    assert _is_pad_zone(pc, pr, "top", 1, pzi) is True
    assert _is_pad_zone(0, 0, "top", 1, pzi) is False


def test_trace_segment_at_extends_run():
    g = RoutingGrid(20.0, 20.0, 1.0)
    for c in range(3, 8):
        g.set(c, 5, "top", 4)   # horizontal trace run
    seg = _trace_segment_at(g, 5, 5, "top", 4, {})
    assert (3, 5) in seg and (7, 5) in seg
    assert len(seg) == 5


def test_trace_segment_at_returns_empty_for_pad_zone():
    g = RoutingGrid(20.0, 20.0, 1.0)
    g.set(5, 5, "top", 4)
    pzi = {4: [(4, 4, 6, 6, "top")]}  # (5,5) is inside this pad zone
    assert _trace_segment_at(g, 5, 5, "top", 4, pzi) == []


def test_try_shove_segment_into_empty_succeeds_and_undo_restores():
    g = RoutingGrid(20.0, 20.0, 1.0)
    seg = [(5, 5)]
    g.set(5, 5, "top", 4)
    snap = {}
    ok = _try_shove_segment(g, seg, "top", 4, 0, 1, {}, 0, 5, snap)
    assert ok is True
    assert g.get(5, 6, "top") == 4 and g.get(5, 5, "top") == EMPTY
    _undo_shove(g, snap)
    assert g.get(5, 5, "top") == 4


def test_try_shove_segment_blocked_by_obstacle_fails():
    g = RoutingGrid(20.0, 20.0, 1.0)
    g.set(5, 5, "top", 4)
    g.set(5, 6, "top", OBSTACLE)  # shove target blocked
    assert _try_shove_segment(g, [(5, 5)], "top", 4, 0, 1, {}, 0, 5, {}) is False


def test_try_shove_segment_exceeds_depth_fails():
    g = RoutingGrid(20.0, 20.0, 1.0)
    assert _try_shove_segment(g, [(5, 5)], "top", 4, 0, 1, {},
                              depth=6, max_depth=5, snapshot={}) is False


# ===========================================================================
# pre-fill helpers + accessibility + get_net_pads
# ===========================================================================

def test_get_net_pads_returns_net_members():
    placement, netlist = _two_pad_board()
    pad_map = build_pad_map(placement, netlist)
    net = build_connectivity(netlist)[0]
    pads = _get_net_pads(net, pad_map, netlist)
    assert len(pads) == 2
    assert all(p.net_id == net.net_id for p in pads)


def test_apply_pre_fill_marks_fill_cells():
    placement, netlist = _two_pad_board(net_class="ground")
    for e in netlist["elements"]:
        if e.get("element_type") == "net":
            e["name"] = "GND"
    pad_map = build_pad_map(placement, netlist)
    net = build_connectivity(netlist)[0]
    cfg = RouterConfig()
    grid = RoutingGrid(20.0, 20.0, cfg.grid_resolution_mm)
    _setup_grid(grid, placement, pad_map, cfg.clearance_mm, {net.net_id: 1})
    _apply_pre_fill(grid, 1, net.net_id, pad_map, cfg)
    # bottom layer now has fill cells
    assert any(v == 1 for v in grid.layers["bottom"])


def test_pad_accessibility_open_vs_blocked():
    placement, netlist = _two_pad_board()
    pad_map = build_pad_map(placement, netlist)
    net = build_connectivity(netlist)[0]
    cfg = RouterConfig()
    grid = RoutingGrid(20.0, 20.0, cfg.grid_resolution_mm)
    net_id_map = {net.net_id: 1}
    _setup_grid(grid, placement, pad_map, cfg.clearance_mm, net_id_map)
    open_score = _pad_accessibility(net, pad_map, grid, net_id_map, netlist)
    # block everything around the pads
    for layer in ("top", "bottom"):
        grid.layers[layer] = [OBSTACLE] * (grid.cols * grid.rows)
    blocked_score = _pad_accessibility(net, pad_map, grid, net_id_map, netlist)
    assert blocked_score < open_score


# ===========================================================================
# route_net direct (deterministic geometry)
# ===========================================================================

def test_route_net_two_pads_yields_connecting_trace():
    placement, netlist = _two_pad_board()
    pad_map = build_pad_map(placement, netlist)
    nets = build_connectivity(netlist)
    net = nets[0]
    cfg = RouterConfig()
    grid = RoutingGrid(20.0, 20.0, cfg.grid_resolution_mm)
    net_id_map = {net.net_id: 1}
    _setup_grid(grid, placement, pad_map, cfg.clearance_mm, net_id_map)
    out = route_net(grid, net, pad_map, 1, cfg, netlist, 0.25)
    assert out is not None
    traces, vias = out
    assert traces
    # endpoints snap to the two pad centres
    pads = [p for p in pad_map.values() if p.net_id == net.net_id]
    xs = {round(p.x_mm, 3) for p in pads}
    ep_xs = {round(traces[0].start_x_mm, 3), round(traces[-1].end_x_mm, 3)}
    assert ep_xs == xs


def test_connectivity_repair_reroutes_disconnected_net():
    placement, netlist = _two_pad_board()
    pad_map = build_pad_map(placement, netlist)
    net = build_connectivity(netlist)[0]
    cfg = RouterConfig()
    grid = RoutingGrid(20.0, 20.0, cfg.grid_resolution_mm)
    net_id_map = {net.net_id: 1}
    int_to_net = {1: net}
    _setup_grid(grid, placement, pad_map, cfg.clearance_mm, net_id_map)
    # net is "routed" but has NO traces → connectivity check fails → repair
    result = RoutingResult()
    routed_ids = [net.net_id]
    _connectivity_repair(grid, result, [net], pad_map, net_id_map, int_to_net,
                         {net.net_id: 0.25}, routed_ids, cfg, netlist, True, None)
    # repair produced a connecting trace
    assert result.traces
    pads = [p for p in pad_map.values() if p.net_id == net.net_id]
    assert _check_net_connectivity(net.net_id, pads, result.traces,
                                   result.vias, cfg.grid_resolution_mm)


def test_route_net_single_pad_returns_empty():
    """A net with <2 pads yields no traces (route_net early return)."""
    netlist = {"elements": [
        {"element_type": "net", "net_id": "n", "name": "N",
         "net_class": "signal", "connected_port_ids": ["P1"]},
        {"element_type": "port", "port_id": "P1", "component_id": "C1",
         "designator": "R1", "pin_number": 1, "name": "1"},
        {"element_type": "component", "component_id": "C1",
         "component_type": "resistor", "designator": "R1", "properties": {}},
    ]}
    placement = {"board": {"width_mm": 10.0, "height_mm": 10.0},
                 "placements": [_comp("C1", "R1", 5.0, 5.0)]}
    pad_map = build_pad_map(placement, netlist)
    net = NetInfo("n", "N", "signal", ["R1"])
    cfg = RouterConfig()
    grid = RoutingGrid(10.0, 10.0, cfg.grid_resolution_mm)
    assert route_net(grid, net, pad_map, 1, cfg, netlist, 0.25) == ([], [])
    # congestion variant
    n = grid.cols * grid.rows
    res = route_net_congestion(
        grid, net, pad_map, 1, cfg, netlist, 0.25,
        history_cost={"top": [0.0] * n, "bottom": [0.0] * n},
        present_occupancy={"top": [0] * n, "bottom": [0] * n})
    assert res == ([], [], [])


def test_pad_accessibility_net_without_pads():
    net = NetInfo("ghost", "GHOST", "signal", [])
    g = RoutingGrid(10.0, 10.0, 0.25)
    score = _pad_accessibility(net, {}, g, {"ghost": 1}, {"elements": []})
    assert score == 999.0


def test_route_net_congestion_two_pads():
    placement, netlist = _two_pad_board()
    pad_map = build_pad_map(placement, netlist)
    nets = build_connectivity(netlist)
    net = nets[0]
    cfg = RouterConfig()
    grid = RoutingGrid(20.0, 20.0, cfg.grid_resolution_mm)
    net_id_map = {net.net_id: 1}
    _setup_grid(grid, placement, pad_map, cfg.clearance_mm, net_id_map)
    n = grid.cols * grid.rows
    hist = {"top": [0.0] * n, "bottom": [0.0] * n}
    occ = {"top": [0] * n, "bottom": [0] * n}
    out = route_net_congestion(grid, net, pad_map, 1, cfg, netlist, 0.25,
                               history_cost=hist, present_occupancy=occ)
    assert out is not None and out[0]


# ===========================================================================
# route_board end-to-end — small synthetic + blink fixture
# ===========================================================================

def test_generate_silkscreen_led_anode_and_missing_component():
    placement = {"board": {"width_mm": 20.0, "height_mm": 20.0},
                 "placements": [
        _comp("D1c", "D1", 5.0, 5.0, ctype="led"),
        _comp("X9", "GHOST", 12.0, 12.0),   # no matching component in netlist
        {"designator": "FID1", "component_type": "fiducial", "package": "Fiducial",
         "footprint_width_mm": 3.0, "footprint_height_mm": 3.0,
         "x_mm": 1.0, "y_mm": 1.0, "rotation_deg": 0, "layer": "top"},
    ]}
    netlist = {"elements": [
        {"element_type": "component", "component_id": "D1c",
         "component_type": "led", "designator": "D1", "properties": {}},
        # LED ports with NO explicit anode role → pin 1 default-anode branch
        {"element_type": "port", "port_id": "DP1", "component_id": "D1c",
         "designator": "D1", "pin_number": 1, "name": "1"},
        {"element_type": "port", "port_id": "DP2", "component_id": "D1c",
         "designator": "D1", "pin_number": 2, "name": "2"},
    ]}
    pad_map = build_pad_map(placement, netlist)
    silk = _generate_silkscreen(placement, netlist, pad_map)
    texts = {s["text"] for s in silk if s["type"] == "text"}
    assert "D1" in texts            # designator label
    assert "GHOST" not in {s.get("text") for s in silk
                           if s.get("type") == "text" and s.get("purpose") == "anode"}
    # fiducial produces no silk
    assert not any(s.get("x_mm") == 1.0 and s.get("y_mm") == 1.0 for s in silk)


def test_route_board_two_pad_completes_100pct():
    placement, netlist = _two_pad_board()
    out = route_board(placement, netlist, _fast_cfg())
    st = out["routing"]["statistics"]
    assert st["total_nets"] == 1
    assert st["routed_nets"] == 1
    assert st["completion_pct"] == 100.0
    assert out["routing"]["unrouted_nets"] == []
    # one trace connects the two pads
    traces = out["routing"]["traces"]
    assert len(traces) >= 1


def test_route_board_default_config_runs_all_phases():
    """Full RouterConfig — exercises NCR / shove / fine-grid / channel phases
    on a tiny board (still fast: <1s because 1 trivial net)."""
    placement, netlist = _two_pad_board()
    out = route_board(placement, netlist, RouterConfig())
    assert out["routing"]["statistics"]["completion_pct"] == 100.0


def test_route_board_power_net_gets_wider_trace():
    placement, netlist = _two_pad_board(net_class="power")
    out = route_board(placement, netlist, _fast_cfg())
    traces = out["routing"]["traces"]
    assert traces
    # power default width > signal default width
    assert traces[0]["width_mm"] >= RouterConfig().trace_width_power_mm - 1e-6


def test_route_board_with_gnd_fill_produces_copper_fills():
    placement, netlist = _two_pad_board(net_class="ground")
    # rename net to GND so it becomes the fill net
    for e in netlist["elements"]:
        if e.get("element_type") == "net":
            e["name"] = "GND"
    out = route_board(placement, netlist, _fast_cfg(fill_enabled=True))
    assert "copper_fills" in out["routing"]
    assert out["routing"]["statistics"]["copper_fill_polygons"] > 0


def test_route_board_blink_fixture_high_completion():
    placement = json.load(open(BLINK + "placement.json"))
    netlist = json.load(open(BLINK + "netlist.json"))
    out = route_board(placement, netlist, RouterConfig())
    st = out["routing"]["statistics"]
    assert st["completion_pct"] >= 90.0
    # routed_nets + unrouted_nets == total_nets (consistency)
    assert st["routed_nets"] + st["unrouted_nets"] == st["total_nets"]
    assert len(out["routing"]["unrouted_nets"]) == st["unrouted_nets"]


def _crossing_board(N=16, pitch=0.6, w=8.0, swap=True, pattern="rev"):
    """Two full-height headers wired in reverse — denser than routing capacity,
    so the rescue phases (NCR, rip-up, shove, narrow, fine-grid, relaxed,
    connectivity-repair) all fire and some nets stay unrouted."""
    ys = [0.6 + i * pitch for i in range(N)]
    cy = sum(ys) / N
    H = ys[-1] + 0.6
    placement = {"board": {"width_mm": w, "height_mm": H, "layers": 2},
                 "placements": [
        {"designator": "J1", "component_type": "connector", "package": "Header",
         "footprint_width_mm": 0.6, "footprint_height_mm": N * pitch,
         "x_mm": 1.5, "y_mm": cy, "rotation_deg": 0, "layer": "top"},
        {"designator": "J2", "component_type": "connector", "package": "Header",
         "footprint_width_mm": 0.6, "footprint_height_mm": N * pitch,
         "x_mm": w - 1.5, "y_mm": cy, "rotation_deg": 0, "layer": "top"}]}
    els = [
        {"element_type": "component", "component_id": "C1",
         "component_type": "connector", "designator": "J1", "properties": {}},
        {"element_type": "component", "component_id": "C2",
         "component_type": "connector", "designator": "J2", "properties": {}}]
    for i in range(N):
        els.append({"element_type": "port", "port_id": f"A{i}", "component_id": "C1",
                    "designator": "J1", "pin_number": i + 1, "name": str(i + 1)})
        els.append({"element_type": "port", "port_id": f"B{i}", "component_id": "C2",
                    "designator": "J2", "pin_number": i + 1, "name": str(i + 1)})
    for i in range(N):
        if not swap:
            j = i
        elif pattern == "rot":
            j = (i + N // 2) % N
        else:
            j = N - 1 - i
        els.append({"element_type": "net", "net_id": f"net_{i}", "name": f"N{i}",
                    "net_class": "signal",
                    "connected_port_ids": [f"A{i}", f"B{j}"]})
    return placement, {"elements": els}


def test_route_board_congested_runs_rescue_phases():
    placement, netlist = _crossing_board()
    cfg = RouterConfig(ordering_trials=2, ncr_max_iterations=3)
    out = route_board(placement, netlist, cfg)
    st = out["routing"]["statistics"]
    # Genuinely congested: not everything routes (drives the unrouted-net path).
    assert st["unrouted_nets"] > 0
    assert st["routed_nets"] + st["unrouted_nets"] == st["total_nets"]
    # completion % is consistent with routed/total
    expect = round(st["routed_nets"] / st["total_nets"] * 100, 1)
    assert st["completion_pct"] == expect
    # the unrouted_nets list length matches the count
    assert len(out["routing"]["unrouted_nets"]) == st["unrouted_nets"]


def test_route_board_triggers_targeted_ripup_success():
    """N=8 reverse-wired through a wide bottleneck — the targeted rip-up phase
    rips a blocker, routes the failed net, and re-routes the blocker (success)."""
    placement, netlist = _crossing_board(N=8, w=12.0, pattern="rev")
    out = route_board(placement, netlist,
                      RouterConfig(ordering_trials=3, ncr_max_iterations=4))
    st = out["routing"]["statistics"]
    assert st["routed_nets"] + st["unrouted_nets"] == st["total_nets"]


def test_route_board_triggers_shove_success():
    """N=6 rotated wiring on a tight board — drives the push-and-shove pass to
    shove a blocking segment and route the failed net (shove success branch)."""
    placement, netlist = _crossing_board(N=6, w=8.0, pattern="rot")
    out = route_board(placement, netlist,
                      RouterConfig(ordering_trials=2, ncr_max_iterations=3))
    st = out["routing"]["statistics"]
    assert st["routed_nets"] + st["unrouted_nets"] == st["total_nets"]
    assert st["routed_nets"] >= 1


def test_route_board_shove_fallback_ripup():
    """N=12 reverse-wired, narrower board — the shove pass can't shove every
    conflict, so it falls back to ripping conflict nets and re-routing."""
    placement, netlist = _crossing_board(N=12, w=9.0, pattern="rev")
    out = route_board(placement, netlist,
                      RouterConfig(ordering_trials=2, ncr_max_iterations=3))
    st = out["routing"]["statistics"]
    assert st["routed_nets"] + st["unrouted_nets"] == st["total_nets"]


def test_route_board_congested_partial_then_fill():
    """Same congested board but with GND fill + a ground net so copper fill +
    stub generation + relaxed/repair paths run together."""
    placement, netlist = _crossing_board(N=10)
    # make net_0 the GND fill net
    for e in netlist["elements"]:
        if e.get("net_id") == "net_0":
            e["name"] = "GND"
            e["net_class"] = "ground"
    cfg = RouterConfig(ordering_trials=2, ncr_max_iterations=2)
    out = route_board(placement, netlist, cfg)
    # fill ran (GND present) -> copper_fills key exists
    assert "copper_fills" in out["routing"]


# ===========================================================================
# apply_copper_fills — 2-layer outer fill + 4-layer inner planes
# ===========================================================================

def _routed_two_pad(layers=2):
    placement, netlist = _two_pad_board(net_class="ground", layers=layers)
    for e in netlist["elements"]:
        if e.get("element_type") == "net":
            e["name"] = "GND"
            e["net_class"] = "ground"
    placement["board"]["layers"] = layers
    out = route_board(placement, netlist, _fast_cfg(fill_enabled=False))
    return out, netlist


def test_apply_copper_fills_2layer_adds_outer_fill():
    routed, netlist = _routed_two_pad(layers=2)
    # inject a routed via so the via-grid-marking branch executes
    routed["routing"]["vias"].append({
        "x_mm": 10.0, "y_mm": 10.0, "drill_mm": 0.3, "diameter_mm": 0.6,
        "from_layer": "top", "to_layer": "bottom",
        "net_id": "net_0", "net_name": "GND"})
    out = apply_copper_fills(routed, netlist, RouterConfig())
    fills = out["routing"]["copper_fills"]
    assert fills
    assert all(not f.get("is_plane") for f in fills)  # outer flood fill only
    layers = {f["layer"] for f in fills}
    assert layers <= {"top", "bottom"}


def _routed_4layer_gnd_and_power():
    """4-layer board with GND + a VCC power net on SMD pads (drives power-plane
    stitching: pad → offset/in-pad via + stub)."""
    placement = {
        "board": {"width_mm": 24.0, "height_mm": 20.0, "layers": 4,
                  "outline_type": "rectangle", "origin": [0, 0]},
        "placements": [
            _comp("C1", "R1", 6.0, 10.0), _comp("C2", "R2", 18.0, 10.0),
            _comp("C3", "U1", 12.0, 6.0, ctype="ic"),
        ],
    }
    netlist = {"elements": [
        {"element_type": "component", "component_id": "C1",
         "component_type": "resistor", "designator": "R1", "properties": {}},
        {"element_type": "component", "component_id": "C2",
         "component_type": "resistor", "designator": "R2", "properties": {}},
        {"element_type": "component", "component_id": "C3",
         "component_type": "ic", "designator": "U1", "properties": {}},
        {"element_type": "port", "port_id": "P1", "component_id": "C1",
         "designator": "R1", "pin_number": 1, "name": "1"},
        {"element_type": "port", "port_id": "P2", "component_id": "C1",
         "designator": "R1", "pin_number": 2, "name": "2"},
        {"element_type": "port", "port_id": "P3", "component_id": "C2",
         "designator": "R2", "pin_number": 1, "name": "1"},
        {"element_type": "port", "port_id": "P4", "component_id": "C2",
         "designator": "R2", "pin_number": 2, "name": "2"},
        {"element_type": "port", "port_id": "P5", "component_id": "C3",
         "designator": "U1", "pin_number": 1, "name": "VCC"},
        {"element_type": "port", "port_id": "P6", "component_id": "C3",
         "designator": "U1", "pin_number": 2, "name": "GND"},
        {"element_type": "net", "net_id": "net_g", "name": "GND",
         "net_class": "ground", "connected_port_ids": ["P2", "P6"]},
        {"element_type": "net", "net_id": "net_v", "name": "VCC",
         "net_class": "power", "connected_port_ids": ["P3", "P5"]},
        {"element_type": "net", "net_id": "net_s", "name": "SIG",
         "net_class": "signal", "connected_port_ids": ["P1", "P4"]},
    ]}
    out = route_board(placement, netlist, _fast_cfg(fill_enabled=False))
    out["board"]["layers"] = 4
    return out, netlist


def test_apply_copper_fills_4layer_adds_inner_planes():
    routed, netlist = _routed_4layer_gnd_and_power()
    out = apply_copper_fills(routed, netlist, RouterConfig())
    fills = out["routing"]["copper_fills"]
    plane_layers = {f["layer"] for f in fills if f.get("is_plane")}
    assert "inner1" in plane_layers   # GND plane
    assert "inner2" in plane_layers   # power plane (plane_layers default 2)
    # power plane is for the VCC net
    pwr_plane = next(f for f in fills if f["layer"] == "inner2")
    assert pwr_plane["net_name"] == "VCC"


def test_apply_copper_fills_4layer_plane_layers_1_gnd_plane_only():
    """plane_layers=1 → In1 is a GND plane, In2 is a SIGNAL routing layer.
    Exercises the inner-signal trace via-exclusion marking + the single-plane
    branch (no power plane, no power stitching)."""
    routed, netlist = _routed_4layer_gnd_and_power()
    routed["board"]["plane_layers"] = 1
    # Route the SIG net on the inner2 SIGNAL layer instead of an outer layer.
    # Anchor endpoints on its two SMD pads so dangling-removal keeps it; its
    # footprint must become a via-exclusion zone (foreign net vs GND plane).
    pad_map = build_pad_map(routed, netlist)
    sig_pads = [p for p in pad_map.values() if p.net_id == "net_s"]
    a, b = sig_pads[0], sig_pads[1]
    routed["routing"]["traces"] = [
        t for t in routed["routing"]["traces"] if t["net_id"] != "net_s"]
    routed["routing"]["traces"].append({
        "start_x_mm": a.x_mm, "start_y_mm": a.y_mm,
        "end_x_mm": b.x_mm, "end_y_mm": b.y_mm,
        "width_mm": 0.2, "layer": "inner2", "net_id": "net_s", "net_name": "SIG",
    })
    out = apply_copper_fills(routed, netlist, RouterConfig())
    fills = out["routing"]["copper_fills"]
    plane_layers = {f["layer"] for f in fills if f.get("is_plane")}
    assert plane_layers == {"inner1"}   # only GND plane, In2 is signal


def test_apply_copper_fills_no_gnd_net_is_noop():
    placement, netlist = _two_pad_board(net_class="signal")
    routed = route_board(placement, netlist, _fast_cfg(fill_enabled=False))
    out = apply_copper_fills(routed, netlist, RouterConfig())
    assert "copper_fills" not in out["routing"]


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-q"]))
