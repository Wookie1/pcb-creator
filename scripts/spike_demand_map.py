#!/usr/bin/env python3
"""Spike: validate enhancement B (routing-demand / RUDY congestion).

Runs run_placement on morgan (and a sparse board) with demand_weight off vs on,
reporting wall time and the demand-grid hotspot (max cell demand vs capacity) so
we can confirm: (a) the term activates on a dense board, (b) it is a no-op on a
sparse one, (c) runtime stays acceptable. Pure Python — no Freerouting.
"""
import sys, time, json, tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from orchestrator.config import OrchestratorConfig
from orchestrator import stages
from optimizers.pad_geometry import configure_lookup
from orchestrator.cache import ComponentCache
import optimizers.placement_optimizer as po
from optimizers.ratsnest import build_connectivity, _PLANE_NET_CLASSES

# Reuse the reroute script's KiCad parsers / netlist builder.
import importlib.util
_spec = importlib.util.spec_from_file_location(
    "rr", str(REPO / "scripts" / "reroute_kicad_pcb.py"))
rr = importlib.util.module_from_spec(_spec); _spec.loader.exec_module(rr)


def _demand_stats(placement_path, netlist_path, track_pitch):
    placement = json.loads(Path(placement_path).read_text())
    netlist = json.loads(Path(netlist_path).read_text())
    pos = {p["designator"]: (p["x_mm"], p["y_mm"]) for p in placement["placements"]}
    nets = [n for n in build_connectivity(netlist)
            if n.net_class not in _PLANE_NET_CLASSES]
    cell = po.DEMAND_CELL_MM
    grid = {}
    for net in nets:
        pts = [pos[d] for d in net.designators if d in pos]
        if len(pts) < 2:
            continue
        xs = [p[0] for p in pts]; ys = [p[1] for p in pts]
        w, h = max(xs) - min(xs), max(ys) - min(ys)
        area = max(w * h, cell * cell)
        contrib = (w + h) / area * cell * cell
        for ci in range(int(min(xs)//cell), int(max(xs)//cell) + 1):
            for cj in range(int(min(ys)//cell), int(max(ys)//cell) + 1):
                grid[(ci, cj)] = grid.get((ci, cj), 0.0) + contrib
    cap = ((cell / max(track_pitch, 0.1)) * cell * po.DEMAND_SIGNAL_LAYERS
           * po.DEMAND_UTILIZATION_LIMIT)
    vals = list(grid.values())
    peak = max(vals, default=0.0)
    hot = sum(1 for v in vals if v > cap)
    return peak, cap, hot, len(nets)


def run_board(pcb_path, plane_layers=1):
    cfg = OrchestratorConfig.from_env(base_dir=REPO)
    cfg.router_engine = "freerouting"
    tmpcache = Path(tempfile.mkdtemp(prefix="dm-cache-"))
    configure_lookup(kicad_index=None, cache=ComponentCache(str(tmpcache / "c.json")))
    board, comps, placements = rr.parse_pcb(pcb_path)
    board["plane_layers"] = plane_layers
    netlist = rr.build_netlist("dm", comps)
    track_pitch = 0.127 + 0.127  # jlcpcb fine-pitch (morgan)

    for dw in (0.0, 40.0):
        tmp = Path(tempfile.mkdtemp(prefix="dm-")); pdir = tmp / "dm"; pdir.mkdir(parents=True)
        (pdir / "dm_netlist.json").write_text(json.dumps(netlist))
        (pdir / "dm_requirements.json").write_text(json.dumps(
            {"board": board, "manufacturing": {"manufacturer": "jlcpcb_4layer"}}))
        cfg2 = OrchestratorConfig.from_env(base_dir=REPO)
        cfg2.router_engine = "freerouting"
        old = po.SAConfig.demand_weight
        po.SAConfig.demand_weight = dw
        t = time.monotonic()
        res = stages.run_placement(pdir, "dm", cfg2,
                                   board_width_mm=board["width_mm"],
                                   board_height_mm=board["height_mm"],
                                   plane_layers=plane_layers, seed=1)
        dt = time.monotonic() - t
        po.SAConfig.demand_weight = old
        if not res.get("success"):
            print(f"  demand_weight={dw}: PLACE FAILED: {res.get('error')}")
            continue
        peak, cap, hot, nn = _demand_stats(pdir / "dm_placement.json",
                                           pdir / "dm_netlist.json", track_pitch)
        print(f"  demand_weight={dw:<4} wire={res['wire_length_mm']:.0f}mm "
              f"cross={res['crossings']:<4} time={dt:.1f}s | cell={po.DEMAND_CELL_MM}mm "
              f"peak={peak:.0f} cap={cap:.0f} util={peak/cap:.2f} hot_cells={hot}")


if __name__ == "__main__":
    pcb = sys.argv[1] if len(sys.argv) > 1 else "morgan_carrier_v11.kicad_pcb"
    print(f"=== {pcb} (demand-map B spike) ===")
    run_board(pcb)
