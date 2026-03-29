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


def fetch_specs(lcsc_id: str) -> dict | None:
    """Extract basic specs from EasyEDA component metadata.

    Currently returns pin_count and package if available.
    """
    if not lcsc_id:
        return None

    cad = _get_cad_data(lcsc_id)
    if cad is None:
        return None

    try:
        specs: dict = {}
        # EasyEDA CAD data often has these attributes
        if hasattr(cad, "info"):
            info = cad.info
            pkg = getattr(info, "package", None) or getattr(info, "footprint", None)
            if pkg:
                specs["package"] = str(pkg)

        return specs if specs else None

    except Exception as e:
        logger.debug(f"Failed to extract specs from EasyEDA: {e}")
        return None
