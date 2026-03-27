"""Generate parametric 3D component models as STEP BREP entities.

Creates simple 3D shapes (boxes, cylinders) from component dimensions,
output as STEP entity strings that can be merged into a STEP assembly.

Since build123d/cadquery don't support Python 3.14 yet, this generates
raw STEP entities using the same hand-written BREP approach as step_exporter.py.
"""

from __future__ import annotations

import math

from .component_heights import get_component_height


def generate_box_entities(
    x_mm: float,
    y_mm: float,
    z_mm: float,
    width_mm: float,
    depth_mm: float,
    height_mm: float,
    rotation_deg: float,
    name: str,
    eid_start: int,
) -> tuple[list[str], int, int]:
    """Generate STEP BREP entities for a rectangular box component.

    The box is centered at (x, y) on the XY plane, sitting on z_mm.

    Args:
        x_mm: Center X position in mm.
        y_mm: Center Y position in mm.
        z_mm: Bottom Z position in mm (board surface).
        width_mm: Box width (X direction before rotation).
        depth_mm: Box depth (Y direction before rotation).
        height_mm: Box height (Z direction).
        rotation_deg: Rotation around Z axis in degrees.
        name: Entity name label.
        eid_start: Starting entity ID number.

    Returns:
        (entities, solid_id, next_eid) — list of STEP entity strings,
        the entity ID of the MANIFOLD_SOLID_BREP, and the next available ID.
    """
    entities: list[str] = []
    eid = [eid_start]

    def next_id() -> int:
        eid[0] += 1
        return eid[0]

    def ref(entity_id: int) -> str:
        return f"#{entity_id}"

    scale = 0.001  # mm to meters

    # Half-dimensions
    hw = width_mm / 2.0
    hd = depth_mm / 2.0

    # 4 corners of the box base (before rotation), relative to center
    corners_local = [
        (-hw, -hd),
        (hw, -hd),
        (hw, hd),
        (-hw, hd),
    ]

    # Apply rotation
    rad = math.radians(rotation_deg)
    cos_r = math.cos(rad)
    sin_r = math.sin(rad)

    def rotate(lx: float, ly: float) -> tuple[float, float]:
        return (lx * cos_r - ly * sin_r, lx * sin_r + ly * cos_r)

    corners_rotated = [rotate(cx, cy) for cx, cy in corners_local]

    # Generate 3D points (top and bottom of box, in meters)
    z_bot = z_mm * scale
    z_top = (z_mm + height_mm) * scale

    point_ids_top: list[int] = []
    point_ids_bot: list[int] = []

    for cx, cy in corners_rotated:
        gx = (x_mm + cx) * scale
        gy = (y_mm + cy) * scale

        pid = next_id()
        entities.append(f"#{pid} = CARTESIAN_POINT('',({gx:.6f},{gy:.6f},{z_top:.6f}));")
        point_ids_top.append(pid)

        pid = next_id()
        entities.append(f"#{pid} = CARTESIAN_POINT('',({gx:.6f},{gy:.6f},{z_bot:.6f}));")
        point_ids_bot.append(pid)

    # Shared direction entities
    dir_z_up = next_id()
    entities.append(f"#{dir_z_up} = DIRECTION('',(0.,0.,1.));")
    dir_z_down = next_id()
    entities.append(f"#{dir_z_down} = DIRECTION('',(0.,0.,-1.));")
    dir_x = next_id()
    entities.append(f"#{dir_x} = DIRECTION('',(1.,0.,0.));")

    # Axis placements for top and bottom
    origin_bot = next_id()
    entities.append(f"#{origin_bot} = CARTESIAN_POINT('',({x_mm * scale:.6f},{y_mm * scale:.6f},{z_bot:.6f}));")
    origin_top = next_id()
    entities.append(f"#{origin_top} = CARTESIAN_POINT('',({x_mm * scale:.6f},{y_mm * scale:.6f},{z_top:.6f}));")

    axis_bot = next_id()
    entities.append(f"#{axis_bot} = AXIS2_PLACEMENT_3D('',#{origin_bot},#{dir_z_up},#{dir_x});")
    axis_top = next_id()
    entities.append(f"#{axis_top} = AXIS2_PLACEMENT_3D('',#{origin_top},#{dir_z_up},#{dir_x});")

    plane_top = next_id()
    entities.append(f"#{plane_top} = PLANE('',#{axis_top});")
    plane_bot = next_id()
    entities.append(f"#{plane_bot} = PLANE('',#{axis_bot});")

    # Build edge loop helper
    def build_edge_loop(point_ids: list[int]) -> int:
        vertex_ids = []
        for pid in point_ids:
            vid = next_id()
            entities.append(f"#{vid} = VERTEX_POINT('',#{pid});")
            vertex_ids.append(vid)

        oriented_edge_ids = []
        for i in range(len(vertex_ids)):
            j = (i + 1) % len(vertex_ids)
            vec_id = next_id()
            entities.append(f"#{vec_id} = VECTOR('',#{dir_x},1.);")
            line_id = next_id()
            entities.append(f"#{line_id} = LINE('',#{point_ids[i]},#{vec_id});")
            edge_id = next_id()
            entities.append(f"#{edge_id} = EDGE_CURVE('',#{vertex_ids[i]},#{vertex_ids[j]},#{line_id},.T.);")
            oe_id = next_id()
            entities.append(f"#{oe_id} = ORIENTED_EDGE('',*,*,#{edge_id},.T.);")
            oriented_edge_ids.append(oe_id)

        el_id = next_id()
        oe_refs = ",".join(ref(oe) for oe in oriented_edge_ids)
        entities.append(f"#{el_id} = EDGE_LOOP('',({oe_refs}));")
        fb_id = next_id()
        entities.append(f"#{fb_id} = FACE_BOUND('',#{el_id},.T.);")
        return fb_id

    # Top face
    top_fb = build_edge_loop(point_ids_top)
    top_face = next_id()
    entities.append(f"#{top_face} = ADVANCED_FACE('',({ref(top_fb)}),#{plane_top},.T.);")

    # Bottom face (reversed winding)
    bot_fb = build_edge_loop(list(reversed(point_ids_bot)))
    bot_face = next_id()
    entities.append(f"#{bot_face} = ADVANCED_FACE('',({ref(bot_fb)}),#{plane_bot},.T.);")

    # Side faces
    n = 4
    side_face_ids = []
    for i in range(n):
        j = (i + 1) % n
        side_pts = [
            point_ids_bot[i], point_ids_bot[j],
            point_ids_top[j], point_ids_top[i],
        ]
        ax_id = next_id()
        entities.append(f"#{ax_id} = AXIS2_PLACEMENT_3D('',#{side_pts[0]},#{dir_x},#{dir_z_up});")
        sp_id = next_id()
        entities.append(f"#{sp_id} = PLANE('',#{ax_id});")
        side_fb = build_edge_loop(side_pts)
        sf_id = next_id()
        entities.append(f"#{sf_id} = ADVANCED_FACE('',({ref(side_fb)}),#{sp_id},.T.);")
        side_face_ids.append(sf_id)

    # Closed shell and solid
    all_faces = [top_face, bot_face] + side_face_ids
    face_refs = ",".join(ref(f) for f in all_faces)
    shell_id = next_id()
    entities.append(f"#{shell_id} = CLOSED_SHELL('',({face_refs}));")

    solid_id = next_id()
    safe_name = name.replace("'", "")
    entities.append(f"#{solid_id} = MANIFOLD_SOLID_BREP('{safe_name}',#{shell_id});")

    return entities, solid_id, eid[0]


def generate_component_model(
    placement: dict,
    board_thickness_mm: float,
    eid_start: int,
) -> tuple[list[str], int, int]:
    """Generate STEP entities for a single component based on placement data.

    Args:
        placement: Placement dict with x_mm, y_mm, rotation_deg, package,
                   footprint_width_mm, footprint_height_mm, designator, etc.
        board_thickness_mm: PCB thickness (components sit on top of this).
        eid_start: Starting entity ID.

    Returns:
        (entities, solid_id, next_eid)
    """
    package = placement.get("package", "0805")
    comp_type = placement.get("component_type", "")
    designator = placement.get("designator", "COMP")
    x = placement.get("x_mm", 0)
    y = placement.get("y_mm", 0)
    rot = placement.get("rotation_deg", 0)
    layer = placement.get("layer", "top")

    # Component body dimensions
    # Use footprint dims as approximation of body, shrunk slightly
    fp_w = placement.get("footprint_width_mm", 2.0)
    fp_h = placement.get("footprint_height_mm", 1.0)
    body_w = fp_w * 0.85  # Body is slightly smaller than footprint
    body_d = fp_h * 0.85
    body_h = get_component_height(package, comp_type)

    # Z position: top layer sits on top of board, bottom sits under
    if layer == "bottom":
        z = -body_h  # Below the board origin
    else:
        z = board_thickness_mm  # On top of board

    return generate_box_entities(
        x_mm=x, y_mm=y, z_mm=z,
        width_mm=body_w, depth_mm=body_d, height_mm=body_h,
        rotation_deg=rot,
        name=designator,
        eid_start=eid_start,
    )
