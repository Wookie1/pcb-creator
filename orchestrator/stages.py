"""Deterministic, file-based pipeline stages — no LLM, no vision critic.

Each stage reads and writes the project directory (the same file handoff the
full pipeline uses) and returns a structured result dict.  These are the units
an external agent (e.g. Hermes) orchestrates directly: it supplies the circuit
intelligence and its own QA loop, while pcb-creator provides fast, inspectable,
deterministic placement / routing / DRC / export.

The full LLM-driven runner (`runner.run_workflow`) also calls run_routing so
there is a single routing implementation.

Conventions
-----------
project_dir : Path to the project folder (…/projects/<name>)
project_name: slug; files are <project_name>_<suffix>.json inside project_dir
config      : OrchestratorConfig (carries router engine, DFM, timeouts)
"""

from __future__ import annotations

import json
import sys
from pathlib import Path


def _p(project_dir: Path, project_name: str, suffix: str) -> Path:
    return project_dir / f"{project_name}_{suffix}.json"


def _load(path: Path) -> dict:
    return json.loads(path.read_text())


# ---------------------------------------------------------------------------
# Placement
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Placement pins: agent-fixed component positions, applied on every placement
# run and validated immediately when set.
# ---------------------------------------------------------------------------

def _pins_path(project_dir: Path, project_name: str) -> Path:
    return project_dir / f"{project_name}_placement_pins.json"


def load_placement_pins(project_dir: Path, project_name: str) -> dict:
    path = _pins_path(project_dir, project_name)
    if path.exists():
        return json.loads(path.read_text())
    return {}


def _resolve_layers(project_dir: Path, project_name: str) -> int:
    """Board layer count from existing placement, circuit draft, or
    requirements (default 2)."""
    for path, key in (
        (_p(project_dir, project_name, "placement"), "board"),
        (project_dir / f"{project_name}_circuit_draft.json", "board"),
        (_p(project_dir, project_name, "requirements"), "board"),
    ):
        if path.exists():
            try:
                n = _load(path).get(key, {}).get("layers")
                if n in (2, 4):
                    return int(n)
            except Exception:
                pass
    return 2


def _resolve_board_dims(project_dir: Path, project_name: str) -> tuple[float | None, float | None]:
    """Board dims from existing placement, circuit draft, or requirements."""
    placement_path = _p(project_dir, project_name, "placement")
    if placement_path.exists():
        try:
            b = _load(placement_path).get("board", {})
            if b.get("width_mm") and b.get("height_mm"):
                return b["width_mm"], b["height_mm"]
        except Exception:
            pass
    draft_path = project_dir / f"{project_name}_circuit_draft.json"
    if draft_path.exists():
        try:
            b = json.loads(draft_path.read_text()).get("board", {})
            if b.get("width_mm") and b.get("height_mm"):
                return b["width_mm"], b["height_mm"]
        except Exception:
            pass
    req_path = _p(project_dir, project_name, "requirements")
    if req_path.exists():
        try:
            b = _load(req_path).get("board", {})
            return b.get("width_mm"), b.get("height_mm")
        except Exception:
            pass
    return None, None


def _suggest_free_position(x_mm, y_mm, rotation_deg, package, pin_count,
                          other_boxes, bw, bh, edge_clearance, min_clearance):
    """Find the nearest board coordinate where the component is in-bounds AND
    clear of every already-pinned component, so a rejected place_component can
    hand back a concrete retry instead of just "move it".

    Spiral outward from the requested point on a coarse grid; return the first
    valid (x, y) rounded to 0.5mm, or None if the board has no room."""
    from optimizers.placement_optimizer import (
        _get_pad_extent_box, _boxes_overlap_with_clearance,
    )

    def _valid(cx, cy):
        b = _get_pad_extent_box(cx, cy, 1.0, 1.0, rotation_deg, package,
                                pin_count)
        if (b[0] < edge_clearance - 0.01 or b[1] < edge_clearance - 0.01
                or b[2] > bw - edge_clearance + 0.01
                or b[3] > bh - edge_clearance + 0.01):
            return False
        return not any(_boxes_overlap_with_clearance(b, ob, min_clearance)
                       for ob in other_boxes)

    # Clamp the search origin into the board so an off-edge request still
    # spirals from the nearest in-bounds point.
    ox = min(max(float(x_mm), edge_clearance), bw - edge_clearance)
    oy = min(max(float(y_mm), edge_clearance), bh - edge_clearance)
    step = 2.0
    max_r = max(bw, bh)
    r = 0.0
    while r <= max_r:
        if r == 0.0:
            cands = [(ox, oy)]
        else:
            cands = []
            n = max(1, int((2 * r) / step))
            for k in range(n + 1):
                t = -r + k * (2 * r / n)
                cands += [(ox + t, oy - r), (ox + t, oy + r),
                          (ox - r, oy + t), (ox + r, oy + t)]
        # Nearest candidates first within the ring.
        for cx, cy in sorted(cands, key=lambda c: (c[0] - ox) ** 2
                             + (c[1] - oy) ** 2):
            if edge_clearance <= cx <= bw - edge_clearance and \
                    edge_clearance <= cy <= bh - edge_clearance and _valid(cx, cy):
                return (round(cx * 2) / 2, round(cy * 2) / 2)
        r += step
    return None


def set_placement_pin(project_dir: Path, project_name: str, designator: str,
                      x_mm: float, y_mm: float, rotation_deg: int = 0,
                      layer: str = "top") -> dict:
    """Pin a component at fixed coordinates; validated immediately.

    Origin is the top-left board corner; x grows right, y grows down
    (same frame as the placement JSON). Returns {"ok": bool, ...}.
    """
    netlist_path = _p(project_dir, project_name, "netlist")
    if not netlist_path.exists():
        return {"ok": False, "code": "no_netlist",
                "error": f"No netlist for '{project_name}' — import or build "
                         "the circuit first."}
    netlist = _load(netlist_path)
    comp = None
    pin_count = 0
    for elem in netlist.get("elements", []):
        if (elem.get("element_type") == "component"
                and elem.get("designator") == designator):
            comp = elem
    if comp is None:
        known = sorted(e.get("designator", "") for e in netlist.get("elements", [])
                       if e.get("element_type") == "component")
        return {"ok": False, "code": "unknown_designator",
                "error": f"No component '{designator}' in the netlist. "
                         f"Known: {', '.join(known)}."}
    for elem in netlist.get("elements", []):
        if (elem.get("element_type") == "port"
                and elem.get("component_id") == comp.get("component_id")):
            pin_count += 1

    if rotation_deg not in (0, 90, 180, 270):
        return {"ok": False, "code": "bad_rotation",
                "error": "rotation_deg must be 0, 90, 180, or 270."}
    if layer not in ("top", "bottom"):
        return {"ok": False, "code": "bad_layer",
                "error": "layer must be 'top' or 'bottom'."}

    from optimizers.placement_optimizer import (
        _get_pad_extent_box, _boxes_overlap_with_clearance,
        BOARD_EDGE_CLEARANCE_MM, MIN_CLEARANCE_MM,
    )
    package = comp.get("package", "")
    box = _get_pad_extent_box(float(x_mm), float(y_mm), 1.0, 1.0,
                              rotation_deg, package, pin_count)

    # Other pinned components on this layer — needed by both the conflict check
    # and the free-position suggester.
    pins = load_placement_pins(project_dir, project_name)
    des_to_pkg = {e.get("designator"): e.get("package", "")
                  for e in netlist.get("elements", [])
                  if e.get("element_type") == "component"}
    other_boxes = [
        _get_pad_extent_box(p["x_mm"], p["y_mm"], 1.0, 1.0,
                            p.get("rotation_deg", 0),
                            des_to_pkg.get(other_des, ""), 2)
        for other_des, p in pins.items()
        if other_des != designator and p.get("layer", "top") == layer
    ]

    # Board-bounds check (when dimensions are known)
    bw, bh = _resolve_board_dims(project_dir, project_name)
    if bw and bh:
        ec = BOARD_EDGE_CLEARANCE_MM
        if (box[0] < ec - 0.01 or box[1] < ec - 0.01
                or box[2] > bw - ec + 0.01 or box[3] > bh - ec + 0.01):
            sug = _suggest_free_position(
                x_mm, y_mm, rotation_deg, package, pin_count,
                other_boxes, bw, bh, ec, MIN_CLEARANCE_MM)
            r = {"ok": False, "code": "out_of_bounds",
                 "error": f"{designator} at ({x_mm}, {y_mm}) rot "
                          f"{rotation_deg} has pads spanning "
                          f"x [{box[0]:.1f}, {box[2]:.1f}], "
                          f"y [{box[1]:.1f}, {box[3]:.1f}] — outside the "
                          f"{bw}x{bh}mm board minus {ec}mm edge clearance. "
                          "Move it inward, rotate it, or enlarge the board."}
            if sug:
                r["suggested_x_mm"], r["suggested_y_mm"] = sug
                r["error"] += (f" A free spot for it is "
                               f"({sug[0]}, {sug[1]}).")
            return r

    # Conflict check against other pinned components
    for other_des, p in pins.items():
        if other_des == designator or p.get("layer", "top") != layer:
            continue
        other_box = _get_pad_extent_box(
            p["x_mm"], p["y_mm"], 1.0, 1.0, p.get("rotation_deg", 0),
            des_to_pkg.get(other_des, ""), 2)
        if _boxes_overlap_with_clearance(box, other_box, MIN_CLEARANCE_MM):
            sug = (_suggest_free_position(
                x_mm, y_mm, rotation_deg, package, pin_count, other_boxes,
                bw, bh, BOARD_EDGE_CLEARANCE_MM, MIN_CLEARANCE_MM)
                if bw and bh else None)
            r = {"ok": False, "code": "pin_overlap",
                 "error": (
                     f"{designator}'s pads span x [{box[0]:.1f}, {box[2]:.1f}], "
                     f"y [{box[1]:.1f}, {box[3]:.1f}] and overlap already-pinned "
                     f"{other_des} (pads x [{other_box[0]:.1f}, {other_box[2]:.1f}], "
                     f"y [{other_box[1]:.1f}, {other_box[3]:.1f}]). NOTE: these are "
                     f"footprint pad/keepout EXTENTS including clearance, NOT centre "
                     f"points — so parts whose centres look far apart can still "
                     f"collide (e.g. a 3.2mm M3 mounting hole spans ~6mm to clear "
                     f"the screw head). The check is correct; move one further "
                     f"apart (do not unpin and hope).")}
            if sug:
                r["suggested_x_mm"], r["suggested_y_mm"] = sug
                r["error"] += (f" A free spot for {designator} is "
                               f"({sug[0]}, {sug[1]}).")
            return r

    pins[designator] = {"x_mm": float(x_mm), "y_mm": float(y_mm),
                        "rotation_deg": rotation_deg, "layer": layer}
    _pins_path(project_dir, project_name).write_text(json.dumps(pins, indent=2))
    return {"ok": True, "designator": designator, "pinned": pins[designator],
            "pinned_count": len(pins)}


def _user_source_in_placement(project_dir: Path, project_name: str) -> set[str]:
    """Designators flagged placement_source=="user" in the current placement
    file — a SECOND pin source besides the durable store (set from requirements
    hints, or written by set_component_positions/place_component). run_placement
    re-scrapes and re-injects these, so they must be cleared too or an unpinned
    component is silently resurrected on the next placement."""
    placement_path = _p(project_dir, project_name, "placement")
    if not placement_path.exists():
        return set()
    try:
        pl = _load(placement_path)
    except Exception:
        return set()
    return {it.get("designator") for it in pl.get("placements", [])
            if it.get("placement_source") == "user"}


def _clear_user_source_in_placement(project_dir: Path, project_name: str,
                                    designators: set[str] | None = None) -> set[str]:
    """Reset placement_source 'user' → 'auto' in the placement file (for the
    given designators, or all when None). Returns the set actually reset."""
    placement_path = _p(project_dir, project_name, "placement")
    if not placement_path.exists():
        return set()
    try:
        pl = _load(placement_path)
    except Exception:
        return set()
    reset = set()
    for it in pl.get("placements", []):
        if it.get("placement_source") == "user" and (
                designators is None or it.get("designator") in designators):
            it["placement_source"] = "auto"
            reset.add(it.get("designator"))
    if reset:
        placement_path.write_text(json.dumps(pl, indent=2))
    return reset


def all_pinned_designators(project_dir: Path, project_name: str) -> list[str]:
    """The TRUE pinned set: union of the durable pin store and the placement
    file's placement_source=="user" flags. Use this for agent-facing visibility
    so a pin set by either path is reported (and neither resurrects silently)."""
    return sorted(set(load_placement_pins(project_dir, project_name))
                  | _user_source_in_placement(project_dir, project_name))


def clear_placement_pin(project_dir: Path, project_name: str,
                        designator: str) -> dict:
    """Unpin a component from BOTH pin sources — the durable store AND the
    placement file's user flag — so optimize_placement won't resurrect it."""
    pins = load_placement_pins(project_dir, project_name)
    in_durable = designator in pins
    if in_durable:
        del pins[designator]
        _pins_path(project_dir, project_name).write_text(json.dumps(pins, indent=2))
    reset = _clear_user_source_in_placement(project_dir, project_name, {designator})
    if not in_durable and not reset:
        remaining = all_pinned_designators(project_dir, project_name)
        return {"ok": False, "code": "not_pinned",
                "error": f"'{designator}' is not pinned. Pinned: "
                         f"{', '.join(remaining) or '(none)'}."}
    remaining = all_pinned_designators(project_dir, project_name)
    return {"ok": True, "designator": designator,
            "pinned_count": len(remaining), "pinned": remaining}


def clear_all_placement_pins(project_dir: Path, project_name: str) -> dict:
    """Unpin EVERY component — wipe the durable store and reset all user flags in
    the placement file — so the next optimize_placement is free to move all."""
    pins = load_placement_pins(project_dir, project_name)
    _pins_path(project_dir, project_name).write_text(json.dumps({}, indent=2))
    reset = _clear_user_source_in_placement(project_dir, project_name, None)
    cleared = sorted(set(pins) | reset)
    return {"ok": True, "cleared": cleared, "cleared_count": len(cleared)}


def run_placement(
    project_dir: Path,
    project_name: str,
    config,
    board_width_mm: float | None = None,
    board_height_mm: float | None = None,
    seed: int | None = None,
    extra_clearance_mm: float = 0.0,
    congestion_weight: float = 0.0,
    two_sided: bool | None = None,
    plane_layers: int | None = None,
    layers: int | None = None,
    escape_weight: float | None = None,
    focus_components: list[str] | None = None,
) -> dict:
    """Deterministic grid placement → repair → SA optimize.

    Reads <project>_netlist.json, writes <project>_placement.json.

    Board dimensions: taken from board_width_mm/board_height_mm if given, else
    from an existing placement's board block (re-optimize case), else from the
    requirements file, else a default.  A KiCad .net import carries no board
    size, so the caller should pass dimensions on first placement.

    Returns:
        {success, component_count, wire_length_mm, crossings,
         board_width_mm, board_height_mm, placement_path}
    """
    from optimizers.initial_placement import generate_grid_placement
    from optimizers.placement_optimizer import (
        optimize_placement, repair_placement, SAConfig,
    )

    netlist_path = _p(project_dir, project_name, "netlist")
    if not netlist_path.exists():
        return {"success": False, "error": f"No netlist found at {netlist_path.name}"}
    netlist = _load(netlist_path)

    # Footprint resolution gate — refuse to place if any component would fall
    # back to a placeholder.  This is the deterministic equivalent of the
    # LLM-flow's footprint verification: the agent gets a structured list of
    # unresolved components to fix (correct the package name, set
    # PCB_KICAD_LIBRARY_PATH, or call provide_footprint) before retrying.
    from validators.verify_footprints import verify_footprints
    unresolved = verify_footprints(netlist)
    if unresolved:
        return {
            "success": False,
            "error": (
                f"{len(unresolved)} component(s) have unresolved footprints and "
                "would become 3mm placeholders. Fix each one (correct the package "
                "name, set PCB_KICAD_LIBRARY_PATH, or call provide_footprint), then "
                "re-run placement."
            ),
            "unresolved_footprints": unresolved,
        }

    placement_path = _p(project_dir, project_name, "placement")

    # Resolve board dimensions and preserve any user-pinned positions + layer count
    # from an existing placement before we regenerate the grid seed.
    bw, bh = board_width_mm, board_height_mm
    user_pinned: dict[str, dict] = {}  # designator → placement item with placement_source=="user"
    existing_layers: int = 2           # default; overridden if existing placement has more
    if placement_path.exists():
        try:
            existing_pl = _load(placement_path)
            existing_board = existing_pl.get("board", {})
            bw = bw or existing_board.get("width_mm")
            bh = bh or existing_board.get("height_mm")
            existing_layers = existing_board.get("layers", 2)
            for item in existing_pl.get("placements", []):
                if item.get("placement_source") == "user":
                    user_pinned[item["designator"]] = item
        except Exception:
            pass
    if bw is None or bh is None:
        req_path = _p(project_dir, project_name, "requirements")
        if req_path.exists():
            try:
                rb = _load(req_path).get("board", {})
                bw = bw or rb.get("width_mm")
                bh = bh or rb.get("height_mm")
            except Exception:
                pass
    if bw is None:
        bw = 50.0
    if bh is None:
        bh = 50.0

    # Resolve layer count: explicit `layers` arg wins, else existing placement →
    # circuit draft → requirements (default 2).
    if layers in (2, 4):
        num_layers = int(layers)
    else:
        num_layers = _resolve_layers(project_dir, project_name)

    # Auto-promote to 4 layers when an inner-plane stackup is requested. The
    # plane_layers parameter only has meaning on a 4-layer board; without this
    # guard, passing plane_layers on a 2-layer board was silently ignored —
    # which is exactly how a board that needed 4 layers got routed (and
    # over-crammed) on 2 (morgan_carrier_v14: plane_layers=0 but layers stayed
    # 2, producing shorts/unconnected that read as a "fake" 100% route).
    layers_promoted = False
    if plane_layers is not None and num_layers < 4:
        num_layers = 4
        layers_promoted = True

    # Deterministic seed placement
    placement = generate_grid_placement(netlist, bw, bh, project_name,
                                        layers=num_layers)
    if placement is None:
        return {"success": False, "error": "No components with resolvable footprints"}

    # Re-inject user-pinned positions captured from the existing placement file
    # (e.g. set via set_component_positions, which writes the placement directly)
    # so repair + SA optimise around them rather than discarding them. Only the
    # fields the user set are overwritten; footprint_width/height come from the
    # freshly resolved footprint def. (num_layers was already applied by
    # generate_grid_placement above, so no separate layer-count restore needed.)
    if user_pinned:
        for item in placement["placements"]:
            pin = user_pinned.get(item["designator"])
            if pin is None:
                continue
            item["x_mm"] = pin["x_mm"]
            item["y_mm"] = pin["y_mm"]
            item["rotation_deg"] = pin.get("rotation_deg", item["rotation_deg"])
            item["layer"] = pin.get("layer", item["layer"])
            item["placement_source"] = "user"

    # Apply agent-set placement pins (place_component store): fixed position,
    # marked placement_source=user so repair/optimize never move them. Applied
    # last so the explicit place_component store is authoritative.
    pins = load_placement_pins(project_dir, project_name)
    if pins:
        for item in placement.get("placements", []):
            pin = pins.get(item["designator"])
            if pin:
                item["x_mm"] = pin["x_mm"]
                item["y_mm"] = pin["y_mm"]
                item["rotation_deg"] = pin.get("rotation_deg", 0)
                item["layer"] = pin.get("layer", "top")
                item["placement_source"] = "user"

    # Two-sided placement: explicit arg wins; otherwise reuse the previous
    # placement's setting (so the routing retry loop re-places consistently).
    if two_sided is None:
        two_sided = bool(placement.get("board", {}).get("two_sided", False))
        if placement_path.exists():
            try:
                two_sided = two_sided or bool(
                    _load(placement_path).get("board", {}).get("two_sided"))
            except Exception:
                pass
    placement.setdefault("board", {})["two_sided"] = two_sided

    # Inner-layer stackup (4-layer): how many inner layers are PLANES vs signal.
    # Resolve from requirements (board.plane_layers); default 2 (both planes).
    # Carried on the placement board so routing uses the same stackup.
    if num_layers >= 4:
        pl = 2
        if plane_layers in (0, 1, 2):
            pl = int(plane_layers)
        else:
            req_path = _p(project_dir, project_name, "requirements")
            if req_path.exists():
                try:
                    rpl = _load(req_path).get("board", {}).get("plane_layers")
                    if rpl in (0, 1, 2):
                        pl = int(rpl)
                except Exception:
                    pass
            # else reuse an existing placement's stackup choice
            elif placement_path.exists():
                try:
                    epl = _load(placement_path).get("board", {}).get("plane_layers")
                    if epl in (0, 1, 2):
                        pl = int(epl)
                except Exception:
                    pass
        placement["board"]["plane_layers"] = pl

    # Repair overlaps/boundary, then optimize.  Thread the seed through both
    # so a given seed yields a fully reproducible placement.
    from optimizers.placement_optimizer import (
        MIN_CLEARANCE_MM, find_placement_violations,
    )
    clearance = MIN_CLEARANCE_MM + max(0.0, extra_clearance_mm)
    # Flipping is only attractive to the annealer through the layer-aware
    # congestion term — give it a floor when two-sided is on.
    if two_sided and congestion_weight <= 0:
        congestion_weight = 2.0
    # Bottom-side reluctance: on a 2-layer board the bottom IS the router's
    # escape layer, so flips must clearly pay for themselves (high penalty).
    # On a 4-layer board the inner layers carry power/ground planes and both
    # outer layers are free for signal routing, so the bottom is cheap — let
    # the optimizer use it freely (low penalty, just a slight top preference
    # for assembly cost).
    bottom_penalty = 0.5 if num_layers >= 4 else 4.0
    # Escape-halo (enhancement A): size the fanout annulus to the board's actual
    # routing rules (trace + clearance). Dense/fine-pitch parts self-qualify;
    # focus_components (the routing-feedback lever C) reserve extra space even
    # if not intrinsically dense.
    rk = _build_router_kwargs(project_dir, project_name)
    track_pitch = (rk.get("trace_width_signal_mm", 0.25)
                   + rk.get("clearance_mm", 0.2))

    # Repair overlaps/boundary, then optimize. A sparse board can still leave a
    # few movable overlaps that a different anneal seed resolves, so when the
    # caller hasn't fixed a seed we try a handful and keep the first clean result
    # (or the fewest-violation one). An explicit seed is honoured as-is for full
    # reproducibility, and pinned-only conflicts are never retried — a different
    # seed cannot move fixed parts.
    import copy as _copy
    grid_seed = _copy.deepcopy(placement)
    seed_candidates = [seed] if seed is not None else [0, 1, 2, 3, 4]
    best_placement = None
    best_violations = None
    for _seed in seed_candidates:
        cand = repair_placement(_copy.deepcopy(grid_seed), netlist,
                                clearance=clearance, seed=_seed,
                                two_sided=two_sided)
        sa = SAConfig(seed=_seed, min_clearance_mm=clearance,
                      congestion_weight=congestion_weight,
                      two_sided=two_sided, bottom_penalty=bottom_penalty,
                      escape_track_pitch_mm=track_pitch,
                      escape_weight=(6.0 if escape_weight is None else escape_weight),
                      focus_components=tuple(focus_components or ()))
        cand = optimize_placement(cand, netlist, sa)
        v = find_placement_violations(cand, netlist, clearance=MIN_CLEARANCE_MM)
        if best_violations is None or v["count"] < best_violations["count"]:
            best_placement, best_violations = cand, v
        if v["count"] == 0:
            break
        # A different seed can't fix pinned/out-of-bounds fixed parts.
        if ([e for e in v["out_of_bounds"] if e["pinned"]]
                + [o for o in v["overlaps"] if o["pinned"]]):
            break
    placement, violations = best_placement, best_violations

    placement_path.write_text(json.dumps(placement, indent=2))

    # Final constraint check — repair logs a warning when it cannot resolve
    # everything, but the caller must SEE the violations (pinned components
    # overlapping each other or hanging past the edge can never be fixed by
    # moving the movable ones).
    if violations["count"]:
        pinned_involved = ([v for v in violations["out_of_bounds"] if v["pinned"]]
                           + [v for v in violations["overlaps"] if v["pinned"]])
        details = ([v["detail"] for v in violations["out_of_bounds"]]
                   + [v["detail"] for v in violations["overlaps"]])
        return {
            "success": False,
            "error": (f"Placement has {violations['count']} unresolved "
                      "constraint violation(s)"
                      + (" — fixed/pinned components are involved; they are "
                         "never moved automatically, so adjust their "
                         "coordinates (place_component) or unpin them "
                         "(unplace_component)" if pinned_involved else
                         " — the board is likely too dense; enlarge it and "
                         "re-run") + "."),
            "violations": violations,
            "violation_details": details[:15],
            "placement_path": str(placement_path),
        }

    # Metrics
    from optimizers.ratsnest import build_connectivity, IncrementalCost
    nets = build_connectivity(netlist)
    positions = {p["designator"]: (p["x_mm"], p["y_mm"]) for p in placement["placements"]}
    ev = IncrementalCost(nets, positions)

    return {
        "success": True,
        "component_count": len(placement["placements"]),
        "wire_length_mm": round(ev.total_wire, 1),
        "crossings": ev.total_cross,
        "board_width_mm": bw,
        "board_height_mm": bh,
        "layers": num_layers,
        "plane_layers": placement["board"].get("plane_layers"),
        "layers_promoted": layers_promoted,
        # The TRUE pinned set (durable store ∪ placement-file user flags) so the
        # agent always knows what is fixed and never guesses about a "stale"
        # pin. Clear with unplace_component (one) or clear_all_pins (all).
        "pinned_components": all_pinned_designators(project_dir, project_name),
        "placement_path": str(placement_path),
    }


# ---------------------------------------------------------------------------
# Routing (lifted from runner.run_workflow so there is one implementation)
# ---------------------------------------------------------------------------

# Below this minimum pad pitch a board is "fine-pitch" and needs tightened
# routing rules (0.5mm-pitch QFN/connectors land here; 0.65mm parts too).
FINE_PITCH_THRESHOLD_MM = 0.8


def _min_pad_pitch(project_dir: Path, project_name: str) -> float | None:
    """Smallest centre-to-centre distance between adjacent pads of any
    component on the board (a proxy for the tightest part's pitch). None if
    it can't be determined."""
    netlist_path = _p(project_dir, project_name, "netlist")
    if not netlist_path.exists():
        return None
    try:
        import math
        from optimizers.pad_geometry import get_footprint_def
        netlist = _load(netlist_path)
        pin_counts: dict[str, int] = {}
        for e in netlist.get("elements", []):
            if e.get("element_type") == "port":
                pin_counts[e.get("component_id", "")] = \
                    pin_counts.get(e.get("component_id", ""), 0) + 1
        best: float | None = None
        for e in netlist.get("elements", []):
            if e.get("element_type") != "component":
                continue
            fp = get_footprint_def(e.get("package", ""),
                                   pin_counts.get(e.get("component_id", ""), 0))
            if fp is None or len(fp.pin_offsets) < 2:
                continue
            pts = list(fp.pin_offsets.values())
            # Nearest-neighbour distance for this footprint
            local = min(
                (math.hypot(pts[i][0] - pts[j][0], pts[i][1] - pts[j][1])
                 for i in range(len(pts)) for j in range(i + 1, len(pts))),
                default=None)
            if local is not None and (best is None or local < best):
                best = local
        return best
    except Exception:
        return None


def _build_router_kwargs(project_dir: Path, project_name: str, log=None) -> dict:
    """Derive router design rules from the requirements/DFM profile (if any).

    log: optional callable(str) — when provided, emits the DFM-profile line the
    CLI runner used to print.  None (default) keeps this silent for MCP callers.
    """
    _log = log or (lambda *_a: None)
    copper_oz = 0.5
    mfg_rules: dict = {}
    req_path = _p(project_dir, project_name, "requirements")
    if req_path.exists():
        try:
            req_data = _load(req_path)
            copper_oz = req_data.get("board", {}).get("copper_weight_oz", 0.5)
            mfg = req_data.get("manufacturing", {})
            if mfg:
                manufacturer = mfg.get("manufacturer", "")
                if manufacturer:
                    from validators.engineering_constants import get_dfm_profile
                    mfg_rules = get_dfm_profile(manufacturer)
                    _log(f"  DFM profile: {mfg_rules.get('description', manufacturer)}")
                for key in ("trace_width_min_mm", "clearance_min_mm",
                            "via_drill_min_mm", "via_diameter_min_mm"):
                    if key in mfg:
                        mfg_rules[key] = mfg[key]
        except Exception:
            pass

    # Is there a fine-pitch part on the board? If the tightest pad pitch is
    # small, the comfortable default rules (0.25mm trace / 0.2mm clearance /
    # 0.6mm via) physically cannot escape the pads or fan out without
    # clearance/short violations — so drop to the manufacturer's MINIMUM
    # trace/clearance/via near such parts. The defaults stay for ordinary
    # boards (finer rules are less robust in fab, so only use them when the
    # geometry demands it).
    min_pitch = _min_pad_pitch(project_dir, project_name)
    fine_pitch = min_pitch is not None and min_pitch < FINE_PITCH_THRESHOLD_MM

    kwargs: dict = {"copper_weight_oz": copper_oz}
    if mfg_rules:
        # On a fine-pitch board respect the DFM minimum (no coarse floor);
        # otherwise keep the robust default as a floor.
        tw_floor = 0.0 if fine_pitch else 0.25
        cl_floor = 0.0 if fine_pitch else 0.2
        via_d_floor = 0.0 if fine_pitch else 0.3
        via_dia_floor = 0.0 if fine_pitch else 0.6
        if "trace_width_min_mm" in mfg_rules:
            tw = mfg_rules["trace_width_min_mm"]
            kwargs["trace_width_signal_mm"] = max(tw_floor, tw)
            # Power/ground keep their robust width — IPC-2221 per-net widths
            # (computed in freerouter) override where current demands more.
            kwargs["trace_width_power_mm"] = max(0.5, tw)
            kwargs["trace_width_ground_mm"] = max(0.5, tw)
        if "clearance_min_mm" in mfg_rules:
            kwargs["clearance_mm"] = max(cl_floor, mfg_rules["clearance_min_mm"])
        if "via_drill_min_mm" in mfg_rules:
            kwargs["via_drill_mm"] = max(via_d_floor, mfg_rules["via_drill_min_mm"])
        if "via_diameter_min_mm" in mfg_rules:
            kwargs["via_diameter_mm"] = max(via_dia_floor, mfg_rules["via_diameter_min_mm"])
    elif fine_pitch:
        # No DFM profile but the board is fine-pitch — use a safe fine ruleset
        # (5 mil / 5 mil, common to every modern fab) so escape is feasible.
        kwargs["trace_width_signal_mm"] = 0.127
        kwargs["clearance_mm"] = 0.127

    if fine_pitch:
        _log(f"  Fine-pitch board (min pad pitch {min_pitch:.2f}mm): using "
             f"tightened rules (trace {kwargs.get('trace_width_signal_mm', 0.127)}mm, "
             f"clearance {kwargs.get('clearance_mm', 0.127)}mm)")
    return kwargs


# Effort levels for routing: Freerouting max passes + timeout. "best" also
# retries once with a doubled timeout if the first attempt times out.
ROUTING_EFFORT = {
    "fast":   {"max_passes": 5,  "timeout_s": 120, "retry_on_timeout": False},
    "normal": {"max_passes": 20, "timeout_s": 300, "retry_on_timeout": False},
    "best":   {"max_passes": 40, "timeout_s": 900, "retry_on_timeout": True},
}


def _short_cleanup(routed, placement_data, netlist_data, exclude_nets,  # pragma: no cover - drives kicad-cli DRC + Freerouting re-route; called only from the live-route path
                   escape_wiring, fr_kwargs, router_kwargs, timeout_s,
                   config, log=None):
    """Drive optimizers.route_cleanup with the real export+kicad-cli-DRC+reroute.
    No-op (returns routed unchanged) when kicad-cli isn't available."""
    import tempfile
    from optimizers.route_cleanup import (
        find_kicad_cli, cleanup_shorts, run_drc_json,
    )
    kcli = find_kicad_cli()
    if not kcli:
        (log or (lambda *_a: None))("  Short cleanup skipped: kicad-cli not found")
        return routed

    from optimizers.freerouter import route_with_freerouting
    from optimizers.router import apply_copper_fills, RouterConfig
    from exporters.kicad_exporter import export_kicad_pcb

    # Cleanup re-routes touch only a few nets (everything else is protected), so
    # cap passes/timeout to keep the extra FR runs cheap.
    base_kwargs = {k: v for k, v in fr_kwargs.items() if k != "fixed_routing"}
    base_kwargs["max_passes"] = min(base_kwargs.get("max_passes", 20), 12)
    cl_timeout = min(timeout_s, 600)

    def _set_rules(rt):
        rt.setdefault("routing", {}).setdefault("config", {}).update({
            "trace_clearance_mm": router_kwargs.get("clearance_mm", 0.2),
            "trace_width_signal_mm": router_kwargs.get("trace_width_signal_mm", 0.25),
            "via_diameter_mm": router_kwargs.get("via_diameter_mm", 0.6),
            "via_drill_mm": router_kwargs.get("via_drill_mm", 0.3),
        })
        return rt

    def _route_fn(fixed):
        try:
            r = route_with_freerouting(placement_data, netlist_data,
                                       timeout_s=cl_timeout, fixed_routing=fixed,
                                       **base_kwargs)
        except Exception:
            return None
        return _set_rules(apply_copper_fills(r, netlist_data,
                                             RouterConfig(**router_kwargs)))

    def _drc_fn(rt):
        _set_rules(rt)
        with tempfile.TemporaryDirectory(prefix="pcb-cleanup-drc-") as td:
            pcb = Path(td) / "cleanup.kicad_pcb"
            export_kicad_pcb(rt, netlist_data, pcb)
            return run_drc_json(pcb, kcli, timeout=300)

    # Keepout big enough to push a via/trace clear of the violation site.
    via_d = router_kwargs.get("via_diameter_mm", 0.6)
    clr = router_kwargs.get("clearance_mm", 0.2)
    keepout_d = max(0.8, via_d + 2 * clr)

    best, _bad = cleanup_shorts(
        routed, netlist_data, escapes=escape_wiring,
        exclude_nets=tuple(exclude_nets), route_fn=_route_fn,
        drc_data_fn=_drc_fn, max_iterations=2,
        keepout_diameter_mm=keepout_d, log=log)
    return best


def run_routing(project_dir: Path, project_name: str, config,
                progress_callback=None, log=None,
                effort: str = "normal", max_seconds: int | None = None,
                fixed_routing: dict | None = None) -> dict:
    """Route the board: Freerouting (if configured) or built-in A* (2-layer only).

    Reads <project>_placement.json + <project>_netlist.json, writes
    <project>_routed.json.

    progress_callback: optional callable(dict) fired by both engines — the
        built-in NCR router per iteration ({iteration, max_iterations,
        legal_nets, total_nets, overused_cells, elapsed_s}) and Freerouting
        per auto-router pass / ~10s heartbeat ({phase: "freerouting",
        pass_num, max_passes, incomplete_connections, score, elapsed_s,
        heartbeat}).
    log: optional callable(str) — when provided (e.g. the CLI runner passes
        print), emits the engine/fallback/stats/validation diagnostic lines.
        None (default) keeps this silent for MCP callers.
    effort: "fast" | "normal" | "best" — maps to Freerouting passes/timeout
        (see ROUTING_EFFORT). "best" retries once with a doubled timeout if
        the first attempt times out.
    max_seconds: overrides the effort level's timeout when given.

    Returns:
        {success, engine, completion_pct, routed_nets, total_nets, via_count,
         trace_length_mm, unrouted_nets, valid, validation_errors,
         validation_warnings, routed_path}
    """
    _log = log or (lambda *_a: None)
    if str(config.base_dir) not in sys.path:  # pragma: no cover - defensive sys.path guard (base_dir already on path under pytest)
        sys.path.insert(0, str(config.base_dir))
    from validators.validate_routing import validate_routing as run_routing_validation

    placement_path = _p(project_dir, project_name, "placement")
    netlist_path = _p(project_dir, project_name, "netlist")
    if not placement_path.exists():
        return {"success": False, "error": "No placement found — run placement first"}
    if not netlist_path.exists():
        return {"success": False, "error": "No netlist found"}

    placement_data = _load(placement_path)
    netlist_data = _load(netlist_path)
    router_kwargs = _build_router_kwargs(project_dir, project_name, log=log)

    # Caller-driven incremental routing (keep_existing) — captured before the
    # escape-fanout logic may set fixed_routing internally on a FRESH route, so
    # the post-merge stats recompute below applies only to true incremental runs.
    _incremental = fixed_routing is not None

    routed = None
    engine = "builtin"
    num_layers = placement_data.get("board", {}).get("layers", 2)

    # 4-layer boards require Freerouting — the built-in A* is 2-layer only
    if num_layers > 2 and config.router_engine != "freerouting":
        return {
            "success": False,
            "error": f"{num_layers}-layer boards require Freerouting. "
                     "Set PCB_ROUTER_ENGINE=freerouting (default) or check Java/JAR availability.",
        }

    if config.router_engine == "freerouting":  # pragma: no cover - live Freerouting/Java routing run (mirrors optimizers/freerouter.py JVM pragmas); covered end-to-end only in the manual flow
        try:
            from optimizers.freerouter import route_with_freerouting
            from optimizers.router import inner_plane_count
            engine = "freerouting"
            _log("  Engine: Freerouting")
            plane_layers = inner_plane_count(placement_data.get("board", {}))
            dsn_config = {
                "trace_width_mm": router_kwargs.get("trace_width_signal_mm", 0.25),
                "clearance_mm": router_kwargs.get("clearance_mm", 0.2),
                "via_drill_mm": router_kwargs.get("via_drill_mm", 0.3),
                "via_diameter_mm": router_kwargs.get("via_diameter_mm", 0.6),
                "num_layers": num_layers,
                "plane_layers": plane_layers,
            }
            if num_layers > 2:
                _log(f"  Layer count: {num_layers}, inner plane layers: {plane_layers} "
                     f"({2 - plane_layers if num_layers >= 4 else 0} inner signal layer(s))")
            # GND is delivered by copper fill/plane → never routed point-to-point.
            # The power net is excluded ONLY when In2 is a plane (plane_layers>=2);
            # with an inner signal layer it is routed as traces instead.
            exclude_nets = ["GND"]
            if plane_layers >= 2:
                best_pwr: tuple[int, str] = (0, "")
                for elem in netlist_data.get("elements", []):
                    if (elem.get("element_type") == "net"
                            and elem.get("net_class") == "power"
                            and elem.get("name", elem.get("net_id", "")) != "GND"):
                        pin_count = len(elem.get("connected_port_ids", []))
                        if pin_count > best_pwr[0]:
                            best_pwr = (pin_count, elem.get("name", elem.get("net_id", "")))
                if best_pwr[1]:
                    exclude_nets.append(best_pwr[1])
                    _log(f"  Excluding power plane net from routing: {best_pwr[1]} ({best_pwr[0]} pins)")
            # Fine-pitch escape fanout: pre-route dog-bone escapes for single-row
            # fine-pitch parts and hand them to Freerouting as protected wiring,
            # so it only routes from comfortable-pitch breakout vias. AUTO-enabled
            # when the board has a fine-pitch part (config.escape_fanout is None);
            # PCB_ESCAPE_FANOUT=true/false forces it on/off. Only on a fresh route
            # (an incremental caller's fixed_routing already carries the escapes).
            _fresh_route = fixed_routing is None
            # Escape wiring is preserved (never ripped) by the short-cleanup pass
            # below, so a fine-pitch net's breakout survives a re-route.
            escape_wiring = {"traces": [], "vias": [], "keepouts": []}
            ef = getattr(config, "escape_fanout", None)
            if ef is None:
                _mp = _min_pad_pitch(project_dir, project_name)
                ef = _mp is not None and _mp < FINE_PITCH_THRESHOLD_MM
            if ef and fixed_routing is None:
                try:
                    from optimizers.escape_router import (
                        generate_escape_routing, EscapeConfig,
                    )
                    ecfg = EscapeConfig(
                        trace_width_mm=router_kwargs.get("trace_width_signal_mm", 0.127),
                        clearance_mm=router_kwargs.get("clearance_mm", 0.127),
                        via_diameter_mm=router_kwargs.get("via_diameter_mm", 0.45),
                        via_drill_mm=router_kwargs.get("via_drill_mm", 0.2),
                        num_layers=num_layers,
                        plane_layers=plane_layers,
                    )
                    escapes = generate_escape_routing(
                        placement_data, netlist_data, ecfg,
                        exclude_nets=tuple(exclude_nets))
                    if escapes["traces"]:
                        _log(f"  Fine-pitch escape fanout: pre-routed "
                             f"{len(escapes['vias'])} pin escape(s) as protected wiring")
                        fixed_routing = escapes
                        escape_wiring = escapes
                except Exception as exc:
                    _log(f"  Escape fanout skipped: {exc}")
            eff = ROUTING_EFFORT.get(effort, ROUTING_EFFORT["normal"])
            timeout_s = max_seconds or eff["timeout_s"] or config.freerouting_timeout_s
            fr_kwargs = dict(
                jar_path=config.freerouting_jar_path,
                exclude_nets=exclude_nets,
                dsn_config=dsn_config,
                progress_callback=progress_callback,
                max_passes=eff["max_passes"],
                fixed_routing=fixed_routing,
            )
            try:
                routed = route_with_freerouting(
                    placement_data, netlist_data,
                    timeout_s=timeout_s, **fr_kwargs,
                )
            except RuntimeError as exc:
                if eff["retry_on_timeout"] and "timed out" in str(exc):
                    _log(f"  Freerouting timed out at {timeout_s}s — retrying once "
                         f"with {timeout_s * 2}s")
                    routed = route_with_freerouting(
                        placement_data, netlist_data,
                        timeout_s=timeout_s * 2, **fr_kwargs,
                    )
                else:
                    raise
            completion = routed.get("routing", {}).get("statistics", {}).get("completion_pct", 0)
            if completion < 100:
                unrouted = routed.get("routing", {}).get("unrouted_nets", [])
                _log(f"  Freerouting incomplete ({completion:.0f}%): {len(unrouted)} nets unrouted")
                _log("  Continuing with partial result (no fallback when Freerouting is the engine)")
            from optimizers.router import apply_copper_fills, RouterConfig
            routed = apply_copper_fills(routed, netlist_data, RouterConfig(**router_kwargs))

            # Short-cleanup pass: rip up the nets kicad-cli DRC reports as
            # shorting (or that are left incomplete) and re-route just those,
            # holding everything else — including all escape wiring — protected.
            # Authoritative bad-net list comes from kicad-cli (the internal
            # geometric short-check over-reports); a no-op where kicad-cli isn't
            # installed. Fresh routes only (an incremental caller drives its own).
            if _fresh_route and getattr(config, "short_cleanup", True):
                try:
                    routed = _short_cleanup(
                        routed, placement_data, netlist_data, exclude_nets,
                        escape_wiring, fr_kwargs, router_kwargs, timeout_s,
                        config, log=_log)
                except Exception as exc:
                    _log(f"  Short cleanup skipped: {exc}")
        except Exception as exc:
            _log(f"  Freerouting FAILED: {exc}")
            # The built-in A* router is 2-layer only; falling back on a 4-layer
            # board would silently route just the outer layers and report an
            # incomplete result. Surface the real failure instead — but frame it
            # as a failure of THIS run, not a layer-count limit. (Agents read
            # "can't route >2 layers" as "the board needs fewer layers" and
            # abandon a perfectly routable 4-layer board.)
            if num_layers > 2:
                return {"success": False, "engine": "freerouting",
                        "error": (f"Freerouting did not finish this routing run on "
                                  f"the {num_layers}-layer board: {exc} — this is a "
                                  f"failure of THIS run, NOT a layer-count limit "
                                  f"({num_layers}-layer routing is fully supported. "
                                  f"Retry route_board (add keep_existing=True to "
                                  f"finish from a partial result), or fix the cause "
                                  f"above (the message says if it was out-of-memory). "
                                  f"Do NOT reduce the layer count or enlarge the "
                                  f"board on the basis of this error.")}
            _log("  Falling back to built-in router")

    if routed is None:  # pragma: no cover - built-in A* router invocation (live routing run); covered end-to-end only in the manual flow
        from optimizers.router import route_board, RouterConfig
        engine = "builtin"
        _log("  Engine: Built-in")
        rc = RouterConfig(**router_kwargs)
        rc.ncr_progress_callback = progress_callback
        routed = route_board(placement_data, netlist_data, rc)

    # Incremental safety net: never lose the caller's protected wiring. If
    # Freerouting echoed it (normal case) the union dedupes to a no-op; if it
    # wrote an empty/short SES (degenerate "nothing to route") we restore the
    # existing traces/vias so the board is never worse than before.
    if fixed_routing and routed is not None:
        rt = routed.setdefault("routing", {})
        def _tk(t):
            return (t.get("net_name") or t.get("net_id"), t.get("layer"),
                    round(t.get("start_x_mm", 0), 3), round(t.get("start_y_mm", 0), 3),
                    round(t.get("end_x_mm", 0), 3), round(t.get("end_y_mm", 0), 3))
        seen_t = {_tk(t) for t in rt.get("traces", [])}
        for t in fixed_routing.get("traces", []):
            if _tk(t) not in seen_t:
                rt.setdefault("traces", []).append(t); seen_t.add(_tk(t))
        def _vk(v):
            return (v.get("net_name") or v.get("net_id"),
                    round(v.get("x_mm", 0), 3), round(v.get("y_mm", 0), 3))
        seen_v = {_vk(v) for v in rt.get("vias", [])}
        n_vias_before = len(rt.get("vias", []))
        for v in fixed_routing.get("vias", []):
            if _vk(v) not in seen_v:
                rt.setdefault("vias", []).append(v); seen_v.add(_vk(v))
        # Re-adding vias here happens AFTER apply_copper_fills cut the inner-plane
        # antipads, so a re-added through-via would have no cutout in a power
        # plane (inner_plane_antipad). Re-cut the planes against the final vias.
        if len(rt.get("vias", [])) != n_vias_before:
            from optimizers.router import regenerate_inner_planes, RouterConfig
            regenerate_inner_planes(routed, netlist_data,
                                    RouterConfig(**router_kwargs))

        # Recompute completion from the MERGED routing. import_ses only saw the
        # newly-routed SES nets, so when Freerouting wrote a degenerate "nothing
        # to route" SES (e.g. an already-complete board finished with
        # keep_existing) it reported 0% / all-unrouted even though the restored
        # protected traces fully connect the board. Use the fill-aware
        # connectivity check so the agent isn't told a finished board is 0% routed
        # and sent into a needless re-route. Merging traces can only IMPROVE
        # connectivity, so we never mark MORE nets unrouted than before.
        from validators.validate_routing import incomplete_net_ids
        st = rt.setdefault("statistics", {})
        total = st.get("total_nets", 0)
        prev_unrouted = set(rt.get("unrouted_nets", []))
        if _incremental and prev_unrouted:
            # The connectivity check SKIPS nets already listed in unrouted_nets
            # (it trusts that field), so a fresh evaluation of the merged board
            # must start from an empty list — otherwise the protected nets are
            # never re-examined and stay "unrouted" forever.
            rt["unrouted_nets"] = []
            incomplete = incomplete_net_ids(routed, netlist_data)
            still_unrouted = sorted(prev_unrouted & incomplete)
            rt["unrouted_nets"] = still_unrouted
            st["unrouted_nets"] = len(still_unrouted)
            st["routed_nets"] = max(0, total - len(still_unrouted))
            st["completion_pct"] = (round(100 * st["routed_nets"] / total, 1)
                                    if total else 100.0)
            st["via_count"] = len(rt.get("vias", []))
            tl = 0.0
            for t in rt.get("traces", []):
                dx = t.get("end_x_mm", 0) - t.get("start_x_mm", 0)
                dy = t.get("end_y_mm", 0) - t.get("start_y_mm", 0)
                tl += (dx * dx + dy * dy) ** 0.5
            st["total_trace_length_mm"] = round(tl, 1)

    # Reconcile reported completion with the AUTHORITATIVE connectivity check on
    # a fresh route. The router credits a net as "routed" when it has wiring, but
    # that wiring can still leave the net's pads in disconnected groups — so it
    # reported 100% while DRC found 3 disconnected nets, and the agent thought
    # the board was done. completion_pct / unrouted_nets now reflect real
    # connectivity (the incremental path above already did this with its
    # prev_unrouted intersection).
    if not _incremental and routed is not None:
        from validators.validate_routing import incomplete_net_ids
        rt = routed.setdefault("routing", {})
        st = rt.setdefault("statistics", {})
        total = st.get("total_nets", 0)
        incomplete = sorted(incomplete_net_ids(routed, netlist_data))
        rt["unrouted_nets"] = incomplete
        st["unrouted_nets"] = len(incomplete)
        st["routed_nets"] = max(0, total - len(incomplete))
        st["completion_pct"] = (round(100 * st["routed_nets"] / total, 1)
                                if total else 100.0)

    # Persist the design rules the board was ACTUALLY routed to, so the DRC
    # checks against the same clearance/widths the router used (otherwise the
    # validator falls back to its 0.2mm default and false-flags fine-pitch
    # boards routed at the manufacturer minimum).
    routed.setdefault("routing", {}).setdefault("config", {})
    routed["routing"]["config"].update({
        "trace_clearance_mm": router_kwargs.get("clearance_mm", 0.2),
        "trace_width_signal_mm": router_kwargs.get("trace_width_signal_mm", 0.25),
        "via_diameter_mm": router_kwargs.get("via_diameter_mm", 0.6),
        "via_drill_mm": router_kwargs.get("via_drill_mm", 0.3),
    })

    # FINAL inner-plane re-cut, UNCONDITIONAL, right before persisting. Short
    # cleanup and the protected-wiring union both move/add vias after the planes
    # were last cut, and the earlier re-cut only fires in narrow conditions — so
    # the persisted planes could be STALE (foreign vias/pads with no antipad =
    # solid copper shorting them, which both the gerbers and DRC then inherit).
    # Re-cutting here against the final via/pad set is the one chokepoint that
    # guarantees the planes match what ships. No-ops when there are no planes.
    if routed is not None and any(  # pragma: no cover - final inner-plane re-cut, reached only after a live 4-layer route produced plane fills
            f.get("is_plane")
            for f in routed.get("routing", {}).get("copper_fills", [])):
        try:
            from optimizers.router import regenerate_inner_planes, RouterConfig
            regenerate_inner_planes(routed, netlist_data,
                                    RouterConfig(**router_kwargs))
        except Exception as exc:
            _log(f"  Inner-plane final re-cut skipped: {exc}")

    routed_path = _p(project_dir, project_name, "routed")
    routed_path.write_text(json.dumps(routed, indent=2))

    val_result = run_routing_validation(str(routed_path), str(netlist_path))
    stats = routed.get("routing", {}).get("statistics", {})
    unrouted = routed.get("routing", {}).get("unrouted_nets", [])

    # Diagnostic summary (mirrors the CLI runner's inline block)
    if not val_result["valid"]:
        _log("  Routing validation FAILED")
        for err in val_result.get("errors", [])[:5]:
            _log(f"    - {err}")
    else:
        _log(f"  Routed: {stats.get('routed_nets', 0)}/{stats.get('total_nets', 0)} nets "
             f"({stats.get('completion_pct', 0)}%)")
        _log(f"  Trace length: {stats.get('total_trace_length_mm', 0):.1f}mm  "
             f"Vias: {stats.get('via_count', 0)}")
        if unrouted:  # pragma: no cover - CLI diagnostic log (only with a live route leaving unrouted nets + log=)
            _log(f"  WARNING: {len(unrouted)} nets unrouted: {', '.join(unrouted)}")
    overrides = routed.get("routing", {}).get("trace_width_overrides", {})
    if overrides:  # pragma: no cover - CLI diagnostic log (only with a live route producing IPC upsizes + log=)
        _log(f"  IPC-2221 trace upsizes: {len(overrides)} nets")

    return {
        "success": True,
        "engine": engine,
        "valid": val_result["valid"],
        "validation_errors": val_result.get("errors", []) or [],
        "validation_warnings": val_result.get("warnings", []) or [],
        "completion_pct": stats.get("completion_pct", 0),
        "routed_nets": stats.get("routed_nets", 0),
        "total_nets": stats.get("total_nets", 0),
        "via_count": stats.get("via_count", 0),
        "trace_length_mm": stats.get("total_trace_length_mm", 0),
        "unrouted_nets": unrouted,
        "routed_path": str(routed_path),
    }


def _route_is_clean(result: dict) -> bool:
    return (bool(result.get("success"))
            and result.get("completion_pct", 0) >= 100
            and bool(result.get("valid", True)))


def _route_score(r: dict) -> tuple:
    """Order routing attempts: success, then completion %, then validity. Used to
    keep the better of two attempts (never regress)."""
    return (bool(r.get("success")), r.get("completion_pct", 0),
            bool(r.get("valid", False)))


# At/above this completion the auto-retry FINISHES the residual nets
# incrementally (protect the routed wiring, route only what's left) instead of
# re-placing from scratch and re-routing all over — which on a near-complete
# board is slow (a full extra route that oscillates) and can regress the good
# result. Below it, the board likely needs a genuinely different placement.
INCREMENTAL_FINISH_PCT = 85.0


def build_incremental_fixed_routing(routed: dict, netlist: dict) -> dict | None:
    """Protected wiring for an incremental (keep_existing) re-route: every
    FULLY-CONNECTED net's traces/vias, EXCLUDING nets still incomplete (those
    stay unprotected so Freerouting re-routes them). None if nothing is routed
    yet. Shared by route_board(keep_existing) and the near-complete auto-retry."""
    rt = (routed or {}).get("routing", {})
    if not (rt.get("traces") or rt.get("vias")):
        return None
    try:
        from validators.validate_routing import incomplete_net_ids
        incomplete = incomplete_net_ids(routed, netlist)
    except Exception:
        incomplete = set()
    return {
        "traces": [t for t in rt.get("traces", []) if t.get("net_id") not in incomplete],
        "vias": [v for v in rt.get("vias", []) if v.get("net_id") not in incomplete],
    }


def _attempt_summary(result: dict) -> dict:
    return {k: result.get(k) for k in
            ("success", "engine", "valid", "completion_pct", "routed_nets",
             "total_nets", "via_count", "trace_length_mm", "unrouted_nets")}


def _components_for_unrouted(project_dir: Path, project_name: str,
                             unrouted: list[str]) -> set[str]:
    """Designators touched by the unrouted nets — the region to re-space.

    Maps unrouted net names/ids back to their connected components so the
    routing-feedback retry can apply a *localized* escape-halo boost there
    (enhancement C) instead of a blunt global clearance bump.
    """
    if not unrouted:
        return set()
    netlist_path = _p(project_dir, project_name, "netlist")
    if not netlist_path.exists():
        return set()
    try:
        from optimizers.ratsnest import build_connectivity
        nets = build_connectivity(_load(netlist_path))
    except Exception:
        return set()
    target = set(unrouted)
    out: set[str] = set()
    for n in nets:
        if n.name in target or n.net_id in target:
            out.update(n.designators)
    return out


def run_route_with_retry(project_dir: Path, project_name: str, config,  # pragma: no cover - drives the live router across re-place/re-route attempts; needs Freerouting/Java
                         progress_callback=None, log=None,
                         effort: str = "normal", max_seconds: int | None = None,
                         allow_grow: bool = False) -> dict:
    """Route with one placement→routing feedback retry.

    If the first route is incomplete or invalid, re-place once and re-route at
    the same effort. The re-placement focuses on the components touched by the
    unrouted nets: it gives them an enlarged escape halo (localized fanout
    reservation, enhancement C) plus the congestion term, on a different seed
    (or a 10% larger board when allow_grow=True). If no unrouted region can be
    identified it falls back to the old blunt +0.5mm global clearance bump.
    Keeps whichever attempt routed better; the result carries both attempts'
    stats under 'attempts'.
    """
    _log = log or (lambda *_a: None)
    routed_path = _p(project_dir, project_name, "routed")
    first = run_routing(project_dir, project_name, config,
                        progress_callback=progress_callback, log=log,
                        effort=effort, max_seconds=max_seconds)
    precondition_failure = (not first.get("success", False)
                            and "No placement" in str(first.get("error", "")))
    if _route_is_clean(first) or precondition_failure:
        first["attempts"] = [_attempt_summary(first)]
        return first

    # Near-complete → FINISH the residual nets incrementally rather than
    # re-placing + re-routing the whole board. The full re-route on a 95% board
    # is the slow path that oscillates for minutes (and can regress the good
    # result); an incremental pass protects the routed wiring and only works the
    # few unrouted nets. Keep whichever is better, so it never regresses.
    comp = first.get("completion_pct", 0)
    if first.get("success") and comp >= INCREMENTAL_FINISH_PCT:
        netlist = _load(_p(project_dir, project_name, "netlist"))
        first_routed = _load(routed_path) if routed_path.exists() else None
        fixed = (build_incremental_fixed_routing(first_routed, netlist)
                 if first_routed else None)
        if fixed and (fixed["traces"] or fixed["vias"]):
            _log(f"  Route near-complete ({comp}%) — finishing the residual "
                 f"{len(first.get('unrouted_nets', []))} net(s) incrementally "
                 "(protecting the routed wiring) instead of re-placing")
            if progress_callback is not None:
                try:
                    progress_callback({"phase": "incremental_finish",
                                       "detail": "finishing residual nets"})
                except Exception:
                    pass
            saved_routed = routed_path.read_text() if routed_path.exists() else None
            finish = run_routing(project_dir, project_name, config,
                                 progress_callback=progress_callback, log=log,
                                 effort=effort, max_seconds=max_seconds,
                                 fixed_routing=fixed)
            attempts = [_attempt_summary(first), _attempt_summary(finish)]
            if _route_score(finish) >= _route_score(first):
                finish["attempts"] = attempts
                finish["retried"] = True
                return finish
            if saved_routed is not None:
                routed_path.write_text(saved_routed)
            first["attempts"] = attempts
            first["retried"] = True
            return first

    focus = sorted(_components_for_unrouted(
        project_dir, project_name, first.get("unrouted_nets", []) or []))
    if focus:
        _log(f"  Route incomplete ({first.get('completion_pct', 0)}%) — re-placing "
             f"with an enlarged escape halo around {len(focus)} component(s) near "
             f"the {len(first.get('unrouted_nets', []))} unrouted net(s), then re-routing")
    else:
        _log(f"  Route incomplete ({first.get('completion_pct', 0)}%) — "
             "re-placing with extra clearance and congestion penalty, then re-routing")
    if progress_callback is not None:
        try:
            progress_callback({"phase": "replace_retry",
                               "detail": (f"re-placing around {len(focus)} unrouted "
                                          f"component(s)" if focus
                                          else "re-placing with extra clearance")})
        except Exception:
            pass

    placement_path = _p(project_dir, project_name, "placement")
    routed_path = _p(project_dir, project_name, "routed")
    saved_placement = placement_path.read_text() if placement_path.exists() else None
    saved_routed = routed_path.read_text() if routed_path.exists() else None

    bw = bh = None
    seed = None
    try:
        board = _load(placement_path).get("board", {}) if placement_path.exists() else {}
        if allow_grow:
            bw = board.get("width_mm")
            bh = board.get("height_mm")
            if bw and bh:
                bw, bh = round(bw * 1.1, 1), round(bh * 1.1, 1)
    except Exception:
        pass

    place_result = run_placement(
        project_dir, project_name, config,
        board_width_mm=bw, board_height_mm=bh,
        seed=(config.optimizer_seed or 0) + 1,
        # Localized escape-halo boost where routing failed (C). Only fall back
        # to the blunt global clearance bump when we can't pin down a region.
        extra_clearance_mm=0.0 if focus else 0.5,
        congestion_weight=2.0,
        escape_weight=12.0 if focus else None,
        focus_components=focus or None,
    )
    if not place_result.get("success"):
        _log(f"  Retry re-place failed: {place_result.get('error')}")
        first["attempts"] = [_attempt_summary(first)]
        return first

    second = run_routing(project_dir, project_name, config,
                         progress_callback=progress_callback, log=log,
                         effort=effort, max_seconds=max_seconds)

    attempts = [_attempt_summary(first), _attempt_summary(second)]

    if _route_score(second) >= _route_score(first):
        second["attempts"] = attempts
        second["retried"] = True
        return second

    # First attempt was better — restore its placement and routing artifacts.
    _log("  Retry routed worse — restoring the first attempt's result")
    if saved_placement is not None:
        placement_path.write_text(saved_placement)
    if saved_routed is not None:
        routed_path.write_text(saved_routed)
    first["attempts"] = attempts
    first["retried"] = True
    return first


# ---------------------------------------------------------------------------
# DRC (deterministic — kept as a first-class stage)
# ---------------------------------------------------------------------------

def run_drc(project_dir: Path, project_name: str, config, log=None) -> dict:
    """Run the deterministic DRC checks on the routed board.

    Reads <project>_routed.json + <project>_netlist.json, writes
    <project>_drc_report.json.

    log: optional callable(str) — when provided, emits the DRC pass/fail summary
        and per-check violation lines the CLI runner printed.  None = silent.

    Returns the full DRC report dict (passed, summary, checks, statistics).
    """
    _log = log or (lambda *_a: None)
    if str(config.base_dir) not in sys.path:  # pragma: no cover - defensive sys.path guard (base_dir already on path under pytest)
        sys.path.insert(0, str(config.base_dir))
    from validators.drc_report import run_drc as _run_drc

    routed_path = _p(project_dir, project_name, "routed")
    netlist_path = _p(project_dir, project_name, "netlist")
    if not routed_path.exists():
        return {"success": False, "error": "No routed board found — run routing first"}

    routed = _load(routed_path)
    netlist_data = _load(netlist_path)

    req_data = None
    req_path = _p(project_dir, project_name, "requirements")
    if req_path.exists():
        try:
            req_data = _load(req_path)
        except Exception:
            pass

    report = _run_drc(routed, netlist_data, req_data)

    # When kicad-cli is installed, supersede the report with KiCad's own DRC for
    # GEOMETRY — the authoritative engine the fab uses (correct rules from the
    # exported .kicad_pro, poured zones, real short/antipad geometry). The
    # internal report is the portable fallback. We carry over the internal
    # CONNECTIVITY and current-capacity checks: connectivity stays on the
    # router-reconciled internal check (KiCad's ratsnest disagrees with the
    # router and would loop agents on "100% routed but N unconnected"), and
    # KiCad has no current-capacity rule.
    # drc_engine records WHICH engine produced this verdict. "internal" is the
    # heuristic fallback — it MISSES THT-pad shorts, mask bridges, and starved
    # thermals, so a clean "internal" report is NOT a manufacturability
    # guarantee. Callers that gate on DRC (export) must treat authoritative ==
    # False as "could not certify", never as "clean".
    report["drc_engine"] = "internal"
    report["authoritative"] = False
    try:
        from optimizers.route_cleanup import find_kicad_cli
        from validators.kicad_drc import run_kicad_drc
        from exporters.kicad_exporter import export_kicad_pcb
        kcli = find_kicad_cli()
        if kcli:
            # Carry these INTERNAL checks into the authoritative report:
            #  - connectivity: router-reconciled (now incl. unrouted nets)
            #  - trace_current_capacity: KiCad has no current-capacity rule
            #  - inner_plane_antipad: validates the copper_fills plane geometry
            #    that the GERBERS actually paint. kicad-cli only sees a pcbnew
            #    RE-POUR of empty board-sized zones — a different rendering than
            #    what ships — so without this a solid/antipad-less inner plane
            #    (which shorts every foreign pad in the gerbers) passes DRC.
            _carry = {"connectivity", "trace_current_capacity",
                      "inner_plane_antipad"}
            extra = [c for c in report.get("checks", [])
                     if c.get("rule") in _carry]
            auth = run_kicad_drc(
                routed, netlist_data, kcli,
                export_fn=lambda rt, nl, pcb: export_kicad_pcb(rt, nl, pcb),
                project_name=project_name, extra_checks=extra)
            if auth is not None:
                auth["manufacturer"] = report.get("manufacturer")
                auth["dfm_profile"] = report.get("dfm_profile")
                auth["drc_engine"] = "kicad-cli"
                auth["authoritative"] = True
                report = auth
                _log("  DRC: using kicad-cli (authoritative)")
            else:
                _log("  DRC: kicad-cli export/run failed — internal report is "
                     "NOT authoritative (geometry unverified)")
        else:
            _log("  DRC: kicad-cli not found — internal report is NOT "
                 "authoritative (geometry unverified)")
    except Exception as exc:  # pragma: no cover - defensive: kicad-cli import/run crashed; fall back to internal report
        _log(f"  kicad-cli DRC unavailable, using internal report (NOT "
             f"authoritative): {exc}")

    _p(project_dir, project_name, "drc_report").write_text(json.dumps(report, indent=2))

    if report.get("passed"):
        _log(f"  DRC: PASSED — {report.get('summary', '')}")
    else:
        _log(f"  DRC: FAILED — {report.get('summary', '')}")
        for check in report.get("checks", []):
            if not check.get("passed", True):
                for v in check.get("violations", [])[:3]:
                    _log(f"    {v.get('severity', '').upper()}: {v.get('message', '')}")
                remaining = len(check.get("violations", [])) - 3
                if remaining > 0:  # pragma: no cover - CLI diagnostic log for a failing check with >3 violations (only with log=)
                    _log(f"    ... and {remaining} more {check.get('rule', '')} violations")

    report["success"] = True
    return report


# ---------------------------------------------------------------------------
# Output generation (Gerbers, drill, BOM CSV, CPL, STEP, ZIP)
# ---------------------------------------------------------------------------

def _bom_from_netlist(netlist: dict) -> dict:
    """Synthesize a BOM (grouped by value+package) from the netlist.

    Used when no <project>_bom.json exists so the manufacturing package still
    includes a BOM CSV. Without this, export silently skips the BOM whenever the
    board came from a KiCad netlist import (no separate BOM file) — the missing
    BOM half of the "manufacturing package incomplete" report. Mounting holes
    and fiducials are excluded (not assembly parts).
    """
    import re as _re
    from collections import defaultdict

    def _natkey(des: str):
        m = _re.match(r"^([A-Za-z]+)(\d+)", des)
        return (m.group(1), int(m.group(2))) if m else (des, 0)

    groups: dict[tuple, list[str]] = defaultdict(list)
    for e in netlist.get("elements", []):
        if e.get("element_type") != "component":
            continue
        pkg = e.get("package", "") or ""
        ctype = e.get("component_type", "")
        if ctype in ("fiducial", "mounting_hole") or "mountinghole" in pkg.lower():
            continue
        groups[(e.get("value", "") or "", pkg)].append(e.get("designator", ""))

    bom = []
    for (value, pkg), dess in groups.items():
        dess_sorted = sorted((d for d in dess if d), key=_natkey)
        bom.append({
            "designator": ", ".join(dess_sorted),
            "value": value,
            "package": pkg,
            "quantity": len(dess_sorted),
        })
    bom.sort(key=lambda b: _natkey(b["designator"].split(",")[0]))
    return {"bom": bom}


def run_export(project_dir: Path, project_name: str, config, log=None) -> dict:
    """Generate manufacturing outputs from the routed board.

    Reads <project>_routed.json (+ optional _netlist/_bom), writes into
    <project_dir>/output/ and produces a ZIP package.  Gerbers, drill, BOM CSV,
    pick-and-place, STEP, and assembly drawing PDF (the last two best-effort).

    log: optional callable(str) — when provided, emits the per-artifact lines the
        CLI runner printed.  None (default) keeps this silent for MCP callers.

    Returns:
        {success, output_dir, files: [...], package: <zip path>}
    """
    _log = log or (lambda *_a: None)
    if str(config.base_dir) not in sys.path:  # pragma: no cover - defensive sys.path guard (base_dir already on path under pytest)
        sys.path.insert(0, str(config.base_dir))
    from exporters.gerber_exporter import export_gerbers, export_drill, create_output_package
    from exporters.bom_csv_exporter import export_bom_csv, export_pick_and_place
    from exporters.step_exporter import export_step_populated

    routed_path = _p(project_dir, project_name, "routed")
    if not routed_path.exists():
        return {"success": False, "error": "No routed board found — run routing first"}

    routed = _load(routed_path)
    netlist_path = _p(project_dir, project_name, "netlist")
    netlist_data = _load(netlist_path) if netlist_path.exists() else {}
    bom_path = _p(project_dir, project_name, "bom")
    bom_data = _load(bom_path) if bom_path.exists() else None
    # No standalone BOM file (e.g. KiCad-netlist import) — derive one from the
    # netlist so the manufacturing package always ships a BOM CSV.
    if bom_data is None and netlist_data.get("elements"):
        bom_data = _bom_from_netlist(netlist_data)

    output_dir = project_dir / "output"
    output_dir.mkdir(exist_ok=True)
    produced: list[str] = []

    gerber_files = export_gerbers(routed, netlist_data, output_dir)
    produced.extend(str(f) for f in gerber_files)
    _log(f"  Gerber layers: {len(gerber_files)} files")

    drill_path = export_drill(routed, netlist_data, output_dir / f"{project_name}.drl")
    produced.append(str(drill_path))
    _log(f"  Drill file: {drill_path.name}")

    if bom_data is not None:
        bom_csv = export_bom_csv(bom_data, output_dir / f"{project_name}_bom.csv")
        produced.append(str(bom_csv))
        _log(f"  BOM: {bom_csv.name}")

    cpl_path = export_pick_and_place(
        routed, output_dir / f"{project_name}_cpl.csv", bom=bom_data
    )
    produced.append(str(cpl_path))
    _log(f"  Pick-and-place: {cpl_path.name}")

    try:
        board_thickness = {4: 1.6}.get(
            routed.get("board", {}).get("layers", 2), 1.6
        )
        step_path = export_step_populated(
            routed, netlist_data, bom_data,
            output_dir / f"{project_name}_board.step",
            board_thickness_mm=board_thickness,
        )
        produced.append(str(step_path))
        _log(f"  STEP model: {step_path.name} (populated)")
    except Exception as exc:
        _log(f"  STEP model: skipped ({exc})")  # best-effort; don't fail export

    # Assembly drawing PDF (best-effort, matches the CLI runner)
    try:
        from exporters.assembly_drawing import export_assembly_drawing
        assy_path = export_assembly_drawing(
            routed, netlist_data, bom_data,
            output_dir / f"{project_name}_assembly.pdf",
            project_name=project_name,
        )
        produced.append(str(assy_path))
        _log(f"  Assembly drawing: {assy_path.name}")
    except Exception as exc:
        _log(f"  Assembly drawing: skipped ({exc})")

    zip_path = create_output_package(output_dir, project_name)
    _log(f"  Package: {zip_path.name}")

    return {
        "success": True,
        "output_dir": str(output_dir),
        "files": [str(Path(f).relative_to(project_dir)) for f in produced],
        "package": str(zip_path),
    }
