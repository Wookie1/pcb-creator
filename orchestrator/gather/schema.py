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


def validate_requirements(data: dict) -> list[str]:
    """Validate requirements JSON against schema. Returns list of error messages."""
    validator = jsonschema.Draft7Validator(REQUIREMENTS_SCHEMA)
    errors = []
    for error in sorted(validator.iter_errors(data), key=lambda e: list(e.path)):
        path = ".".join(str(p) for p in error.absolute_path) or "(root)"
        errors.append(f"{path}: {error.message}")
    return errors
