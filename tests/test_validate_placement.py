"""Unit tests for placement validator."""

import json
import os
import sys
import tempfile

import pytest

# Add validators/ to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "validators"))

from validate_placement import (
    validate_board_boundary,
    validate_cross_reference,
    validate_overlap_and_clearance,
    validate_placement,
    validate_placement_rules,
    validate_schema,
)


def _minimal_netlist(*components):
    """Build a minimal valid netlist with the given components."""
    elements = []
    for comp in components:
        elements.append({
            "element_type": "component",
            "component_id": f"comp_{comp['designator'].lower()}",
            "designator": comp["designator"],
            "component_type": comp["component_type"],
            "value": comp.get("value", "test"),
            "package": comp["package"],
            "description": comp.get("description", "test component"),
        })
        elements.append({
            "element_type": "port",
            "port_id": f"port_{comp['designator'].lower()}_1",
            "component_id": f"comp_{comp['designator'].lower()}",
            "pin_number": 1,
            "name": "1",
            "electrical_type": "passive",
        })
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


def _minimal_placement(*items, board_w=50, board_h=30):
    """Build a minimal valid placement."""
    return {
        "version": "1.0",
        "project_name": "test",
        "source_netlist": "test_netlist.json",
        "source_bom": "test_bom.json",
        "board": {
            "width_mm": board_w,
            "height_mm": board_h,
            "outline_type": "rectangle",
            "origin": [0, 0],
        },
        "placements": list(items),
    }


def _place(designator, component_type, package, x, y, w=2.5, h=1.8, rot=0, layer="top", source="llm"):
    """Create a single placement item."""
    return {
        "designator": designator,
        "component_type": component_type,
        "package": package,
        "footprint_width_mm": w,
        "footprint_height_mm": h,
        "x_mm": x,
        "y_mm": y,
        "rotation_deg": rot,
        "layer": layer,
        "placement_source": source,
    }


def _write_temp(data: dict) -> str:
    f = tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False)
    json.dump(data, f)
    f.close()
    return f.name


# --- Schema validation tests ---

class TestSchema:
    def test_valid_placement(self):
        placement = _minimal_placement(
            _place("R1", "resistor", "0805", 10, 15),
        )
        errors = validate_schema(placement)
        assert len(errors) == 0

    def test_missing_required_field(self):
        placement = _minimal_placement(
            _place("R1", "resistor", "0805", 10, 15),
        )
        del placement["source_bom"]
        errors = validate_schema(placement)
        assert len(errors) > 0

    def test_invalid_rotation(self):
        placement = _minimal_placement(
            _place("R1", "resistor", "0805", 10, 15, rot=45),
        )
        errors = validate_schema(placement)
        assert any("rotation_deg" in e for e in errors)

    def test_invalid_layer(self):
        item = _place("R1", "resistor", "0805", 10, 15)
        item["layer"] = "inner"
        placement = _minimal_placement(item)
        errors = validate_schema(placement)
        assert any("layer" in e for e in errors)


# --- Cross-reference tests ---

class TestCrossReference:
    def test_matching_placement_and_netlist(self):
        netlist = _minimal_netlist(
            {"designator": "R1", "component_type": "resistor", "package": "0805"},
            {"designator": "D1", "component_type": "led", "package": "0805"},
        )
        placement = _minimal_placement(
            _place("R1", "resistor", "0805", 10, 15),
            _place("D1", "led", "0805", 20, 15),
        )
        errors, warnings = validate_cross_reference(placement, netlist)
        assert len(errors) == 0

    def test_missing_placement(self):
        netlist = _minimal_netlist(
            {"designator": "R1", "component_type": "resistor", "package": "0805"},
            {"designator": "D1", "component_type": "led", "package": "0805"},
        )
        placement = _minimal_placement(
            _place("R1", "resistor", "0805", 10, 15),
        )
        errors, warnings = validate_cross_reference(placement, netlist)
        assert any("D1" in e and "missing" in e for e in errors)

    def test_phantom_placement(self):
        netlist = _minimal_netlist(
            {"designator": "R1", "component_type": "resistor", "package": "0805"},
        )
        placement = _minimal_placement(
            _place("R1", "resistor", "0805", 10, 15),
            _place("R2", "resistor", "0805", 20, 15),
        )
        errors, warnings = validate_cross_reference(placement, netlist)
        assert any("R2" in e and "no matching" in e.lower() for e in errors)

    def test_type_mismatch(self):
        netlist = _minimal_netlist(
            {"designator": "R1", "component_type": "resistor", "package": "0805"},
        )
        placement = _minimal_placement(
            _place("R1", "capacitor", "0805", 10, 15),
        )
        errors, warnings = validate_cross_reference(placement, netlist)
        assert any("component_type mismatch" in e for e in errors)

    def test_package_mismatch(self):
        netlist = _minimal_netlist(
            {"designator": "R1", "component_type": "resistor", "package": "0805"},
        )
        placement = _minimal_placement(
            _place("R1", "resistor", "0603", 10, 15),
        )
        errors, warnings = validate_cross_reference(placement, netlist)
        assert any("package mismatch" in e for e in errors)

    def test_duplicate_placement(self):
        netlist = _minimal_netlist(
            {"designator": "R1", "component_type": "resistor", "package": "0805"},
        )
        placement = _minimal_placement(
            _place("R1", "resistor", "0805", 10, 15),
            _place("R1", "resistor", "0805", 20, 15),
        )
        errors, warnings = validate_cross_reference(placement, netlist)
        assert any("Duplicate" in e for e in errors)


# --- Board boundary tests ---

class TestBoardBoundary:
    def test_within_bounds(self):
        placement = _minimal_placement(
            _place("R1", "resistor", "0805", 10, 15, w=2.5, h=1.8),
            board_w=50, board_h=30,
        )
        errors = validate_board_boundary(placement)
        assert len(errors) == 0

    def test_extends_past_left(self):
        # x=1, w=2.5 → bbox left = -0.25
        placement = _minimal_placement(
            _place("R1", "resistor", "0805", 1, 15, w=2.5, h=1.8),
            board_w=50, board_h=30,
        )
        errors = validate_board_boundary(placement)
        assert any("left" in e for e in errors)

    def test_extends_past_right(self):
        # x=49, w=2.5 → bbox right = 50.25
        placement = _minimal_placement(
            _place("R1", "resistor", "0805", 49, 15, w=2.5, h=1.8),
            board_w=50, board_h=30,
        )
        errors = validate_board_boundary(placement)
        assert any("right" in e for e in errors)

    def test_extends_past_top(self):
        placement = _minimal_placement(
            _place("R1", "resistor", "0805", 10, 29.5, w=2.5, h=1.8),
            board_w=50, board_h=30,
        )
        errors = validate_board_boundary(placement)
        assert any("top" in e for e in errors)

    def test_rotation_swaps_dimensions(self):
        # 0805: w=2.5, h=1.8. At 90°: effective w=1.8, h=2.5
        # x=1, rotated: bbox left = 1 - 0.9 = 0.1 — within bounds
        placement = _minimal_placement(
            _place("R1", "resistor", "0805", 1, 15, w=2.5, h=1.8, rot=90),
            board_w=50, board_h=30,
        )
        errors = validate_board_boundary(placement)
        assert len(errors) == 0


# --- Overlap and clearance tests ---

class TestOverlapAndClearance:
    def test_no_overlap(self):
        placement = _minimal_placement(
            _place("R1", "resistor", "0805", 10, 15, w=2.5, h=1.8),
            _place("R2", "resistor", "0805", 20, 15, w=2.5, h=1.8),
        )
        errors, warnings = validate_overlap_and_clearance(placement)
        assert len(errors) == 0

    def test_overlap(self):
        placement = _minimal_placement(
            _place("R1", "resistor", "0805", 10, 15, w=2.5, h=1.8),
            _place("R2", "resistor", "0805", 11, 15, w=2.5, h=1.8),
        )
        errors, warnings = validate_overlap_and_clearance(placement)
        assert any("overlap" in e for e in errors)

    def test_insufficient_clearance(self):
        # R1 bbox: (8.75, 14.1) to (11.25, 15.9)
        # R2 bbox: (11.5, 14.1) to (14.0, 15.9)  — gap = 0.25mm < 0.5mm
        placement = _minimal_placement(
            _place("R1", "resistor", "0805", 10, 15, w=2.5, h=1.8),
            _place("R2", "resistor", "0805", 12.75, 15, w=2.5, h=1.8),
        )
        errors, warnings = validate_overlap_and_clearance(placement)
        assert any("clearance" in e for e in errors)

    def test_different_layers_no_conflict(self):
        placement = _minimal_placement(
            _place("R1", "resistor", "0805", 10, 15, layer="top"),
            _place("R2", "resistor", "0805", 10, 15, layer="bottom"),
        )
        errors, warnings = validate_overlap_and_clearance(placement)
        assert len(errors) == 0


# --- Placement rules tests (warnings) ---

class TestPlacementRules:
    def test_connector_on_edge(self):
        placement = _minimal_placement(
            _place("J1", "connector", "PinHeader_1x2", 3, 15, w=5.6, h=3.1),
            board_w=50, board_h=30,
        )
        warnings = validate_placement_rules(placement)
        assert len(warnings) == 0  # Within 5mm of left edge

    def test_connector_far_from_edge(self):
        placement = _minimal_placement(
            _place("J1", "connector", "PinHeader_1x2", 25, 15, w=5.6, h=3.1),
            board_w=50, board_h=30,
        )
        warnings = validate_placement_rules(placement)
        assert any("connector" in w.lower() for w in warnings)


# --- Full validation integration test ---

class TestFullValidation:
    def test_valid_placement(self):
        netlist = _minimal_netlist(
            {"designator": "R1", "component_type": "resistor", "package": "0805"},
            {"designator": "D1", "component_type": "led", "package": "0805"},
        )
        placement = _minimal_placement(
            _place("R1", "resistor", "0805", 10, 15),
            _place("D1", "led", "0805", 20, 15),
        )

        netlist_path = _write_temp(netlist)
        placement_path = _write_temp(placement)

        try:
            result = validate_placement(placement_path, netlist_path)
            assert result["valid"] is True, f"Unexpected errors: {result['errors']}"
            assert len(result["errors"]) == 0
        finally:
            os.unlink(netlist_path)
            os.unlink(placement_path)

    def test_invalid_missing_component(self):
        netlist = _minimal_netlist(
            {"designator": "R1", "component_type": "resistor", "package": "0805"},
            {"designator": "D1", "component_type": "led", "package": "0805"},
        )
        placement = _minimal_placement(
            _place("R1", "resistor", "0805", 10, 15),
        )

        netlist_path = _write_temp(netlist)
        placement_path = _write_temp(placement)

        try:
            result = validate_placement(placement_path, netlist_path)
            assert result["valid"] is False
            assert any("D1" in e for e in result["errors"])
        finally:
            os.unlink(netlist_path)
            os.unlink(placement_path)

    def test_invalid_overlapping(self):
        netlist = _minimal_netlist(
            {"designator": "R1", "component_type": "resistor", "package": "0805"},
            {"designator": "R2", "component_type": "resistor", "package": "0805"},
        )
        placement = _minimal_placement(
            _place("R1", "resistor", "0805", 10, 15),
            _place("R2", "resistor", "0805", 10, 15),  # Same position
        )

        netlist_path = _write_temp(netlist)
        placement_path = _write_temp(placement)

        try:
            result = validate_placement(placement_path, netlist_path)
            assert result["valid"] is False
            assert any("overlap" in e for e in result["errors"])
        finally:
            os.unlink(netlist_path)
            os.unlink(placement_path)
