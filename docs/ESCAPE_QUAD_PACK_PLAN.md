# Plan: escape fanout for quad packs (LQFP/TQFP/QFN)

Status: **DONE** (see "What shipped" at the bottom). Branch
`electrical-checks-and-pad-geometry`. Suite: 1700 passed, 3 skipped.

## Why

`scripts/test_stm32_4layer.py` ends with exactly one DRC error:

```
Net net_vcc3v3 is unrouted — the router left it incomplete
Power plane: no clear via site for VCC3V3 pad U1.9  — net kept unrouted
Power plane: no clear via site for VCC3V3 pad U1.24 — net kept unrouted
Power plane: no clear via site for VCC3V3 pad U1.36 — net kept unrouted
```

Three LQFP-48 VDD pins cannot reach the inner power plane. Post-route stitching
(`optimizers/router.py`, the `pwr_stitch_vias` loop) runs **after** Freerouting,
so the signals have already taken the escape channels around the part. Via-in-pad
is unavailable too: a 0.6 mm via does not fit a 0.3 mm-wide pad.

## The actual gap

The escape router already does the right thing for plane nets —
`generate_escape_routing` docstring: *"Pins on a plane net (`exclude_nets` —
GND/power planes) get a stub + via that drops straight to the plane (no onward
trace)."* That is not what is missing.

What is missing is **quad-pack support**. `optimizers/escape_router.py` ~line 199
groups pads by part, then classifies the part as a single row or column:

```python
span_x, span_y = max(xs) - min(xs), max(ys) - min(ys)
tol = pitch * 0.5
if span_y <= tol and span_x > tol:   ...   # horizontal row
elif span_x <= tol and span_y > tol: ...   # vertical row
else:
    continue                 # not a single row — leave to the autorouter
```

An LQFP-48 has pads on four sides, so both spans are large and the part is
skipped outright. Confirmed empirically: the first-pass DSN contains **zero**
keepouts (only the post-route cleanup pass emits `esc_ko_*`).

## The change

Decompose a part into **side groups** and run the existing per-row logic once per
side. Everything downstream of the row/column decision — stub, via, staggered
second via row, release line, `_via_clears_foreign_traces` — is already
side-agnostic and needs no modification.

1. **Side assignment.** Compute the part's pad bounding box. Assign each SMD pad
   to L/R/B/T by which edge it is within `tol` of. Corner pads can match two
   edges; break the tie toward the side whose axis the pad is *long* on (an LQFP
   pad's long axis points outward from its own side).
2. **Escape direction per side.** Outward from the part centre:
   L→(-1,0), R→(+1,0), B→(0,-1), T→(0,+1). Note this differs from the current
   single-row rule, which aims at the *board* centre (`bcy >= mean(ys)`) — right
   for a connector on a board edge, wrong for one side of a quad pack.
3. **Per-side pitch.** Call `_min_adjacent_pitch` on the side group, not the
   whole part. Across a quad pack the global minimum can come from two pads on
   different sides, which is not a pitch.
4. **Preserve accumulation.** `placed_via_centers` and `placed_traces` are
   already in the enclosing scope; iterate sides *inside* the part loop so
   opposite sides keep clearing each other's vias.
5. **Keep it opt-in by pitch.** The existing `pitch_threshold_mm` gate stays, so
   coarse quad packs are untouched.

Prefer refactoring the row body into a local `_escape_one_side(pads, edir)` and
calling it from both paths, rather than duplicating it.

## Risks

- **Via count.** 48 pins escaping could congest a small board. `leaving =
  _nets_leaving_part(netlist, des)` already limits to nets that actually leave
  the part, and no-connect pins have no net — verify escapes are only generated
  for those, not for all 48 pads.
- **Board area.** The release line sits outside the pad field on all four sides;
  on a 50x35 mm board with parts packed into a ~37x14 mm corner this may not fit.
  Check `release` against the board edge and skip the side if it would not.
- **Nondeterminism.** Freerouting output varies run to run on this board
  (observed 0%/72.7%/81.8%/100% across identical inputs). Judge the change on
  several runs, not one.

## Verification

1. `U1.9`, `U1.24`, `U1.36` get escape vias; the "no clear via site" warnings
   disappear; `unstitched_plane_pads` is empty.
2. `net_vcc3v3` routed → `stages.export_blocked` no longer refuses on
   connectivity → the E2E reaches step 5 and writes Gerbers.
3. First-pass DSN contains `esc_ko_*` keepouts for U1 (currently zero).
4. Re-check the whole DRC set stays at zero for `tracks_crossing`,
   `shorting_items`, `clearance_min`, `hole_to_hole`.
5. `.venv/bin/python -m pytest tests/ -q` stays at 1696 passed, 3 skipped.
6. Add a regression test asserting a quad pack yields escapes on all four sides
   (`tests/test_pad_size_orientation.py` has a working `kicad_lookup` fixture to
   copy — it skips when no system KiCad library is present).

## Run it

```bash
cd /Users/James/ai-sandbox/Productizr/pcb-creator
.venv/bin/python scripts/test_stm32_4layer.py 2>&1 | sed -n '/Step 3/,$p'
```

Needs `_init_lookup()` (or `configure_lookup`) before any footprint call —
without it the KiCad library tier is silently off and every footprint misses.
The script now does this itself; ad-hoc diagnostic scripts must do it too.

---

## What shipped

Quad-pack escape works end to end. On the STM32 fixture all five of U1's VCC3V3
pins now fan out to the inner power plane deterministically (they never appear in
the "no clear via site" list across ~15 runs), and the board reaches 100% routed
/ DRC-pass on cooperative Freerouting runs — which the baseline (always 1 error
on U1.9/24/36) never did.

Four changes, plus three integration bugs found and fixed along the way:

1. **`escape_router.py` — side decomposition.** `_side_groups()` splits a part
   into L/R/B/T runs (a real quad pack needs all four sides ≥3 pads, else it
   falls through to the old single-row path). Each side escapes *outward from the
   part centre* with its own per-side pitch. The row body was extracted into
   `_escape_row()`, shared by both paths. Two new guards a lone connector never
   needed: `_overshoots()` (copper must clear the board edge by
   `edge_clearance_mm`, new field, default 0.5) and `_clears_foreign_pads()` (a
   dense pack escapes into a neighbour's pads).

2. **Plane escapes are through-vias.** Was dropping every plane pin to
   `plane_names[0]` (In1=GND); a power net lives on the In2 plane, so the via
   never reached it. Now `top→bottom`, passing every inner plane and connecting
   to whichever carries the net — matches the power stitch vias.

3. **Stitcher skips escaped pads (`router.py`).** The post-route plane stitcher
   iterated *all* power pads and re-stitched ones the escape already delivered —
   redundant vias where there was room, a false "no clear via site" where the
   escape via itself blocked the ring. Now skips any pad with an escape trace
   endpoint on it (keyed by net, since the plane net is excluded from Freerouting
   and can carry no other trace — `escape_role` is stripped by the SES round-trip).

4. **Escapes made visible to the stitch pass (`stages.py`).** The excluded
   plane net is absent from the DSN, so Freerouting never echoes its escapes;
   they were only re-merged *after* the fill/stitch pass. `_premerge_excluded_escapes()`
   now merges them in first, in both the fresh-route and short-cleanup paths
   (the later safety-net merge dedupes to a no-op). Also tightened the fine-pitch
   via to 0.45/0.2 (was left at the coarse 0.6mm default, mismatched with the
   escape vias and too big for the stitcher to place on a small power pad near
   dense routing).

Tests: `TestQuadPack` in `tests/test_escape_router.py` (four sides escape,
opposite sides collision-free, plane pins reach the plane, a corner-packed part
skips its off-board side).

## Known remaining issue (pre-existing, NOT quad packs)

On ~half the Freerouting runs a *non-fine-pitch* power pad (C2.1, R1.1, U2.2 —
0402 caps / SOT-223 regulator) still fails to stitch: Freerouting nondeterministically
routes signal traces densely around it, and the post-route ring search finds no
clear via site. This is the post-route stitcher's architecture (it runs *after*
Freerouting, which has no reason to leave room near a power pad) surfacing under
tighter via density, not a quad-pack regression — those pads were never escaped.

The clean fix is deterministic **pre-route** plane stitching: pre-place a
protected via-in-pad on every plane pad (not just fine-pitch quad packs) before
Freerouting, so it routes around them and the post-route stitch pass has nothing
to do. That is a separate feature — scope beyond this plan.
