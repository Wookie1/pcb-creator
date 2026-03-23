"""Shared engineering constants and value-parsing utilities.

Single source of truth for numeric values used by both:
- validators/drc_checks.py (deterministic Python checks)
- orchestrator/gather/calculator.py (requirements enrichment)

The LLM-facing engineering_rules.md uses the same numbers as prose.
"""

import re

# ---------------------------------------------------------------------------
# Package power ratings (watts)
# ---------------------------------------------------------------------------
PACKAGE_POWER: dict[str, float] = {
    "0402": 0.063,
    "0603": 0.100,
    "0805": 0.125,
    "1206": 0.250,
    "1210": 0.500,
    "2010": 0.750,
    "2512": 1.000,
}

# ---------------------------------------------------------------------------
# LED forward voltage defaults (volts)
# ---------------------------------------------------------------------------
LED_VF_DEFAULTS: dict[str, float] = {
    "red": 2.0,
    "orange": 2.0,
    "yellow": 2.0,
    "green": 3.2,
    "blue": 3.2,
    "white": 3.2,
}

LED_IF_DEFAULT: float = 0.020  # 20 mA

# ---------------------------------------------------------------------------
# Derating factors
# ---------------------------------------------------------------------------
RESISTOR_POWER_DERATING: float = 2.0      # rated >= 2× calculated
CERAMIC_VOLTAGE_DERATING: float = 1.5     # V_rated >= 1.5× V_supply
ELECTROLYTIC_VOLTAGE_DERATING: float = 2.0  # V_rated >= 2× V_supply

# ---------------------------------------------------------------------------
# Sanity-check ranges
# ---------------------------------------------------------------------------
RESISTOR_MIN_OHM: float = 1.0
RESISTOR_MAX_OHM: float = 10_000_000.0  # 10 MΩ
CAPACITOR_MIN_F: float = 1e-12   # 1 pF
CAPACITOR_MAX_F: float = 0.01    # 10 mF

# ---------------------------------------------------------------------------
# Decoupling capacitor requirements
# ---------------------------------------------------------------------------
DECOUPLING_CAP_F: float = 100e-9  # 100 nF
DECOUPLING_CAP_TOLERANCE: float = 0.1  # ±10%

# ---------------------------------------------------------------------------
# Value parsing
# ---------------------------------------------------------------------------

_VOLTAGE_RE = re.compile(r"([\d.]+)\s*V", re.IGNORECASE)
_CURRENT_RE = re.compile(r"([\d.]+)\s*(m?A)", re.IGNORECASE)
_RESISTANCE_RE = re.compile(
    r"([\d.]+)\s*(M(?:ohm|Ω)?|k(?:ohm|Ω)?|ohm|Ω)", re.IGNORECASE
)
_CAPACITANCE_RE = re.compile(
    r"([\d.]+)\s*(m|u|µ|n|p)?F", re.IGNORECASE
)


def parse_voltage(s: str) -> float:
    """Parse a voltage string like '5V' or '3.3V' into float volts."""
    m = _VOLTAGE_RE.search(s)
    if not m:
        raise ValueError(f"Cannot parse voltage: {s}")
    return float(m.group(1))


def parse_current(s: str) -> float:
    """Parse a current string like '20mA' or '1A' into float amps."""
    m = _CURRENT_RE.search(s)
    if not m:
        raise ValueError(f"Cannot parse current: {s}")
    value = float(m.group(1))
    if m.group(2).lower().startswith("m"):
        value /= 1000
    return value


def parse_resistance(s: str) -> float:
    """Parse a resistance string like '220ohm', '4.7kohm', '10Mohm' into float ohms."""
    m = _RESISTANCE_RE.search(s)
    if not m:
        raise ValueError(f"Cannot parse resistance: {s}")
    value = float(m.group(1))
    unit = m.group(2).lower()
    if unit.startswith("m"):
        value *= 1_000_000
    elif unit.startswith("k"):
        value *= 1_000
    return value


def parse_capacitance(s: str) -> float:
    """Parse a capacitance string like '100nF', '10uF', '1pF' into float farads."""
    m = _CAPACITANCE_RE.search(s)
    if not m:
        raise ValueError(f"Cannot parse capacitance: {s}")
    value = float(m.group(1))
    prefix = (m.group(2) or "").lower()
    multipliers = {
        "": 1.0,
        "m": 1e-3,
        "u": 1e-6,
        "µ": 1e-6,
        "n": 1e-9,
        "p": 1e-12,
    }
    return value * multipliers.get(prefix, 1.0)


# ---------------------------------------------------------------------------
# Routing parameters
# ---------------------------------------------------------------------------
TRACE_WIDTH_POWER_MM: float = 0.5
TRACE_WIDTH_GROUND_MM: float = 0.5
TRACE_WIDTH_SIGNAL_MM: float = 0.25
TRACE_WIDTH_SIGNAL_NARROW_MM: float = 0.15  # narrow trace for congested signal routing
TRACE_CLEARANCE_MM: float = 0.2
TRACE_CLEARANCE_NARROW_MM: float = 0.15  # narrow clearance for congested areas
VIA_DRILL_MM: float = 0.3
VIA_DIAMETER_MM: float = 0.6
ROUTING_GRID_MM: float = 0.25
COPPER_WEIGHT_DEFAULT_OZ: float = 0.5  # 0.5oz ≈ 17.5μm

# ---------------------------------------------------------------------------
# Copper fill parameters
# ---------------------------------------------------------------------------
FILL_CLEARANCE_MM: float = 0.25           # clearance between fill and foreign-net features
THERMAL_RELIEF_GAP_MM: float = 0.2        # annular gap around pads connected to fill net
THERMAL_RELIEF_SPOKE_WIDTH_MM: float = 0.25  # width of thermal relief spoke connections


# ---------------------------------------------------------------------------
# Manufacturer DFM profiles
# ---------------------------------------------------------------------------
# Each profile defines the minimum manufacturing capabilities.
# Values are minimums — the router uses whichever is larger: the DFM minimum
# or the electrical requirement (e.g., IPC-2221 trace width for current).

MANUFACTURER_DFM_PROFILES: dict[str, dict] = {
    "jlcpcb_standard": {
        "description": "JLCPCB standard process (1-2 layer, 1.6mm, HASL)",
        "trace_width_min_mm": 0.127,     # 5 mil
        "clearance_min_mm": 0.127,       # 5 mil
        "via_drill_min_mm": 0.3,         # 0.3mm drill
        "via_diameter_min_mm": 0.6,      # 0.6mm annular ring
        "min_annular_ring_mm": 0.13,     # 5 mil
        "board_edge_clearance_mm": 0.3,  # copper to edge
        "silkscreen_min_width_mm": 0.15,
        "silkscreen_min_height_mm": 0.8,
        "min_hole_to_hole_mm": 0.5,
        "min_copper_to_edge_mm": 0.2,
    },
    "jlcpcb_advanced": {
        "description": "JLCPCB advanced process (4+ layer, HDI)",
        "trace_width_min_mm": 0.09,      # 3.5 mil
        "clearance_min_mm": 0.09,        # 3.5 mil
        "via_drill_min_mm": 0.2,         # 0.2mm laser drill
        "via_diameter_min_mm": 0.45,
        "min_annular_ring_mm": 0.1,
        "board_edge_clearance_mm": 0.2,
        "silkscreen_min_width_mm": 0.1,
        "silkscreen_min_height_mm": 0.6,
        "min_hole_to_hole_mm": 0.4,
        "min_copper_to_edge_mm": 0.15,
    },
    "oshpark_2layer": {
        "description": "OSH Park 2-layer (ENIG, purple soldermask)",
        "trace_width_min_mm": 0.152,     # 6 mil
        "clearance_min_mm": 0.152,       # 6 mil
        "via_drill_min_mm": 0.254,       # 10 mil
        "via_diameter_min_mm": 0.508,    # 20 mil pad
        "min_annular_ring_mm": 0.127,    # 5 mil
        "board_edge_clearance_mm": 0.381,  # 15 mil
        "silkscreen_min_width_mm": 0.15,
        "silkscreen_min_height_mm": 0.8,
        "min_hole_to_hole_mm": 0.635,    # 25 mil
        "min_copper_to_edge_mm": 0.254,  # 10 mil
    },
    "pcbway_standard": {
        "description": "PCBWay standard process (1-2 layer)",
        "trace_width_min_mm": 0.127,     # 5 mil
        "clearance_min_mm": 0.127,       # 5 mil
        "via_drill_min_mm": 0.3,
        "via_diameter_min_mm": 0.6,
        "min_annular_ring_mm": 0.15,
        "board_edge_clearance_mm": 0.25,
        "silkscreen_min_width_mm": 0.15,
        "silkscreen_min_height_mm": 0.8,
        "min_hole_to_hole_mm": 0.5,
        "min_copper_to_edge_mm": 0.2,
    },
    "generic": {
        "description": "Conservative generic defaults (safe for most fabs)",
        "trace_width_min_mm": 0.2,
        "clearance_min_mm": 0.2,
        "via_drill_min_mm": 0.3,
        "via_diameter_min_mm": 0.6,
        "min_annular_ring_mm": 0.15,
        "board_edge_clearance_mm": 0.3,
        "silkscreen_min_width_mm": 0.15,
        "silkscreen_min_height_mm": 1.0,
        "min_hole_to_hole_mm": 0.5,
        "min_copper_to_edge_mm": 0.25,
    },
}


def get_dfm_profile(manufacturer: str) -> dict:
    """Look up a manufacturer DFM profile by name (case-insensitive, fuzzy).

    Tries exact match first, then partial match. Returns generic if not found.
    """
    key = manufacturer.lower().replace(" ", "_").replace("-", "_")

    # Exact match
    if key in MANUFACTURER_DFM_PROFILES:
        return MANUFACTURER_DFM_PROFILES[key]

    # Partial match
    for profile_key, profile in MANUFACTURER_DFM_PROFILES.items():
        if key in profile_key or profile_key in key:
            return profile

    return MANUFACTURER_DFM_PROFILES["generic"]


def format_resistance(ohms: float) -> str:
    """Format resistance value with units: 150ohm, 10kohm, 1Mohm."""
    if ohms >= 1_000_000:
        return f"{ohms / 1_000_000:g}Mohm"
    elif ohms >= 1_000:
        return f"{ohms / 1_000:g}kohm"
    else:
        return f"{ohms:g}ohm"
