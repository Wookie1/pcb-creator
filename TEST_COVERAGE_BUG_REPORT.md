# pcb-creator — bug report for the test-coverage session

Found while compacting a real 4-layer board (the "Morgan G-100T carrier": 73 parts,
7× TO-220 THT, a Molex Micro-Fit, pin headers, and an FH35 30-pin 0.5 mm-pitch FFC).
Reproduction netlist: `carrier_board.net` (in the Morgan G-100T Automation project).
Each item below is written so you can add a **failing test first**, then fix.

Priorities: **B1/B2 are clean unit tests** (do these first). B3/B4 are real but need
small integration fixtures.

---

## B1 — Inner-plane antipad under-clears rectangular / oval THT pads  [HIGH]

**Symptom.** Any board with a rectangular or oval **through-hole** pad over an inner
GND/power plane fails pcb-creator's own DRC with
`inner_plane_antipad: Insufficient antipad clearance ... 0.162mm < 0.200mm`, which then
**blocks `export_outputs`** (it refuses on failed DRC). It is geometrically unavoidable
with the current code, so such a board can never pass. The same under-clearance is baked
into the Gerber copper fills, so it would also ship as a real manufacturing defect.

**Root cause.** `optimizers/router.py`, `create_copper_fill` (~line 2896), antipad sizing
at **router.py:3031**:
```python
pad_r = max(pad_info.pad_width_mm, pad_info.pad_height_mm) / 2   # <-- half the LONGER side
...
r = pad_r + clearance        # foreign-net antipad
r = pad_r + thermal_gap      # same-net thermal ring
```
For a rectangular pad the farthest copper is the **corner**, at `hypot(w,h)/2`, not
`max(w,h)/2`. So the circular antipad doesn't reach the corners and the real clearance is
less than requested. This is compounded by the 24-sided polygon approximation
(`ANTIPAD_SEGMENTS = 24`, router.py:3015): the inscribed radius is only `r·cos(π/24) ≈
0.991·r`, shaving clearance further.

**Expected.** Antipad clearance ≥ the configured value at *every* point of the pad
outline, including corners, after polygon approximation.

**Fix sketch.**
```python
half_extent = math.hypot(pad_info.pad_width_mm, pad_info.pad_height_mm) / 2  # corner reach
r = (half_extent + clearance) / math.cos(math.pi / ANTIPAD_SEGMENTS)        # undo inscription
```
(same for the `thermal_gap` branch). Round pads are unaffected (w==h ⇒ hypot/2 == max/2·√2…
note round pads should keep `max/2`; gate on pad shape, or treat circular pads specially).

**Suggested test.** Build a `pad_map` with one rectangular THT pad (e.g. 1.7×1.7 mm) over a
plane net, call `create_copper_fill(...)`, and assert the min distance from the pad's four
corners to the nearest antipad-cutout edge is ≥ `fill_clearance_mm`. Add a round-pad case
to prove no regression.

---

## B2 — Footprint lookup rejects a valid footprint when some pins are unconnected  [HIGH]

**Symptom.** A part with NC pins (very common: a TO-220 with one pin unused, a 6-pin Molex
with 2 wired, the FH35 with 28 of 30 wired) fails to resolve — even when the correct
footprint is in the KiCad library *or* registered via `register_custom_footprint`. It is
reported as an unresolved footprint and `optimize_placement` aborts.

**Root cause.** `exporters/kicad_mod_parser.py:210`:
```python
if pin_count > 0 and len(fp.pin_offsets) != pin_count:
    return None
```
`pin_count` here is the number of **connected** pins from the netlist (ports are only made
for pins that appear in a net), but `fp.pin_offsets` is the **full** pad count. Strict `!=`
rejects every footprint that has more pads than the design happens to connect.

**Expected.** A footprint with *at least* the connected pin count should resolve. NC pads
are normal and should not block lookup.

**Fix sketch.** `if pin_count > 0 and len(fp.pin_offsets) < pin_count: return None`
(reject only when the footprint has *fewer* pads than needed). Optionally warn on a large
mismatch rather than reject.

**Suggested test.** Parse/lookup a known multi-pad footprint (e.g. `TO-220-3_Vertical`,
3 pads) with `get_footprint(pkg, pin_count=2)` and assert it resolves. Add a case with
`pin_count` > pad count and assert it still returns `None`.

---

## B3 — `route_board` reports 100 % complete while a net is left in disconnected groups  [MED]

**Symptom.** Routing returns `completion_pct: 100.0, unrouted_nets: []`, but KiCad's own
DRC on the exported+poured board finds an open net (observed: `5V` split into 3 groups,
one FFC 5 V pad fully unrouted). `route_board(keep_existing=True)` — the documented
remedy — did **not** recover it across repeated runs.

**Expected.** Either the completion metric reflects true pad-to-pad connectivity (so 100 %
means 0 opens under KiCad DRC), or the incremental/keep_existing pass actually closes the
residual groups.

**Suggested test.** Route the `carrier_board.net` fixture, export to KiCad, fill zones,
run `kicad-cli pcb drc`, and assert `unconnected_items == 0` *whenever* the reported
completion is 100 %. (This guards the metric against KiCad's authoritative connectivity.)

---

## B4 — KiCad export: zones ship unpoured + duplicated / unmirrored silkscreen text  [MED]

Two independent issues in `exporters/kicad_exporter.py`:

**B4a — zones exported unpoured.** `_copper_fills` (~line 461) writes zone *outlines* and
relies on KiCad to fill them later, so the saved `.kicad_pcb` has **0 filled polygons**.
Any consumer that doesn't auto-pour — including `kicad-cli pcb drc` — then reports a flood
of false "unconnected" items (observed 78). *Expected:* export poured zones (or document
that a fill pass is required and provide one). *Test:* export, reload with `pcbnew`, assert
every copper zone has a non-empty `GetFilledPolysList`, and `kicad-cli drc` shows 0
unconnected.

**B4b — duplicate + unmirrored silk reference text.** The exporter emits each footprint's
`Reference` field as silk (kicad_exporter.py ~311) **and** a standalone silk text item from
the silk-items list (`_silkscreen`, ~line 516) → two copies of every designator stacked on
each other (`silk_overlap` / `silk_over_copper`). Separately, reference/value text on
**bottom-side** footprints is not mirrored → `nonmirrored_text_on_back_layer` (observed 54).
On this board that was 117 silk DRC warnings total. *Expected:* one designator per part,
back-side text mirrored. *Test:* export a board with a bottom-side footprint; assert no
duplicate silk text shares a footprint's reference string, and back-layer text has the
mirror flag set.

---

### How to reproduce the environment
```python
import sys; sys.path.insert(0, "<pcb-creator path>")
import mcp_server as m
m.import_kicad_netlist("bugrepro", "<path>/carrier_board.net", overwrite=True)
# register the 3 custom fps (fh35_30s_0.5sv_52, pc814_sot23_4, TI_SO-PowerPAD-8) — see B2
m.optimize_placement("bugrepro", board_width_mm=100, board_height_mm=50,
                     layers=4, plane_layers=1, two_sided=True)
m.route_board("bugrepro", effort="best")           # poll get_project_status
m.run_drc("bugrepro")                               # B1 fires here
m.export_kicad("bugrepro")                          # inspect for B4
```
Authoritative DRC = `kicad-cli pcb drc` on the exported board **after** filling zones with
`pcbnew.ZONE_FILLER`, using the project's `.kicad_pro` (fine-line rules: 0.127 mm
clearance/track) next to it.

---

# Resolution status (test-coverage session, 2026-06-28)

Worked test-first (failing test → fix → green), kept logic-core coverage at 100%,
full suite 1537 passed. Commits are on `claude/upbeat-allen-6dfa4b`.

## ✅ B1 — FIXED + tested
`optimizers/router.py` `generate_inner_plane`: antipad now sized from the pad's
**corner reach** `hypot(w,h)/2` (was `max(w,h)/2`) and divided by
`cos(pi/ANTIPAD_SEGMENTS)` to undo the polygon inscription (applied to pad and via
antipads). Round/oval pads are conservatively over-cleared (safe, never a defect);
tightening that needs per-pad shape data the pipeline doesn't carry (`PadInfo` has
no shape field — a future enhancement if plane voids matter on a dense board).
Test: `tests/test_inner_plane_antipad.py` (rect/oval corner clearance + round
no-regression). The default `fill_clearance_mm` is 0.25, and even the round case
failed pre-fix (0.2479 < 0.25), confirming the inscription half of the bug.

## ✅ B2 — FIXED + tested (with an extra guard the naive fix would have missed)
`exporters/kicad_mod_parser.py` `get_footprint`: rejects only when pads **<**
connected pins (NC pins resolve). **Caveat discovered:** the report's plain
`< pin_count` introduces a real regression — the short alias `SOT-23` resolves to
the 5-pad `SOT-23-5_HandSoldering` (an alias-generation collision: the regex
collapses `SOT-23-5` → `SOT-23`). Pre-fix, the strict `!=` accidentally masked
this by rejecting the 5≠3 mismatch and falling back to the generated 3-pad
footprint; plain `< pin_count` would have shipped a 3-pin transistor with a 5-pad
footprint (caught by the existing `tests/test_export_rotation.py`). The fix
therefore trusts *extra* pads only on an **exact full-name match**, not a
degenerate short alias — so `TO-220-3_Vertical` (NC pin) resolves but `SOT-23`
won't grab `SOT-23-5`. The underlying alias collision is a separate latent bug
(`_generate_aliases`) worth its own fix. Test: `tests/test_footprint_pin_count.py`.

## ✅ B4b — FIXED + tested
`exporters/kicad_exporter.py`: footprint `Reference`/`Value` moved to the `*.Fab`
layer (the visible silk designator is now solely the overlap-aware `gr_text` from
`_generate_silkscreen`, the same text the Gerbers render — no more stacked
duplicates), and a `_mirror_suffix(layer)` helper adds `(justify mirror)` to all
back-layer (`B.*`) text. Test: `tests/test_export_silk.py`.

## ✅ B3 — FIXED + tested + verified end-to-end (follow-up session, KiCad/pcbnew available)
`optimizers/router.py` `apply_copper_fills`: plane-net completion is now **pad-level**.
The power-plane stitching loop collects every SMD pad that finds no clear via site
into `unstitched_plane_pads`; a plane net is stripped from `unrouted_nets` **only**
when all its pads are stitched, so an open power pad now keeps its net unrouted and
drops `completion_pct` below 100 instead of silently reporting complete. The open pad
is surfaced at `routing.unstitched_plane_pads` (`[{designator, net_id}]`). The
candidate via-site ring was also densified (7 radii × 30° vs 3 × 45°) so crowded
fine-pitch pads find a site more often before giving up — that denser search is the
in-place "retry" (a `keep_existing` re-route re-runs `apply_copper_fills`, so it gets
another attempt as routing shifts). Test: `tests/test_b3_plane_pad_completion.py`
(boxed-in pad → net stays unrouted + completion <100 + pad surfaced; open pad →
stitched, net delivered, completion 100). **End-to-end verified:** a real
`carrier_board.net` route at `plane_layers=2` reported `completion_pct: 85.1` with
`unstitched_plane_pads: [{R27, net_n12v}]` and `net_n12v` in `unrouted_nets` — exactly
the open power pad the old net-level metric had hidden as 100%.
*Note:* an even tighter auto-fix (nudge the blocking neighbor, as the escape router
does) is a possible future enhancement; clearance relaxation is intentionally NOT
done because it would trade an honest open for a DRC clearance violation.

## ✅ B4a — FIXED + tested + verified end-to-end (follow-up session)
`exporters/kicad_exporter.py`: `export_kicad_pcb` already called `fill_zones_pcbnew`,
but its python-candidate list (`/usr/bin/python3`, `python3`) never found pcbnew on
macOS, so the pour silently no-op'd. New `_kicad_python_candidates()` also probes
KiCad.app's bundled framework python (`…/Python.framework/Versions/*/bin/python3`,
derived from `PCB_KICAD_CLI` or the default install) plus `sys.executable`, so the
pour actually runs and the exported `.kicad_pcb` ships poured zones. Chosen option
(b) (pcbnew `ZONE_FILLER`) over hand-emitting `filled_polygon` geometry — it gives
KiCad-correct fill-with-holes without re-deriving the fracture format. Side effect:
pcbnew canonicalises (re-serialises) the saved board, which exposed a brittle
single-line regex in `tests/test_export_silk.py` — fixed to paren-match each
`gr_text` block so it validates mirroring in either format. Tests:
`tests/test_b4a_export_pour.py` (candidate ordering, KiCad-bundle probing, and an
integration pour assert that the exported board contains `(filled_polygon`).
**End-to-end verified:** exporting the real v19 board now yields 9 filled polygons,
and `kicad-cli pcb drc` reports the route's *true* 4 unconnected (down from ~78 false
unconnected on the previously-unpoured export).

**Net:** all four bugs (B1, B2, B3, B4a, B4b) are now fixed, unit-tested, and — for
B3/B4a — verified end-to-end against `carrier_board.net` with Freerouting +
pcbnew + kicad-cli.

---

# Newly discovered while writing the B3 integration test (2026-06-28)

The authoritative integration test (`tests/test_integration_b3_carrier.py`: route the
real board → export → pour → `kicad-cli pcb drc`) revealed that `completion_pct == 100`
is **still not fully connectivity-true** even after B3 — there are two more sources,
*distinct* from the power-plane SMD-pad path B3 fixed. B3 closes its case (the test
hard-asserts an un-stitched power-plane pad is never silently credited); these two are
filed for separate fixes and the integration test surfaces them as warnings rather than
over-claiming a global guarantee it can't yet make.

## ⏳ B5 — GND outer-pour island with no stitching via to the inner plane
**Observed.** `kicad-cli` reports `Zone [GND] on In1.Cu / Zone [GND] on F.Cu` unconnected
at 100% completion. `apply_copper_fills` pours outer-layer GND fills + stitching vias
(`create_copper_fill`), but a fill fragment that routing chops off can end up with no
stitching via, leaving it electrically isolated from the In1 GND plane. (Same class hit
manually during the carrier compaction — fixed there by dropping a GND via into each
isolated region.) **Fix sketch.** After fills, detect GND fill regions with no through-via
tying them to the plane and add an all-layer-clear stitching via per region (the manual
repair logic), or surface them so completion reflects the gap.

## ⏳ B6 — Freerouting credits a point-to-point net while a pad gap remains
**Observed.** A signal net (`SWDIO`, CN1.19 ↔ SWD1.2) reported routed at 100% yet
`kicad-cli` finds the pads unconnected. Signal-net completion is taken from Freerouting's
`incomplete_connections` report, which can disagree with KiCad's authoritative
connectivity; the kicad-cli-driven short-cleanup pass that should catch it didn't here.
**Fix sketch.** Reconcile final `completion_pct` / `unrouted_nets` against authoritative
connectivity (`validators.validate_routing.incomplete_net_ids` or kicad-cli DRC) instead
of trusting Freerouting's net-level count, and feed any residual to the
short-cleanup / `keep_existing` retry. This is the general form of B3 (net-level →
pad-level completion) for routed nets, not just plane-delivered ones.

Full suite: 1553 passed, 1 skipped (+ the opt-in `test_integration_b3_carrier.py`).
