"""Net-class and pin electrical-type inference from names.

Shared by the KiCad netlist importer and the incremental circuit builder so
both flows classify nets identically.
"""

from __future__ import annotations

import re

_GROUND_NAMES: frozenset[str] = frozenset({
    "GND", "GROUND", "VSS", "AGND", "DGND", "PGND", "EGND", "EARTH",
})
_POWER_RE = re.compile(
    r"^(\+?\d+(?:V\d*|V)|VCC|VDD|VBAT|VBUS|VIN|VOUT|VPP|3V3|5V|3\.3V|PWR)",
    re.IGNORECASE,
)


def infer_net_class(net_name: str) -> str:
    """Classify a net as 'ground', 'power', or 'signal' from its name."""
    name = net_name.strip().upper().lstrip("/")
    if not name:
        return "signal"
    if name in _GROUND_NAMES or name.startswith("GND"):
        return "ground"
    if _POWER_RE.match(name):
        return "power"
    if re.match(r"^[+\-]?\d+V\d*$", name):
        return "power"
    if name.startswith(("VCC", "VDD", "VBAT", "VBUS")):
        return "power"
    return "signal"


def infer_electrical_type(net_class: str, component_type: str) -> str:
    """Infer a pin's electrical_type from its net class and component type."""
    if net_class == "ground":
        return "ground"
    if net_class == "power":
        # Connectors and regulators *supply* power; ICs and passives *receive* it.
        if component_type in ("connector", "voltage_regulator"):
            return "power_out"
        return "power_in"
    # Signal net: passives are passive, everything else is signal.
    if component_type in ("resistor", "capacitor", "inductor", "fuse"):
        return "passive"
    return "signal"
