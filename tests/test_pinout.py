"""Tests for IC pinout parsing, type inference, auto-correction, and DRC validation."""

import json
import os
import sys

import pytest

# Add validators/ to path
_validators_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "validators")
if _validators_dir not in sys.path:
    sys.path.insert(0, _validators_dir)

from pinout import (
    PinInfo,
    build_pinout_from_requirements,
    infer_electrical_type,
    parse_pinout,
)
from drc_checks import check_pinout_compliance, build_lookups
from validate_netlist import _fix_pinout_from_requirements


# ---------------------------------------------------------------------------
# Test fixtures
# ---------------------------------------------------------------------------

ATMEGA328P_PINOUT = (
    "1:PC6/RESET 2:PD0 3:PD1 4:PD2 5:PD3 6:PD4 7:VCC 8:GND "
    "9:PB6/XTAL1 10:PB7/XTAL2 11:PD5 12:PD6 13:PD7 14:PB0 "
    "15:PB1 16:PB2 17:PB3/MOSI 18:PB4/MISO 19:PB5/SCK 20:AVCC "
    "21:AREF 22:GND 23:PC0 24:PC1 25:PC2 26:PC3 27:PC4 28:PC5"
)

LM7805_PINOUT = "1:IN 2:GND 3:OUT"


def _make_requirements(components):
    return {"components": components}


def _make_netlist(elements):
    return {"version": "1.0", "elements": elements}


def _make_component(comp_id, designator, comp_type="ic", value="ATmega328P", package="DIP-28"):
    return {
        "element_type": "component",
        "component_id": comp_id,
        "designator": designator,
        "component_type": comp_type,
        "value": value,
        "package": package,
        "description": "test",
    }


def _make_port(port_id, comp_id, pin_number, name, etype="signal"):
    return {
        "element_type": "port",
        "port_id": port_id,
        "component_id": comp_id,
        "pin_number": pin_number,
        "name": name,
        "electrical_type": etype,
    }


# ---------------------------------------------------------------------------
# parse_pinout tests
# ---------------------------------------------------------------------------

class TestParsePinout:
    def test_atmega328p_full(self):
        pins = parse_pinout(ATMEGA328P_PINOUT)
        assert len(pins) == 28
        # Check specific pins
        assert pins[1].primary_name == "PC6"
        assert pins[1].alt_names == ["RESET"]
        assert pins[7].primary_name == "VCC"
        assert pins[7].alt_names == []
        assert pins[8].primary_name == "GND"
        assert pins[28].primary_name == "PC5"

    def test_atmega328p_types(self):
        pins = parse_pinout(ATMEGA328P_PINOUT)
        assert pins[7].inferred_electrical_type == "power_in"   # VCC
        assert pins[8].inferred_electrical_type == "ground"     # GND
        assert pins[20].inferred_electrical_type == "power_in"  # AVCC
        assert pins[22].inferred_electrical_type == "ground"    # GND
        assert pins[1].inferred_electrical_type == "signal"     # PC6/RESET (mixed)
        assert pins[2].inferred_electrical_type == "signal"     # PD0

    def test_lm7805(self):
        pins = parse_pinout(LM7805_PINOUT)
        assert len(pins) == 3
        assert pins[1].primary_name == "IN"
        assert pins[1].inferred_electrical_type == "power_in"
        assert pins[2].primary_name == "GND"
        assert pins[2].inferred_electrical_type == "ground"
        assert pins[3].primary_name == "OUT"
        assert pins[3].inferred_electrical_type == "power_out"

    def test_empty_input(self):
        assert parse_pinout("") == {}
        assert parse_pinout(None) == {}

    def test_malformed_tokens_skipped(self):
        pins = parse_pinout("1:VCC bad_token 3:GND")
        assert len(pins) == 2
        assert 1 in pins
        assert 3 in pins

    def test_all_names_property(self):
        pins = parse_pinout("1:PC6/RESET")
        assert pins[1].all_names == ["PC6", "RESET"]

    def test_from_arduino_fixture(self):
        """Parse pinout from the actual test fixture file."""
        fixture_path = os.path.join(os.path.dirname(__file__), "test_arduino_uno.json")
        with open(fixture_path) as f:
            data = json.load(f)
        # Find U2 (ATmega328P)
        u2 = next(c for c in data["components"] if c["ref"] == "U2")
        pins = parse_pinout(u2["specs"]["pinout"])
        assert len(pins) == 28


# ---------------------------------------------------------------------------
# infer_electrical_type tests
# ---------------------------------------------------------------------------

class TestInferElectricalType:
    @pytest.mark.parametrize("name,expected", [
        ("VCC", "power_in"),
        ("AVCC", "power_in"),
        ("VDD", "power_in"),
        ("VBAT", "power_in"),
        ("VIN", "power_in"),
        ("IN", "power_in"),
        ("GND", "ground"),
        ("AGND", "ground"),
        ("VSS", "ground"),
        ("OUT", "power_out"),
        ("VOUT", "power_out"),
        ("NC", "no_connect"),
        ("N/C", "no_connect"),
        ("PD0", "signal"),
        ("RESET", "signal"),
        ("SCK", "signal"),
        ("MOSI", "signal"),
        ("AREF", "signal"),
    ])
    def test_type_inference(self, name, expected):
        assert infer_electrical_type(name) == expected

    def test_case_insensitive(self):
        assert infer_electrical_type("vcc") == "power_in"
        assert infer_electrical_type("gnd") == "ground"
        assert infer_electrical_type("Vout") == "power_out"


# ---------------------------------------------------------------------------
# build_pinout_from_requirements tests
# ---------------------------------------------------------------------------

class TestBuildPinoutFromRequirements:
    def test_extracts_ic_pinouts(self):
        reqs = _make_requirements([
            {"ref": "U1", "type": "voltage_regulator", "specs": {"pinout": LM7805_PINOUT}},
            {"ref": "U2", "type": "ic", "specs": {"pinout": ATMEGA328P_PINOUT}},
            {"ref": "R1", "type": "resistor", "specs": {}},
        ])
        result = build_pinout_from_requirements(reqs)
        assert "U1" in result
        assert "U2" in result
        assert "R1" not in result
        assert len(result["U1"]) == 3
        assert len(result["U2"]) == 28

    def test_no_pinout_components_skipped(self):
        reqs = _make_requirements([
            {"ref": "R1", "type": "resistor", "specs": {}},
            {"ref": "C1", "type": "capacitor", "specs": {"voltage_rating": "50V"}},
        ])
        result = build_pinout_from_requirements(reqs)
        assert result == {}

    def test_empty_requirements(self):
        assert build_pinout_from_requirements({}) == {}
        assert build_pinout_from_requirements({"components": []}) == {}


# ---------------------------------------------------------------------------
# Auto-correction tests
# ---------------------------------------------------------------------------

class TestPinoutAutoCorrection:
    def test_fixes_wrong_pin_name(self):
        reqs = _make_requirements([
            {"ref": "U1", "type": "ic", "specs": {"pinout": "1:VCC 2:GND 3:OUT"}},
        ])
        netlist = _make_netlist([
            _make_component("comp_u1", "U1"),
            _make_port("port_u1_1", "comp_u1", 1, "WRONG_NAME", "power_in"),
            _make_port("port_u1_2", "comp_u1", 2, "GND", "ground"),
            _make_port("port_u1_3", "comp_u1", 3, "OUT", "power_out"),
        ])
        corrections = _fix_pinout_from_requirements(netlist, reqs)
        # Pin 1 name should be corrected
        port1 = next(e for e in netlist["elements"] if e.get("port_id") == "port_u1_1")
        assert port1["name"] == "VCC"
        assert any("pin 1" in c and "WRONG_NAME" in c for c in corrections)

    def test_fixes_wrong_electrical_type(self):
        reqs = _make_requirements([
            {"ref": "U1", "type": "ic", "specs": {"pinout": "1:VCC 2:GND"}},
        ])
        netlist = _make_netlist([
            _make_component("comp_u1", "U1"),
            _make_port("port_u1_1", "comp_u1", 1, "VCC", "signal"),  # wrong type
            _make_port("port_u1_2", "comp_u1", 2, "GND", "ground"),
        ])
        corrections = _fix_pinout_from_requirements(netlist, reqs)
        port1 = next(e for e in netlist["elements"] if e.get("port_id") == "port_u1_1")
        assert port1["electrical_type"] == "power_in"
        assert any("type 'signal' -> 'power_in'" in c for c in corrections)

    def test_accepts_alt_name(self):
        """Port using an alternate name (e.g. 'RESET' for 'PC6/RESET') should not be corrected."""
        reqs = _make_requirements([
            {"ref": "U1", "type": "ic", "specs": {"pinout": "1:PC6/RESET"}},
        ])
        netlist = _make_netlist([
            _make_component("comp_u1", "U1"),
            _make_port("port_u1_1", "comp_u1", 1, "RESET", "signal"),
        ])
        corrections = _fix_pinout_from_requirements(netlist, reqs)
        port1 = next(e for e in netlist["elements"] if e.get("port_id") == "port_u1_1")
        # Name should NOT be changed — RESET is a valid alt name
        assert port1["name"] == "RESET"
        # No name correction (only possible type correction)
        assert not any("name" in c for c in corrections)

    def test_skips_non_ic_components(self):
        reqs = _make_requirements([
            {"ref": "R1", "type": "resistor", "specs": {}},
        ])
        netlist = _make_netlist([
            _make_component("comp_r1", "R1", comp_type="resistor", value="220ohm"),
            _make_port("port_r1_1", "comp_r1", 1, "1", "passive"),
        ])
        corrections = _fix_pinout_from_requirements(netlist, reqs)
        assert corrections == []

    def test_no_requirements(self):
        netlist = _make_netlist([
            _make_component("comp_u1", "U1"),
            _make_port("port_u1_1", "comp_u1", 1, "VCC", "power_in"),
        ])
        corrections = _fix_pinout_from_requirements(netlist, {})
        assert corrections == []


# ---------------------------------------------------------------------------
# DRC pinout compliance tests
# ---------------------------------------------------------------------------

class TestPinoutDRC:
    def test_catches_pin_out_of_range(self):
        reqs = _make_requirements([
            {"ref": "U1", "type": "ic", "specs": {"pinout": "1:VCC 2:GND 3:OUT"}},
        ])
        elements = [
            _make_component("comp_u1", "U1"),
            _make_port("port_u1_1", "comp_u1", 1, "VCC", "power_in"),
            _make_port("port_u1_2", "comp_u1", 2, "GND", "ground"),
            _make_port("port_u1_99", "comp_u1", 99, "BOGUS", "signal"),  # out of range
        ]
        components, ports, nets = build_lookups(elements)
        errors, warnings = check_pinout_compliance(components, ports, nets, reqs)
        assert any("pin_number 99" in e for e in errors)

    def test_catches_wrong_name_after_autocorrect(self):
        """If auto-correction already ran, names should match. But if they don't..."""
        reqs = _make_requirements([
            {"ref": "U1", "type": "ic", "specs": {"pinout": "1:VCC 2:GND"}},
        ])
        elements = [
            _make_component("comp_u1", "U1"),
            _make_port("port_u1_1", "comp_u1", 1, "WRONG", "power_in"),
            _make_port("port_u1_2", "comp_u1", 2, "GND", "ground"),
        ]
        components, ports, nets = build_lookups(elements)
        errors, warnings = check_pinout_compliance(components, ports, nets, reqs)
        assert any("WRONG" in e and "VCC" in e for e in errors)

    def test_warns_on_missing_pins(self):
        reqs = _make_requirements([
            {"ref": "U1", "type": "ic", "specs": {"pinout": "1:VCC 2:GND 3:OUT"}},
        ])
        elements = [
            _make_component("comp_u1", "U1"),
            _make_port("port_u1_1", "comp_u1", 1, "VCC", "power_in"),
            # pins 2 and 3 missing
        ]
        components, ports, nets = build_lookups(elements)
        errors, warnings = check_pinout_compliance(components, ports, nets, reqs)
        assert any("missing ports" in w for w in warnings)

    def test_no_errors_on_correct_netlist(self):
        reqs = _make_requirements([
            {"ref": "U1", "type": "ic", "specs": {"pinout": "1:VCC 2:GND 3:OUT"}},
        ])
        elements = [
            _make_component("comp_u1", "U1"),
            _make_port("port_u1_1", "comp_u1", 1, "VCC", "power_in"),
            _make_port("port_u1_2", "comp_u1", 2, "GND", "ground"),
            _make_port("port_u1_3", "comp_u1", 3, "OUT", "power_out"),
        ]
        components, ports, nets = build_lookups(elements)
        errors, warnings = check_pinout_compliance(components, ports, nets, reqs)
        assert errors == []

    def test_no_requirements_no_errors(self):
        elements = [
            _make_component("comp_u1", "U1"),
            _make_port("port_u1_1", "comp_u1", 1, "VCC", "power_in"),
        ]
        components, ports, nets = build_lookups(elements)
        errors, warnings = check_pinout_compliance(components, ports, nets, None)
        assert errors == []
        assert warnings == []

    def test_nc_pins_not_warned(self):
        """NC pins missing from netlist should not trigger warnings."""
        reqs = _make_requirements([
            {"ref": "U1", "type": "ic", "specs": {"pinout": "1:VCC 2:NC 3:OUT"}},
        ])
        elements = [
            _make_component("comp_u1", "U1"),
            _make_port("port_u1_1", "comp_u1", 1, "VCC", "power_in"),
            # pin 2 (NC) intentionally missing
            _make_port("port_u1_3", "comp_u1", 3, "OUT", "power_out"),
        ]
        components, ports, nets = build_lookups(elements)
        errors, warnings = check_pinout_compliance(components, ports, nets, reqs)
        assert errors == []
        # No warning about missing NC pin
        assert not any("pin" in w and "2" in w for w in warnings)
