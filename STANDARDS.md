# PCB-Creator Standards

This document defines file formats, naming conventions, and validation rules for the PCB-Creator tool. Agents reference specific sections of this file — each section is self-contained.

---

## 1. File Format Standards

- All data files use JSON format with `.json` extension.
- All documentation and framework files use Markdown with `.md` extension.
- All files use UTF-8 encoding.
- No binary files are used in the workflow.
- JSON files must be valid and parseable. No comments, no trailing commas.
- Structured runtime data (STATUS, QUALITY) uses `.json`. Prose runtime data (REQUIREMENTS) uses `.md`.

---

## 2. Circuit Netlist Schema

The circuit netlist is a JSON file containing a flat array of typed elements. The full JSON Schema is in `schemas/circuit_schema.json`.

### Top-Level Structure

```
{
  "version": "1.0",
  "project_name": "<lowercase_with_underscores>",
  "description": "<human-readable description>",
  "elements": [ ... ]
}
```

### Element Types

There are exactly three element types:

**component** — A physical electronic part.
- `component_id`: unique ID, must start with `comp_` (e.g., `comp_r1`)
- `designator`: standard reference designator (e.g., `R1`, `D2`, `U1`)
- `component_type`: one of: resistor, capacitor, inductor, led, diode, transistor_npn, transistor_pnp, transistor_nmos, transistor_pmos, ic, connector, switch, voltage_regulator, crystal, fuse, relay
- `value`: value with units as a string (e.g., `220ohm`, `100nF`, `red`, `LM7805`)
- `package`: footprint name (e.g., `0805`, `SOT-23`, `DIP-8`)
- `description`: what this component does in the circuit
- `properties`: (optional) extra key-value specs like `forward_voltage: "2.0V"`

**port** — A pin on a component.
- `port_id`: unique ID, must start with `port_` (e.g., `port_r1_1`)
- `component_id`: must match an existing component's `component_id`
- `pin_number`: integer starting at 1
- `name`: human-readable pin name (e.g., `1`, `anode`, `VCC`)
- `electrical_type`: one of: power_in, power_out, signal, ground, passive, no_connect

**net** — An electrical connection between two or more ports.
- `net_id`: unique ID, must start with `net_` (e.g., `net_vcc`)
- `name`: human-readable net name (e.g., `VCC`, `GND`, `R1_TO_D1`)
- `connected_port_ids`: array of port_id values (minimum 2)
- `net_class`: one of: signal, power, ground

### ID Naming Rules

- All IDs use lowercase letters, numbers, and underscores only.
- Component IDs: `comp_` + descriptive name (e.g., `comp_r1`, `comp_u1`)
- Port IDs: `port_` + component reference + pin identifier (e.g., `port_r1_1`, `port_d1_a`)
- Net IDs: `net_` + descriptive name (e.g., `net_vcc`, `net_gnd`, `net_r1_to_d1`)

---

## 3. Designator Conventions

Standard reference designator prefixes by component type:

| Prefix | Component Types |
|--------|----------------|
| R | resistor |
| C | capacitor |
| L | inductor |
| D | led, diode |
| Q | transistor_npn, transistor_pnp, transistor_nmos, transistor_pmos |
| U | ic, voltage_regulator |
| J | connector |
| SW | switch |
| Y | crystal |
| F | fuse |
| K | relay |
| RV | resistor (variable/potentiometer) |

Numbering rules:
- Sequential starting from 1 (R1, R2, R3...)
- No gaps in numbering within a designator prefix
- Each designator must be unique across the entire design

---

## 4. Validation Rules

The netlist validator (`validators/validate_netlist.py`) enforces these rules:

1. The JSON must conform to `schemas/circuit_schema.json`.
2. Every port's `component_id` must reference an existing component.
3. Every net's `connected_port_ids` must all reference existing ports.
4. Every component must have at least one port.
5. No duplicate IDs across all elements (component_id, port_id, net_id).
6. No duplicate designators.
7. Designator prefixes must match the component_type (see table above).
8. Designator numbers must be sequential starting from 1 per prefix.
9. No duplicate pin_numbers within the same component.
10. All ports should appear in at least one net (warning if not, unless `electrical_type` is `no_connect`).

---

## 5. Project Directory Structure

Each project is created under `projects/{project_name}/`:

```
projects/{project_name}/
├── REQUIREMENTS.md              # Project requirements (prose)
├── STATUS.json                  # Workflow state
├── QUALITY.json                 # QA reports
├── {project_name}_netlist.json  # Circuit netlist output
├── {project_name}_bom.json     # Bill of materials
├── {project_name}_placement.json # Component placement
├── {project_name}_routed.json  # Routed PCB (traces + vias)
└── *_view.html                 # Interactive visualizations
```

### STATUS.json Format

```json
{
  "project_name": "example",
  "current_step": 1,
  "current_status": "IN_PROGRESS",
  "steps": {
    "0": { "status": "COMPLETE", "timestamp": "2026-03-15T10:00:00Z" },
    "1": { "status": "IN_PROGRESS", "timestamp": "2026-03-15T10:05:00Z", "rework_count": 0 }
  }
}
```

### QUALITY.json Format

```json
{
  "project_name": "example",
  "reviews": [
    {
      "step": 1,
      "step_name": "Schematic/Netlist",
      "passed": true,
      "issues": [],
      "summary": "Netlist validates against schema and meets requirements.",
      "timestamp": "2026-03-15T10:10:00Z"
    }
  ]
}
```

---

## 6. Value Format Conventions

Component values embed units directly in the string:

| Type | Examples | Notes |
|------|----------|-------|
| Resistance | `220ohm`, `10kohm`, `1Mohm` | Use k for kilo, M for mega |
| Capacitance | `100nF`, `10uF`, `1pF` | Use p/n/u prefixes |
| Inductance | `10uH`, `100nH`, `1mH` | Use n/u/m prefixes |
| Voltage | `3.3V`, `5V`, `12V` | Used in properties |
| Current | `20mA`, `1A` | Used in properties |
| Other | `red`, `LM7805`, `2-pin header` | Descriptive strings |

---

## 7. Routing Parameters

Default routing parameters (configurable via `RouterConfig`):

| Parameter | Default | Description |
|-----------|---------|-------------|
| Trace width (power) | 0.5mm | Minimum, may be upsized by IPC-2221 |
| Trace width (ground) | 0.5mm | Minimum, may be upsized by IPC-2221 |
| Trace width (signal) | 0.25mm | Minimum for signal nets |
| Trace clearance | 0.2mm | Minimum distance between traces of different nets |
| Via drill | 0.3mm | Via hole diameter |
| Via diameter | 0.6mm | Via annular ring outer diameter |
| Grid resolution | 0.25mm | A* routing grid cell size |
| Copper weight | 0.5oz | Default copper weight (~17.5μm), affects IPC-2221 calculation |

### IPC-2221 Trace Width

The router auto-calculates minimum trace width using IPC-2221:
- External layer formula: `I = 0.048 × ΔT^0.44 × A^0.725`
- Default temperature rise: 10°C
- The final trace width is `max(IPC-2221 minimum, default for net class)`
- Net current is estimated from component properties (LED forward current, voltage regulator max current)
