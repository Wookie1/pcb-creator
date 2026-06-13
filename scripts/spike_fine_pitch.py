#!/usr/bin/env python3
"""Spike: routing a 0.5mm-pitch connector on a 4-layer board.

Reproduces the "traces route but violate clearance / short to adjacent pads"
symptom, and lets us compare the coarse default design rules against
fine-pitch-aware rules (DFM minimums). Run before/after the rule change.
"""

import json
import sys
import tempfile
import shutil
from pathlib import Path

REPO = Path(__file__).parent.parent
sys.path.insert(0, str(REPO))

from orchestrator.config import OrchestratorConfig  # noqa: E402
from orchestrator.stages import run_placement, run_routing, run_drc  # noqa: E402
from optimizers.pad_geometry import configure_lookup, get_default_cache  # noqa: E402
from orchestrator.cache import ComponentCache  # noqa: E402


def main() -> int:
    cfg = OrchestratorConfig.from_env(base_dir=REPO)
    tmpcache = Path(tempfile.mkdtemp(prefix="fp-cache-"))
    configure_lookup(kicad_index=None,
                     cache=ComponentCache(str(tmpcache / "c.json")))
    cfg.router_engine = "freerouting"

    # Cache a 0.5mm-pitch 1x16 row connector (like an FPC/board-to-board).
    cache = get_default_cache()
    pitch, n = 0.5, 16
    span = (n - 1) * pitch
    offsets = {str(i + 1): [round(-span / 2 + i * pitch, 4), 0.0]
               for i in range(n)}
    cache.put_footprint("FineConn_1x16_P0.5mm", offsets, [0.27, 1.2],
                        source="spike", needs_review=False)

    # Connector J1; 8 of its pins go to 8 resistors (forces escape routing),
    # the other 8 split power/ground.
    elements = [
        {"element_type": "component", "component_id": "comp_j1", "designator": "J1",
         "component_type": "connector", "value": "FPC16", "package": "FineConn_1x16_P0.5mm"},
    ]
    for i in range(1, 17):
        et = ("power_in" if i == 1 else "ground" if i == 2 else "signal")
        elements.append({"element_type": "port", "port_id": f"port_j1_{i}",
                         "component_id": "comp_j1", "pin_number": i,
                         "name": str(i), "electrical_type": et})
    nets = [("net_vcc", "VCC", "power", ["port_j1_1"]),
            ("net_gnd", "GND", "ground", ["port_j1_2"])]
    for k in range(8):  # signal pins 3..10 → R1..R8
        des = f"R{k + 1}"
        cid = f"comp_r{k + 1}"
        elements.append({"element_type": "component", "component_id": cid,
                         "designator": des, "component_type": "resistor",
                         "value": "10k", "package": "0402"})
        elements.append({"element_type": "port", "port_id": f"port_{des.lower()}_1",
                         "component_id": cid, "pin_number": 1, "name": "1",
                         "electrical_type": "passive"})
        elements.append({"element_type": "port", "port_id": f"port_{des.lower()}_2",
                         "component_id": cid, "pin_number": 2, "name": "2",
                         "electrical_type": "passive"})
        nets.append((f"net_s{k}", f"S{k}", "signal",
                     [f"port_j1_{3 + k}", f"port_{des.lower()}_1"]))
        nets[0][3].append(f"port_{des.lower()}_2")  # other R pin → VCC (loads)
    # remaining J1 pins 11..16 → GND
    for i in range(11, 17):
        nets[1][3].append(f"port_j1_{i}")
    for nid, name, ncls, pins in nets:
        elements.append({"element_type": "net", "net_id": nid, "name": name,
                         "connected_port_ids": pins, "net_class": ncls})
    netlist = {"version": "1.0", "project_name": "fp", "elements": elements}

    name = "fp"
    tmp = Path(tempfile.mkdtemp(prefix="finepitch-"))
    pdir = tmp / name
    pdir.mkdir(parents=True)
    (pdir / f"{name}_netlist.json").write_text(json.dumps(netlist))
    # 4-layer board with a JLCPCB manufacturing profile
    req = {"project_name": name, "board": {"width_mm": 30, "height_mm": 24, "layers": 4},
           "manufacturing": {"manufacturer": "jlcpcb_4layer"}}
    (pdir / f"{name}_requirements.json").write_text(json.dumps(req))

    r = run_placement(pdir, name, cfg, board_width_mm=30, board_height_mm=24, seed=7)
    print("placement:", r.get("success"), r.get("error", "")[:90])
    if not r.get("success"):
        shutil.rmtree(tmp, ignore_errors=True); shutil.rmtree(tmpcache, ignore_errors=True)
        return 1

    rr = run_routing(pdir, name, cfg, effort="normal", log=print)
    print(f"\nrouting: completion={rr.get('completion_pct')}% valid={rr.get('valid')}")

    rep = run_drc(pdir, name, cfg)
    st = rep.get("statistics", {})
    print(f"DRC: passed={rep.get('passed')} errors={st.get('errors')} warnings={st.get('warnings')}")
    by_rule = {}
    for c in rep.get("checks", []):
        e = sum(1 for v in c.get("violations", []) if v.get("severity") == "error")
        if e:
            by_rule[c["rule"]] = e
    for rule, cnt in by_rule.items():
        print(f"  {rule}: {cnt} errors")

    shutil.rmtree(tmp, ignore_errors=True)
    shutil.rmtree(tmpcache, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
