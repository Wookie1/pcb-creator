"""Netlist Visualizer — generates an interactive schematic-style block diagram.

Shows components as labeled boxes with pins, connected by color-coded nets.
Hover tooltips show component details and net connectivity.
"""

from __future__ import annotations

import json
import math
from pathlib import Path


# Component type → fill color (matching placement_viewer palette)
TYPE_COLORS = {
    "resistor": "#4a90d9",
    "capacitor": "#e8a838",
    "led": "#e04040",
    "diode": "#e04040",
    "ic": "#6b8e23",
    "voltage_regulator": "#6b8e23",
    "connector": "#8b5cf6",
    "switch": "#14b8a6",
    "crystal": "#ec4899",
    "fuse": "#f97316",
    "relay": "#b45309",
    "transistor_npn": "#059669",
    "transistor_pnp": "#059669",
    "transistor_nmos": "#059669",
    "transistor_pmos": "#059669",
    "inductor": "#7c3aed",
}
DEFAULT_COLOR = "#94a3b8"

# Net class → wire color
NET_COLORS = {
    "power": "#ef4444",
    "ground": "#3b82f6",
    "signal": "#a3a3a3",
}


def _parse_netlist(netlist: dict):
    """Extract components, ports, and nets from netlist JSON."""
    elements = netlist.get("elements", [])
    components = {}
    ports = {}
    nets = {}

    for e in elements:
        t = e.get("element_type")
        if t == "component":
            components[e["component_id"]] = e
        elif t == "port":
            ports[e["port_id"]] = e
        elif t == "net":
            nets[e["net_id"]] = e

    return components, ports, nets


def _layout_components(components: dict, ports: dict) -> dict:
    """Assign (x, y) positions to components using a simple grid layout.

    Groups by type: connectors on left, ICs in center, passives on right.
    Returns {component_id: {x, y, w, h, pins_left, pins_right}}.
    """
    # Categorize
    connectors = []
    ics = []
    passives = []

    for cid, comp in components.items():
        ctype = comp.get("component_type", "")
        if ctype in ("connector",):
            connectors.append(cid)
        elif ctype in ("ic", "voltage_regulator"):
            ics.append(cid)
        else:
            passives.append(cid)

    # Sort each group by designator
    def sort_key(cid):
        return components[cid].get("designator", "Z99")

    connectors.sort(key=sort_key)
    ics.sort(key=sort_key)
    passives.sort(key=sort_key)

    # Build port lists per component
    comp_ports = {}
    for pid, port in ports.items():
        cid = port["component_id"]
        comp_ports.setdefault(cid, []).append(port)

    # Sort pins by pin_number
    for cid in comp_ports:
        comp_ports[cid].sort(key=lambda p: p.get("pin_number", 0))

    # Sizing constants
    PIN_SPACING = 28
    MIN_HEIGHT = 60
    BOX_WIDTH = 100
    IC_WIDTH = 120
    COLUMN_GAP = 220
    ROW_GAP = 30

    layouts = {}

    def place_column(cids, col_x, width):
        y_cursor = 40
        for cid in cids:
            pins = comp_ports.get(cid, [])
            n_pins = len(pins)
            half = math.ceil(n_pins / 2)
            h = max(MIN_HEIGHT, half * PIN_SPACING + 20)

            # Split pins: left half, right half
            pins_left = pins[:half]
            pins_right = pins[half:]

            layouts[cid] = {
                "x": col_x, "y": y_cursor,
                "w": width, "h": h,
                "pins_left": pins_left,
                "pins_right": pins_right,
            }
            y_cursor += h + ROW_GAP

    # Three columns: connectors | ICs | passives
    col1_x = 60
    col2_x = col1_x + COLUMN_GAP
    col3_x = col2_x + COLUMN_GAP

    # If no ICs, use two columns
    if not ics:
        place_column(connectors, col1_x, BOX_WIDTH)
        place_column(passives, col2_x, BOX_WIDTH)
    else:
        place_column(connectors, col1_x, BOX_WIDTH)
        place_column(ics, col2_x, IC_WIDTH)
        place_column(passives, col3_x, BOX_WIDTH)

    return layouts, comp_ports


def _build_pin_positions(layouts: dict, comp_ports: dict) -> dict:
    """Compute absolute (x, y) and exit direction for each port_id.

    Returns {port_id: (x, y, side)} where side is "left" or "right".
    """
    pin_pos = {}
    for cid, lay in layouts.items():
        x, y, w, h = lay["x"], lay["y"], lay["w"], lay["h"]

        # Left pins — connection point is at the left edge of the box
        for i, port in enumerate(lay["pins_left"]):
            py = y + 24 + i * 28
            pin_pos[port["port_id"]] = (x, py, "left")

        # Right pins — connection point is at the right edge
        for i, port in enumerate(lay["pins_right"]):
            py = y + 24 + i * 28
            pin_pos[port["port_id"]] = (x + w, py, "right")

    return pin_pos


def generate_netlist_html(netlist: dict, bom: dict | None = None) -> str:
    """Generate a self-contained HTML page with the netlist block diagram."""
    components, ports, nets = _parse_netlist(netlist)
    layouts, comp_ports = _layout_components(components, ports)
    pin_pos = _build_pin_positions(layouts, comp_ports)

    project_name = netlist.get("project_name", "PCB")

    # Build BOM lookup for tooltips
    bom_lookup = {}
    if bom:
        for item in bom.get("bom", []):
            bom_lookup[item.get("designator", "")] = item

    # Compute canvas size (extra left margin for curves exiting left)
    max_x = max((l["x"] + l["w"] for l in layouts.values()), default=400) + 100
    max_y = max((l["y"] + l["h"] for l in layouts.values()), default=300) + 80

    # --- Build SVG ---
    svg_parts = []

    # Draw nets as curved paths that exit away from component boxes
    CURVE_OFFSET = 60  # how far the curve extends away from the box before turning

    def _bezier_path(px, py, side, tx, ty):
        """Build a cubic bezier from pin (px,py) to target (tx,ty).

        The curve exits in the direction of `side` (away from the box)
        before curving toward the target, so it never passes through the box.
        """
        # Control point 1: extend outward from the pin's side
        if side == "left":
            c1x = px - CURVE_OFFSET
        else:
            c1x = px + CURVE_OFFSET
        c1y = py
        # Control point 2: come in horizontally toward the target
        c2x = tx
        c2y = ty
        return f"M{px},{py} C{c1x},{c1y} {c2x},{c2y} {tx},{ty}"

    for nid, net in nets.items():
        color = NET_COLORS.get(net.get("net_class", "signal"), NET_COLORS["signal"])
        net_name = net.get("name", nid)
        connected = net.get("connected_port_ids", [])

        pin_data = [pin_pos[pid] for pid in connected if pid in pin_pos]
        if len(pin_data) < 2:
            continue

        # For multi-point nets, connect all to a central junction
        if len(pin_data) > 2:
            cx = sum(p[0] for p in pin_data) / len(pin_data)
            cy = sum(p[1] for p in pin_data) / len(pin_data)

            for px, py, side in pin_data:
                d = _bezier_path(px, py, side, cx, cy)
                svg_parts.append(
                    f'<path d="{d}" '
                    f'stroke="{color}" stroke-width="2" fill="none" opacity="0.7">'
                    f'<title>{net_name} ({net.get("net_class", "")})</title></path>'
                )
            # Junction dot
            svg_parts.append(
                f'<circle cx="{cx}" cy="{cy}" r="4" fill="{color}" opacity="0.9">'
                f'<title>{net_name}</title></circle>'
            )
        else:
            # Two-point net: direct bezier
            p1x, p1y, s1 = pin_data[0]
            p2x, p2y, s2 = pin_data[1]
            # Control points exit away from their respective boxes
            if s1 == "left":
                c1x = p1x - CURVE_OFFSET
            else:
                c1x = p1x + CURVE_OFFSET
            if s2 == "left":
                c2x = p2x - CURVE_OFFSET
            else:
                c2x = p2x + CURVE_OFFSET
            svg_parts.append(
                f'<path d="M{p1x},{p1y} C{c1x},{p1y} {c2x},{p2y} {p2x},{p2y}" '
                f'stroke="{color}" stroke-width="2" fill="none" opacity="0.7">'
                f'<title>{net_name} ({net.get("net_class", "")})</title></path>'
            )

    # Draw component boxes
    for cid, lay in layouts.items():
        comp = components[cid]
        x, y, w, h = lay["x"], lay["y"], lay["w"], lay["h"]
        des = comp.get("designator", "?")
        ctype = comp.get("component_type", "")
        value = comp.get("value", "")
        color = TYPE_COLORS.get(ctype, DEFAULT_COLOR)
        desc = comp.get("description", "")

        # BOM info for tooltip
        bom_item = bom_lookup.get(des, {})
        specs = bom_item.get("specs", {})
        specs_str = ", ".join(f"{k}: {v}" for k, v in specs.items()) if specs else ""

        tooltip_lines = [f"{des} — {ctype}"]
        if value:
            tooltip_lines.append(f"Value: {value}")
        if comp.get("package"):
            tooltip_lines.append(f"Package: {comp['package']}")
        if specs_str:
            tooltip_lines.append(f"Specs: {specs_str}")
        if desc:
            tooltip_lines.append(desc[:80])
        tooltip = "&#10;".join(tooltip_lines)

        # Box with rounded corners
        svg_parts.append(
            f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="6" ry="6" '
            f'fill="{color}" fill-opacity="0.15" stroke="{color}" stroke-width="2" '
            f'class="comp-box" data-des="{des}">'
            f'<title>{tooltip}</title></rect>'
        )

        # Designator label (bold, top)
        svg_parts.append(
            f'<text x="{x + w/2}" y="{y + 14}" text-anchor="middle" '
            f'font-size="13" font-weight="bold" fill="{color}" '
            f'pointer-events="none">{des}</text>'
        )

        # Value label (smaller, below designator)
        if value:
            display_val = value if len(value) <= 14 else value[:12] + ".."
            svg_parts.append(
                f'<text x="{x + w/2}" y="{y + h - 6}" text-anchor="middle" '
                f'font-size="10" fill="#999" pointer-events="none">{display_val}</text>'
            )

        # Draw pins
        for i, port in enumerate(lay["pins_left"]):
            py = y + 24 + i * 28
            pin_name = port.get("name", str(port.get("pin_number", "")))
            etype = port.get("electrical_type", "")

            # Pin dot
            svg_parts.append(
                f'<circle cx="{x}" cy="{py}" r="3" fill="{color}">'
                f'<title>{des}.{pin_name} ({etype})</title></circle>'
            )
            # Pin label (inside box)
            svg_parts.append(
                f'<text x="{x + 6}" y="{py + 4}" font-size="9" fill="#ccc" '
                f'pointer-events="none">{pin_name}</text>'
            )

        for i, port in enumerate(lay["pins_right"]):
            py = y + 24 + i * 28
            pin_name = port.get("name", str(port.get("pin_number", "")))
            etype = port.get("electrical_type", "")

            svg_parts.append(
                f'<circle cx="{x + w}" cy="{py}" r="3" fill="{color}">'
                f'<title>{des}.{pin_name} ({etype})</title></circle>'
            )
            svg_parts.append(
                f'<text x="{x + w - 6}" y="{py + 4}" font-size="9" fill="#ccc" '
                f'text-anchor="end" pointer-events="none">{pin_name}</text>'
            )

    svg_content = "\n    ".join(svg_parts)

    # Count stats
    n_comp = len(components)
    n_nets = len(nets)
    n_ports = len(ports)

    # Net legend
    legend_items = []
    net_classes_present = sorted(set(n.get("net_class", "signal") for n in nets.values()))
    for nc in net_classes_present:
        color = NET_COLORS.get(nc, NET_COLORS["signal"])
        count = sum(1 for n in nets.values() if n.get("net_class") == nc)
        legend_items.append(
            f'<span style="display:inline-flex;align-items:center;gap:4px;margin-right:12px;">'
            f'<span style="width:20px;height:3px;background:{color};display:inline-block;"></span>'
            f'{nc} ({count})</span>'
        )

    # Component type legend
    types_present = sorted(set(c.get("component_type", "?") for c in components.values()))
    for t in types_present:
        color = TYPE_COLORS.get(t, DEFAULT_COLOR)
        count = sum(1 for c in components.values() if c.get("component_type") == t)
        legend_items.append(
            f'<span style="display:inline-flex;align-items:center;gap:4px;margin-right:12px;">'
            f'<span style="width:12px;height:12px;background:{color};opacity:0.4;'
            f'border:2px solid {color};border-radius:2px;display:inline-block;"></span>'
            f'{t} ({count})</span>'
        )

    legend_html = "".join(legend_items)

    # --- Build full HTML ---
    html = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8">
<title>Netlist: {project_name}</title>
<style>
  * {{ margin: 0; padding: 0; box-sizing: border-box; }}
  body {{ background: #1a1a2e; color: #e0e0e0; font-family: -apple-system, sans-serif; overflow: hidden; }}
  .header {{
    background: #16213e; padding: 8px 16px; display: flex;
    align-items: center; justify-content: space-between; border-bottom: 1px solid #333;
  }}
  .header h2 {{ font-size: 15px; color: #e0e0e0; }}
  .stats {{ font-size: 12px; color: #888; }}
  .legend {{ padding: 6px 16px; background: #16213e; font-size: 11px; color: #aaa;
    border-bottom: 1px solid #333; display: flex; flex-wrap: wrap; gap: 4px; }}
  .canvas-wrap {{
    width: 100%; height: calc(100vh - 70px); overflow: hidden;
  }}
  svg {{ display: block; width: 100%; height: 100%; }}
  .comp-box {{ cursor: pointer; transition: fill-opacity 0.15s; }}
  .comp-box:hover {{ fill-opacity: 0.35 !important; stroke-width: 3; }}
</style>
</head><body>
<div class="header">
  <h2>Netlist: {project_name}</h2>
  <span class="stats">{n_comp} components &middot; {n_nets} nets &middot; {n_ports} pins</span>
</div>
<div class="legend">{legend_html}</div>
<div class="canvas-wrap" id="wrap">
  <svg xmlns="http://www.w3.org/2000/svg"
       viewBox="0 0 {max_x} {max_y}"
       preserveAspectRatio="xMidYMid meet">
    {svg_content}
  </svg>
</div>
<script>
// Pan support
const wrap = document.getElementById('wrap');
let isPanning = false, startX, startY, scrollL, scrollT;
wrap.addEventListener('mousedown', e => {{
  isPanning = true; startX = e.pageX; startY = e.pageY;
  scrollL = wrap.scrollLeft; scrollT = wrap.scrollTop;
}});
wrap.addEventListener('mousemove', e => {{
  if (!isPanning) return;
  wrap.scrollLeft = scrollL - (e.pageX - startX);
  wrap.scrollTop = scrollT - (e.pageY - startY);
}});
wrap.addEventListener('mouseup', () => isPanning = false);
wrap.addEventListener('mouseleave', () => isPanning = false);
</script>
</body></html>"""

    return html


# --- CLI entry point ---
if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Generate netlist block diagram")
    parser.add_argument("netlist", type=Path, help="Path to netlist JSON")
    parser.add_argument("--bom", type=Path, default=None, help="Optional BOM JSON")
    parser.add_argument("-o", "--output", type=Path, default=None, help="Output HTML")
    args = parser.parse_args()

    netlist = json.loads(args.netlist.read_text())
    bom = json.loads(args.bom.read_text()) if args.bom else None
    html = generate_netlist_html(netlist, bom)

    out = args.output or args.netlist.with_suffix(".html").with_stem(
        args.netlist.stem + "_netlist_view"
    )
    out.write_text(html)
    print(f"Written: {out}")
