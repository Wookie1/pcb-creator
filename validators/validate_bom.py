#!/usr/bin/env python3
"""
BOM Validator for PCB-Creator
Validates Bill of Materials JSON against the schema and cross-references with the source netlist.

Usage:
    python validate_bom.py <bom.json> --netlist <netlist.json> [--schema <bom_schema.json>]

Output:
    JSON to stdout: { "valid": true/false, "errors": [...], "warnings": [...], "summary": "..." }
"""

import json
import sys
import os

try:
    import jsonschema
except ImportError:
    print(json.dumps({
        "valid": False,
        "errors": ["Python 'jsonschema' package not installed. Run: pip install jsonschema"],
        "warnings": [],
        "summary": "Missing dependency"
    }))
    sys.exit(1)


# Specs that should be present for each component type (warnings if missing)
EXPECTED_SPECS = {
    "resistor": ["tolerance", "power_rating"],
    "capacitor": ["voltage_rating"],
    "led": ["forward_voltage"],
    "inductor": ["current_rating"],
    "diode": ["reverse_voltage"],
    "voltage_regulator": ["output_voltage", "max_current"],
}


def validate_schema(bom: dict, schema: dict) -> list[str]:
    """Validate BOM against JSON Schema. Returns list of error strings."""
    errors = []
    validator = jsonschema.Draft7Validator(schema)
    for error in sorted(validator.iter_errors(bom), key=lambda e: list(e.path)):
        path = ".".join(str(p) for p in error.absolute_path)
        if path:
            errors.append(f"Schema: {path} - {error.message}")
        else:
            errors.append(f"Schema: {error.message}")
    return errors


def validate_cross_reference(bom: dict, netlist: dict) -> tuple[list[str], list[str]]:
    """
    Cross-reference BOM against the source netlist.
    Returns (errors, warnings).
    """
    errors = []
    warnings = []

    # Build netlist component lookup by designator
    elements = netlist.get("elements", [])
    netlist_components = {}
    for elem in elements:
        if elem.get("element_type") == "component":
            des = elem.get("designator")
            if des:
                netlist_components[des] = elem

    # Build BOM lookup by designator
    bom_items = bom.get("bom", [])
    bom_designators = {}
    for item in bom_items:
        des = item.get("designator")
        if des:
            if des in bom_designators:
                errors.append(f"Duplicate BOM entry for designator '{des}'")
            bom_designators[des] = item

    # Check: every netlist component must appear in BOM
    for des in netlist_components:
        if des not in bom_designators:
            errors.append(f"Netlist component '{des}' is missing from BOM")

    # Check: no phantom BOM entries without matching netlist component
    for des in bom_designators:
        if des not in netlist_components:
            errors.append(f"BOM entry '{des}' has no matching netlist component")

    # Cross-check matching entries
    for des, bom_item in bom_designators.items():
        net_comp = netlist_components.get(des)
        if net_comp is None:
            continue

        # Component type must match
        bom_type = bom_item.get("component_type")
        net_type = net_comp.get("component_type")
        if bom_type != net_type:
            errors.append(
                f"'{des}' component_type mismatch: BOM has '{bom_type}', "
                f"netlist has '{net_type}'"
            )

        # Package must match
        bom_pkg = bom_item.get("package", "")
        net_pkg = net_comp.get("package", "")
        if bom_pkg.lower() != net_pkg.lower():
            errors.append(
                f"'{des}' package mismatch: BOM has '{bom_pkg}', "
                f"netlist has '{net_pkg}'"
            )

        # Value must match
        bom_val = bom_item.get("value", "")
        net_val = net_comp.get("value", "")
        if bom_val.lower() != net_val.lower():
            errors.append(
                f"'{des}' value mismatch: BOM has '{bom_val}', "
                f"netlist has '{net_val}'"
            )

    return errors, warnings


def validate_specs_completeness(bom: dict) -> list[str]:
    """
    Check that BOM items have appropriate specs for their component type.
    Returns warnings (not errors — specs are recommendations).
    """
    warnings = []
    for item in bom.get("bom", []):
        des = item.get("designator", "?")
        ctype = item.get("component_type", "")
        specs = item.get("specs", {})

        expected = EXPECTED_SPECS.get(ctype, [])
        for spec_name in expected:
            if spec_name not in specs:
                warnings.append(f"'{des}' ({ctype}) is missing recommended spec '{spec_name}'")

    return warnings


def validate_bom(
    bom_path: str,
    netlist_path: str | None = None,
    schema_path: str | None = None,
) -> dict:
    """
    Full validation of a BOM file.
    Returns dict with: valid, errors, warnings, summary
    """
    # Resolve schema path
    if schema_path is None:
        schema_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "schemas", "bom_schema.json"
        )

    # Load BOM
    try:
        with open(bom_path, "r") as f:
            bom = json.load(f)
    except json.JSONDecodeError as e:
        return {
            "valid": False,
            "errors": [f"Invalid JSON: {e}"],
            "warnings": [],
            "summary": "File is not valid JSON"
        }
    except FileNotFoundError:
        return {
            "valid": False,
            "errors": [f"File not found: {bom_path}"],
            "warnings": [],
            "summary": "File not found"
        }

    # Load schema
    try:
        with open(schema_path, "r") as f:
            schema = json.load(f)
    except (json.JSONDecodeError, FileNotFoundError) as e:
        return {
            "valid": False,
            "errors": [f"Cannot load schema: {e}"],
            "warnings": [],
            "summary": "Schema file error"
        }

    # Run validations
    all_errors = []
    all_warnings = []

    # 1. JSON Schema validation
    schema_errors = validate_schema(bom, schema)
    all_errors.extend(schema_errors)

    # 2. Cross-reference with netlist (if provided)
    if not schema_errors and netlist_path:
        try:
            with open(netlist_path, "r") as f:
                netlist = json.load(f)
            xref_errors, xref_warnings = validate_cross_reference(bom, netlist)
            all_errors.extend(xref_errors)
            all_warnings.extend(xref_warnings)
        except (json.JSONDecodeError, FileNotFoundError) as e:
            all_warnings.append(f"Could not load netlist for cross-reference: {e}")

    # 3. Specs completeness (warnings only)
    if not all_errors:
        spec_warnings = validate_specs_completeness(bom)
        all_warnings.extend(spec_warnings)

    # Build summary
    valid = len(all_errors) == 0
    if valid and not all_warnings:
        summary = "BOM is valid. No errors or warnings."
    elif valid:
        summary = f"BOM is valid with {len(all_warnings)} warning(s)."
    else:
        summary = f"BOM is INVALID. {len(all_errors)} error(s), {len(all_warnings)} warning(s)."

    return {
        "valid": valid,
        "errors": all_errors,
        "warnings": all_warnings,
        "summary": summary
    }


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(f"Usage: {sys.argv[0]} <bom.json> --netlist <netlist.json> [--schema <bom_schema.json>]")
        sys.exit(1)

    bom_file = sys.argv[1]
    netlist_file = None
    schema_file = None

    if "--netlist" in sys.argv:
        idx = sys.argv.index("--netlist")
        if idx + 1 < len(sys.argv):
            netlist_file = sys.argv[idx + 1]

    if "--schema" in sys.argv:
        idx = sys.argv.index("--schema")
        if idx + 1 < len(sys.argv):
            schema_file = sys.argv[idx + 1]

    result = validate_bom(bom_file, netlist_file, schema_file)
    print(json.dumps(result, indent=2))
    sys.exit(0 if result["valid"] else 1)
