"""Unit tests for pure gather modules: calculator, curated_specs, schema."""

import copy
import json
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(_ROOT))

from orchestrator.gather.calculator import (
    calculate_requirements,
    check_package_power,
    led_resistor,
)
from orchestrator.gather.curated_specs import (
    lookup_footprint_dims,
    lookup_specs,
)
from orchestrator.gather.schema import (
    auto_fix_duplicate_pins,
    coerce_requirements_types,
    validate_requirements,
    _validate_pin_uniqueness,
)

_TC01 = _ROOT / "tests" / "test_cases" / "tc01_2l_minimal.json"


def _valid_req() -> dict:
    return json.loads(_TC01.read_text())


# ===========================================================================
# calculator.py
# ===========================================================================

def test_led_resistor_formula():
    # (5 - 2.0) / 0.020 = 150 ohm; P = 0.020^2 * 150 = 0.06 W
    r = led_resistor(5.0, 2.0, 0.020)
    assert r["resistance_ohms"] == pytest.approx(150.0)
    assert r["power_watts"] == pytest.approx(0.06)
    assert r["value"] == "150ohm"
    assert r["power"] == "60mW"
    assert r["formula"] == "(5.0V - 2.0V) / 20mA"


def test_check_package_power_known_pass():
    # 0805 rated 0.125W; 2x margin over 0.06W = 0.12W <= 0.125 -> OK
    assert check_package_power("0805", 0.06) is True


def test_check_package_power_known_fail():
    # 0402 rated 0.063W; 2x over 0.06 = 0.12 > 0.063 -> not OK
    assert check_package_power("0402", 0.06) is False


def test_check_package_power_unknown_assumes_ok():
    assert check_package_power("9999", 999.0) is True


def test_calculate_requirements_leds():
    req = _valid_req()
    result = calculate_requirements(req)
    calcs = result["calculations"]
    # tc01 has 3 single-quantity red LEDs at 5V -> R1,R2,R3 each 150ohm/60mW
    assert set(calcs) == {"R1", "R2", "R3"}
    for name in ("R1", "R2", "R3"):
        assert calcs[name]["value"] == "150ohm"
        assert calcs[name]["power"] == "60mW"
        assert calcs[name]["package_ok"] is True
    # original input untouched (returns a copy)
    assert "calculations" not in req


def test_calculate_requirements_specs_and_quantity():
    # LED with explicit vf/if specs and quantity>1, plus packages field.
    req = {
        "power": {"voltage": "5V"},
        "packages": "0603 SMD",
        "components": [
            {
                "type": "led",
                "quantity": 2,
                "specs": {"vf": "2.0V", "if": "20mA"},
            }
        ],
    }
    result = calculate_requirements(req)
    calcs = result["calculations"]
    assert set(calcs) == {"R1", "R2"}
    assert calcs["R1"]["value"] == "150ohm"
    # 0603 rated 0.100W; 2x*0.06=0.12 > 0.1 -> not OK
    assert calcs["R1"]["package_ok"] is False


def test_calculate_requirements_color_default_vf():
    # Color absent from LED_VF_DEFAULTS -> falls back to 2.0V default.
    req = {
        "power": {"voltage": "5V"},
        "components": [{"type": "led", "specs": {"color": "purple"}}],
    }
    calcs = calculate_requirements(req)["calculations"]
    # (5 - 2.0)/0.020 = 150ohm
    assert calcs["R1"]["value"] == "150ohm"


def test_calculate_requirements_green_default_vf():
    # green default vf 3.2 -> (5-3.2)/0.02 = 90ohm
    req = {
        "power": {"voltage": "5V"},
        "components": [{"type": "led", "specs": {"color": "green"}}],
    }
    calcs = calculate_requirements(req)["calculations"]
    assert calcs["R1"]["value"] == "90ohm"


def test_calculate_requirements_unparseable_voltage_no_calcs():
    req = {
        "power": {"voltage": "five volts"},
        "components": [{"type": "led", "value": "red"}],
    }
    result = calculate_requirements(req)
    assert "calculations" not in result


def test_calculate_requirements_no_voltage_no_calcs():
    req = {"power": {}, "components": [{"type": "led"}]}
    assert "calculations" not in calculate_requirements(req)


def test_calculate_requirements_no_leds():
    req = {
        "power": {"voltage": "5V"},
        "components": [{"type": "resistor", "value": "1k"}],
    }
    assert "calculations" not in calculate_requirements(req)


# ===========================================================================
# curated_specs.py
# ===========================================================================

def test_lookup_specs_led_by_value():
    assert lookup_specs("led", "red")["vf"] == "2.0V"


def test_lookup_specs_led_by_package():
    s = lookup_specs("led", "D1", package="blue smd")
    assert s["color"] == "blue"


def test_lookup_specs_led_unknown_returns_none():
    assert lookup_specs("led", "magenta") is None


def test_lookup_specs_ic_exact():
    assert lookup_specs("ic", "NE555")["pin_count"] == 8


def test_lookup_specs_ic_case_insensitive():
    assert lookup_specs("ic", "ne555")["pin_count"] == 8


def test_lookup_specs_ic_suffix_strip():
    # ATMEGA328P-AU is itself a key (exact match wins, pin_count 32)
    assert lookup_specs("ic", "ATMEGA328P-AU")["pin_count"] == 32


def test_lookup_specs_ic_base_after_suffix():
    # Unknown suffix -> strip "-XX" to base ATMEGA328P (pin_count 28)
    assert lookup_specs("ic", "ATMEGA328P-ZZ")["pin_count"] == 28


def test_lookup_specs_ic_partial_prefix():
    # "LM7805CT" startswith "LM7805"
    assert lookup_specs("voltage_regulator", "LM7805CT")["output_voltage"] == "5V"


def test_lookup_specs_ic_unknown_returns_none():
    assert lookup_specs("ic", "TOTALLY_FAKE_PART") is None


def test_lookup_specs_transistor_exact():
    assert lookup_specs("transistor_npn", "2N2222")["type"] == "npn"


def test_lookup_specs_transistor_partial():
    # "2N2222A" exact also present; use a prefix-only hit instead.
    assert lookup_specs("transistor_nmos", "IRF540")["type"] == "nmos"


def test_lookup_specs_transistor_unknown_returns_none():
    assert lookup_specs("transistor_pnp", "ZZZ999") is None


def test_lookup_specs_capacitor_typed():
    assert lookup_specs("capacitor", "10uF electrolytic")["voltage_rating"] == "25V"


def test_lookup_specs_capacitor_default_ceramic():
    assert lookup_specs("capacitor", "100nF")["type"] == "ceramic"


def test_lookup_specs_unknown_type_returns_none():
    assert lookup_specs("widget", "foo") is None


def test_lookup_footprint_dims_exact():
    d = lookup_footprint_dims("SOT-223")
    assert d["footprint_width_mm"] == 6.5


def test_lookup_footprint_dims_case_insensitive():
    d = lookup_footprint_dims("qfn-48")
    assert d["footprint_width_mm"] == 7.0


def test_lookup_footprint_dims_unknown_returns_none():
    assert lookup_footprint_dims("NONEXISTENT") is None


# ===========================================================================
# schema.py — validate_requirements
# ===========================================================================

def test_validate_requirements_valid_tc01():
    assert validate_requirements(_valid_req()) == []


def test_validate_requirements_missing_required_field():
    req = _valid_req()
    del req["description"]
    errors = validate_requirements(req)
    assert any("description" in e and "required" in e for e in errors)


def test_validate_requirements_bad_project_name_pattern():
    req = _valid_req()
    req["project_name"] = "Bad Name"
    errors = validate_requirements(req)
    assert any("project_name" in e for e in errors)


def test_validate_requirements_bad_type():
    req = _valid_req()
    req["components"] = "not a list"
    errors = validate_requirements(req)
    assert any("components" in e for e in errors)


def test_validate_requirements_net_fewer_than_2_pins():
    req = _valid_req()
    req["connections"][0]["pins"] = ["J1.1"]
    errors = validate_requirements(req)
    assert any("pins" in e for e in errors)


def test_validate_requirements_additional_property():
    req = _valid_req()
    req["bogus_top_level"] = 1
    errors = validate_requirements(req)
    assert any("Additional properties" in e or "additional" in e.lower() for e in errors)


def test_validate_requirements_duplicate_pin_across_nets():
    req = _valid_req()
    # J1.1 is already in VCC; also add it to GND.
    req["connections"][1]["pins"].append("J1.1")
    errors = validate_requirements(req)
    assert any("J1.1" in e and "multiple nets" in e for e in errors)


# ===========================================================================
# schema.py — _validate_pin_uniqueness (direct)
# ===========================================================================

def test_pin_uniqueness_clean():
    data = {"connections": [{"net_name": "A", "pins": ["X.1", "X.2"]}]}
    assert _validate_pin_uniqueness(data) == []


def test_pin_uniqueness_duplicate():
    data = {
        "connections": [
            {"net_name": "A", "pins": ["X.1", "X.2"]},
            {"net_name": "B", "pins": ["X.1", "Y.1"]},
        ]
    }
    errs = _validate_pin_uniqueness(data)
    assert len(errs) == 1
    assert "X.1" in errs[0] and "'A'" in errs[0] and "'B'" in errs[0]


def test_pin_uniqueness_missing_net_name_default():
    data = {"connections": [{"pins": ["X.1", "X.1"]}]}
    errs = _validate_pin_uniqueness(data)
    assert "'?'" in errs[0]


# ===========================================================================
# schema.py — coerce_requirements_types
# ===========================================================================

def test_coerce_board_string_numbers():
    data = {"board": {"width_mm": "50", "height_mm": "35.5",
                      "corner_radius_mm": "1", "copper_weight_oz": "1.0",
                      "layers": "2"}}
    out = coerce_requirements_types(data)
    assert out["board"]["width_mm"] == 50.0
    assert out["board"]["height_mm"] == 35.5
    assert out["board"]["layers"] == 2
    assert isinstance(out["board"]["layers"], int)


def test_coerce_board_bad_strings_left_alone():
    data = {"board": {"width_mm": "wide", "layers": "many"}}
    out = coerce_requirements_types(data)
    assert out["board"]["width_mm"] == "wide"
    assert out["board"]["layers"] == "many"


def test_coerce_strips_none_from_sections():
    data = {
        "board": {"width_mm": 50.0, "corner_radius_mm": None},
        "manufacturing": {"manufacturer": "jlcpcb_standard", "clearance_min_mm": None},
        "power": {"voltage": "5V", "source": None},
    }
    out = coerce_requirements_types(data)
    assert "corner_radius_mm" not in out["board"]
    assert "clearance_min_mm" not in out["manufacturing"]
    assert "source" not in out["power"]


def test_coerce_strips_none_from_components_and_specs():
    data = {
        "components": [
            {"ref": "R1", "type": "resistor", "value": None,
             "specs": {"vf": "2V", "if": None}},
            "not_a_dict",
        ]
    }
    out = coerce_requirements_types(data)
    comp = out["components"][0]
    assert "value" not in comp
    assert "if" not in comp["specs"]
    assert comp["specs"]["vf"] == "2V"


def test_coerce_manufacturing_string_numbers():
    data = {"manufacturing": {"trace_width_min_mm": "0.15",
                              "clearance_min_mm": "bad"}}
    out = coerce_requirements_types(data)
    assert out["manufacturing"]["trace_width_min_mm"] == 0.15
    assert out["manufacturing"]["clearance_min_mm"] == "bad"


def test_coerce_placement_hints():
    data = {"placement_hints": [
        {"ref": "U1", "x_mm": "25", "y_mm": "17.5",
         "rotation_deg": "90", "edge": None},
        "skip_me",
    ]}
    out = coerce_requirements_types(data)
    hint = out["placement_hints"][0]
    assert hint["x_mm"] == 25.0
    assert hint["rotation_deg"] == 90
    assert isinstance(hint["rotation_deg"], int)
    assert "edge" not in hint


def test_coerce_placement_hint_bad_numbers_left_alone():
    data = {"placement_hints": [{"ref": "U1", "x_mm": "left", "rotation_deg": "spin"}]}
    out = coerce_requirements_types(data)
    assert out["placement_hints"][0]["x_mm"] == "left"
    assert out["placement_hints"][0]["rotation_deg"] == "spin"


def test_coerce_attachment_used_by_steps():
    data = {"attachments": [
        {"filename": "f", "used_by_steps": ["3", 4], "purpose": None},
        "skip_me",
    ]}
    out = coerce_requirements_types(data)
    att = out["attachments"][0]
    assert att["used_by_steps"] == [3, 4]
    assert "purpose" not in att


# ===========================================================================
# schema.py — auto_fix_duplicate_pins
# ===========================================================================

def test_auto_fix_removes_pin_from_power_keeps_signal():
    data = {"connections": [
        {"net_name": "VCC", "net_class": "power", "pins": ["A.1", "B.1", "D.1"]},
        {"net_name": "SIG", "net_class": "signal", "pins": ["A.1", "C.1"]},
    ]}
    fixed, warnings = auto_fix_duplicate_pins(data)
    vcc = next(c for c in fixed["connections"] if c["net_name"] == "VCC")
    sig = next(c for c in fixed["connections"] if c["net_name"] == "SIG")
    assert "A.1" not in vcc["pins"]
    assert "A.1" in sig["pins"]
    assert any("removed 'A.1' from 'VCC'" in w for w in warnings)


def test_auto_fix_multiple_signal_nets_keeps_first():
    data = {"connections": [
        {"net_name": "S1", "net_class": "signal", "pins": ["A.1", "B.1"]},
        {"net_name": "S2", "net_class": "signal", "pins": ["A.1", "C.1", "E.1"]},
    ]}
    fixed, warnings = auto_fix_duplicate_pins(data)
    s1 = next(c for c in fixed["connections"] if c["net_name"] == "S1")
    s2 = next(c for c in fixed["connections"] if c["net_name"] == "S2")
    assert "A.1" in s1["pins"]
    assert "A.1" not in s2["pins"]
    assert any("duplicate signal assignment" in w for w in warnings)


def test_auto_fix_drops_connection_under_2_pins():
    # Removing A.1 from S2 leaves it with only C.1 -> dropped entirely.
    data = {"connections": [
        {"net_name": "S1", "net_class": "signal", "pins": ["A.1", "B.1"]},
        {"net_name": "S2", "net_class": "signal", "pins": ["A.1", "C.1"]},
    ]}
    fixed, _ = auto_fix_duplicate_pins(data)
    assert all(c["net_name"] != "S2" for c in fixed["connections"])


def test_auto_fix_no_duplicates_no_change():
    data = {"connections": [
        {"net_name": "A", "net_class": "signal", "pins": ["X.1", "X.2"]},
    ]}
    original = copy.deepcopy(data)
    fixed, warnings = auto_fix_duplicate_pins(data)
    assert warnings == []
    assert fixed["connections"] == original["connections"]
    # input not mutated (deepcopy inside)
    assert data == original


def test_auto_fix_default_net_class_is_signal():
    # No net_class given -> treated as signal; two such nets -> keep first.
    data = {"connections": [
        {"net_name": "A", "pins": ["P.1", "X.1"]},
        {"net_name": "B", "pins": ["P.1", "Y.1"]},
    ]}
    fixed, warnings = auto_fix_duplicate_pins(data)
    assert warnings  # a fix happened
