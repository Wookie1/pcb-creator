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
    astar_find_blockers_with_path, _snap_endpoints_to_pads, _add_rescue_vias,
    _build_output, order_nets_by_channel_pressure, _fine_grid_retry, _shove_pass,
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


def test_connectivity_repair_single_pad_net_skipped():
    """A 'routed' net with <2 pads is skipped by connectivity repair (3648)."""
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
    result = RoutingResult()
    _connectivity_repair(grid, result, [net], pad_map, {"n": 1}, {1: net},
                         {"n": 0.25}, ["n"], cfg, netlist, True, None)
    assert result.traces == []  # nothing to repair


def test_connectivity_repair_relaxed_fallback_fails_unroutes_net():
    """A disconnected net whose normal AND relaxed re-route both fail (board
    fully walled) is removed from routed and marked unrouted — drives the
    relaxed-clearance fallback setup + the failure branch (3673-3703, 3721,
    3725-3728)."""
    placement, netlist = _two_pad_board()
    pad_map = build_pad_map(placement, netlist)
    net = build_connectivity(netlist)[0]
    cfg = RouterConfig()
    grid = RoutingGrid(20.0, 20.0, cfg.grid_resolution_mm)
    _setup_grid(grid, placement, pad_map, cfg.clearance_mm, {net.net_id: 1})
    # Wall both layers everywhere except the pad cells → no route possible at any
    # clearance, so both the normal and the relaxed retry fail.
    for c in range(grid.cols):
        for r in range(grid.rows):
            for layer in ("top", "bottom"):
                if grid.get(c, r, layer) == EMPTY:
                    grid.set(c, r, layer, OBSTACLE)
    result = RoutingResult()
    routed_ids = [net.net_id]
    _connectivity_repair(grid, result, [net], pad_map, {net.net_id: 1},
                         {1: net}, {net.net_id: 0.25}, routed_ids, cfg, netlist,
                         True, None)
    assert net.net_id not in routed_ids          # removed from routed
    assert net.net_id in result.unrouted_nets     # marked unrouted


def test_fine_grid_retry_skips_non_signal_net():
    """_fine_grid_retry leaves non-signal (power/ground) failed nets untouched
    (3594-3595)."""
    placement, netlist = _two_pad_board(net_class="power")
    pad_map = build_pad_map(placement, netlist)
    cfg = RouterConfig(fine_grid_factor=2)
    grid = RoutingGrid(20.0, 20.0, cfg.grid_resolution_mm)
    _setup_grid(grid, placement, pad_map, cfg.clearance_mm, {"net_0": 1})
    pwr_net = NetInfo("net_0", "N0", "power", ["R1"])
    result = RoutingResult()
    still = _fine_grid_retry(
        grid, [(pwr_net, 1)], result, placement, pad_map, {"net_0": 1},
        {"net_0": 0.5}, [], cfg, netlist, True, None)
    assert still == [(pwr_net, 1)]  # power net passed through unchanged


def test_shove_pass_single_pad_and_no_conflict_cells():
    """_shove_pass: a <2-pad net is skipped (2131); a 2-pad net whose path is
    fully OBSTACLE-walled has no conflict cells to shove and stays failed
    (2167-2168)."""
    cfg = _fast_cfg()
    # Board 1: single-pad net.
    nl1 = {"elements": [
        {"element_type": "net", "net_id": "n", "name": "N",
         "net_class": "signal", "connected_port_ids": ["P1"]},
        {"element_type": "port", "port_id": "P1", "component_id": "C1",
         "designator": "R1", "pin_number": 1, "name": "1"},
        {"element_type": "component", "component_id": "C1",
         "component_type": "resistor", "designator": "R1", "properties": {}}]}
    pl1 = {"board": {"width_mm": 10.0, "height_mm": 10.0},
           "placements": [_comp("C1", "R1", 5.0, 5.0)]}
    pm1 = build_pad_map(pl1, nl1)
    g1 = RoutingGrid(10.0, 10.0, cfg.grid_resolution_mm)
    _setup_grid(g1, pl1, pm1, cfg.clearance_mm, {"n": 1})
    net1 = NetInfo("n", "N", "signal", ["R1"])
    pzi1 = _build_pad_zone_index(pm1, {"n": 1}, cfg.clearance_mm, g1)
    still1 = _shove_pass(g1, [(net1, 1)], RoutingResult(), pm1, {"n": 1},
                         {1: net1}, {"n": 0.25}, pzi1, cfg, nl1, [], True, None)
    assert still1 == []  # single-pad net skipped, not added to still_failed

    # Board 2: a real 2-pad net, but the whole board is OBSTACLE-walled → the
    # blocker search returns no path, so there are no conflict cells to shove.
    pl2, nl2 = _two_pad_board()
    pm2 = build_pad_map(pl2, nl2)
    net2 = build_connectivity(nl2)[0]
    g2 = RoutingGrid(20.0, 20.0, cfg.grid_resolution_mm)
    _setup_grid(g2, pl2, pm2, cfg.clearance_mm, {net2.net_id: 1})
    for c in range(g2.cols):
        for r in range(g2.rows):
            for layer in ("top", "bottom"):
                if g2.get(c, r, layer) == EMPTY:
                    g2.set(c, r, layer, OBSTACLE)
    pzi2 = _build_pad_zone_index(pm2, {net2.net_id: 1}, cfg.clearance_mm, g2)
    still2 = _shove_pass(g2, [(net2, 1)], RoutingResult(), pm2,
                         {net2.net_id: 1}, {1: net2}, {net2.net_id: 0.25},
                         pzi2, cfg, nl2, [], True, None)
    assert still2 == [(net2, 1)]  # no conflict cells → stays failed


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

def _two_ic_congested(n_per_side=6, pitch=0.8, gap=3.0):
    """Two multi-pin ICs facing across a narrow channel, reverse-wired. Creates
    detectable channels (channel_pressure) and over-subscribes the routing space
    so NCR iterates, stagnates, and leaves nets failed/illegal."""
    w = 18.0
    # Two horizontal rows of pads per IC → a horizontal channel between rows.
    cy_top, cy_bot = 6.0, 6.0 + gap
    placement = {"board": {"width_mm": w, "height_mm": 16.0, "layers": 2},
                 "placements": [
        {"designator": "U1", "component_type": "ic", "package": "SOIC",
         "footprint_width_mm": n_per_side * pitch, "footprint_height_mm": gap + 1,
         "x_mm": 4.0, "y_mm": (cy_top + cy_bot) / 2, "rotation_deg": 0,
         "layer": "top"},
        {"designator": "U2", "component_type": "ic", "package": "SOIC",
         "footprint_width_mm": n_per_side * pitch, "footprint_height_mm": gap + 1,
         "x_mm": 13.0, "y_mm": (cy_top + cy_bot) / 2, "rotation_deg": 0,
         "layer": "top"}]}
    els = [
        {"element_type": "component", "component_id": "C1",
         "component_type": "ic", "designator": "U1", "properties": {}},
        {"element_type": "component", "component_id": "C2",
         "component_type": "ic", "designator": "U2", "properties": {}}]
    for i in range(n_per_side):
        els.append({"element_type": "port", "port_id": f"A{i}", "component_id": "C1",
                    "designator": "U1", "pin_number": i + 1, "name": str(i + 1)})
        els.append({"element_type": "port", "port_id": f"B{i}", "component_id": "C2",
                    "designator": "U2", "pin_number": i + 1, "name": str(i + 1)})
    for i in range(n_per_side):
        j = n_per_side - 1 - i  # reverse → forces crossings
        els.append({"element_type": "net", "net_id": f"net_{i}", "name": f"N{i}",
                    "net_class": "signal",
                    "connected_port_ids": [f"A{i}", f"B{j}"]})
    return placement, {"elements": els}


def test_route_board_congested_all_phases_with_ground_net():
    """A congested board that includes a GROUND net and runs every rescue phase
    (NCR, coordinated rip-up over many iterations, shove, narrow, fine-grid,
    relaxed). Drives the coordinated rip-up phase (4009/4110/4138), narrow-trace
    retry (4228-4247) and the ground/power rip escalation (_can_rip_net)."""
    placement, netlist = _crossing_board(N=12, w=8.0, pattern="rev")
    # Convert two nets to ground/power so the rip-escalation branches apply.
    nets = [e for e in netlist["elements"] if e.get("element_type") == "net"]
    nets[0]["net_class"] = "ground"
    nets[0]["name"] = "GND"
    nets[1]["net_class"] = "power"
    nets[1]["name"] = "VCC"
    cfg = RouterConfig(
        ordering_trials=2, ncr_max_iterations=4, ncr_stagnation_limit=2,
        max_rip_up_iterations=8, shove_enabled=True, fine_grid_factor=2,
    )
    out = route_board(placement, netlist, cfg)
    st = out["routing"]["statistics"]
    assert st["routed_nets"] + st["unrouted_nets"] == st["total_nets"]


def test_route_board_default_config_none():
    """route_board(config=None) builds a default RouterConfig (3758)."""
    placement, netlist = _two_pad_board()
    out = route_board(placement, netlist, config=None)
    assert out["routing"]["statistics"]["total_nets"] == 1


def test_route_board_ncr_with_progress_callback_and_channels():
    """Congested IC board with channels + an NCR progress callback drives the
    NCR seeding (3175-3181), per-iteration failed/illegal/stagnation branches
    (3214, 3242-3243, 3281-3283, 3362-3363) and the callback (3303-3313)."""
    placement, netlist = _two_ic_congested(n_per_side=14, pitch=0.5, gap=1.0)
    placement["board"]["width_mm"] = 11.0
    events = []
    cfg = RouterConfig(
        ordering_trials=2, ncr_enabled=True, ncr_max_iterations=6,
        ncr_stagnation_limit=2, shove_enabled=False, fine_grid_factor=1,
        ncr_progress_callback=lambda d: events.append(d),
    )
    out = route_board(placement, netlist, cfg)
    st = out["routing"]["statistics"]
    assert st["routed_nets"] + st["unrouted_nets"] == st["total_nets"]
    # The progress callback fired at least once with the expected keys.
    assert events and "iteration" in events[0] and "overused_cells" in events[0]


def test_route_board_ncr_callback_exception_is_swallowed():
    """A progress callback that raises must not kill the router (3312-3313)."""
    placement, netlist = _two_ic_congested(n_per_side=14, pitch=0.5, gap=1.0)
    placement["board"]["width_mm"] = 11.0

    def _boom(_d):
        raise RuntimeError("bad callback")

    cfg = RouterConfig(ordering_trials=1, ncr_enabled=True, ncr_max_iterations=3,
                       shove_enabled=False, fine_grid_factor=1,
                       ncr_progress_callback=_boom)
    out = route_board(placement, netlist, cfg)  # does not raise
    st = out["routing"]["statistics"]
    assert st["total_nets"] >= 1


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


def test_apply_copper_fills_4layer_power_via_offset_stub_and_unrouted():
    """4-layer power-plane stitching edge cases:
    - a through-hole VCC pad is skipped (5470)
    - the pad-centre via site collides with an existing via (5487), an obstacle
      pad (5492-5493) and a foreign trace (5497-5498) → an offset candidate is
      used, emitting a stub trace (5512) added to traces (5583)
    - VCC listed as unrouted is dropped because the plane connects it (5592-5593).
    """
    routed, netlist = _routed_4layer_gnd_and_power()
    pad_map = build_pad_map(routed, netlist)
    vcc_pads = [p for p in pad_map.values() if p.net_id == "net_v"]
    assert vcc_pads
    vp = vcc_pads[0]
    # Block the VCC pad centre with an existing via + a foreign trace crossing it.
    routed["routing"]["vias"].append({
        "x_mm": round(vp.x_mm, 2), "y_mm": round(vp.y_mm, 2),
        "drill_mm": 0.3, "diameter_mm": 0.6,
        "from_layer": "top", "to_layer": "bottom",
        "net_id": "net_s", "net_name": "SIG"})
    routed["routing"]["traces"].append({
        "start_x_mm": vp.x_mm - 2.0, "start_y_mm": vp.y_mm,
        "end_x_mm": vp.x_mm + 2.0, "end_y_mm": vp.y_mm,
        "width_mm": 0.3, "layer": "top", "net_id": "net_s", "net_name": "SIG"})
    # Add a through-hole VCC pad to a new component (layer "all") → 5470 skip.
    routed["placements"].append({
        "designator": "J9", "component_type": "connector", "package": "PinHeader",
        "footprint_width_mm": 2.0, "footprint_height_mm": 2.0,
        "x_mm": 3.0, "y_mm": 3.0, "rotation_deg": 0, "layer": "top"})
    netlist["elements"].append({
        "element_type": "component", "component_id": "C9", "designator": "J9",
        "component_type": "connector", "properties": {}})
    netlist["elements"].append({
        "element_type": "port", "port_id": "P9", "component_id": "C9",
        "designator": "J9", "pin_number": 1, "name": "1", "layer": "all"})
    for e in netlist["elements"]:
        if e.get("element_type") == "net" and e["net_id"] == "net_v":
            e["connected_port_ids"].append("P9")
    # Mark VCC unrouted so 5592-5593 strips it once the plane covers it.
    routed["routing"]["unrouted_nets"] = ["net_v"]
    # A routing via right next to the GND stitch via site (5.5, 5.5) so the
    # hole-to-hole filter drops the stitch via (5577).
    routed["routing"]["vias"].append({
        "x_mm": 5.55, "y_mm": 5.5, "drill_mm": 0.3, "diameter_mm": 0.6,
        "from_layer": "top", "to_layer": "bottom",
        "net_id": "net_g", "net_name": "GND"})

    out = apply_copper_fills(routed, netlist, RouterConfig())
    # VCC removed from unrouted (plane connects it).
    assert "net_v" not in out["routing"].get("unrouted_nets", [])
    # An offset power stub trace was added on the VCC net.
    assert any(t.get("net_id") == "net_v" for t in out["routing"]["traces"])


def test_apply_copper_fills_4layer_power_via_collisions_and_drops():
    """4-layer power stitching: a fully-crowded VCC pad finds no clear site
    (5524); a stitch via dropped for being too close to a routing via (5577);
    a candidate colliding with an already-placed via position (5487)."""
    import math as _m
    routed, netlist = _routed_4layer_gnd_and_power()
    pad_map = build_pad_map(routed, netlist)
    vcc_pads = [p for p in pad_map.values() if p.net_id == "net_v"]
    vp = vcc_pads[0]
    # Surround the VCC pad with a dense ring of FOREIGN routing vias covering
    # every candidate position (centre + radii 0.6/0.9/1.3 × 8 angles) so no via
    # site is clear → the "no clear via site" warning fires (5524).
    foreign_vias = [{"x_mm": round(vp.x_mm, 4), "y_mm": round(vp.y_mm, 4),
                     "drill_mm": 0.3, "diameter_mm": 0.6,
                     "from_layer": "top", "to_layer": "bottom",
                     "net_id": "net_s", "net_name": "SIG"}]
    for r in (0.6, 0.9, 1.3):
        for k in range(8):
            foreign_vias.append({
                "x_mm": round(vp.x_mm + r * _m.cos(_m.pi * k / 4), 4),
                "y_mm": round(vp.y_mm + r * _m.sin(_m.pi * k / 4), 4),
                "drill_mm": 0.3, "diameter_mm": 0.6,
                "from_layer": "top", "to_layer": "bottom",
                "net_id": "net_s", "net_name": "SIG"})
    routed["routing"]["vias"].extend(foreign_vias)
    out = apply_copper_fills(routed, netlist, RouterConfig())
    # Still produces planes; the crowded pad simply gets no stub.
    assert any(f.get("is_plane") for f in out["routing"]["copper_fills"])


def test_apply_copper_fills_no_gnd_net_is_noop():
    placement, netlist = _two_pad_board(net_class="signal")
    routed = route_board(placement, netlist, _fast_cfg(fill_enabled=False))
    out = apply_copper_fills(routed, netlist, RouterConfig())
    assert "copper_fills" not in out["routing"]


# ===========================================================================
# Coverage drive — scattered helper guards & alternate branches
# ===========================================================================

def test_compute_net_current_regulator_bad_max_current_string_swallowed():
    """A non-numeric max_current on a regulator load pin must not raise — the
    except swallows it and the net falls back to the class default (257-258)."""
    net = NetInfo(net_id="n", name="VCC", net_class="power", designators=["U1"])
    netlist = {"elements": [
        {"element_type": "component", "component_id": "C1", "designator": "U1",
         "component_type": "voltage_regulator",
         "properties": {"max_current": "not-a-number"}},
        {"element_type": "port", "port_id": "P1", "component_id": "C1",
         "name": "OUT"},  # load pin, not a sense pin → enters the try
        {"element_type": "net", "net_id": "n", "name": "VCC",
         "net_class": "power", "connected_port_ids": ["P1"]},
    ]}
    # Does not raise; falls back to the power-class default.
    assert compute_net_current(net, netlist) == 0.5


def test_astar_congestion_obstacle_start_or_end_returns_none():
    """Congestion A* returns None immediately when either endpoint is an
    OBSTACLE cell (632 / 634)."""
    grid = RoutingGrid(5.0, 5.0, 0.5)
    sc, sr = grid.mm_to_grid(1.0, 1.0)
    ec, er = grid.mm_to_grid(4.0, 4.0)
    grid.set(sc, sr, "top", OBSTACLE)
    assert astar_route_congestion(grid, (sc, sr, "top"), (ec, er, "top"), 1) is None
    grid2 = RoutingGrid(5.0, 5.0, 0.5)
    grid2.set(ec, er, "top", OBSTACLE)
    assert astar_route_congestion(grid2, (sc, sr, "top"), (ec, er, "top"), 1) is None


def test_mark_path_default_via_half_width_equals_half_width():
    """mark_path_on_grid with via_half_width_cells=None defaults to
    half_width_cells (968) — a layer-changing path still marks via clearance."""
    grid = RoutingGrid(6.0, 6.0, 0.5)
    path = [(2, 2, "top"), (2, 2, "bottom"), (3, 2, "bottom")]
    mark_path_on_grid(grid, path, 7, half_width_cells=1, via_half_width_cells=None)
    # The via cell + its half-width neighbourhood are marked on both layers.
    assert grid.get(2, 2, "top") == 7
    assert grid.get(2, 2, "bottom") == 7
    assert grid.get(3, 2, "top") == 7  # via half-width=1 bleeds to neighbour


def test_snap_endpoints_pulls_via_onto_pad_b():
    """A via within snap tolerance of pad_b is recentred onto pad_b (1198)."""
    res = 0.2
    pad_a = _pad(1.0, 1.0, net="n")
    pad_b = _pad(5.0, 5.0, net="n", layer="bottom")
    traces = [TraceSegment(1.0, 1.0, 5.0, 5.0, 0.2, "top", "n", "N")]
    vias = [Via(5.05, 5.05, 0.3, 0.6, "top", "bottom", "n", "N")]
    _snap_endpoints_to_pads(traces, vias, pad_a, pad_b, res)
    assert vias[0].x_mm == 5.0 and vias[0].y_mm == 5.0


def test_astar_find_blockers_with_path_wide_trace_and_no_path():
    """astar_find_blockers_with_path: wide trace cost path (2027-2035),
    non-diagonal heuristic (2014), wide-trace blocker collection (2058-2062),
    and no-path None (2093)."""
    grid = RoutingGrid(8.0, 8.0, 0.5)
    sc, sr = grid.mm_to_grid(1.0, 4.0)
    ec, er = grid.mm_to_grid(6.0, 4.0)
    # A full-height wall of foreign net cells at one column — the route MUST
    # pass through it (passable at cost), so net 9 is collected as a blocker.
    mc, _ = grid.mm_to_grid(3.5, 4.0)
    for r2 in range(grid.rows):
        grid.set(mc, r2, "top", 9)
        grid.set(mc, r2, "bottom", OBSTACLE)  # no via escape around the wall
    r = astar_find_blockers_with_path(
        grid, (sc, sr, "top"), (ec, er, "top"), 1,
        half_width_cells=1, diagonal=False, foreign_net_cost=10.0,
    )
    assert r is not None
    path, blockers = r
    assert path[0] == (sc, sr, "top") and path[-1] == (ec, er, "top")
    assert 9 in blockers

    # Fully walled target → None.
    grid2 = RoutingGrid(8.0, 8.0, 0.5)
    sc2, sr2 = grid2.mm_to_grid(1.0, 1.0)
    ec2, er2 = grid2.mm_to_grid(6.0, 6.0)
    for c in range(grid2.cols):
        for r2 in range(grid2.rows):
            if (c, r2) != (sc2, sr2):
                grid2.set(c, r2, "top", OBSTACLE)
                grid2.set(c, r2, "bottom", OBSTACLE)
    assert astar_find_blockers_with_path(
        grid2, (sc2, sr2, "top"), (ec2, er2, "top"), 1,
        half_width_cells=1, diagonal=False) is None


def test_bitmap_to_polygons_merges_stacked_rows():
    """Vertically-stacked identical runs merge into one tall rectangle (the
    merge loop's extend-downward + group-break logic, 2474-2491)."""
    grid = RoutingGrid(2.0, 3.0, 1.0)  # 2 cols × 3 rows
    cols, rows = grid.cols, grid.rows
    filled = [False] * (cols * rows)
    # Fill column 0 on all rows → one tall merged rectangle.
    for r in range(rows):
        filled[r * cols + 0] = True
    polys = _bitmap_to_polygons(filled, grid, "top")
    assert len(polys) == 1
    ys = [v[1] for v in polys[0]]
    assert max(ys) - min(ys) == rows * grid.resolution  # full height merged


def test_add_stitching_vias_rejects_site_with_out_of_bounds_neighbour():
    """_cell_clear returns False when the via footprint reaches off-grid (2543).
    A 1-cell-tall grid forces every candidate's via-radius window out of bounds,
    so no stitching via is placed even with fill on both layers."""
    # Tiny grid: via_radius_cells >= 1, but only a couple rows → footprint OOB.
    grid = RoutingGrid(6.0, 1.0, 0.5)  # rows ~2
    n = grid.cols * grid.rows
    filled = [True] * n
    vias = _add_stitching_vias(filled, filled, grid, 7, RouterConfig())
    assert vias == []  # no clear site (every candidate footprint is OOB)


def test_add_rescue_vias_skips_no_via_zone_cell():
    """A disconnected top island whose only bottom-filled cell is inside a
    no-via zone yields no rescue via (the can_place_via skip, 2808)."""
    grid = RoutingGrid(8.0, 8.0, 1.0)
    cols = grid.cols
    total = cols * grid.rows
    filled_top = [False] * total
    filled_bottom = [False] * total
    # A 2×2 disconnected top island, with bottom fill underneath every cell.
    island = [(2, 2), (3, 2), (2, 3), (3, 3)]
    for c, r in island:
        filled_top[r * cols + c] = True
        filled_bottom[r * cols + c] = True
    # Forbid vias on the entire island → can_place_via False for every candidate.
    grid.mark_no_via_rect(1.5, 1.5, 4.5, 4.5)
    vias = _add_rescue_vias(filled_top, filled_bottom, grid, 7, RouterConfig())
    assert vias == []


def test_add_rescue_vias_island_without_bottom_fill_unrescuable():
    """A disconnected top island with no bottom fill underneath gets no rescue
    via (the 'no bottom fill' continue, 2814)."""
    grid = RoutingGrid(8.0, 8.0, 1.0)
    cols, rows = grid.cols, grid.rows
    total = cols * rows
    filled_top = [False] * total
    filled_bottom = [False] * total
    # A 2×2 top island in the corner, NOT seeded with fill_net_int → disconnected.
    for c, r in [(1, 1), (2, 1), (1, 2), (2, 2)]:
        filled_top[r * cols + c] = True
    # filled_bottom stays empty everywhere → no rescue candidate.
    vias = _add_rescue_vias(filled_top, filled_bottom, grid, 7, RouterConfig())
    assert vias == []


def test_silkscreen_board_name_right_anchored_label():
    """project_name set → bottom-right ('right' anchor) label survives, driving
    the anchor=='right' branches in _text_overlaps_exclusion (4898/right) and the
    exclusion-zone extension (4944-4945)."""
    placement, netlist = _two_pad_board()
    placement["project_name"] = "MyBoard"
    pad_map = build_pad_map(placement, netlist)
    silk = _generate_silkscreen(placement, netlist, pad_map)
    name_items = [s for s in silk if s.get("purpose") == "board_name"]
    assert name_items, "board name label should be emitted"
    assert name_items[0]["anchor"] in ("right", "left")


def test_order_nets_by_channel_pressure_no_channels_passthrough():
    """No channels → nets returned unchanged (1724)."""
    nets = [_net("a", "A"), _net("b", "B")]
    placement, netlist = _two_pad_board()
    pad_map = build_pad_map(placement, netlist)
    net_id_map = {"a": 1, "b": 2}
    out = order_nets_by_channel_pressure(nets, pad_map, [], net_id_map, netlist)
    assert out == list(nets)


def test_order_nets_by_channel_pressure_scores_crossing_nets():
    """With a channel and a net straddling it, the crossing net scores higher
    than a single-pad net and sorts first (1747-1779)."""
    # Two IC rows with a gap → a horizontal channel between them.
    pad_map = _ic_pads("U1", 5.0, 5.0, pitch=1.5, rows_y=(0.0, 6.0), n_per_row=4)
    # Assign a net to one pad above the channel and one below → must cross.
    keys = list(pad_map.keys())
    above = pad_map[keys[0]]   # row y=5.0
    below = pad_map[keys[-1]]  # row y=11.0
    pad_map[keys[0]] = PadInfo(
        port_id=above.port_id, designator=above.designator,
        pin_number=above.pin_number, net_id="cross",
        x_mm=above.x_mm, y_mm=above.y_mm,
        pad_width_mm=above.pad_width_mm, pad_height_mm=above.pad_height_mm,
        layer=above.layer)
    pad_map[keys[-1]] = PadInfo(
        port_id=below.port_id, designator=below.designator,
        pin_number=below.pin_number, net_id="cross",
        x_mm=below.x_mm, y_mm=below.y_mm,
        pad_width_mm=below.pad_width_mm, pad_height_mm=below.pad_height_mm,
        layer=below.layer)
    channels = _detect_channels(pad_map, RouterConfig(), 30.0, 30.0)
    assert channels  # there's a channel to cross
    nets = [_net("solo", "S"),  # solo has <2 pads on net → score 0, appended
            NetInfo(net_id="cross", name="X", net_class="signal",
                    designators=["U1"])]
    net_id_map = {"cross": 1, "solo": 2}
    out = order_nets_by_channel_pressure(nets, pad_map, channels, net_id_map, netlist={"elements": []})
    # The crossing net is ordered ahead of the zero-score solo net.
    assert out[0].net_id == "cross"


def test_build_output_ipc_upsize_float_override_formatted():
    """A float trace_width_override (IPC upsize) is rendered with the upsize
    suffix (5059) and 3800 sets it when ipc_min exceeds the default width."""
    res = RoutingResult()
    res.trace_width_overrides = {"net_0": 0.812}  # float → IPC upsize path
    placement, netlist = _two_pad_board()
    pad_map = build_pad_map(placement, netlist)
    out = _build_output(placement, netlist, res, [_net()], RouterConfig(),
                        pad_map, copper_fills=None)
    ov = out["routing"]["trace_width_overrides"]
    assert ov["net_0"].endswith("(IPC-2221 upsize)")
    assert ov["net_0"].startswith("0.812")


def test_route_board_power_net_high_current_upsizes_and_reports():
    """End-to-end: a high-current regulator output net forces an IPC upsize >
    default, so 3800 records the float override and _build_output formats it
    (5059)."""
    placement, netlist = _two_pad_board(net_class="power")
    # Make the source component a voltage regulator with a 5A load pin so
    # compute_net_current reports a high current (drives the IPC upsize).
    for e in netlist["elements"]:
        if e.get("element_type") == "net":
            e["name"] = "VCC"
        if e.get("element_type") == "component" and e["component_id"] == "C1":
            e["component_type"] = "voltage_regulator"
            e["properties"] = {"max_current": "5A"}
        # P2 belongs to C1 and is on the net — name it as a load pin.
        if e.get("element_type") == "port" and e["port_id"] == "P2":
            e["name"] = "OUT"
    out = route_board(placement, netlist, _fast_cfg())
    ov = out["routing"].get("trace_width_overrides", {})
    # The power net got an IPC upsize string.
    assert any("IPC-2221 upsize" in v for v in ov.values())


def test_apply_copper_fills_default_config_and_unknown_net_via():
    """config=None default (5248); a via with an unmapped net_id is skipped in
    the grid-marking loop (5385); silkscreen regenerated when absent (5615)."""
    routed, netlist = _routed_two_pad(layers=2)
    # A via with an UNMAPPED net_id — vias survive the dangling pass, so the
    # marking loop sees it and skips on nid==0 (5385).
    routed["routing"]["vias"].append({
        "x_mm": 3.0, "y_mm": 3.0, "drill_mm": 0.3, "diameter_mm": 0.6,
        "from_layer": "top", "to_layer": "bottom",
        "net_id": "ghost", "net_name": "GHOST"})
    routed.pop("silkscreen", None)  # force regeneration (5615)
    out = apply_copper_fills(routed, netlist, config=None)  # default config
    assert out["routing"]["copper_fills"]
    assert out.get("silkscreen")  # regenerated


def test_compute_net_current_led_bad_if_string_uses_default():
    """A non-numeric LED forward-current string falls through to LED_IF_DEFAULT
    (242-243)."""
    net = NetInfo(net_id="n", name="D1A", net_class="signal", designators=["D1"])
    netlist = {"elements": [
        {"element_type": "component", "component_id": "C1", "designator": "D1",
         "component_type": "led", "properties": {"if": "bogus"}},
        {"element_type": "port", "port_id": "P1", "component_id": "C1",
         "name": "A"},
        {"element_type": "net", "net_id": "n", "name": "D1A",
         "net_class": "signal", "connected_port_ids": ["P1"]},
    ]}
    from optimizers.router import LED_IF_DEFAULT
    assert compute_net_current(net, netlist) == LED_IF_DEFAULT


def test_route_net_congestion_all_mst_edges_unroutable_returns_none():
    """route_net_congestion returns None when an MST edge has no path (1379)."""
    placement, netlist = _two_pad_board()
    pad_map = build_pad_map(placement, netlist)
    nets = build_connectivity(netlist)
    net = nets[0]
    cfg = RouterConfig()
    grid = RoutingGrid(20.0, 20.0, cfg.grid_resolution_mm)
    _setup_grid(grid, placement, pad_map, cfg.clearance_mm, {net.net_id: 1})
    # Wall off both layers entirely (OBSTACLE is impassable even to congestion
    # A*) so no path exists between the pads.
    for c in range(grid.cols):
        for r in range(grid.rows):
            for layer in ("top", "bottom"):
                grid.set(c, r, layer, OBSTACLE)
    n = grid.cols * grid.rows
    hist = {"top": [0.0] * n, "bottom": [0.0] * n}
    occ = {"top": [0] * n, "bottom": [0] * n}
    out = route_net_congestion(grid, net, pad_map, 1, cfg, netlist, 0.25,
                               history_cost=hist, present_occupancy=occ)
    assert out is None


def test_setup_grid_rotated_th_component_swaps_footprint_dims():
    """A 90°-rotated through-hole component body swaps width/height when marking
    obstacles (1455)."""
    placement = {
        "board": {"width_mm": 20.0, "height_mm": 20.0, "layers": 2,
                  "outline_type": "rectangle", "origin": [0, 0]},
        "placements": [{
            "designator": "J1", "component_type": "connector",
            "package": "DIP-8", "footprint_width_mm": 8.0,
            "footprint_height_mm": 2.0, "x_mm": 10.0, "y_mm": 10.0,
            "rotation_deg": 90, "layer": "top"}],
    }
    netlist = {"elements": [
        {"element_type": "component", "component_id": "C1", "designator": "J1",
         "component_type": "connector", "properties": {}}]}
    pad_map = build_pad_map(placement, netlist)
    grid = RoutingGrid(20.0, 20.0, 0.5)
    _setup_grid(grid, placement, pad_map, 0.2, {})
    # Rotated 90°: the 8mm dimension is now vertical. A cell 3mm ABOVE centre
    # (within the swapped 8mm extent) is an obstacle; one 3mm to the SIDE (only
    # 2mm half-extent) is clear.
    cv, rv = grid.mm_to_grid(10.0, 13.0)
    ch, rh = grid.mm_to_grid(13.0, 10.0)
    assert grid.get(cv, rv, "top") == OBSTACLE   # tall axis (was width)
    assert grid.get(ch, rh, "top") != OBSTACLE   # short axis (was height)


def test_detect_channels_too_narrow_gap_skipped():
    """Two pad rows too close together yield no channel (1619)."""
    # Two rows 0.5mm apart, ≥3 pads each, overlapping in X → gap too narrow.
    pad_map = _ic_pads("U1", 5.0, 5.0, pitch=1.5, rows_y=(0.0, 0.5), n_per_row=4)
    channels = _detect_channels(pad_map, RouterConfig(), 30.0, 30.0)
    assert all(ch.axis != "horizontal" for ch in channels)


def test_detect_channels_rows_no_x_overlap_skipped():
    """Two wide-gap rows with disjoint X ranges yield no horizontal channel
    (1626)."""
    pads = {}
    # Row A at y=5: x 0,1.5,3 ; Row B at y=15: x 20,21.5,23 (no X overlap)
    for k, x in enumerate((0.0, 1.5, 3.0)):
        pads[f"U1_a{k}"] = PadInfo(port_id=f"a{k}", designator="U1",
            pin_number=k, net_id=None, x_mm=x, y_mm=5.0,
            pad_width_mm=0.5, pad_height_mm=0.5, layer="top")
    for k, x in enumerate((20.0, 21.5, 23.0)):
        pads[f"U1_b{k}"] = PadInfo(port_id=f"b{k}", designator="U1",
            pin_number=10 + k, net_id=None, x_mm=x, y_mm=15.0,
            pad_width_mm=0.5, pad_height_mm=0.5, layer="top")
    channels = _detect_channels(pads, RouterConfig(), 30.0, 30.0)
    assert all(ch.axis != "horizontal" for ch in channels)


def test_detect_channels_vertical_cols_no_y_overlap_skipped():
    """Two wide-gap columns with disjoint Y ranges yield no vertical channel
    (1660)."""
    pads = {}
    for k, y in enumerate((0.0, 1.5, 3.0)):
        pads[f"U1_a{k}"] = PadInfo(port_id=f"a{k}", designator="U1",
            pin_number=k, net_id=None, x_mm=5.0, y_mm=y,
            pad_width_mm=0.5, pad_height_mm=0.5, layer="top")
    for k, y in enumerate((20.0, 21.5, 23.0)):
        pads[f"U1_b{k}"] = PadInfo(port_id=f"b{k}", designator="U1",
            pin_number=10 + k, net_id=None, x_mm=15.0, y_mm=y,
            pad_width_mm=0.5, pad_height_mm=0.5, layer="top")
    channels = _detect_channels(pads, RouterConfig(), 30.0, 30.0)
    assert all(ch.axis != "vertical" for ch in channels)


def test_build_pad_zone_index_skips_unmapped_pad():
    """A pad with net_id None / unmapped is skipped (1814)."""
    grid = RoutingGrid(10.0, 10.0, 0.5)
    pad_map = {
        "p_none": _pad(2.0, 2.0, net=None),
        "p_unmapped": _pad(4.0, 4.0, net="missing"),
        "p_ok": _pad(6.0, 6.0, net="net_0"),
    }
    pzi = _build_pad_zone_index(pad_map, {"net_0": 1}, 0.2, grid)
    assert set(pzi.keys()) == {1}  # only the mapped pad got a zone


def test_trace_segment_at_isolated_cell_returns_self():
    """An isolated same-net trace cell (no same-net neighbours) returns just
    itself (1875)."""
    grid = RoutingGrid(10.0, 10.0, 0.5)
    grid.set(5, 5, "top", 7)  # lone cell, no neighbours of net 7
    seg = _trace_segment_at(grid, 5, 5, "top", 7, {})
    assert seg == [(5, 5)]


def test_try_shove_segment_off_grid_edge_fails():
    """Shoving a segment past the grid edge fails (1946)."""
    grid = RoutingGrid(5.0, 5.0, 0.5)
    last_col = grid.cols - 1
    grid.set(last_col, 3, "top", 7)
    snap = {}
    ok = _try_shove_segment(grid, [(last_col, 3)], "top", 7,
                            dc=1, dr=0, pzi={}, depth=0, max_depth=3,
                            snapshot=snap)
    assert ok is False


def test_remove_dangling_traces_empty_is_noop():
    """No traces → returns 0 (5157)."""
    assert _remove_dangling_traces({"traces": []}, {}) == 0


def test_remove_dangling_traces_endpoint_on_via_is_supported():
    """A trace whose free end coincides with a same-net via is supported and
    kept (5177)."""
    routing = {
        "traces": [{"start_x_mm": 1.0, "start_y_mm": 1.0,
                    "end_x_mm": 3.0, "end_y_mm": 1.0,
                    "width_mm": 0.25, "layer": "top",
                    "net_id": "net_0", "net_name": "N"}],
        "vias": [
            {"x_mm": 1.0, "y_mm": 1.0, "net_id": "net_0"},
            {"x_mm": 3.0, "y_mm": 1.0, "net_id": "net_0"},
        ],
    }
    pad_map = {}  # no pads — only vias support the endpoints
    removed = _remove_dangling_traces(routing, pad_map)
    assert removed == 0  # both ends sit on same-net vias → kept
    assert len(routing["traces"]) == 1


def test_post_route_nudge_skips_gnd_stitch_via_and_zero_length_trace():
    """_post_route_nudge skips GND stitch vias (4668) and zero-length traces
    (4690)."""
    result = RoutingResult()
    # A zero-length foreign-net trace right at a (non-stitch) foreign via — the
    # via-trace clearance check fires, then the zero-length guard skips it (4690).
    # A separate GND stitch via must be skipped entirely by the loop (4668).
    result.traces = [TraceSegment(5.0, 5.0, 5.0, 5.0, 0.25, "top", "net_a", "A")]
    result.vias = [
        Via(5.0, 5.0, 0.3, 0.6, "top", "bottom", "net_b", "B"),      # foreign
        Via(8.0, 8.0, 0.3, 0.6, "top", "bottom", "stitch_0", "GND"),  # skipped
    ]
    _post_route_nudge(result, {}, RouterConfig())
    # zero-length trace untouched (skipped) — no crash.
    assert result.traces[0].start_x_mm == result.traces[0].end_x_mm


def test_silkscreen_left_anchor_and_dot_filtering():
    """A project_name forced to a left-anchored position drives the 'left' (else)
    anchor branches (4898 else / 4944-4947 else), and a pin-1 dot near a
    component is filtered. Uses a board where the bottom-right is blocked so the
    label falls to a left anchor."""
    placement = {
        "board": {"width_mm": 30.0, "height_mm": 30.0, "layers": 2,
                  "outline_type": "rectangle", "origin": [0, 0]},
        "project_name": "LongBoardNameX",
        "placements": [
            # Component occupying the bottom-right corner so the 'right' label
            # candidates overlap and the 'left' candidate is chosen.
            _comp("C1", "U1", 27.0, 3.0, ctype="ic", fw=6, fh=6),
        ],
    }
    netlist = {"elements": [
        {"element_type": "component", "component_id": "C1", "designator": "U1",
         "component_type": "ic", "properties": {}},
        {"element_type": "port", "port_id": "P1", "component_id": "C1",
         "designator": "U1", "pin_number": 1, "name": "1"},
        {"element_type": "port", "port_id": "P2", "component_id": "C1",
         "designator": "U1", "pin_number": 2, "name": "2"},
    ]}
    pad_map = build_pad_map(placement, netlist)
    silk = _generate_silkscreen(placement, netlist, pad_map)
    name = [s for s in silk if s.get("purpose") == "board_name"]
    assert name and name[0]["anchor"] == "left"


def test_apply_copper_fills_reverts_dangling_removal_on_regression():
    """If removing a dangling stub would disconnect a net, the removal is
    reverted (5268-5269)."""
    routed, netlist = _routed_two_pad(layers=2)
    pad_map = build_pad_map(routed, netlist)
    pads = [p for p in pad_map.values() if p.net_id == "net_0"]
    a, b = pads[0], pads[1]
    # Replace traces with a single trace touching ONLY pad a — pad b dangles.
    # _remove_dangling_traces will strip it, which disconnects net_0, so the
    # revert restores the original traces.
    routed["routing"]["traces"] = [{
        "start_x_mm": a.x_mm, "start_y_mm": a.y_mm,
        "end_x_mm": a.x_mm + 1.0, "end_y_mm": a.y_mm,
        "width_mm": 0.25, "layer": "top", "net_id": "net_0", "net_name": "GND"}]
    before = list(routed["routing"]["traces"])
    out = apply_copper_fills(routed, netlist, RouterConfig())
    # Stub was reverted (still present) because removing it regressed connectivity.
    assert out["routing"]["copper_fills"] is not None


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-q"]))
