"""Fetch 3D STEP models from LCSC/EasyEDA with local caching.

Uses the easyeda2kicad library to download component STEP files by LCSC
part number. Downloaded models are cached locally to avoid repeated API calls.

Requires: pip install easyeda2kicad
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path

logger = logging.getLogger(__name__)


def _get_cache_dir() -> Path:
    """Get the 3D model cache directory."""
    env = os.environ.get("PCB_3D_MODELS_DIR")
    if env:
        p = Path(env).expanduser()
    else:
        p = Path.home() / ".pcb-creator" / "3d-models"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _sanitize_filename(name: str) -> str:
    """Create a filesystem-safe filename from a component identifier."""
    return re.sub(r"[^a-zA-Z0-9_\-.]", "_", name)


def fetch_3d_model_by_lcsc(lcsc_id: str) -> Path | None:
    """Download a STEP model from LCSC/EasyEDA by LCSC part number.

    Args:
        lcsc_id: LCSC part number (e.g. "C2040", "C123456").

    Returns:
        Path to cached STEP file, or None if not available.
    """
    cache_dir = _get_cache_dir()
    cache_path = cache_dir / f"{lcsc_id}.step"

    # Check cache first
    if cache_path.exists() and cache_path.stat().st_size > 0:
        return cache_path

    try:
        from easyeda2kicad.easyeda.easyeda_api import EasyedaApi
        from easyeda2kicad.easyeda.easyeda_importer import Easyeda3dModelImporter
    except ImportError:
        logger.debug("easyeda2kicad not installed — skipping LCSC model fetch")
        return None

    try:
        api = EasyedaApi()
        cad_data = api.get_cad_data_of_component(lcsc_id=lcsc_id)
        if not cad_data:
            logger.debug(f"No CAD data for LCSC {lcsc_id}")
            return None

        importer = Easyeda3dModelImporter(
            easyeda_cp_cad_data=cad_data,
            download_raw_3d_model=True,
        )
        model = importer.output
        if model and model.step:
            cache_path.write_bytes(model.step)
            logger.info(f"Cached 3D model for {lcsc_id} ({len(model.step)} bytes)")
            return cache_path

        logger.debug(f"No STEP model available for LCSC {lcsc_id}")
        return None

    except Exception as e:
        logger.debug(f"Failed to fetch 3D model for {lcsc_id}: {e}")
        return None


def fetch_3d_model(
    package: str,
    value: str = "",
    component_type: str = "",
    lcsc_id: str = "",
) -> Path | None:
    """Try to fetch a 3D STEP model for a component.

    Priority:
    1. Local cache by package name
    2. LCSC download by part number (if provided)
    3. None (caller should use parametric fallback)

    Args:
        package: Package type (e.g. "0805", "SOIC-8", "DIP-28").
        value: Component value (e.g. "ATmega328P", "100nF").
        component_type: Type (e.g. "resistor", "ic").
        lcsc_id: Optional LCSC part number for direct lookup.

    Returns:
        Path to STEP file, or None if not available.
    """
    cache_dir = _get_cache_dir()

    # Check cache by package name
    pkg_cache = cache_dir / f"{_sanitize_filename(package)}.step"
    if pkg_cache.exists() and pkg_cache.stat().st_size > 0:
        return pkg_cache

    # Check cache by value (for specific ICs)
    if value:
        val_cache = cache_dir / f"{_sanitize_filename(value)}.step"
        if val_cache.exists() and val_cache.stat().st_size > 0:
            return val_cache

    # Try LCSC download if we have a part number
    if lcsc_id:
        result = fetch_3d_model_by_lcsc(lcsc_id)
        if result:
            return result

    return None
