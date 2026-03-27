"""IC pinout parsing, electrical type inference, and requirements integration.

Parses pinout strings like "1:PC6/RESET 2:PD0 3:PD1 ... 7:VCC 8:GND"
into structured data for validation and auto-correction of LLM-generated netlists.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class PinInfo:
    """Parsed information about a single IC pin."""
    pin_number: int
    primary_name: str
    alt_names: list[str] = field(default_factory=list)
    inferred_electrical_type: str = "signal"

    @property
    def all_names(self) -> list[str]:
        """All names (primary + alternates) for matching."""
        return [self.primary_name] + self.alt_names


# ---------------------------------------------------------------------------
# Electrical type inference from pin function names
# ---------------------------------------------------------------------------

_POWER_IN_NAMES = frozenset({
    "VCC", "AVCC", "VDD", "VBAT", "V+", "VIN", "IN", "VCCA", "VCCB",
    "VDDIO", "VCC_IO", "DVCC", "DVDD",
})

_GROUND_NAMES = frozenset({
    "GND", "AGND", "VSS", "V-", "DGND", "PGND", "GNDA", "GNDD",
    "AVSS", "DVSS", "EPAD", "EP",
})

_POWER_OUT_NAMES = frozenset({
    "OUT", "VOUT", "OUTPUT", "FB",
})

_NO_CONNECT_NAMES = frozenset({
    "NC", "N/C", "DNC", "NO_CONNECT",
})


def infer_electrical_type(function_name: str) -> str:
    """Infer the electrical type of a pin from its function name.

    For multi-function pins (e.g. "PC6/RESET"), call this on each segment
    and use the most specific match. Pure power/ground pins get their type;
    mixed-function pins default to signal.
    """
    upper = function_name.strip().upper()
    if upper in _POWER_IN_NAMES:
        return "power_in"
    if upper in _GROUND_NAMES:
        return "ground"
    if upper in _POWER_OUT_NAMES:
        return "power_out"
    if upper in _NO_CONNECT_NAMES:
        return "no_connect"
    return "signal"


def _infer_pin_type(names: list[str]) -> str:
    """Infer type for a pin with potentially multiple function names.

    For single-function pins, returns the direct inference.
    For multi-function pins (e.g. PC6/RESET), if ALL names map to the same
    non-signal type (e.g. all power_in), use that; otherwise default to signal.
    The exception: if any name is power/ground and there's no signal name,
    prefer the power/ground type.
    """
    if len(names) == 1:
        return infer_electrical_type(names[0])

    types = [infer_electrical_type(n) for n in names]
    unique = set(types)

    # All agree on one type
    if len(unique) == 1:
        return types[0]

    # Mixed: if there's a power_in or ground among otherwise-signal names,
    # a pure power/ground pin with an alias is still power/ground
    # (e.g. "VCC/AVCC" → power_in). But "PC6/RESET" → signal.
    non_signal = unique - {"signal"}
    if non_signal and "signal" not in unique:
        # All non-signal, pick the most common
        return max(non_signal, key=lambda t: types.count(t))

    return "signal"


# ---------------------------------------------------------------------------
# Pinout string parser
# ---------------------------------------------------------------------------

def parse_pinout(pinout_str: str) -> dict[int, PinInfo]:
    """Parse a pinout string into a mapping of pin_number -> PinInfo.

    Expected format: "1:PC6/RESET 2:PD0 3:PD1 ... 7:VCC 8:GND"
    Tokens are whitespace-separated. Each token is pin_number:function.
    Functions may contain '/' for alternate names.

    Returns empty dict for empty/None input. Skips malformed tokens.
    """
    if not pinout_str or not pinout_str.strip():
        return {}

    pins: dict[int, PinInfo] = {}
    for token in pinout_str.strip().split():
        if ":" not in token:
            continue
        parts = token.split(":", 1)
        try:
            pin_num = int(parts[0])
        except (ValueError, IndexError):
            continue

        func = parts[1].strip()
        if not func:
            continue

        names = [n.strip() for n in func.split("/") if n.strip()]
        if not names:
            continue

        primary = names[0]
        alts = names[1:]
        etype = _infer_pin_type(names)

        pins[pin_num] = PinInfo(
            pin_number=pin_num,
            primary_name=primary,
            alt_names=alts,
            inferred_electrical_type=etype,
        )

    return pins


# ---------------------------------------------------------------------------
# Requirements integration
# ---------------------------------------------------------------------------

def build_pinout_from_requirements(
    requirements: dict,
) -> dict[str, dict[int, PinInfo]]:
    """Extract parsed pinouts from a requirements dict, keyed by component ref.

    Only includes components that have a specs.pinout field (typically ICs
    and voltage regulators). Skips passive components, LEDs, connectors, etc.

    Returns:
        {"U1": {1: PinInfo(...), 2: PinInfo(...), ...}, "U2": {...}}
    """
    result: dict[str, dict[int, PinInfo]] = {}

    for comp in requirements.get("components", []):
        ref = comp.get("ref", "")
        specs = comp.get("specs", {})
        pinout_str = specs.get("pinout", "")
        if not pinout_str:
            continue

        parsed = parse_pinout(pinout_str)
        if parsed:
            result[ref] = parsed

    return result
