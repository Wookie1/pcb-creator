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
# QFP (gull-wing quad flat pack: TQFP / LQFP / QFP)
# ---------------------------------------------------------------------------

# Common JEDEC body size / pitch by pin count (used when the package string
# carries no explicit dimensions).
_QFP_DEFAULTS: dict[int, tuple[float, float]] = {
    32: (7.0, 0.8),
    44: (10.0, 0.8),
    48: (7.0, 0.5),
    64: (10.0, 0.5),
    80: (12.0, 0.5),
    100: (14.0, 0.5),
    144: (20.0, 0.5),
    176: (24.0, 0.5),
}


def make_qfp(
    pin_count: int,
    body_mm: float | None = None,
    pitch_mm: float | None = None,
) -> FootprintDef | None:
    """Generate a QFP footprint (gull-wing leads extending beyond the body).

    Pin numbering: CCW from top of left side (KiCad/datasheet convention).
    """
    if pin_count < 8 or pin_count % 4 != 0:
        return None
    if body_mm is None or pitch_mm is None:
        defaults = _QFP_DEFAULTS.get(pin_count)
        if defaults is None:
            # Derive a plausible body from pitch=0.5: per-side span + margin
            pitch_mm = pitch_mm or 0.5
            body_mm = body_mm or round((pin_count // 4 - 1) * pitch_mm + 2.0, 1)
        else:
            body_mm = body_mm or defaults[0]
            pitch_mm = pitch_mm or defaults[1]

    per_side = pin_count // 4
    # Gull-wing leads extend ~1mm beyond the body on each side
    pad_len = 1.5
    pad_w = round(pitch_mm * 0.55, 3)
    pad_centre = body_mm / 2.0 + 1.0  # lead-tip land centre

    offsets: dict[int, tuple[float, float]] = {}
    span = (per_side - 1) * pitch_mm
    pin = 1
    # Left side (top to bottom)
    for i in range(per_side):
        offsets[pin] = (round(-pad_centre, 4), round(span / 2 - i * pitch_mm, 4))
        pin += 1
    # Bottom side (left to right)
    for i in range(per_side):
        offsets[pin] = (round(-span / 2 + i * pitch_mm, 4), round(pad_centre, 4))
        pin += 1
    # Right side (bottom to top)
    for i in range(per_side):
        offsets[pin] = (round(pad_centre, 4), round(-span / 2 + i * pitch_mm, 4))
        pin += 1
    # Top side (right to left)
    for i in range(per_side):
        offsets[pin] = (round(span / 2 - i * pitch_mm, 4), round(-pad_centre, 4))
        pin += 1

    return FootprintDef(pin_offsets=offsets, pad_size=(pad_len, pad_w))


# ---------------------------------------------------------------------------
# SOD (small-outline diode): 2 pads, pin 1 = cathode
# ---------------------------------------------------------------------------

_SOD_FAMILIES: dict[str, tuple[float, tuple[float, float]]] = {
    # name -> (pad-centre spacing, (pad_w, pad_h))
    "123": (3.4, (0.9, 1.2)),
    "323": (2.4, (0.6, 0.9)),
    "523": (1.6, (0.5, 0.7)),
    "723": (1.2, (0.4, 0.55)),
    "80": (3.5, (1.0, 1.4)),
}


def make_sod(variant: str) -> FootprintDef | None:
    params = _SOD_FAMILIES.get(variant)
    if params is None:
        return None
    spacing, pad = params
    half = spacing / 2.0
    return FootprintDef(
        pin_offsets={1: (-half, 0.0), 2: (half, 0.0)},
        pad_size=pad,
    )


# ---------------------------------------------------------------------------
# DO-214 SMD diodes (SMA / SMB / SMC): 2 pads, pin 1 = cathode
# ---------------------------------------------------------------------------

_DO214_FAMILIES: dict[str, tuple[float, tuple[float, float]]] = {
    "SMA": (4.0, (1.5, 1.8)),
    "SMB": (4.3, (2.0, 2.1)),
    "SMC": (6.9, (2.3, 3.0)),
}


def make_do214(variant: str) -> FootprintDef | None:
    params = _DO214_FAMILIES.get(variant.upper())
    if params is None:
        return None
    spacing, pad = params
    half = spacing / 2.0
    return FootprintDef(
        pin_offsets={1: (-half, 0.0), 2: (half, 0.0)},
        pad_size=pad,
    )


# ---------------------------------------------------------------------------
# ESP8266 castellated modules (ESP-12E/F/S, ESP-07): 2 rows of 8, 2mm pitch
# ---------------------------------------------------------------------------

def make_esp12(pin_count: int = 22) -> FootprintDef:
    """ESP-12 style module: castellated pads, 2mm pitch.

    Two variants, both per the module datasheets:
    - 22 pins (ESP-12E/F): 1-8 down the left edge, 9-14 the six bottom
      programming pads (left to right), 15-22 up the right edge.
    - 16 pins (classic ESP-12): 1-8 down the left edge, 9-16 up the right.
    """
    offsets: dict[int, tuple[float, float]] = {}
    pitch = 2.0
    half_w = 7.6  # pad centres ~15.2mm apart on a 16mm-wide module
    span = 7 * pitch
    for i in range(8):  # left edge, top to bottom
        offsets[i + 1] = (-half_w, round(-span / 2 + i * pitch, 4))
    if pin_count >= 22:
        bottom_span = 5 * pitch
        for i in range(6):  # bottom edge, left to right
            offsets[i + 9] = (round(-bottom_span / 2 + i * pitch, 4), 10.0)
        for i in range(8):  # right edge, bottom to top
            offsets[i + 15] = (half_w, round(span / 2 - i * pitch, 4))
    else:
        for i in range(8):  # right edge, bottom to top
            offsets[i + 9] = (half_w, round(span / 2 - i * pitch, 4))
    return FootprintDef(pin_offsets=offsets, pad_size=(2.0, 1.1))


# ---------------------------------------------------------------------------
# MC-306 SMD tuning-fork crystal: 2 pads
# ---------------------------------------------------------------------------

def make_mc306() -> FootprintDef:
    return FootprintDef(
        pin_offsets={1: (-2.35, 0.0), 2: (2.35, 0.0)},
        pad_size=(1.7, 1.4),
    )


# ---------------------------------------------------------------------------
# DPAK / D2PAK power packages (TO-252 / TO-263): leads + tab as last pad
# ---------------------------------------------------------------------------

def make_dpak(lead_count: int, d2: bool = False) -> FootprintDef | None:
    """TO-252 (DPAK) / TO-263 (D2PAK) with N leads and the tab as pad N+1."""
    if lead_count < 2 or lead_count > 9:
        return None
    if d2:
        pitch = 2.54 if lead_count <= 3 else 1.7
        lead_y, tab_y = 5.5, -2.5
        tab_size = (10.0, 8.0)
        pad = (1.0 if lead_count > 3 else 1.4, 2.2)
    else:
        pitch = 2.28 if lead_count <= 3 else 1.27
        lead_y, tab_y = 4.5, -2.0
        tab_size = (6.2, 5.8)
        pad = (0.9 if lead_count > 3 else 1.3, 1.8)

    offsets: dict[int, tuple[float, float]] = {}
    span = (lead_count - 1) * pitch
    for i in range(lead_count):
        offsets[i + 1] = (round(-span / 2 + i * pitch, 4), lead_y)
    offsets[lead_count + 1] = (0.0, tab_y)  # tab (heatsink/center pin)
    # pad_size applies to leads; the tab is approximated by the same pad size
    # extent at its centre (placement spacing comes from pad extents).
    _ = tab_size
    return FootprintDef(pin_offsets=offsets, pad_size=pad)


# ---------------------------------------------------------------------------
# Screw terminals: 1xN row, default 5mm pitch
# ---------------------------------------------------------------------------

def make_screw_terminal(positions: int, pitch_mm: float = 5.0) -> FootprintDef | None:
    if positions < 1 or positions > 24:
        return None
    span = (positions - 1) * pitch_mm
    offsets = {i + 1: (round(-span / 2 + i * pitch_mm, 4), 0.0)
               for i in range(positions)}
    return FootprintDef(pin_offsets=offsets, pad_size=(2.4, 2.4))


# ---------------------------------------------------------------------------
# Multiwatt (staggered power package, e.g. L298N Multiwatt-15)
# ---------------------------------------------------------------------------

def make_multiwatt(pin_count: int) -> FootprintDef | None:
    if pin_count < 9 or pin_count > 25:
        return None
    pitch = 1.7
    span = (pin_count - 1) * pitch
    offsets: dict[int, tuple[float, float]] = {}
    for i in range(pin_count):
        # Staggered two-row THT pins: odd pins front row, even pins back row
        y = 0.0 if i % 2 == 0 else 2.7
        offsets[i + 1] = (round(-span / 2 + i * pitch, 4), y)
    return FootprintDef(pin_offsets=offsets, pad_size=(1.5, 1.5))


# ---------------------------------------------------------------------------
# Generic dimensional packages the LLM pipeline emits: "12x12mm" SMD power
# inductors and "electrolytic_10x12" radial capacitors.
# ---------------------------------------------------------------------------

def make_smd_2pad_body(length_mm: float, width_mm: float) -> FootprintDef | None:
    """Generic 2-pad SMD part described only by body size (power inductors,
    large 2-terminal parts): pads under the body ends."""
    if length_mm < 1.0 or length_mm > 60 or width_mm < 1.0 or width_mm > 60:
        return None
    pad_len = round(length_mm * 0.35, 2)
    pad_w = round(width_mm * 0.6, 2)
    centre = round(length_mm / 2 - pad_len / 2, 3)
    return FootprintDef(
        pin_offsets={1: (-centre, 0.0), 2: (centre, 0.0)},
        pad_size=(pad_len, pad_w),
    )


def make_radial_electrolytic(diameter_mm: float) -> FootprintDef | None:
    """Radial THT electrolytic: 2 leads at the standard spacing for the can
    diameter. Pin 1 = positive."""
    if diameter_mm < 3 or diameter_mm > 40:
        return None
    if diameter_mm <= 5:
        spacing, pad = 2.0, 1.4
    elif diameter_mm <= 6.3:
        spacing, pad = 2.5, 1.6
    elif diameter_mm <= 8:
        spacing, pad = 3.5, 1.8
    else:
        spacing, pad = 5.0, 2.0
    half = spacing / 2.0
    return FootprintDef(
        pin_offsets={1: (-half, 0.0), 2: (half, 0.0)},
        pad_size=(pad, pad),
        is_through_hole=True,
    )


# ---------------------------------------------------------------------------
# Mounting holes: single pad whose extent is the annulus, so the placement
# engine keeps components away from the hole (M3: 3.2mm hole → ~6.4mm pad).
# ---------------------------------------------------------------------------

def make_mounting_hole(hole_mm: float) -> FootprintDef:
    pad_d = round(hole_mm * 2.0, 2)
    return FootprintDef(pin_offsets={1: (0.0, 0.0)}, pad_size=(pad_d, pad_d))


# ---------------------------------------------------------------------------
# 3mm/3x3 SMD trimmer potentiometer (3 pads in a triangle)
# ---------------------------------------------------------------------------

def make_trimmer_3mm() -> FootprintDef:
    return FootprintDef(
        pin_offsets={1: (-1.3, 1.0), 2: (0.0, -1.0), 3: (1.3, 1.0)},
        pad_size=(0.8, 0.9),
    )


# ---------------------------------------------------------------------------
# Dispatch: parse package string and pick generator
# ---------------------------------------------------------------------------

# Patterns to match package strings and extract parameters
_PATTERNS: list[tuple[re.Pattern, str]] = [
    # QFN-16, QFN-16-EP, QFN-32_5x5mm, etc.
    (re.compile(r"QFN-?(\d+)", re.IGNORECASE), "qfn"),
    # DFN-8, DFN-6, etc.
    (re.compile(r"DFN-?(\d+)", re.IGNORECASE), "dfn"),
    # TQFP-32, LQFP-48, QFP-100 (gull-wing — distinct from QFN)
    (re.compile(r"(?:T|L)?QFP-?(\d+)", re.IGNORECASE), "qfp"),
    # SSOP-20, TSSOP-16, MSOP-8, SOP-8
    (re.compile(r"(TSSOP|SSOP|MSOP|SOP)-?(\d+)", re.IGNORECASE), "sop_family"),
    # SOT-223
    (re.compile(r"SOT-?223", re.IGNORECASE), "sot223"),
    # SOT-89
    (re.compile(r"SOT-?89", re.IGNORECASE), "sot89"),
    # SOD-123, SOD-323, SOD-523, SOD-723, SOD-80 (2-pad diodes)
    (re.compile(r"SOD-?(\d+)", re.IGNORECASE), "sod"),
    # DO-214 SMD diodes: SMA/SMB/SMC (word-bounded — avoid matching e.g. 'SMART')
    (re.compile(r"^(SMA|SMB|SMC)\b|DO-?214(AC|AA|AB)", re.IGNORECASE), "do214"),
    # ESP8266 castellated modules
    (re.compile(r"ESP-?(?:12[EFS]?|07)\b", re.IGNORECASE), "esp12"),
    # MC-306 tuning-fork crystal
    (re.compile(r"MC-?306", re.IGNORECASE), "mc306"),
    # TO-263 / D2PAK (must precede TO-252/DPAK so 'D2PAK' wins over 'DPAK')
    (re.compile(r"TO-?263(?:-(\d+))?|D2PAK(?:-(\d+))?|DDPAK", re.IGNORECASE), "d2pak"),
    # TO-252 / DPAK
    (re.compile(r"TO-?252(?:-(\d+))?|DPAK(?:-(\d+))?", re.IGNORECASE), "dpak"),
    # ScrewTerminal_1x2_5mm, ScrewTerminal_1x3 (default 5mm pitch)
    (re.compile(r"ScrewTerminal_1x(\d+)(?:_(\d+(?:\.\d+)?)mm)?", re.IGNORECASE), "screw"),
    # Multiwatt-15 (L298 etc.)
    (re.compile(r"Multiwatt-?(\d+)", re.IGNORECASE), "multiwatt"),
    # 3mm SMD trimmer potentiometer
    (re.compile(r"trimmer[-_]?3(?:mm|x3)?", re.IGNORECASE), "trimmer3"),
    # MountingHole_3.2mm_M3, MountingHole_2.7mm_M2.5_Pad, NPTH variants
    (re.compile(r"MountingHole_?(\d+(?:\.\d+)?)mm", re.IGNORECASE), "mounting_hole"),
    # Radial electrolytic: electrolytic_10x12, CP_Radial_D10.0mm
    (re.compile(r"electrolytic[-_]?(\d+(?:\.\d+)?)x\d|CP_Radial_D(\d+(?:\.\d+)?)",
                re.IGNORECASE), "radial_cap"),
    # Bare body-size 2-pad SMD: "12x12mm" (power inductors etc.)
    (re.compile(r"^(\d+(?:\.\d+)?)x(\d+(?:\.\d+)?)(?:mm)?$", re.IGNORECASE),
     "smd_body_2pad"),
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

        if family == "qfp":
            n = int(m.group(1))
            count = pin_count if pin_count > 0 else n
            return make_qfp(count, body_mm=body_mm, pitch_mm=pitch_mm)

        if family == "sod":
            return make_sod(m.group(1))

        if family == "do214":
            variant = m.group(1)
            if not variant:  # matched the DO-214xx alternative
                do_map = {"AC": "SMA", "AA": "SMB", "AB": "SMC"}
                variant = do_map.get((m.group(2) or "").upper(), "SMA")
            return make_do214(variant)

        if family == "esp12":
            # E/F/S variants carry 6 extra bottom pads (22 total); honour an
            # explicit pin_count when the caller knows better.
            has_bottom = bool(re.search(r"12[EFS]", package, re.IGNORECASE))
            default = 22 if has_bottom else 16
            return make_esp12(pin_count if pin_count in (16, 22) else default)

        if family == "mc306":
            return make_mc306()

        if family in ("dpak", "d2pak"):
            n_str = m.group(1) or m.group(2)  # both alternatives carry a group
            leads = int(n_str) if n_str else 3
            return make_dpak(leads, d2=(family == "d2pak"))

        if family == "screw":
            positions = int(m.group(1))
            pitch = float(m.group(2)) if m.group(2) else 5.0
            return make_screw_terminal(positions, pitch)

        if family == "multiwatt":
            return make_multiwatt(int(m.group(1)))

        if family == "trimmer3":
            return make_trimmer_3mm()

        if family == "mounting_hole":
            return make_mounting_hole(float(m.group(1)))

        if family == "radial_cap":
            dia = float(m.group(1) or m.group(2))
            return make_radial_electrolytic(dia)

        if family == "smd_body_2pad":
            # Only safe for 2-terminal parts — a bare "NxM" body size says
            # nothing about pin layout for higher pin counts.
            if pin_count > 2:
                return None
            return make_smd_2pad_body(float(m.group(1)), float(m.group(2)))

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
