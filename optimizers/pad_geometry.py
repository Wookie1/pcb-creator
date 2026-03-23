"""Pad geometry — compute absolute pad positions from placement + netlist.

Maps each port (pin) in the netlist to its physical position on the board
by combining component center coordinates, footprint pad offsets, and rotation.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass


@dataclass
class PadInfo:
    """Absolute pad position on the board."""
    port_id: str
    designator: str
    pin_number: int
    net_id: str | None       # which net this pad belongs to (None if unconnected)
    x_mm: float              # absolute board X
    y_mm: float              # absolute board Y
    pad_width_mm: float
    pad_height_mm: float
    layer: str               # "top" or "bottom"


@dataclass
class FootprintDef:
    """Pad layout for a package, relative to component center at rotation=0.

    pin_offsets maps pin_number -> (dx_mm, dy_mm) offset from center.
    pad_size is (width_mm, height_mm) of each pad.
    """
    pin_offsets: dict[int, tuple[float, float]]
    pad_size: tuple[float, float]


# ---------------------------------------------------------------------------
# Footprint definitions — offsets relative to component center at rotation=0
# ---------------------------------------------------------------------------

# 2-pad SMD passives: pads along X axis at ±half-body-length
_SMD_2PAD = {
    "0402": FootprintDef(
        pin_offsets={1: (-0.50, 0.0), 2: (0.50, 0.0)},
        pad_size=(0.4, 0.5),
    ),
    "0603": FootprintDef(
        pin_offsets={1: (-0.75, 0.0), 2: (0.75, 0.0)},
        pad_size=(0.5, 0.7),
    ),
    "0805": FootprintDef(
        pin_offsets={1: (-0.90, 0.0), 2: (0.90, 0.0)},
        pad_size=(0.6, 0.9),
    ),
    "1206": FootprintDef(
        pin_offsets={1: (-1.10, 0.0), 2: (1.10, 0.0)},
        pad_size=(0.8, 1.0),
    ),
    "1210": FootprintDef(
        pin_offsets={1: (-1.10, 0.0), 2: (1.10, 0.0)},
        pad_size=(0.8, 1.2),
    ),
}

# SOT-23 (3 pins): pin 1 top-left, pin 2 top-right, pin 3 bottom-center
_SOT23 = FootprintDef(
    pin_offsets={
        1: (-0.95, 1.0),
        2: (0.95, 1.0),
        3: (0.0, -1.0),
    },
    pad_size=(0.6, 0.7),
)

# SOIC-8: 4 pins per side, 1.27mm pitch, 2.7mm half-row-spacing
_SOIC8 = FootprintDef(
    pin_offsets={
        1: (-1.905, 2.7),
        2: (-0.635, 2.7),
        3: (0.635, 2.7),
        4: (1.905, 2.7),
        5: (1.905, -2.7),
        6: (0.635, -2.7),
        7: (-0.635, -2.7),
        8: (-1.905, -2.7),
    },
    pad_size=(0.6, 1.5),
)

# TO-220: 3 pins inline, 2.54mm pitch
_TO220 = FootprintDef(
    pin_offsets={
        1: (-2.54, 0.0),
        2: (0.0, 0.0),
        3: (2.54, 0.0),
    },
    pad_size=(1.0, 1.8),
)

# HC49 crystal: 2 pins, 4.88mm apart
_HC49 = FootprintDef(
    pin_offsets={1: (-2.44, 0.0), 2: (2.44, 0.0)},
    pad_size=(0.8, 1.5),
)

# PJ-002A barrel jack: 3 pins
_PJ002A = FootprintDef(
    pin_offsets={
        1: (0.0, 0.0),       # center pin (tip)
        2: (-3.5, -3.3),     # sleeve
        3: (3.5, -3.3),      # switch
    },
    pad_size=(1.5, 1.5),
)

# 6mm tactile switch: 4 pins in a rectangle pattern
# Pins 1,2 on left side, pins 3,4 on right side
_6MM_TACTILE = FootprintDef(
    pin_offsets={
        1: (-3.25, 2.25),
        2: (-3.25, -2.25),
        3: (3.25, -2.25),
        4: (3.25, 2.25),
    },
    pad_size=(1.0, 1.5),
)

# Fiducial: single pad at center (no electrical connection)
_FIDUCIAL = FootprintDef(
    pin_offsets={1: (0.0, 0.0)},
    pad_size=(1.0, 1.0),
)


def _make_dip(pin_count: int) -> FootprintDef:
    """Generate DIP-N footprint: two rows, 2.54mm pitch, 7.62mm row spacing."""
    if pin_count < 2 or pin_count % 2 != 0:
        raise ValueError(f"DIP pin count must be even and >= 2, got {pin_count}")

    pins_per_side = pin_count // 2
    pitch = 2.54  # mm
    row_half_spacing = 3.81  # 7.62mm / 2

    offsets: dict[int, tuple[float, float]] = {}
    # Pins 1..N/2 on left side (negative X), top to bottom (positive to negative Y)
    for i in range(pins_per_side):
        pin_num = i + 1
        y = (pins_per_side - 1) / 2 * pitch - i * pitch
        offsets[pin_num] = (-row_half_spacing, y)

    # Pins N/2+1..N on right side (positive X), bottom to top
    for i in range(pins_per_side):
        pin_num = pins_per_side + i + 1
        y = -(pins_per_side - 1) / 2 * pitch + i * pitch
        offsets[pin_num] = (row_half_spacing, y)

    return FootprintDef(pin_offsets=offsets, pad_size=(0.6, 1.6))


def _make_pin_header_1xn(pin_count: int) -> FootprintDef:
    """Generate single-row pin header: pins along Y axis, 2.54mm pitch."""
    pitch = 2.54
    offsets: dict[int, tuple[float, float]] = {}
    for i in range(pin_count):
        pin_num = i + 1
        y = (pin_count - 1) / 2 * pitch - i * pitch
        offsets[pin_num] = (0.0, y)
    return FootprintDef(pin_offsets=offsets, pad_size=(1.0, 1.7))


def _make_pin_header_2xn(pin_count: int) -> FootprintDef:
    """Generate dual-row pin header: 2.54mm pitch, 2.54mm row spacing.

    Pin numbering: odd pins on left column, even pins on right column.
    """
    if pin_count < 2 or pin_count % 2 != 0:
        raise ValueError(f"2xN header total pin count must be even, got {pin_count}")

    rows = pin_count // 2
    pitch = 2.54
    row_half_spacing = 1.27  # 2.54mm / 2

    offsets: dict[int, tuple[float, float]] = {}
    for i in range(rows):
        y = (rows - 1) / 2 * pitch - i * pitch
        # Odd pin on left, even pin on right
        offsets[2 * i + 1] = (-row_half_spacing, y)
        offsets[2 * i + 2] = (row_half_spacing, y)

    return FootprintDef(pin_offsets=offsets, pad_size=(1.0, 1.7))


def _make_tqfp(pin_count: int, body_mm: float = 7.0) -> FootprintDef:
    """Generate TQFP-N footprint: pins on all 4 sides, 0.8mm pitch."""
    if pin_count < 4 or pin_count % 4 != 0:
        raise ValueError(f"TQFP pin count must be multiple of 4, got {pin_count}")

    pins_per_side = pin_count // 4
    pitch = 0.8
    edge_center = body_mm / 2 + 0.5  # pads extend beyond body

    offsets: dict[int, tuple[float, float]] = {}
    pin = 1

    # Left side (pins go top to bottom)
    for i in range(pins_per_side):
        y = (pins_per_side - 1) / 2 * pitch - i * pitch
        offsets[pin] = (-edge_center, y)
        pin += 1

    # Bottom side (pins go left to right)
    for i in range(pins_per_side):
        x = -(pins_per_side - 1) / 2 * pitch + i * pitch
        offsets[pin] = (x, -edge_center)
        pin += 1

    # Right side (pins go bottom to top)
    for i in range(pins_per_side):
        y = -(pins_per_side - 1) / 2 * pitch + i * pitch
        offsets[pin] = (edge_center, y)
        pin += 1

    # Top side (pins go right to left)
    for i in range(pins_per_side):
        x = (pins_per_side - 1) / 2 * pitch - i * pitch
        offsets[pin] = (x, edge_center)
        pin += 1

    return FootprintDef(pin_offsets=offsets, pad_size=(0.45, 1.2))


def get_footprint_def(package: str, pin_count: int) -> FootprintDef:
    """Return pad offsets for a known package.

    Falls back to edge distribution for unknown packages.
    """
    pkg_upper = package.upper()

    # Check SMD 2-pad packages
    if package in _SMD_2PAD:
        return _SMD_2PAD[package]

    # Named packages
    if package == "6mm_tactile":
        return _6MM_TACTILE
    if pkg_upper == "SOT-23":
        return _SOT23
    if pkg_upper == "SOIC-8":
        return _SOIC8
    if pkg_upper == "TO-220":
        return _TO220
    if pkg_upper == "HC49":
        return _HC49
    if pkg_upper == "PJ-002A":
        return _PJ002A
    if package == "Fiducial_1mm":
        return _FIDUCIAL

    # DIP-N pattern
    m = re.match(r"DIP-(\d+)", package, re.IGNORECASE)
    if m:
        return _make_dip(int(m.group(1)))

    # PinHeader_1xN pattern
    m = re.match(r"PinHeader_1x(\d+)", package)
    if m:
        return _make_pin_header_1xn(int(m.group(1)))

    # PinHeader_2xN pattern
    m = re.match(r"PinHeader_2x(\d+)", package)
    if m:
        total_pins = int(m.group(1)) * 2
        return _make_pin_header_2xn(total_pins)

    # TQFP-N pattern
    m = re.match(r"TQFP-(\d+)", package, re.IGNORECASE)
    if m:
        return _make_tqfp(int(m.group(1)))

    # Unknown: fall back to None (caller should use fallback)
    return None


def _generate_fallback_footprint(
    footprint_w: float,
    footprint_h: float,
    pin_count: int,
) -> FootprintDef:
    """Distribute pins evenly around the footprint perimeter for unknown packages."""
    if pin_count <= 0:
        return FootprintDef(pin_offsets={}, pad_size=(0.5, 0.5))

    if pin_count == 1:
        return FootprintDef(pin_offsets={1: (0.0, 0.0)}, pad_size=(0.5, 0.5))

    if pin_count == 2:
        # Two pads along X axis
        return FootprintDef(
            pin_offsets={1: (-footprint_w / 3, 0.0), 2: (footprint_w / 3, 0.0)},
            pad_size=(0.5, 0.5),
        )

    # Distribute around perimeter
    perimeter = 2 * (footprint_w + footprint_h)
    offsets: dict[int, tuple[float, float]] = {}

    for i in range(pin_count):
        frac = i / pin_count
        dist = frac * perimeter
        hw, hh = footprint_w / 2, footprint_h / 2

        if dist < footprint_w:
            # Top edge, left to right
            offsets[i + 1] = (-hw + dist, hh)
        elif dist < footprint_w + footprint_h:
            # Right edge, top to bottom
            d = dist - footprint_w
            offsets[i + 1] = (hw, hh - d)
        elif dist < 2 * footprint_w + footprint_h:
            # Bottom edge, right to left
            d = dist - footprint_w - footprint_h
            offsets[i + 1] = (hw - d, -hh)
        else:
            # Left edge, bottom to top
            d = dist - 2 * footprint_w - footprint_h
            offsets[i + 1] = (-hw, -hh + d)

    return FootprintDef(pin_offsets=offsets, pad_size=(0.5, 0.5))


def _rotate_offset(dx: float, dy: float, rotation_deg: int) -> tuple[float, float]:
    """Rotate a pad offset by component rotation (0/90/180/270 CCW)."""
    if rotation_deg == 0:
        return dx, dy
    elif rotation_deg == 90:
        return -dy, dx
    elif rotation_deg == 180:
        return -dx, -dy
    elif rotation_deg == 270:
        return dy, -dx
    else:
        # Arbitrary angle (shouldn't happen in normal flow)
        rad = math.radians(rotation_deg)
        cos_r = math.cos(rad)
        sin_r = math.sin(rad)
        return dx * cos_r - dy * sin_r, dx * sin_r + dy * cos_r


def build_pad_map(
    placement: dict,
    netlist: dict,
) -> dict[str, PadInfo]:
    """Build a complete map of port_id -> PadInfo with absolute board coordinates.

    For each port in the netlist:
    1. Find the component via component_id
    2. Find placement entry by designator
    3. Look up footprint definition
    4. Apply rotation to pin offset
    5. Add component center to get absolute position
    6. Determine which net (if any) the port belongs to
    """
    elements = netlist.get("elements", [])

    # Build lookup tables from netlist
    components: dict[str, dict] = {}    # component_id -> element
    ports: list[dict] = []              # all port elements
    port_to_net: dict[str, str] = {}    # port_id -> net_id

    for elem in elements:
        etype = elem.get("element_type")
        if etype == "component":
            components[elem["component_id"]] = elem
        elif etype == "port":
            ports.append(elem)
        elif etype == "net":
            for pid in elem.get("connected_port_ids", []):
                port_to_net[pid] = elem["net_id"]

    # Build designator -> placement entry lookup
    placements_by_des: dict[str, dict] = {}
    for p in placement.get("placements", []):
        placements_by_des[p["designator"]] = p

    # Count pins per component for fallback footprint generation
    comp_pin_counts: dict[str, int] = {}
    for port in ports:
        cid = port.get("component_id", "")
        comp_pin_counts[cid] = comp_pin_counts.get(cid, 0) + 1

    pad_map: dict[str, PadInfo] = {}

    for port in ports:
        port_id = port["port_id"]
        comp_id = port.get("component_id", "")
        pin_number = port.get("pin_number", 1)

        comp = components.get(comp_id)
        if not comp:
            continue

        designator = comp["designator"]
        plc = placements_by_des.get(designator)
        if not plc:
            continue  # component not placed (e.g., fiducials have no netlist entry)

        package = plc.get("package", comp.get("package", ""))
        pin_count = comp_pin_counts.get(comp_id, 1)

        # Get footprint definition
        fp = get_footprint_def(package, pin_count)
        if fp is None:
            fp = _generate_fallback_footprint(
                plc.get("footprint_width_mm", 2.0),
                plc.get("footprint_height_mm", 2.0),
                pin_count,
            )

        # Get pin offset (fall back to center if pin not in definition)
        if pin_number in fp.pin_offsets:
            dx, dy = fp.pin_offsets[pin_number]
        else:
            # Pin number not in definition — use center
            dx, dy = 0.0, 0.0

        # Apply component rotation
        rotation = plc.get("rotation_deg", 0)
        dx_rot, dy_rot = _rotate_offset(dx, dy, rotation)

        # Absolute position
        abs_x = plc["x_mm"] + dx_rot
        abs_y = plc["y_mm"] + dy_rot

        # Rotate pad dimensions if needed
        pw, ph = fp.pad_size
        if rotation in (90, 270):
            pw, ph = ph, pw

        # Through-hole pads span both layers; SMD pads are on the component layer only
        is_th = fp.is_through_hole if hasattr(fp, 'is_through_hole') else (
            package.startswith(("DIP", "PinHeader", "PJ-002A", "TO-220", "HC49",
                                "6mm_tactile"))
        )
        pad_layer = "all" if is_th else plc.get("layer", "top")

        pad_map[port_id] = PadInfo(
            port_id=port_id,
            designator=designator,
            pin_number=pin_number,
            net_id=port_to_net.get(port_id),
            x_mm=abs_x,
            y_mm=abs_y,
            pad_width_mm=pw,
            pad_height_mm=ph,
            layer=pad_layer,
        )

    return pad_map
