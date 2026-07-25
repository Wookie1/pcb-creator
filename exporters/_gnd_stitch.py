"""Stitch isolated GND copper-pour regions to the GND plane (B5, export layer).

Runs under KiCad's bundled python (pcbnew), invoked as:

    <kicad-python> exporters/_gnd_stitch.py <board.kicad_pcb> [min_dia_mm] [min_drill_mm]

The minima are the board's own design-rule floor (kicad_exporter.board_via_minima,
written into the .kicad_pro). A via smaller than that violates the rules the board
was exported with, so it is NEVER emitted: if no legal via fits an island, the
island is left unstitched and counted in "skipped" rather than shipping an
unmanufacturable hole.

pcb-creator's grid fill model is not KiCad's poured geometry, so the in-core
rescue (router._add_rescue_vias) can leave a GND outer-pour fragment that KiCad's
actual pour reports unconnected to the plane (kicad-cli: "Zone [GND] / Zone [GND]").
This pass works on the AUTHORITATIVE poured geometry: it pours the board, finds GND
filled regions with no through-connection to the plane, drops a GND through-via at a
clear interior point of each (KiCad re-pours the inner power-plane antipad around it,
so no short), re-pours, and saves.

Prints one JSON line: {"added": [{x_mm, y_mm, diameter_mm, drill_mm}, ...],
"skipped": n}. The caller harvests "added" back into routed.json so the vias
reach the GERBERS too — what ships must be what passed DRC.

Self-contained / no project imports, so it runs under the KiCad interpreter.
"""
import json
import math
import sys

import pcbnew

VIA_TRIES = ((0.6, 0.3, 0.45), (0.45, 0.2, 0.30))  # (dia, drill, inset) mm
CLEAR_MM = 0.2          # keepout from foreign copper
HOLE_CLEAR_MM = 0.25    # hole-to-hole keepout
MAX_ROUNDS = 4


def _obstacles(board, gnd):
    """Non-GND pads/track-segs/via-centres + all drilled holes, in mm."""
    TOMM = pcbnew.ToMM
    pads, segs, holes = [], [], []
    for fp in board.GetFootprints():
        for p in fp.Pads():
            c = p.GetCenter()
            r = TOMM(max(p.GetSize().x, p.GetSize().y)) / 2
            pads.append((TOMM(c.x), TOMM(c.y), r, p.GetNetCode()))
            if p.GetDrillSize().x > 0:
                holes.append((TOMM(c.x), TOMM(c.y), TOMM(p.GetDrillSize().x) / 2))
    for t in board.GetTracks():
        if t.Type() == pcbnew.PCB_VIA_T:
            c = t.GetPosition()
            holes.append((TOMM(c.x), TOMM(c.y), TOMM(t.GetDrill()) / 2))
            # Copper obstacle is the via's annular ring (width), not its drill.
            pads.append((TOMM(c.x), TOMM(c.y), TOMM(t.GetWidth()) / 2, t.GetNetCode()))
        else:
            s, e = t.GetStart(), t.GetEnd()
            segs.append((TOMM(s.x), TOMM(s.y), TOMM(e.x), TOMM(e.y),
                         TOMM(t.GetWidth()) / 2, t.GetNetCode()))
    return pads, segs, holes


def _seg_dist(px, py, x1, y1, x2, y2):
    dx, dy = x2 - x1, y2 - y1
    L2 = dx * dx + dy * dy
    t = 0.0 if L2 == 0 else max(0.0, min(1.0, ((px - x1) * dx + (py - y1) * dy) / L2))
    return math.hypot(px - (x1 + t * dx), py - (y1 + t * dy))


def _clear(px, py, r, drill, pads, segs, holes, gnd):
    for x, y, pr, nc in pads:
        if nc == gnd:
            continue
        if math.hypot(px - x, py - y) < r + pr + CLEAR_MM:
            return False
    for x1, y1, x2, y2, hw, nc in segs:
        if nc == gnd:
            continue
        if _seg_dist(px, py, x1, y1, x2, y2) < r + hw + CLEAR_MM:
            return False
    for x, y, hr in holes:
        if math.hypot(px - x, py - y) < drill / 2 + hr + HOLE_CLEAR_MM:
            return False
    return True


def _through_points(board, gnd):
    """Centres that tie copper to the inner GND plane: GND through-vias + PTH GND pads."""
    pts = []
    for t in board.GetTracks():
        if (t.Type() == pcbnew.PCB_VIA_T and t.GetNetCode() == gnd
                and t.GetViaType() == pcbnew.VIATYPE_THROUGH):
            pts.append(t.GetPosition())
    for fp in board.GetFootprints():
        for p in fp.Pads():
            if p.GetNetCode() == gnd and p.GetDrillSize().x > 0:
                pts.append(p.GetPosition())
    return pts


def _interior(contains, bb, dia, drill, inset, pads, segs, holes, gnd):
    """First scan point inside the region (hole-aware `contains` predicate)
    with `inset` copper margin on 4 sides and clear of all obstacles."""
    FROM, TOMM = pcbnew.FromMM, pcbnew.ToMM
    r = dia / 2
    step = FROM(0.3)
    ci = FROM(inset)
    yy = bb.GetY()
    while yy <= bb.GetBottom():
        xx = bb.GetX()
        while xx <= bb.GetRight():
            p = pcbnew.VECTOR2I(int(xx), int(yy))
            if contains(p) and all(
                contains(pcbnew.VECTOR2I(int(xx + dx), int(yy + dy)))
                for dx, dy in ((ci, 0), (-ci, 0), (0, ci), (0, -ci))
            ) and _clear(TOMM(int(xx)), TOMM(int(yy)), r, drill, pads, segs, holes, gnd):
                return p
            xx += step
        yy += step
    return None


def main(path, min_dia=0.6, min_drill=0.3):
    b = pcbnew.LoadBoard(path)
    gnet = b.FindNet("GND")
    if gnet is None:
        print(json.dumps({"added": [], "skipped": 0}))
        return
    gnd = gnet.GetNetCode()
    FROM = pcbnew.FromMM
    # Never emit a via below the board's own rule floor (see module docstring).
    tries = [t for t in VIA_TRIES
             if t[0] >= min_dia - 1e-9 and t[1] >= min_drill - 1e-9]
    if not tries:  # board demands a via larger than any preset — use the floor
        tries = [(min_dia, min_drill, min_dia * 0.75)]
    added_vias = []
    skipped = 0
    for _ in range(MAX_ROUNDS):
        pcbnew.ZONE_FILLER(b).Fill(b.Zones())
        tp = _through_points(b, gnd)
        pads, segs, holes = _obstacles(b, gnd)
        new_pts = []
        skipped = 0  # recomputed each round: the last round's value is current
        for z in b.Zones():
            if z.GetNetCode() != gnd:
                continue
            sp = z.GetFilledPolysList(z.GetLayer())
            for i in range(sp.OutlineCount()):
                # Hole-aware containment (Contains with the subpolygon index):
                # testing only the outline would count a via sitting in a
                # clearance void as "on this island" and skip a real island,
                # or pick a scan point inside a void where the via touches no
                # island copper.
                def _in_region(pt, _sp=sp, _i=i):
                    return _sp.Contains(pt, _i)
                if any(_in_region(pt) for pt in tp):
                    continue  # already tied to the plane
                bb = sp.Outline(i).BBox()
                for dia, drl, inset in tries:
                    ip = _interior(_in_region, bb, dia, drl, inset,
                                   pads, segs, holes, gnd)
                    if ip is not None:
                        new_pts.append((ip, dia, drl))
                        # Same-round mutual drill clearance: later islands this
                        # round must respect the via just chosen (two identical
                        # F.Cu/B.Cu islands would otherwise pick coincident
                        # points -> duplicate drill hit).
                        TOMM = pcbnew.ToMM
                        holes.append((TOMM(ip.x), TOMM(ip.y), drl / 2))
                        break
                else:
                    # No rule-legal via fits this island. Leave it — an honest
                    # unstitched island beats an unmanufacturable hole.
                    skipped += 1
        if not new_pts:
            break
        for ip, dia, drl in new_pts:
            v = pcbnew.PCB_VIA(b)
            v.SetPosition(ip)
            v.SetDrill(FROM(drl))
            v.SetWidth(FROM(dia))
            v.SetNetCode(gnd)
            v.SetViaType(pcbnew.VIATYPE_THROUGH)
            v.SetLayerPair(pcbnew.F_Cu, pcbnew.B_Cu)
            b.Add(v)
            # Report in the SAME coordinate space routed.json uses: _vias()
            # writes via (at x y) straight through with no Y flip, so board mm
            # here map 1:1 back into routed.json.
            added_vias.append({
                "x_mm": round(pcbnew.ToMM(ip.x), 3),
                "y_mm": round(pcbnew.ToMM(ip.y), 3),
                "diameter_mm": dia, "drill_mm": drl,
            })
            # keep the new via out of the obstacle/through sets handled by re-loop
    if added_vias:
        pcbnew.ZONE_FILLER(b).Fill(b.Zones())
        pcbnew.SaveBoard(path, b)
    print(json.dumps({"added": added_vias, "skipped": skipped}))


if __name__ == "__main__":
    main(sys.argv[1],
         float(sys.argv[2]) if len(sys.argv) > 2 else 0.6,
         float(sys.argv[3]) if len(sys.argv) > 3 else 0.3)
