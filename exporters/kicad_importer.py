"""Import KiCad .kicad_pcb file back into the pcb-creator pipeline.

Parses traces, vias, and copper fills from a KiCad board file and merges
them into an existing routed JSON structure. Compatible with KiCad 6-9
S-expression format.

The importer is version-tolerant: it searches for known element types
(segment, via, zone) and ignores unrecognized fields, making it compatible
with any KiCad version that uses the S-expression format.
"""

from __future__ import annotations

import math
from pathlib import Path


# ---------------------------------------------------------------------------
# S-expression parser
# ---------------------------------------------------------------------------

def _tokenize(text: str) -> list[str]:
    """Tokenize an S-expression string into a list of tokens.

    Handles: ( ) "quoted strings" and bare_tokens.
    """
    tokens: list[str] = []
    i = 0
    n = len(text)

    while i < n:
        c = text[i]

        # Skip whitespace
        if c in " \t\n\r":
            i += 1
            continue

        # Parentheses
        if c == "(":
            tokens.append("(")
            i += 1
            continue
        if c == ")":
            tokens.append(")")
            i += 1
            continue

        # Quoted string
        if c == '"':
            j = i + 1
            while j < n:
                if text[j] == "\\" and j + 1 < n:
                    j += 2  # skip escaped char
                elif text[j] == '"':
                    break
                else:
                    j += 1
            token = text[i + 1 : j]  # content without quotes
            tokens.append(f'"{token}"')
            i = j + 1
            continue

        # Bare token (word, number, etc.)
        j = i
        while j < n and text[j] not in " \t\n\r()\"":
            j += 1
        tokens.append(text[i:j])
        i = j

    return tokens


def _parse_sexpr(tokens: list[str], pos: int = 0) -> tuple[list, int]:
    """Parse tokens into nested list structure starting from pos.

    Returns (parsed_list, next_position).
    """
    result: list = []
    i = pos

    while i < len(tokens):
        token = tokens[i]

        if token == "(":
            # Recursive: parse sub-expression
            sub, i = _parse_sexpr(tokens, i + 1)
            result.append(sub)
        elif token == ")":
            return result, i + 1
        else:
            # Strip quotes from quoted strings
            if token.startswith('"') and token.endswith('"'):
                result.append(token[1:-1])
            else:
                result.append(token)
            i += 1

    return result, i


def parse_kicad_sexpr(text: str) -> list:
    """Parse a complete KiCad S-expression file into nested lists."""
    tokens = _tokenize(text)
    result, _ = _parse_sexpr(tokens, 0)
    return result


# ---------------------------------------------------------------------------
# Element extraction helpers
# ---------------------------------------------------------------------------

_LAYER_REVERSE = {
    "F.Cu": "top",
    "B.Cu": "bottom",
    "F.SilkS": "top_silk",
    "B.SilkS": "bottom_silk",
}


def _find_field(sexpr: list, field_name: str) -> list | None:
    """Find a sub-expression by its first element name.

    E.g., _find_field(segment_sexpr, "start") finds ["start", "1.0", "2.0"]
    """
    if not isinstance(sexpr, list):
        return None
    for item in sexpr:
        if isinstance(item, list) and len(item) > 0 and item[0] == field_name:
            return item
    return None


def _find_all(sexpr: list, field_name: str) -> list[list]:
    """Find all sub-expressions matching a field name."""
    if not isinstance(sexpr, list):
        return []
    return [item for item in sexpr
            if isinstance(item, list) and len(item) > 0 and item[0] == field_name]


def _to_float(val: str | int | float) -> float:
    """Convert a token to float."""
    try:
        return float(val)
    except (ValueError, TypeError):
        return 0.0


# ---------------------------------------------------------------------------
# Import logic
# ---------------------------------------------------------------------------

def _extract_nets(tree: list) -> dict[int, str]:
    """Extract net declarations: kicad_net_num -> net_name.

    Only present in pre-10 KiCad files (top-level net table). KiCad 10 dropped
    the table and writes net names inline on every element, so this returns {}
    there and net resolution falls back to names (see _net_ref)."""
    nets: dict[int, str] = {}
    for item in tree:
        if isinstance(item, list) and len(item) >= 3 and item[0] == "net":
            try:
                num = int(item[1])
                name = str(item[2])
                nets[num] = name
            except (ValueError, IndexError):
                pass
    return nets


def _net_ref(field: list | None) -> tuple[int, str]:
    """Parse a (net ...) field into (num, name), tolerating both KiCad forms.

    Pre-10 carries a number resolved via the top-level net table — pads
    `(net 3 "GND")`, tracks/vias `(net 3)`. KiCad 10 dropped the table and
    writes the name inline everywhere: `(net "GND")`. A token that parses as an
    int is a number (name via table); otherwise it is the name itself. Either
    part may be absent: `(net 3)` -> (3, ""), `(net "GND")` -> (0, "GND")."""
    if not field or len(field) < 2:
        return 0, ""
    num, name = 0, ""
    try:
        num = int(field[1])
    except (ValueError, TypeError):
        name = str(field[1])
    if len(field) >= 3:  # trailing name token: (net 3 "GND")
        name = str(field[2])
    return num, name


def _extract_segments(tree: list, net_name_to_id: dict[str, str]) -> list[dict]:
    """Extract trace segments from the parsed tree."""
    traces = []
    for item in tree:
        if not isinstance(item, list) or not item or item[0] != "segment":
            continue

        start = _find_field(item, "start")
        end = _find_field(item, "end")
        width = _find_field(item, "width")
        layer = _find_field(item, "layer")
        net = _find_field(item, "net")

        if not (start and end and width and layer):
            continue

        layer_name = _LAYER_REVERSE.get(str(layer[1]), "top")
        net_num, net_name = _net_ref(net)

        # net_id resolved later from name (KiCad 10) or number (via net table)

        traces.append({
            "start_x_mm": round(_to_float(start[1]), 4),
            "start_y_mm": round(_to_float(start[2]), 4),
            "end_x_mm": round(_to_float(end[1]), 4),
            "end_y_mm": round(_to_float(end[2]), 4),
            "width_mm": round(_to_float(width[1]), 4),
            "layer": layer_name,
            "_net_num": net_num,  # resolved to net_id later
            "_net_name": net_name,
        })

    return traces


def _extract_vias(tree: list) -> list[dict]:
    """Extract vias from the parsed tree."""
    vias = []
    for item in tree:
        if not isinstance(item, list) or not item or item[0] != "via":
            continue

        at = _find_field(item, "at")
        size = _find_field(item, "size")
        drill = _find_field(item, "drill")
        net = _find_field(item, "net")

        if not (at and size and drill):
            continue

        net_num, net_name = _net_ref(net)

        vias.append({
            "x_mm": round(_to_float(at[1]), 4),
            "y_mm": round(_to_float(at[2]), 4),
            "diameter_mm": round(_to_float(size[1]), 4),
            "drill_mm": round(_to_float(drill[1]), 4),
            "from_layer": "top",
            "to_layer": "bottom",
            "_net_num": net_num,
            "_net_name": net_name,
        })

    return vias


def _extract_zones(tree: list, net_name_to_id: dict[str, str]) -> list[dict]:
    """Extract copper fill zones from the parsed tree."""
    zones = []
    for item in tree:
        if not isinstance(item, list) or not item or item[0] != "zone":
            continue

        net_field = _find_field(item, "net")
        net_name_field = _find_field(item, "net_name")
        layer_field = _find_field(item, "layer")

        if not layer_field:
            continue

        layer_name = _LAYER_REVERSE.get(str(layer_field[1]), "top")
        net_num, ref_name = _net_ref(net_field)
        # Pre-10 carries the name in a separate (net_name ...); KiCad 10 inlines
        # it in (net ...). Prefer whichever is present.
        net_name = str(net_name_field[1]) if net_name_field else ref_name

        # Extract filled polygons
        polygons = []
        for fp in _find_all(item, "filled_polygon"):
            pts = _find_field(fp, "pts")
            if not pts:
                continue
            poly = []
            for xy in _find_all(pts, "xy"):
                if len(xy) >= 3:
                    poly.append([round(_to_float(xy[1]), 3), round(_to_float(xy[2]), 3)])
            if poly:
                polygons.append(poly)

        if polygons:
            zones.append({
                "layer": layer_name,
                "net_name": net_name,
                "_net_num": net_num,
                "_net_name": net_name,
                "polygons": polygons,
            })

    return zones


def _compute_statistics(
    traces: list[dict],
    vias: list[dict],
    fills: list[dict],
    total_nets: int,
    routed_net_ids: set[str],
    unrouted: list[str],
) -> dict:
    """Compute routing statistics from imported data."""
    total_length = 0.0
    top_length = 0.0
    bot_length = 0.0

    for t in traces:
        dx = t["end_x_mm"] - t["start_x_mm"]
        dy = t["end_y_mm"] - t["start_y_mm"]
        seg_len = math.sqrt(dx * dx + dy * dy)
        total_length += seg_len
        if t["layer"] == "top":
            top_length += seg_len
        else:
            bot_length += seg_len

    fill_poly_count = sum(len(f.get("polygons", [])) for f in fills)
    fill_layers = list({f["layer"] for f in fills})

    routed_count = len(routed_net_ids)
    completion = round(100.0 * routed_count / total_nets, 1) if total_nets > 0 else 0.0

    return {
        "total_nets": total_nets,
        "routed_nets": routed_count,
        "unrouted_nets": total_nets - routed_count,
        "completion_pct": completion,
        "total_trace_length_mm": round(total_length, 1),
        "via_count": len(vias),
        "layer_usage": {
            "top_trace_length_mm": round(top_length, 1),
            "bottom_trace_length_mm": round(bot_length, 1),
        },
        "copper_fill_polygons": fill_poly_count,
        "copper_fill_layers": fill_layers,
    }


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def import_kicad_pcb(
    kicad_path: str | Path,
    original_routed: dict,
    netlist: dict,
) -> dict:
    """Import a KiCad .kicad_pcb file and merge with original routed data.

    Extracts traces, vias, and copper fills from the KiCad file.
    Keeps placements, board outline, and silkscreen from the original.

    Args:
        kicad_path: Path to the .kicad_pcb file.
        original_routed: The original routed JSON dict.
        netlist: The netlist JSON dict.

    Returns:
        Updated routed JSON dict with imported routing data.
    """
    kicad_path = Path(kicad_path)
    text = kicad_path.read_text(encoding="utf-8")

    # Parse S-expressions
    tree = parse_kicad_sexpr(text)

    # The file is wrapped: [["kicad_pcb", ...]]
    if tree and isinstance(tree[0], list) and tree[0] and tree[0][0] == "kicad_pcb":
        tree = tree[0]

    # Extract net declarations from KiCad file
    kicad_nets = _extract_nets(tree)  # num -> name

    # Build net_name -> net_id mapping from our netlist
    elements = netlist.get("elements", [])
    net_name_to_id: dict[str, str] = {}
    net_id_to_name: dict[str, str] = {}
    all_net_ids: set[str] = set()
    for elem in elements:
        if elem.get("element_type") == "net":
            nid = elem["net_id"]
            name = elem.get("name", nid)
            net_name_to_id[name] = nid
            net_id_to_name[nid] = name
            all_net_ids.add(nid)

    # Build kicad_num -> net_id mapping
    kicad_num_to_net_id: dict[int, str] = {}
    for num, name in kicad_nets.items():
        if name in net_name_to_id:
            kicad_num_to_net_id[num] = net_name_to_id[name]

    # Extract routing elements
    traces = _extract_segments(tree, net_name_to_id)
    vias = _extract_vias(tree)
    fills = _extract_zones(tree, net_name_to_id)

    # KiCad's file frame is Y-DOWN; pcb-creator's internal frame is Y-UP
    # (the Gerber convention; kicad_exporter writes y_kicad = board_h - y).
    # Convert everything read back so the round trip is the identity, and a
    # board edited in KiCad isn't re-imported as its own mirror image.
    _h = float((original_routed or {}).get("board", {}).get("height_mm", 0.0) or 0.0)
    if not _h:   # no board record: take the height from the file's Edge.Cuts
        ys = [_to_float(xy[2]) for it in tree if isinstance(it, list) and it
              and it[0] in ("gr_line", "gr_rect")
              and str((_find_field(it, "layer") or ["", ""])[1]) == "Edge.Cuts"
              for key in ("start", "end") if (xy := _find_field(it, key))]
        _h = max(ys) + min(ys) if ys else 0.0   # flip about the outline's centre
    for t in (traces if _h > 0 else []):
        t["start_y_mm"] = round(_h - t["start_y_mm"], 4)
        t["end_y_mm"] = round(_h - t["end_y_mm"], 4)
    for v in (vias if _h > 0 else []):
        v["y_mm"] = round(_h - v["y_mm"], 4)
    for f in (fills if _h > 0 else []):
        f["polygons"] = [[[x, round(_h - y, 3)] for x, y in poly]
                         for poly in f.get("polygons", [])]

    # Resolve _net_num to net_id/net_name on all elements
    routed_net_ids: set[str] = set()

    # Resolve to net_id by NAME first (KiCad 10, no net table), then by number
    # (pre-10, via the table). Name wins because it is unambiguous across formats.
    def _resolve(elem: dict) -> tuple[str, str]:
        num = elem.pop("_net_num", 0)
        name = elem.pop("_net_name", "")
        net_id = net_name_to_id.get(name, "") if name else ""
        if not net_id and num:
            net_id = kicad_num_to_net_id.get(num, "")
        return net_id, net_id_to_name.get(net_id, name or kicad_nets.get(num, ""))

    for t in traces:
        net_id, net_name = _resolve(t)
        t["net_id"] = net_id
        t["net_name"] = net_name
        if net_id:
            routed_net_ids.add(net_id)

    for v in vias:
        net_id, net_name = _resolve(v)
        v["net_id"] = net_id
        v["net_name"] = net_name
        if net_id:
            routed_net_ids.add(net_id)

    for f in fills:
        net_id, net_name = _resolve(f)
        f["net_id"] = net_id
        if net_name:
            f["net_name"] = net_name
        if net_id:
            routed_net_ids.add(net_id)

    # Also count nets connected via fill (e.g., GND)
    # A net with pads on the fill net's layer is considered routed via fill
    for f in fills:
        if f.get("net_id"):
            routed_net_ids.add(f["net_id"])

    # Determine unrouted nets
    unrouted = sorted(all_net_ids - routed_net_ids)

    # Build output — merge with original
    result = {
        "version": original_routed.get("version", "1.0"),
        "project_name": original_routed.get("project_name", ""),
        "source_netlist": original_routed.get("source_netlist", ""),
        "source_bom": original_routed.get("source_bom", ""),
        "board": original_routed.get("board", {}),
        "placements": original_routed.get("placements", []),
        "silkscreen": original_routed.get("silkscreen", []),
        "routing": {
            "traces": traces,
            "vias": vias,
            "unrouted_nets": unrouted,
            "statistics": _compute_statistics(
                traces, vias, fills, len(all_net_ids), routed_net_ids, unrouted,
            ),
            "config": original_routed.get("routing", {}).get("config", {}),
            "copper_fills": fills,
            "trace_width_overrides": {},
        },
    }

    return result
