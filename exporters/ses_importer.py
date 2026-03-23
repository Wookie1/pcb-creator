"""Import Freerouting SES (Session) file into pcb-creator routed format.

Parses the SES file produced by Freerouting, extracts traces and vias,
and merges them with the original placement data to produce a routed dict
matching routed_schema.json.

Reuses the S-expression parser from kicad_importer.py.
"""

from __future__ import annotations

from pathlib import Path

from .kicad_importer import _tokenize, _parse_sexpr


# ---------------------------------------------------------------------------
# SES layer name mapping
# ---------------------------------------------------------------------------

_LAYER_MAP = {
    "F.Cu": "top",
    "B.Cu": "bottom",
    # Some Freerouting versions may output layer indices
    "0": "top",
    "1": "bottom",
}


# ---------------------------------------------------------------------------
# S-expression navigation helpers
# ---------------------------------------------------------------------------

def _find(sexpr: list, name: str) -> list | None:
    """Find first sub-expression starting with 'name'."""
    if not isinstance(sexpr, list):
        return None
    for item in sexpr:
        if isinstance(item, list) and len(item) > 0 and item[0] == name:
            return item
    return None


def _find_all(sexpr: list, name: str) -> list[list]:
    """Find all sub-expressions starting with 'name'."""
    results = []
    if not isinstance(sexpr, list):
        return results
    for item in sexpr:
        if isinstance(item, list) and len(item) > 0 and item[0] == name:
            results.append(item)
    return results


def _find_value(sexpr: list, name: str, default: str = "") -> str:
    """Find (name value) and return value as string."""
    node = _find(sexpr, name)
    if node and len(node) >= 2:
        return str(node[1])
    return default


# ---------------------------------------------------------------------------
# SES parsing
# ---------------------------------------------------------------------------

def _parse_resolution(routes: list) -> float:
    """Extract resolution multiplier from (resolution <unit> <value>).

    Returns a factor to convert SES coordinates to mm.
    """
    res = _find(routes, "resolution")
    if not res or len(res) < 3:
        return 0.001  # default: um with 1000 resolution

    unit = str(res[1]).lower()
    try:
        divisor = float(res[2])
    except (ValueError, IndexError):
        divisor = 1000.0

    if unit == "mm":
        return 1.0 / divisor
    elif unit in ("um", "µm"):
        return 0.001 / divisor
    elif unit == "mil":
        return 0.0254 / divisor
    else:
        return 1.0 / divisor


def _extract_routes(
    tree: list,
    net_name_to_id: dict[str, str],
    scale: float,
    via_drill_mm: float,
    via_diameter_mm: float,
) -> tuple[list[dict], list[dict], set[str]]:
    """Extract traces and vias from SES routes section.

    Returns (traces, vias, routed_net_ids).
    """
    traces: list[dict] = []
    vias: list[dict] = []
    routed_net_ids: set[str] = set()

    # Find (routes ...) section
    session = tree[0] if tree and isinstance(tree[0], list) else tree
    routes = _find(session, "routes")
    if not routes:
        return traces, vias, routed_net_ids

    scale = _parse_resolution(routes)

    # Find (network_out ...) section within routes
    network_out = _find(routes, "network_out")
    if not network_out:
        return traces, vias, routed_net_ids

    for net_node in _find_all(network_out, "net"):
        if len(net_node) < 2:
            continue

        net_name = str(net_node[1])
        net_id = net_name_to_id.get(net_name, "")

        has_wires = False

        # Extract wire paths
        for wire in _find_all(net_node, "wire"):
            path = _find(wire, "path")
            if not path or len(path) < 4:
                continue

            layer_name = str(path[1])
            layer = _LAYER_MAP.get(layer_name, "top")

            try:
                width_mm = float(path[2]) * scale
            except (ValueError, IndexError):
                width_mm = 0.25

            # Path coordinates: x1 y1 x2 y2 ...
            coords = []
            for val in path[3:]:
                try:
                    coords.append(float(val) * scale)
                except ValueError:
                    continue

            # Each pair of consecutive points forms a segment
            for i in range(0, len(coords) - 3, 2):
                traces.append({
                    "start_x_mm": round(coords[i], 4),
                    "start_y_mm": round(coords[i + 1], 4),
                    "end_x_mm": round(coords[i + 2], 4),
                    "end_y_mm": round(coords[i + 3], 4),
                    "width_mm": round(width_mm, 4),
                    "layer": layer,
                    "net_id": net_id,
                    "net_name": net_name,
                })
                has_wires = True

        # Extract vias
        for via_node in _find_all(net_node, "via"):
            if len(via_node) < 4:
                continue

            # (via <padstack_name> <x> <y>)
            try:
                x_mm = float(via_node[2]) * scale
                y_mm = float(via_node[3]) * scale
            except (ValueError, IndexError):
                continue

            vias.append({
                "x_mm": round(x_mm, 4),
                "y_mm": round(y_mm, 4),
                "drill_mm": via_drill_mm,
                "diameter_mm": via_diameter_mm,
                "from_layer": "top",
                "to_layer": "bottom",
                "net_id": net_id,
                "net_name": net_name,
            })
            has_wires = True

        if has_wires and net_id:
            routed_net_ids.add(net_id)

    return traces, vias, routed_net_ids


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def import_ses(
    ses_path: str | Path,
    placement: dict,
    netlist: dict,
    via_drill_mm: float = 0.3,
    via_diameter_mm: float = 0.6,
) -> dict:
    """Import Freerouting SES output and merge with placement data.

    Args:
        ses_path: Path to .ses file from Freerouting.
        placement: Original placement dict (board, placements kept as-is).
        netlist: Netlist dict for net name -> net_id resolution.
        via_drill_mm: Via drill size for imported vias.
        via_diameter_mm: Via pad diameter for imported vias.

    Returns:
        Routed dict matching routed_schema.json format.
        Does NOT include copper_fills (added separately).
    """
    ses_path = Path(ses_path)
    text = ses_path.read_text(encoding="utf-8")

    # Parse S-expression
    tokens = _tokenize(text)
    tree, _ = _parse_sexpr(tokens, 0)

    # Build net_name -> net_id mapping from netlist
    net_name_to_id: dict[str, str] = {}
    all_net_ids: set[str] = set()
    for elem in netlist.get("elements", []):
        if elem.get("element_type") == "net":
            net_name = elem.get("name", elem["net_id"])
            net_name_to_id[net_name] = elem["net_id"]
            all_net_ids.add(elem["net_id"])

    # Extract routing data
    traces, vias, routed_net_ids = _extract_routes(
        tree, net_name_to_id, 1.0,  # scale determined inside
        via_drill_mm, via_diameter_mm,
    )

    # Compute statistics
    total_nets = len(all_net_ids)
    routed_count = len(routed_net_ids)
    unrouted = sorted(all_net_ids - routed_net_ids)

    total_trace_length = 0.0
    for t in traces:
        dx = t["end_x_mm"] - t["start_x_mm"]
        dy = t["end_y_mm"] - t["start_y_mm"]
        total_trace_length += (dx**2 + dy**2) ** 0.5

    # Count layer usage
    top_traces = sum(1 for t in traces if t["layer"] == "top")
    bot_traces = sum(1 for t in traces if t["layer"] == "bottom")

    # Build output dict
    routed = {
        "version": placement.get("version", "1.0"),
        "project_name": placement.get("project_name", ""),
        "source_netlist": placement.get("source_netlist", ""),
        "source_bom": placement.get("source_bom", ""),
        "board": placement.get("board", {}),
        "placements": placement.get("placements", []),
        "routing": {
            "traces": traces,
            "vias": vias,
            "unrouted_nets": unrouted,
            "statistics": {
                "total_nets": total_nets,
                "routed_nets": routed_count,
                "completion_pct": round(100 * routed_count / total_nets, 1) if total_nets else 100,
                "total_trace_length_mm": round(total_trace_length, 1),
                "via_count": len(vias),
                "layer_usage": {
                    "top": top_traces,
                    "bottom": bot_traces,
                },
            },
            "config": {
                "router": "freerouting",
                "via_drill_mm": via_drill_mm,
                "via_diameter_mm": via_diameter_mm,
            },
        },
        "silkscreen": [],  # will be generated separately
    }

    return routed
