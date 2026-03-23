#!/usr/bin/env python3
"""
Netlist Validator for PCB-Creator
Validates circuit netlist JSON files against the schema and referential integrity rules.

Usage:
    python validate_netlist.py <netlist.json> [--schema <schema.json>]

Output:
    JSON to stdout: { "valid": true/false, "errors": [...], "warnings": [...], "summary": "..." }
"""

import json
import sys
import os
import re
from collections import Counter

# Ensure validators/ is on the path for sibling imports
_validators_dir = os.path.dirname(os.path.abspath(__file__))
if _validators_dir not in sys.path:
    sys.path.insert(0, _validators_dir)

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

from drc_checks import run_all_drc_checks


# Maps component_type to expected designator prefix(es)
DESIGNATOR_MAP = {
    "resistor": ["R"],
    "capacitor": ["C"],
    "inductor": ["L"],
    "led": ["D"],
    "diode": ["D"],
    "transistor_npn": ["Q"],
    "transistor_pnp": ["Q"],
    "transistor_nmos": ["Q"],
    "transistor_pmos": ["Q"],
    "ic": ["U"],
    "connector": ["J"],
    "switch": ["SW"],
    "voltage_regulator": ["U"],
    "crystal": ["Y"],
    "fuse": ["F"],
}


def validate_schema(netlist: dict, schema: dict) -> list[str]:
    """Validate netlist against JSON Schema. Returns list of error strings."""
    errors = []
    validator = jsonschema.Draft7Validator(schema)
    for error in sorted(validator.iter_errors(netlist), key=lambda e: list(e.path)):
        path = ".".join(str(p) for p in error.absolute_path)
        if path:
            errors.append(f"Schema: {path} - {error.message}")
        else:
            errors.append(f"Schema: {error.message}")
    return errors


def validate_referential_integrity(netlist: dict) -> tuple[list[str], list[str]]:
    """
    Validate referential integrity beyond what JSON Schema can express.
    Returns (errors, warnings).
    """
    errors = []
    warnings = []

    elements = netlist.get("elements", [])

    # Collect elements by type
    components = {}
    ports = {}
    nets = {}
    all_ids = []

    for i, elem in enumerate(elements):
        etype = elem.get("element_type")
        if etype == "component":
            cid = elem.get("component_id", f"<missing_id_at_index_{i}>")
            components[cid] = elem
            all_ids.append(cid)
        elif etype == "port":
            pid = elem.get("port_id", f"<missing_id_at_index_{i}>")
            ports[pid] = elem
            all_ids.append(pid)
        elif etype == "net":
            nid = elem.get("net_id", f"<missing_id_at_index_{i}>")
            nets[nid] = elem
            all_ids.append(nid)

    # Check for duplicate IDs
    id_counts = Counter(all_ids)
    for eid, count in id_counts.items():
        if count > 1:
            errors.append(f"Duplicate ID: '{eid}' appears {count} times")

    # Check that every port references a valid component
    for pid, port in ports.items():
        cid = port.get("component_id")
        if cid not in components:
            errors.append(f"Port '{pid}' references non-existent component '{cid}'")

    # Check that every net references valid ports
    for nid, net in nets.items():
        for port_ref in net.get("connected_port_ids", []):
            if port_ref not in ports:
                errors.append(f"Net '{nid}' references non-existent port '{port_ref}'")

    # Check that every component has at least one port
    components_with_ports = set()
    for port in ports.values():
        components_with_ports.add(port.get("component_id"))
    for cid in components:
        if cid not in components_with_ports:
            errors.append(f"Component '{cid}' has no ports defined")

    # Check that every port appears in at least one net (unless no_connect)
    ports_in_nets = set()
    for net in nets.values():
        for port_ref in net.get("connected_port_ids", []):
            ports_in_nets.add(port_ref)
    for pid, port in ports.items():
        if pid not in ports_in_nets:
            if port.get("electrical_type") == "no_connect":
                continue
            warnings.append(f"Port '{pid}' is not connected to any net (and is not 'no_connect')")

    # Check that designator prefixes match component_type
    for cid, comp in components.items():
        ctype = comp.get("component_type")
        designator = comp.get("designator", "")
        if ctype in DESIGNATOR_MAP:
            expected_prefixes = DESIGNATOR_MAP[ctype]
            # Extract the letter prefix from the designator
            prefix_match = re.match(r"^([A-Z]+)", designator)
            if prefix_match:
                prefix = prefix_match.group(1)
                if prefix not in expected_prefixes:
                    errors.append(
                        f"Component '{cid}' has type '{ctype}' but designator '{designator}' "
                        f"uses prefix '{prefix}' (expected: {expected_prefixes})"
                    )
            else:
                errors.append(f"Component '{cid}' has invalid designator format: '{designator}'")

    # Check for duplicate designators
    designators = [c.get("designator") for c in components.values()]
    des_counts = Counter(designators)
    for des, count in des_counts.items():
        if count > 1:
            errors.append(f"Duplicate designator: '{des}' appears {count} times")

    # Check sequential designator numbering per prefix
    prefix_numbers = {}
    for comp in components.values():
        designator = comp.get("designator", "")
        match = re.match(r"^([A-Z]+)(\d+)$", designator)
        if match:
            prefix = match.group(1)
            number = int(match.group(2))
            if prefix not in prefix_numbers:
                prefix_numbers[prefix] = []
            prefix_numbers[prefix].append(number)

    for prefix, numbers in prefix_numbers.items():
        numbers_sorted = sorted(numbers)
        expected = list(range(1, len(numbers_sorted) + 1))
        if numbers_sorted != expected:
            errors.append(
                f"Designators with prefix '{prefix}' are not sequential starting from 1: "
                f"found {numbers_sorted}, expected {expected}"
            )

    # Check no duplicate port pin_numbers within the same component
    comp_pins = {}
    for port in ports.values():
        cid = port.get("component_id")
        pin = port.get("pin_number")
        if cid not in comp_pins:
            comp_pins[cid] = []
        comp_pins[cid].append(pin)
    for cid, pins in comp_pins.items():
        pin_counts = Counter(pins)
        for pin, count in pin_counts.items():
            if count > 1:
                errors.append(
                    f"Component '{cid}' has duplicate pin_number {pin} "
                    f"({count} ports with same pin)"
                )

    # Check no port appears in more than one net (one pin = one net)
    port_to_nets: dict[str, list[str]] = {}
    for nid, net in nets.items():
        for port_ref in net.get("connected_port_ids", []):
            if port_ref not in port_to_nets:
                port_to_nets[port_ref] = []
            port_to_nets[port_ref].append(nid)
    for port_ref, net_ids in port_to_nets.items():
        if len(net_ids) > 1:
            # Look up the port's component and pin for a clear error message
            port = ports.get(port_ref, {})
            comp_id = port.get("component_id", "?")
            comp = components.get(comp_id, {})
            designator = comp.get("designator", comp_id)
            pin_name = port.get("name", port.get("pin_number", "?"))
            net_names = [nets[nid].get("name", nid) for nid in net_ids]
            errors.append(
                f"Port '{port_ref}' ({designator} pin {pin_name}) appears in "
                f"multiple nets: {net_names}. A physical pin can only belong "
                f"to one net — merge these nets if they share a pin."
            )

    # Check that the same physical pin (component_id + pin_number) doesn't
    # reach multiple nets via different port elements.
    # (Complements the port-in-multiple-nets check above, which only catches
    # reuse of the same port_id string.)
    pin_to_nets: dict[tuple[str, str], list[tuple[str, str]]] = {}  # (comp_id, pin) -> [(port_id, net_id)]
    for nid, net in nets.items():
        for port_ref in net.get("connected_port_ids", []):
            port = ports.get(port_ref)
            if not port:
                continue
            cid = port.get("component_id")
            pin = str(port.get("pin_number", ""))
            key = (cid, pin)
            if key not in pin_to_nets:
                pin_to_nets[key] = []
            pin_to_nets[key].append((port_ref, nid))
    for (cid, pin), entries in pin_to_nets.items():
        # Deduplicate by net_id — same pin in same net via different ports is
        # caught by the duplicate-pin check, not here.
        unique_nets = list({nid for _, nid in entries})
        if len(unique_nets) > 1:
            comp = components.get(cid, {})
            designator = comp.get("designator", cid)
            net_names = [nets[nid].get("name", nid) for nid in unique_nets]
            port_ids = [pid for pid, _ in entries]
            errors.append(
                f"Physical pin {designator} pin {pin} connects to "
                f"{len(unique_nets)} nets ({net_names}) via port elements "
                f"{port_ids}. A pin can only belong to one net."
            )

    return errors, warnings


def validate_netlist(
    netlist_path: str,
    schema_path: str | None = None,
    requirements_path: str | None = None,
) -> dict:
    """
    Full validation of a netlist file.
    Returns dict with: valid, errors, warnings, summary
    """
    # Resolve schema path
    if schema_path is None:
        schema_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "schemas", "circuit_schema.json"
        )

    # Load files
    try:
        with open(netlist_path, "r") as f:
            netlist = json.load(f)
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
            "errors": [f"File not found: {netlist_path}"],
            "warnings": [],
            "summary": "File not found"
        }

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

    # Load requirements if provided (for power-aware DRC checks)
    requirements = None
    if requirements_path:
        try:
            with open(requirements_path, "r") as f:
                requirements = json.load(f)
        except (json.JSONDecodeError, FileNotFoundError):
            pass  # Silently skip — power-aware checks just won't run

    # Run validations
    all_errors = []
    all_warnings = []

    # 1. JSON Schema validation
    schema_errors = validate_schema(netlist, schema)
    all_errors.extend(schema_errors)

    # 2. Referential integrity (only if schema is roughly valid)
    if not schema_errors:
        ref_errors, ref_warnings = validate_referential_integrity(netlist)
        all_errors.extend(ref_errors)
        all_warnings.extend(ref_warnings)

    # 3. DRC checks (only if schema and referential integrity pass)
    if not all_errors:
        drc_errors, drc_warnings = run_all_drc_checks(
            netlist.get("elements", []),
            requirements=requirements,
        )
        all_errors.extend(drc_errors)
        all_warnings.extend(drc_warnings)

    # Build summary
    valid = len(all_errors) == 0
    if valid and not all_warnings:
        summary = "Netlist is valid. No errors or warnings."
    elif valid:
        summary = f"Netlist is valid with {len(all_warnings)} warning(s)."
    else:
        summary = f"Netlist is INVALID. {len(all_errors)} error(s), {len(all_warnings)} warning(s)."

    return {
        "valid": valid,
        "errors": all_errors,
        "warnings": all_warnings,
        "summary": summary
    }


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(f"Usage: {sys.argv[0]} <netlist.json> [--schema <schema.json>] [--requirements <requirements.json>]")
        sys.exit(1)

    netlist_file = sys.argv[1]
    schema_file = None
    requirements_file = None

    if "--schema" in sys.argv:
        idx = sys.argv.index("--schema")
        if idx + 1 < len(sys.argv):
            schema_file = sys.argv[idx + 1]

    if "--requirements" in sys.argv:
        idx = sys.argv.index("--requirements")
        if idx + 1 < len(sys.argv):
            requirements_file = sys.argv[idx + 1]

    result = validate_netlist(netlist_file, schema_file, requirements_file)
    print(json.dumps(result, indent=2))
    sys.exit(0 if result["valid"] else 1)
