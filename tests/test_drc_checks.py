"""Unit tests for DRC checks."""

import sys
from pathlib import Path

# Add validators/ to path
sys.path.insert(0, str(Path(__file__).parent.parent / "validators"))

from drc_checks import (
    build_lookups,
    check_capacitor_voltage_rating,
    check_component_value_sanity,
    check_decoupling_capacitors,
    check_duplicate_nets,
    check_net_class_vs_pin_types,
    check_pin_type_conflicts,
    check_power_budget,
    check_resistor_power,
    check_single_pin_nets,
    run_all_drc_checks,
)
from engineering_constants import parse_capacitance, parse_resistance


# ---------------------------------------------------------------------------
# Helpers to build minimal netlist elements
# ---------------------------------------------------------------------------

def _comp(comp_id, designator, comp_type, value="", package="0805", props=None):
    e = {
        "element_type": "component",
        "component_id": comp_id,
        "designator": designator,
        "component_type": comp_type,
        "value": value,
        "package": package,
    }
    if props:
        e["properties"] = props
    return e


def _port(port_id, comp_id, pin_number, name, etype="passive"):
    return {
        "element_type": "port",
        "port_id": port_id,
        "component_id": comp_id,
        "pin_number": pin_number,
        "name": name,
        "electrical_type": etype,
    }


def _net(net_id, name, port_ids, net_class="signal"):
    return {
        "element_type": "net",
        "net_id": net_id,
        "name": name,
        "connected_port_ids": port_ids,
        "net_class": net_class,
    }


# ---------------------------------------------------------------------------
# Test: parse_resistance / parse_capacitance
# ---------------------------------------------------------------------------

def test_parse_resistance():
    assert parse_resistance("220ohm") == 220.0
    assert parse_resistance("4.7kohm") == 4700.0
    assert parse_resistance("10Mohm") == 10_000_000.0
    assert parse_resistance("1.5ohm") == 1.5


def test_parse_capacitance():
    assert abs(parse_capacitance("100nF") - 100e-9) < 1e-15
    assert abs(parse_capacitance("10uF") - 10e-6) < 1e-12
    assert parse_capacitance("1pF") == 1e-12
    assert abs(parse_capacitance("4.7uF") - 4.7e-6) < 1e-10


# ---------------------------------------------------------------------------
# Test 1: Single-pin nets
# ---------------------------------------------------------------------------

def test_single_pin_nets_duplicate_port():
    """Duplicate port_id in a net should be an error."""
    elements = [
        _comp("comp_r1", "R1", "resistor", "100ohm"),
        _port("port_r1_1", "comp_r1", 1, "1"),
        _port("port_r1_2", "comp_r1", 2, "2"),
        _net("net_bad", "BAD", ["port_r1_1", "port_r1_1"]),  # duplicate
    ]
    c, p, n = build_lookups(elements)
    errors, warnings = check_single_pin_nets(c, p, n)
    assert any("duplicate" in e.lower() for e in errors)


def test_single_pin_nets_same_component():
    """Net connecting two pins of the same component should warn."""
    elements = [
        _comp("comp_r1", "R1", "resistor", "100ohm"),
        _port("port_r1_1", "comp_r1", 1, "1"),
        _port("port_r1_2", "comp_r1", 2, "2"),
        _net("net_self", "SELF_LOOP", ["port_r1_1", "port_r1_2"]),
    ]
    c, p, n = build_lookups(elements)
    errors, warnings = check_single_pin_nets(c, p, n)
    assert len(errors) == 0
    assert any("same component" in w.lower() or "all ports belong" in w.lower() for w in warnings)


# ---------------------------------------------------------------------------
# Test 2: Duplicate nets
# ---------------------------------------------------------------------------

def test_duplicate_nets():
    """Two nets with identical port sets should be an error."""
    elements = [
        _comp("comp_r1", "R1", "resistor", "100ohm"),
        _port("port_r1_1", "comp_r1", 1, "1"),
        _comp("comp_r2", "R2", "resistor", "100ohm"),
        _port("port_r2_1", "comp_r2", 1, "1"),
        _net("net_a", "NET_A", ["port_r1_1", "port_r2_1"]),
        _net("net_b", "NET_B", ["port_r1_1", "port_r2_1"]),  # same ports
    ]
    c, p, n = build_lookups(elements)
    errors, _ = check_duplicate_nets(c, p, n)
    assert any("redundant" in e.lower() for e in errors)


def test_no_duplicate_nets():
    """Different port sets should not trigger."""
    elements = [
        _comp("comp_r1", "R1", "resistor", "100ohm"),
        _port("port_r1_1", "comp_r1", 1, "1"),
        _port("port_r1_2", "comp_r1", 2, "2"),
        _comp("comp_r2", "R2", "resistor", "100ohm"),
        _port("port_r2_1", "comp_r2", 1, "1"),
        _net("net_a", "NET_A", ["port_r1_1", "port_r2_1"]),
        _net("net_b", "NET_B", ["port_r1_2", "port_r2_1"]),
    ]
    c, p, n = build_lookups(elements)
    errors, _ = check_duplicate_nets(c, p, n)
    assert len(errors) == 0


# ---------------------------------------------------------------------------
# Test 3: Net class vs pin types
# ---------------------------------------------------------------------------

def test_ground_net_with_power_out():
    """Ground net with a power_out pin should error."""
    elements = [
        _comp("comp_u1", "U1", "voltage_regulator", "LM7805"),
        _port("port_u1_out", "comp_u1", 1, "OUT", "power_out"),
        _comp("comp_j1", "J1", "connector", "2-pin"),
        _port("port_j1_1", "comp_j1", 1, "1", "ground"),
        _net("net_gnd", "GND", ["port_u1_out", "port_j1_1"], "ground"),
    ]
    c, p, n = build_lookups(elements)
    errors, _ = check_net_class_vs_pin_types(c, p, n)
    assert any("power_out" in e for e in errors)


def test_ground_net_no_ground_pin():
    """Ground net where no pin is typed 'ground' should warn."""
    elements = [
        _comp("comp_r1", "R1", "resistor", "100ohm"),
        _port("port_r1_1", "comp_r1", 1, "1", "passive"),
        _comp("comp_r2", "R2", "resistor", "100ohm"),
        _port("port_r2_1", "comp_r2", 1, "1", "passive"),
        _net("net_gnd", "GND", ["port_r1_1", "port_r2_1"], "ground"),
    ]
    c, p, n = build_lookups(elements)
    _, warnings = check_net_class_vs_pin_types(c, p, n)
    assert any("ground" in w.lower() and "no pin" in w.lower() for w in warnings)


# ---------------------------------------------------------------------------
# Test 4: Pin type conflicts
# ---------------------------------------------------------------------------

def test_multiple_power_out_on_net():
    """Two power_out pins on the same net should error."""
    elements = [
        _comp("comp_u1", "U1", "voltage_regulator", "LM7805"),
        _port("port_u1_out", "comp_u1", 1, "OUT", "power_out"),
        _comp("comp_u2", "U2", "voltage_regulator", "LM7805"),
        _port("port_u2_out", "comp_u2", 1, "OUT", "power_out"),
        _net("net_vcc", "VCC", ["port_u1_out", "port_u2_out"], "power"),
    ]
    c, p, n = build_lookups(elements)
    errors, _ = check_pin_type_conflicts(c, p, n)
    assert any("power_out" in e and "short" in e.lower() for e in errors)


# ---------------------------------------------------------------------------
# Test 5: Component value sanity
# ---------------------------------------------------------------------------

def test_extreme_resistor_value():
    """Extremely low resistor should warn."""
    elements = [
        _comp("comp_r1", "R1", "resistor", "0.1ohm"),
    ]
    c, p, n = build_lookups(elements)
    _, warnings = check_component_value_sanity(c, p, n)
    assert any("extremely low" in w.lower() for w in warnings)


def test_extreme_capacitor_value():
    """Extremely large capacitor should warn."""
    elements = [
        _comp("comp_c1", "C1", "capacitor", "100F"),
    ]
    c, p, n = build_lookups(elements)
    _, warnings = check_component_value_sanity(c, p, n)
    assert any("extremely large" in w.lower() for w in warnings)


def test_normal_values_no_warnings():
    """Normal values should not warn."""
    elements = [
        _comp("comp_r1", "R1", "resistor", "150ohm"),
        _comp("comp_c1", "C1", "capacitor", "100nF"),
    ]
    c, p, n = build_lookups(elements)
    _, warnings = check_component_value_sanity(c, p, n)
    assert len(warnings) == 0


# ---------------------------------------------------------------------------
# Test 6: Missing decoupling capacitors
# ---------------------------------------------------------------------------

def test_ic_missing_decoupling():
    """IC with VCC pin but no decoupling cap should warn."""
    elements = [
        _comp("comp_u1", "U1", "ic", "ATmega328"),
        _port("port_u1_vcc", "comp_u1", 1, "VCC", "power_in"),
        _port("port_u1_gnd", "comp_u1", 2, "GND", "ground"),
        _comp("comp_j1", "J1", "connector", "2-pin"),
        _port("port_j1_1", "comp_j1", 1, "1", "power_in"),
        _net("net_vcc", "VCC", ["port_u1_vcc", "port_j1_1"], "power"),
    ]
    c, p, n = build_lookups(elements)
    _, warnings = check_decoupling_capacitors(c, p, n)
    assert any("decoupling" in w.lower() for w in warnings)


def test_ic_with_decoupling():
    """IC with a 100nF cap on VCC should not warn."""
    elements = [
        _comp("comp_u1", "U1", "ic", "ATmega328"),
        _port("port_u1_vcc", "comp_u1", 1, "VCC", "power_in"),
        _comp("comp_c1", "C1", "capacitor", "100nF"),
        _port("port_c1_1", "comp_c1", 1, "1", "passive"),
        _port("port_c1_2", "comp_c1", 2, "2", "passive"),
        _comp("comp_j1", "J1", "connector", "2-pin"),
        _port("port_j1_1", "comp_j1", 1, "1", "power_in"),
        _net("net_vcc", "VCC", ["port_u1_vcc", "port_c1_1", "port_j1_1"], "power"),
    ]
    c, p, n = build_lookups(elements)
    _, warnings = check_decoupling_capacitors(c, p, n)
    assert not any("decoupling" in w.lower() for w in warnings)


# ---------------------------------------------------------------------------
# Test 7: Resistor power rating
# ---------------------------------------------------------------------------

def test_resistor_power_exceeds_rating():
    """A resistor dissipating more than package rating should error."""
    # 10ohm resistor, LED at 20mA → P = 0.02² × 10 = 4mW — fine for 0805
    # But a 10ohm on a 5V power net: P = 25/10 = 2.5W — way over 0805 125mW
    elements = [
        _comp("comp_r1", "R1", "resistor", "10ohm", "0805"),
        _port("port_r1_1", "comp_r1", 1, "1", "passive"),
        _port("port_r1_2", "comp_r1", 2, "2", "passive"),
        _comp("comp_j1", "J1", "connector", "2-pin"),
        _port("port_j1_1", "comp_j1", 1, "1", "power_in"),
        _net("net_vcc", "VCC", ["port_r1_1", "port_j1_1"], "power"),
    ]
    c, p, n = build_lookups(elements)
    errors, _ = check_resistor_power(c, p, n, v_supply=5.0)
    assert any("power dissipation" in e.lower() for e in errors)


def test_resistor_power_led_series_uses_vf():
    """LED series resistor should compute I=(Vsupply-Vf)/R, not use LED max If."""
    # 1kohm resistor in series with green LED (Vf=3.2V) on 5V supply
    # I = (5 - 3.2) / 1000 = 1.8mA → P = 0.0018² × 1000 = 3.24mW — well under 125mW
    elements = [
        _comp("comp_r1", "R1", "resistor", "1kohm", "0805"),
        _port("port_r1_1", "comp_r1", 1, "1", "passive"),
        _port("port_r1_2", "comp_r1", 2, "2", "passive"),
        _comp("comp_d1", "D1", "led", "green", "0805",
               props={"vf": "3.2V", "if": "5mA"}),
        _port("port_d1_a", "comp_d1", 1, "anode", "passive"),
        _port("port_d1_k", "comp_d1", 2, "cathode", "passive"),
        # R1 pin 2 connects to D1 anode via signal net
        _net("net_r1_d1", "R1_TO_D1", ["port_r1_2", "port_d1_a"], "signal"),
        # R1 pin 1 on VCC (power net — should be skipped for LED detection)
        _comp("comp_j1", "J1", "connector", "2-pin"),
        _port("port_j1_1", "comp_j1", 1, "1", "power_in"),
        _net("net_vcc", "VCC", ["port_r1_1", "port_j1_1"], "power"),
    ]
    c, p, n = build_lookups(elements)
    errors, warnings = check_resistor_power(c, p, n, v_supply=5.0)
    # Should NOT flag an error — 3.24mW is well under 125mW
    assert len(errors) == 0, f"Unexpected errors: {errors}"


def test_resistor_power_no_supply():
    """Without V_supply, power checks should not run."""
    elements = [
        _comp("comp_r1", "R1", "resistor", "10ohm", "0805"),
    ]
    c, p, n = build_lookups(elements)
    errors, warnings = check_resistor_power(c, p, n, v_supply=None)
    assert len(errors) == 0 and len(warnings) == 0


# ---------------------------------------------------------------------------
# Test 8: Capacitor voltage derating
# ---------------------------------------------------------------------------

def test_cap_below_derating():
    """Capacitor voltage rating below derating should error."""
    elements = [
        _comp("comp_c1", "C1", "capacitor", "100nF", "0805",
               props={"voltage_rating": "6V"}),
        _port("port_c1_1", "comp_c1", 1, "1", "passive"),
        _net("net_vcc", "VCC", ["port_c1_1"], "power"),  # will fail min 2 ports in real schema
    ]
    c, p, n = build_lookups(elements)
    errors, _ = check_capacitor_voltage_rating(c, p, n, v_supply=5.0)
    # Ceramic derating: 1.5 × 5V = 7.5V > 6V → error
    assert any("derating" in e.lower() for e in errors)


def test_cap_above_derating():
    """Capacitor with adequate voltage rating should not error."""
    elements = [
        _comp("comp_c1", "C1", "capacitor", "100nF", "0805",
               props={"voltage_rating": "16V"}),
    ]
    c, p, n = build_lookups(elements)
    errors, _ = check_capacitor_voltage_rating(c, p, n, v_supply=5.0)
    assert len(errors) == 0


# ---------------------------------------------------------------------------
# Test 9: Power budget
# ---------------------------------------------------------------------------

def test_power_budget_with_leds():
    """Power budget should sum LED current draws."""
    elements = [
        _comp("comp_d1", "D1", "led", "red"),
        _comp("comp_d2", "D2", "led", "blue"),
    ]
    c, p, n = build_lookups(elements)
    _, warnings = check_power_budget(c, p, n, v_supply=5.0)
    assert any("power budget" in w.lower() for w in warnings)
    assert any("40mA" in w for w in warnings)  # 20mA × 2


def test_power_budget_no_supply():
    """Power budget should not run without V_supply."""
    elements = [
        _comp("comp_d1", "D1", "led", "red"),
    ]
    c, p, n = build_lookups(elements)
    _, warnings = check_power_budget(c, p, n, v_supply=None)
    assert len(warnings) == 0


# ---------------------------------------------------------------------------
# Test: run_all_drc_checks integration
# ---------------------------------------------------------------------------

def test_run_all_clean_netlist():
    """A simple valid netlist should pass all checks."""
    elements = [
        _comp("comp_r1", "R1", "resistor", "150ohm", "0805"),
        _port("port_r1_1", "comp_r1", 1, "1", "passive"),
        _port("port_r1_2", "comp_r1", 2, "2", "passive"),
        _comp("comp_d1", "D1", "led", "red", "0805"),
        _port("port_d1_a", "comp_d1", 1, "anode", "passive"),
        _port("port_d1_k", "comp_d1", 2, "cathode", "passive"),
        _comp("comp_j1", "J1", "connector", "2-pin", "PinHeader_1x2"),
        _port("port_j1_1", "comp_j1", 1, "1", "power_in"),
        _port("port_j1_2", "comp_j1", 2, "2", "ground"),
        _net("net_vcc", "VCC", ["port_j1_1", "port_r1_1"], "power"),
        _net("net_r1_d1", "R1_TO_D1", ["port_r1_2", "port_d1_a"], "signal"),
        _net("net_gnd", "GND", ["port_d1_k", "port_j1_2"], "ground"),
    ]
    requirements = {"power": {"voltage": "5V"}}
    errors, warnings = run_all_drc_checks(elements, requirements)
    # Should have no errors (warnings about power budget are fine)
    assert len(errors) == 0


if __name__ == "__main__":
    import pytest
    pytest.main([__file__, "-v"])
