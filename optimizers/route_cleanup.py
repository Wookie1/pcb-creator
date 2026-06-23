#!/usr/bin/env python3
"""Short-cleanup pass: rip up the nets that DRC reports as shorting (or that are
left incomplete) and re-route them with everything else — crucially including
all fine-pitch escape wiring — held as protected wiring, so the autorouter gives
just those nets a fresh path without disturbing the rest.

Why a separate pass driven by kicad-cli DRC: Freerouting's first pass routes the
whole board greedily and occasionally clips a through-hole pad (gate-driver
pins, headers). Re-routing only the offending nets, with every other trace
fixed, reliably clears those shorts (validated on morgan: board shorts → 0,
fine-pitch CN1 untouched). The bad-net list MUST come from kicad-cli's
authoritative ``shorting_items`` — the internal geometric short-check
over-reports ~15× against escape/through-hole geometry, so it can't target the
right nets. Where kicad-cli isn't installed the pass is a no-op.

This module is dependency-injected (``route_fn`` / ``drc_net_names_fn``) so the
rip/preserve/accept logic is unit-testable without Freerouting or KiCad; the
orchestrator (`stages._short_cleanup`) wires in the real export+DRC+route.
"""

from __future__ import annotations

import json
import math
import os
import shutil
import subprocess
from pathlib import Path
from typing import Callable

from validators.validate_routing import incomplete_net_ids


_KICAD_CLI_CANDIDATES = (
    "/usr/bin/kicad-cli",
    "/usr/local/bin/kicad-cli",
    "/opt/homebrew/bin/kicad-cli",
    "/Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli",
)


def find_kicad_cli() -> str | None:
    """Locate a usable ``kicad-cli`` binary, or None. Honours ``PCB_KICAD_CLI``,
    then ``PATH``, then well-known absolute locations.

    The absolute-path fallback matters: the MCP server is often spawned with a
    stripped PATH (no /usr/bin), so ``shutil.which`` finds nothing and the
    authoritative DRC silently degrades to the optimistic internal validator.
    Probing the known install paths keeps DRC authoritative regardless of PATH.
    """
    env = os.environ.get("PCB_KICAD_CLI")
    if env and Path(env).exists():
        return env
    found = shutil.which("kicad-cli")
    if found:
        return found
    for cand in _KICAD_CLI_CANDIDATES:
        if Path(cand).exists():
            return cand
    return None


# kicad-cli DRC violation types whose involved nets the cleanup can fix by
# ripping + re-routing them: copper shorts, and clearance violations (a trace/via
# sitting too close to another net — re-routing that net moves it away). Hole-to-
# hole / drill spacing is NOT here: it is a via-placement issue, not fixable by
# re-routing a net (handled at via generation instead).
_FIXABLE_BY_REROUTE = {
    "shorting_items",
    "clearance", "track_clearance", "via_clearance", "hole_clearance",
    "copper_clearance", "creepage",
    # a mask sliver between different-net items — re-routing one away clears it
    # (and a short to a pad usually pairs a shorting_items with this).
    "solder_mask_bridge",
}


def run_drc_json(pcb_path: str | Path, kicad_cli: str,
                 *, timeout: int = 300) -> dict | None:
    """Run ``kicad-cli pcb drc`` and return the parsed JSON report, or None if the
    tool can't be run (caller treats as 'skip cleanup')."""
    pcb_path = Path(pcb_path)
    out = pcb_path.with_suffix(".cleanup_drc.json")
    try:
        subprocess.run(
            [kicad_cli, "pcb", "drc", "--format", "json", "--output", str(out),
             "--severity-error", str(pcb_path)],
            capture_output=True, timeout=timeout, check=False)
        return json.loads(out.read_text())
    except (FileNotFoundError, OSError, subprocess.SubprocessError,
            json.JSONDecodeError, ValueError):
        return None
    finally:
        try:
            out.unlink()
        except OSError:
            pass


def drc_shorting_net_names(pcb_path: str | Path, kicad_cli: str,
                           *, timeout: int = 300) -> set[str] | None:
    """Net NAMES involved in reroute-fixable violations (shorts + clearance).
    None if kicad-cli couldn't run; an empty set means 'ran, nothing to fix'."""
    data = run_drc_json(pcb_path, kicad_cli, timeout=timeout)
    return None if data is None else _parse_shorting_net_names(data)


def _item_net(desc: str) -> str | None:
    """Net name in a DRC item description's first [..] token."""
    if "[" in desc and "]" in desc:
        return desc[desc.index("[") + 1:desc.index("]")]
    return None


def parse_cleanup_drc(drc_data: dict) -> tuple[set[str], list[tuple[float, float]],
                                               set[str]]:
    """From a kicad-cli DRC report derive what the cleanup needs:
      • bad_names    — nets to rip + re-route (shorts ∪ clearance)
      • keepouts     — (x,y) collision loci to wall off on the re-route, so the
                       re-routed net is forced AWAY from where it shorted /
                       violated clearance (breaks the 'same mistake every round'
                       loop)
      • protect_names — for a short to a PAD, that pad's net is kept PROTECTED so
                       the keepout sitting on the pad doesn't block the pad's own
                       connection (net-safe by construction). The foreign (track/
                       via) net is the one ripped and re-routed clear of it."""
    bad: set[str] = set()
    keepouts: list[tuple[float, float]] = []
    protect: set[str] = set()
    for v in drc_data.get("violations", []):
        if v.get("type") not in _FIXABLE_BY_REROUTE:
            continue
        items = v.get("items", [])
        for it in items:
            n = _item_net(it.get("description", ""))
            if n:
                bad.add(n)
        # Choose the keepout centre: the PAD for a short (wall the pad off);
        # otherwise the first positioned item.
        pos_item = None
        if v.get("type") == "shorting_items":
            for it in items:
                if "pad" in it.get("description", "").lower():
                    pn = _item_net(it.get("description", ""))
                    if pn:
                        protect.add(pn)
                    if it.get("pos"):
                        pos_item = it
        if pos_item is None:
            pos_item = next((it for it in items if it.get("pos")), None)
        if pos_item and pos_item.get("pos"):
            p = pos_item["pos"]
            keepouts.append((round(p.get("x", 0.0), 3), round(p.get("y", 0.0), 3)))
    return bad, keepouts, protect


def _parse_shorting_net_names(drc_data: dict) -> set[str]:
    """Net names appearing in reroute-fixable violations (shorts + clearance) of
    a kicad-cli DRC report. Item descriptions read like ``Track [FB_DIV] on
    F.Cu …`` / ``Via [5V] at …`` / ``Pad 1 [GATE_Q3] of Q3 …`` — the net name is
    the first bracketed token. Both nets of a two-item clearance violation are
    collected, so re-routing either can resolve it."""
    names: set[str] = set()
    for v in drc_data.get("violations", []):
        if v.get("type") not in _FIXABLE_BY_REROUTE:
            continue
        for it in v.get("items", []):
            desc = it.get("description", "")
            if "[" in desc and "]" in desc:
                names.add(desc[desc.index("[") + 1:desc.index("]")])
    return names


def _trace_key(t: dict) -> tuple:
    return (round(t["start_x_mm"], 3), round(t["start_y_mm"], 3),
            round(t["end_x_mm"], 3), round(t["end_y_mm"], 3), t.get("layer"))


def _via_key(v: dict) -> tuple:
    return (round(v["x_mm"], 3), round(v["y_mm"], 3))


def build_protected_wiring(routed: dict, escapes: dict,
                           bad_net_ids: set[str]) -> dict:
    """Protected wiring for the cleanup re-route: ALL escape stubs/vias/fanout
    (so ripping a fine-pitch net doesn't lose its breakout) plus every
    good-net trace/via. Only the bad nets' NON-escape (autorouter onward)
    wiring is dropped, so Freerouting re-routes just those from their escapes."""
    rt = routed.get("routing", {})
    esc_tk = {_trace_key(t) for t in escapes.get("traces", [])}
    esc_vk = {_via_key(v) for v in escapes.get("vias", [])}

    keep_t = [t for t in rt.get("traces", [])
              if _trace_key(t) in esc_tk or t.get("net_id") not in bad_net_ids]
    keep_v = [v for v in rt.get("vias", [])
              if _via_key(v) in esc_vk or v.get("net_id") not in bad_net_ids]

    have_tk = {_trace_key(t) for t in keep_t}
    have_vk = {_via_key(v) for v in keep_v}
    for t in escapes.get("traces", []):
        if _trace_key(t) not in have_tk:
            keep_t.append(t)
    for v in escapes.get("vias", []):
        if _via_key(v) not in have_vk:
            keep_v.append(v)
    return {"traces": keep_t, "vias": keep_v,
            "keepouts": escapes.get("keepouts", [])}


def _analyze(routed: dict, netlist: dict, exclude_ids: set[str],
             name_to_id: dict[str, str],
             drc_data_fn: Callable[[dict], dict | None]
             ) -> tuple[set[str], list[tuple[float, float]]] | None:
    """(bad_net_ids, keepout_points) for a routed board, or None if DRC couldn't
    run. bad = (shorts ∪ clearance ∪ incomplete) minus excluded and minus the
    pad-nets we keep protected (so their pad-keepout doesn't disconnect them)."""
    data = drc_data_fn(routed)
    if data is None:
        return None
    bad_names, keepouts, protect_names = parse_cleanup_drc(data)
    bad = {name_to_id.get(n, n) for n in bad_names}
    bad |= incomplete_net_ids(routed, netlist)
    bad -= exclude_ids
    bad -= {name_to_id.get(n, n) for n in protect_names}
    bad.discard(None)
    return bad, keepouts


def _keepout_feature_index(routed: dict, netlist: dict
                           ) -> list[tuple[float, float, float]]:
    """(x, y, extent_mm) for every pad and via, so a short-cleanup keepout can be
    sized to the feature it must wall off. A through-hole pad spans ~1.7 mm; a
    fixed 0.8 mm keepout (0.4 mm radius) only dots its centre and leaves the
    re-routed net free to re-clip the annular ring — the 'same short every round'
    loop. Sizing to the real extent forces the re-route fully clear."""
    feats: list[tuple[float, float, float]] = []
    try:
        from optimizers.pad_geometry import build_pad_map
        for p in build_pad_map(routed, netlist).values():
            feats.append((p.x_mm, p.y_mm, max(p.pad_width_mm, p.pad_height_mm)))
    except Exception:
        pass
    for v in routed.get("routing", {}).get("vias", []):
        feats.append((v["x_mm"], v["y_mm"], v.get("diameter_mm", 0.6)))
    return feats


def _sized_keepout_diameter(x: float, y: float,
                            feats: list[tuple[float, float, float]],
                            clearance_mm: float, default_mm: float) -> float:
    """Keepout diameter covering the pad/via at (x, y) PLUS clearance on each
    side, so the re-route is pushed clear of the whole feature — not a dot at its
    centre. Falls back to default_mm when the locus isn't on a known feature
    (e.g. a trace-vs-trace clearance nick)."""
    best_ext = None
    best_d = 0.30  # match the violation locus to a feature within 0.3 mm
    for (fx, fy, ext) in feats:
        d = math.hypot(fx - x, fy - y)
        if d <= best_d:
            best_d = d
            best_ext = ext
    if best_ext is None:
        return default_mm
    return max(default_mm, best_ext + 2 * clearance_mm)


def cleanup_shorts(
    routed: dict,
    netlist: dict,
    *,
    escapes: dict,
    exclude_nets: tuple[str, ...] = (),
    route_fn: Callable[[dict], dict | None],
    drc_data_fn: Callable[[dict], dict | None],
    max_iterations: int = 2,
    keepout_diameter_mm: float = 0.8,
    log: Callable[[str], None] | None = None,
) -> tuple[dict, set[str]]:
    """Iteratively rip+re-route the shorting/clearance/incomplete nets until none
    remain (per DRC) or no further progress. Each pass walls off the violation
    loci with keepouts, so a re-routed net is forced clear of the pad/trace it
    shorted instead of re-making the same mistake. Returns (best_routed, bad_ids).

    route_fn(fixed_routing) -> routed|None routes the un-protected nets.
    drc_data_fn(routed) -> parsed kicad-cli DRC json, or None if unavailable.
    """
    _log = log or (lambda *_a: None)
    name_to_id: dict[str, str] = {}
    for e in netlist.get("elements", []):
        if e.get("element_type") == "net":
            nid = e["net_id"]
            name_to_id[e.get("name", nid)] = nid
            name_to_id[nid] = nid
    exclude_ids = {name_to_id.get(n, n) for n in exclude_nets}

    best = routed
    first = _analyze(best, netlist, exclude_ids, name_to_id, drc_data_fn)
    if first is None:
        _log("  Short cleanup skipped: kicad-cli DRC unavailable")
        return best, set()
    best_bad, _ = first

    feats = _keepout_feature_index(routed, netlist)
    clearance_mm = routed.get("routing", {}).get("config", {}).get(
        "clearance_mm", 0.2)

    acc_keepouts: list[dict] = []
    seen_kpts: set[tuple[float, float]] = set()
    for i in range(max_iterations):
        if not best_bad:
            break
        cur = _analyze(best, netlist, exclude_ids, name_to_id, drc_data_fn)
        if cur is None:
            break
        _, kpts = cur
        for (x, y) in kpts:
            if (x, y) not in seen_kpts:
                seen_kpts.add((x, y))
                dia = _sized_keepout_diameter(x, y, feats, clearance_mm,
                                              keepout_diameter_mm)
                acc_keepouts.append({"x_mm": x, "y_mm": y, "diameter_mm": dia})
        _log(f"  Short cleanup pass {i + 1}: re-routing {len(best_bad)} net(s) "
             f"with everything else (incl. escapes) protected and "
             f"{len(acc_keepouts)} keepout(s) at the violation site(s)")
        fixed = build_protected_wiring(best, escapes, best_bad)
        fixed["keepouts"] = list(fixed.get("keepouts", [])) + acc_keepouts
        cand = route_fn(fixed)
        if cand is None:
            break
        cand_res = _analyze(cand, netlist, exclude_ids, name_to_id, drc_data_fn)
        cand_bad = cand_res[0] if cand_res else None
        if cand_bad is None or len(cand_bad) >= len(best_bad):
            _log("  Short cleanup: no improvement — keeping previous route")
            break
        _log(f"  Short cleanup: bad nets {len(best_bad)} -> {len(cand_bad)}")
        best, best_bad = cand, cand_bad

    return best, best_bad
