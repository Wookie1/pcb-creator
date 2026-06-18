"""Export routed PCB design to KiCad .kicad_pcb format.

Generates a KiCad 8.0-compatible (version 20240108) S-expression file
that can be opened in KiCad 8.x and 9.x for manual editing.

Uses simplified inline footprints (pads only, no library references)
which is sufficient for manual routing in KiCad.
"""

from __future__ import annotations

import json
import re
import uuid
from pathlib import Path

from optimizers.pad_geometry import get_footprint_def, _generate_fallback_footprint


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_LAYER_MAP = {
    "top": "F.Cu",
    "inner1": "In1.Cu",
    "inner2": "In2.Cu",
    "bottom": "B.Cu",
    "top_silk": "F.SilkS",
    "bottom_silk": "B.SilkS",
}

# Through-hole package prefixes/names
_TH_PACKAGES = {"DIP-", "PinHeader_", "TO-220", "HC49", "PJ-002A", "6mm_tactile"}


def _is_through_hole(package: str) -> bool:
    """Check if a package is through-hole based on name."""
    for prefix in _TH_PACKAGES:
        if package.upper().startswith(prefix.upper()):
            return True
    return False


def _mounting_hole_drill_mm(package: str, component_type: str = "") -> float | None:
    """Return the drill diameter (mm) if this part is a mounting hole, else None.

    Mounting holes are NPTH (non-plated through holes) with no copper pad and no
    net. They are detected by package name ("MountingHole_3.2mm_M3") or by the
    "mounting_hole" component type. Without this, the footprint resolver can't
    parse a mounting-hole .kicad_mod (its pad is unnumbered np_thru_hole, which
    the parser skips) and falls back to an SMD placeholder pad — the recurring
    "H1-H4 are SMD pads, not drilled NPTH" bug.
    """
    pkg = (package or "").lower()
    if "mountinghole" not in pkg and "mounting_hole" not in pkg \
            and component_type != "mounting_hole":
        return None
    # Parse the hole/drill diameter from the package name, e.g.
    # "MountingHole_3.2mm_M3" -> 3.2. Fall back to 3.2mm (M3) if absent.
    m = re.search(r"(\d+(?:\.\d+)?)\s*mm", pkg)
    if m:
        return round(float(m.group(1)), 3)
    return 3.2


def _mounting_hole_footprint(plc: dict, drill_mm: float) -> str:
    """Generate an NPTH mounting-hole footprint: a single non-plated through
    hole, no copper, no net, excluded from BOM/position files."""
    des = plc["designator"]
    package = plc.get("package", "MountingHole")
    cx, cy = plc["x_mm"], plc["y_mm"]
    # A small annular ring of bare copper is conventional but a pure NPTH has
    # size == drill (no copper). Keep it pure NPTH so DRC sees no net.
    d = round(drill_mm, 3)
    pad_clear = round(d + 1.0, 3)  # courtyard around the hole
    return "\n".join([
        f'  (footprint "pcb-creator:{des}_{package}"',
        f'    (layer "F.Cu")',
        f'    (tstamp {_uid()})',
        f'    (at {cx} {cy})',
        f'    (attr exclude_from_pos_files exclude_from_bom)',
        f'    (property "Reference" "{des}"',
        f'      (at 0 {-pad_clear/2 - 1.0})',
        f'      (layer "F.SilkS")',
        f'      (effects (font (size 1 1) (thickness 0.15)))',
        f'    )',
        f'    (fp_circle (center 0 0) (end {d/2} 0)'
        f' (stroke (width 0.05) (type default)) (layer "F.CrtYd"))',
        f'    (pad "" np_thru_hole circle (at 0 0)'
        f' (size {d} {d}) (drill {d}) (layers "*.Cu" "*.Mask"))',
        f'  )',
    ])


def _uid() -> str:
    """Generate a KiCad-compatible UUID string."""
    return str(uuid.uuid4())


# ---------------------------------------------------------------------------
# S-expression building helpers
# ---------------------------------------------------------------------------

def _header(num_layers: int = 2) -> str:
    """KiCad PCB file header with layer definitions."""
    # Inner copper layers occupy indices 1..N-2 in KiCad's numbering
    inner_layers = ""
    if num_layers >= 4:
        inner_layers = '\n    (1 "In1.Cu" signal)\n    (2 "In2.Cu" signal)'
    return f"""\
(kicad_pcb
  (version 20240108)
  (generator "pcb-creator")
  (generator_version "1.0")
  (general
    (thickness 1.6)
    (legacy_teardrops no)
  )
  (paper "A4")
  (layers
    (0 "F.Cu" signal){inner_layers}
    (31 "B.Cu" signal)
    (32 "B.Adhes" user "B.Adhesive")
    (33 "F.Adhes" user "F.Adhesive")
    (34 "B.Paste" user)
    (35 "F.Paste" user)
    (36 "B.SilkS" user "B.Silkscreen")
    (37 "F.SilkS" user "F.Silkscreen")
    (38 "B.Mask" user "B.Mask")
    (39 "F.Mask" user "F.Mask")
    (40 "Dwgs.User" user "User.Drawings")
    (41 "Cmts.User" user "User.Comments")
    (42 "Eco1.User" user "User.Eco1")
    (43 "Eco2.User" user "User.Eco2")
    (44 "Edge.Cuts" user)
    (45 "Margin" user)
    (46 "B.CrtYd" user "B.Courtyard")
    (47 "F.CrtYd" user "F.Courtyard")
    (48 "B.Fab" user "B.Fabrication")
    (49 "F.Fab" user "F.Fabrication")
  )
"""


def _setup(config: dict) -> str:
    """Generate setup section with design rules."""
    clearance = config.get("trace_clearance_mm", 0.2)
    via_drill = config.get("via_drill_mm", 0.3)
    via_dia = config.get("via_diameter_mm", 0.6)
    trace_min = 0.15  # reasonable minimum

    return f"""\
  (setup
    (pad_to_mask_clearance 0.05)
    (pcbplotparams
      (layerselection 0x00010fc_ffffffff)
      (plot_on_all_layers_selection 0x0000000_00000000)
      (disableapertmacros no)
      (usegerberextensions no)
      (usegerberattributes yes)
      (usegerberadvancedattributes yes)
      (creategerberjobfile yes)
      (dashed_line_dash_ratio 12.000000)
      (dashed_line_gap_ratio 3.000000)
      (svgprecision 4)
      (plotframeref no)
      (viasonmask no)
      (mode 1)
      (useauxorigin no)
      (hpglpennumber 1)
      (hpglpenspeed 20)
      (hpglpendiameter 15.000000)
      (pdf_front_fp_property_popups yes)
      (pdf_back_fp_property_popups yes)
      (dxf_imperial_units yes)
      (dxf_use_pcbnew_font yes)
      (psnegative no)
      (psa4output no)
      (plotreference yes)
      (plotvalue yes)
      (plotfptext yes)
      (plotinvisibletext no)
      (sketchpadsonfab no)
      (subtractmaskfromsilk no)
      (outputformat 1)
      (mirror no)
      (drillshape 1)
      (scaleselection 1)
      (outputdirectory "")
    )
  )
"""


def _net_declarations(nets: list[dict]) -> str:
    """Generate net declaration section."""
    lines = ['  (net 0 "")']
    for net in nets:
        lines.append(f'  (net {net["num"]} "{net["name"]}")')
    return "\n".join(lines) + "\n"


def _board_outline(board: dict) -> str:
    """Generate board outline as gr_line segments on Edge.Cuts."""
    w = board.get("width_mm", 50.0)
    h = board.get("height_mm", 50.0)
    uid = _uid

    return f"""\
  (gr_line (start 0 0) (end {w} 0) (stroke (width 0.05) (type default)) (layer "Edge.Cuts") (tstamp {uid()}))
  (gr_line (start {w} 0) (end {w} {h}) (stroke (width 0.05) (type default)) (layer "Edge.Cuts") (tstamp {uid()}))
  (gr_line (start {w} {h}) (end 0 {h}) (stroke (width 0.05) (type default)) (layer "Edge.Cuts") (tstamp {uid()}))
  (gr_line (start 0 {h}) (end 0 0) (stroke (width 0.05) (type default)) (layer "Edge.Cuts") (tstamp {uid()}))
"""


def _footprint(
    plc: dict,
    port_net_map: dict[str, tuple[int, str]],
    comp_ports: dict[str, list[dict]],
    components: dict[str, dict],
) -> str:
    """Generate a footprint block for one placed component."""
    des = plc["designator"]
    package = plc.get("package", "0805")
    cx, cy = plc["x_mm"], plc["y_mm"]
    rot = plc.get("rotation_deg", 0)
    layer = _LAYER_MAP.get(plc.get("layer", "top"), "F.Cu")
    fw = plc.get("footprint_width_mm", 2.0)
    fh = plc.get("footprint_height_mm", 1.0)

    # Mounting holes export as NPTH (no copper pad, no net) — never as the
    # SMD-placeholder footprint the resolver falls back to.
    mh_drill = _mounting_hole_drill_mm(package, plc.get("component_type", ""))
    if mh_drill is not None:
        return _mounting_hole_footprint(plc, mh_drill)

    # Find component info to get pin count
    comp_id = None
    for cid, comp in components.items():
        if comp.get("designator") == des:
            comp_id = cid
            break

    ports = comp_ports.get(comp_id, []) if comp_id else []
    pin_count = len(ports)

    # Get footprint definition
    fp_def = get_footprint_def(package, pin_count)
    if fp_def is None:
        fp_def = _generate_fallback_footprint(fw, fh, pin_count)

    is_th = _is_through_hole(package)

    lines = [
        f'  (footprint "pcb-creator:{des}_{package}"',
        f'    (layer "{layer}")',
        f'    (tstamp {_uid()})',
        f'    (at {cx} {cy} {rot})',
        f'    (property "Reference" "{des}"',
        f'      (at 0 {-fh/2 - 1.0})',
        f'      (layer "{layer.replace("Cu", "SilkS")}")',
        f'      (effects (font (size 1 1) (thickness 0.15)))',
        f'    )',
        f'    (property "Value" "{package}"',
        f'      (at 0 {fh/2 + 1.0})',
        f'      (layer "{layer.replace("Cu", "Fab")}")',
        f'      (effects (font (size 1 1) (thickness 0.15)))',
        f'    )',
    ]

    # Courtyard
    hw, hh = fw / 2 + 0.25, fh / 2 + 0.25
    lines.append(
        f'    (fp_rect (start {-hw} {-hh}) (end {hw} {hh})'
        f' (stroke (width 0.05) (type default)) (layer "{layer.replace("Cu", "CrtYd")}"))'
    )

    # Fab outline
    hw2, hh2 = fw / 2, fh / 2
    lines.append(
        f'    (fp_rect (start {-hw2} {-hh2}) (end {hw2} {hh2})'
        f' (stroke (width 0.1) (type default)) (layer "{layer.replace("Cu", "Fab")}"))'
    )

    # Generate pads
    # Build pin_number -> port_id mapping
    pin_port_map: dict[int, str] = {}
    for port in ports:
        pin_port_map[port.get("pin_number", 0)] = port.get("port_id", "")

    pad_w, pad_h = fp_def.pad_size

    for pin_num, (dx, dy) in sorted(fp_def.pin_offsets.items()):
        # Round offsets to avoid floating point noise (e.g., 3.8099999999999987)
        # which causes KiCad to compute pad positions that don't match trace endpoints
        # Back-side footprints store X-mirrored local offsets (KiCad bakes the
        # flip into the file) — must match build_pad_map's mirror convention.
        if layer == "B.Cu":
            dx = -dx
        dx = round(dx, 4)
        dy = round(dy, 4)

        port_id = pin_port_map.get(pin_num, "")
        net_num, net_name = port_net_map.get(port_id, (0, ""))

        if is_th:
            # Through-hole pad
            # Drill = pin diameter (smaller dimension) + 0.2mm tolerance
            pin_dia = min(pad_w, pad_h)
            drill = pin_dia + 0.2
            drill = max(0.6, round(drill, 2))  # minimum 0.6mm drill
            pad_dia = max(pad_w, pad_h)
            shape = "circle" if pin_num > 1 else "rect"  # pin 1 = square for identification
            lines.append(
                f'    (pad "{pin_num}" thru_hole {shape}'
                f' (at {dx} {dy})'
                f' (size {pad_dia} {pad_dia})'
                f' (drill {drill})'
                f' (layers "*.Cu" "*.Mask")'
                f' (net {net_num} "{net_name}")'
                f' (zone_connect 1)'
                f' (tstamp {_uid()}))'
            )
        else:
            # SMD pad
            mask_layer = "F" if layer.startswith("F") else "B"
            is_fiducial = plc.get("component_type") == "fiducial"
            if is_fiducial:
                # Fiducial: 1mm circular copper dot, no paste, enlarged
                # solder mask opening (2mm clearance around the dot)
                pad_dia = max(pad_w, pad_h)
                mask_expansion = 1.0  # 1mm on each side → 3mm mask opening
                lines.append(
                    f'    (pad "{pin_num}" smd circle'
                    f' (at {dx} {dy})'
                    f' (size {pad_dia} {pad_dia})'
                    f' (layers "{layer}" "{mask_layer}.Mask")'
                    f' (solder_mask_margin {mask_expansion})'
                    f' (tstamp {_uid()}))'
                )
            else:
                lines.append(
                    f'    (pad "{pin_num}" smd rect'
                    f' (at {dx} {dy})'
                    f' (size {pad_w} {pad_h})'
                    f' (layers "{layer}" "{mask_layer}.Paste" "{mask_layer}.Mask")'
                    f' (net {net_num} "{net_name}")'
                    f' (zone_connect 1)'
                    f' (tstamp {_uid()}))'
                )

    lines.append("  )")
    return "\n".join(lines)


def _traces(traces: list[dict], net_num_map: dict[str, int]) -> str:
    """Generate trace segments."""
    lines = []
    for t in traces:
        layer = _LAYER_MAP.get(t["layer"], "F.Cu")
        net_num = net_num_map.get(t.get("net_id", ""), 0)
        sx, sy = t["start_x_mm"], t["start_y_mm"]
        ex, ey = t["end_x_mm"], t["end_y_mm"]
        w = t["width_mm"]
        lines.append(
            f'  (segment (start {sx} {sy}) (end {ex} {ey})'
            f' (width {w}) (layer "{layer}")'
            f' (net {net_num}) (tstamp {_uid()}))'
        )
    return "\n".join(lines)


def _vias(vias: list[dict], net_num_map: dict[str, int], num_layers: int = 2) -> str:
    """Generate via definitions."""
    # Through-vias always span the full stack (F.Cu to B.Cu)
    via_layers = '"F.Cu" "B.Cu"'
    lines = []
    for v in vias:
        net_num = net_num_map.get(v.get("net_id", ""), 0)
        lines.append(
            f'  (via (at {v["x_mm"]} {v["y_mm"]})'
            f' (size {v["diameter_mm"]}) (drill {v["drill_mm"]})'
            f' (layers {via_layers})'
            f' (net {net_num}) (tstamp {_uid()}))'
        )
    return "\n".join(lines)


def _copper_fills(
    fills: list[dict],
    net_num_map: dict[str, int],
    board: dict,
) -> str:
    """Generate zone definitions for copper fills.

    Uses modern KiCad zone format: defines a board-sized outline with fill rules
    and lets KiCad compute the fill. One zone per layer per net. This avoids the
    legacy filled_polygon approach that KiCad 9 warns about.
    """
    # Group fills by (layer, net) to avoid duplicate zones
    seen: set[tuple[str, str]] = set()
    lines = []

    board_w = board.get("width_mm", 50.0)
    board_h = board.get("height_mm", 50.0)
    # Zone outline: inset from board edge to match the router's edge clearance
    # and satisfy standard manufacturing copper-to-edge requirements (≥0.25 mm).
    margin = 0.3
    x0, y0 = margin, margin
    x1, y1 = board_w - margin, board_h - margin

    for fill in fills:
        layer = _LAYER_MAP.get(fill["layer"], "F.Cu")
        net_id = fill.get("net_id", "")
        net_num = net_num_map.get(net_id, 0)
        net_name = fill.get("net_name", "")

        key = (layer, net_id)
        if key in seen:
            continue
        seen.add(key)

        zone_uid = _uid()
        lines.append(
            f'  (zone (net {net_num}) (net_name "{net_name}")'
            f' (layer "{layer}") (tstamp {zone_uid})'
        )
        lines.append(f'    (hatch edge 0.5)')
        lines.append(f'    (connect_pads (clearance 0.2))')
        lines.append(f'    (min_thickness 0.15)')
        lines.append(
            f'    (fill yes (thermal_gap 0.25) (thermal_bridge_width 0.25))'
        )
        # Zone outline covers the full board
        lines.append(f'    (polygon (pts')
        lines.append(f'      (xy {x0} {y0}) (xy {x1} {y0})')
        lines.append(f'      (xy {x1} {y1}) (xy {x0} {y1})')
        lines.append(f'    ))')
        lines.append("  )")

    return "\n".join(lines)


def _silkscreen(silk_items: list[dict]) -> str:
    """Generate silkscreen elements."""
    lines = []
    for item in silk_items:
        layer = _LAYER_MAP.get(item.get("layer", "top_silk"), "F.SilkS")

        if item.get("type") == "text":
            text = item.get("text", "")
            x, y = item.get("x_mm", 0), item.get("y_mm", 0)
            font_h = item.get("font_height_mm", 1.0)
            lines.append(
                f'  (gr_text "{text}" (at {x} {y})'
                f' (layer "{layer}")'
                f' (effects (font (size {font_h} {font_h}) (thickness {font_h * 0.15})))'
                f' (tstamp {_uid()}))'
            )
        elif item.get("type") == "dot":
            x, y = item.get("x_mm", 0), item.get("y_mm", 0)
            r = item.get("diameter_mm", 0.5) / 2
            lines.append(
                f'  (gr_circle (center {x} {y}) (end {x + r} {y})'
                f' (stroke (width 0) (type default))'
                f' (fill solid) (layer "{layer}")'
                f' (tstamp {_uid()}))'
            )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def _net_class(name: str, clearance: float, track_w: float,
               via_dia: float, via_drill: float) -> dict:
    """One KiCad net-class entry (KiCad 9 .kicad_pro schema)."""
    return {
        "name": name,
        "clearance": round(clearance, 4),
        "track_width": round(track_w, 4),
        "via_diameter": round(via_dia, 4),
        "via_drill": round(via_drill, 4),
        "diff_pair_width": 0.2,
        "diff_pair_gap": 0.25,
        "diff_pair_via_gap": 0.25,
        "microvia_diameter": 0.3,
        "microvia_drill": 0.1,
        "bus_width": 12,
        "wire_width": 6,
        "priority": 2147483647,
        "line_style": 0,
        "pcb_color": "rgba(0, 0, 0, 0.000)",
        "schematic_color": "rgba(0, 0, 0, 0.000)",
    }


def build_kicad_pro(routed: dict, project_name: str) -> dict:
    """Build a .kicad_pro project dict whose design rules MATCH how the board
    was actually routed.

    KiCad 9's ``kicad-cli pcb drc`` reads net-class clearance / track width and
    the board minimum-clearance rule from the sibling .kicad_pro — NOT from the
    .kicad_pcb. Without one it falls back to its 0.2mm defaults, so a fine-pitch
    board routed at 0.127mm gets a false clearance violation on essentially every
    trace. Emitting the real rules here is what lets DRC pass a board that is in
    fact correct.
    """
    cfg = routed.get("routing", {}).get("config", {})
    clearance = float(cfg.get("trace_clearance_mm", 0.2))
    track_w = float(cfg.get("trace_width_signal_mm", 0.2))
    via_dia = float(cfg.get("via_diameter_mm", 0.6))
    via_drill = float(cfg.get("via_drill_mm", 0.3))

    return {
        "meta": {"filename": f"{project_name}.kicad_pro", "version": 3},
        "board": {
            "design_settings": {
                "defaults": {},
                "drc_exclusions": [],
                "rule_severities": {},
                "rules": {
                    "min_clearance": round(clearance, 4),
                    "min_track_width": round(track_w, 4),
                    "min_via_diameter": round(via_dia, 4),
                    "min_through_hole_diameter": round(via_drill, 4),
                    "min_hole_clearance": 0.2,
                    "min_hole_to_hole": 0.25,
                },
                "meta": {"version": 2},
            }
        },
        "net_settings": {
            "meta": {"version": 3},
            "net_colors": None,
            "netclass_assignments": None,
            "netclass_patterns": [],
            "classes": [
                _net_class("Default", clearance, track_w, via_dia, via_drill),
            ],
        },
        "schematic": {},
        "sheets": [],
        "cvpcb": {},
        "libraries": {"pinned_footprint_libs": [], "pinned_symbol_libs": []},
        "pcbnew": {"last_paths": {}, "page_layout_descr_file": ""},
        "text_variables": {},
    }


def export_kicad_pro(routed: dict, output_path: str | Path) -> Path:
    """Write a .kicad_pro next to the .kicad_pcb so DRC honors the routed rules."""
    output_path = Path(output_path)
    project_name = output_path.stem
    output_path.write_text(
        json.dumps(build_kicad_pro(routed, project_name), indent=2),
        encoding="utf-8",
    )
    return output_path


def export_kicad_pcb(
    routed: dict,
    netlist: dict,
    output_path: str | Path,
) -> Path:
    """Export a routed PCB design to KiCad .kicad_pcb format.

    Args:
        routed: The routed JSON dict (from route_board or loaded from file).
        netlist: The netlist JSON dict.
        output_path: Where to write the .kicad_pcb file.

    Returns:
        Path to the written file.
    """
    output_path = Path(output_path)
    elements = netlist.get("elements", [])

    # Build net list with sequential numbering
    net_elements = [e for e in elements if e.get("element_type") == "net"]
    net_list = []
    net_num_map: dict[str, int] = {}  # net_id -> sequential number
    for i, net in enumerate(net_elements, start=1):
        net_id = net["net_id"]
        net_name = net.get("name", net_id)
        net_list.append({"num": i, "name": net_name, "net_id": net_id})
        net_num_map[net_id] = i

    # Build port -> net mapping for pad net assignment
    port_net: dict[str, str] = {}  # port_id -> net_id
    for net in net_elements:
        for pid in net.get("connected_port_ids", []):
            port_net[pid] = net["net_id"]

    # port_id -> (net_number, net_name)
    port_net_map: dict[str, tuple[int, str]] = {}
    for pid, nid in port_net.items():
        num = net_num_map.get(nid, 0)
        name = next(
            (n["name"] for n in net_list if n["net_id"] == nid), ""
        )
        port_net_map[pid] = (num, name)

    # Build component/port lookups
    comp_ports: dict[str, list[dict]] = {}
    components: dict[str, dict] = {}
    for elem in elements:
        if elem.get("element_type") == "port":
            cid = elem.get("component_id", "")
            comp_ports.setdefault(cid, []).append(elem)
        elif elem.get("element_type") == "component":
            components[elem["component_id"]] = elem

    num_layers = routed.get("board", {}).get("layers", 2)

    # Assemble the file
    parts = [_header(num_layers)]
    parts.append(_setup(routed.get("routing", {}).get("config", {})))
    parts.append(_net_declarations(net_list))
    parts.append(_board_outline(routed.get("board", {})))

    # Footprints
    for plc in routed.get("placements", []):
        parts.append(
            _footprint(plc, port_net_map, comp_ports, components)
        )

    # Routing
    routing = routed.get("routing", {})
    if routing.get("traces"):
        parts.append(_traces(routing["traces"], net_num_map))
    if routing.get("vias"):
        parts.append(_vias(routing["vias"], net_num_map, num_layers))
    if routing.get("copper_fills"):
        parts.append(_copper_fills(routing["copper_fills"], net_num_map, routed.get("board", {})))

    # Silkscreen
    if routed.get("silkscreen"):
        parts.append(_silkscreen(routed["silkscreen"]))

    # Close the top-level sexp
    parts.append(")")

    content = "\n".join(parts) + "\n"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(content, encoding="utf-8")

    # Emit a sibling .kicad_pro so kicad-cli DRC honors the routed design rules
    # (clearance/track width) instead of its 0.2mm defaults.
    export_kicad_pro(routed, output_path.with_suffix(".kicad_pro"))

    return output_path
