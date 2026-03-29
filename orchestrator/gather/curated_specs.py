"""Curated lookup tables for common component specs and footprint dimensions.

These tables eliminate LLM calls for well-known components.  All data comes
from datasheets and IPC standards — not from LLM inference.
"""

from __future__ import annotations


# ---------------------------------------------------------------------------
# Component specs — keyed by (type, value) or just value (case-insensitive)
# ---------------------------------------------------------------------------

# LED specs by colour (typical 3mm/5mm or SMD)
_LED_SPECS: dict[str, dict] = {
    "red":    {"vf": "2.0V", "if": "20mA", "color": "red"},
    "green":  {"vf": "2.2V", "if": "20mA", "color": "green"},
    "blue":   {"vf": "3.2V", "if": "20mA", "color": "blue"},
    "white":  {"vf": "3.2V", "if": "20mA", "color": "white"},
    "yellow": {"vf": "2.1V", "if": "20mA", "color": "yellow"},
    "orange": {"vf": "2.1V", "if": "20mA", "color": "orange"},
    "ir":     {"vf": "1.2V", "if": "20mA", "color": "infrared"},
    "uv":     {"vf": "3.4V", "if": "20mA", "color": "UV"},
}

# IC specs — keyed by part number (case-insensitive normalised)
_IC_SPECS: dict[str, dict] = {
    "ATMEGA328P": {
        "pin_count": 28,
        "pinout": "1:PC6/RESET 2:PD0 3:PD1 4:PD2 5:PD3 6:PD4 7:VCC 8:GND 9:PB6/XTAL1 10:PB7/XTAL2 11:PD5 12:PD6 13:PD7 14:PB0 15:PB1 16:PB2 17:PB3/MOSI 18:PB4/MISO 19:PB5/SCK 20:AVCC 21:AREF 22:GND 23:PC0/A0 24:PC1/A1 25:PC2/A2 26:PC3/A3 27:PC4/SDA 28:PC5/SCL",
        "vcc_min": "1.8V",
        "vcc_max": "5.5V",
        "package": "DIP-28",
    },
    "ATMEGA328P-AU": {
        "pin_count": 32,
        "pinout": "1:PD3 2:PD4 3:GND 4:VCC 5:GND 6:VCC 7:PB6/XTAL1 8:PB7/XTAL2 9:PD5 10:PD6 11:PD7 12:PB0 13:PB1 14:PB2 15:PB3/MOSI 16:PB4/MISO 17:PB5/SCK 18:AVCC 19:ADC6 20:AREF 21:GND 22:ADC7 23:PC0/A0 24:PC1/A1 25:PC2/A2 26:PC3/A3 27:PC4/SDA 28:PC5/SCL 29:PC6/RESET 30:PD0 31:PD1 32:PD2",
        "vcc_min": "1.8V",
        "vcc_max": "5.5V",
        "package": "TQFP-32",
    },
    "NE555": {
        "pin_count": 8,
        "pinout": "1:GND 2:TRIGGER 3:OUTPUT 4:RESET 5:CONTROL_VOLTAGE 6:THRESHOLD 7:DISCHARGE 8:VCC",
        "vcc_min": "4.5V",
        "vcc_max": "16V",
        "package": "DIP-8",
    },
    "LM555": {
        "pin_count": 8,
        "pinout": "1:GND 2:TRIGGER 3:OUTPUT 4:RESET 5:CONTROL_VOLTAGE 6:THRESHOLD 7:DISCHARGE 8:VCC",
        "vcc_min": "4.5V",
        "vcc_max": "16V",
        "package": "DIP-8",
    },
    "LM7805": {
        "pin_count": 3,
        "pinout": "1:INPUT 2:GND 3:OUTPUT",
        "vcc_min": "7V",
        "vcc_max": "25V",
        "output_voltage": "5V",
        "max_current": "1.5A",
        "package": "TO-220",
    },
    "LM7812": {
        "pin_count": 3,
        "pinout": "1:INPUT 2:GND 3:OUTPUT",
        "vcc_min": "14V",
        "vcc_max": "27V",
        "output_voltage": "12V",
        "max_current": "1.5A",
        "package": "TO-220",
    },
    "LM7833": {
        "pin_count": 3,
        "pinout": "1:INPUT 2:GND 3:OUTPUT",
        "vcc_min": "5.3V",
        "vcc_max": "20V",
        "output_voltage": "3.3V",
        "max_current": "1.5A",
        "package": "TO-220",
    },
    "LM317": {
        "pin_count": 3,
        "pinout": "1:ADJUST 2:OUTPUT 3:INPUT",
        "vcc_min": "1.25V",
        "vcc_max": "37V",
        "output_voltage": "1.25-37V (adjustable)",
        "max_current": "1.5A",
        "package": "TO-220",
    },
    "LM1117-3.3": {
        "pin_count": 3,
        "pinout": "1:GND/ADJ 2:OUTPUT 3:INPUT",
        "vcc_min": "4.75V",
        "vcc_max": "15V",
        "output_voltage": "3.3V",
        "max_current": "800mA",
        "package": "SOT-223",
    },
    "AMS1117-3.3": {
        "pin_count": 3,
        "pinout": "1:GND/ADJ 2:OUTPUT 3:INPUT",
        "vcc_min": "4.75V",
        "vcc_max": "15V",
        "output_voltage": "3.3V",
        "max_current": "1A",
        "package": "SOT-223",
    },
    "MCP2515": {
        "pin_count": 18,
        "pinout": "1:TXCAN 2:RXCAN 3:CLKOUT 4:TX0RTS 5:TX1RTS 6:TX2RTS 7:OSC2 8:OSC1 9:VSS 10:RX0BF 11:RX1BF 12:INT 13:SCK 14:SI 15:SO 16:CS 17:RESET 18:VDD",
        "vcc_min": "2.7V",
        "vcc_max": "5.5V",
        "package": "DIP-18",
    },
    "ESP32": {
        "pin_count": 38,
        "vcc_min": "3.0V",
        "vcc_max": "3.6V",
        "package": "QFN-48",
    },
    "STM32F103C8T6": {
        "pin_count": 48,
        "vcc_min": "2.0V",
        "vcc_max": "3.6V",
        "package": "LQFP-48",
    },
    "74HC595": {
        "pin_count": 16,
        "pinout": "1:QB 2:QC 3:QD 4:QE 5:QF 6:QG 7:QH 8:GND 9:QH' 10:SRCLR 11:SRCLK 12:RCLK 13:OE 14:SER 15:QA 16:VCC",
        "vcc_min": "2.0V",
        "vcc_max": "6.0V",
        "package": "DIP-16",
    },
    "CD4051": {
        "pin_count": 16,
        "pinout": "1:Y4 2:Y6 3:COMMON 4:Y7 5:Y5 6:INH 7:VEE 8:VSS 9:A 10:B 11:C 12:Y3 13:Y0 14:Y1 15:Y2 16:VDD",
        "vcc_min": "3.0V",
        "vcc_max": "18V",
        "package": "DIP-16",
    },
    "MAX232": {
        "pin_count": 16,
        "pinout": "1:C1+ 2:VS+ 3:C1- 4:C2+ 5:C2- 6:VS- 7:T2OUT 8:R2IN 9:R2OUT 10:T2IN 11:T1IN 12:R1OUT 13:R1IN 14:T1OUT 15:GND 16:VCC",
        "vcc_min": "4.5V",
        "vcc_max": "5.5V",
        "package": "DIP-16",
    },
    "L293D": {
        "pin_count": 16,
        "pinout": "1:ENABLE1,2 2:INPUT1 3:OUTPUT1 4:GND 5:GND 6:OUTPUT2 7:INPUT2 8:VS 9:ENABLE3,4 10:INPUT3 11:OUTPUT3 12:GND 13:GND 14:OUTPUT4 15:INPUT4 16:VSS",
        "vcc_min": "4.5V",
        "vcc_max": "36V",
        "max_current": "600mA",
        "package": "DIP-16",
    },
    "LM358": {
        "pin_count": 8,
        "pinout": "1:OUT_A 2:IN-_A 3:IN+_A 4:GND 5:IN+_B 6:IN-_B 7:OUT_B 8:VCC",
        "vcc_min": "3.0V",
        "vcc_max": "32V",
        "package": "DIP-8",
    },
    "LM393": {
        "pin_count": 8,
        "pinout": "1:OUT_A 2:IN-_A 3:IN+_A 4:GND 5:IN+_B 6:IN-_B 7:OUT_B 8:VCC",
        "vcc_min": "2.0V",
        "vcc_max": "36V",
        "package": "DIP-8",
    },
}

# Transistor specs
_TRANSISTOR_SPECS: dict[str, dict] = {
    "2N2222": {
        "type": "npn",
        "vce_max": "40V",
        "ic_max": "800mA",
        "hfe_min": "100",
        "package": "TO-92",
    },
    "2N2222A": {
        "type": "npn",
        "vce_max": "40V",
        "ic_max": "800mA",
        "hfe_min": "100",
        "package": "TO-92",
    },
    "2N3904": {
        "type": "npn",
        "vce_max": "40V",
        "ic_max": "200mA",
        "hfe_min": "100",
        "package": "TO-92",
    },
    "2N3906": {
        "type": "pnp",
        "vce_max": "40V",
        "ic_max": "200mA",
        "hfe_min": "100",
        "package": "TO-92",
    },
    "BC547": {
        "type": "npn",
        "vce_max": "45V",
        "ic_max": "100mA",
        "hfe_min": "110",
        "package": "TO-92",
    },
    "BC557": {
        "type": "pnp",
        "vce_max": "45V",
        "ic_max": "100mA",
        "hfe_min": "110",
        "package": "TO-92",
    },
    "TIP120": {
        "type": "npn",
        "vce_max": "60V",
        "ic_max": "5A",
        "hfe_min": "1000",
        "package": "TO-220",
    },
    "TIP122": {
        "type": "npn",
        "vce_max": "100V",
        "ic_max": "5A",
        "hfe_min": "1000",
        "package": "TO-220",
    },
    "IRF540N": {
        "type": "nmos",
        "vds_max": "100V",
        "id_max": "33A",
        "rds_on": "0.044Ω",
        "package": "TO-220",
    },
    "IRF9540N": {
        "type": "pmos",
        "vds_max": "-100V",
        "id_max": "-23A",
        "rds_on": "0.117Ω",
        "package": "TO-220",
    },
    "IRLZ44N": {
        "type": "nmos",
        "vds_max": "55V",
        "id_max": "47A",
        "rds_on": "0.022Ω",
        "package": "TO-220",
    },
    "2N7000": {
        "type": "nmos",
        "vds_max": "60V",
        "id_max": "200mA",
        "rds_on": "1.8Ω",
        "package": "TO-92",
    },
    "BS170": {
        "type": "nmos",
        "vds_max": "60V",
        "id_max": "500mA",
        "rds_on": "1.2Ω",
        "package": "TO-92",
    },
}

# Capacitor defaults by type (when no specific part is given)
_CAP_DEFAULTS: dict[str, dict] = {
    "ceramic":      {"voltage_rating": "50V", "type": "ceramic"},
    "electrolytic": {"voltage_rating": "25V", "type": "electrolytic"},
    "tantalum":     {"voltage_rating": "16V", "type": "tantalum"},
}

# ---------------------------------------------------------------------------
# Footprint dimensions for packages not in pad_geometry.py
# ---------------------------------------------------------------------------

CURATED_FOOTPRINT_DIMS: dict[str, dict] = {
    # Format: {footprint_width_mm, footprint_height_mm, courtyard_margin_mm}
    "SOT-223":   {"footprint_width_mm": 6.5,  "footprint_height_mm": 7.0,  "courtyard_margin_mm": 0.25},
    "SOT-89":    {"footprint_width_mm": 4.5,  "footprint_height_mm": 4.5,  "courtyard_margin_mm": 0.25},
    "SOT-23-5":  {"footprint_width_mm": 3.0,  "footprint_height_mm": 3.0,  "courtyard_margin_mm": 0.25},
    "SOT-23-6":  {"footprint_width_mm": 3.0,  "footprint_height_mm": 3.0,  "courtyard_margin_mm": 0.25},
    "SC-70":     {"footprint_width_mm": 2.2,  "footprint_height_mm": 2.4,  "courtyard_margin_mm": 0.25},
    "SSOP-8":    {"footprint_width_mm": 5.3,  "footprint_height_mm": 4.4,  "courtyard_margin_mm": 0.25},
    "SSOP-16":   {"footprint_width_mm": 5.3,  "footprint_height_mm": 6.2,  "courtyard_margin_mm": 0.25},
    "SSOP-20":   {"footprint_width_mm": 5.3,  "footprint_height_mm": 7.5,  "courtyard_margin_mm": 0.25},
    "SSOP-28":   {"footprint_width_mm": 5.3,  "footprint_height_mm": 10.2, "courtyard_margin_mm": 0.25},
    "TSSOP-8":   {"footprint_width_mm": 4.4,  "footprint_height_mm": 3.0,  "courtyard_margin_mm": 0.25},
    "TSSOP-14":  {"footprint_width_mm": 4.4,  "footprint_height_mm": 5.0,  "courtyard_margin_mm": 0.25},
    "TSSOP-16":  {"footprint_width_mm": 4.4,  "footprint_height_mm": 5.0,  "courtyard_margin_mm": 0.25},
    "TSSOP-20":  {"footprint_width_mm": 4.4,  "footprint_height_mm": 6.5,  "courtyard_margin_mm": 0.25},
    "MSOP-8":    {"footprint_width_mm": 3.0,  "footprint_height_mm": 3.0,  "courtyard_margin_mm": 0.25},
    "MSOP-10":   {"footprint_width_mm": 3.0,  "footprint_height_mm": 3.0,  "courtyard_margin_mm": 0.25},
    "QFN-16":    {"footprint_width_mm": 3.0,  "footprint_height_mm": 3.0,  "courtyard_margin_mm": 0.25},
    "QFN-20":    {"footprint_width_mm": 4.0,  "footprint_height_mm": 4.0,  "courtyard_margin_mm": 0.25},
    "QFN-24":    {"footprint_width_mm": 4.0,  "footprint_height_mm": 4.0,  "courtyard_margin_mm": 0.25},
    "QFN-32":    {"footprint_width_mm": 5.0,  "footprint_height_mm": 5.0,  "courtyard_margin_mm": 0.25},
    "QFN-48":    {"footprint_width_mm": 7.0,  "footprint_height_mm": 7.0,  "courtyard_margin_mm": 0.25},
    "QFN-64":    {"footprint_width_mm": 9.0,  "footprint_height_mm": 9.0,  "courtyard_margin_mm": 0.25},
    "DFN-8":     {"footprint_width_mm": 3.0,  "footprint_height_mm": 3.0,  "courtyard_margin_mm": 0.25},
    "LQFP-32":   {"footprint_width_mm": 9.0,  "footprint_height_mm": 9.0,  "courtyard_margin_mm": 0.25},
    "LQFP-48":   {"footprint_width_mm": 9.0,  "footprint_height_mm": 9.0,  "courtyard_margin_mm": 0.25},
    "LQFP-64":   {"footprint_width_mm": 12.0, "footprint_height_mm": 12.0, "courtyard_margin_mm": 0.25},
    "LQFP-100":  {"footprint_width_mm": 16.0, "footprint_height_mm": 16.0, "courtyard_margin_mm": 0.25},
    "LQFP-144":  {"footprint_width_mm": 22.0, "footprint_height_mm": 22.0, "courtyard_margin_mm": 0.25},
    "SOP-8":     {"footprint_width_mm": 6.0,  "footprint_height_mm": 5.0,  "courtyard_margin_mm": 0.25},
    "SOP-16":    {"footprint_width_mm": 6.0,  "footprint_height_mm": 10.3, "courtyard_margin_mm": 0.25},
    "USB-A":     {"footprint_width_mm": 12.0, "footprint_height_mm": 14.0, "courtyard_margin_mm": 0.5},
    "USB-B":     {"footprint_width_mm": 12.0, "footprint_height_mm": 16.0, "courtyard_margin_mm": 0.5},
    "USB-C-16P": {"footprint_width_mm": 8.94, "footprint_height_mm": 7.35, "courtyard_margin_mm": 0.5},
    "Micro-USB":  {"footprint_width_mm": 7.5,  "footprint_height_mm": 5.5,  "courtyard_margin_mm": 0.5},
    "Mini-USB":   {"footprint_width_mm": 7.7,  "footprint_height_mm": 9.3,  "courtyard_margin_mm": 0.5},
}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def lookup_specs(component_type: str, value: str, package: str = "") -> dict | None:
    """Look up specs for a component from the curated tables.

    Returns a dict of specs if found, or None.
    """
    val_upper = value.strip().upper()
    ctype = component_type.strip().lower()

    # LEDs — match by colour
    if ctype == "led":
        for colour, specs in _LED_SPECS.items():
            if colour.upper() in val_upper or colour.upper() in package.upper():
                return dict(specs)
        return None

    # ICs and voltage regulators — match by part number
    if ctype in ("ic", "voltage_regulator"):
        # Try exact match, then strip suffixes
        if val_upper in _IC_SPECS:
            return dict(_IC_SPECS[val_upper])
        # Try without common suffixes (-AU, -PU, -SN, etc.)
        base = val_upper.split("-")[0] if "-" in val_upper else val_upper
        if base in _IC_SPECS:
            return dict(_IC_SPECS[base])
        # Try partial match (e.g., "LM7805CT" → "LM7805")
        for key, specs in _IC_SPECS.items():
            if val_upper.startswith(key) or key.startswith(val_upper):
                return dict(specs)
        return None

    # Transistors
    if ctype in ("transistor_npn", "transistor_pnp", "transistor_nmos", "transistor_pmos"):
        if val_upper in _TRANSISTOR_SPECS:
            return dict(_TRANSISTOR_SPECS[val_upper])
        # Partial match
        for key, specs in _TRANSISTOR_SPECS.items():
            if val_upper.startswith(key) or key.startswith(val_upper):
                return dict(specs)
        return None

    # Capacitors — default specs by type
    if ctype == "capacitor":
        for cap_type, specs in _CAP_DEFAULTS.items():
            if cap_type.upper() in val_upper or cap_type.upper() in package.upper():
                return dict(specs)
        # Default: ceramic
        return dict(_CAP_DEFAULTS["ceramic"])

    return None


def lookup_footprint_dims(package: str) -> dict | None:
    """Look up footprint bounding-box dimensions for a package.

    Returns dict with footprint_width_mm, footprint_height_mm,
    courtyard_margin_mm — or None if not in the table.
    """
    key = package.strip()
    # Try exact (case-sensitive first, then upper)
    if key in CURATED_FOOTPRINT_DIMS:
        return dict(CURATED_FOOTPRINT_DIMS[key])

    key_upper = key.upper()
    for k, v in CURATED_FOOTPRINT_DIMS.items():
        if k.upper() == key_upper:
            return dict(v)

    return None
