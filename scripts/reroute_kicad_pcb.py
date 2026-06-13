#!/usr/bin/env python3
"""Reconstruct a pcb-creator netlist + placement from an existing .kicad_pcb
(keeping the original component placement and footprint geometry) and re-route
it through the current pipeline. Reports routing completion + DRC, comparing
the new fine-pitch-aware rules against the old coarse-rule behaviour.

Usage: python scripts/reroute_kicad_pcb.py <board.kicad_pcb>
"""

import json
import re
import sys
import tempfile
import shutil
import math
from collections import Counter
from pathlib import Path

REPO = Path(__file__).parent.parent
sys.path.insert(0, str(REPO))

from orchestrator.config import OrchestratorConfig  # noqa: E402
from orchestrator import stages  # noqa: E402
from optimizers.pad_geometry import configure_lookup, get_default_cache  # noqa: E402
from orchestrator.cache import ComponentCache  # noqa: E402
from validators.net_classes import infer_net_class, infer_electrical_type  # noqa: E402

_REF_TYPE = [(re.compile(r"^C"), "capacitor"), (re.compile(r"^R"), "resistor"),
             (re.compile(r"^L"), "inductor"), (re.compile(r"^LED"), "led"),
             (re.compile(r"^D"), "diode"), (re.compile(r"^Q"), "transistor_npn"),
             (re.compile(r"^U"), "ic"), (re.compile(r"^Y|^X"), "crystal"),
             (re.compile(r"^SW"), "switch"), (re.compile(r"^H"), "connector"),
             (re.compile(r"^(J|CN|TB|HDR|SWD|P)"), "connector")]


def _ctype(ref):
    for rx, t in _REF_TYPE:
        if rx.match(ref):
            return t
    return "ic"


def parse_pcb(path):
    txt = Path(path).read_text()
    board = {"layers": len(set(re.findall(r'"(In[12])\.Cu"', txt))) and 4 or 2}
    # board size from Edge.Cuts max extent
    xs = [float(a) for a in re.findall(r'Edge\.Cuts[^\n]*', txt) for a in []]  # noop
    ec = re.findall(r'\(start ([0-9.-]+) ([0-9.-]+)\) \(end ([0-9.-]+) ([0-9.-]+)\)[^\n]*Edge\.Cuts', txt)
    coords = [float(v) for t in re.findall(r'\(gr_line \(start ([0-9.-]+) ([0-9.-]+)\) \(end ([0-9.-]+) ([0-9.-]+)\)[^)]*Edge', txt) for v in t]
    if not coords:
        coords = [float(v) for t in re.findall(r'\(start ([0-9.-]+) ([0-9.-]+)\)', txt) for v in t]
    board["width_mm"] = max((c for c in coords[::2]), default=100.0)
    board["height_mm"] = max((c for c in coords[1::2]), default=50.0)

    components, placements = [], []
    for blk in re.split(r'\n  \(footprint ', txt)[1:]:
        name = re.match(r'"([^"]+)"', blk).group(1)
        pkg = name.split(":", 1)[-1]
        ref = re.search(r'\(property "Reference" "([^"]+)"', blk)
        ref = ref.group(1) if ref else pkg
        flayer = re.search(r'\(layer "([^"]+)"\)', blk).group(1)
        layer = "bottom" if flayer.startswith("B") else "top"
        at = re.search(r'\(at ([0-9.-]+) ([0-9.-]+)(?: ([0-9.-]+))?\)', blk)
        fx, fy, frot = float(at.group(1)), float(at.group(2)), int(float(at.group(3) or 0))
        cid = "comp_" + re.sub(r'[^a-z0-9]', '_', ref.lower())

        pin_offsets, sizes, ports = {}, [], []
        for pm in re.finditer(
                r'\(pad "([^"]*)" \S+ \S+ \(at ([0-9.-]+) ([0-9.-]+)(?: [0-9.-]+)?\) \(size ([0-9.-]+) ([0-9.-]+)\)'
                r'(?:[^\n]*?\(net (\d+) "([^"]*)"\))?', blk):
            pname, px, py, w, h, netnum, netname = pm.groups()
            if not pname.isdigit():
                continue
            pin = int(pname)
            pin_offsets[pin] = [float(px), float(py)]
            sizes.append((float(w), float(h)))
            ports.append((pin, netname or ""))

        if not pin_offsets:
            continue
        # most common pad size for the single-pad-size FootprintDef
        pad_size = list(Counter(sizes).most_common(1)[0][0])
        xs2 = [o[0] for o in pin_offsets.values()]; ys2 = [o[1] for o in pin_offsets.values()]
        fw = (max(xs2) - min(xs2)) + pad_size[0] + 0.5
        fh = (max(ys2) - min(ys2)) + pad_size[1] + 0.5

        # cache geometry so get_footprint_def resolves this package
        get_default_cache().put_footprint(
            pkg, {str(k): v for k, v in pin_offsets.items()}, pad_size,
            source="reroute", needs_review=False)

        components.append({"_cid": cid, "ref": ref, "ctype": _ctype(ref),
                           "pkg": pkg, "ports": ports})
        placements.append({"designator": ref, "package": pkg,
                           "component_type": _ctype(ref),
                           "x_mm": fx, "y_mm": fy, "rotation_deg": frot,
                           "layer": layer,
                           "footprint_width_mm": round(fw, 2),
                           "footprint_height_mm": round(fh, 2)})
    return board, components, placements


def build_netlist(name, components):
    elements, nets = [], {}
    for c in components:
        elements.append({"element_type": "component", "component_id": c["_cid"],
                         "designator": c["ref"], "component_type": c["ctype"],
                         "value": "?", "package": c["pkg"]})
        for pin, netname in c["ports"]:
            ncls = infer_net_class(netname) if netname else "signal"
            et = (infer_electrical_type(ncls, c["ctype"]) if netname
                  else "no_connect")
            elements.append({"element_type": "port",
                             "port_id": f"port_{c['ref'].lower()}_{pin}",
                             "component_id": c["_cid"], "pin_number": pin,
                             "name": str(pin), "electrical_type": et})
            if netname:
                nets.setdefault(netname, []).append(f"port_{c['ref'].lower()}_{pin}")
    for netname, pids in nets.items():
        if len(pids) < 2:
            continue
        nid = "net_" + re.sub(r'[^a-z0-9]', '_', netname.lower()).strip("_")
        elements.append({"element_type": "net", "net_id": nid, "name": netname,
                         "connected_port_ids": pids,
                         "net_class": infer_net_class(netname)})
    return {"version": "1.0", "project_name": name, "elements": elements}


def run(pcb_path, fine_pitch_enabled):
    cfg = OrchestratorConfig.from_env(base_dir=REPO)
    cfg.router_engine = "freerouting"
    tmpcache = Path(tempfile.mkdtemp(prefix="rr-cache-"))
    configure_lookup(kicad_index=None, cache=ComponentCache(str(tmpcache / "c.json")))

    board, comps, placements = parse_pcb(pcb_path)
    name = "reroute"
    netlist = build_netlist(name, comps)

    tmp = Path(tempfile.mkdtemp(prefix="reroute-"))
    pdir = tmp / name; pdir.mkdir(parents=True)
    placement = {"version": "1.0", "project_name": name,
                 "board": {**board}, "placements": placements}
    (pdir / f"{name}_netlist.json").write_text(json.dumps(netlist))
    (pdir / f"{name}_placement.json").write_text(json.dumps(placement))
    (pdir / f"{name}_requirements.json").write_text(json.dumps(
        {"board": board, "manufacturing": {"manufacturer": "jlcpcb_4layer"}}))

    if not fine_pitch_enabled:
        stages.FINE_PITCH_THRESHOLD_MM = 0.0  # simulate old coarse-only behaviour
    else:
        import importlib; importlib.reload(stages)  # restore default threshold
        configure_lookup(kicad_index=None, cache=ComponentCache(str(tmpcache / "c.json")))

    sig_nets = sum(1 for e in netlist["elements"]
                   if e.get("element_type") == "net" and e.get("net_class") == "signal")
    print(f"  board {board['width_mm']}x{board['height_mm']}mm {board['layers']}-layer | "
          f"{len(placements)} parts | "
          f"{sum(1 for e in netlist['elements'] if e['element_type']=='net')} nets "
          f"({sig_nets} signal)")
    r = stages.run_routing(pdir, name, cfg, effort=globals().get("_EFFORT", "fast"))
    rep = stages.run_drc(pdir, name, cfg)
    st = rep.get("statistics", {})
    print(f"  -> completion {r.get('completion_pct')}%  valid={r.get('valid')}  "
          f"DRC errors={st.get('errors')} warnings={st.get('warnings')}  "
          f"unrouted={len(r.get('unrouted_nets', []))}")
    if r.get("unrouted_nets"):
        print(f"     unrouted: {r['unrouted_nets'][:12]}")
    shutil.rmtree(tmp, ignore_errors=True); shutil.rmtree(tmpcache, ignore_errors=True)
    return r


def finish(pcb_path, effort="best", max_seconds=2400, plane_layers=None):
    """INCREMENTAL: keep the .kicad_pcb's existing routing as protected wiring
    and route only the UNROUTED nets (finish the board instead of redoing it)."""
    cfg = OrchestratorConfig.from_env(base_dir=REPO)
    cfg.router_engine = "freerouting"
    tmpcache = Path(tempfile.mkdtemp(prefix="rr-cache-"))
    configure_lookup(kicad_index=None, cache=ComponentCache(str(tmpcache / "c.json")))

    board, comps, placements = parse_pcb(pcb_path)
    if plane_layers in (0, 1, 2):
        board["plane_layers"] = plane_layers
    netlist = build_netlist("inc", comps)
    placement = {"version": "1.0", "project_name": "inc",
                 "board": board, "placements": placements}

    # Import the existing routing (traces/vias) from the .kicad_pcb.
    from exporters.kicad_importer import import_kicad_pcb
    base = {"version": "1.0", "project_name": "inc", "board": board,
            "placements": placements, "routing": {"traces": [], "vias": []}}
    existing = import_kicad_pcb(pcb_path, base, netlist)
    er = existing.get("routing", {})
    fixed = {"traces": er.get("traces", []), "vias": er.get("vias", [])}
    print(f"  existing routing: {len(fixed['traces'])} traces, "
          f"{len(fixed['vias'])} vias (protected)", flush=True)

    tmp = Path(tempfile.mkdtemp(prefix="reroute-inc-"))
    pdir = tmp / "inc"; pdir.mkdir(parents=True)
    (pdir / "inc_netlist.json").write_text(json.dumps(netlist))
    (pdir / "inc_placement.json").write_text(json.dumps(placement))
    (pdir / "inc_requirements.json").write_text(json.dumps(
        {"board": board, "manufacturing": {"manufacturer": "jlcpcb_4layer"}}))

    r = stages.run_routing(pdir, "inc", cfg, effort=effort, max_seconds=max_seconds,
                           fixed_routing=fixed, log=lambda m: print("  [route]", m, flush=True))
    print(f"  -> completion {r.get('completion_pct')}%  unrouted={len(r.get('unrouted_nets', []))}",
          flush=True)
    if r.get("unrouted_nets"):
        print(f"     still unrouted: {r['unrouted_nets'][:12]}", flush=True)
    shutil.rmtree(tmp, ignore_errors=True); shutil.rmtree(tmpcache, ignore_errors=True)
    return r


def replace_and_route(pcb_path, effort="best", max_seconds=None,
                      plane_layers=1, two_sided=False):
    """FULL re-placement + feedback-retry routing — the only path that
    exercises escape halos (A) and localized re-place (C). Rebuilds the netlist
    from the board, SA-places from scratch (so the escape-halo term runs), then
    routes via run_route_with_retry (so an incomplete first route triggers a
    focused re-place around the unrouted components)."""
    cfg = OrchestratorConfig.from_env(base_dir=REPO)
    cfg.router_engine = "freerouting"
    tmpcache = Path(tempfile.mkdtemp(prefix="rr-cache-"))
    configure_lookup(kicad_index=None, cache=ComponentCache(str(tmpcache / "c.json")))

    board, comps, placements = parse_pcb(pcb_path)
    if plane_layers in (0, 1, 2):
        board["plane_layers"] = plane_layers
    netlist = build_netlist("rep", comps)

    tmp = Path(tempfile.mkdtemp(prefix="reroute-rep-"))
    pdir = tmp / "rep"; pdir.mkdir(parents=True)
    (pdir / "rep_netlist.json").write_text(json.dumps(netlist))
    (pdir / "rep_requirements.json").write_text(json.dumps(
        {"board": board, "manufacturing": {"manufacturer": "jlcpcb_4layer"}}))

    sig = sum(1 for e in netlist["elements"]
              if e.get("element_type") == "net" and e.get("net_class") == "signal")
    print(f"  board {board['width_mm']}x{board['height_mm']}mm {board['layers']}-layer "
          f"plane_layers={board.get('plane_layers')} | {len(placements)} parts | {sig} signal nets",
          flush=True)

    # From-scratch placement of a dense board can be marginal (a stray
    # overlap/overhang on an unlucky seed), so try a few fixed seeds until one
    # produces a feasible placement instead of failing on a random draw.
    place = None
    for seed in (1, 2, 3, 4, 5):
        place = stages.run_placement(
            pdir, "rep", cfg,
            board_width_mm=board["width_mm"], board_height_mm=board["height_mm"],
            two_sided=two_sided, plane_layers=plane_layers, seed=seed)
        if place.get("success"):
            print(f"  placement ok (seed {seed}): wire={place.get('wire_length_mm')}mm "
                  f"crossings={place.get('crossings')}", flush=True)
            break
        print(f"  placement seed {seed} infeasible: "
              f"{(place.get('violation_details') or ['?'])[0]}", flush=True)
    if not place.get("success"):
        print(f"  PLACEMENT FAILED on all seeds: {place.get('error')}", flush=True)
        shutil.rmtree(tmp, ignore_errors=True); shutil.rmtree(tmpcache, ignore_errors=True)
        return place

    r = stages.run_route_with_retry(
        pdir, "rep", cfg, effort=effort, max_seconds=max_seconds,
        log=lambda m: print("  [route]", m, flush=True))
    print(f"  -> completion {r.get('completion_pct')}%  retried={r.get('retried')}  "
          f"unrouted={len(r.get('unrouted_nets', []))}", flush=True)
    for a in r.get("attempts", []):
        print(f"     attempt: completion={a.get('completion_pct')}% "
              f"routed={a.get('routed_nets')}/{a.get('total_nets')}", flush=True)
    if r.get("unrouted_nets"):
        print(f"     still unrouted: {r['unrouted_nets'][:12]}", flush=True)
    shutil.rmtree(tmp, ignore_errors=True); shutil.rmtree(tmpcache, ignore_errors=True)
    return r


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    pcb = args[0] if args else "morgan_carrier_v11.kicad_pcb"
    effort = args[1] if len(args) > 1 else "fast"
    pl = 1 if "--plane1" in sys.argv else None
    if "--replace" in sys.argv:
        print(f"=== FULL re-place + feedback-retry route (tests A+C), effort={effort}"
              f"{', plane_layers=1' if pl else ''} ===")
        replace_and_route(pcb, effort=effort, plane_layers=(pl if pl is not None else 1),
                          two_sided="--two-sided" in sys.argv)
    elif "--incremental" in sys.argv:
        print(f"=== INCREMENTAL finish (keep existing routing), effort={effort}"
              f"{', plane_layers=1' if pl else ''} ===")
        finish(pcb, effort=effort, plane_layers=pl)
    else:
        print(f"=== OLD coarse rules (fine-pitch disabled), effort={effort} ===")
        run(pcb, fine_pitch_enabled=False)
        print(f"=== NEW fine-pitch-aware rules, effort={effort} ===")
        run(pcb, fine_pitch_enabled=True)
