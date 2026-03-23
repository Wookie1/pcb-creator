# Routing Improvements Plan

## Goal
Fix disconnected groups issue and improve routing completion toward 100%.

## Part 1: Fix Disconnected Groups

### 1A. Add endpoint snapping to relaxed clearance phase
**File:** `optimizers/router.py` lines 3120-3145

The relaxed clearance phase routes MST edges individually but **never snaps trace endpoints to exact pad positions**, unlike `route_net()` (line 1121-1135) and `route_net_congestion()` (line 1216-1229). This causes trace endpoints to land at grid-quantized positions instead of exact pad centers, which can cause the DRC validator to report disconnected groups.

**Fix:** After `simplify_path()` in the relaxed clearance loop (after line 3143), add the same endpoint snapping logic:
```python
if traces:
    t0 = traces[0]
    traces[0] = TraceSegment(pad_a.x_mm, pad_a.y_mm, t0.end_x_mm, t0.end_y_mm, ...)
    tN = traces[-1]
    traces[-1] = TraceSegment(tN.start_x_mm, tN.start_y_mm, pad_b.x_mm, pad_b.y_mm, ...)
```

### 1B. Add post-routing connectivity repair pass
**File:** `optimizers/router.py` — new function + integration in `route_board()`

After all routing phases complete (NCR → rip-up → shove → relaxed), run the DRC connectivity check on the result. For any nets with disconnected groups:
1. Rip up the net's traces from the grid
2. Re-route using `route_net()` (which has proper endpoint snapping)
3. If re-route fails, try with relaxed clearance (with the new snapping fix)

This is a safety net that catches any edge cases we haven't identified.

## Part 2: Improve NCR Convergence

### 2A. Tune NCR parameters
**File:** `optimizers/router.py` — `RouterConfig` and `_negotiated_congestion_route()`

Current: 12 iterations, hfac starts at 0.5, increments 0.5. The router hit 30-41 overused cells and couldn't converge.

Changes:
- Increase `ncr_max_iterations` from 12 → 20
- Increase `ncr_hfac_increment` from 0.5 → 1.0 (stronger penalties on persistent congestion)
- Add exponential backoff: if overused cells stop decreasing for 3 iterations, multiply hfac by 2x
- Track overused cell trend and terminate early if oscillating (saves time on boards that won't converge)

### 2B. Dynamic net reordering within NCR
**File:** `optimizers/router.py` — `_negotiated_congestion_route()` inner loop

Currently, net ordering is fixed within NCR iterations. Freerouting-style approach: after each iteration, re-sort nets so that the most congested (illegal) nets route first in the next iteration. This gives congested nets priority access to scarce routing resources.

Add after the legal/illegal classification (around line 2572):
```python
# Reorder: illegal nets first (sorted by congestion severity), then legal nets
net_order = reorder_by_congestion(net_order, net_paths, present_occupancy, ...)
```

## Part 3: Improve Rip-up and Shove

### 3A. Multi-net coordinated rip-up
**File:** `optimizers/router.py` — coordinated rip-up section (lines 2980-3084)

Currently rips one blocker at a time. Improvement: when multiple failed nets share the same set of blockers, rip all related blockers simultaneously before re-routing. This avoids the sequential "rip one, route some, get stuck again" pattern.

### 3B. Deeper shove with segment splitting
**File:** `optimizers/router.py` — `_shove_pass()`

Currently shoves entire trace segments by 1-3 cells perpendicular. Improvement: allow splitting a blocking segment at the conflict point — the segment breaks into two pieces that route around the obstruction. This is closer to how Freerouting's shove router works.

## Implementation Order

1. **1A** — Endpoint snapping fix (quick, high-confidence fix)
2. **1B** — Connectivity repair pass (safety net)
3. **2A** — NCR parameter tuning (moderate effort, good ROI)
4. **2B** — Dynamic net reordering in NCR (moderate effort)
5. **3A** — Multi-net coordinated rip-up (moderate effort)
6. **3B** — Segment splitting in shove (higher effort, higher reward)

After each step, run the Arduino board and check DRC results.

## Expected Outcome
- Zero disconnected groups (from 1A + 1B)
- Better NCR convergence (27→29 nets legal before fallback, from 2A + 2B)
- Higher completion without relaxed clearance (from 3A + 3B)
- Goal: 29/29 nets, 0 shorts, 0 clearance violations
