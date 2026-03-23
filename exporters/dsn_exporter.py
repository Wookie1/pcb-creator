"""Export placement + netlist to Specctra DSN format for Freerouting.

Generates a DSN file containing board outline, component footprints with
pad definitions, netlist connectivity, and design rules. Freerouting reads
this file and produces a SES file with routing results.

Coordinate system: DSN uses mm natively (resolution mm 1000).
"""

from __future__ import annotations

import re
from pathlib import Path

from optimizers.pad_geometry import (
    FootprintDef,
    get_footprint_def,
    _generate_fallback_footprint,
    _rotate_offset,
)
from validators.engineering_constants import (
    TRACE_WIDTH_SIGNAL_MM,
    TRACE_WIDTH_POWER_MM,
    TRACE_CLEARANCE_MM,
    VIA_DRILL_MM,
    VIA_DIAMETER_MM,
)


# ---------------------------------------------------------------------------
# Through-hole detection (shared with kicad_exporter)
# ---------------------------------------------------------------------------

_TH_PACKAGES = {"DIP-", "PinHeader_", "TO-220", "HC49", "PJ-002A", "6mm_tactile"}


def _is_through_hole(package: str) -> bool:
    for prefix in _TH_PACKAGES:
        if package.upper().startswith(prefix.upper()):
            return True
    return False


# ---------------------------------------------------------------------------
# DSN coordinate formatting
# ---------------------------------------------------------------------------

def _fmt(val: float) -> str:
    """Format a mm value for DSN output (4 decimal places, strip trailing zeros)."""
    return f"{val:.4f}".rstrip("0").rstrip(".")


# ---------------------------------------------------------------------------
# Netlist parsing helpers
# ---------------------------------------------------------------------------

def _build_netlist_lookups(netlist: dict) -> tuple[
    dict[str, dict],          # components: component_id -> element
    dict[str, list[dict]],    # ports_by_comp: component_id -> [port elements]
    dict[str, dict],          # port_by_id: port_id -> port element
    dict[str, dict],          # nets_by_id: net_id -> net element
    dict[str, str],           # port_to_net: port_id -> net_id
]:
    """Parse netlist elements into lookup dictionaries."""
    components: dict[str, dict] = {}
    ports_by_comp: dict[str, list[dict]] = {}
    port_by_id: dict[str, dict] = {}
    nets_by_id: dict[str, dict] = {}
    port_to_net: dict[str, str] = {}

    for elem in netlist.get("elements", []):
        etype = elem.get("element_type")
        if etype == "component":
            components[elem["component_id"]] = elem
        elif etype == "port":
            port_by_id[elem["port_id"]] = elem
            ports_by_comp.setdefault(elem["component_id"], []).append(elem)
        elif etype == "net":
            nets_by_id[elem["net_id"]] = elem
            for pid in elem.get("connected_port_ids", []):
                port_to_net[pid] = elem["net_id"]

    return components, ports_by_comp, port_by_id, nets_by_id, port_to_net


# ---------------------------------------------------------------------------
# DSN section generators
# ---------------------------------------------------------------------------

def _dsn_header(project_name: str) -> str:
    return f'(pcb "{project_name}"\n  (parser\n    (string_quote ")\n    (host_cad "pcb-creator")\n    (host_version "1.0")\n  )\n  (resolution mm 1000)\n  (unit mm)\n'


def _dsn_structure(board: dict, config: dict) -> str:
    """Generate structure section: layers, boundary, design rules."""
    w = board.get("width_mm", 50.0)
    h = board.get("height_mm", 50.0)

    clearance = config.get("clearance_mm", TRACE_CLEARANCE_MM)
    trace_w = config.get("trace_width_mm", TRACE_WIDTH_SIGNAL_MM)
    via_dia = config.get("via_diameter_mm", VIA_DIAMETER_MM)
    via_drill = config.get("via_drill_mm", VIA_DRILL_MM)

    lines = [
        "  (structure",
        '    (layer "F.Cu" (type signal))',
        '    (layer "B.Cu" (type signal))',
        "    (boundary",
        "      (path pcb 0",
        f"        0 0 {_fmt(w)} 0 {_fmt(w)} {_fmt(h)} 0 {_fmt(h)} 0 0",
        "      )",
        "    )",
        "    (via Via_Default)",
        "    (rule",
        f"      (width {_fmt(trace_w)})",
        f"      (clearance {_fmt(clearance)})",
        "    )",
        "  )",
    ]
    return "\n".join(lines) + "\n"


def _dsn_library(
    placements: list[dict],
    netlist: dict,
    config: dict,
) -> tuple[str, dict[str, str]]:
    """Generate library section with images (component footprints) and padstacks.

    Returns (dsn_text, des_image_map) where des_image_map maps designator -> image_id.
    """
    components, ports_by_comp, port_by_id, nets_by_id, port_to_net = \
        _build_netlist_lookups(netlist)

    # Build designator -> component_id map
    des_to_comp_id: dict[str, str] = {}
    for cid, comp in components.items():
        des_to_comp_id[comp["designator"]] = cid

    via_dia = config.get("via_diameter_mm", VIA_DIAMETER_MM)
    via_drill = config.get("via_drill_mm", VIA_DRILL_MM)

    # Collect unique (package) combinations using physical footprint pin count
    # Note: We use the footprint's physical pin count (not netlist port count)
    # because the DSN image needs all physical pads defined.
    image_defs: dict[str, tuple[FootprintDef, bool, float, float]] = {}
    # Track which image_id each designator maps to
    des_image_map: dict[str, str] = {}

    for plc in placements:
        if plc.get("component_type") == "fiducial":
            continue

        des = plc["designator"]
        package = plc.get("package", "0805")
        comp_id = des_to_comp_id.get(des)
        netlist_pin_count = len(ports_by_comp.get(comp_id, [])) if comp_id else 2

        # Try to get footprint def — first with netlist count, then let it figure
        # out physical pins from the package name
        fp_def = get_footprint_def(package, netlist_pin_count)
        if fp_def is None:
            fp_def = _generate_fallback_footprint(
                plc.get("footprint_width_mm", 2.0),
                plc.get("footprint_height_mm", 2.0),
                netlist_pin_count,
            )

        # Use the footprint's actual pin count for the image ID
        physical_pin_count = len(fp_def.pin_offsets)
        image_id = f"{package}_{physical_pin_count}"
        des_image_map[des] = image_id

        if image_id in image_defs:
            continue
        is_th = _is_through_hole(package)
        pw, ph = fp_def.pad_size
        image_defs[image_id] = (fp_def, is_th, pw, ph)

    lines = ["  (library"]

    # Padstack definitions
    # SMD padstacks: one per unique pad size per layer side
    padstack_ids: dict[str, str] = {}  # key -> padstack_id

    for image_id, (fp_def, is_th, pw, ph) in image_defs.items():
        if is_th:
            # Through-hole padstack
            pad_dia = max(pw, ph)
            drill = max(0.6, round(min(pw, ph) + 0.2, 2))
            key = f"th_{_fmt(pad_dia)}_{_fmt(drill)}"
            if key not in padstack_ids:
                ps_id = f"TH_{_fmt(pad_dia)}_{_fmt(drill)}".replace(".", "p")
                padstack_ids[key] = ps_id
                lines.append(f'    (padstack {ps_id}')
                lines.append(f'      (shape (circle "F.Cu" {_fmt(pad_dia)}))')
                lines.append(f'      (shape (circle "B.Cu" {_fmt(pad_dia)}))')
                lines.append(f'      (attach off)')
                lines.append(f'    )')
        else:
            # SMD padstack (front and back variants)
            for layer_name in ("F.Cu", "B.Cu"):
                key = f"smd_{_fmt(pw)}_{_fmt(ph)}_{layer_name}"
                if key not in padstack_ids:
                    ps_id = f"SMD_{_fmt(pw)}x{_fmt(ph)}_{layer_name.replace('.', '_')}".replace(".", "p")
                    padstack_ids[key] = ps_id
                    hx, hy = pw / 2, ph / 2
                    lines.append(f'    (padstack {ps_id}')
                    lines.append(f'      (shape (rect "{layer_name}" {_fmt(-hx)} {_fmt(-hy)} {_fmt(hx)} {_fmt(hy)}))')
                    lines.append(f'      (attach off)')
                    lines.append(f'    )')

    # Via padstack
    lines.append(f'    (padstack Via_Default')
    lines.append(f'      (shape (circle "F.Cu" {_fmt(via_dia)}))')
    lines.append(f'      (shape (circle "B.Cu" {_fmt(via_dia)}))')
    lines.append(f'      (attach off)')
    lines.append(f'    )')

    # Image definitions
    for image_id, (fp_def, is_th, pw, ph) in image_defs.items():
        lines.append(f'    (image {image_id}')

        for pin_num, (dx, dy) in sorted(fp_def.pin_offsets.items()):
            dx = round(dx, 4)
            dy = round(dy, 4)

            if is_th:
                pad_dia = max(pw, ph)
                drill = max(0.6, round(min(pw, ph) + 0.2, 2))
                key = f"th_{_fmt(pad_dia)}_{_fmt(drill)}"
            else:
                # Use front-layer padstack by default; placement side
                # determines actual layer at component level
                key = f"smd_{_fmt(pw)}_{_fmt(ph)}_F.Cu"

            ps_id = padstack_ids.get(key, "Via_Default")
            lines.append(f'      (pin {ps_id} {pin_num} {_fmt(dx)} {_fmt(dy)})')

        lines.append(f'    )')

    lines.append("  )")
    return "\n".join(lines) + "\n", des_image_map


def _dsn_placement(
    placements: list[dict],
    netlist: dict,
    des_image_map: dict[str, str] | None = None,
) -> str:
    """Generate placement section with component positions."""
    lines = ["  (placement"]

    # Build des_image_map if not provided (fallback)
    if des_image_map is None:
        components, ports_by_comp, _, _, _ = _build_netlist_lookups(netlist)
        des_to_comp_id: dict[str, str] = {}
        for cid, comp in components.items():
            des_to_comp_id[comp["designator"]] = cid
        des_image_map = {}
        for plc in placements:
            des = plc["designator"]
            package = plc.get("package", "0805")
            comp_id = des_to_comp_id.get(des)
            pin_count = len(ports_by_comp.get(comp_id, [])) if comp_id else 2
            des_image_map[des] = f"{package}_{pin_count}"

    # Group by image_id for DSN format
    image_groups: dict[str, list[dict]] = {}
    for plc in placements:
        if plc.get("component_type") == "fiducial":
            continue
        des = plc["designator"]
        image_id = des_image_map.get(des, f"{plc.get('package', '0805')}_2")
        image_groups.setdefault(image_id, []).append(plc)

    for image_id, plcs in image_groups.items():
        lines.append(f'    (component {image_id}')
        for plc in plcs:
            des = plc["designator"]
            x = plc["x_mm"]
            y = plc["y_mm"]
            rot = plc.get("rotation_deg", 0)
            layer = plc.get("layer", "top")
            side = "front" if layer == "top" else "back"
            lines.append(f'      (place {des} {_fmt(x)} {_fmt(y)} {side} {rot})')
        lines.append(f'    )')

    lines.append("  )")
    return "\n".join(lines) + "\n"


def _dsn_network(
    netlist: dict,
    exclude_nets: list[str] | None = None,
    net_widths: dict[str, float] | None = None,
    default_width: float = 0.25,
) -> str:
    """Generate network section with net definitions and per-class trace widths.

    Args:
        netlist: Netlist dict.
        exclude_nets: Net names to skip (e.g., GND for copper fill).
        net_widths: Optional map of net_name -> trace_width_mm (IPC-2221 computed).
        default_width: Default signal trace width.
    """
    components, ports_by_comp, port_by_id, nets_by_id, port_to_net = \
        _build_netlist_lookups(netlist)

    exclude_names = set(exclude_nets or [])

    # Build port_id -> (designator, pin_number) lookup
    port_to_pin: dict[str, tuple[str, int]] = {}
    for cid, comp in components.items():
        des = comp["designator"]
        for port in ports_by_comp.get(cid, []):
            port_to_pin[port["port_id"]] = (des, port["pin_number"])

    lines = ["  (network"]

    # Group nets by class for separate width rules
    power_nets: list[str] = []
    signal_nets: list[str] = []

    for net_id, net in nets_by_id.items():
        net_name = net.get("name", net_id)
        if net_name in exclude_names:
            continue

        pin_refs = []
        for pid in net.get("connected_port_ids", []):
            info = port_to_pin.get(pid)
            if info:
                des, pin_num = info
                pin_refs.append(f"{des}-{pin_num}")

        if len(pin_refs) < 2:
            continue

        lines.append(f'    (net "{net_name}"')
        lines.append(f'      (pins {" ".join(pin_refs)})')
        lines.append(f'    )')

        nc = net.get("net_class", "signal")
        if nc in ("power", "ground"):
            power_nets.append(net_name)
        else:
            signal_nets.append(net_name)

    # Compute power trace width (max of all power net widths)
    power_width = default_width
    if net_widths:
        for name in power_nets:
            if name in net_widths:
                power_width = max(power_width, net_widths[name])
    power_width = max(power_width, TRACE_WIDTH_POWER_MM)

    # Net classes with appropriate widths
    if power_nets:
        quoted = " ".join(f'"{n}"' for n in power_nets)
        lines.append(f'    (class power {quoted}')
        lines.append(f'      (circuit')
        lines.append(f'        (use_via Via_Default)')
        lines.append(f'      )')
        lines.append(f'      (rule (width {_fmt(power_width)}))')
        lines.append(f'    )')

    if signal_nets:
        quoted = " ".join(f'"{n}"' for n in signal_nets)
        lines.append(f'    (class signal {quoted}')
        lines.append(f'      (circuit')
        lines.append(f'        (use_via Via_Default)')
        lines.append(f'      )')
        lines.append(f'      (rule (width {_fmt(default_width)}))')
        lines.append(f'    )')

    lines.append("  )")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def export_dsn(
    placement: dict,
    netlist: dict,
    output_path: str | Path,
    config: dict | None = None,
) -> Path:
    """Export placement + netlist to Specctra DSN format.

    Args:
        placement: Placement JSON dict (board, placements).
        netlist: Netlist JSON dict (elements).
        output_path: Where to write the .dsn file.
        config: Optional dict with design rules:
            trace_width_mm, clearance_mm, via_drill_mm, via_diameter_mm

    Returns:
        Path to the written file.
    """
    output_path = Path(output_path)
    cfg = config or {}

    board = placement.get("board", {})
    project_name = placement.get("project_name", "pcb")
    placements = placement.get("placements", [])

    # Determine which nets to exclude (typically GND for copper fill)
    exclude_nets = cfg.get("exclude_nets", [])

    library_text, des_image_map = _dsn_library(placements, netlist, cfg)

    parts = [
        _dsn_header(project_name),
        _dsn_structure(board, cfg),
        library_text,
        _dsn_placement(placements, netlist, des_image_map=des_image_map),
        _dsn_network(
            netlist,
            exclude_nets=exclude_nets,
            net_widths=cfg.get("net_widths"),
            default_width=cfg.get("trace_width_mm", TRACE_WIDTH_SIGNAL_MM),
        ),
        "  (wiring)\n",
        ")\n",
    ]

    content = "".join(parts)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(content, encoding="utf-8")

    return output_path
