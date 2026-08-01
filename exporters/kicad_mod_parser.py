"""Parse KiCad .kicad_mod footprint files into FootprintDef objects.

Reuses the S-expression parser from kicad_importer.py.  Provides a lazy
library index that scans a KiCad footprint library directory and maps
normalised package names to file paths for fast lookup.
"""

from __future__ import annotations

import re
from collections import Counter
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
    pin_pad_sizes: dict[int, tuple[float, float]] = {}

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
            w = _to_float(size_field[1])
            h = _to_float(size_field[2])
            pad_widths.append(w)
            pad_heights.append(h)
            pin_pad_sizes[pad_num] = (round(w, 4), round(h, 4))

    if not pin_offsets:
        return None

    # Re-centre the pad field on the origin. KiCad anchors a footprint wherever
    # its author chose — pin 1, a mounting hole, a mechanical datum — and 56% of
    # the stock library is NOT centred (e.g. DIP-8_W7.62mm anchors pin 1 at the
    # origin, so its pads sit 3.81mm right and down of it). Every consumer here
    # treats a placement's (x_mm, y_mm) as the component CENTRE: the body box,
    # the clearance checks, the silk relocator. Feeding them corner-anchored
    # offsets makes _get_pad_extent_box union a centred body box with an
    # off-centre pad box, roughly doubling the part's apparent size — which is
    # what made parts "not fit" on boards with room to spare and pushed pads past
    # the board edge. Centring here fixes it once, for every consumer, since the
    # exporters synthesize pads from these offsets rather than referencing the
    # library file.
    xs = [o[0] for o in pin_offsets.values()]
    ys = [o[1] for o in pin_offsets.values()]
    cx = (min(xs) + max(xs)) / 2
    cy = (min(ys) + max(ys)) / 2
    if abs(cx) > 1e-6 or abs(cy) > 1e-6:
        pin_offsets = {n: (round(x - cx, 4), round(y - cy, 4))
                       for n, (x, y) in pin_offsets.items()}

    # Representative pad = the most common (width, height) PAIR.
    #
    # This used to take the median of widths and of heights INDEPENDENTLY, which
    # is wrong for any footprint holding two pad orientations in similar numbers.
    # An LQFP-48 has 24 pads at 0.3×1.475 and 24 at 1.475×0.3; sorting each axis
    # on its own puts both medians at 1.475, yielding a 1.475mm SQUARE — the
    # union of both orientations, and a pad that no longer exists. On 0.5mm pitch
    # that overlaps its neighbours by 0.975mm, so every pad on a side merged into
    # one blob: Freerouting saw the nets already shorted and routed ~0%, and
    # kicad-cli reported clearance 0.0000mm. Taking the modal pair keeps a real
    # pad, and pin_pad_sizes below preserves the per-pin truth.
    if pin_pad_sizes:
        counts = Counter(pin_pad_sizes.values())
        pw, ph = counts.most_common(1)[0][0]
    elif pad_widths:
        pw, ph = round(pad_widths[0], 3), round(pad_heights[0], 3)
    else:
        pw, ph = 0.5, 0.5

    # Only carry per-pin sizes when they actually vary — uniform footprints
    # (every passive, every SOIC) stay on the single representative size.
    varied = {n: s for n, s in pin_pad_sizes.items() if s != (pw, ph)}

    return FootprintDef(pin_offsets=pin_offsets, pad_size=(pw, ph),
                        pin_pad_sizes=pin_pad_sizes if varied else None)


# ---------------------------------------------------------------------------
# Library index
# ---------------------------------------------------------------------------

# Regexes to extract the short package name from a KiCad footprint filename.
#
# Two flavours, because the separator after the base name tells you whether the
# rest of the filename describes the SAME part or a DIFFERENT one:
#
#   "_" → dimensions/variant of the same part.  "DIP-8_W7.62mm" is a DIP-8.
#   "-" → the base name continues with another number, naming a different part
#         or a range.  "DIP-8-16_W7.62mm_Socket" is a socket that accepts DIP-8
#         *through DIP-16*; its body is 17.78mm long, not 7.62mm.
#
# Both still produce the alias (a "-" file is better than nothing when no "_"
# file exists — see test_generated_alias_still_fills_empty_slot), but the index
# registers them in separate passes so the "_" form always wins the key.
_ALIAS_DIM_RE = re.compile(
    r"^([A-Za-z][\w-]*?\d+)"   # base name + first number
    r"(?:[-_]\d+EP)?"          # optional exposed-pad suffix
    r"_"                       # dimension/variant suffix of the SAME part
)
_ALIAS_CONT_RE = re.compile(
    r"^([A-Za-z][\w-]*?\d+)"   # base name + first number
    r"(?:[-_]\d+EP)?"          # optional exposed-pad suffix
    r"-\d"                     # base name continues — a different part/range
)


def _alias_tiers(filename_stem: str) -> tuple[str | None, str | None]:
    """(dimension_alias, continuation_alias) for a filename stem, uppercased.

    Either may be None. See the regex comments above for why they rank
    differently.
    """
    stem_upper = filename_stem.upper()

    def _short(rx: re.Pattern[str]) -> str | None:
        m = rx.match(filename_stem)
        if not m:
            return None
        short = m.group(1).upper()
        return short if short != stem_upper else None

    return _short(_ALIAS_DIM_RE), _short(_ALIAS_CONT_RE)


def _generate_aliases(filename_stem: str) -> list[str]:
    """Generate lookup aliases from a KiCad footprint filename stem.

    Returns a list of normalised aliases (uppercase), most specific first.
    """
    aliases = [filename_stem.upper()]
    for alias in _alias_tiers(filename_stem):
        if alias and alias not in aliases:
            aliases.append(alias)
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

        files = list(self._root.rglob("*.kicad_mod"))
        # Three passes, each filling only empty slots, so resolution is
        # independent of scan order:
        #   1. exact filename stems — a real SOT-23.kicad_mod always wins
        #      "SOT-23" over any alias.
        #   2. "_"-separated aliases — a dimension/variant suffix of the same
        #      part (DIP-8_W7.62mm → DIP-8).
        #   3. "-"-separated aliases — the base name continues into a different
        #      part or range (DIP-8-16_W7.62mm_Socket → DIP-8), acceptable only
        #      when nothing better claimed the key.
        # Within an alias pass, the plainest stem wins: fewest "_"-separated
        # suffix segments, then shortest. "DIP-8" should mean DIP-8_W7.62mm, not
        # DIP-8_W8.89mm_SMDSocket_LongPads — a bare package name asks for the
        # ordinary part, and the specific variants are still reachable by their
        # full names.
        #
        # When the plainest candidates TIE, what they disagree about decides
        # whether the alias is usable:
        #
        #   Same pitch, different body — SOIC-8_3.9x4.9mm_P1.27mm vs
        #   _5.3x6.2mm_ vs _5.3x5.3mm_. Any of them is a real SOIC-8 with the
        #   right 1.27mm pitch; the body size differs slightly. Pick one
        #   deterministically (by name, so it never depends on directory order).
        #
        #   Different pitch — PinHeader_1x15_P1.00mm/_P1.27mm/_P2.54mm. These are
        #   not variants of one part, they are different parts. Guessing turns a
        #   0.1" header into a 0.05" one, which lands 15 pins in half the space
        #   at half the pad size. Drop the alias so the IPC-7351 and built-in
        #   tiers supply their sensible default, which beats a coin flip between
        #   incompatible footprints.
        #
        # An alias rejected as ambiguous at one tier stays rejected: a "-N"
        # continuation file must not rescue a name the "_" variants could not
        # agree on (that is how SOIC-8 briefly resolved to an exposed-pad part).
        def _plainness(f: Path) -> tuple[int, int]:
            return (f.stem.count("_"), len(f.stem))

        def _pitch(f: Path) -> str | None:
            m = re.search(r"[_-]P(\d+(?:\.\d+)?)mm", f.stem)
            return m.group(1) if m else None

        for mod_file in files:
            index.setdefault(mod_file.stem.upper(), mod_file)
        ambiguous: set[str] = set()
        for tier in (0, 1):
            claims: dict[str, list[Path]] = {}
            for mod_file in files:
                alias = _alias_tiers(mod_file.stem)[tier]
                if alias and alias not in index and alias not in ambiguous:
                    claims.setdefault(alias, []).append(mod_file)
            for alias, candidates in claims.items():
                rank = min(_plainness(c) for c in candidates)
                tied = [c for c in candidates if _plainness(c) == rank]
                if len({_pitch(c) for c in tied}) > 1:
                    ambiguous.add(alias)     # different parts — let a lower tier decide
                    continue
                index.setdefault(alias, min(tied, key=lambda f: f.stem))

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
