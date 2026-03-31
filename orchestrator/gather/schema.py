"""Requirements JSON schema and validation."""

import jsonschema

REQUIREMENTS_SCHEMA = {
    "$schema": "http://json-schema.org/draft-07/schema#",
    "title": "PCB-Creator Requirements",
    "type": "object",
    "required": ["project_name", "description", "components", "connections"],
    "additionalProperties": False,
    "properties": {
        "project_name": {
            "type": "string",
            "pattern": "^[a-z][a-z0-9_]*$",
        },
        "description": {"type": "string", "minLength": 1},
        "power": {
            "type": "object",
            "required": ["voltage", "source"],
            "properties": {
                "voltage": {"type": "string"},
                "source": {"type": "string"},
            },
        },
        "components": {
            "type": "array",
            "minItems": 1,
            "items": {
                "type": "object",
                "required": ["ref", "type"],
                "additionalProperties": False,
                "properties": {
                    "ref": {
                        "type": "string",
                        "description": "Unique reference like R1, D1, J1",
                    },
                    "type": {
                        "type": "string",
                        "description": "Component type: resistor, led, connector, etc.",
                    },
                    "value": {
                        "type": "string",
                        "description": "Component value with units: 150ohm, red, 2-pin",
                    },
                    "package": {"type": "string"},
                    "specs": {
                        "type": "object",
                        "description": "Additional specs: vf, if, power_rating, etc.",
                    },
                    "purpose": {
                        "type": "string",
                        "description": "What this component does in the circuit",
                    },
                },
            },
        },
        "connections": {
            "type": "array",
            "minItems": 1,
            "description": "Explicit point-to-point connections between component pins",
            "items": {
                "type": "object",
                "required": ["net_name", "pins"],
                "additionalProperties": False,
                "properties": {
                    "net_name": {
                        "type": "string",
                        "description": "Net name: VCC, GND, R1_TO_D1, etc.",
                    },
                    "net_class": {
                        "type": "string",
                        "enum": ["power", "ground", "signal"],
                    },
                    "pins": {
                        "type": "array",
                        "minItems": 2,
                        "items": {
                            "type": "string",
                            "description": "Pin reference: J1.1, R1.1, D1.anode, etc.",
                        },
                    },
                },
            },
        },
        "packages": {"type": "string"},
        "calculations": {
            "type": "object",
            "additionalProperties": {
                "type": "object",
                "properties": {
                    "formula": {"type": "string"},
                    "value": {"type": "string"},
                    "power": {"type": "string"},
                    "package_ok": {"type": "boolean"},
                },
            },
        },
        "board": {
            "type": "object",
            "properties": {
                "width_mm": {"type": "number", "minimum": 5},
                "height_mm": {"type": "number", "minimum": 5},
                "corner_radius_mm": {"type": "number"},
                "layers": {"type": "integer", "enum": [1, 2, 4]},
                "outline_type": {
                    "type": "string",
                    "enum": ["rectangle", "dxf"],
                },
                "copper_weight_oz": {
                    "type": "number",
                    "enum": [0.5, 1.0, 2.0],
                    "description": "Copper weight in oz/ft². Default 0.5oz (~17.5μm)",
                },
            },
        },
        "manufacturing": {
            "type": "object",
            "description": "PCB manufacturing specifications and DFM rules",
            "properties": {
                "manufacturer": {
                    "type": "string",
                    "description": "Manufacturer name or profile (e.g., 'jlcpcb_standard', 'oshpark_2layer', 'pcbway_standard')",
                },
                "trace_width_min_mm": {
                    "type": "number",
                    "minimum": 0.05,
                    "description": "Minimum trace width in mm",
                },
                "clearance_min_mm": {
                    "type": "number",
                    "minimum": 0.05,
                    "description": "Minimum trace-to-trace/pad clearance in mm",
                },
                "via_drill_min_mm": {
                    "type": "number",
                    "minimum": 0.1,
                    "description": "Minimum via drill diameter in mm",
                },
                "via_diameter_min_mm": {
                    "type": "number",
                    "minimum": 0.2,
                    "description": "Minimum via pad diameter in mm",
                },
            },
        },
        "placement_hints": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["ref"],
                "additionalProperties": False,
                "properties": {
                    "ref": {"type": "string"},
                    "x_mm": {"type": "number"},
                    "y_mm": {"type": "number"},
                    "rotation_deg": {
                        "type": "integer",
                        "enum": [0, 90, 180, 270],
                    },
                    "edge": {
                        "type": "string",
                        "enum": ["top", "bottom", "left", "right"],
                    },
                    "near": {
                        "type": "string",
                        "description": "Place near this designator",
                    },
                },
            },
        },
        "attachments": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["filename", "type", "purpose"],
                "additionalProperties": False,
                "properties": {
                    "filename": {"type": "string"},
                    "type": {
                        "type": "string",
                        "enum": [
                            "board_outline",
                            "sketch",
                            "photo",
                            "datasheet",
                            "other",
                        ],
                    },
                    "purpose": {"type": "string"},
                    "used_by_steps": {
                        "type": "array",
                        "items": {"type": "integer"},
                    },
                },
            },
        },
    },
}


def coerce_requirements_types(data: dict) -> dict:
    """Fix common LLM type errors: string numbers → actual numbers.

    LLMs frequently output "2" instead of 2 in JSON. This function walks
    the requirements dict and coerces string values to the types the schema
    expects, so validation passes without rework loops.
    """
    # --- Strip None values from all sub-objects ---
    # LLMs frequently output null for optional fields, which fails
    # JSON Schema validation (schema expects the type or key absence).
    for section_key in ("board", "manufacturing", "power"):
        section = data.get(section_key)
        if isinstance(section, dict):
            none_keys = [k for k, v in section.items() if v is None]
            for k in none_keys:
                del section[k]

    # Also strip None from component specs
    for comp in data.get("components", []):
        if isinstance(comp, dict):
            none_keys = [k for k, v in comp.items() if v is None]
            for k in none_keys:
                del comp[k]
            specs = comp.get("specs")
            if isinstance(specs, dict):
                none_keys = [k for k, v in specs.items() if v is None]
                for k in none_keys:
                    del specs[k]

    # --- board fields ---
    board = data.get("board")
    if isinstance(board, dict):
        for key in ("width_mm", "height_mm", "corner_radius_mm", "copper_weight_oz"):
            if key in board and isinstance(board[key], str):
                try:
                    board[key] = float(board[key])
                except (ValueError, TypeError):
                    pass
        if "layers" in board and isinstance(board["layers"], str):
            try:
                board["layers"] = int(board["layers"])
            except (ValueError, TypeError):
                pass

    # --- manufacturing fields ---
    mfg = data.get("manufacturing")
    if isinstance(mfg, dict):
        for key in ("trace_width_min_mm", "clearance_min_mm",
                     "via_drill_min_mm", "via_diameter_min_mm"):
            if key in mfg and isinstance(mfg[key], str):
                try:
                    mfg[key] = float(mfg[key])
                except (ValueError, TypeError):
                    pass

    # --- placement_hints fields ---
    for hint in data.get("placement_hints", []):
        if not isinstance(hint, dict):
            continue
        # Strip None values — LLMs often output null for optional fields
        # which fails JSON Schema validation (expects type or absence)
        none_keys = [k for k, v in hint.items() if v is None]
        for k in none_keys:
            del hint[k]
        for key in ("x_mm", "y_mm"):
            if key in hint and isinstance(hint[key], str):
                try:
                    hint[key] = float(hint[key])
                except (ValueError, TypeError):
                    pass
        if "rotation_deg" in hint and isinstance(hint["rotation_deg"], str):
            try:
                hint["rotation_deg"] = int(hint["rotation_deg"])
            except (ValueError, TypeError):
                pass

    # --- attachment used_by_steps ---
    for att in data.get("attachments", []):
        if not isinstance(att, dict):
            continue
        # Strip None values here too
        none_keys = [k for k, v in att.items() if v is None]
        for k in none_keys:
            del att[k]
        steps = att.get("used_by_steps")
        if isinstance(steps, list):
            att["used_by_steps"] = [
                int(s) if isinstance(s, str) else s for s in steps
            ]

    return data


def validate_requirements(data: dict) -> list[str]:
    """Validate requirements JSON against schema. Returns list of error messages.

    Automatically coerces common LLM type errors (string→number) before validation.
    """
    data = coerce_requirements_types(data)
    validator = jsonschema.Draft7Validator(REQUIREMENTS_SCHEMA)
    errors = []
    for error in sorted(validator.iter_errors(data), key=lambda e: list(e.path)):
        path = ".".join(str(p) for p in error.absolute_path) or "(root)"
        errors.append(f"{path}: {error.message}")

    # Post-schema: check for duplicate pins across connections
    errors.extend(_validate_pin_uniqueness(data))

    return errors


def _validate_pin_uniqueness(data: dict) -> list[str]:
    """Ensure each component pin appears in at most one connection/net."""
    errors = []
    pin_to_net: dict[str, str] = {}

    for conn in data.get("connections", []):
        net_name = conn.get("net_name", "?")
        for pin in conn.get("pins", []):
            if pin in pin_to_net:
                errors.append(
                    f"connections: Pin '{pin}' appears in multiple nets "
                    f"('{pin_to_net[pin]}' and '{net_name}'). "
                    f"Each pin must belong to exactly one net."
                )
            else:
                pin_to_net[pin] = net_name

    return errors


def auto_fix_duplicate_pins(data: dict) -> tuple[dict, list[str]]:
    """Last-resort fix for pins appearing in multiple nets.

    When a pin appears in both a power/ground net and a signal net,
    remove it from the power/ground net (keep the more specific signal
    assignment). Returns (fixed_data, warnings).

    Only call this after rework attempts are exhausted.
    """
    import copy

    warnings: list[str] = []
    data = copy.deepcopy(data)

    _POWER_CLASSES = {"power", "ground"}

    # Build pin → list of (net_index, net_name, net_class)
    pin_nets: dict[str, list[tuple[int, str, str]]] = {}
    for i, conn in enumerate(data.get("connections", [])):
        net_name = conn.get("net_name", "?")
        net_class = conn.get("net_class", "signal")
        for pin in conn.get("pins", []):
            pin_nets.setdefault(pin, []).append((i, net_name, net_class))

    # Find duplicates and decide which to remove
    removals: list[tuple[int, str, str]] = []  # (conn_index, pin, net_name)
    for pin, nets in pin_nets.items():
        if len(nets) <= 1:
            continue

        # Separate power/ground nets from signal nets
        power_entries = [(i, name, cls) for i, name, cls in nets if cls in _POWER_CLASSES]
        signal_entries = [(i, name, cls) for i, name, cls in nets if cls not in _POWER_CLASSES]

        if power_entries and signal_entries:
            # Remove from power/ground nets, keep in signal nets
            for idx, net_name, _ in power_entries:
                removals.append((idx, pin, net_name))
                warnings.append(
                    f"Auto-fix: removed '{pin}' from '{net_name}' net "
                    f"(kept in signal net)"
                )
        elif len(signal_entries) > 1:
            # Multiple signal nets — keep in the first, remove from rest
            for idx, net_name, _ in signal_entries[1:]:
                removals.append((idx, pin, net_name))
                warnings.append(
                    f"Auto-fix: removed '{pin}' from '{net_name}' net "
                    f"(duplicate signal assignment)"
                )

    # Apply removals
    for conn_idx, pin, _ in removals:
        conn = data["connections"][conn_idx]
        pins = conn.get("pins", [])
        if pin in pins:
            pins.remove(pin)

    # Remove connections with fewer than 2 pins (now invalid)
    data["connections"] = [
        c for c in data["connections"]
        if len(c.get("pins", [])) >= 2
    ]

    return data, warnings
