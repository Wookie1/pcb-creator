"""Thread-safe JSON cache for resolved component footprints and specs.

Stores results from all resolution tiers (curated, KiCad, EasyEDA, LLM) in a
single JSON file so subsequent runs can skip expensive lookups.  Each entry
records *which* tier resolved it and whether it needs human review.
"""

from __future__ import annotations

import json
import threading
from datetime import date
from pathlib import Path
from typing import Any


_DEFAULT_PATH = Path("~/.pcb-creator/component_cache.json").expanduser()


class ComponentCache:
    """Lazy-loaded, write-through JSON cache with two sections.

    Sections
    --------
    footprints : package_name → {pin_offsets, pad_size, source, resolved, needs_review}
    specs      : lookup_key   → {<spec fields>, source, resolved, needs_review}

    The file is read on first access and rewritten on every mutation so no data
    is lost if the process exits unexpectedly.
    """

    def __init__(self, path: str | Path | None = None) -> None:
        self._path = Path(path).expanduser() if path else _DEFAULT_PATH
        self._lock = threading.Lock()
        self._data: dict[str, dict[str, Any]] | None = None  # lazy

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _ensure_loaded(self) -> dict[str, dict[str, Any]]:
        """Load from disk on first access.  Never called outside the lock."""
        if self._data is None:
            if self._path.exists():
                try:
                    self._data = json.loads(self._path.read_text())
                except (json.JSONDecodeError, OSError):
                    self._data = {"footprints": {}, "specs": {}}
            else:
                self._data = {"footprints": {}, "specs": {}}
            # Ensure both sections exist even if file was partial
            self._data.setdefault("footprints", {})
            self._data.setdefault("specs", {})
        return self._data

    def _flush(self) -> None:
        """Write current state to disk.  Called inside the lock after mutations."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._data, indent=2, default=str))
        tmp.replace(self._path)

    @staticmethod
    def _normalise_key(key: str) -> str:
        return key.strip().upper()

    # ------------------------------------------------------------------
    # Footprint accessors
    # ------------------------------------------------------------------

    def get_footprint(self, package: str) -> dict[str, Any] | None:
        """Return cached footprint dict or *None*."""
        key = self._normalise_key(package)
        with self._lock:
            data = self._ensure_loaded()
            return data["footprints"].get(key)

    def put_footprint(
        self,
        package: str,
        pin_offsets: dict[str, list[float]],
        pad_size: list[float] | tuple[float, float],
        source: str,
        needs_review: bool = False,
    ) -> None:
        """Store a footprint entry and flush to disk."""
        key = self._normalise_key(package)
        entry: dict[str, Any] = {
            "pin_offsets": pin_offsets,
            "pad_size": list(pad_size),
            "source": source,
            "resolved": date.today().isoformat(),
            "needs_review": needs_review,
        }
        with self._lock:
            data = self._ensure_loaded()
            data["footprints"][key] = entry
            self._flush()

    # ------------------------------------------------------------------
    # Spec accessors
    # ------------------------------------------------------------------

    def get_specs(self, key: str) -> dict[str, Any] | None:
        """Return cached spec dict or *None*."""
        nkey = self._normalise_key(key)
        with self._lock:
            data = self._ensure_loaded()
            return data["specs"].get(nkey)

    def put_specs(
        self,
        key: str,
        specs: dict[str, Any],
        source: str,
        needs_review: bool = False,
    ) -> None:
        """Store a spec entry and flush to disk."""
        nkey = self._normalise_key(key)
        entry = dict(specs)
        entry["source"] = source
        entry["resolved"] = date.today().isoformat()
        entry["needs_review"] = needs_review
        with self._lock:
            data = self._ensure_loaded()
            data["specs"][nkey] = entry
            self._flush()
