"""Export routed PCB to Gerber RS-274X and Excellon drill files.

Generates manufacturer-ready files using the gerber-writer library for
Gerber layers and direct text generation for Excellon drill format.

Output layers:
- F_Cu / B_Cu — copper layers (traces, pads, vias, fills)
- F_SilkS / B_SilkS — silkscreen
- F_Mask / B_Mask — solder mask (negative: openings at pads)
- F_Paste — solder paste stencil (SMD pads only)
- Edge_Cuts — board outline
"""

from __future__ import annotations

import math
import zipfile
from pathlib import Path

import gerber_writer as gw

from optimizers.pad_geometry import build_pad_map, get_footprint_def, _generate_fallback_footprint


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_TH_PACKAGES = ("DIP", "PinHeader", "PJ-002A", "TO-220", "HC49", "6mm_tactile")

MASK_EXPANSION_MM = 0.05  # solder mask opening larger than pad on each side

LAYER_FUNCTIONS = {
    "F_Cu": "Copper,L1,Top",
    "B_Cu": "Copper,L2,Bot",
    "F_SilkS": "Legend,Top",
    "B_SilkS": "Legend,Bot",
    "F_Mask": "Soldermask,Top",
    "B_Mask": "Soldermask,Bot",
    "F_Paste": "Paste,Top",
    "Edge_Cuts": "Profile",
}

# Map internal layer names to Gerber layer keys
_COPPER_MAP = {"top": "F_Cu", "bottom": "B_Cu"}
_SILK_MAP = {"top_silk": "F_SilkS", "bottom_silk": "B_SilkS"}
_MASK_MAP = {"top": "F_Mask", "bottom": "B_Mask"}


def _is_through_hole(package: str) -> bool:
    for prefix in _TH_PACKAGES:
        if package.upper().startswith(prefix.upper()):
            return True
    return False


# ---------------------------------------------------------------------------
# Board outline helpers
# ---------------------------------------------------------------------------

def _get_board_vertices(board: dict) -> list[tuple[float, float]]:
    """Get board outline vertices. Supports rectangle and arbitrary polygon."""
    vertices = board.get("outline_vertices")
    if vertices:
        return [(v[0], v[1]) for v in vertices]
    # Default: rectangle from dimensions
    w = board.get("width_mm", 50.0)
    h = board.get("height_mm", 50.0)
    return [(0, 0), (w, 0), (w, h), (0, h)]


# ---------------------------------------------------------------------------
# Gerber layer generators
# ---------------------------------------------------------------------------

def _generate_copper_layer(
    routed: dict,
    netlist: dict,
    layer: str,
    pad_map: dict,
) -> gw.DataLayer:
    """Generate copper Gerber for one layer (top or bottom)."""
    gerber_key = _COPPER_MAP[layer]
    dl = gw.DataLayer(LAYER_FUNCTIONS[gerber_key], negative=False)

    routing = routed.get("routing", {})

    # Traces on this layer
    for trace in routing.get("traces", []):
        if trace.get("layer") != layer:
            continue
        dl.add_trace_line(
            (trace["start_x_mm"], trace["start_y_mm"]),
            (trace["end_x_mm"], trace["end_y_mm"]),
            trace.get("width_mm", 0.25),
            "Conductor",
        )

    # Via pads (vias appear on both layers)
    for via in routing.get("vias", []):
        dia = via.get("diameter_mm", 0.6)
        pad = gw.Circle(dia, "ViaPad")
        dl.add_pad(pad, (via["x_mm"], via["y_mm"]))

    # Component pads on this layer
    for pad_info in pad_map.values():
        if pad_info.layer == layer or pad_info.layer == "all":
            pw, ph = pad_info.pad_width_mm, pad_info.pad_height_mm
            if pad_info.layer == "all":
                # Through-hole: circular pad
                dia = max(pw, ph)
                ap = gw.Circle(dia, "ComponentPad")
            else:
                # SMD: rectangular pad
                ap = gw.Rectangle(pw, ph, "SMDPad,CuDef")
            dl.add_pad(ap, (pad_info.x_mm, pad_info.y_mm))

    # Fiducial pads (not in pad_map since they have no netlist ports)
    for plc in routed.get("placements", []):
        if plc.get("component_type") != "fiducial":
            continue
        if plc.get("layer", "top") != layer:
            continue
        # Fiducial: 1mm copper dot
        fid_dia = 1.0
        ap = gw.Circle(fid_dia, "FiducialPad")
        dl.add_pad(ap, (plc["x_mm"], plc["y_mm"]))

    # Copper fill polygons on this layer
    for fill_region in routing.get("copper_fills", []):
        if fill_region.get("layer") != layer:
            continue
        for polygon in fill_region.get("polygons", []):
            if len(polygon) < 3:
                continue
            path = gw.Path()
            path.moveto((polygon[0][0], polygon[0][1]))
            for pt in polygon[1:]:
                path.lineto((pt[0], pt[1]))
            path.lineto((polygon[0][0], polygon[0][1]))  # close
            dl.add_region(path, "Conductor")

    return dl


def _render_text_strokes(
    dl: gw.DataLayer,
    text: str,
    x: float,
    y: float,
    height: float,
    stroke_w: float,
    anchor: str = "center",
) -> None:
    """Render text as stroke font line segments in a Gerber DataLayer."""
    from .stroke_font import STROKE_FONT

    char_width = height * 0.6
    spacing = height * 0.15
    total_width = len(text) * char_width + max(0, len(text) - 1) * spacing

    # Compute start X based on anchor
    if anchor == "center":
        start_x = x - total_width / 2
    elif anchor == "right":
        start_x = x - total_width
    else:
        start_x = x

    cursor_x = start_x
    for ch in text.upper():
        strokes = STROKE_FONT.get(ch, STROKE_FONT.get(".", []))
        for (x1, y1), (x2, y2) in strokes:
            dl.add_trace_line(
                (cursor_x + x1 * height, y + y1 * height),
                (cursor_x + x2 * height, y + y2 * height),
                stroke_w,
                "Other",
            )
        cursor_x += char_width + spacing


def _generate_silkscreen(
    routed: dict,
    layer: str,
) -> gw.DataLayer:
    """Generate silkscreen Gerber for one layer with stroke font text."""
    silk_layer = f"{layer}_silk"
    gerber_key = _SILK_MAP[silk_layer]
    dl = gw.DataLayer(LAYER_FUNCTIONS[gerber_key], negative=False)

    for silk in routed.get("silkscreen", []):
        if silk.get("layer") != silk_layer:
            continue

        if silk["type"] == "text":
            x = silk.get("x_mm", 0)
            y = silk.get("y_mm", 0)
            fh = silk.get("font_height_mm", 1.0)
            text = silk.get("text", "")
            stroke_w = max(fh * 0.15, 0.1)
            anchor = silk.get("anchor", "center")
            _render_text_strokes(dl, text, x, y, fh, stroke_w, anchor)

        elif silk["type"] == "dot":
            dia = silk.get("diameter_mm", 0.5)
            ap = gw.Circle(dia, "Other")
            dl.add_pad(ap, (silk.get("x_mm", 0), silk.get("y_mm", 0)))

    return dl


def _generate_solder_mask(
    routed: dict,
    netlist: dict,
    layer: str,
    pad_map: dict,
) -> gw.DataLayer:
    """Generate solder mask Gerber (negative: openings at pads/vias)."""
    gerber_key = _MASK_MAP[layer]
    dl = gw.DataLayer(LAYER_FUNCTIONS[gerber_key], negative=True)

    # Pad openings
    for pad_info in pad_map.values():
        if pad_info.layer == layer or pad_info.layer == "all":
            pw = pad_info.pad_width_mm + 2 * MASK_EXPANSION_MM
            ph = pad_info.pad_height_mm + 2 * MASK_EXPANSION_MM
            if pad_info.layer == "all":
                dia = max(pw, ph)
                ap = gw.Circle(dia, "ComponentPad")
            else:
                ap = gw.Rectangle(pw, ph, "SMDPad,CuDef")
            dl.add_pad(ap, (pad_info.x_mm, pad_info.y_mm))

    # Fiducial mask openings (large opening for vision system)
    for plc in routed.get("placements", []):
        if plc.get("component_type") != "fiducial":
            continue
        if plc.get("layer", "top") != layer:
            continue
        # 3mm mask opening (1mm pad + 1mm clearance each side)
        mask_dia = 3.0
        ap = gw.Circle(mask_dia, "FiducialPad")
        dl.add_pad(ap, (plc["x_mm"], plc["y_mm"]))

    # Via openings (expose vias for better conductivity)
    for via in routed.get("routing", {}).get("vias", []):
        dia = via.get("diameter_mm", 0.6) + 2 * MASK_EXPANSION_MM
        ap = gw.Circle(dia, "ViaPad")
        dl.add_pad(ap, (via["x_mm"], via["y_mm"]))

    return dl


def _generate_paste(
    routed: dict,
    layer: str,
    pad_map: dict,
) -> gw.DataLayer:
    """Generate solder paste Gerber (SMD pads only)."""
    dl = gw.DataLayer(LAYER_FUNCTIONS["F_Paste"], negative=False)

    for pad_info in pad_map.values():
        # Only SMD pads on the specified layer
        if pad_info.layer != layer or pad_info.layer == "all":
            continue
        pw, ph = pad_info.pad_width_mm, pad_info.pad_height_mm
        ap = gw.Rectangle(pw, ph, "SMDPad,CuDef")
        dl.add_pad(ap, (pad_info.x_mm, pad_info.y_mm))

    return dl


def _generate_edge_cuts(board: dict) -> gw.DataLayer:
    """Generate board outline Gerber."""
    dl = gw.DataLayer(LAYER_FUNCTIONS["Edge_Cuts"], negative=False)

    vertices = _get_board_vertices(board)
    n = len(vertices)
    for i in range(n):
        x1, y1 = vertices[i]
        x2, y2 = vertices[(i + 1) % n]
        dl.add_trace_line((x1, y1), (x2, y2), 0.05, "Profile")

    return dl


# ---------------------------------------------------------------------------
# Public API — Gerber export
# ---------------------------------------------------------------------------

def export_gerbers(
    routed: dict,
    netlist: dict,
    output_dir: Path,
) -> list[Path]:
    """Export all Gerber layers for a routed PCB design.

    Returns list of generated file paths.
    """
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    project = routed.get("project_name", "board")
    pad_map = build_pad_map(routed, netlist)

    gw.set_generation_software("Productizr", "pcb-creator", "1.0")

    generated: list[Path] = []

    # Copper layers
    for layer in ("top", "bottom"):
        dl = _generate_copper_layer(routed, netlist, layer, pad_map)
        gerber_key = _COPPER_MAP[layer]
        path = output_dir / f"{project}-{gerber_key}.gbr"
        with open(path, "w") as f:
            f.write(dl.dumps_gerber())
        generated.append(path)

    # Silkscreen
    for layer in ("top", "bottom"):
        dl = _generate_silkscreen(routed, layer)
        gerber_key = _SILK_MAP[f"{layer}_silk"]
        path = output_dir / f"{project}-{gerber_key}.gbr"
        with open(path, "w") as f:
            f.write(dl.dumps_gerber())
        generated.append(path)

    # Solder mask
    for layer in ("top", "bottom"):
        dl = _generate_solder_mask(routed, netlist, layer, pad_map)
        gerber_key = _MASK_MAP[layer]
        path = output_dir / f"{project}-{gerber_key}.gbr"
        with open(path, "w") as f:
            f.write(dl.dumps_gerber())
        generated.append(path)

    # Solder paste (front only typically)
    dl = _generate_paste(routed, "top", pad_map)
    path = output_dir / f"{project}-F_Paste.gbr"
    with open(path, "w") as f:
        f.write(dl.dumps_gerber())
    generated.append(path)

    # Edge cuts
    dl = _generate_edge_cuts(routed.get("board", {}))
    path = output_dir / f"{project}-Edge_Cuts.gbr"
    with open(path, "w") as f:
        f.write(dl.dumps_gerber())
    generated.append(path)

    return generated


# ---------------------------------------------------------------------------
# Excellon drill file
# ---------------------------------------------------------------------------

def export_drill(
    routed: dict,
    netlist: dict,
    output_path: Path,
) -> Path:
    """Export Excellon drill file for all through-holes and vias.

    Returns path to the generated file.
    """
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    # Collect all drill holes: (x_mm, y_mm, drill_mm)
    holes: list[tuple[float, float, float]] = []

    # Vias
    for via in routed.get("routing", {}).get("vias", []):
        holes.append((via["x_mm"], via["y_mm"], via.get("drill_mm", 0.3)))

    # Through-hole pads
    pad_map = build_pad_map(routed, netlist)
    for pad in pad_map.values():
        if pad.layer == "all":  # through-hole
            drill = max(0.6, round(min(pad.pad_width_mm, pad.pad_height_mm) + 0.2, 2))
            holes.append((pad.x_mm, pad.y_mm, drill))

    if not holes:
        # Write empty drill file
        output_path.write_text("M48\nMETRIC,TZ\n%\nM30\n")
        return output_path

    # Group by drill size
    by_drill: dict[float, list[tuple[float, float]]] = {}
    for x, y, d in holes:
        d_rounded = round(d, 3)
        by_drill.setdefault(d_rounded, []).append((x, y))

    # Assign tool numbers
    tools = sorted(by_drill.keys())

    lines = ["M48", "METRIC,TZ"]
    for i, drill in enumerate(tools, start=1):
        lines.append(f"T{i}C{drill:.3f}")
    lines.append("%")

    # Drill hits per tool
    for i, drill in enumerate(tools, start=1):
        lines.append(f"T{i}")
        for x, y in by_drill[drill]:
            # Excellon uses integer format: multiply by 1000 for 3 decimal places
            xi = int(round(x * 1000))
            yi = int(round(y * 1000))
            lines.append(f"X{xi}Y{yi}")

    lines.append("M30")

    output_path.write_text("\n".join(lines) + "\n", encoding="ascii")
    return output_path


# ---------------------------------------------------------------------------
# Zip package
# ---------------------------------------------------------------------------

def create_output_package(
    output_dir: Path,
    project_name: str,
) -> Path:
    """Zip all Gerber and drill files into a manufacturer upload package."""
    output_dir = Path(output_dir)
    zip_path = output_dir / f"{project_name}_gerbers.zip"

    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for f in output_dir.iterdir():
            if f.suffix in (".gbr", ".drl") and f.name != zip_path.name:
                zf.write(f, f.name)

    return zip_path
