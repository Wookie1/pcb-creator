"""Orderable-BOM support: part-number resolution and fab cost estimation.

resolve_part_numbers() fills mpn/manufacturer/lcsc on BOM items so the
exported BOM CSV auto-matches parts at JLCPCB assembly. quote_project()
combines that with a deterministic board-price estimate (and optional live
LCSC stock/price lookups) into an order-readiness report.

Prices are ballpark by design — the estimate dict always carries
{"estimate": True, "source": ...}; override the table with PCB_PRICE_TABLE
(path to a JSON file with the same shape as _DEFAULT_PRICE_TABLE) when
published pricing drifts.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

# ponytail: crude tier model (base price × qty factor + oversize area charge).
# Real fab pricing has many more knobs (thickness, finish, color, remote-area
# shipping); calibrate via PCB_PRICE_TABLE if the ballpark drifts.
_DEFAULT_PRICE_TABLE: dict[str, Any] = {
    "source": "JLCPCB published pricing (approx), 2026-07 — verify at order time",
    "currency": "USD",
    # 5 boards, <= 100x100 mm, standard options
    "base_price": {"2": 4.0, "4": 25.0},
    # multiplier on base_price by quantity tier (interpolated linearly above)
    "qty_factor": {"5": 1.0, "10": 1.4, "30": 2.4, "100": 5.5},
    # per-board surcharge per cm^2 beyond 100x100 mm
    "oversize_per_cm2": {"2": 0.06, "4": 0.12},
}


def _load_price_table() -> dict[str, Any]:
    override = os.environ.get("PCB_PRICE_TABLE")
    if override:
        try:
            return json.loads(Path(override).read_text())
        except (OSError, json.JSONDecodeError):
            pass  # fall through to the default rather than failing a quote
    return _DEFAULT_PRICE_TABLE


def estimate_board_price(width_mm: float, height_mm: float, layers: int,
                         qty: int = 5) -> dict[str, Any]:
    """Deterministic fab price estimate for bare boards.

    Returns {total, per_board, currency, qty, estimate: True, source, notes}.
    Unknown layer counts fall back to the 4-layer rate.
    """
    table = _load_price_table()
    base = table["base_price"].get(str(layers), table["base_price"]["4"])

    tiers = sorted((int(q), f) for q, f in table["qty_factor"].items())
    factor = None
    for i, (q, f) in enumerate(tiers):
        if qty <= q:
            if i == 0 or qty == q:
                factor = f
            else:
                q0, f0 = tiers[i - 1]
                factor = f0 + (f - f0) * (qty - q0) / (q - q0)
            break
    if factor is None:  # beyond the largest tier: extrapolate per-board
        q_max, f_max = tiers[-1]
        factor = f_max * qty / q_max

    area_cm2 = width_mm * height_mm / 100.0
    oversize = max(0.0, area_cm2 - 100.0)
    surcharge = oversize * table["oversize_per_cm2"].get(
        str(layers), table["oversize_per_cm2"]["4"]) * qty

    total = round(base * factor + surcharge, 2)
    return {
        "total": total,
        "per_board": round(total / qty, 2),
        "currency": table.get("currency", "USD"),
        "qty": qty,
        "layers": layers,
        "board_mm": [round(width_mm, 1), round(height_mm, 1)],
        "estimate": True,
        "source": table.get("source", "built-in table"),
        "notes": "Bare-board fab only; excludes assembly, shipping, and tax.",
    }


def resolve_part_numbers(bom_data: dict) -> int:
    """Fill mpn/manufacturer/lcsc on BOM items from the curated tables + cache.

    Mutates bom_data in place. An existing value on the item (LLM/user
    provided) always wins. Curated hits are cached; prior cache hits (source
    'curated'/'agent'/'easyeda') are reused. Returns the number of items that
    gained a part number.
    """
    from orchestrator.cache import ComponentCache
    from orchestrator.gather.curated_specs import lookup_part_number

    cache = ComponentCache(os.environ.get("PCB_COMPONENT_CACHE_PATH"))
    changed = 0
    for item in bom_data.get("bom", []):
        if item.get("lcsc") or item.get("mpn"):
            continue
        ctype = item.get("component_type", "")
        value = item.get("value", "")
        package = item.get("package", "")
        key = f"part:{ctype}:{value}:{package}"

        part = lookup_part_number(ctype, value, package)
        if part is None:
            cached = cache.get_specs(key)
            if cached and cached.get("lcsc"):
                part = {k: cached[k] for k in ("lcsc", "mpn", "manufacturer")
                        if cached.get(k)}
        else:
            cache.put_specs(key, part, source="curated")

        if part:
            for k, v in part.items():
                item.setdefault(k, v)
            changed += 1
    return changed


def quote_project(project_dir: Path, project_name: str, qty: int = 5,
                  live: bool = False) -> dict[str, Any]:
    """Order-readiness quote: board price estimate + per-line part status.

    Reads <project>_bom.json (or derives a BOM from the netlist), resolves
    part numbers, optionally verifies each unique LCSC id live (stock, USD
    price, MPN cross-check), and totals what it can.

    Returns {success, board_estimate, parts, parts_total_usd, unresolved,
    notes}. parts_total_usd is None when no live prices were available.
    """
    bom_path = project_dir / f"{project_name}_bom.json"
    if bom_path.exists():
        bom_data = json.loads(bom_path.read_text())
    else:
        netlist_path = project_dir / f"{project_name}_netlist.json"
        if not netlist_path.exists():
            return {"success": False,
                    "error": "No BOM or netlist found — build the circuit first."}
        from orchestrator.stages import _bom_from_netlist
        bom_data = _bom_from_netlist(json.loads(netlist_path.read_text()))

    resolve_part_numbers(bom_data)

    # Board dims/layers from the routed board if present, else the placement.
    board: dict[str, Any] = {}
    for suffix in ("routed", "placement"):
        p = project_dir / f"{project_name}_{suffix}.json"
        if p.exists():
            board = json.loads(p.read_text()).get("board", {})
            if board:
                break
    board_estimate = None
    if board.get("width_mm") and board.get("height_mm"):
        board_estimate = estimate_board_price(
            board["width_mm"], board["height_mm"],
            board.get("layers", 2), qty)

    live_info: dict[str, dict] = {}
    if live:
        from orchestrator.gather.easyeda_lookup import fetch_part_info
        unique_ids = {item["lcsc"] for item in bom_data.get("bom", [])
                      if item.get("lcsc")}
        for lcsc_id in sorted(unique_ids):
            info = fetch_part_info(lcsc_id)
            if info:
                live_info[lcsc_id] = info

    parts: list[dict[str, Any]] = []
    unresolved: list[str] = []
    parts_total = 0.0
    have_prices = False
    notes: list[str] = []
    for item in bom_data.get("bom", []):
        line: dict[str, Any] = {
            "designator": item.get("designator"),
            "value": item.get("value"),
            "package": item.get("package"),
            "lcsc": item.get("lcsc"),
            "mpn": item.get("mpn"),
        }
        info = live_info.get(item.get("lcsc") or "")
        if info:
            line["stock"] = info.get("stock")
            line["unit_price_usd"] = info.get("unit_price_usd")
            if info.get("unit_price_usd"):
                parts_total += info["unit_price_usd"] * item.get("quantity", 1) * qty
                have_prices = True
            if not info.get("stock"):
                notes.append(f"{line['designator']}: {line['lcsc']} appears out of stock")
            # MPN cross-check: a curated/LLM id that fetches a different part
            # number is worth a human look before ordering. Prefix-tolerant:
            # LCSC decorates some MPNs (e.g. "0805W8F1003T5E (SMT...)")
            a, b = item.get("mpn", "").upper(), str(info.get("mpn") or "").upper()
            if a and b and not (a.startswith(b) or b.startswith(a)):
                line["needs_review"] = True
                notes.append(f"{line['designator']}: BOM mpn {item['mpn']} != "
                             f"LCSC {info['mpn']} for {line['lcsc']}")
        if not (item.get("lcsc") or item.get("mpn")):
            unresolved.append(item.get("designator", "?"))
        parts.append(line)

    if live and not live_info and any(i.get("lcsc") for i in bom_data.get("bom", [])):
        notes.append("Live LCSC lookups unavailable (offline or rate-limited); "
                     "quote uses catalog data only.")
    if unresolved:
        notes.append(f"{len(unresolved)} BOM line(s) have no part number — "
                     "set 'lcsc'/'mpn' in the BOM or pick parts at order time.")

    return {
        "success": True,
        "board_estimate": board_estimate,
        "parts": parts,
        "parts_total_usd": round(parts_total, 2) if have_prices else None,
        "unresolved": unresolved,
        "notes": notes,
    }
