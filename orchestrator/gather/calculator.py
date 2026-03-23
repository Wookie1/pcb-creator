"""Engineering calculations for PCB design.

Uses shared constants from validators/engineering_constants.py.
"""

import sys
from pathlib import Path

# Add project root to path so we can import from validators/
_project_root = str(Path(__file__).parent.parent.parent)
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

from validators.engineering_constants import (
    LED_IF_DEFAULT,
    LED_VF_DEFAULTS,
    PACKAGE_POWER,
    format_resistance,
    parse_current,
    parse_voltage,
)


def led_resistor(v_supply: float, v_forward: float, i_forward: float) -> dict:
    """Calculate LED current-limiting resistor.

    Returns dict with formula, value (ohms), power (watts), formatted strings.
    """
    r = (v_supply - v_forward) / i_forward
    p = i_forward**2 * r

    return {
        "resistance_ohms": r,
        "power_watts": p,
        "formula": f"({v_supply}V - {v_forward}V) / {i_forward * 1000:g}mA",
        "value": format_resistance(r),
        "power": f"{p * 1000:g}mW",
    }


def check_package_power(package: str, power_watts: float) -> bool:
    """Check if package can handle the power with 2x safety margin."""
    rated = PACKAGE_POWER.get(package)
    if rated is None:
        return True  # Unknown package, assume OK
    return rated >= 2 * power_watts


def calculate_requirements(requirements: dict) -> dict:
    """Add engineering calculations to a requirements dict. Returns updated copy."""
    result = dict(requirements)
    calculations = {}

    power = requirements.get("power", {})
    v_supply = None
    if voltage_str := power.get("voltage"):
        try:
            v_supply = parse_voltage(voltage_str)
        except ValueError:
            pass

    package = requirements.get("packages", "").split()[0] if requirements.get("packages") else "0805"

    components = requirements.get("components", [])
    led_index = 0

    for comp in components:
        if comp["type"] == "led" and v_supply is not None:
            specs = comp.get("specs", {})
            vf = parse_voltage(specs["vf"]) if "vf" in specs else LED_VF_DEFAULTS.get(
                specs.get("color", "red"), 2.0
            )
            i_forward = parse_current(specs["if"]) if "if" in specs else LED_IF_DEFAULT

            for i in range(comp.get("quantity", 1)):
                led_index += 1
                calc = led_resistor(v_supply, vf, i_forward)
                calc["package_ok"] = check_package_power(package, calc["power_watts"])
                name = f"R{led_index}"
                calculations[name] = {
                    "formula": calc["formula"],
                    "value": calc["value"],
                    "power": calc["power"],
                    "package_ok": calc["package_ok"],
                }

    if calculations:
        result["calculations"] = calculations

    return result
