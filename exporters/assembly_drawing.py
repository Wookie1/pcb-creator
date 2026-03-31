"""Assembly drawing PDF generator for PCB manufacturing.

Produces a print-friendly PDF with:
- Board outline
- Component courtyard rectangles with designator labels
- Pin 1 / polarity indicators
- Board dimensions
- Title block
- BOM table
"""

from __future__ import annotations

import datetime
from pathlib import Path

from .gerber_exporter import _get_board_vertices


# Component type colors (muted palette for print)
_TYPE_COLORS = {
    "resistor": "#6699cc",
    "capacitor": "#e8a838",
    "inductor": "#8db6cd",
    "led": "#cc4444",
    "diode": "#cc4444",
    "transistor_npn": "#559955",
    "transistor_pnp": "#559955",
    "transistor_nmos": "#559955",
    "transistor_pmos": "#559955",
    "ic": "#558855",
    "voltage_regulator": "#558855",
    "connector": "#8866bb",
    "switch": "#449988",
    "crystal": "#cc5599",
    "fuse": "#cc7733",
    "relay": "#996633",
}

_POLARITY_TYPES = {"led", "diode", "transistor_npn", "transistor_pnp",
                   "transistor_nmos", "transistor_pmos"}
_IC_TYPES = {"ic", "voltage_regulator"}


def export_assembly_drawing(
    routed: dict,
    netlist: dict | None,
    bom: dict | None,
    output_path: str | Path,
    project_name: str = "",
) -> Path:
    """Generate an assembly drawing PDF.

    Args:
        routed: Routed PCB dict (contains placements, board, silkscreen).
        netlist: Netlist dict (for component info).
        bom: BOM dict (for value/package info).
        output_path: Path for the output PDF file.
        project_name: Project name for the title block.

    Returns:
        Path to the generated PDF file.
    """
    import cairosvg

    output_path = Path(output_path)

    board = routed.get("board", {})
    board_w = board.get("width_mm", 50)
    board_h = board.get("height_mm", 30)
    items = routed.get("placements", [])

    # Separate by layer
    top_items = [i for i in items if i.get("layer", "top") == "top"
                 and i.get("component_type") != "fiducial"]
    bottom_items = [i for i in items if i.get("layer") == "bottom"
                    and i.get("component_type") != "fiducial"]

    # Build BOM lookup
    bom_lookup: dict[str, dict] = {}
    if bom:
        for item in bom.get("bom", bom.get("bom_items", [])):
            des = item.get("designator", "")
            if des:
                bom_lookup[des] = item

    pages: list[str] = []

    # Always generate top page
    pages.append(_generate_page(
        board, board_w, board_h, top_items, bom_lookup,
        project_name, "Top", routed,
    ))

    # Generate bottom page only if there are bottom components
    if bottom_items:
        pages.append(_generate_page(
            board, board_w, board_h, bottom_items, bom_lookup,
            project_name, "Bottom", routed,
        ))

    # For multi-page PDF, we generate each page as a separate PDF
    # and concatenate (cairosvg generates single-page PDFs)
    if len(pages) == 1:
        cairosvg.svg2pdf(bytestring=pages[0].encode("utf-8"),
                         write_to=str(output_path))
    else:
        # Generate individual page PDFs then concatenate
        page_paths = []
        for i, svg in enumerate(pages):
            p = output_path.with_suffix(f".page{i}.pdf")
            cairosvg.svg2pdf(bytestring=svg.encode("utf-8"), write_to=str(p))
            page_paths.append(p)

        _concatenate_pdfs(page_paths, output_path)

        # Clean up temp files
        for p in page_paths:
            p.unlink(missing_ok=True)

    return output_path


def _generate_page(
    board: dict,
    board_w: float,
    board_h: float,
    items: list[dict],
    bom_lookup: dict[str, dict],
    project_name: str,
    side: str,
    routed: dict,
) -> str:
    """Generate SVG for one page of the assembly drawing."""
    scale = 8  # px per mm (slightly smaller than viewer for page layout)
    margin = 40
    title_height = 60
    bom_row_height = 14
    bom_header_height = 20

    # BOM table for this side's components
    bom_rows = _build_bom_table(items, bom_lookup)
    bom_height = bom_header_height + len(bom_rows) * bom_row_height + 20

    board_area_w = board_w * scale
    board_area_h = board_h * scale
    dim_annotation_h = 30  # space for dimension arrows

    svg_w = max(board_area_w + margin * 2, 500)
    svg_h = board_area_h + margin * 2 + title_height + dim_annotation_h + bom_height

    # Center board horizontally
    board_x = (svg_w - board_area_w) / 2
    board_y = margin + title_height

    parts: list[str] = []

    # White background
    parts.append(f'<rect width="{svg_w}" height="{svg_h}" fill="white"/>')

    # Title block
    parts.append(
        f'<text x="{svg_w / 2}" y="25" text-anchor="middle" '
        f'font-size="16" font-family="sans-serif" font-weight="bold" fill="#222">'
        f'Assembly Drawing — {side} Side</text>'
    )
    parts.append(
        f'<text x="{svg_w / 2}" y="42" text-anchor="middle" '
        f'font-size="11" font-family="sans-serif" fill="#666">'
        f'{project_name}    |    Rev 1.0    |    '
        f'{datetime.date.today().isoformat()}    |    '
        f'Board: {board_w}x{board_h}mm</text>'
    )
    parts.append(
        f'<line x1="20" y1="50" x2="{svg_w - 20}" y2="50" '
        f'stroke="#ccc" stroke-width="1"/>'
    )

    # Board outline
    vertices = _get_board_vertices(board)
    if len(vertices) > 4 or board.get("outline_vertices"):
        # Polygon outline
        points_str = " ".join(
            f"{board_x + v[0] * scale:.1f},{board_y + (board_h - v[1]) * scale:.1f}"
            for v in vertices
        )
        parts.append(
            f'<polygon points="{points_str}" '
            f'fill="#f8f8f0" stroke="#333" stroke-width="1.5"/>'
        )
    else:
        # Rectangle outline
        parts.append(
            f'<rect x="{board_x}" y="{board_y}" '
            f'width="{board_area_w}" height="{board_area_h}" '
            f'fill="#f8f8f0" stroke="#333" stroke-width="1.5" rx="1"/>'
        )

    # Dimension annotations
    dim_y = board_y + board_area_h + 15
    # Width dimension
    parts.append(
        f'<line x1="{board_x}" y1="{dim_y}" x2="{board_x + board_area_w}" y2="{dim_y}" '
        f'stroke="#888" stroke-width="0.5" marker-start="url(#arrowL)" marker-end="url(#arrowR)"/>'
    )
    parts.append(
        f'<text x="{board_x + board_area_w / 2}" y="{dim_y + 12}" '
        f'text-anchor="middle" font-size="9" font-family="sans-serif" fill="#666">'
        f'{board_w}mm</text>'
    )
    # Height dimension
    dim_x = board_x - 15
    parts.append(
        f'<line x1="{dim_x}" y1="{board_y}" x2="{dim_x}" y2="{board_y + board_area_h}" '
        f'stroke="#888" stroke-width="0.5" marker-start="url(#arrowU)" marker-end="url(#arrowD)"/>'
    )
    parts.append(
        f'<text x="{dim_x}" y="{board_y + board_area_h / 2}" '
        f'text-anchor="middle" font-size="9" font-family="sans-serif" fill="#666" '
        f'transform="rotate(-90, {dim_x}, {board_y + board_area_h / 2})">'
        f'{board_h}mm</text>'
    )

    # Components
    for item in items:
        des = item["designator"]
        ctype = item.get("component_type", "unknown")
        x_mm = item["x_mm"]
        y_mm = item["y_mm"]
        w_mm = item["footprint_width_mm"]
        h_mm = item["footprint_height_mm"]
        rot = item.get("rotation_deg", 0)

        dw, dh = w_mm, h_mm
        if rot in (90, 270):
            dw, dh = h_mm, w_mm

        fill = _TYPE_COLORS.get(ctype, "#aaa")
        cx = board_x + x_mm * scale
        cy = board_y + (board_h - y_mm) * scale
        rx = dw * scale / 2
        ry = dh * scale / 2

        # Component rectangle
        parts.append(
            f'<rect x="{cx - rx:.1f}" y="{cy - ry:.1f}" '
            f'width="{dw * scale:.1f}" height="{dh * scale:.1f}" '
            f'fill="{fill}" fill-opacity="0.3" stroke="{fill}" stroke-width="1" rx="0.5"/>'
        )

        # Pin 1 indicator
        if ctype in _IC_TYPES or ctype in _POLARITY_TYPES:
            if rot == 0:
                px, py = cx - rx + 2, cy - ry + 2
            elif rot == 90:
                px, py = cx - rx + 2, cy + ry - 2
            elif rot == 180:
                px, py = cx + rx - 2, cy + ry - 2
            else:
                px, py = cx + rx - 2, cy - ry + 2
            parts.append(
                f'<circle cx="{px:.1f}" cy="{py:.1f}" r="2" fill="{fill}"/>'
            )

        # Polarity mark for diodes/LEDs
        if ctype in _POLARITY_TYPES:
            parts.append(
                f'<text x="{cx - rx + 3:.1f}" y="{cy + 3:.1f}" '
                f'font-size="6" font-family="sans-serif" font-weight="bold" fill="#333">+</text>'
            )

        # Designator label
        font_size = min(8, max(5, min(dw, dh) * scale * 0.4))
        parts.append(
            f'<text x="{cx:.1f}" y="{cy + font_size * 0.35:.1f}" '
            f'text-anchor="middle" font-size="{font_size:.1f}" '
            f'font-family="sans-serif" font-weight="bold" fill="#222">'
            f'{des}</text>'
        )

    # BOM table
    bom_y = board_y + board_area_h + dim_annotation_h + 10
    parts.append(
        f'<text x="{svg_w / 2}" y="{bom_y}" text-anchor="middle" '
        f'font-size="10" font-family="sans-serif" font-weight="bold" fill="#333">'
        f'Bill of Materials — {side}</text>'
    )
    bom_y += 8

    # Table headers
    col_widths = [60, 100, 80, 40, 160]  # Ref, Value, Package, Qty, Description
    headers = ["Ref", "Value", "Package", "Qty", "Description"]
    table_w = sum(col_widths)
    table_x = (svg_w - table_w) / 2

    # Header background
    parts.append(
        f'<rect x="{table_x}" y="{bom_y}" width="{table_w}" height="{bom_header_height}" '
        f'fill="#eee" stroke="#ccc" stroke-width="0.5"/>'
    )
    col_x = table_x
    for header, cw in zip(headers, col_widths):
        parts.append(
            f'<text x="{col_x + 4}" y="{bom_y + 14}" '
            f'font-size="8" font-family="sans-serif" font-weight="bold" fill="#333">'
            f'{header}</text>'
        )
        col_x += cw

    bom_y += bom_header_height

    # Table rows
    for i, row in enumerate(bom_rows):
        bg = "#f8f8f8" if i % 2 == 0 else "#fff"
        parts.append(
            f'<rect x="{table_x}" y="{bom_y}" width="{table_w}" '
            f'height="{bom_row_height}" fill="{bg}" stroke="#eee" stroke-width="0.5"/>'
        )
        col_x = table_x
        for val, cw in zip(row, col_widths):
            # Truncate long values
            display = val[:int(cw / 4)] if len(val) > cw / 4 else val
            parts.append(
                f'<text x="{col_x + 4}" y="{bom_y + 10}" '
                f'font-size="7" font-family="sans-serif" fill="#444">'
                f'{_svg_escape(display)}</text>'
            )
            col_x += cw
        bom_y += bom_row_height

    # Arrow marker definitions
    defs = '''<defs>
  <marker id="arrowR" markerWidth="6" markerHeight="4" refX="6" refY="2" orient="auto">
    <path d="M0,0 L6,2 L0,4" fill="none" stroke="#888" stroke-width="0.5"/>
  </marker>
  <marker id="arrowL" markerWidth="6" markerHeight="4" refX="0" refY="2" orient="auto">
    <path d="M6,0 L0,2 L6,4" fill="none" stroke="#888" stroke-width="0.5"/>
  </marker>
  <marker id="arrowD" markerWidth="4" markerHeight="6" refX="2" refY="6" orient="auto">
    <path d="M0,0 L2,6 L4,0" fill="none" stroke="#888" stroke-width="0.5"/>
  </marker>
  <marker id="arrowU" markerWidth="4" markerHeight="6" refX="2" refY="0" orient="auto">
    <path d="M0,6 L2,0 L4,6" fill="none" stroke="#888" stroke-width="0.5"/>
  </marker>
</defs>'''

    svg = (
        f'<svg xmlns="http://www.w3.org/2000/svg" '
        f'width="{svg_w}" height="{svg_h}" viewBox="0 0 {svg_w} {svg_h}">\n'
        f'{defs}\n'
        + "\n".join(parts)
        + "\n</svg>"
    )

    return svg


def _build_bom_table(
    items: list[dict], bom_lookup: dict[str, dict]
) -> list[tuple[str, str, str, str, str]]:
    """Build BOM table rows grouped by value+package."""
    # Group by (value, package, component_type)
    groups: dict[tuple[str, str, str], list[str]] = {}
    for item in items:
        des = item["designator"]
        bom_item = bom_lookup.get(des, {})
        value = bom_item.get("value", item.get("value", ""))
        package = bom_item.get("package", item.get("package", ""))
        ctype = item.get("component_type", "")
        key = (value, package, ctype)
        groups.setdefault(key, []).append(des)

    rows = []
    for (value, package, ctype), designators in sorted(groups.items()):
        refs = ", ".join(sorted(designators, key=_des_sort_key))
        desc = ""
        if designators:
            bom_item = bom_lookup.get(designators[0], {})
            desc = bom_item.get("description", "")
        rows.append((refs, value, package, str(len(designators)), desc))

    return rows


def _des_sort_key(des: str) -> tuple[str, int]:
    """Sort designators by prefix then number (R1 < R2 < R10)."""
    import re
    m = re.match(r"([A-Z]+)(\d+)", des)
    if m:
        return (m.group(1), int(m.group(2)))
    return (des, 0)


def _svg_escape(text: str) -> str:
    """Escape text for SVG."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def _concatenate_pdfs(input_paths: list[Path], output_path: Path) -> None:
    """Concatenate multiple single-page PDFs into one.

    Uses a minimal PDF merge approach — reads pages and writes a combined PDF.
    Falls back to just using the first page if merge fails.
    """
    try:
        # Try pypdf if available
        from pypdf import PdfMerger
        merger = PdfMerger()
        for p in input_paths:
            merger.append(str(p))
        merger.write(str(output_path))
        merger.close()
    except ImportError:
        # No pypdf — just use the first page
        import shutil
        shutil.copy2(input_paths[0], output_path)
