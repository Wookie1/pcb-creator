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

Geometry (per pad row — a single-row/column part like an FPC or edge connector
is one row; a quad pack (LQFP/TQFP/QFN) is decomposed into its four sides and
each is fanned out the same way, outward from the part centre):

  pad ──stub──▶ via ──onward──▶ release line
                (2 staggered via rows so the Ø-via field clears at pad pitch)

Every escaping pin is broken out — including pins on a plane net (e.g. GND),
which drop straight to their plane layer with a via (no onward trace; the plane
makes the connection).  The near via-row's onward traces thread the gaps
*between* the far via-row deterministically (≈0.2 mm clearance), which the
generic autorouter could not reliably do on its own.  Onward traces drop to a
stackup-aware signal layer (an inner signal layer when one exists, never a
plane).
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
    edge_clearance_mm: float = 0.5   # copper-to-board-edge (fab minimum)
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


_SIDE_EDIR = {"L": (-1.0, 0.0), "R": (1.0, 0.0),
              "B": (0.0, -1.0), "T": (0.0, 1.0)}


def _side_groups(smd: list[PadInfo], pitch: float, bcx: float,
                 bcy: float) -> list[tuple[list[PadInfo], tuple[float, float]]]:
    """Split a part's pads into escapable rows: ``[(ordered_pads, edir), ...]``.

    A single row/column yields one group escaping toward the BOARD centre (right
    for a connector sitting on a board edge).  A quad pack — pads hugging all
    FOUR edges of their bounding box — yields one group per side, each escaping
    outward from the PART centre.  Pads near no edge (a centre thermal pad)
    belong to no side.  Anything else (a dual-row connector, a staggered field)
    is left to the autorouter, as before.
    """
    xs = [p.x_mm for p in smd]
    ys = [p.y_mm for p in smd]
    span_x, span_y = max(xs) - min(xs), max(ys) - min(ys)
    tol = pitch * 0.5
    if span_y <= tol and span_x > tol:
        return [(sorted(smd, key=lambda p: p.x_mm),
                 (0.0, 1.0 if bcy >= sum(ys) / len(ys) else -1.0))]
    if span_x <= tol and span_y > tol:
        return [(sorted(smd, key=lambda p: p.y_mm),
                 (1.0 if bcx >= sum(xs) / len(xs) else -1.0, 0.0))]

    minx, maxx, miny, maxy = min(xs), max(xs), min(ys), max(ys)
    groups: dict[str, list[PadInfo]] = {"L": [], "R": [], "B": [], "T": []}
    for p in smd:
        d = {"L": p.x_mm - minx, "R": maxx - p.x_mm,
             "B": p.y_mm - miny, "T": maxy - p.y_mm}
        near = min(d.values())
        if near > pitch:
            continue                       # centre / thermal pad — no side
        cands = [s for s, v in d.items() if v <= near + tol]
        if len(cands) > 1:
            # A pad's long axis points outward from its own side, so a corner
            # pad matching two edges belongs to the one its length agrees with.
            pref = ("L", "R") if p.pad_width_mm >= p.pad_height_mm else ("B", "T")
            cands = [s for s in cands if s in pref] or cands
        groups[min(cands, key=lambda s: d[s])].append(p)

    # A real side is a run of pads, not the two end pads of a dual-row connector
    # that happen to hug the left/right edge — so require all four to be present
    # before treating the part as a quad pack at all.
    sides = {s: g for s, g in groups.items() if len(g) >= 3}
    if len(sides) < 4:
        return []
    out = []
    for side, g in sides.items():
        key = (lambda p: p.y_mm) if side in ("L", "R") else (lambda p: p.x_mm)
        out.append((sorted(g, key=key), _SIDE_EDIR[side]))
    return out


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

    # Group pads by part
    by_part: dict[str, list[PadInfo]] = {}
    for pad in pad_map.values():
        by_part.setdefault(pad.designator, []).append(pad)

    traces: list[dict] = []
    vias: list[dict] = []
    keepouts: list[dict] = []
    placed_via_centers: list[tuple[float, float, str]] = []  # (x,y,net) across parts
    # Foreign-net stub/fanout traces already placed, so a later escape via keeps
    # clear of them (not just of other vias). Each: (sx, sy, ex, ey, net_id, halfw).
    placed_traces: list[tuple] = []

    via_r = cfg.via_diameter_mm / 2.0
    via_clear = cfg.via_diameter_mm + cfg.clearance_mm  # min centre distance
    trace_half = cfg.trace_width_mm / 2.0

    def _pt_seg_dist(px, py, ax, ay, bx, by):
        dx, dy = bx - ax, by - ay
        L2 = dx * dx + dy * dy
        u = 0.0 if L2 == 0 else max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / L2))
        return math.hypot(px - (ax + u * dx), py - (ay + u * dy))

    def _via_clears_foreign_traces(vx, vy, net_id):
        for sx, sy, ex, ey, tn, thw in placed_traces:
            if tn == net_id:
                continue
            if _pt_seg_dist(vx, vy, sx, sy, ex, ey) < via_r + thw + cfg.clearance_mm - 1e-6:
                return False
        return True

    def _overshoots(x: float, y: float, edir: tuple[float, float],
                    half: float) -> bool:
        """True when an escape's copper would sit within edge clearance of the
        board edge it heads for (or off the board entirely).  ``half`` is the
        copper's half-extent (via radius or trace half-width); it must clear the
        edge by ``edge_clearance_mm``.  Only the escape axis is checked — the
        pad's own position on the other axis is a given, and clamping it would
        reject a pad legitimately near a side edge."""
        margin = half + cfg.edge_clearance_mm
        if edir[0] > 0:
            return x > bw - margin
        if edir[0] < 0:
            return x < margin
        if edir[1] > 0:
            return y > bh - margin
        return y < margin

    # Foreign-component pad rects an escape via/stub must clear (not the
    # escaping part's own pads — those are what it fans out FROM). A dense quad
    # pack escapes into its neighbours; a lone connector never did, so the
    # single-row path never needed this.
    pad_rects: list[tuple[float, float, float, float, str]] = []  # cx,cy,hw,hh,net
    for p in pad_map.values():
        if p.layer in ("top", "bottom"):
            pad_rects.append((p.x_mm, p.y_mm, p.pad_width_mm / 2.0,
                              p.pad_height_mm / 2.0, p.net_id or ""))

    def _pt_rect_gap(px, py, cx, cy, hw, hh):
        return math.hypot(max(abs(px - cx) - hw, 0.0),
                          max(abs(py - cy) - hh, 0.0))

    def _clears_foreign_pads(px, py, net_id, des_pads, extra):
        """`px,py` (a via or a stub point) must clear every pad that is neither
        the escaping part's own nor on the same net."""
        for cx, cy, hw, hh, pn in pad_rects:
            if (cx, cy) in des_pads or pn == net_id:
                continue
            if _pt_rect_gap(px, py, cx, cy, hw, hh) < extra + cfg.clearance_mm - 1e-6:
                return False
        return True

    def _escape_row(order: list[PadInfo], edir: tuple[float, float],
                    leaving: set[str], des_pads: set[tuple[float, float]],
                    part_vias: list[tuple[float, float, str]]) -> None:
        """Fan one pad row out along `edir`, appending to the enclosing lists."""
        # Escape-axis half-extent of the widest pad (the stub must clear the
        # pad edge + via body + clearance before the via lands).
        if edir[0] != 0.0:
            half_pad = max(p.pad_width_mm for p in order) / 2.0
        else:
            half_pad = max(p.pad_height_mm for p in order) / 2.0
        base = half_pad + via_r + cfg.clearance_mm + 0.05
        stagger = via_clear + 0.05         # second via row this much further out
        # Release line: past the far via row + its body + clearance, so onward
        # traces leave the field as a clean comfortable grid the router resumes.
        release = base + stagger + via_r + cfg.clearance_mm + trace_half \
            + cfg.onward_margin_mm

        drop_signal = cfg.drop_layer or _auto_drop_layer(
            order[0].layer, cfg.num_layers, cfg.plane_layers)

        for i, pad in enumerate(order):
            net_id = pad.net_id
            if not net_id or net_id not in leaving:
                continue
            is_plane = _is_plane_net(net_id)
            dist = base + (i % 2) * stagger
            vx = round(pad.x_mm + edir[0] * dist, 3)
            vy = round(pad.y_mm + edir[1] * dist, 3)
            rx = round(pad.x_mm + edir[0] * release, 3)
            ry = round(pad.y_mm + edir[1] * release, 3)

            # The escape must land on the board: a part packed into a corner has
            # no room on its outward sides, and an off-board via is worse than
            # leaving the pin to the autorouter.
            if _overshoots(vx, vy, edir, via_r + cfg.clearance_mm):
                continue
            if not is_plane and _overshoots(rx, ry, edir, trace_half):
                continue

            # Collision guard, three checks — skip this pad's escape if any fails
            # (an unescaped pad just falls to the autorouter; a plane-net pad is
            # still connected by the plane). These prevent the CN1 fine-pitch
            # clearance violation between two PROTECTED escapes that the short-
            # cleanup cannot move:
            #   1. via ↔ any other via (hole-to-hole / copper)
            #   2. via ↔ foreign-net stub/fanout trace
            #   3. this pad's STUB ↔ any foreign via (order-independent: catches a
            #      stub placed later that would sit too close to an earlier via)
            all_vias = placed_via_centers + part_vias
            via_via = any(math.hypot(vx - ox, vy - oy) < via_clear - 1e-6
                          for ox, oy, _ in all_vias)
            stub_clear = all(
                on == net_id or _pt_seg_dist(ox, oy, pad.x_mm, pad.y_mm, vx, vy)
                >= via_r + trace_half + cfg.clearance_mm - 1e-6
                for ox, oy, on in all_vias)
            if (via_via or not stub_clear
                    or not _via_clears_foreign_traces(vx, vy, net_id)
                    or not _clears_foreign_pads(vx, vy, net_id, des_pads, via_r)
                    or (not is_plane and not _clears_foreign_pads(
                        rx, ry, net_id, des_pads, trace_half))):
                continue

            nm = net_names.get(net_id, net_id)
            # Stub: pad → via on the pad's own layer.
            stub = {
                "start_x_mm": round(pad.x_mm, 3), "start_y_mm": round(pad.y_mm, 3),
                "end_x_mm": vx, "end_y_mm": vy,
                "width_mm": cfg.trace_width_mm, "layer": pad.layer,
                "net_id": net_id, "net_name": nm, "escape_role": "stub",
            }
            traces.append(stub)
            placed_traces.append((stub["start_x_mm"], stub["start_y_mm"],
                                  stub["end_x_mm"], stub["end_y_mm"],
                                  net_id, trace_half))
            # A plane-net escape is a full through-via: it passes every inner
            # plane and connects to whichever one carries its net (In1=GND,
            # In2=power — router.py:2046), antipad-cleared on the others. Dropping
            # to a single named plane layer instead only reaches In1, so a power
            # pin on the In2 plane would be left unconnected. Matches the power
            # stitch vias, which are likewise top→bottom.
            to_layer = ("bottom" if pad.layer == "top" else "top") \
                if is_plane else drop_signal
            vias.append({
                "x_mm": vx, "y_mm": vy,
                "drill_mm": cfg.via_drill_mm, "diameter_mm": cfg.via_diameter_mm,
                "from_layer": pad.layer, "to_layer": to_layer,
                "net_id": net_id, "net_name": nm,
            })
            # Signal nets: deterministic onward fanout to the release line on a
            # signal layer (plane nets are connected by the plane — no onward).
            if not is_plane:
                traces.append({
                    "start_x_mm": vx, "start_y_mm": vy,
                    "end_x_mm": rx, "end_y_mm": ry,
                    "width_mm": cfg.trace_width_mm, "layer": drop_signal,
                    "net_id": net_id, "net_name": nm, "escape_role": "fanout",
                })
                placed_traces.append((vx, vy, rx, ry, net_id, trace_half))
            else:
                # A plane-net (GND) escape is excluded from the routing netlist,
                # so the autorouter cannot see its stub/via and will route other
                # nets straight over them. Emit keepout circles along the stub
                # and at the via so the router steers clear (the plane connects
                # the pin — no fanout to protect it).
                ko_d = cfg.via_diameter_mm + cfg.clearance_mm
                for fr in (0.5, 1.0):
                    keepouts.append({
                        "x_mm": round(pad.x_mm + edir[0] * dist * fr, 3),
                        "y_mm": round(pad.y_mm + edir[1] * dist * fr, 3),
                        "diameter_mm": round(ko_d, 3),
                    })
            part_vias.append((vx, vy, net_id))

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

        leaving = _nets_leaving_part(netlist, des)
        des_pads = {(p.x_mm, p.y_mm) for p in pads}
        # Sides share one via list so opposite sides keep clearing each other.
        part_vias: list[tuple[float, float, str]] = []   # (x, y, net_id)
        for side_pads, edir in _side_groups(smd, pitch, bcx, bcy):
            # Per-side pitch: across a quad pack the global minimum can fall
            # between two pads on DIFFERENT sides, which is not a pitch.
            side_pitch = _min_adjacent_pitch(side_pads)
            if side_pitch is None or side_pitch >= cfg.pitch_threshold_mm:
                continue
            _escape_row(side_pads, edir, leaving, des_pads, part_vias)
        placed_via_centers.extend(part_vias)

    return {"traces": traces, "vias": vias, "keepouts": keepouts}
