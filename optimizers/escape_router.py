#!/usr/bin/env python3
"""Fine-pitch escape / fanout pre-routing.

At 0.5 mm pad pitch with 0.127 mm trace/clearance only one trace fits between
adjacent pads, so getting N signals out of a fine-pitch pad field needs a
systematic *breakout*: each pad gets a short stub to a via ("dog-bone") just
outside the pad row, dropping the signal to another layer where it routes at
normal pitch. Freerouting — a generic net-by-net rip-up router — has no concept
of fanning a pad field out as a group, so it leaves fine-pitch pins as stubs or
unrouted. This module pre-generates those escapes as *protected wiring* (fed to
Freerouting via the existing `fixed_routing` / `(type protect)` path) so the
autorouter only has to route from the comfortable-pitch breakout vias onward.

v1 handles single-row fine-pitch parts (FPC/edge connectors — morgan's `CN1`),
escaping all pads perpendicular to the row, staggered into two via rows so the
vias clear each other at the pad pitch. Multi-row / quad parts (QFP/QFN) are
left for the autorouter (skipped) until a per-edge version is added.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from .pad_geometry import build_pad_map, PadInfo

# A part is an escape candidate when its tightest adjacent-pad pitch is below
# this and it has at least this many pins (targets connectors / fine-pitch ICs,
# not 2-pin passives).
ESCAPE_PITCH_THRESHOLD_MM = 0.8
ESCAPE_MIN_PINS = 10


@dataclass
class EscapeConfig:
    trace_width_mm: float = 0.127
    clearance_mm: float = 0.127
    via_diameter_mm: float = 0.45
    via_drill_mm: float = 0.2
    drop_layer: str = "bottom"   # layer the escape vias drop the signal onto
    pitch_threshold_mm: float = ESCAPE_PITCH_THRESHOLD_MM
    min_pins: int = ESCAPE_MIN_PINS


def _nets_leaving_part(netlist: dict, designator: str) -> set[str]:
    """Net ids on `designator` that also connect to at least one OTHER
    component — i.e. nets that genuinely have to escape the part."""
    comp_of_port: dict[str, str] = {}
    des_of_comp: dict[str, str] = {}
    for e in netlist.get("elements", []):
        if e.get("element_type") == "component":
            des_of_comp[e["component_id"]] = e.get("designator", "")
        elif e.get("element_type") == "port":
            comp_of_port[e["port_id"]] = e.get("component_id", "")
    leaving: set[str] = set()
    for e in netlist.get("elements", []):
        if e.get("element_type") != "net":
            continue
        dess = {des_of_comp.get(comp_of_port.get(pid, ""), "")
                for pid in e.get("connected_port_ids", [])}
        dess.discard("")
        if designator in dess and len(dess) >= 2:
            leaving.add(e["net_id"])
    return leaving


def _min_adjacent_pitch(pads: list[PadInfo]) -> float | None:
    best: float | None = None
    for i in range(len(pads)):
        for j in range(i + 1, len(pads)):
            d = math.hypot(pads[i].x_mm - pads[j].x_mm,
                           pads[i].y_mm - pads[j].y_mm)
            if d > 1e-6 and (best is None or d < best):
                best = d
    return best


def _is_protected(net_id: str | None, exclude: set[str], names: dict) -> bool:
    return bool(net_id) and net_id not in exclude and \
        names.get(net_id, net_id) not in exclude


def generate_escape_routing(
    placement: dict,
    netlist: dict,
    config: EscapeConfig | None = None,
    exclude_nets: tuple[str, ...] = (),
    pad_map: dict | None = None,
) -> dict:
    """Return {"traces": [...], "vias": [...]} of dog-bone escapes for the
    board's single-row fine-pitch parts. Empty when there are none.

    The output is in routed-schema trace/via form, ready to hand to
    `route_with_freerouting(..., fixed_routing=...)` as protected wiring.
    `pad_map` may be supplied to bypass footprint resolution (used in tests).
    """
    cfg = config or EscapeConfig()
    if pad_map is None:
        pad_map = build_pad_map(placement, netlist)

    board = placement.get("board", {})
    bw = board.get("width_mm", 50.0)
    bh = board.get("height_mm", 50.0)
    bcx, bcy = bw / 2.0, bh / 2.0

    # net id -> name (exclude_nets may be given as names or ids)
    net_names: dict[str, str] = {}
    for e in netlist.get("elements", []):
        if e.get("element_type") == "net":
            net_names[e["net_id"]] = e.get("name", e["net_id"])
    exclude = set(exclude_nets)

    # Group pads by part
    by_part: dict[str, list[PadInfo]] = {}
    for pad in pad_map.values():
        by_part.setdefault(pad.designator, []).append(pad)

    traces: list[dict] = []
    vias: list[dict] = []
    placed_via_centers: list[tuple[float, float]] = []  # collision across parts

    via_r = cfg.via_diameter_mm / 2.0
    via_clear = cfg.via_diameter_mm + cfg.clearance_mm  # min center distance

    for des, pads in by_part.items():
        if len(pads) < cfg.min_pins:
            continue
        # Skip through-hole pads (layer "all") — they don't need an escape.
        smd = [p for p in pads if p.layer in ("top", "bottom")]
        if len(smd) < cfg.min_pins:
            continue
        pitch = _min_adjacent_pitch(smd)
        if pitch is None or pitch >= cfg.pitch_threshold_mm:
            continue

        xs = [p.x_mm for p in smd]
        ys = [p.y_mm for p in smd]
        span_x, span_y = max(xs) - min(xs), max(ys) - min(ys)
        tol = pitch * 0.5
        if span_y <= tol and span_x > tol:
            row_axis = "x"           # pads vary in x → escape along ±y
        elif span_x <= tol and span_y > tol:
            row_axis = "y"           # pads vary in y → escape along ±x
        else:
            continue                 # not a single row — leave to the autorouter

        leaving = _nets_leaving_part(netlist, des)

        # Escape direction: perpendicular to the row, toward the board interior.
        if row_axis == "x":
            cx = sum(xs) / len(xs)
            edir = (0.0, 1.0 if bcy >= sum(ys) / len(ys) else -1.0)
            order = sorted(smd, key=lambda p: p.x_mm)
        else:
            edir = (1.0 if bcx >= sum(xs) / len(xs) else -1.0, 0.0)
            order = sorted(smd, key=lambda p: p.y_mm)

        # Stub base: clear the pad edge + the via body + clearance.
        half_pad = max(max(p.pad_width_mm, p.pad_height_mm) for p in smd) / 2.0
        base = half_pad + via_r + cfg.clearance_mm + 0.05
        stagger = via_clear + 0.05    # second via row sits this much further out

        part_vias: list[tuple[float, float]] = []
        for i, pad in enumerate(order):
            if not _is_protected(pad.net_id, exclude, net_names):
                continue
            if pad.net_id not in leaving:
                continue
            dist = base + (i % 2) * stagger
            vx = round(pad.x_mm + edir[0] * dist, 3)
            vy = round(pad.y_mm + edir[1] * dist, 3)

            # Collision guard: keep clear of every via already placed.
            clash = any(math.hypot(vx - ox, vy - oy) < via_clear - 1e-6
                        for ox, oy in placed_via_centers + part_vias)
            if clash:
                continue

            nm = net_names.get(pad.net_id, pad.net_id)
            traces.append({
                "start_x_mm": round(pad.x_mm, 3), "start_y_mm": round(pad.y_mm, 3),
                "end_x_mm": vx, "end_y_mm": vy,
                "width_mm": cfg.trace_width_mm, "layer": pad.layer,
                "net_id": pad.net_id, "net_name": nm,
            })
            vias.append({
                "x_mm": vx, "y_mm": vy,
                "drill_mm": cfg.via_drill_mm, "diameter_mm": cfg.via_diameter_mm,
                "from_layer": pad.layer, "to_layer": cfg.drop_layer,
                "net_id": pad.net_id, "net_name": nm,
            })
            part_vias.append((vx, vy))
        placed_via_centers.extend(part_vias)

    return {"traces": traces, "vias": vias}
