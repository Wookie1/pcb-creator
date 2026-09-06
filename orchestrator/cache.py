"""Thread- and process-safe JSON cache for resolved component footprints and specs.

Stores results from all resolution tiers (curated, KiCad, EasyEDA, LLM) in a
single JSON file so subsequent runs can skip expensive lookups.  Each entry
records *which* tier resolved it and whether it needs human review.

The MCP server, CLI runs and helper scripts share one cache file and may run
concurrently, so every read-modify-write cycle re-reads the file from disk
under an advisory file lock (a sidecar ``<cache>.lock``).  A writer therefore
merges with whatever other processes have already committed instead of
overwriting the whole file from a stale in-memory copy (last-writer-wins
lost-update bug, audit F8).
"""

from __future__ import annotations

import json
import os
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date
from pathlib import Path
from typing import Any

try:  # POSIX (macOS / Linux)
    import fcntl
except ImportError:  # pragma: no cover - Windows has no fcntl
    fcntl = None  # type: ignore[assignment]

try:  # Windows
    import msvcrt
except ImportError:  # pragma: no cover - POSIX / macOS has no msvcrt
    msvcrt = None  # type: ignore[assignment]


_DEFAULT_PATH = Path("~/.pcb-creator/component_cache.json").expanduser()

_SECTIONS = ("footprints", "specs")

_DEFAULT_LOCK_TIMEOUT = 10.0
_LOCK_POLL = 0.05


class ComponentCache:
    """Lazy-loaded, write-through JSON cache with two sections.

    Sections
    --------
    footprints : package_name → {pin_offsets, pad_size, source, resolved, needs_review}
    specs      : lookup_key   → {<spec fields>, source, resolved, needs_review}

    The file is read on first access and rewritten on every mutation so no data
    is lost if the process exits unexpectedly.  Mutations take a threading lock
    (same instance) *and* a best-effort cross-process file lock; under those
    locks the file is re-read and merged, so a concurrent writer can never
    wipe this process's entries (or be wiped by it).
    """

    def __init__(self, path: str | Path | None = None,
                 lock_timeout: float = _DEFAULT_LOCK_TIMEOUT) -> None:
        self._path = Path(path).expanduser() if path else _DEFAULT_PATH
        self._lock = threading.Lock()
        self._lock_timeout = lock_timeout
        self._data: dict[str, dict[str, Any]] | None = None  # lazy

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @property
    def _lock_path(self) -> Path:
        return self._path.with_name(self._path.name + ".lock")

    def _parse(self) -> dict[str, Any] | None:
        """Parse the cache file; None if missing, unreadable or not a dict."""
        if not self._path.exists():
            return None
        try:
            data = json.loads(self._path.read_text())
        except (json.JSONDecodeError, OSError):  # corrupt / concurrently rewritten
            return None
        return data if isinstance(data, dict) else None

    @staticmethod
    def _sections(data: dict[str, Any]) -> dict[str, dict[str, Any]]:
        """Return both sections as dicts, synthesizing missing/garbage ones."""
        return {s: data[s] if isinstance(data.get(s), dict) else {}
                for s in _SECTIONS}

    def _ensure_loaded(self) -> dict[str, dict[str, Any]]:
        """Load from disk on first access.  Never called outside the lock."""
        if self._data is None:
            parsed = self._parse()
            # Missing/corrupt file starts empty; a valid file is adopted as-is
            # (garbage sections are replaced so later writes never crash).
            self._data = self._sections(parsed if parsed is not None else {})
        return self._data

    def _reload_for_write(self) -> dict[str, dict[str, Any]]:
        """Re-read the file for a write cycle.  Called inside both locks.

        Starting from the on-disk state rather than this instance's possibly
        stale memory copy is the merge step: entries another process committed
        since our last flush survive.  When the file is missing or corrupt we
        fall back to the in-memory state (recovery must not destroy entries
        we already hold).
        """
        parsed = self._parse()
        if parsed is not None:
            data = self._sections(parsed)
        elif self._data is not None:
            data = {s: dict(self._data[s]) for s in _SECTIONS}
        else:
            data = {s: {} for s in _SECTIONS}
        self._data = data
        return data

    def _flush(self) -> None:
        """Write current state to disk.  Called inside both locks."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._data, indent=2, default=str))
        tmp.replace(self._path)

    @contextmanager
    def _file_lock(self) -> Iterator[None]:
        """Best-effort advisory lock spanning one whole read-modify-write.

        Contenders queue on a sidecar ``<cache>.lock`` file that is never
        renamed or truncated; flock()/msvcrt.locking() release it at the OS
        level even if the holder dies, so a crashed process cannot wedge the
        cache.  Reads don't take it: ``_flush`` publishes via an atomic
        replace, so readers always see a complete old or new file.

        If the lock cannot be acquired within ``lock_timeout`` we proceed
        *unlocked*: the cache is an optimisation, and hanging a design run
        behind a wedged writer is worse than a possible lost entry.  Returns
        immediately (no file lock) when no OS locking primitive exists.
        """
        if fcntl is None and msvcrt is None:  # pragma: no cover - exotic platform without any file-lock primitive
            yield
            return
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(self._lock_path, os.O_CREAT | os.O_RDWR)
        except OSError:  # pragma: no cover - unwritable cache dir (e.g. read-only HOME)
            yield
            return
        acquired = False
        try:
            deadline = time.monotonic() + self._lock_timeout
            while True:
                try:
                    if fcntl is not None:
                        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    else:  # pragma: no cover - Windows-only branch (dev/test platforms are POSIX)
                        os.lseek(fd, 0, os.SEEK_SET)
                        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                    acquired = True
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        break  # writer wedged elsewhere — degrade to unlocked
                    time.sleep(_LOCK_POLL)
            yield
        finally:
            if acquired:
                if fcntl is not None:
                    fcntl.flock(fd, fcntl.LOCK_UN)
                else:  # pragma: no cover - Windows-only branch (dev/test platforms are POSIX)
                    os.lseek(fd, 0, os.SEEK_SET)
                    msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            os.close(fd)

    def _store(self, section: str, key: str, entry: dict[str, Any]) -> None:
        """Merge one entry into the file under the thread + file locks."""
        with self._lock, self._file_lock():
            data = self._reload_for_write()
            data[section][key] = entry
            self._flush()

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
        self._store("footprints", key, entry)

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
        self._store("specs", nkey, entry)
