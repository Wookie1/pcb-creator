# PCB Data Format Reference

This document describes the JSON data formats produced by the pcb-creator pipeline. Use it to build custom visualizations, analysis tools, or export adapters.

## Project Output Files

Each project in `projects/<name>/` contains:

| File | Description |
|------|-------------|
| `<name>_netlist.json` | Circuit connectivity (components, pins, nets) |
| `<name>_bom.json` | Bill of materials (values, specs, descriptions) |
| `<name>_placement.json` | Board layout (positions, rotations, footprints) |

All three files cross-reference by **designator** (e.g., `R1`, `U1`, `C3`).

---

## Coordinate System

- **Origin**: Bottom-left corner of the board at `(0, 0)`
- **X axis**: Left to right (millimeters)
- **Y axis**: Bottom to top (millimeters)
- **Component position**: Center point of the footprint bounding box
- **Rotation**: Counterclockwise in degrees — `0`, `90`, `180`, or `270`
- **Rotation effect**: `90`/`270` swap the footprint's width and height

```
         board_height_mm
    ┌────────────────────┐
    │                    │
    │    (x, y) = center │
    │                    │
    └────────────────────┘
  (0,0)          board_width_mm
```

---

## Placement JSON (`*_placement.json`)

```json
{
  "version": "1.0",
  "project_name": "my_project",
  "source_netlist": "my_project_netlist.json",
  "source_bom": "my_project_bom.json",
  "board": {
    "width_mm": 68.6,
    "height_mm": 53.4,
    "outline_type": "rectangle",
    "origin": [0, 0]
  },
  "placements": [
    {
      "designator": "R1",
      "component_type": "resistor",
      "package": "0805",
      "footprint_width_mm": 2.5,
      "footprint_height_mm": 1.8,
      "x_mm": 15.0,
      "y_mm": 10.0,
      "rotation_deg": 0,
      "layer": "top",
      "placement_source": "llm"
    }
  ]
}
```

### Placement Item Fields

| Field | Type | Description |
|-------|------|-------------|
| `designator` | string | Reference ID: `R1`, `C3`, `U1`, `FID1` |
| `component_type` | string | `resistor`, `capacitor`, `led`, `ic`, `connector`, `switch`, `crystal`, `voltage_regulator`, `fiducial`, etc. |
| `package` | string | Footprint name: `0805`, `SOT-23`, `TQFP-32`, `DIP-28`, `PinHeader_1x2`, `Fiducial_1mm` |
| `footprint_width_mm` | number | Footprint width before rotation (mm) |
| `footprint_height_mm` | number | Footprint height before rotation (mm) |
| `x_mm` | number | Center X position on board (mm from left) |
| `y_mm` | number | Center Y position on board (mm from bottom) |
| `rotation_deg` | int | `0`, `90`, `180`, or `270` degrees CCW |
| `layer` | string | `"top"` or `"bottom"` |
| `placement_source` | string | `"user"` (manually specified), `"llm"` (AI-generated), `"optimizer"` (SA-optimized) |

### Bounding Box Calculation

```python
w, h = footprint_width_mm, footprint_height_mm
if rotation_deg in (90, 270):
    w, h = h, w
x_min = x_mm - w / 2
y_min = y_mm - h / 2
x_max = x_mm + w / 2
y_max = y_mm + h / 2
```

---

## Netlist JSON (`*_netlist.json`)

Contains a flat `elements` array with three element types:

### Component Element
```json
{
  "element_type": "component",
  "component_id": "comp_r1",
  "designator": "R1",
  "component_type": "resistor",
  "value": "10kohm",
  "package": "0805",
  "description": "Pull-down resistor"
}
```

### Port Element (Pin)
```json
{
  "element_type": "port",
  "port_id": "port_r1_1",
  "component_id": "comp_r1",
  "pin_number": 1,
  "name": "1",
  "electrical_type": "passive"
}
```

`electrical_type`: `passive`, `power_in`, `power_out`, `signal`, `ground`, `no_connect`

### Net Element (Connection)
```json
{
  "element_type": "net",
  "net_id": "net_vcc",
  "name": "VCC",
  "connected_port_ids": ["port_r1_1", "port_sw1_2"],
  "net_class": "power"
}
```

`net_class`: `signal`, `power`, `ground`

### Resolving Connections

To find which components a net connects:
```
net.connected_port_ids → port.component_id → component.designator → placement.(x_mm, y_mm)
```

---

## BOM JSON (`*_bom.json`)

```json
{
  "version": "1.0",
  "project_name": "my_project",
  "source_netlist": "my_project_netlist.json",
  "bom": [
    {
      "designator": "R1",
      "component_type": "resistor",
      "value": "10kohm",
      "package": "0805",
      "quantity": 1,
      "specs": {
        "tolerance": "5%",
        "power_rating": "125mW",
        "material": "thick film"
      },
      "description": "10k ohm 5% 0805 SMD thick film resistor, 125mW",
      "notes": "Pull-down resistor for switch"
    }
  ]
}
```

### Common Specs by Component Type

| Type | Typical Specs |
|------|--------------|
| Resistor | `tolerance`, `power_rating`, `material` |
| Capacitor | `voltage_rating`, `type` (ceramic/electrolytic) |
| LED | `forward_voltage`, `forward_current`, `color` |
| Connector | `pin_count`, `pitch`, `mating_type` |
| Switch | `contact_rating`, `actuation_force`, `type` |
| IC | varies by device |

---

## Common Footprint Dimensions

| Package | Width (mm) | Height (mm) |
|---------|-----------|------------|
| 0402 | 1.5 | 1.0 |
| 0603 | 2.1 | 1.3 |
| 0805 | 2.5 | 1.8 |
| 1206 | 3.7 | 2.1 |
| SOT-23 | 3.4 | 2.8 |
| SOIC-8 | 5.4 | 5.2 |
| TQFP-32 | 9.0 | 9.0 |
| DIP-28 | 36.3 | 8.9 |
| PinHeader_1x2 | 5.6 | 3.1 |
| 6mm_tactile | 6.8 | 6.8 |
| Fiducial_1mm | 3.0 | 3.0 |

---

## Routed JSON (`*_routed.json`)

Extends the placement JSON with a `routing` object:

```json
{
  "version": "1.0",
  "project_name": "my_project",
  "board": { ... },
  "placements": [ ... ],
  "routing": {
    "traces": [
      {
        "start_x_mm": 2.8, "start_y_mm": 7.25,
        "end_x_mm": 6.25, "end_y_mm": 8.25,
        "width_mm": 0.5, "layer": "top",
        "net_id": "net_vcc", "net_name": "VCC"
      }
    ],
    "vias": [
      {
        "x_mm": 10.0, "y_mm": 1.25,
        "drill_mm": 0.3, "diameter_mm": 0.6,
        "from_layer": "top", "to_layer": "bottom",
        "net_id": "net_gnd", "net_name": "GND"
      }
    ],
    "unrouted_nets": ["net_sw_out"],
    "statistics": {
      "total_nets": 4,
      "routed_nets": 3,
      "completion_pct": 75.0,
      "total_trace_length_mm": 42.5,
      "via_count": 2,
      "layer_usage": {
        "top_trace_length_mm": 28.0,
        "bottom_trace_length_mm": 14.5
      }
    },
    "copper_fills": [
      {
        "layer": "top",
        "net_id": "net_gnd",
        "net_name": "GND",
        "polygons": [
          [[0.0, 0.0], [5.0, 0.0], [5.0, 3.0], [0.0, 3.0]]
        ]
      },
      {
        "layer": "bottom",
        "net_id": "net_gnd",
        "net_name": "GND",
        "polygons": [
          [[0.0, 0.0], [20.0, 0.0], [20.0, 15.0], [0.0, 15.0]]
        ]
      }
    ],
    "silkscreen": [
      {
        "type": "text",
        "text": "R1",
        "x_mm": 5.0, "y_mm": 10.0,
        "font_size_mm": 0.8,
        "layer": "top"
      },
      {
        "type": "pin1_dot",
        "x_mm": 3.0, "y_mm": 9.0,
        "radius_mm": 0.2,
        "layer": "top"
      },
      {
        "type": "anode_marker",
        "text": "A",
        "x_mm": 7.0, "y_mm": 12.0,
        "font_size_mm": 0.6,
        "layer": "top"
      }
    ],
    "config": {
      "copper_weight_oz": 0.5,
      "grid_resolution_mm": 0.25,
      "trace_clearance_mm": 0.2,
      "via_drill_mm": 0.3,
      "via_diameter_mm": 0.6
    }
  }
}
```

### Copper Fill Fields

| Field | Type | Description |
|-------|------|-------------|
| `layer` | string | `"top"` or `"bottom"` |
| `net_id` | string | Net ID (typically `"net_gnd"`) |
| `net_name` | string | Net name (typically `"GND"`) |
| `polygons` | array | List of rectangles, each `[[x1,y1], [x2,y2], [x3,y3], [x4,y4]]` in mm |

Copper fill covers unused board area on both layers. Polygons are merged rectangles from run-length encoding. Clearance is maintained around non-fill-net features. GND pads have thermal relief (cardinal spokes with gaps).

### Stitching Vias

Vias with `net_id` matching the fill net and no corresponding trace endpoints are **stitching vias** — they connect the top and bottom ground planes on a ~5mm grid. Render them the same as routing vias.

### Silkscreen Fields

| Field | Type | Description |
|-------|------|-------------|
| `type` | string | `"text"` (designator), `"pin1_dot"` (pin 1 indicator), `"anode_marker"` (LED/diode anode "A") |
| `text` | string | Label text (for `text` and `anode_marker` types) |
| `x_mm`, `y_mm` | number | Position in mm |
| `font_size_mm` | number | Text height (for `text` and `anode_marker`) |
| `radius_mm` | number | Dot radius (for `pin1_dot`) |
| `layer` | string | `"top"` or `"bottom"` |

---

## Built-in Visualizer

```bash
python visualizers/placement_viewer.py <placement.json> \
  --netlist <netlist.json> \
  --bom <bom.json> \
  --routed <routed.json> \
  --open
```

Generates an interactive HTML file with pan/zoom, hover tooltips, ratsnest lines, routed traces (including diagonal segments), vias, pad shapes, silkscreen, and component details.

---

## Tips for Custom Visualizations

- **Join data by designator**: All three files use the same `designator` values
- **Flip Y for screen coordinates**: Most rendering systems have Y increasing downward, but PCB coordinates have Y increasing upward
- **Ratsnest**: For each net, compute a minimum spanning tree of the connected component center positions using Manhattan distance
- **Color coding**: Use `component_type` for color (resistor=blue, capacitor=orange, IC=green, connector=purple, LED=red)
- **Layer opacity**: Show bottom-layer components at reduced opacity to distinguish from top layer
- **Pad shapes**: Use `pad_geometry.py` to compute per-pin positions, rendered as small rectangles on the component body
- **Diagonal traces**: Trace segments may have non-axis-aligned endpoints (45° diagonal routing). Render as SVG `<line>` elements — works with any start/end coordinates
- **Routing progress**: Header shows routing completion % with color coding: green (90%+), yellow (70-89%), red (<70%)
