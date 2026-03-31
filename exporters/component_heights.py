"""Component height lookup table for 3D model generation.

Maps package types to typical body heights in mm. Used by the parametric
model generator when no library STEP model is available.
"""

from __future__ import annotations

# Typical component body heights in mm (from datasheets / IPC standards)
PACKAGE_HEIGHTS_MM: dict[str, float] = {
    # SMD passives (2-terminal chip)
    "0201": 0.3,
    "0402": 0.5,
    "0603": 0.6,
    "0805": 0.8,
    "1206": 0.9,
    "1210": 1.0,
    "1812": 1.0,
    "2010": 1.0,
    "2512": 1.0,
    # Electrolytic / tantalum caps (taller)
    "CASE-A": 1.6,
    "CASE-B": 1.8,
    "CASE-C": 2.2,
    "CASE-D": 2.8,
    # Transistors / small ICs
    "SOT-23": 1.1,
    "SOT-223": 1.6,
    "SOT-89": 1.5,
    "SC-70": 0.8,
    # SOIC packages
    "SOIC-8": 1.5,
    "SOIC-14": 1.5,
    "SOIC-16": 1.5,
    "SOIC8": 1.5,
    "SOP-8": 1.5,
    # QFP
    "TQFP-32": 1.0,
    "TQFP-44": 1.0,
    "TQFP-48": 1.0,
    "TQFP-64": 1.0,
    "TQFP-100": 1.0,
    "LQFP-32": 1.2,
    "LQFP-48": 1.2,
    "LQFP-64": 1.2,
    # QFN / DFN
    "QFN-16": 0.8,
    "QFN-20": 0.8,
    "QFN-32": 0.8,
    "QFN-48": 0.8,
    "DFN-8": 0.8,
    # Through-hole DIP
    "DIP-8": 3.5,
    "DIP-14": 3.5,
    "DIP-16": 3.5,
    "DIP-20": 3.5,
    "DIP-28": 3.5,
    "DIP-40": 3.5,
    # Through-hole power
    "TO-220": 10.0,
    "TO-92": 4.5,
    "TO-252": 2.3,
    "TO-263": 2.3,
    # Crystal
    "HC49": 3.5,
    "HC49S": 3.5,
    # Connectors
    "USB-B": 11.0,
    "USB-C": 3.2,
    "USB-MICRO": 2.7,
    "USB-MINI": 3.9,
    "PJ-002A": 11.0,  # Barrel jack
    # Pin headers
    "PinHeader-1x1": 8.5,
    "PinHeader-1x2": 8.5,
    "PinHeader-1x3": 8.5,
    "PinHeader-1x4": 8.5,
    "PinHeader-1x5": 8.5,
    "PinHeader-1x6": 8.5,
    "PinHeader-1x8": 8.5,
    "PinHeader-1x10": 8.5,
    "PinHeader-2x3": 8.5,
    "PinHeader-2x4": 8.5,
    "PinHeader-2x5": 8.5,
    "PinHeader-2x7": 8.5,
    "PinHeader-2x10": 8.5,
    "PinHeader-2x20": 8.5,
    # Tactile switch
    "SW-6MM": 5.0,
    "SW-TACTILE": 5.0,
}

# Default heights by component type when package isn't in the table
_TYPE_DEFAULT_HEIGHTS_MM: dict[str, float] = {
    "resistor": 0.8,
    "capacitor": 0.8,
    "inductor": 1.2,
    "led": 1.0,
    "diode": 1.0,
    "transistor_npn": 1.5,
    "transistor_pnp": 1.5,
    "transistor_nmos": 1.5,
    "transistor_pmos": 1.5,
    "ic": 2.0,
    "voltage_regulator": 2.0,
    "connector": 8.0,
    "switch": 5.0,
    "crystal": 3.5,
    "fuse": 3.0,
    "relay": 15.0,
}


def get_component_height(package: str, component_type: str = "") -> float:
    """Get estimated component body height in mm.

    Looks up by exact package name first, then tries normalized variants,
    then falls back to component type default, then 1.5mm generic default.
    """
    # Exact match
    if package in PACKAGE_HEIGHTS_MM:
        return PACKAGE_HEIGHTS_MM[package]

    # Try uppercase
    pkg_upper = package.upper()
    for key, val in PACKAGE_HEIGHTS_MM.items():
        if key.upper() == pkg_upper:
            return val

    # Try prefix match (e.g. "DIP-28" matches any DIP-N)
    for prefix in ("DIP-", "TQFP-", "LQFP-", "QFN-", "SOIC-", "PinHeader-"):
        if pkg_upper.startswith(prefix.upper()):
            # Find any entry with this prefix
            for key, val in PACKAGE_HEIGHTS_MM.items():
                if key.upper().startswith(prefix.upper()):
                    return val

    # Fall back to component type
    if component_type and component_type in _TYPE_DEFAULT_HEIGHTS_MM:
        return _TYPE_DEFAULT_HEIGHTS_MM[component_type]

    return 1.5  # Generic default
