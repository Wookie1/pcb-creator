"""Parse KiCad .kicad_mod footprint files into FootprintDef objects.

Reuses the S-expression parser from kicad_importer.py.  Provides a lazy
library index that scans a KiCad footprint library directory and maps
normalised package names to file paths for fast lookup.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import TYPE_CHECKING

from exporters.kicad_importer import (
    parse_kicad_sexpr,
    _find_all,
    _find_field,
    _to_float,
)

if TYPE_CHECKING:
    from optimizers.pad_geometry import FootprintDef


# ---------------------------------------------------------------------------
# Single-file parser
# ---------------------------------------------------------------------------

def parse_kicad_mod(path: str | Path) -> "FootprintDef | None":
    """Parse a .kicad_mod file and return a FootprintDef.

    Returns None if the file can't be parsed or contains no pads.
    """
    from optimizers.pad_geometry import FootprintDef

    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None

    tree = parse_kicad_sexpr(text)
    if not tree:
        return None

    # File wraps as [["footprint", name, ...]] or [["module", name, ...]]
    root = tree[0] if tree and isinstance(tree[0], list) else tree
    if not root:
        return None

    pads = _find_all(root, "pad")
    if not pads:
        return None

    pin_offsets: dict[int, tuple[float, float]] = {}
    pad_widths: list[float] = []
    pad_heights: list[float] = []

    for pad in pads:
        # (pad <number> <type> <shape> (at x y [rot]) (size w h) ...)
        if len(pad) < 3:
            continue

        pad_num_str = str(pad[1])
        # Skip non-numbered pads (e.g., "" mounting pads)
        if not pad_num_str or pad_num_str == "":
            continue

        # Try to parse pad number as int; fall back to sequential
        try:
            pad_num = int(pad_num_str)
        except ValueError:
            # Lettered pads like "A1" for BGA — skip for now
            # (BGA needs a dedicated handler)
            continue

        at_field = _find_field(pad, "at")
        size_field = _find_field(pad, "size")

        if not at_field or len(at_field) < 3:
            continue

        x = _to_float(at_field[1])
        y = _to_float(at_field[2])

        # KiCad Y-axis is inverted relative to our convention
        pin_offsets[pad_num] = (round(x, 4), round(-y, 4))

        if size_field and len(size_field) >= 3:
            pad_widths.append(_to_float(size_field[1]))
            pad_heights.append(_to_float(size_field[2]))

    if not pin_offsets:
        return None

    # Use median pad size (most common pad in the footprint)
    if pad_widths:
        pad_widths.sort()
        pad_heights.sort()
        mid = len(pad_widths) // 2
        pw = round(pad_widths[mid], 3)
        ph = round(pad_heights[mid], 3)
    else:
        pw, ph = 0.5, 0.5

    return FootprintDef(pin_offsets=pin_offsets, pad_size=(pw, ph))


# ---------------------------------------------------------------------------
# Library index
# ---------------------------------------------------------------------------

# Regex to extract the short package name from a KiCad footprint filename.
# E.g., "SOIC-8_3.9x4.9mm_P1.27mm" → "SOIC-8"
# E.g., "QFN-16-1EP_3x3mm_P0.5mm" → "QFN-16"
_ALIAS_RE = re.compile(
    r"^([A-Za-z][\w-]*?\d+)"  # base name + first number
    r"(?:[-_]\d+EP)?"          # optional exposed-pad suffix
    r"[-_]"                    # separator before dimensions
    r"\d"                      # start of dimension string
)


def _generate_aliases(filename_stem: str) -> list[str]:
    """Generate lookup aliases from a KiCad footprint filename stem.

    Returns a list of normalised aliases (uppercase), most specific first.
    """
    aliases = [filename_stem.upper()]

    m = _ALIAS_RE.match(filename_stem)
    if m:
        short = m.group(1).upper()
        if short != aliases[0]:
            aliases.append(short)

    return aliases


class KiCadLibraryIndex:
    """Lazy index mapping normalised package names to .kicad_mod file paths.

    The index is built on first lookup by scanning the library directory.
    Multiple aliases are generated per file so that both "SOIC-8" and
    "SOIC-8_3.9X4.9MM_P1.27MM" resolve to the same footprint.
    """

    def __init__(self, library_root: str | Path) -> None:
        self._root = Path(library_root)
        self._index: dict[str, Path] | None = None  # lazy
        # Parsed-footprint cache keyed by resolved file path. Parsing a
        # .kicad_mod (read + tokenize + s-expr parse) is expensive and the hot
        # placement loops (repair/optimize) resolve the same packages thousands
        # of times — without this, repair_placement re-parses every footprint on
        # every iteration (≈2000s for morgan on a Pi). A FootprintDef for a given
        # file never changes within a run, so caching the parse is safe.
        self._parsed: dict[Path, "FootprintDef | None"] = {}

    def _build_index(self) -> dict[str, Path]:
        """Scan all .kicad_mod files and build the alias map."""
        index: dict[str, Path] = {}
        if not self._root.is_dir():
            return index

        for mod_file in self._root.rglob("*.kicad_mod"):
            stem = mod_file.stem
            for alias in _generate_aliases(stem):
                # First file wins for each alias (most specific filenames
                # are typically in the right .pretty directory)
                if alias not in index:
                    index[alias] = mod_file

        return index

    def _ensure_index(self) -> dict[str, Path]:
        if self._index is None:
            self._index = self._build_index()
        return self._index

    def get_footprint(self, package: str, pin_count: int = 0) -> "FootprintDef | None":
        """Look up a footprint by package name.

        Tries the full package name first, then falls back to generated
        aliases.  *pin_count* is used to validate the match — if the parsed
        footprint has a different pin count and pin_count > 0, the match is
        rejected to avoid silent mismatches.
        """
        index = self._ensure_index()
        key = package.strip().upper()

        # Try exact match, then aliases of the query
        candidates = [key] + _generate_aliases(package)
        path: Path | None = None
        matched: str = ""
        for candidate in candidates:
            if candidate in index:
                path = index[candidate]
                matched = candidate
                break

        if path is None:
            return None

        if path in self._parsed:
            fp = self._parsed[path]
        else:
            fp = parse_kicad_mod(path)
            self._parsed[path] = fp
        if fp is None:
            return None

        # Validate pin count if caller specified one. pin_count is the number of
        # CONNECTED pins (ports exist only for pins in a net), so a footprint
        # legitimately has >= that many pads (NC pins are normal).
        if pin_count > 0:
            n = len(fp.pin_offsets)
            if n < pin_count:
                return None  # footprint has fewer pads than the design needs
            if n > pin_count and matched != path.stem.upper():
                # Extra pads, and the query only hit a DEGENERATE short alias
                # (e.g. "SOT-23" resolving to "SOT-23-5_HandSoldering") rather
                # than the footprint's full name — almost certainly an alias
                # collision, not a part with NC pins. Reject so the tiered
                # lookup falls back to the correct generated footprint. Extra
                # pads are trusted only when the caller named the footprint
                # exactly (real NC-pin parts: TO-220-3 with 2 wired, etc.).
                return None

        return fp

    def invalidate(self) -> None:
        """Reset the lazy index so the next lookup rebuilds from disk.

        Call this after writing new .kicad_mod files into the library root so
        that subsequent ``get_footprint`` calls see the new files.
        """
        self._index = None
        self._parsed.clear()

    @property
    def root(self) -> Path:
        """The root directory this index scans."""
        return self._root

    @property
    def size(self) -> int:
        """Number of entries in the index (builds index if needed)."""
        return len(self._ensure_index())
