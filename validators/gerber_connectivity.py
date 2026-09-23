"""Connectivity verification on the EXPORTED Gerber + Excellon files.

Every other check validates a model of the board: routed.json, or a KiCad file
that pcbnew re-pours with its own zone rules. This one reads the manufacturing
files themselves — renders each copper layer, links layers through the drill
hits, and compares the resulting copper groups against the netlist:

  * SHORT  — one connected copper group carries pads of two or more nets
  * OPEN   — one net's pads fall in two or more separate copper groups
  * TIED   — a no-net pad (NC pin) sits in copper that carries a net

It exists because three separate faults shipped "DRC clean" on one board
(parking_flasher_xor_20mm): trace-through-pad shorts, a surface pour flooding
plane-stitch vias, and inner planes that no via touched (a plane-only net left
OPEN). All three were visible in the gerbers and invisible to kicad-cli.

Renders only what gerber_exporter emits: %ADD circle/rect apertures, D01
strokes, D03 flashes, G36/G37 regions, %LPD/%LPC polarity. Pure numpy + PIL.
"""

from __future__ import annotations

import math
import re
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

_MAX_PIXELS = 20_000_000       # per layer; sets the resolution on large boards


class _Canvas:
    def __init__(self, w_mm: float, h_mm: float):
        self.res = max(0.01, math.sqrt(w_mm * h_mm / _MAX_PIXELS))
        self.w_mm, self.h_mm = w_mm, h_mm
        self.W = int(math.ceil(w_mm / self.res)) + 2
        self.H = int(math.ceil(h_mm / self.res)) + 2

    def px(self, x: float, y: float) -> tuple[float, float]:
        """Gerber mm (Y-up) -> image pixel (row 0 at max Y)."""
        return x / self.res, (self.h_mm - y) / self.res


def render_gerber(path: Path, cv: _Canvas) -> np.ndarray:
    """Render one Gerber file to a boolean copper mask."""
    img = Image.new("L", (cv.W, cv.H), 0)
    d = ImageDraw.Draw(img)
    aps: dict[int, tuple[str, float, float]] = {}
    cur, pol = None, 255
    x = y = 0.0
    in_region, contour, contours = False, [], []
    fmt_div = 1e6
    for raw in Path(path).read_text().replace("\n", "").split("*"):
        s = raw.strip().lstrip("%")
        if not s or s.startswith("G04"):
            continue
        m = re.match(r"FSLAX(\d)(\d)Y", s)
        if m:
            fmt_div = 10 ** int(m.group(2)); continue
        m = re.match(r"ADD(\d+)([CR]),([0-9.]+)(?:X([0-9.]+))?", s)
        if m:
            aps[int(m.group(1))] = (m.group(2), float(m.group(3)),
                                    float(m.group(4) or m.group(3)))
            continue
        if s.startswith("LPD"):
            pol = 255; continue
        if s.startswith("LPC"):
            pol = 0; continue
        if s == "G36":
            in_region, contour, contours = True, [], []; continue
        if s == "G37":
            if contour:
                contours.append(contour)
            for c in contours:
                if len(c) >= 3:
                    d.polygon([cv.px(*p) for p in c], fill=pol)
            in_region = False; continue
        m = re.fullmatch(r"D(\d+)", s)
        if m and int(m.group(1)) >= 10:
            cur = aps.get(int(m.group(1))); continue
        m = re.fullmatch(r"(?:G0?1)?(?:X(-?\d+))?(?:Y(-?\d+))?D0?([123])", s)
        if not m:
            continue
        nx = int(m.group(1)) / fmt_div if m.group(1) else x
        ny = int(m.group(2)) / fmt_div if m.group(2) else y
        op = m.group(3)
        if in_region:
            if op == "2":
                if contour:
                    contours.append(contour)
                contour = [(nx, ny)]
            else:
                contour.append((nx, ny))
        elif op == "1" and cur:
            wid = cur[1] / cv.res
            d.line([cv.px(x, y), cv.px(nx, ny)], fill=pol, width=max(1, round(wid)))
            for ex, ey in ((x, y), (nx, ny)):
                cx, cy = cv.px(ex, ey); r = wid / 2
                d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=pol)
        elif op == "3" and cur:
            cx, cy = cv.px(nx, ny)
            if cur[0] == "C":
                r = cur[1] / cv.res / 2
                d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=pol)
            else:
                hw, hh = cur[1] / cv.res / 2, cur[2] / cv.res / 2
                d.rectangle([cx - hw, cy - hh, cx + hw, cy + hh], fill=pol)
        x, y = nx, ny
    return np.asarray(img) > 127


def _label(mask: np.ndarray) -> np.ndarray:
    """Connected components (4-connectivity) by row runs + union-find."""
    parent: list[int] = []

    def find(a: int) -> int:
        while parent[a] != a:
            parent[a] = parent[parent[a]]; a = parent[a]
        return a

    rows = []
    for r in range(mask.shape[0]):
        dif = np.diff(np.concatenate(([0], mask[r].astype(np.int8), [0])))
        runs = []
        for s, e in zip(np.where(dif == 1)[0], np.where(dif == -1)[0]):
            rid = len(parent); parent.append(rid); runs.append((s, e, rid))
        if rows:
            prev, j = rows[-1], 0
            for s, e, rid in runs:
                while j < len(prev) and prev[j][1] <= s:
                    j += 1
                k = j
                while k < len(prev) and prev[k][0] < e:
                    a, b = find(rid), find(prev[k][2])
                    if a != b:
                        parent[a] = b
                    k += 1
        rows.append(runs)
    lab = np.zeros(mask.shape, np.int32)
    ids: dict[int, int] = {}
    for r, runs in enumerate(rows):
        for s, e, rid in runs:
            lab[r, s:e] = ids.setdefault(find(rid), len(ids) + 1)
    return lab


def _grow(mask: np.ndarray, k: int) -> np.ndarray:
    out = mask.copy()
    for dy in range(-k, k + 1):
        for dx in range(-k, k + 1):
            if dx * dx + dy * dy <= k * k:
                out |= np.roll(np.roll(mask, dy, 0), dx, 1)
    return out


def parse_drill(path: Path) -> list[tuple[float, float, float]]:
    """Excellon hits as (x_mm, y_mm, dia_mm). Decimal coordinates, or legacy
    integer METRIC,TZ (implied 3 decimals)."""
    tools, holes, tool = {}, [], None
    for line in Path(path).read_text().splitlines():
        line = line.strip()
        m = re.fullmatch(r"T(\d+)C([0-9.]+)", line)
        if m:
            tools[int(m.group(1))] = float(m.group(2)); continue
        m = re.fullmatch(r"T(\d+)", line)
        if m:
            tool = tools.get(int(m.group(1))); continue
        m = re.fullmatch(r"X(-?[0-9.]+)Y(-?[0-9.]+)", line)
        if m and tool:
            xs, ys = m.group(1), m.group(2)
            conv = (lambda v: float(v)) if "." in xs + ys else (lambda v: int(v) / 1000)
            holes.append((conv(xs), conv(ys), tool))
    return holes


_LAYER_ORDER = ["F_Cu"] + [f"In{i}_Cu" for i in range(1, 31)] + ["B_Cu"]


def verify_gerber_connectivity(output_dir: Path, project_name: str,
                               routed: dict, netlist: dict) -> dict:
    """Extract net connectivity from the exported copper + drill files and
    compare with the netlist. Returns a report with 'passed' and details."""
    from optimizers.pad_geometry import build_pad_map

    output_dir = Path(output_dir)
    board = routed.get("board", {})
    cv = _Canvas(board.get("width_mm", 50.0), board.get("height_mm", 50.0))
    layers = [L for L in _LAYER_ORDER
              if (output_dir / f"{project_name}-{L}.gbr").exists()]
    masks = {L: render_gerber(output_dir / f"{project_name}-{L}.gbr", cv) for L in layers}
    raw = {L: _label(masks[L]) for L in layers}
    # Opens are judged on copper grown by ~0.02mm so a spoke that merely TOUCHES
    # a pad can't be split by rasterisation rounding; shorts on the raw copper.
    g = max(1, round(0.02 / cv.res))
    grown = {L: _label(_grow(masks[L], g)) for L in layers}

    drill = output_dir / f"{project_name}.drl"
    holes = parse_drill(drill) if drill.exists() else []

    def at(lab, x, y):
        cx, cy = cv.px(x, y); c, r = int(round(cx)), int(round(cy))
        return int(lab[r, c]) if 0 <= r < cv.H and 0 <= c < cv.W else 0

    netname = {e["net_id"]: e.get("name", e["net_id"]) for e in netlist.get("elements", [])
               if e.get("element_type") == "net"}
    pads = list(build_pad_map(routed, netlist, include_netless=True).values())

    def groups(labs):
        parent: dict = {}

        def f(a):
            parent.setdefault(a, a)
            while parent[a] != a:
                parent[a] = parent[parent[a]]; a = parent[a]
            return a
        for hx, hy, _ in holes:
            hit = [(L, at(labs[L], hx, hy)) for L in layers if at(labs[L], hx, hy)]
            for a, b in zip(hit, hit[1:]):
                parent[f(a)] = f(b)
        node = {}
        for p in pads:
            L = "B_Cu" if p.layer == "bottom" else "F_Cu"
            if L not in labs:
                continue
            i = at(labs[L], p.x_mm, p.y_mm)
            node[f"{p.designator}.{p.pin_number}"] = f((L, i)) if i else None
        return node

    tag_net = {f"{p.designator}.{p.pin_number}": (netname.get(p.net_id) if p.net_id else None)
               for p in pads}
    raw_node, grown_node = groups(raw), groups(grown)

    no_copper = sorted(t for t, n in raw_node.items() if n is None)
    by_group: dict = {}
    for t, n in raw_node.items():
        if n is not None:
            by_group.setdefault(n, []).append(t)
    shorts, tied = [], []
    for members in by_group.values():
        nets = sorted({tag_net[t] for t in members if tag_net[t]})
        if len(nets) > 1:
            shorts.append(nets)
        if nets:
            tied += [t for t in members if not tag_net[t]]
    net_groups: dict = {}
    for t, n in grown_node.items():
        if tag_net[t] and n is not None:
            net_groups.setdefault(tag_net[t], {}).setdefault(n, []).append(t)
    opens = {net: sorted(sorted(v) for v in gs.values())
             for net, gs in net_groups.items() if len(gs) > 1}

    # Open vias inside SMD pads wick solder at reflow unless filled & capped —
    # not a connectivity fault, but the fab has to be told.
    via_in_pad = sorted({f"{p.designator}.{p.pin_number}"
                         for v in routed.get("routing", {}).get("vias", [])
                         for p in pads if p.layer in ("top", "bottom")
                         and abs(v["x_mm"] - p.x_mm) < p.pad_width_mm / 2
                         and abs(v["y_mm"] - p.y_mm) < p.pad_height_mm / 2})

    return {
        "passed": not (shorts or opens or tied or no_copper),
        "via_in_pad": via_in_pad,
        "resolution_mm": round(cv.res, 4),
        "layers": layers,
        "holes": len(holes),
        "shorts": sorted(shorts),
        "opens": opens,
        "tied_no_net_pads": sorted(tied),
        "pads_without_copper": no_copper,
    }


def summarize(report: dict) -> str:
    if report["passed"]:
        return "gerber connectivity verified: every net is one copper group, no shorts"
    parts = []
    for nets in report["shorts"]:
        parts.append("SHORT " + "+".join(nets))
    for net, gs in report["opens"].items():
        parts.append(f"OPEN {net} ({len(gs)} pieces: " +
                     "; ".join(",".join(g) for g in gs[:4]) + ")")
    if report["tied_no_net_pads"]:
        parts.append("no-net pad tied to copper: " + ", ".join(report["tied_no_net_pads"]))
    if report["pads_without_copper"]:
        parts.append("pad with no copper: " + ", ".join(report["pads_without_copper"]))
    return "; ".join(parts)
