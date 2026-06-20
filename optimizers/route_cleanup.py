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
import os
import shutil
import subprocess
from pathlib import Path
from typing import Callable

from validators.validate_routing import incomplete_net_ids


def find_kicad_cli() -> str | None:
    """Locate a usable ``kicad-cli`` binary, or None. Honours ``PCB_KICAD_CLI``,
    then ``PATH``, then the macOS app bundle."""
    env = os.environ.get("PCB_KICAD_CLI")
    if env and Path(env).exists():
        return env
    found = shutil.which("kicad-cli")
    if found:
        return found
    mac = "/Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli"
    if Path(mac).exists():
        return mac
    return None


def drc_shorting_net_names(pcb_path: str | Path, kicad_cli: str,
                           *, timeout: int = 300) -> set[str] | None:
    """Run ``kicad-cli pcb drc`` and return the net NAMES involved in
    ``shorting_items``. None if the tool can't be run (caller treats as
    "skip cleanup"). An empty set means "ran, no shorts"."""
    pcb_path = Path(pcb_path)
    out = pcb_path.with_suffix(".cleanup_drc.json")
    try:
        subprocess.run(
            [kicad_cli, "pcb", "drc", "--format", "json", "--output", str(out),
             "--severity-error", str(pcb_path)],
            capture_output=True, timeout=timeout, check=False)
        data = json.loads(out.read_text())
    except (FileNotFoundError, OSError, subprocess.SubprocessError,
            json.JSONDecodeError, ValueError):
        return None
    finally:
        try:
            out.unlink()
        except OSError:
            pass
    return _parse_shorting_net_names(data)


def _parse_shorting_net_names(drc_data: dict) -> set[str]:
    """Net names appearing in ``shorting_items`` of a kicad-cli DRC report.
    Item descriptions read like ``Track [FB_DIV] on F.Cu …`` / ``Pad 1
    [GATE_Q3] of Q3 …`` — the net name is the first bracketed token."""
    names: set[str] = set()
    for v in drc_data.get("violations", []):
        if v.get("type") != "shorting_items":
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


def _bad_net_ids(routed: dict, netlist: dict, exclude_ids: set[str],
                 name_to_id: dict[str, str],
                 drc_net_names_fn: Callable[[dict], set[str] | None]
                 ) -> set[str] | None:
    """Union of DRC-shorting nets and incomplete nets, minus excluded ones.
    None if DRC couldn't run (signal to skip cleanup entirely)."""
    short_names = drc_net_names_fn(routed)
    if short_names is None:
        return None
    bad = {name_to_id.get(n, n) for n in short_names}
    bad |= incomplete_net_ids(routed, netlist)
    bad -= exclude_ids
    bad.discard(None)
    return bad


def cleanup_shorts(
    routed: dict,
    netlist: dict,
    *,
    escapes: dict,
    exclude_nets: tuple[str, ...] = (),
    route_fn: Callable[[dict], dict | None],
    drc_net_names_fn: Callable[[dict], set[str] | None],
    max_iterations: int = 2,
    log: Callable[[str], None] | None = None,
) -> tuple[dict, set[str]]:
    """Iteratively rip+re-route the shorting/incomplete nets until no shorts
    remain (per DRC) or no further progress. Returns (best_routed, bad_net_ids).

    route_fn(fixed_routing) -> routed|None routes the un-protected nets.
    drc_net_names_fn(routed) -> shorting net names, or None if DRC unavailable.
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
    best_bad = _bad_net_ids(best, netlist, exclude_ids, name_to_id, drc_net_names_fn)
    if best_bad is None:
        _log("  Short cleanup skipped: kicad-cli DRC unavailable")
        return best, set()

    for i in range(max_iterations):
        if not best_bad:
            break
        _log(f"  Short cleanup pass {i + 1}: re-routing {len(best_bad)} net(s) "
             f"with everything else (incl. escapes) protected")
        fixed = build_protected_wiring(best, escapes, best_bad)
        cand = route_fn(fixed)
        if cand is None:
            break
        cand_bad = _bad_net_ids(cand, netlist, exclude_ids, name_to_id,
                                drc_net_names_fn)
        if cand_bad is None or len(cand_bad) >= len(best_bad):
            _log("  Short cleanup: no improvement — keeping previous route")
            break
        _log(f"  Short cleanup: bad nets {len(best_bad)} -> {len(cand_bad)}")
        best, best_bad = cand, cand_bad

    return best, best_bad
