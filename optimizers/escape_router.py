#!/usr/bin/env python3
"""Fine-pitch escape / fanout pre-routing.

At 0.5 mm pad pitch with 0.127 mm trace/clearance only one trace fits between
adjacent pads, so getting N signals out of a fine-pitch pad field needs a
systematic *breakout*: each pad gets a short stub to a via ("dog-bone") just
outside the pad row, dropping the signal to another layer where it routes at
normal pitch. Freerouting — a generic net-by-net rip-up router — has no concept
of fanning a pad field out as a group, so it leaves fine-pitch pins as stubs or
routes a trace straight across a neighbour's pad. This module pre-generates the
whole breakout as *protected wiring* (fed to Freerouting via the existing
``fixed_routing`` / ``(type protect)`` path) so the autorouter starts from a
clean, comfortable-pitch grid clear of the pad field and never has to enter it.

Geometry (single-row / single-column fine-pitch parts — FPC/edge connectors,
morgan's ``CN1``):

  pad ──stub──▶ via ──onward──▶ release line
                (2 staggered via rows so the Ø-via field clears at pad pitch)

Every escaping pin is broken out — including pins on a plane net (e.g. GND),
which drop straight to their plane layer with a via (no onward trace; the plane
makes the connection).  The near via-row's onward traces thread the gaps
*between* the far via-row deterministically (≈0.2 mm clearance), which the
generic autorouter could not reliably do on its own.  Onward traces drop to a
stackup-aware signal layer (an inner signal layer when one exists, never a
plane).  Multi-row / quad parts (QFP/QFN) are left to the autorouter.
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

# Ordered copper layers by board layer count (routed-schema layer names).
_LAYER_ORDER = {
    2: ["top", "bottom"],
    4: ["top", "inner1", "inner2", "bottom"],
}
_INNER_PLANE_LAYERS = ["inner1", "inner2"]


@dataclass
class EscapeConfig:
    trace_width_mm: float = 0.127
    clearance_mm: float = 0.127
    via_diameter_mm: float = 0.45
    via_drill_mm: float = 0.2
    drop_layer: str | None = None   # onward-trace layer; None → stackup-aware auto
    num_layers: int = 4
    plane_layers: int = 0           # inner layers that are solid planes (In1=GND…)
    pitch_threshold_mm: float = ESCAPE_PITCH_THRESHOLD_MM
    min_pins: int = ESCAPE_MIN_PINS
    onward_margin_mm: float = 0.15  # release line offset past the far via row


def _plane_layer_names(num_layers: int, plane_layers: int) -> list[str]:
    """Routed-schema names of the inner PLANE layers (In1 first = GND)."""
    if num_layers < 4:
        return []
    return _INNER_PLANE_LAYERS[:max(0, plane_layers)]


def _auto_drop_layer(pad_layer: str, num_layers: int, plane_layers: int) -> str:
    """Pick a routable SIGNAL layer to fan out on: never a plane, never the
    pad's own layer.  Prefer an inner signal layer (best shielding), else the
    opposite outer layer."""
    order = _LAYER_ORDER.get(num_layers, _LAYER_ORDER[2])
    planes = set(_plane_layer_names(num_layers, plane_layers))
    signal = [l for l in order if l not in planes and l != pad_layer]
    for pref in ("inner2", "inner1", "bottom", "top"):
        if pref in signal:
            return pref
    return signal[0] if signal else ("bottom" if pad_layer == "top" else "top")


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


def generate_escape_routing(
    placement: dict,
    netlist: dict,
    config: EscapeConfig | None = None,
    exclude_nets: tuple[str, ...] = (),
    pad_map: dict | None = None,
) -> dict:
    """Return {"traces": [...], "vias": [...]} — the full dog-bone breakout for
    the board's single-row/column fine-pitch parts.  Empty when there are none.

    Each escaping pin yields a pad→via stub, a through via, and (for signal
    nets) an onward fanout trace ending on a clean release line clear of the
    pad field.  Pins on a plane net (``exclude_nets`` — GND/power planes) get a
    stub + via that drops straight to the plane (no onward trace).  Output is in
    routed-schema form, ready for ``route_with_freerouting(..., fixed_routing)``.
    ``pad_map`` may be supplied to bypass footprint resolution (used in tests).
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

    def _is_plane_net(net_id: str | None) -> bool:
        return bool(net_id) and (net_id in exclude or
                                 net_names.get(net_id, net_id) in exclude)

    plane_names = _plane_layer_names(cfg.num_layers, cfg.plane_layers)
    gnd_plane_layer = plane_names[0] if plane_names else "bottom"

    # Group pads by part
    by_part: dict[str, list[PadInfo]] = {}
    for pad in pad_map.values():
        by_part.setdefault(pad.designator, []).append(pad)

    traces: list[dict] = []
    vias: list[dict] = []
    placed_via_centers: list[tuple[float, float]] = []  # collision across parts

    via_r = cfg.via_diameter_mm / 2.0
    via_clear = cfg.via_diameter_mm + cfg.clearance_mm  # min centre distance
    trace_half = cfg.trace_width_mm / 2.0

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
            edir = (0.0, 1.0 if bcy >= sum(ys) / len(ys) else -1.0)
            order = sorted(smd, key=lambda p: p.x_mm)
        elif span_x <= tol and span_y > tol:
            row_axis = "y"           # pads vary in y → escape along ±x
            edir = (1.0 if bcx >= sum(xs) / len(xs) else -1.0, 0.0)
            order = sorted(smd, key=lambda p: p.y_mm)
        else:
            continue                 # not a single row — leave to the autorouter

        leaving = _nets_leaving_part(netlist, des)

        # Escape-axis half-extent of the widest pad (the stub must clear the
        # pad edge + via body + clearance before the via lands).
        if edir[0] != 0.0:
            half_pad = max(p.pad_width_mm for p in smd) / 2.0
        else:
            half_pad = max(p.pad_height_mm for p in smd) / 2.0
        base = half_pad + via_r + cfg.clearance_mm + 0.05
        stagger = via_clear + 0.05         # second via row this much further out
        # Release line: past the far via row + its body + clearance, so onward
        # traces leave the field as a clean comfortable grid the router resumes.
        release = base + stagger + via_r + cfg.clearance_mm + trace_half \
            + cfg.onward_margin_mm

        drop_signal = cfg.drop_layer or _auto_drop_layer(
            order[0].layer, cfg.num_layers, cfg.plane_layers)

        part_vias: list[tuple[float, float]] = []
        for i, pad in enumerate(order):
            net_id = pad.net_id
            if not net_id or net_id not in leaving:
                continue
            is_plane = _is_plane_net(net_id)
            dist = base + (i % 2) * stagger
            vx = round(pad.x_mm + edir[0] * dist, 3)
            vy = round(pad.y_mm + edir[1] * dist, 3)

            # Collision guard: keep clear of every via already placed.
            clash = any(math.hypot(vx - ox, vy - oy) < via_clear - 1e-6
                        for ox, oy in placed_via_centers + part_vias)
            if clash:
                continue

            nm = net_names.get(net_id, net_id)
            # Stub: pad → via on the pad's own layer.
            traces.append({
                "start_x_mm": round(pad.x_mm, 3), "start_y_mm": round(pad.y_mm, 3),
                "end_x_mm": vx, "end_y_mm": vy,
                "width_mm": cfg.trace_width_mm, "layer": pad.layer,
                "net_id": net_id, "net_name": nm, "escape_role": "stub",
            })
            to_layer = gnd_plane_layer if is_plane else drop_signal
            vias.append({
                "x_mm": vx, "y_mm": vy,
                "drill_mm": cfg.via_drill_mm, "diameter_mm": cfg.via_diameter_mm,
                "from_layer": pad.layer, "to_layer": to_layer,
                "net_id": net_id, "net_name": nm,
            })
            # Signal nets: deterministic onward fanout to the release line on a
            # signal layer (plane nets are connected by the plane — no onward).
            if not is_plane:
                rx = round(pad.x_mm + edir[0] * release, 3)
                ry = round(pad.y_mm + edir[1] * release, 3)
                traces.append({
                    "start_x_mm": vx, "start_y_mm": vy,
                    "end_x_mm": rx, "end_y_mm": ry,
                    "width_mm": cfg.trace_width_mm, "layer": drop_signal,
                    "net_id": net_id, "net_name": nm, "escape_role": "fanout",
                })
            part_vias.append((vx, vy))
        placed_via_centers.extend(part_vias)

    return {"traces": traces, "vias": vias}
