# Spec: Fine-pitch escape router for dense connectors (morgan CN1)

**Status:** open / ready to implement. **Owner:** next session.
**Goal:** make a dense fine-pitch connector (the 30-pin 0.5 mm FFC `CN1` on the
morgan board) route DRC-clean by improving the escape/fanout pre-router.

This session must **modify the main repo and test on the Pi** (kicad-cli DRC is
only available there). Read `STANDARDS.md` / `ARCHITECTURE.md` first. Work on a
branch off `main` (current `HEAD = ff804cf`).

---

## 1. Problem & evidence

morgan (`100×50 mm`, 4-layer, `plane_layers=1`, 73 components) now passes through
the whole pipeline; DRC went **705 → 120 errors** after a long bug-fix pass
(commits `d8d3eb2 → ff804cf`, see `memory/project_morgan_bug_fixes.md`). The
**dominant remaining error is ~86 `shorting_items`, essentially all at `CN1`** —
the Hirose `FH35-30S-0.5SV_52` 30-pin **0.5 mm-pitch FFC**.

DRC reports them as `Track [net A] … Pad N [net B]` where the two items sit one
0.5 mm pitch apart in the pad column (x ≈ 2–3 mm, the FFC pad row). A trace
escaping one pin crosses the **adjacent pin's pad** — at 0.5 mm pitch with
0.127 mm trace + 0.127 mm clearance you **cannot** get more than one trace
between adjacent pads, so most inner pins cannot escape on-layer at all.

These are **genuine geometry shorts**, NOT a coordinate bug:
- Pad positions were verified consistent end-to-end: `build_pad_map` ==
  exported KiCad pads (0 mismatch after the rotation fix `3ad0df3`), and the DSN
  handed to Freerouting uses `(place x y side rot)` with FR rotating CCW =
  `build_pad_map`. So router model, DSN, and export all agree pad-for-pad.
- Therefore the fix must be in **how the pad field is broken out**, not in any
  transform.

## 2. Why Freerouting alone can't do it

Freerouting is a generic net-by-net rip-up router with no concept of fanning a
pad field out as a *group*. On a 0.5 mm field it leaves pins as stubs or routes
a trace straight across a neighbor's pad. The intended remedy already exists in
the repo (v1, below) but is **insufficient**.

## 3. Current state — the v1 escape router (must be improved)

- `optimizers/escape_router.py` — `generate_escape_routing(placement, netlist,
  EscapeConfig, exclude_nets, pad_map)` → `{"traces": [...], "vias": [...]}` of
  dog-bone escapes (pad → short stub → via that drops the signal to
  `drop_layer`), in routed-schema form. v1 handles **single-row** fine-pitch
  parts, escapes perpendicular to the row, staggers vias into two rows
  (`i % 2`). Candidate gate: `pitch < 0.8 mm` and `>= 10` SMD pins.
- Wired in `orchestrator/stages.run_routing` (search `escape_fanout`), opt-in
  via `config.escape_fanout` / env `PCB_ESCAPE_FANOUT=true`, only on a fresh
  route (`fixed_routing is None`). The escapes become Freerouting protected
  wiring (`fixed_routing`), so FR routes only from the breakout vias onward.
- Tests: `tests/test_escape_router.py`.

**Observed failure (the key data point):** routing morgan with
`PCB_ESCAPE_FANOUT=true` (the "v18" run) **reached 100% routed** but DRC did
**not** improve — it rose to ~195 (escapes add vias) and the CN1 shorts
remained ~90. So v1 generates escapes but they don't actually clear the
fine-pitch shorts. Diagnosing *why* is step 1.

## 4. Investigation (do this first, before redesigning)

On the Pi, with a fresh morgan project (recipe in §7), generate escapes and
inspect them:
1. How many of CN1's 30 pins get an escape? (v18 logged **24** — which 6 are
   skipped, and why? GND/non-leaving filtering in `_nets_leaving_part` +
   `exclude_nets`. Are signal pins being dropped?)
2. Do the **stub traces themselves** cross adjacent pads? For a pad column
   (row varies in y, escape in ±x) a horizontal stub at pad i's y shouldn't
   cross pad i±1 — verify against the actual geometry, including the **via**
   bodies (Ø0.45) vs adjacent pads/stubs.
3. After FR routes with the escapes protected, are the residual shorts on
   **escaped** pins (escape geometry is itself bad) or **non-escaped** pins (FR
   routing the leftover pins across the field)? Cross-reference the DRC short
   coordinates with the escape via/stub coordinates.
4. Does FR **honor** the protected escapes, or rip/re-route through the pad
   field anyway? (Check the SES output vs the protected `fixed_routing`.)

This tells you whether to fix the escape geometry, the pin-selection, or the
FR handoff.

## 5. Design directions (likely needed)

- **Escape *every* signal pin** of the dense part (currently some are skipped).
  Inner pins that can't reach the interior on-layer must drop to a via
  immediately adjacent to the pad (true dog-bone), alternating escape direction
  or via-row depth so vias clear at 0.5 mm pitch.
- **Drop to an inner signal layer**, not just `bottom`. morgan is
  `plane_layers=1` (In1=GND plane, In2=signal, B=signal); dropping FFC pins onto
  In2/B and fanning out there is the standard approach. Make `drop_layer`
  stackup-aware (don't drop onto a plane layer).
- **Two-sided / multi-depth via staggering** so 30 vias at 0.5 mm pitch don't
  violate via-to-via or via-to-pad clearance (Ø0.45 via + 0.127 clearance ⇒
  ≥0.577 mm center spacing — at 0.5 mm pitch you need ≥2 rows, possibly 3).
- Consider generating the **full breakout** (pad→via→short fanout trace to a
  comfortable-pitch grid) as protected wiring so FR starts from a clean grid,
  not just from the via.
- Keep it **opt-in** until validated, then consider auto-enabling when a part
  trips the fine-pitch threshold.

## 6. Success criteria

- morgan DRC **`shorting_items` at CN1 → 0** (or single digits), measured by
  `kicad-cli pcb drc --severity-error` on the exported `.kicad_pcb`.
- Overall morgan DRC **errors well below 120** (target: dominated only by any
  genuinely unroutable nets, ideally 0 shorts).
- No regression: the full suite (`pytest -q`, currently **406 passed, 1
  skipped**) stays green; add escape-router tests for the new behavior.
- Don't regress the other validated boards (arduino etc.) — escape fanout is
  gated on the fine-pitch threshold, so non-fine-pitch boards must be untouched.

## 7. Repro environment (Pi)

Projects live in `~/.pcb-creator/projects/` on host **`pi-claw`** (user `jclaw`,
repo `/home/jclaw/pcb-creator`, kicad-cli 9.0.8, Freerouting v2.1.0, pcbnew on
`/usr/bin/python3`). Footprint env: `PCB_KICAD_LIBRARY_PATH=/usr/share/kicad/footprints`
plus the project-local `custom-footprints.pretty/` (FH35, pc814, TI_SO-PowerPAD,
MountingHole — copy from `morgan_carrier_v7`).

Build a fresh morgan project through the **fixed** pipeline (re-classifies the
netlist so TB/HDR/SWD are connectors), place, route, export, DRC. The script
used this session (adapt the project name):

```python
# /tmp/test_morgan.py  — run with:
#   cd /home/jclaw/pcb-creator && PYTHONPATH=. \
#   PCB_KICAD_LIBRARY_PATH=/usr/share/kicad/footprints \
#   PCB_FREEROUTING_TIMEOUT=900 PCB_ESCAPE_FANOUT=true \
#   .venv/bin/python /tmp/test_morgan.py
import json, shutil, subprocess
from collections import Counter
from pathlib import Path
from orchestrator.config import OrchestratorConfig
from orchestrator import stages
from exporters.kicad_netlist_importer import _infer_component_type
import mcp_server
SRC = Path.home()/".pcb-creator/projects/morgan_carrier_v14/morgan_carrier_v14_netlist.json"
V7  = Path.home()/".pcb-creator/projects/morgan_carrier_v7/custom-footprints.pretty"
NAME="morgan_escape"; PDIR=Path.home()/".pcb-creator/projects"/NAME
if PDIR.exists(): shutil.rmtree(PDIR)
PDIR.mkdir(parents=True); shutil.copytree(V7, PDIR/"custom-footprints.pretty")
nl=json.loads(SRC.read_text())
for e in nl["elements"]:
    if e.get("element_type")=="component":
        e["component_type"]=_infer_component_type(e.get("designator",""), e.get("package",""))
(PDIR/f"{NAME}_netlist.json").write_text(json.dumps(nl,indent=2))
mcp_server._init_lookup(); mcp_server._activate_project_lookup(NAME)
cfg=OrchestratorConfig.from_env()
stages.run_placement(PDIR,NAME,cfg,board_width_mm=100.0,board_height_mm=50.0,layers=4,plane_layers=1)
stages.run_routing(PDIR,NAME,cfg,effort="best",max_seconds=900,log=print)
from exporters.kicad_exporter import export_kicad_pcb
routed=json.loads((PDIR/f"{NAME}_routed.json").read_text())
pcb=PDIR/f"{NAME}.kicad_pcb"; export_kicad_pcb(routed,nl,pcb)  # also pours zones via pcbnew
out=PDIR/"drc.json"
subprocess.run(["kicad-cli","pcb","drc","--format","json","--output",str(out),
                "--severity-error",str(pcb)],timeout=300)
d=json.loads(out.read_text())
print("ERRORS:",len(d["violations"]),"unconnected:",len(d.get("unconnected_items",[])))
print(dict(Counter(x.get("type") for x in d["violations"])))
```

To inspect escapes directly: call `generate_escape_routing(placement, netlist,
EscapeConfig(...))` and print the traces/vias; compare their coords to the DRC
short coords (each short item carries a `pos`).

A full route is ~5–15 min (Freerouting). Launch with `nohup … &` and poll the
log; do not block a single command on it.

## 8. Deploy / workflow

Mac repo `/Users/James/ai-sandbox/Productizr/pcb-creator` is canonical. The Pi
tracks `origin/main` via a deploy key. Standard loop: commit on Mac → `git push
origin main` → on Pi `git fetch && git reset --hard origin/main` → restart the
MCP server (`pkill -f pcb-creator-mcp`; it re-spawns on next session). The Pi's
MCP is an **editable** install, so deployed files are picked up by the next
spawn. (During iteration you can `scp` just the changed file to the Pi to test
before committing.) End commit messages with the project's Co-Authored-By line.

## 9. Files

- `optimizers/escape_router.py` — the generator (primary work).
- `orchestrator/stages.py` — integration (`escape_fanout` block in
  `run_routing`); may need stackup-aware `drop_layer`, auto-enable logic.
- `optimizers/pad_geometry.py` — `build_pad_map`, `_rotate_offset` (read-only
  reference; the pad model everything agrees on — do not change conventions).
- `exporters/dsn_exporter.py` — how pads/keepouts reach Freerouting (read to
  confirm FR respects the protected escapes and pad keepouts).
- `tests/test_escape_router.py` — extend.

## 10. Constraints / gotchas

- **Do not change pad-position conventions.** `build_pad_map` (CCW
  `_rotate_offset`), the DSN `(place … rot)`, and the negated-angle KiCad export
  are now mutually consistent (that consistency was the hard-won fix `3ad0df3`).
  Escape geometry must be produced in the same `build_pad_map` frame.
- Escapes feed Freerouting as `fixed_routing` / `(type protect)`. Confirm FR
  honors them; if it rips them up, the handoff (not the geometry) is the bug.
- `via_diameter 0.45 / drill 0.2` for escapes vs `0.6/0.3` for normal vias —
  keep escape vias small enough to fit the pitch but manufacturable.
- morgan placement is now reliable via seed-retry (`seed=None`); an explicit
  seed is reproducible. Use `seed=None` for repros unless you need determinism.
- The morgan FFC may simply need 3 via-depth rows or an inner-layer drop; if
  after a solid effort the 0.5 mm FFC still can't be made fully clean, document
  the residual honestly rather than forcing it.
