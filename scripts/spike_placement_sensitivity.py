#!/usr/bin/env python3
"""Is any board actually PLACEMENT-bound? (Phase 1 of the co-optimization plan)

A candidate-and-select placement/routing outer loop can only ever recover the
spread in routing outcome that placement itself causes. So measure that spread
before building the loop:

  1. AREA SWEEP — scale the board down until routing first drops below 100%.
     Routability is insensitive to placement when there is slack and when the
     board is hopeless; it matters most at that knee.
  2. SEED SPREAD — at the knee, re-place with N seeds and route each. The spread
     in completion_pct is the headroom a co-optimizer could capture.
  3. NOISE FLOOR — route the SAME placement R times. Freerouting is
     nondeterministic; spread below this floor is engine noise, not signal.

Verdict per board:
  easy            — 100% everywhere; nothing to capture
  router-limited  — headroom is inside the noise floor; placement is not the
                    bottleneck (this was the dense reference board)
  seed-flaky      — median already at the ceiling; selection buys reliability
  placement-bound — headroom > noise; a co-optimizer has real headroom

One route yields the whole pass curve: the progress callback reports unrouted
count after every pass, so pass-1 doubles as the cheap probe a Phase 2 oracle
would use, measured against the same run's final result (`probe_correlation`).

MEASURED VERDICT (2026-07-26, 8 seeds x 5 routes, 11 boards): NO-GO, 0/11
placement-bound. Every board's headroom sits inside its own noise floor once
each candidate is scored on a MEDIAN of routes rather than one lucky one --
e.g. arduino_nano 10.55 pts of headroom against an 18.4 pt floor. Scored on a
single route the same board flips verdict run to run (ads1115 came out
placement-bound at 27.85 pts on one sweep and router-limited at 11.1 on the
next), which is what makes best-of-N selection worthless here: the winner is
the lucky route, not the good placement.

The cheap oracle fails independently. Pass-1 unrouted correlates only -0.47 to
-0.51 with the final result; scoring the probe in the objective's own units
(a 1-pass route imported to net-based completion) is *anti*-correlated, -0.46
and -0.32 -- the seed it ranks best is mid-tier and the true winner ranks last.

Reports land in eval_output/, which is gitignored -- re-run to regenerate.

Usage:
    python scripts/spike_placement_sensitivity.py [--boards a,b] [--seeds 8]
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scripts"))

from eval_boards import _build_netlist, configure_eval_lookup  # noqa: E402
from optimizers.freerouter import route_with_freerouting  # noqa: E402
from orchestrator.config import OrchestratorConfig  # noqa: E402
from orchestrator.stages import run_placement  # noqa: E402

REQ_DIR = REPO / "test" / "requirements"
OUT_DIR = REPO / "scripts" / "eval_output"

# Scale factors tried largest-first; the knee is the first one below 100%.
# Spans both directions so a board already failing at nominal is caught too.
SCALES = (1.3, 1.15, 1.0, 0.9, 0.8, 0.7, 0.6)
PROBE_PASSES = 10      # bounded — real since the STOP_PASS_NO fix landed
ROUTE_TIMEOUT_S = 240


def _prepare(board: str):
    """Build netlist + project dir once; every probe re-places into it."""
    req = json.loads((REQ_DIR / f"{board}.json").read_text())
    tmp = Path(tempfile.mkdtemp(prefix=f"sens-{board}-"))
    pdir = tmp / board
    ok, msg = _build_netlist(req, pdir, board, {})
    if not ok:
        return None, None, f"netlist build failed: {msg}"
    (pdir / f"{board}_requirements.json").write_text(json.dumps(req))
    return req, pdir, None


def _probe(pdir: Path, board: str, config, w: float, h: float, seed: int,
           reuse_placement: bool = False) -> dict:
    """Place (unless reusing) + route once. Returns the per-pass curve."""
    out = {"seed": seed, "w": round(w, 1), "h": round(h, 1)}
    if not reuse_placement:
        place = run_placement(pdir, board, config, board_width_mm=w,
                              board_height_mm=h, seed=seed)
        if not place.get("success"):
            out["status"] = "place_failed"
            out["detail"] = str(place.get("error", "?"))[:120]
            return out
        out["wire_mm"] = place.get("wire_length_mm")

    placement = json.loads((pdir / f"{board}_placement.json").read_text())
    netlist = json.loads((pdir / f"{board}_netlist.json").read_text())

    curve: dict[int, int] = {}

    def on_progress(p):
        n, inc = p.get("pass_num"), p.get("incomplete_connections")
        if n is not None and inc is not None:
            curve.setdefault(n, inc)

    t0 = time.monotonic()
    try:
        res = route_with_freerouting(placement, netlist,
                                     timeout_s=ROUTE_TIMEOUT_S,
                                     max_passes=PROBE_PASSES,
                                     progress_callback=on_progress)
    except Exception as exc:  # noqa: BLE001 — one bad probe must not kill the sweep
        out.update(status="route_failed", detail=f"{type(exc).__name__}: {exc}"[:120],
                   route_s=round(time.monotonic() - t0, 1))
        return out
    stats = res.get("routing", {}).get("statistics", {})
    out.update(status="ok",
               completion=stats.get("completion_pct"),
               vias=stats.get("via_count"),
               route_s=round(time.monotonic() - t0, 1),
               pass_curve=[[n, curve[n]] for n in sorted(curve)],
               probe_unrouted=curve.get(1))
    return out


def _spread(vals: list[float]) -> float:
    return round(max(vals) - min(vals), 2) if len(vals) > 1 else 0.0


def _verdict(best: float, median: float, noise: float) -> tuple[str, float]:
    """Classify a board by what a candidate-selector could actually win.

    Best-of-N only ever moves you from the median to the best, so it is that
    gap — not the full seed spread — that has to clear the engine's own noise.
    """
    headroom = round(best - median, 2)
    if headroom <= 0:
        return "seed-flaky", headroom
    if headroom <= noise:
        return "router-limited", headroom
    return "placement-bound", headroom


def _selftest() -> None:
    assert _verdict(100.0, 100.0, 0.0)[0] == "seed-flaky"
    # The real regression: gating on seed spread (33.4) instead of headroom
    # called ads1115 placement-bound, when its 11.1 pts sit inside an 11.2 pt floor.
    assert _verdict(44.4, 33.3, 11.2) == ("router-limited", 11.1)
    assert _verdict(44.4, 33.3, 5.0) == ("placement-bound", 11.1)
    print("selftest ok")


def analyze(board: str, config, n_seeds: int, n_repeats: int) -> dict:
    req, pdir, err = _prepare(board)
    if err:
        return {"board": board, "verdict": "build_failed", "detail": err}

    nom_w = req.get("board", {}).get("width_mm", 50)
    nom_h = req.get("board", {}).get("height_mm", 40)
    row: dict = {"board": board, "nominal_mm": [nom_w, nom_h],
                 "components": len(req.get("components", []))}

    # 1. Area sweep → find the knee.
    sweep = []
    knee = None
    stopped_on_place = False
    for s in SCALES:
        r = _probe(pdir, board, config, nom_w * s, nom_h * s, seed=42)
        r["scale"] = s
        sweep.append(r)
        print(f"    scale {s:<5} {r.get('status'):<12} "
              f"completion={r.get('completion', '-')}% "
              f"({r.get('route_s', '-')}s)", flush=True)
        if r.get("status") == "ok" and (r.get("completion") or 0) < 100:
            knee = s
            break
        if r.get("status") == "place_failed":
            # The legalizer ran out of room before routing ever got a say —
            # a different bottleneck from routability, so say so rather than
            # scoring it as a board placement can't improve.
            stopped_on_place = True
            break
    row["sweep"] = sweep

    if knee is None:
        ok_scales = [r["scale"] for r in sweep if r.get("status") == "ok"]
        if stopped_on_place:
            row["verdict"] = "placement-infeasible"
            row["detail"] = (
                f"SA legalizer could not fit the parts below scale "
                f"{min(ok_scales) if ok_scales else max(SCALES)}x — bounded by "
                "placement legality, not routability")
        else:
            row["verdict"] = "easy"
            row["detail"] = ("routed 100% at every scale tried down to "
                             f"{min(SCALES)}x — no knee, nothing for placement to win")
        return row
    row["knee_scale"] = knee
    kw, kh = nom_w * knee, nom_h * knee

    # 2. Seed spread at the knee.
    print(f"    knee at scale {knee} ({kw:.1f}x{kh:.1f}mm) — {n_seeds} seeds",
          flush=True)
    # Each seed is routed n_repeats times and scored on its MEDIAN. Routing a
    # placement once gives a single draw from a distribution ~10-25 pts wide, so
    # a single-sample "best seed" is mostly a lucky route: the same board came
    # out placement-bound on one run and router-limited on the next.
    seeds = []
    for k in range(n_seeds):
        r = _probe(pdir, board, config, kw, kh, seed=k)
        if r.get("status") == "ok":
            reps = [r] + [_probe(pdir, board, config, kw, kh, seed=k,
                                 reuse_placement=True)
                          for _ in range(n_repeats - 1)]
            got = [x["completion"] for x in reps
                   if x.get("status") == "ok" and x.get("completion") is not None]
            r["completion_runs"] = got
            r["completion"] = round(statistics.median(got), 2)
        seeds.append(r)
        print(f"      seed {k}: {r.get('status'):<12} "
              f"completion={r.get('completion', '-')}% "
              f"of {r.get('completion_runs', '-')}", flush=True)
    row["seeds"] = seeds
    comps = [r["completion"] for r in seeds
             if r.get("status") == "ok" and r.get("completion") is not None]
    if len(comps) < 2:
        row["verdict"] = "inconclusive"
        row["detail"] = f"only {len(comps)} seed(s) produced a route at the knee"
        return row
    row["seed_completions"] = comps
    row["seed_spread"] = _spread(comps)
    row["seed_best"] = max(comps)
    row["seed_median"] = round(statistics.median(comps), 2)

    # 3. Noise floor — the repeats above already routed each fixed placement
    # n_repeats times, so the within-placement spread is free. Take the median
    # across seeds rather than one placement's: it is the typical amount the
    # engine moves when placement is held constant.
    within = [_spread(r["completion_runs"]) for r in seeds
              if r.get("completion_runs")]
    row["noise_per_seed"] = within
    row["noise_spread"] = round(statistics.median(within), 2) if within else 0.0

    # Verdict. Gate on HEADROOM vs noise, not spread vs noise: best-of-N only
    # ever moves you from the median to the best, so the spread below the median
    # is not winnable and must not count towards clearing the noise floor.
    # (Gating on spread promoted both ads1115 and arduino_nano, whose headroom
    # is *inside* the floor — the same placement re-routed swings further than
    # the best seed beats the median one.)
    row["verdict"], row["headroom_pts"] = _verdict(
        row["seed_best"], row["seed_median"], row["noise_spread"])
    if row["verdict"] == "seed-flaky":
        # Worth selecting for RELIABILITY (dodge the occasional bad seed), not
        # for median quality. Kept distinct so it is not counted as headroom.
        row["detail"] = (f"median seed already hits {row['seed_median']}% — "
                         f"spread {row['seed_spread']} pts is occasional bad "
                         "seeds, so selection buys reliability, not quality")
    elif row["verdict"] == "router-limited":
        row["detail"] = (f"median {row['seed_median']}% → best "
                         f"{row['seed_best']}% is only {row['headroom_pts']} pts, "
                         f"inside the {row['noise_spread']} pt engine noise floor "
                         "— selecting the best seed selects a lucky route")
    else:
        row["verdict"] = "placement-bound"
        row["detail"] = (f"median {row['seed_median']}% → best "
                         f"{row['seed_best']}% = {row['headroom_pts']} pts of "
                         f"headroom, over a {row['noise_spread']} pt noise floor")

    # Does pass-1 predict the final result? (validity of a Phase 2 oracle)
    pairs = [(r["probe_unrouted"], r["completion"]) for r in seeds
             if r.get("status") == "ok" and r.get("probe_unrouted") is not None
             and r.get("completion") is not None]
    if len(pairs) >= 3 and len({p[0] for p in pairs}) > 1:
        try:
            row["probe_correlation"] = round(
                statistics.correlation([p[0] for p in pairs],
                                       [p[1] for p in pairs]), 3)
        except statistics.StatisticsError:
            pass
    return row


def main() -> int:
    global PROBE_PASSES
    ap = argparse.ArgumentParser()
    ap.add_argument("--boards", default="", help="comma-separated stems (default: all)")
    ap.add_argument("--seeds", type=int, default=8)
    ap.add_argument("--repeats", type=int, default=3, help="noise-floor routes")
    ap.add_argument("--passes", type=int, default=PROBE_PASSES,
                    help="bounded passes per probe — lower it to price a cheap "
                         "Phase 2 oracle against the full-depth result")
    # A partial re-run must not clobber the full sweep's record; that has
    # already happened once (sensitivity.md left reporting 1/1 boards).
    ap.add_argument("--out", default="sensitivity", help="output file stem")
    ap.add_argument("--selftest", action="store_true",
                    help="check the verdict gate and exit")
    args = ap.parse_args()
    if args.selftest:
        _selftest()
        return 0
    PROBE_PASSES = args.passes

    paths = sorted(REQ_DIR.glob("*.json"))
    if args.boards:
        want = {b.strip().lower() for b in args.boards.split(",")}
        paths = [p for p in paths if p.stem.lower() in want]
    if not paths:
        print("no boards matched", file=sys.stderr)
        return 1

    config = OrchestratorConfig.from_env(base_dir=REPO)
    # MUST come before any placement: without the real KiCad footprints the
    # boards measure far easier than they are (see configure_eval_lookup).
    configure_eval_lookup(config)
    rows = []
    for p in paths:
        print(f"\n{p.stem}", flush=True)
        try:
            row = analyze(p.stem.lower(), config, args.seeds, args.repeats)
        except Exception as exc:  # noqa: BLE001 — one bad board must not kill the run
            row = {"board": p.stem.lower(), "verdict": "crashed",
                   "detail": str(exc)[:200]}
        print(f"  => {row['verdict']}: {row.get('detail', '')}", flush=True)
        rows.append(row)

    OUT_DIR.mkdir(exist_ok=True)
    (OUT_DIR / f"{args.out}.json").write_text(
        json.dumps({"passes": PROBE_PASSES, "scales": SCALES,
                    "seeds": args.seeds, "rows": rows}, indent=2))

    cols = ["board", "verdict", "knee_scale", "seed_spread", "noise_spread",
            "headroom_pts", "seed_best", "seed_median", "probe_correlation"]
    lines = [f"# Placement sensitivity — {args.seeds} seeds, "
             f"{PROBE_PASSES} passes", "",
             "| " + " | ".join(cols) + " |", "|" + "---|" * len(cols)]
    for r in rows:
        lines.append("| " + " | ".join(str(r.get(c, "-")) for c in cols) + " |")
    lines += ["", "## Verdicts", ""]
    for r in rows:
        lines.append(f"- **{r['board']}** — {r['verdict']}: {r.get('detail', '')}")
    bound = [r for r in rows if r.get("verdict") == "placement-bound"]
    flaky = [r for r in rows if r.get("verdict") == "seed-flaky"]
    lines += ["", f"**{len(bound)}/{len(rows)} boards have real placement headroom**"
                  f" ({len(flaky)} more are seed-flaky: selection would buy"
                  f" reliability, not median quality).", ""]
    if bound:
        lines.append("Go/no-go on Phase 2: GO — "
                     + ", ".join(f"{r['board']} ({r['headroom_pts']} pts)"
                                 for r in bound))
        # A candidate-selector is only as good as the cheap score it ranks by.
        # Pass-1 unrouted count must actually predict the final result, or the
        # loop just burns routes picking noise.
        corrs = [(r["board"], r["probe_correlation"]) for r in bound
                 if r.get("probe_correlation") is not None]
        if corrs:
            worst = max(corrs, key=lambda c: c[1])  # least negative = weakest
            lines += ["", f"**Oracle caveat:** the pass-1 probe correlates "
                          f"{worst[1]} with final completion on {worst[0]} "
                          "(-1.0 = perfect). A selector ranking candidates by a "
                          "probe this weak picks near-randomly — validate the "
                          "score before building the loop."]
    else:
        lines.append("Go/no-go on Phase 2: NO-GO — no board shows placement "
                     "headroom above the engine noise floor.")
    (OUT_DIR / f"{args.out}.md").write_text("\n".join(lines) + "\n")

    print(f"\n{len(bound)}/{len(rows)} placement-bound — "
          f"report at {OUT_DIR}/{args.out}.md")
    return 0


if __name__ == "__main__":
    sys.exit(main())
