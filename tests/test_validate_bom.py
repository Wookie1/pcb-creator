"""Unit tests for BOM validator."""

import json
import os
import sys
import tempfile

import pytest

# Add validators/ to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "validators"))

from validate_bom import validate_cross_reference, validate_specs_completeness


def _write_temp(data: dict) -> str:
    """Write a dict to a temporary JSON file and return the path."""
    f = tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False)
    json.dump(data, f)
    f.close()
    return f.name


def _minimal_netlist(*components):
    """Build a minimal valid netlist with the given components."""
    elements = []
    for comp in components:
        elements.append({
            "element_type": "component",
            "component_id": f"comp_{comp['designator'].lower()}",
            "designator": comp["designator"],
            "component_type": comp["component_type"],
            "value": comp["value"],
            "package": comp["package"],
            "description": comp.get("description", "test component"),
        })
        # Add a port
        elements.append({
            "element_type": "port",
            "port_id": f"port_{comp['designator'].lower()}_1",
            "component_id": f"comp_{comp['designator'].lower()}",
            "pin_number": 1,
            "name": "1",
            "electrical_type": "passive",
        })
    # Add a dummy net connecting first two ports if we have at least 2 components
    if len(components) >= 2:
        elements.append({
            "element_type": "net",
            "net_id": "net_test",
            "name": "TEST",
            "connected_port_ids": [
                f"port_{components[0]['designator'].lower()}_1",
                f"port_{components[1]['designator'].lower()}_1",
            ],
            "net_class": "signal",
        })
    return {
        "version": "1.0",
        "project_name": "test",
        "elements": elements,
    }


def _minimal_bom(*items):
    """Build a minimal valid BOM with the given items."""
    return {
        "version": "1.0",
        "project_name": "test",
        "source_netlist": "test_netlist.json",
        "bom": list(items),
    }


def _bom_item(designator, component_type, value, package, **extra_specs):
    """Create a single BOM item."""
    item = {
        "designator": designator,
        "component_type": component_type,
        "value": value,
        "package": package,
        "quantity": 1,
        "specs": extra_specs,
        "description": f"{value} {package} {component_type}",
    }
    return item


# --- Cross-reference tests ---

class TestCrossReference:
    def test_matching_bom_and_netlist(self):
        """BOM and netlist with matching components should pass."""
        netlist = _minimal_netlist(
            {"designator": "R1", "component_type": "resistor", "value": "220ohm", "package": "0805"},
            {"designator": "D1", "component_type": "led", "value": "red", "package": "0805"},
        )
        bom = _minimal_bom(
            _bom_item("R1", "resistor", "220ohm", "0805"),
            _bom_item("D1", "led", "red", "0805"),
        )
        errors, warnings = validate_cross_reference(bom, netlist)
        assert len(errors) == 0, f"Unexpected errors: {errors}"

    def test_missing_bom_entry(self):
        """Netlist component not in BOM should be an error."""
        netlist = _minimal_netlist(
            {"designator": "R1", "component_type": "resistor", "value": "220ohm", "package": "0805"},
            {"designator": "D1", "component_type": "led", "value": "red", "package": "0805"},
        )
        bom = _minimal_bom(
            _bom_item("R1", "resistor", "220ohm", "0805"),
            # D1 missing
        )
        errors, warnings = validate_cross_reference(bom, netlist)
        assert any("D1" in e and "missing" in e for e in errors)

    def test_phantom_bom_entry(self):
        """BOM entry without matching netlist component should be an error."""
        netlist = _minimal_netlist(
            {"designator": "R1", "component_type": "resistor", "value": "220ohm", "package": "0805"},
        )
        bom = _minimal_bom(
            _bom_item("R1", "resistor", "220ohm", "0805"),
            _bom_item("R2", "resistor", "1kohm", "0805"),  # phantom
        )
        errors, warnings = validate_cross_reference(bom, netlist)
        assert any("R2" in e and "no matching" in e for e in errors)

    def test_type_mismatch(self):
        """BOM with wrong component_type should be an error."""
        netlist = _minimal_netlist(
            {"designator": "R1", "component_type": "resistor", "value": "220ohm", "package": "0805"},
        )
        bom = _minimal_bom(
            _bom_item("R1", "capacitor", "220ohm", "0805"),  # wrong type
        )
        errors, warnings = validate_cross_reference(bom, netlist)
        assert any("component_type mismatch" in e for e in errors)

    def test_package_mismatch(self):
        """BOM with wrong package should be an error."""
        netlist = _minimal_netlist(
            {"designator": "R1", "component_type": "resistor", "value": "220ohm", "package": "0805"},
        )
        bom = _minimal_bom(
            _bom_item("R1", "resistor", "220ohm", "0603"),  # wrong package
        )
        errors, warnings = validate_cross_reference(bom, netlist)
        assert any("package mismatch" in e for e in errors)

    def test_value_mismatch(self):
        """BOM with wrong value should be an error."""
        netlist = _minimal_netlist(
            {"designator": "R1", "component_type": "resistor", "value": "220ohm", "package": "0805"},
        )
        bom = _minimal_bom(
            _bom_item("R1", "resistor", "1kohm", "0805"),  # wrong value
        )
        errors, warnings = validate_cross_reference(bom, netlist)
        assert any("value mismatch" in e for e in errors)

    def test_case_insensitive_value(self):
        """Value comparison should be case-insensitive."""
        netlist = _minimal_netlist(
            {"designator": "R1", "component_type": "resistor", "value": "220ohm", "package": "0805"},
        )
        bom = _minimal_bom(
            _bom_item("R1", "resistor", "220Ohm", "0805"),
        )
        errors, warnings = validate_cross_reference(bom, netlist)
        assert len(errors) == 0

    def test_duplicate_bom_entry(self):
        """Duplicate designators in BOM should be an error."""
        netlist = _minimal_netlist(
            {"designator": "R1", "component_type": "resistor", "value": "220ohm", "package": "0805"},
        )
        bom = _minimal_bom(
            _bom_item("R1", "resistor", "220ohm", "0805"),
            _bom_item("R1", "resistor", "220ohm", "0805"),  # duplicate
        )
        errors, warnings = validate_cross_reference(bom, netlist)
        assert any("Duplicate" in e for e in errors)


# --- Specs completeness tests ---

class TestSpecsCompleteness:
    def test_resistor_missing_specs(self):
        """Resistor without tolerance or power_rating should warn."""
        bom = _minimal_bom(
            _bom_item("R1", "resistor", "220ohm", "0805"),  # no specs
        )
        warnings = validate_specs_completeness(bom)
        assert any("tolerance" in w for w in warnings)
        assert any("power_rating" in w for w in warnings)

    def test_resistor_with_specs(self):
        """Resistor with required specs should not warn."""
        bom = _minimal_bom(
            _bom_item("R1", "resistor", "220ohm", "0805",
                       tolerance="1%", power_rating="125mW"),
        )
        warnings = validate_specs_completeness(bom)
        assert len(warnings) == 0

    def test_capacitor_missing_voltage_rating(self):
        """Capacitor without voltage_rating should warn."""
        bom = _minimal_bom(
            _bom_item("C1", "capacitor", "100nF", "0805"),
        )
        warnings = validate_specs_completeness(bom)
        assert any("voltage_rating" in w for w in warnings)

    def test_led_missing_forward_voltage(self):
        """LED without forward_voltage should warn."""
        bom = _minimal_bom(
            _bom_item("D1", "led", "red", "0805"),
        )
        warnings = validate_specs_completeness(bom)
        assert any("forward_voltage" in w for w in warnings)

    def test_connector_no_required_specs(self):
        """Connector has no required specs, should not warn."""
        bom = _minimal_bom(
            _bom_item("J1", "connector", "2-pin header", "PinHeader_1x2"),
        )
        warnings = validate_specs_completeness(bom)
        assert len(warnings) == 0


# --- Full validation integration test ---

class TestFullValidation:
    def test_valid_bom_with_netlist(self):
        """Full validation of a valid BOM against a matching netlist."""
        from validate_bom import validate_bom

        netlist = _minimal_netlist(
            {"designator": "R1", "component_type": "resistor", "value": "220ohm", "package": "0805"},
            {"designator": "D1", "component_type": "led", "value": "red", "package": "0805"},
        )
        bom = _minimal_bom(
            _bom_item("R1", "resistor", "220ohm", "0805",
                       tolerance="1%", power_rating="125mW"),
            _bom_item("D1", "led", "red", "0805",
                       forward_voltage="2.0V"),
        )

        netlist_path = _write_temp(netlist)
        bom_path = _write_temp(bom)

        try:
            result = validate_bom(bom_path, netlist_path)
            assert result["valid"] is True, f"Unexpected errors: {result['errors']}"
            assert len(result["errors"]) == 0
        finally:
            os.unlink(netlist_path)
            os.unlink(bom_path)

    def test_invalid_bom_missing_component(self):
        """Full validation catches missing BOM entry."""
        from validate_bom import validate_bom

        netlist = _minimal_netlist(
            {"designator": "R1", "component_type": "resistor", "value": "220ohm", "package": "0805"},
            {"designator": "D1", "component_type": "led", "value": "red", "package": "0805"},
        )
        bom = _minimal_bom(
            _bom_item("R1", "resistor", "220ohm", "0805",
                       tolerance="1%", power_rating="125mW"),
            # D1 missing
        )

        netlist_path = _write_temp(netlist)
        bom_path = _write_temp(bom)

        try:
            result = validate_bom(bom_path, netlist_path)
            assert result["valid"] is False
            assert any("D1" in e for e in result["errors"])
        finally:
            os.unlink(netlist_path)
            os.unlink(bom_path)
