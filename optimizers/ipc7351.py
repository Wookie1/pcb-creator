"""IPC-7351B parametric footprint generator.

Generates FootprintDef objects for standard package families using the
IPC-7351B land pattern standard (density level B — "nominal").  Covers
families not handled by pad_geometry.py's hardcoded definitions.

All dimensions are in millimetres.  Pin numbering follows the conventions
used by KiCad and most datasheets (counter-clockwise from pin 1).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from optimizers.pad_geometry import FootprintDef


# ---------------------------------------------------------------------------
# QFN / DFN  (quad/dual flat no-lead)
# ---------------------------------------------------------------------------

def make_qfn(
    pin_count: int,
    body_mm: float = 3.0,
    pitch_mm: float = 0.5,
    exposed_pad: bool = True,
) -> FootprintDef | None:
    """Generate a QFN footprint.

    Pin numbering: CCW from bottom-left of left side, 4 sides.
    Exposed pad (if present) is the last pin number.
    """
    ep_pins = 1 if exposed_pad else 0
    signal_pins = pin_count - ep_pins
    if signal_pins < 4 or signal_pins % 4 != 0:
        return None

    per_side = signal_pins // 4
    offsets: dict[int, tuple[float, float]] = {}

    # Pad dimensions per IPC-7351B for QFN nominal density
    pad_len = 0.8   # toe-to-heel
    pad_w = pitch_mm * 0.55  # slightly narrower than pitch

    half_body = body_mm / 2.0
    # Pad centre sits at body edge minus half pad length (toe extends out)
    pad_centre = half_body - pad_len / 2.0 + 0.25  # 0.25 mm toe extension

    pin = 1
    # Left side (top to bottom)
    span = (per_side - 1) * pitch_mm
    y_start = span / 2.0
    for i in range(per_side):
        offsets[pin] = (round(-pad_centre, 4), round(y_start - i * pitch_mm, 4))
        pin += 1

    # Bottom side (left to right)
    x_start = -span / 2.0
    for i in range(per_side):
        offsets[pin] = (round(x_start + i * pitch_mm, 4), round(pad_centre, 4))
        pin += 1

    # Right side (bottom to top)
    for i in range(per_side):
        offsets[pin] = (round(pad_centre, 4), round(-y_start + i * pitch_mm, 4))
        pin += 1

    # Top side (right to left)
    for i in range(per_side):
        offsets[pin] = (round(-x_start - i * pitch_mm, 4), round(-pad_centre, 4))
        pin += 1

    # Exposed pad at centre
    if exposed_pad:
        offsets[pin] = (0.0, 0.0)

    return FootprintDef(pin_offsets=offsets, pad_size=(pad_w, pad_len))


def make_dfn(
    pin_count: int,
    body_mm: float = 3.0,
    pitch_mm: float = 0.5,
    exposed_pad: bool = False,
) -> FootprintDef | None:
    """Generate a DFN (dual flat no-lead) footprint.

    Two rows of pins on left and right sides.
    """
    ep_pins = 1 if exposed_pad else 0
    signal_pins = pin_count - ep_pins
    if signal_pins < 2 or signal_pins % 2 != 0:
        return None

    per_side = signal_pins // 2
    offsets: dict[int, tuple[float, float]] = {}

    pad_len = 0.8
    pad_w = pitch_mm * 0.55
    half_body = body_mm / 2.0
    pad_centre = half_body - pad_len / 2.0 + 0.25

    pin = 1
    span = (per_side - 1) * pitch_mm
    y_start = -span / 2.0

    # Left side (top to bottom)
    for i in range(per_side):
        offsets[pin] = (round(-pad_centre, 4), round(y_start + i * pitch_mm, 4))
        pin += 1

    # Right side (bottom to top)
    for i in range(per_side):
        offsets[pin] = (round(pad_centre, 4), round(-y_start - i * pitch_mm, 4))
        pin += 1

    if exposed_pad:
        offsets[pin] = (0.0, 0.0)

    return FootprintDef(pin_offsets=offsets, pad_size=(pad_w, pad_len))


# ---------------------------------------------------------------------------
# SOP family (SOP, SSOP, TSSOP, MSOP)
# ---------------------------------------------------------------------------

@dataclass
class _SOPParams:
    pitch_mm: float
    row_span_mm: float   # centre-to-centre of the two pad rows
    pad_width: float
    pad_height: float


_SOP_FAMILIES: dict[str, _SOPParams] = {
    "SOP":   _SOPParams(pitch_mm=1.27, row_span_mm=5.4, pad_width=0.6, pad_height=1.5),
    "SOIC":  _SOPParams(pitch_mm=1.27, row_span_mm=5.4, pad_width=0.6, pad_height=1.5),
    "SSOP":  _SOPParams(pitch_mm=0.65, row_span_mm=5.3, pad_width=0.4, pad_height=1.2),
    "TSSOP": _SOPParams(pitch_mm=0.65, row_span_mm=4.4, pad_width=0.35, pad_height=1.0),
    "MSOP":  _SOPParams(pitch_mm=0.5,  row_span_mm=3.0, pad_width=0.3, pad_height=0.8),
}


def make_sop(
    pin_count: int,
    family: str = "SOP",
    pitch_mm: float | None = None,
    body_width_mm: float | None = None,
) -> FootprintDef | None:
    """Generate a SOP-family footprint (SOP, SSOP, TSSOP, MSOP).

    Pin numbering: 1..N/2 on left (top-to-bottom), N/2+1..N on right
    (bottom-to-top) — standard DIP-style convention.
    """
    if pin_count < 2 or pin_count % 2 != 0:
        return None

    params = _SOP_FAMILIES.get(family.upper())
    if params is None:
        return None

    p = pitch_mm if pitch_mm is not None else params.pitch_mm
    row = params.row_span_mm
    if body_width_mm is not None:
        row = body_width_mm + params.pad_height  # pads extend beyond body

    per_side = pin_count // 2
    half_row = row / 2.0
    offsets: dict[int, tuple[float, float]] = {}

    span = (per_side - 1) * p
    y_start = -span / 2.0

    # Left side pins 1..per_side (top to bottom)
    for i in range(per_side):
        offsets[i + 1] = (round(-half_row, 4), round(y_start + i * p, 4))

    # Right side pins per_side+1..pin_count (bottom to top)
    for i in range(per_side):
        offsets[per_side + i + 1] = (round(half_row, 4), round(-y_start - i * p, 4))

    return FootprintDef(
        pin_offsets=offsets,
        pad_size=(params.pad_width, params.pad_height),
    )


# ---------------------------------------------------------------------------
# SOT-223 / SOT-89
# ---------------------------------------------------------------------------

def make_sot223() -> FootprintDef:
    """SOT-223 (4 pins: 3 small + 1 large tab)."""
    return FootprintDef(
        pin_offsets={
            1: (-2.3, -3.15),
            2: (0.0, -3.15),
            3: (2.3, -3.15),
            4: (0.0, 3.15),   # large tab
        },
        pad_size=(0.7, 1.5),
    )


def make_sot89() -> FootprintDef:
    """SOT-89 (3 pins + collector tab)."""
    return FootprintDef(
        pin_offsets={
            1: (-1.5, -2.0),
            2: (0.0, -2.0),
            3: (1.5, -2.0),
        },
        pad_size=(0.55, 1.5),
    )


# ---------------------------------------------------------------------------
# BGA
# ---------------------------------------------------------------------------

def make_bga(
    rows: int,
    cols: int,
    pitch_mm: float = 0.8,
    body_mm: float | None = None,
) -> FootprintDef | None:
    """Generate a BGA footprint.

    Pin numbering is row-major: row A cols 1..N, row B cols 1..N, etc.
    Pin numbers are sequential integers (1-based) for compatibility with
    FootprintDef which requires int keys.
    """
    if rows < 1 or cols < 1:
        return None

    offsets: dict[int, tuple[float, float]] = {}
    pin = 1

    x_start = -(cols - 1) * pitch_mm / 2.0
    y_start = -(rows - 1) * pitch_mm / 2.0

    for r in range(rows):
        for c in range(cols):
            x = round(x_start + c * pitch_mm, 4)
            y = round(y_start + r * pitch_mm, 4)
            offsets[pin] = (x, y)
            pin += 1

    # BGA ball diameter is typically ~0.5 * pitch for solder land
    ball_d = round(pitch_mm * 0.5, 3)
    return FootprintDef(pin_offsets=offsets, pad_size=(ball_d, ball_d))


# ---------------------------------------------------------------------------
# Dispatch: parse package string and pick generator
# ---------------------------------------------------------------------------

# Patterns to match package strings and extract parameters
_PATTERNS: list[tuple[re.Pattern, str]] = [
    # QFN-16, QFN-16-EP, QFN-32_5x5mm, etc.
    (re.compile(r"QFN-?(\d+)", re.IGNORECASE), "qfn"),
    # DFN-8, DFN-6, etc.
    (re.compile(r"DFN-?(\d+)", re.IGNORECASE), "dfn"),
    # SSOP-20, TSSOP-16, MSOP-8, SOP-8
    (re.compile(r"(TSSOP|SSOP|MSOP|SOP)-?(\d+)", re.IGNORECASE), "sop_family"),
    # SOT-223
    (re.compile(r"SOT-?223", re.IGNORECASE), "sot223"),
    # SOT-89
    (re.compile(r"SOT-?89", re.IGNORECASE), "sot89"),
    # BGA-NxM or BGA-N (assumes square)
    (re.compile(r"BGA-?(\d+)(?:x(\d+))?", re.IGNORECASE), "bga"),
]

# Extract body size from package string: "QFN-16_3x3mm" → 3.0
_BODY_RE = re.compile(r"(\d+(?:\.\d+)?)x\d+(?:\.\d+)?mm", re.IGNORECASE)
# Extract pitch: "P0.5mm" → 0.5
_PITCH_RE = re.compile(r"P(\d+(?:\.\d+)?)mm", re.IGNORECASE)


def ipc7351_lookup(package: str, pin_count: int = 0) -> FootprintDef | None:
    """Try to generate a footprint from the package string using IPC-7351.

    Returns None if the package doesn't match any known IPC family.
    """
    for pattern, family in _PATTERNS:
        m = pattern.search(package)
        if not m:
            continue

        # Extract optional body size and pitch from the package string
        body_m = _BODY_RE.search(package)
        body_mm = float(body_m.group(1)) if body_m else None
        pitch_m = _PITCH_RE.search(package)
        pitch_mm = float(pitch_m.group(1)) if pitch_m else None

        if family == "qfn":
            n = int(m.group(1))
            # QFN-N conventionally means N signal pins; EP is extra.
            # The number from the package name (n) is the signal count.
            # pin_count from the caller may or may not include the EP.
            signal = n  # trust the package name for signal pin count
            has_ep = True
            total = signal + (1 if has_ep else 0)
            return make_qfn(
                total,
                body_mm=body_mm or 3.0,
                pitch_mm=pitch_mm or 0.5,
                exposed_pad=has_ep,
            )

        if family == "dfn":
            n = int(m.group(1))
            count = pin_count if pin_count > 0 else n
            return make_dfn(
                count,
                body_mm=body_mm or 3.0,
                pitch_mm=pitch_mm or 0.5,
            )

        if family == "sop_family":
            sop_type = m.group(1).upper()
            n = int(m.group(2))
            count = pin_count if pin_count > 0 else n
            return make_sop(count, family=sop_type, pitch_mm=pitch_mm)

        if family == "sot223":
            return make_sot223()

        if family == "sot89":
            return make_sot89()

        if family == "bga":
            n = int(m.group(1))
            m2 = m.group(2)
            if m2:
                rows, cols = n, int(m2)
            else:
                # Square BGA: find nearest square
                import math
                side = int(math.ceil(math.sqrt(pin_count if pin_count > 0 else n)))
                rows = cols = side
            return make_bga(rows, cols, pitch_mm=pitch_mm or 0.8, body_mm=body_mm)

    return None
