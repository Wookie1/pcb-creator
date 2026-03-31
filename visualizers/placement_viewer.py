#!/usr/bin/env python3
"""PCB Placement Visualizer — generates an interactive HTML/SVG board view.

Usage:
    python placement_viewer.py <placement.json> [--netlist <netlist.json>] [--output board.html]

Generates a self-contained HTML file with:
- Board outline
- Component footprints (color-coded by type)
- Designator labels
- Ratsnest lines (if netlist provided)
- Fiducial markers
- Hover tooltips with component details
- Pan/zoom via mouse
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Add project root for imports
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from optimizers.ratsnest import build_connectivity, compute_mst_edges
from optimizers.pad_geometry import (
    build_pad_map, get_footprint_def, _generate_fallback_footprint, _rotate_offset,
)


# Component type → fill color
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
    "fiducial": "#888888",
    "transistor_npn": "#059669",
    "transistor_pnp": "#059669",
    "transistor_nmos": "#059669",
    "transistor_pmos": "#059669",
    "inductor": "#7c3aed",
}

DEFAULT_COLOR = "#94a3b8"

# Net class → ratsnest line color
NET_COLORS = {
    "power": "#ef4444",
    "ground": "#3b82f6",
    "signal": "#a3a3a3",
}


def _build_bom_lookup(bom: dict | None) -> dict[str, dict]:
    """Build a designator -> BOM item lookup."""
    if not bom:
        return {}
    return {item["designator"]: item for item in bom.get("bom", [])}


def _format_specs(specs: dict) -> str:
    """Format specs dict into a compact display string."""
    parts = []
    # Prioritize the most useful fields per component type
    priority_keys = [
        "tolerance", "power_rating", "voltage_rating", "forward_voltage",
        "forward_current", "color", "pin_count", "pitch", "type",
        "contact_rating", "actuation_force",
    ]
    for key in priority_keys:
        if key in specs:
            label = key.replace("_", " ").title()
            parts.append(f"{label}: {specs[key]}")
    # Add remaining keys not in priority list
    for key, val in specs.items():
        if key not in priority_keys and key not in ("material", "mating_type", "life_cycles"):
            label = key.replace("_", " ").title()
            parts.append(f"{label}: {val}")
    return ", ".join(parts)


def _routing_stats_html(routed: dict | None, netlist: dict | None = None) -> str:
    """Build routing statistics HTML for the side panel, including per-net breakdown."""
    if not routed:
        return ""
    routing = routed.get("routing", {})
    stats = routing.get("statistics", {})
    if not stats:
        return ""

    completion = stats.get("completion_pct", 0)
    color = "#22c55e" if completion == 100 else "#f59e0b" if completion >= 50 else "#ef4444"
    unrouted = routing.get("unrouted_nets", [])
    unrouted_str = ", ".join(unrouted) if unrouted else "none"
    overrides = routing.get("trace_width_overrides", {})

    html = f'''<div style="margin-bottom:16px">
      <div class="legend-title">Routing</div>
      <div style="font-size:12px;line-height:1.8">
        <div>Completion: <span style="color:{color};font-weight:600">{completion}%</span>
          ({stats.get("routed_nets", 0)}/{stats.get("total_nets", 0)} nets)</div>
        <div>Traces: {len(routing.get("traces", []))} segments,
          {stats.get("total_trace_length_mm", 0):.1f}mm total</div>
        <div>Vias: {stats.get("via_count", 0)}</div>
        <div>Top: {stats.get("layer_usage", {}).get("top_trace_length_mm", 0):.1f}mm
          / Bottom: {stats.get("layer_usage", {}).get("bottom_trace_length_mm", 0):.1f}mm</div>'''

    if unrouted:
        html += f'\n        <div style="color:#f59e0b">Unrouted: {unrouted_str}</div>'

    if overrides:
        html += '\n        <div style="color:#8bf">IPC-2221 upsizes: ' + ", ".join(
            f'{nid}' for nid in overrides
        ) + '</div>'

    # Copper fill stats
    fill_polygons = stats.get("copper_fill_polygons", 0)
    fill_layers = stats.get("copper_fill_layers", [])
    if fill_polygons > 0:
        fill_layers_str = " + ".join(fill_layers)
        fill_net = routing.get("copper_fills", [{}])[0].get("net_name", "GND") if routing.get("copper_fills") else "GND"
        html += f'\n        <div style="color:#3b82f6">Copper fill: {fill_net} ({fill_layers_str}), {fill_polygons} polygons</div>'

    html += '\n      </div>\n    </div>'

    # Per-net breakdown table
    if netlist and routing.get("traces"):
        html += _per_net_stats_html(routing, netlist)

    return html


def _per_net_stats_html(routing: dict, netlist: dict) -> str:
    """Build collapsible per-net routing stats table."""
    traces = routing.get("traces", [])
    vias = routing.get("vias", [])

    # Build net info from netlist
    net_info: dict[str, dict] = {}  # net_id -> {name, class, components}
    comp_map: dict[str, str] = {}  # component_id -> designator
    port_to_comp: dict[str, str] = {}  # port_id -> component_id

    for elem in netlist.get("elements", []):
        etype = elem.get("element_type")
        if etype == "component":
            comp_map[elem["component_id"]] = elem["designator"]
        elif etype == "port":
            port_to_comp[elem["port_id"]] = elem.get("component_id", "")
        elif etype == "net":
            designators = set()
            for pid in elem.get("connected_port_ids", []):
                cid = port_to_comp.get(pid)
                if cid and cid in comp_map:
                    designators.add(comp_map[cid])
            net_info[elem["net_id"]] = {
                "name": elem.get("name", elem["net_id"]),
                "net_class": elem.get("net_class", "signal"),
                "components": sorted(designators),
            }

    # Aggregate trace/via stats per net
    net_stats: dict[str, dict] = {}
    for t in traces:
        nid = t.get("net_id", "")
        if nid not in net_stats:
            net_stats[nid] = {"traces": 0, "length": 0.0, "vias": 0, "widths": set()}
        ns = net_stats[nid]
        ns["traces"] += 1
        dx = t["end_x_mm"] - t["start_x_mm"]
        dy = t["end_y_mm"] - t["start_y_mm"]
        ns["length"] += (dx**2 + dy**2) ** 0.5
        ns["widths"].add(t.get("width_mm", 0.25))

    for v in vias:
        nid = v.get("net_id", "")
        if nid in net_stats:
            net_stats[nid]["vias"] += 1

    # Also count copper fill nets
    for fill in routing.get("copper_fills", []):
        nid = fill.get("net_id", "")
        if nid not in net_stats:
            net_stats[nid] = {"traces": 0, "length": 0.0, "vias": 0, "widths": set()}
        net_stats[nid]["fill"] = True

    NC_COLORS = {"power": "#ef4444", "ground": "#3b82f6", "signal": "#a3a3a3"}
    NC_ORDER = {"power": 0, "ground": 1, "signal": 2}

    # Sort: power first, ground, signal
    sorted_nets = sorted(
        net_stats.items(),
        key=lambda item: (
            NC_ORDER.get(net_info.get(item[0], {}).get("net_class", "signal"), 2),
            net_info.get(item[0], {}).get("name", item[0]),
        ),
    )

    rows = []
    for nid, ns in sorted_nets:
        info = net_info.get(nid, {"name": nid, "net_class": "signal", "components": []})
        nc = info["net_class"]
        nc_color = NC_COLORS.get(nc, "#a3a3a3")
        width_str = "/".join(f"{w:.2f}" for w in sorted(ns["widths"])) if ns["widths"] else "-"
        comps = ", ".join(info["components"][:6])
        if len(info["components"]) > 6:
            comps += f" +{len(info['components']) - 6}"
        fill_badge = ' <span style="color:#3b82f6" title="copper fill">F</span>' if ns.get("fill") else ""

        rows.append(
            f'<tr>'
            f'<td><span style="color:{nc_color}">&#9679;</span> {info["name"]}{fill_badge}</td>'
            f'<td>{width_str}</td>'
            f'<td>{ns["length"]:.1f}</td>'
            f'<td>{ns["vias"]}</td>'
            f'<td style="color:#888;font-size:10px" title="{", ".join(info["components"])}">{comps}</td>'
            f'</tr>'
        )

    html = f'''<div style="margin-bottom:16px">
      <details>
        <summary style="cursor:pointer;font-size:13px;font-weight:600;color:#aaa;margin-bottom:6px">
          Per-Net Details ({len(sorted_nets)} nets)
        </summary>
        <table style="font-size:10px">
          <tr>
            <th>Net</th><th>W(mm)</th><th>Len</th><th>Vias</th><th>Components</th>
          </tr>
          {"".join(rows)}
        </table>
      </details>
    </div>'''
    return html


def _kicad_export_html(routed: dict | None, netlist: dict | None) -> str:
    """Build KiCad export button HTML + embedded data for client-side export."""
    if not routed or not netlist:
        return ""

    routing = routed.get("routing", {})
    if not routing:
        return ""

    project_name = routed.get("project_name", "board")
    default_filename = f"{project_name}.kicad_pcb"

    # Embed JSON data for client-side export
    routed_json = json.dumps(routed)
    netlist_json = json.dumps(netlist)

    return f'''
    <div style="margin-bottom:16px">
      <div class="legend-title">Export</div>
      <div style="display:flex;gap:8px;align-items:center;margin-bottom:8px">
        <input type="text" id="kicadFilename" value="{default_filename}"
          style="flex:1;background:#1a1a2e;border:1px solid #2a2a4a;color:#e2e8f0;
                 padding:4px 8px;border-radius:4px;font-size:12px">
        <button onclick="exportKicad()"
          style="background:#4a90d9;color:white;border:none;padding:6px 12px;
                 border-radius:4px;cursor:pointer;font-size:12px;white-space:nowrap">
          Export KiCad
        </button>
      </div>
    </div>
    <script type="application/json" id="routedData">{routed_json}</script>
    <script type="application/json" id="netlistData">{netlist_json}</script>
    <script>
    function exportKicad() {{
      const routed = JSON.parse(document.getElementById('routedData').textContent);
      const netlist = JSON.parse(document.getElementById('netlistData').textContent);
      const filename = document.getElementById('kicadFilename').value || 'board.kicad_pcb';

      const uid = () => crypto.randomUUID ? crypto.randomUUID() :
        'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g, c => {{
          const r = Math.random() * 16 | 0;
          return (c === 'x' ? r : (r & 0x3 | 0x8)).toString(16);
        }});

      const layerMap = {{'top': 'F.Cu', 'bottom': 'B.Cu', 'top_silk': 'F.SilkS', 'bottom_silk': 'B.SilkS'}};

      // Build net list
      const nets = netlist.elements.filter(e => e.element_type === 'net');
      const netNumMap = {{}};
      nets.forEach((n, i) => {{ netNumMap[n.net_id] = i + 1; }});

      // Build port->net mapping
      const portNet = {{}};
      nets.forEach(n => {{
        (n.connected_port_ids || []).forEach(pid => {{ portNet[pid] = n; }});
      }});

      let out = `(kicad_pcb\\n  (version 20240108)\\n  (generator "pcb-creator")\\n  (generator_version "1.0")\\n`;
      out += `  (general (thickness 1.6) (legacy_teardrops no))\\n  (paper "A4")\\n`;

      // Layers
      out += `  (layers\\n    (0 "F.Cu" signal)\\n    (31 "B.Cu" signal)\\n`;
      out += `    (36 "B.SilkS" user "B.Silkscreen")\\n    (37 "F.SilkS" user "F.Silkscreen")\\n`;
      out += `    (38 "B.Mask" user "B.Mask")\\n    (39 "F.Mask" user "F.Mask")\\n`;
      out += `    (44 "Edge.Cuts" user)\\n    (47 "F.CrtYd" user "F.Courtyard")\\n`;
      out += `    (46 "B.CrtYd" user "B.Courtyard")\\n    (49 "F.Fab" user "F.Fabrication")\\n`;
      out += `    (48 "B.Fab" user "B.Fabrication")\\n    (34 "B.Paste" user)\\n    (35 "F.Paste" user)\\n  )\\n`;

      // Setup
      out += `  (setup (pad_to_mask_clearance 0.05) (pcbplotparams (layerselection 0x00010fc_ffffffff) (disableapertmacros no) (usegerberextensions no) (usegerberattributes yes) (creategerberjobfile yes) (dashed_line_dash_ratio 12.0) (dashed_line_gap_ratio 3.0) (svgprecision 4) (plotframeref no) (viasonmask no) (mode 1) (useauxorigin no) (hpglpennumber 1) (hpglpenspeed 20) (hpglpendiameter 15.0) (pdf_front_fp_property_popups yes) (pdf_back_fp_property_popups yes) (dxf_imperial_units yes) (dxf_use_pcbnew_font yes) (psnegative no) (psa4output no) (plotreference yes) (plotvalue yes) (plotfptext yes) (plotinvisibletext no) (sketchpadsonfab no) (subtractmaskfromsilk no) (outputformat 1) (mirror no) (drillshape 1) (scaleselection 1) (outputdirectory "")))\\n`;

      // Nets
      out += `  (net 0 "")\\n`;
      nets.forEach((n, i) => {{ out += `  (net ${{i+1}} "${{n.name || n.net_id}}")\\n`; }});

      // Board outline
      const bw = routed.board.width_mm, bh = routed.board.height_mm;
      [[0,0,bw,0],[bw,0,bw,bh],[bw,bh,0,bh],[0,bh,0,0]].forEach(([x1,y1,x2,y2]) => {{
        out += `  (gr_line (start ${{x1}} ${{y1}}) (end ${{x2}} ${{y2}}) (stroke (width 0.05) (type default)) (layer "Edge.Cuts") (tstamp ${{uid()}}))\\n`;
      }});

      // Traces
      (routed.routing.traces || []).forEach(t => {{
        const layer = layerMap[t.layer] || 'F.Cu';
        const nn = netNumMap[t.net_id] || 0;
        out += `  (segment (start ${{t.start_x_mm}} ${{t.start_y_mm}}) (end ${{t.end_x_mm}} ${{t.end_y_mm}}) (width ${{t.width_mm}}) (layer "${{layer}}") (net ${{nn}}) (tstamp ${{uid()}}))\\n`;
      }});

      // Vias
      (routed.routing.vias || []).forEach(v => {{
        const nn = netNumMap[v.net_id] || 0;
        out += `  (via (at ${{v.x_mm}} ${{v.y_mm}}) (size ${{v.diameter_mm}}) (drill ${{v.drill_mm}}) (layers "F.Cu" "B.Cu") (net ${{nn}}) (tstamp ${{uid()}}))\\n`;
      }});

      // Copper fills — modern zone format (outline + fill rules, KiCad refills)
      const seenZones = new Set();
      const zm = 0.1;
      (routed.routing.copper_fills || []).forEach(f => {{
        const layer = layerMap[f.layer] || 'F.Cu';
        const nn = netNumMap[f.net_id] || 0;
        const key = layer + ':' + f.net_id;
        if (seenZones.has(key)) return;
        seenZones.add(key);
        out += `  (zone (net ${{nn}}) (net_name "${{f.net_name}}") (layer "${{layer}}") (tstamp ${{uid()}})\\n`;
        out += `    (hatch edge 0.5)\\n`;
        out += `    (connect_pads (clearance 0.5))\\n`;
        out += `    (min_thickness 0.2)\\n`;
        out += `    (fill yes (thermal_gap 0.5) (thermal_bridge_width 0.5))\\n`;
        out += `    (polygon (pts (xy ${{zm}} ${{zm}}) (xy ${{bw-zm}} ${{zm}}) (xy ${{bw-zm}} ${{bh-zm}}) (xy ${{zm}} ${{bh-zm}})))\\n`;
        out += `  )\\n`;
      }});

      // Silkscreen
      (routed.silkscreen || []).forEach(s => {{
        const layer = layerMap[s.layer] || 'F.SilkS';
        if (s.type === 'text') {{
          const fh = s.font_height_mm || 1.0;
          out += `  (gr_text "${{s.text}}" (at ${{s.x_mm}} ${{s.y_mm}}) (layer "${{layer}}") (effects (font (size ${{fh}} ${{fh}}) (thickness ${{(fh*0.15).toFixed(2)}}))) (tstamp ${{uid()}}))\\n`;
        }} else if (s.type === 'dot') {{
          const r = (s.diameter_mm || 0.5) / 2;
          out += `  (gr_circle (center ${{s.x_mm}} ${{s.y_mm}}) (end ${{s.x_mm+r}} ${{s.y_mm}}) (stroke (width 0) (type default)) (fill solid) (layer "${{layer}}") (tstamp ${{uid()}}))\\n`;
        }}
      }});

      out += `)\\n`;

      // Download
      const blob = new Blob([out], {{type: 'text/plain'}});
      const a = document.createElement('a');
      a.href = URL.createObjectURL(blob);
      a.download = filename;
      a.click();
      URL.revokeObjectURL(a.href);
    }}
    </script>'''


def _header_routing_stat(routed: dict | None) -> str:
    """Inline routing stat for the header bar."""
    if not routed:
        return ""
    stats = routed.get("routing", {}).get("statistics", {})
    pct = stats.get("completion_pct", 0)
    color = "#22c55e" if pct == 100 else "#f59e0b" if pct >= 50 else "#ef4444"
    return f' &bull; <span style="color:{color}">routed {pct:.0f}%</span>'


def _progress_bar_html(routed: dict | None) -> str:
    """Thin progress bar below the header showing routing completion."""
    if not routed:
        return ""
    stats = routed.get("routing", {}).get("statistics", {})
    pct = stats.get("completion_pct", 0)
    color = "#22c55e" if pct == 100 else "#f59e0b" if pct >= 50 else "#ef4444"
    return (
        f'<div class="progress-bar">'
        f'<div class="progress-fill" style="width:{pct}%;background:{color}"></div>'
        f'</div>'
    )


def generate_svg(
    placement: dict,
    netlist: dict | None = None,
    bom: dict | None = None,
    routed: dict | None = None,
) -> str:
    """Generate SVG content for the placement."""
    board = placement.get("board", {})
    board_w = board.get("width_mm", 50)
    board_h = board.get("height_mm", 30)
    items = placement.get("placements", [])
    bom_lookup = _build_bom_lookup(bom)

    # SVG coordinate system: scale mm to pixels (10px per mm)
    scale = 10
    margin = 20  # px margin around board
    svg_w = board_w * scale + margin * 2
    svg_h = board_h * scale + margin * 2

    parts: list[str] = []

    # Board background
    outline_vertices = board.get("outline_vertices")
    if outline_vertices and len(outline_vertices) >= 3:
        # Polygon board outline
        points_str = " ".join(
            f"{margin + v[0] * scale:.1f},{margin + (board_h - v[1]) * scale:.1f}"
            for v in outline_vertices
        )
        parts.append(
            f'<polygon points="{points_str}" '
            f'fill="#1a1a2e" stroke="#4a4a6a" stroke-width="2"/>'
        )
    else:
        # Rectangular board
        parts.append(
            f'<rect x="{margin}" y="{margin}" width="{board_w * scale}" height="{board_h * scale}" '
            f'fill="#1a1a2e" stroke="#4a4a6a" stroke-width="2" rx="2"/>'
        )

    # Grid lines (5mm spacing)
    for x_mm in range(0, int(board_w) + 1, 5):
        x = margin + x_mm * scale
        parts.append(
            f'<line x1="{x}" y1="{margin}" x2="{x}" y2="{margin + board_h * scale}" '
            f'stroke="#2a2a4a" stroke-width="0.5"/>'
        )
    for y_mm in range(0, int(board_h) + 1, 5):
        y = margin + (board_h - y_mm) * scale
        parts.append(
            f'<line x1="{margin}" y1="{y}" x2="{margin + board_w * scale}" y2="{y}" '
            f'stroke="#2a2a4a" stroke-width="0.5"/>'
        )

    # Ratsnest lines (only shown when no routed traces — otherwise traces replace them)
    has_traces = routed and len(routed.get("routing", {}).get("traces", [])) > 0
    if netlist and not has_traces:
        nets = build_connectivity(netlist)
        positions = {}
        for item in items:
            positions[item["designator"]] = (item["x_mm"], item["y_mm"])

        for net in nets:
            pts = [(positions[d][0], positions[d][1])
                   for d in net.designators if d in positions]
            if len(pts) < 2:
                continue
            edges = compute_mst_edges(pts)
            color = NET_COLORS.get(net.net_class, "#a3a3a3")
            for ia, ib, _ in edges:
                x1 = margin + pts[ia][0] * scale
                y1 = margin + (board_h - pts[ia][1]) * scale
                x2 = margin + pts[ib][0] * scale
                y2 = margin + (board_h - pts[ib][1]) * scale
                parts.append(
                    f'<line x1="{x1:.1f}" y1="{y1:.1f}" x2="{x2:.1f}" y2="{y2:.1f}" '
                    f'stroke="{color}" stroke-width="1" stroke-dasharray="3,3" opacity="0.5">'
                    f'<title>{net.name} ({net.net_class})</title></line>'
                )

    # Routed traces and vias
    if routed:
        routing = routed.get("routing", {})
        traces = routing.get("traces", [])
        vias_list = routing.get("vias", [])

        # Build net_id -> color lookup
        net_class_map: dict[str, str] = {}
        if netlist:
            for elem in netlist.get("elements", []):
                if elem.get("element_type") == "net":
                    net_class_map[elem["net_id"]] = elem.get("net_class", "signal")

        # Generate distinct colors for nets
        trace_colors: dict[str, str] = {}
        color_palette = [
            "#ef4444", "#3b82f6", "#22c55e", "#f59e0b", "#a855f7",
            "#ec4899", "#14b8a6", "#f97316", "#06b6d4", "#84cc16",
        ]
        color_idx = 0
        for trace in traces:
            nid = trace.get("net_id", "")
            if nid not in trace_colors:
                nc = net_class_map.get(nid, "signal")
                if nc == "power":
                    trace_colors[nid] = "#ef4444"
                elif nc == "ground":
                    trace_colors[nid] = "#3b82f6"
                else:
                    trace_colors[nid] = color_palette[color_idx % len(color_palette)]
                    color_idx += 1

        # Draw copper fills (behind everything — lowest z-order)
        copper_fills = routing.get("copper_fills", [])
        for fill_region in copper_fills:
            fill_layer = fill_region.get("layer", "top")
            fill_opacity = "0.12" if fill_layer == "top" else "0.06"
            fill_nid = fill_region.get("net_id", "")
            fill_color = trace_colors.get(fill_nid, "#3b82f6")
            for polygon in fill_region.get("polygons", []):
                if len(polygon) < 3:
                    continue
                points_str = " ".join(
                    f"{margin + p[0] * scale:.1f},{margin + (board_h - p[1]) * scale:.1f}"
                    for p in polygon
                )
                parts.append(
                    f'<polygon points="{points_str}" '
                    f'fill="{fill_color}" opacity="{fill_opacity}" '
                    f'stroke="none" class="copper-fill">'
                    f'<title>Fill: {fill_region.get("net_name", fill_nid)} ({fill_layer})</title>'
                    f'</polygon>'
                )

        # Build net class lookup for trace tooltips
        net_info_map: dict[str, dict] = {}
        if netlist:
            for elem in netlist.get("elements", []):
                if elem.get("element_type") == "net":
                    net_info_map[elem["net_id"]] = {
                        "name": elem.get("name", ""),
                        "net_class": elem.get("net_class", "signal"),
                    }

        # Draw traces (bottom layer first, then top for proper z-order)
        for layer_name in ["bottom", "top"]:
            layer_opacity = "0.5" if layer_name == "bottom" else "0.85"
            for trace in traces:
                if trace.get("layer") != layer_name:
                    continue
                nid = trace.get("net_id", "")
                color = trace_colors.get(nid, "#a3a3a3")
                tw_mm = trace.get("width_mm", 0.25)
                tw = tw_mm * scale
                x1 = margin + trace["start_x_mm"] * scale
                y1 = margin + (board_h - trace["start_y_mm"]) * scale
                x2 = margin + trace["end_x_mm"] * scale
                y2 = margin + (board_h - trace["end_y_mm"]) * scale
                # Compute segment length
                dx = trace["end_x_mm"] - trace["start_x_mm"]
                dy = trace["end_y_mm"] - trace["start_y_mm"]
                seg_len = (dx**2 + dy**2) ** 0.5

                net_name = trace.get("net_name", net_info_map.get(nid, {}).get("name", nid))
                net_class = net_info_map.get(nid, {}).get("net_class", "signal")

                # Use a wider invisible hit-target for thin traces
                hit_width = max(tw, 5.0)

                # Data attributes for custom tooltip
                data_attrs = (
                    f'data-trace-net="{net_name}" '
                    f'data-trace-class="{net_class}" '
                    f'data-trace-width="{tw_mm:.2f}" '
                    f'data-trace-layer="{layer_name}" '
                    f'data-trace-len="{seg_len:.2f}"'
                )

                # Invisible wider hit target
                parts.append(
                    f'<line x1="{x1:.1f}" y1="{y1:.1f}" x2="{x2:.1f}" y2="{y2:.1f}" '
                    f'stroke="transparent" stroke-width="{hit_width:.1f}" '
                    f'class="trace-hit" {data_attrs}/>'
                )
                # Visible trace
                parts.append(
                    f'<line x1="{x1:.1f}" y1="{y1:.1f}" x2="{x2:.1f}" y2="{y2:.1f}" '
                    f'stroke="{color}" stroke-width="{tw:.1f}" stroke-linecap="round" '
                    f'opacity="{layer_opacity}" class="trace" pointer-events="none"/>'
                )

        # Draw vias
        for via in vias_list:
            vx = margin + via["x_mm"] * scale
            vy = margin + (board_h - via["y_mm"]) * scale
            outer_r = via.get("diameter_mm", 0.6) / 2 * scale
            inner_r = via.get("drill_mm", 0.3) / 2 * scale
            nid = via.get("net_id", "")
            color = trace_colors.get(nid, "#a3a3a3")
            parts.append(
                f'<circle cx="{vx:.1f}" cy="{vy:.1f}" r="{outer_r:.1f}" '
                f'fill="{color}" opacity="0.8" class="via">'
                f'<title>Via: {via.get("net_name", nid)}</title></circle>'
            )
            parts.append(
                f'<circle cx="{vx:.1f}" cy="{vy:.1f}" r="{inner_r:.1f}" '
                f'fill="#0f0f1a" class="via-drill"/>'
            )

    # Silkscreen elements
    silk_items = []
    if routed:
        silk_items = routed.get("silkscreen", [])
    for silk in silk_items:
        sx = margin + silk.get("x_mm", 0) * scale
        sy = margin + (board_h - silk.get("y_mm", 0)) * scale
        silk_layer = silk.get("layer", "top_silk")
        silk_opacity = "0.9" if silk_layer == "top_silk" else "0.5"

        if silk["type"] == "text":
            fh = silk.get("font_height_mm", 0.8) * scale
            purpose = silk.get("purpose", "")
            # Anode markers in a distinct color
            if purpose == "anode":
                text_fill = "#ffcc00"
                font_weight = "bold"
            else:
                text_fill = "#ccc"
                font_weight = "normal"
            parts.append(
                f'<text x="{sx:.1f}" y="{sy + fh * 0.35:.1f}" '
                f'text-anchor="middle" font-size="{fh:.1f}px" '
                f'fill="{text_fill}" font-family="monospace" font-weight="{font_weight}" '
                f'opacity="{silk_opacity}" pointer-events="none" class="silkscreen">'
                f'{silk["text"]}</text>'
            )
        elif silk["type"] == "dot":
            dr = silk.get("diameter_mm", 0.5) / 2 * scale
            parts.append(
                f'<circle cx="{sx:.1f}" cy="{sy:.1f}" r="{dr:.1f}" '
                f'fill="#fff" opacity="{silk_opacity}" pointer-events="none" class="silkscreen"/>'
            )

    # Components
    for item in items:
        des = item["designator"]
        ctype = item.get("component_type", "unknown")
        pkg = item.get("package", "?")
        x_mm = item["x_mm"]
        y_mm = item["y_mm"]
        w_mm = item["footprint_width_mm"]
        h_mm = item["footprint_height_mm"]
        rot = item.get("rotation_deg", 0)
        layer = item.get("layer", "top")
        source = item.get("placement_source", "?")

        # Swap dimensions for 90/270 rotation
        dw, dh = w_mm, h_mm
        if rot in (90, 270):
            dw, dh = h_mm, w_mm

        fill = TYPE_COLORS.get(ctype, DEFAULT_COLOR)
        opacity = "0.65" if layer == "top" else "0.4"

        # Convert to SVG coords (flip Y axis)
        cx = margin + x_mm * scale
        cy = margin + (board_h - y_mm) * scale
        rx = dw * scale / 2
        ry = dh * scale / 2

        # Enrich with BOM data
        bom_item = bom_lookup.get(des, {})
        value = bom_item.get("value", "")
        specs = bom_item.get("specs", {})
        specs_str = _format_specs(specs) if specs else ""
        description = bom_item.get("description", "")

        # Data attributes for custom tooltip
        # Escape quotes in values for HTML attributes
        def _esc(s: str) -> str:
            return str(s).replace('"', '&quot;').replace('<', '&lt;').replace('>', '&gt;')

        data_attrs = (
            f'data-des="{_esc(des)}" data-type="{_esc(ctype)}" data-pkg="{_esc(pkg)}" '
            f'data-pos="({x_mm:.1f}, {y_mm:.1f})" data-size="{w_mm:.1f} x {h_mm:.1f}" '
            f'data-rot="{rot}" data-layer="{layer}" data-source="{source}" '
            f'data-value="{_esc(value)}" data-specs="{_esc(specs_str)}" '
            f'data-desc="{_esc(description)}"'
        )

        if ctype == "fiducial":
            # Draw fiducials as circles
            r = w_mm * scale / 2
            parts.append(
                f'<circle cx="{cx:.1f}" cy="{cy:.1f}" r="{r:.1f}" '
                f'fill="none" stroke="#888" stroke-width="1.5" stroke-dasharray="4,2" '
                f'opacity="{opacity}" class="component" {data_attrs}/>'
            )
            # Inner dot
            parts.append(
                f'<circle cx="{cx:.1f}" cy="{cy:.1f}" r="{scale * 0.5:.1f}" '
                f'fill="#888" opacity="{opacity}" pointer-events="none"/>'
            )
        else:
            # Draw component rectangle
            parts.append(
                f'<rect x="{cx - rx:.1f}" y="{cy - ry:.1f}" '
                f'width="{dw * scale:.1f}" height="{dh * scale:.1f}" '
                f'fill="{fill}" stroke="#fff" stroke-width="1" '
                f'opacity="{opacity}" rx="1" class="component" {data_attrs}/>'
            )

            # Pin 1 indicator (small dot at top-left of original orientation)
            if rot == 0:
                px, py = cx - rx + 2, cy - ry + 2
            elif rot == 90:
                px, py = cx - rx + 2, cy + ry - 2
            elif rot == 180:
                px, py = cx + rx - 2, cy + ry - 2
            else:
                px, py = cx + rx - 2, cy - ry + 2
            parts.append(
                f'<circle cx="{px:.1f}" cy="{py:.1f}" r="1.5" fill="#fff" opacity="0.7"/>'
            )

        # Label
        font_size = min(8, max(5, min(dw, dh) * scale * 0.35))
        parts.append(
            f'<text x="{cx:.1f}" y="{cy + font_size * 0.35:.1f}" '
            f'text-anchor="middle" font-size="{font_size:.1f}px" '
            f'fill="#fff" font-family="monospace" font-weight="bold" '
            f'pointer-events="none">{des}</text>'
        )

    # Pads — render ALL physical pins from footprint definitions (not just netlist ports)
    # This ensures components like 6mm_tactile (4 pins, 2 ports) show all pads.
    _TH_PKGS = ("DIP", "PinHeader", "PJ-002A", "TO-220", "HC49", "6mm_tactile")
    try:
        for item in items:
            des = item["designator"]
            if item.get("component_type") == "fiducial":
                continue
            pkg = item.get("package", "")
            cx_mm = item["x_mm"]
            cy_mm = item["y_mm"]
            rot = item.get("rotation_deg", 0)
            comp_layer = item.get("layer", "top")
            fw = item.get("footprint_width_mm", 2.0)
            fh = item.get("footprint_height_mm", 2.0)

            # Get footprint definition for physical pin positions
            # Try with a generous pin count guess first
            fp_def = get_footprint_def(pkg, 99)
            if fp_def is None:
                # Try with netlist port count
                if netlist:
                    comp_id = None
                    for elem in netlist.get("elements", []):
                        if elem.get("element_type") == "component" and elem.get("designator") == des:
                            comp_id = elem["component_id"]
                            break
                    if comp_id:
                        port_count = sum(1 for e in netlist.get("elements", [])
                                         if e.get("element_type") == "port" and e.get("component_id") == comp_id)
                        fp_def = get_footprint_def(pkg, port_count)
                if fp_def is None:
                    fp_def = _generate_fallback_footprint(fw, fh, 2)

            is_th = any(pkg.startswith(p) for p in _TH_PKGS)
            pw_mm, ph_mm = fp_def.pad_size

            for pin_num, (dx, dy) in sorted(fp_def.pin_offsets.items()):
                # Apply rotation
                rdx, rdy = _rotate_offset(dx, dy, rot)
                abs_x = cx_mm + rdx
                abs_y = cy_mm + rdy

                px = margin + abs_x * scale
                py = margin + (board_h - abs_y) * scale

                if is_th:
                    # Through-hole pad: circle with drill hole
                    pad_dia = max(pw_mm, ph_mm)
                    r = max(pad_dia * scale / 2, 3.0)  # min 3px radius
                    drill_r = r * 0.45
                    parts.append(
                        f'<circle cx="{px:.1f}" cy="{py:.1f}" r="{r:.1f}" '
                        f'fill="#c8a84e" stroke="#a08030" stroke-width="0.5" '
                        f'opacity="0.95" pointer-events="none"/>'
                    )
                    parts.append(
                        f'<circle cx="{px:.1f}" cy="{py:.1f}" r="{drill_r:.1f}" '
                        f'fill="#0f0f1a" pointer-events="none"/>'
                    )
                else:
                    # SMD pad: rectangle
                    rpw, rph = pw_mm, ph_mm
                    if rot in (90, 270):
                        rpw, rph = rph, rpw
                    spw = max(rpw * scale, 2.0)
                    sph = max(rph * scale, 2.0)
                    pad_opacity = "0.9" if comp_layer == "top" else "0.5"
                    parts.append(
                        f'<rect x="{px - spw / 2:.1f}" y="{py - sph / 2:.1f}" '
                        f'width="{spw:.1f}" height="{sph:.1f}" '
                        f'fill="#c8a84e" stroke="#9a7b2e" stroke-width="0.3" '
                        f'opacity="{pad_opacity}" rx="0.5" pointer-events="none"/>'
                    )
    except Exception:
        pass  # pad rendering is optional — don't break visualization

    # Origin marker
    parts.append(
        f'<circle cx="{margin}" cy="{margin + board_h * scale}" r="3" fill="#ff0" opacity="0.6"/>'
    )
    parts.append(
        f'<text x="{margin + 5}" y="{margin + board_h * scale - 3}" '
        f'font-size="7px" fill="#ff0" opacity="0.6" font-family="monospace">(0,0)</text>'
    )

    # Dimension labels
    parts.append(
        f'<text x="{margin + board_w * scale / 2}" y="{margin + board_h * scale + 15}" '
        f'text-anchor="middle" font-size="9px" fill="#888" font-family="monospace">'
        f'{board_w:.1f}mm</text>'
    )
    parts.append(
        f'<text x="{margin - 5}" y="{margin + board_h * scale / 2}" '
        f'text-anchor="end" font-size="9px" fill="#888" font-family="monospace" '
        f'transform="rotate(-90, {margin - 5}, {margin + board_h * scale / 2})">'
        f'{board_h:.1f}mm</text>'
    )

    svg = (
        f'<svg xmlns="http://www.w3.org/2000/svg" '
        f'viewBox="0 0 {svg_w:.0f} {svg_h:.0f}" '
        f'width="{svg_w:.0f}" height="{svg_h:.0f}" '
        f'id="board-svg">\n'
        + "\n".join(parts)
        + "\n</svg>"
    )
    return svg


def _actions_html(routed: dict | None, netlist: dict | None, api_url: str) -> str:
    """Build the actions section with Export, Import, and Continue buttons."""
    if not routed:
        return ""

    project = routed.get("project_name", "board")

    html = '''<div style="margin-bottom:16px">
      <div class="legend-title">Actions</div>
      <div style="display:flex;flex-direction:column;gap:8px;margin-top:8px">'''

    # Export/Import row
    html += '''
        <div style="display:flex;gap:8px">
          <button id="btn-export" onclick="exportKicad()" style="flex:1;padding:6px 12px;background:#2563eb;color:#fff;border:none;border-radius:4px;cursor:pointer;font-size:12px">Export KiCad</button>
          <button id="btn-import" onclick="importKicad()" style="flex:1;padding:6px 12px;background:#7c3aed;color:#fff;border:none;border-radius:4px;cursor:pointer;font-size:12px">Import KiCad</button>
        </div>'''

    # Hidden file input for import
    html += '''
        <input type="file" id="kicad-file-input" accept=".kicad_pcb" style="display:none"/>'''

    # Continue button (prominent green)
    html += '''
        <button id="btn-continue" onclick="continueToNext()" style="padding:10px 16px;background:#16a34a;color:#fff;border:none;border-radius:6px;cursor:pointer;font-size:14px;font-weight:600;letter-spacing:0.3px">
          &#9654; Continue to DRC &amp; Export
        </button>'''

    # Status indicator
    html += f'''
        <div id="action-status" style="font-size:11px;color:#888;text-align:center"></div>
      </div>
    </div>'''

    return html


def _drc_panel_html(drc_report: dict | None) -> str:
    """Build DRC results panel for the side panel."""
    if not drc_report:
        return ""

    passed = drc_report.get("passed", False)
    stats = drc_report.get("statistics", {})
    badge_color = "#22c55e" if passed else "#ef4444"
    badge_text = "PASSED" if passed else "FAILED"
    mfg = drc_report.get("dfm_profile", drc_report.get("manufacturer", "generic"))

    html = f'''<div style="margin-bottom:16px">
      <div class="legend-title">DRC Report</div>
      <div style="display:flex;align-items:center;gap:8px;margin-bottom:6px">
        <span style="background:{badge_color};color:#fff;padding:2px 8px;border-radius:3px;font-size:11px;font-weight:600">{badge_text}</span>
        <span style="font-size:11px;color:#888">{drc_report.get("summary", "")}</span>
      </div>
      <div style="font-size:11px;color:#888;margin-bottom:6px">DFM: {mfg}</div>'''

    # Per-category summary
    categories: dict[str, tuple[int, int]] = {}  # category -> (passed, total)
    for check in drc_report.get("checks", []):
        cat = check.get("category", "other")
        p, t = categories.get(cat, (0, 0))
        categories[cat] = (p + (1 if check["passed"] else 0), t + 1)

    CAT_LABELS = {"electrical": "Electrical", "dfm": "DFM", "current": "Current", "mechanical": "Mechanical"}
    for cat, (p, t) in sorted(categories.items()):
        cat_color = "#22c55e" if p == t else "#ef4444"
        html += f'<div style="font-size:11px"><span style="color:{cat_color}">{CAT_LABELS.get(cat, cat)}: {p}/{t}</span></div>'

    # Collapsible violations list
    failed_checks = [c for c in drc_report.get("checks", []) if not c["passed"]]
    if failed_checks:
        rows = []
        for check in failed_checks:
            for v in check["violations"][:5]:
                sev = v.get("severity", "error")
                sev_color = "#ef4444" if sev == "error" else "#f59e0b"
                rows.append(
                    f'<tr><td style="color:{sev_color}">{sev[0].upper()}</td>'
                    f'<td>{check["rule"]}</td>'
                    f'<td style="font-size:10px">{v.get("message", "")}</td></tr>'
                )
            remaining = len(check["violations"]) - 5
            if remaining > 0:
                rows.append(f'<tr><td></td><td></td><td style="color:#888;font-size:10px">+{remaining} more</td></tr>')

        html += f'''
      <details style="margin-top:6px">
        <summary style="cursor:pointer;font-size:11px;color:#f59e0b">Violations ({stats.get("errors", 0)} errors, {stats.get("warnings", 0)} warnings)</summary>
        <table style="font-size:10px;margin-top:4px">
          <tr><th></th><th>Rule</th><th>Detail</th></tr>
          {"".join(rows)}
        </table>
      </details>'''

    html += '\n    </div>'
    return html


def generate_html(
    placement: dict,
    netlist: dict | None = None,
    bom: dict | None = None,
    routed: dict | None = None,
    title: str = "",
    api_url: str = "",
    drc_report: dict | None = None,
    embed_mode: bool = False,
) -> str:
    """Generate a self-contained HTML page with the board visualization."""
    board = placement.get("board", {})
    board_w = board.get("width_mm", 50)
    board_h = board.get("height_mm", 30)
    items = placement.get("placements", [])
    project = placement.get("project_name", "PCB")

    n_components = len([i for i in items if i.get("component_type") != "fiducial"])
    n_fiducials = len([i for i in items if i.get("component_type") == "fiducial"])

    svg = generate_svg(placement, netlist, bom, routed)

    # Build legend entries
    types_present = sorted(set(i.get("component_type", "?") for i in items))
    legend_items = []
    for t in types_present:
        color = TYPE_COLORS.get(t, DEFAULT_COLOR)
        count = sum(1 for i in items if i.get("component_type") == t)
        legend_items.append(
            f'<span style="display:inline-flex;align-items:center;gap:4px;margin-right:12px">'
            f'<span style="width:12px;height:12px;background:{color};border-radius:2px;display:inline-block"></span>'
            f'{t} ({count})</span>'
        )

    # Component table
    bom_lookup = _build_bom_lookup(bom)
    table_rows = []
    for item in sorted(items, key=lambda i: i["designator"]):
        bom_item = bom_lookup.get(item["designator"], {})
        value = bom_item.get("value", "")
        table_rows.append(
            f'<tr>'
            f'<td>{item["designator"]}</td>'
            f'<td>{item.get("component_type", "?")}</td>'
            f'<td style="color:#adf">{value}</td>'
            f'<td>{item.get("package", "?")}</td>'
            f'<td>({item["x_mm"]:.1f}, {item["y_mm"]:.1f})</td>'
            f'<td>{item.get("rotation_deg", 0)}</td>'
            f'<td>{item.get("layer", "?")}</td>'
            f'</tr>'
        )

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>{project} — PCB Layout</title>
<style>
  * {{ margin: 0; padding: 0; box-sizing: border-box; }}
  body {{ background: #0f0f1a; color: #e0e0e0; font-family: -apple-system, sans-serif; }}
  .header {{ padding: 16px 24px; border-bottom: 1px solid #2a2a4a; display: flex; justify-content: space-between; align-items: center; }}
  .header h1 {{ font-size: 18px; font-weight: 600; }}
  .header .stats {{ font-size: 13px; color: #888; }}
  .progress-bar {{ height: 4px; background: #1a1a2e; }}
  .progress-fill {{ height: 100%; transition: width 0.3s; }}
  .main {{ display: flex; height: calc(100vh - 57px); }}
  .board-panel {{ flex: 1; overflow: hidden; display: flex; align-items: center; justify-content: center; background: #0a0a15; cursor: grab; }}
  .board-panel:active {{ cursor: grabbing; }}
  .side-panel {{ width: 320px; border-left: 1px solid #2a2a4a; overflow-y: auto; padding: 16px; }}
  .legend {{ font-size: 12px; line-height: 2; margin-bottom: 16px; }}
  .legend-title {{ font-size: 13px; font-weight: 600; margin-bottom: 6px; color: #aaa; }}
  table {{ width: 100%; border-collapse: collapse; font-size: 11px; }}
  th {{ text-align: left; padding: 4px 6px; border-bottom: 1px solid #2a2a4a; color: #888; font-weight: 500; }}
  td {{ padding: 3px 6px; border-bottom: 1px solid #1a1a2e; }}
  tr:hover td {{ background: #1a1a3e; }}
  .component:hover {{ opacity: 1 !important; stroke-width: 2.5; filter: brightness(1.3); cursor: pointer; }}
  .trace-hit {{ cursor: crosshair; }}
  .trace-hit:hover + .trace {{ filter: brightness(1.6); opacity: 1 !important; }}
  .ratsnest-toggle {{ margin-bottom: 12px; }}
  .ratsnest-toggle label {{ font-size: 12px; cursor: pointer; }}
  .net-legend {{ font-size: 11px; margin-bottom: 16px; }}
  .net-legend span {{ margin-right: 10px; }}
  #tooltip {{
    position: fixed; pointer-events: none; z-index: 1000;
    background: #1e1e32; border: 1px solid #4a4a7a; border-radius: 6px;
    padding: 10px 14px; font-size: 12px; font-family: monospace;
    color: #e0e0e0; display: none; box-shadow: 0 4px 16px rgba(0,0,0,0.5);
    max-width: 280px; line-height: 1.6;
  }}
  #tooltip .tt-des {{ font-size: 15px; font-weight: bold; color: #fff; margin-bottom: 4px; }}
  #tooltip .tt-row {{ display: flex; gap: 8px; }}
  #tooltip .tt-label {{ color: #888; min-width: 60px; }}
  #tooltip .tt-val {{ color: #ccc; }}
  #tooltip .tt-color {{ display: inline-block; width: 10px; height: 10px; border-radius: 2px; margin-right: 4px; vertical-align: middle; }}
</style>
</head>
<body>
<div class="header">
  <h1>{project}</h1>
  <div class="stats">{board_w:.1f} x {board_h:.1f}mm &bull; {n_components} components &bull; {n_fiducials} fiducials{_header_routing_stat(routed)}</div>
</div>
{_progress_bar_html(routed)}
<div id="tooltip"></div>
<div class="main">
  <div class="board-panel" id="boardPanel">
    <div id="svgContainer" style="transform-origin: center center;">
      {svg}
    </div>
  </div>
  <div class="side-panel">
    <div class="legend">
      <div class="legend-title">Component Types</div>
      {"".join(legend_items)}
    </div>
    {"" if not netlist else '''
    <div class="net-legend">
      <div class="legend-title">Ratsnest</div>
      <span><span style="color:#ef4444">---</span> power</span>
      <span><span style="color:#3b82f6">---</span> ground</span>
      <span><span style="color:#a3a3a3">---</span> signal</span>
    </div>
    '''}
    {_routing_stats_html(routed, netlist)}
    {_drc_panel_html(drc_report)}
    {'' if embed_mode else _actions_html(routed, netlist, api_url)}
    {_kicad_export_html(routed, netlist)}
    <div class="legend-title">Components</div>
    <table>
      <thead><tr><th>Ref</th><th>Type</th><th>Value</th><th>Pkg</th><th>Pos</th><th>Rot</th><th>Layer</th></tr></thead>
      <tbody>{"".join(table_rows)}</tbody>
    </table>
  </div>
</div>
<script>
  // Pan & zoom
  const panel = document.getElementById('boardPanel');
  const container = document.getElementById('svgContainer');
  let scale = 1, panX = 0, panY = 0, dragging = false, lastX, lastY;

  function updateTransform() {{
    container.style.transform = `translate(${{panX}}px, ${{panY}}px) scale(${{scale}})`;
  }}

  panel.addEventListener('wheel', (e) => {{
    e.preventDefault();
    const delta = e.deltaY > 0 ? 0.9 : 1.1;
    scale = Math.max(0.2, Math.min(10, scale * delta));
    updateTransform();
  }});

  panel.addEventListener('mousedown', (e) => {{
    dragging = true; lastX = e.clientX; lastY = e.clientY;
  }});

  window.addEventListener('mousemove', (e) => {{
    if (!dragging) return;
    panX += e.clientX - lastX;
    panY += e.clientY - lastY;
    lastX = e.clientX; lastY = e.clientY;
    updateTransform();
  }});

  window.addEventListener('mouseup', () => {{ dragging = false; }});

  // Fit to view on load (robust retry for iframe embedding)
  const svg = document.getElementById('board-svg');
  function fitToView() {{
    const svgW = svg.viewBox.baseVal.width;
    const svgH = svg.viewBox.baseVal.height;
    const panelRect = panel.getBoundingClientRect();
    if (panelRect.width < 50 || panelRect.height < 50) {{
      setTimeout(fitToView, 100);
      return;
    }}
    scale = Math.min(panelRect.width / svgW, panelRect.height / svgH) * 0.92;
    panX = (panelRect.width - svgW * scale) / 2;
    panY = (panelRect.height - svgH * scale) / 2;
    updateTransform();
  }}
  // Multiple attempts — iframe may not have final dimensions immediately
  fitToView();
  setTimeout(fitToView, 200);
  setTimeout(fitToView, 600);
  window.addEventListener('resize', fitToView);
  if (typeof ResizeObserver !== 'undefined') {{
    new ResizeObserver(() => fitToView()).observe(panel);
  }}

  // Custom tooltip
  const typeColors = {json.dumps(TYPE_COLORS)};
  const tooltip = document.getElementById('tooltip');

  document.querySelectorAll('.component').forEach(el => {{
    el.addEventListener('mouseenter', (e) => {{
      const d = el.dataset;
      const color = typeColors[d.type] || '#94a3b8';
      const valueRow = d.value ? `<div class="tt-row"><span class="tt-label">Value</span><span class="tt-val" style="color:#8bf">${{d.value}}</span></div>` : '';
      const specsRow = d.specs ? `<div class="tt-row"><span class="tt-label">Specs</span><span class="tt-val" style="font-size:10px">${{d.specs}}</span></div>` : '';
      const descRow = d.desc ? `<div style="margin-top:4px;font-size:10px;color:#777;border-top:1px solid #2a2a4a;padding-top:4px">${{d.desc}}</div>` : '';
      tooltip.innerHTML = `
        <div class="tt-des"><span class="tt-color" style="background:${{color}}"></span>${{d.des}}</div>
        ${{valueRow}}
        <div class="tt-row"><span class="tt-label">Type</span><span class="tt-val">${{d.type}}</span></div>
        <div class="tt-row"><span class="tt-label">Package</span><span class="tt-val">${{d.pkg}}</span></div>
        ${{specsRow}}
        <div class="tt-row"><span class="tt-label">Position</span><span class="tt-val">${{d.pos}} mm</span></div>
        <div class="tt-row"><span class="tt-label">Size</span><span class="tt-val">${{d.size}} mm</span></div>
        <div class="tt-row"><span class="tt-label">Rotation</span><span class="tt-val">${{d.rot}}&deg;</span></div>
        <div class="tt-row"><span class="tt-label">Layer</span><span class="tt-val">${{d.layer}}</span></div>
        ${{descRow}}
      `;
      tooltip.style.display = 'block';
    }});
    el.addEventListener('mousemove', (e) => {{
      tooltip.style.left = (e.clientX + 14) + 'px';
      tooltip.style.top = (e.clientY + 14) + 'px';
    }});
    el.addEventListener('mouseleave', () => {{
      tooltip.style.display = 'none';
    }});
  }});

  // Trace tooltips (lower priority than components — transparent hit targets)
  const NET_CLASS_LABELS = {{ 'power': 'Power', 'ground': 'Ground', 'signal': 'Signal' }};
  const NET_CLASS_COLORS = {{ 'power': '#ef4444', 'ground': '#3b82f6', 'signal': '#a3a3a3' }};
  document.querySelectorAll('.trace-hit').forEach(el => {{
    el.addEventListener('mouseenter', (e) => {{
      const d = el.dataset;
      const nc = d.traceClass || 'signal';
      const ncLabel = NET_CLASS_LABELS[nc] || nc;
      const ncColor = NET_CLASS_COLORS[nc] || '#a3a3a3';
      tooltip.innerHTML = `
        <div class="tt-des" style="font-size:13px"><span class="tt-color" style="background:${{ncColor}}"></span>${{d.traceNet}}</div>
        <div class="tt-row"><span class="tt-label">Net class</span><span class="tt-val">${{ncLabel}}</span></div>
        <div class="tt-row"><span class="tt-label">Width</span><span class="tt-val">${{d.traceWidth}} mm</span></div>
        <div class="tt-row"><span class="tt-label">Layer</span><span class="tt-val">${{d.traceLayer}}</span></div>
        <div class="tt-row"><span class="tt-label">Segment</span><span class="tt-val">${{d.traceLen}} mm</span></div>
      `;
      tooltip.style.display = 'block';
    }});
    el.addEventListener('mousemove', (e) => {{
      tooltip.style.left = (e.clientX + 14) + 'px';
      tooltip.style.top = (e.clientY + 14) + 'px';
    }});
    el.addEventListener('mouseleave', () => {{
      tooltip.style.display = 'none';
    }});
  }});

  // --- Action buttons: server-aware Export/Import/Continue ---
  const API_URL = '{api_url}';
  const statusEl = document.getElementById('action-status');
  let serverAvailable = false;

  // Check if approval server is running
  if (API_URL) {{
    fetch(API_URL + '/status')
      .then(r => r.json())
      .then(data => {{
        serverAvailable = true;
        if (statusEl) statusEl.textContent = 'Connected to pipeline';
        if (statusEl) statusEl.style.color = '#22c55e';
      }})
      .catch(() => {{
        if (statusEl) statusEl.textContent = 'Viewing saved file (pipeline not running)';
        if (statusEl) statusEl.style.color = '#888';
        // Disable continue/import if no server
        const btnContinue = document.getElementById('btn-continue');
        const btnImport = document.getElementById('btn-import');
        if (btnContinue) {{
          btnContinue.style.background = '#374151';
          btnContinue.style.cursor = 'not-allowed';
          btnContinue.title = 'Pipeline not running — open from the CLI to enable';
        }}
        if (btnImport) {{
          btnImport.style.background = '#374151';
          btnImport.style.cursor = 'not-allowed';
          btnImport.title = 'Pipeline not running — use CLI: pcb-creator import-kicad';
        }}
      }});
  }}

  function continueToNext() {{
    if (!serverAvailable) {{
      alert('Pipeline is not running.\\nRun the pipeline from the CLI to enable this button.');
      return;
    }}
    const btn = document.getElementById('btn-continue');
    btn.textContent = 'Continuing...';
    btn.style.background = '#15803d';
    btn.disabled = true;
    fetch(API_URL + '/continue', {{ method: 'POST' }})
      .then(r => r.json())
      .then(data => {{
        if (statusEl) statusEl.textContent = 'Pipeline continuing...';
        if (statusEl) statusEl.style.color = '#22c55e';
        btn.textContent = '\\u2714 Approved';
      }})
      .catch(e => {{
        btn.textContent = '\\u2718 Error';
        btn.style.background = '#dc2626';
        if (statusEl) statusEl.textContent = 'Error: ' + e.message;
      }});
  }}

  function importKicad() {{
    if (!serverAvailable) {{
      alert('Pipeline is not running.\\nUse CLI: pcb-creator import-kicad --project <name> --kicad-file <path>');
      return;
    }}
    document.getElementById('kicad-file-input').click();
  }}

  // File input handler for KiCad import
  const fileInput = document.getElementById('kicad-file-input');
  if (fileInput) {{
    fileInput.addEventListener('change', (e) => {{
      const file = e.target.files[0];
      if (!file) return;
      if (statusEl) statusEl.textContent = 'Importing ' + file.name + '...';
      if (statusEl) statusEl.style.color = '#f59e0b';
      const reader = new FileReader();
      reader.onload = () => {{
        fetch(API_URL + '/import', {{
          method: 'POST',
          headers: {{ 'Content-Type': 'application/json' }},
          body: JSON.stringify({{ filename: file.name, content: reader.result }}),
        }})
        .then(r => r.json())
        .then(data => {{
          if (data.status === 'ok') {{
            if (statusEl) statusEl.textContent = 'Imported! Reloading...';
            if (statusEl) statusEl.style.color = '#22c55e';
            setTimeout(() => window.location.reload(), 500);
          }} else {{
            if (statusEl) statusEl.textContent = 'Import error: ' + data.message;
            if (statusEl) statusEl.style.color = '#ef4444';
          }}
        }})
        .catch(err => {{
          if (statusEl) statusEl.textContent = 'Import failed: ' + err.message;
          if (statusEl) statusEl.style.color = '#ef4444';
        }});
      }};
      reader.readAsText(file);
      fileInput.value = '';
    }});
  }}
</script>
</body>
</html>"""
    return html


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Visualize PCB placement as HTML/SVG")
    parser.add_argument("placement", type=Path, help="Path to placement JSON")
    parser.add_argument("--netlist", type=Path, default=None, help="Path to netlist JSON (for ratsnest)")
    parser.add_argument("--bom", type=Path, default=None, help="Path to BOM JSON (for values and specs)")
    parser.add_argument("--routed", type=Path, default=None, help="Path to routed JSON (for trace visualization)")
    parser.add_argument("--output", "-o", type=Path, default=None, help="Output HTML path (default: <placement>_view.html)")
    parser.add_argument("--open", action="store_true", help="Open in browser after generating")
    args = parser.parse_args(argv)

    placement = json.loads(args.placement.read_text())
    netlist = None
    if args.netlist:
        netlist = json.loads(args.netlist.read_text())

    bom = None
    if args.bom:
        bom = json.loads(args.bom.read_text())

    routed = None
    if args.routed:
        routed = json.loads(args.routed.read_text())

    output = args.output or args.placement.with_suffix("").with_name(args.placement.stem + "_view.html")

    html = generate_html(placement, netlist, bom, routed=routed, title=placement.get("project_name", ""))
    output.write_text(html)
    print(f"Board visualization: {output}")

    if args.open:
        import webbrowser
        webbrowser.open(f"file://{output.resolve()}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
