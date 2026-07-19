"""Fetch footprint pad data from EasyEDA/LCSC via the easyeda2kicad API.

This reuses the optional ``easyeda2kicad`` dependency that is already used
for 3D model downloads.  The same API call returns CAD data that includes
footprint pad geometry — we just need to extract it differently.

Gracefully returns None when the library isn't installed or the network is
unavailable.
"""

from __future__ import annotations

import logging
import re
import time
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from optimizers.pad_geometry import FootprintDef

logger = logging.getLogger(__name__)

# Simple rate limiter: minimum interval between API calls (seconds)
_MIN_INTERVAL = 1.0
_last_call_time = 0.0


def _rate_limit() -> None:
    """Block until at least _MIN_INTERVAL seconds since the last API call."""
    global _last_call_time
    now = time.monotonic()
    elapsed = now - _last_call_time
    if elapsed < _MIN_INTERVAL:
        time.sleep(_MIN_INTERVAL - elapsed)
    _last_call_time = time.monotonic()


def _get_cad_data(component_id: str) -> Any | None:
    """Fetch EasyEDA CAD data for a component.  Returns None on failure."""
    try:
        from easyeda2kicad.easyeda.easyeda_api import EasyedaApi
    except ImportError:
        return None

    _rate_limit()
    try:
        api = EasyedaApi()
        return api.get_cad_data_of_component(lcsc_id=component_id)
    except Exception as e:
        logger.debug(f"EasyEDA API error for {component_id}: {e}")
        return None


def _extract_footprint_from_cad(cad_data: Any) -> "FootprintDef | None":
    """Extract pad positions from EasyEDA CAD data into a FootprintDef.

    EasyEDA footprint data varies by version but the general approach is:
    - Get the footprint importer output
    - Extract pad positions and sizes from the KiCad footprint it generates
    """
    from optimizers.pad_geometry import FootprintDef

    try:
        from easyeda2kicad.easyeda.easyeda_importer import EasyedaFootprintImporter
    except ImportError:
        return None

    try:
        importer = EasyedaFootprintImporter(
            easyeda_cp_cad_data=cad_data,
        )
        footprint = importer.output
        if footprint is None:
            return None

        # The output is a KiCad footprint object.  Extract pad info.
        # easyeda2kicad's KicadFootprint has a .pads list
        pads = getattr(footprint, "pads", None)
        if not pads:
            return None

        pin_offsets: dict[int, tuple[float, float]] = {}
        pad_widths: list[float] = []
        pad_heights: list[float] = []

        for pad in pads:
            # Each pad has: number, pos_x, pos_y, size_x, size_y, etc.
            num_str = getattr(pad, "number", "") or ""
            try:
                num = int(num_str)
            except (ValueError, TypeError):
                continue

            x = float(getattr(pad, "pos_x", 0) or 0)
            y = float(getattr(pad, "pos_y", 0) or 0)
            pin_offsets[num] = (round(x, 4), round(-y, 4))  # Y-axis inversion

            sx = float(getattr(pad, "size_x", 0) or 0)
            sy = float(getattr(pad, "size_y", 0) or 0)
            if sx > 0 and sy > 0:
                pad_widths.append(sx)
                pad_heights.append(sy)

        if not pin_offsets:
            return None

        # Use median pad size
        if pad_widths:
            pad_widths.sort()
            pad_heights.sort()
            mid = len(pad_widths) // 2
            pw = round(pad_widths[mid], 3)
            ph = round(pad_heights[mid], 3)
        else:
            pw, ph = 0.5, 0.5

        return FootprintDef(pin_offsets=pin_offsets, pad_size=(pw, ph))

    except Exception as e:
        logger.debug(f"Failed to extract footprint from EasyEDA data: {e}")
        return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

_LCSC_RE = re.compile(r"^C\d+$", re.IGNORECASE)


def fetch_footprint(value: str, lcsc_id: str = "") -> "FootprintDef | None":
    """Try to fetch a footprint from EasyEDA/LCSC.

    Args:
        value: Component value or part number (e.g., "ATmega328P").
        lcsc_id: LCSC part number if known (e.g., "C14877").

    Returns:
        FootprintDef or None if unavailable.
    """
    # Try LCSC ID first (most reliable)
    component_id = lcsc_id if lcsc_id else ""
    if not component_id and _LCSC_RE.match(value):
        component_id = value

    if component_id:
        cad = _get_cad_data(component_id)
        if cad:
            fp = _extract_footprint_from_cad(cad)
            if fp:
                return fp

    # No LCSC ID or failed — cannot search by name through this API
    return None


# ---------------------------------------------------------------------------
# Live part availability (stock / price / MPN) by LCSC id
# ---------------------------------------------------------------------------

# The products endpoint 403s hard after a burst of requests; once that happens
# stop calling it for a while instead of burning every remaining lookup.
_disabled_until = 0.0
_BACKOFF_S = 600.0


def fetch_part_info(lcsc_id: str) -> dict | None:
    """Fetch live availability for an LCSC part id (e.g. "C14663").

    Uses the same EasyEDA products endpoint as the footprint fetcher, via
    stdlib urllib (no easyeda2kicad needed). Returns {lcsc, mpn, manufacturer,
    stock, unit_price_usd, min_order, basic_part} or None when the id is
    invalid, the network is unavailable, or the API rate-limits (a 403
    disables further lookups for this process for a few minutes).
    """
    global _disabled_until
    if not _LCSC_RE.match(lcsc_id or ""):
        return None
    if time.monotonic() < _disabled_until:
        return None

    import json as _json
    import urllib.error
    import urllib.request

    _rate_limit()
    url = f"https://easyeda.com/api/products/{lcsc_id}/components?version=6.4.19.5"
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
        "Accept": "application/json, text/javascript, */*; q=0.01",
    })
    try:
        data = _json.load(urllib.request.urlopen(req, timeout=15))
    except urllib.error.HTTPError as e:
        if e.code == 403:
            _disabled_until = time.monotonic() + _BACKOFF_S
            logger.warning("EasyEDA part-info API rate-limited (403); "
                           "pausing live lookups for %d s", int(_BACKOFF_S))
        else:
            logger.debug(f"EasyEDA part-info error for {lcsc_id}: {e}")
        return None
    except Exception as e:
        logger.debug(f"EasyEDA part-info error for {lcsc_id}: {e}")
        return None

    result = data.get("result") or {}
    if not isinstance(result, dict) or not result:
        return None
    lcsc = result.get("lcsc") or {}
    szlcsc = result.get("szlcsc") or {}
    c_para = ((result.get("dataStr") or {}).get("head") or {}).get("c_para") or {}
    return {
        "lcsc": lcsc.get("number") or szlcsc.get("number") or lcsc_id,
        "mpn": c_para.get("Manufacturer Part") or result.get("title"),
        "manufacturer": c_para.get("Manufacturer"),
        # lcsc block is USD but sometimes zeroed; szlcsc is the CNY mirror —
        # only the USD price is reported, stock falls back to szlcsc.
        "stock": lcsc.get("stock") or szlcsc.get("stock"),
        "unit_price_usd": lcsc.get("price") or None,
        "min_order": lcsc.get("min") or szlcsc.get("min"),
        "basic_part": c_para.get("JLCPCB Part Class") == "Basic Part",
    }
