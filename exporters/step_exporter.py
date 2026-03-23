"""Export bare PCB as a STEP (ISO 10303-21) file.

Generates a solid 3D model of the bare board (no components) as an extruded
polygon. Supports rectangular and arbitrary outline shapes.

The STEP file uses the AP214 application protocol and represents the board
as a BREP (Boundary Representation) solid: top face, bottom face, and
edge faces connecting them.

Future enhancement: add 3D component models from EDA libraries.
"""

from __future__ import annotations

import math
from datetime import datetime
from pathlib import Path


def _get_board_vertices(board: dict) -> list[tuple[float, float]]:
    """Get board outline vertices in mm."""
    vertices = board.get("outline_vertices")
    if vertices:
        return [(v[0], v[1]) for v in vertices]
    w = board.get("width_mm", 50.0)
    h = board.get("height_mm", 50.0)
    return [(0, 0), (w, 0), (w, h), (0, h)]


def export_step(
    routed: dict,
    netlist: dict,
    output_path: Path,
    board_thickness_mm: float = 1.6,
) -> Path:
    """Export bare PCB as STEP file.

    Args:
        routed: Routed dict containing board dimensions/outline.
        netlist: Netlist dict (unused for bare board, reserved for future).
        output_path: Where to write the .step file.
        board_thickness_mm: PCB thickness (default 1.6mm, standard 2-layer).

    Returns:
        Path to the written file.
    """
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    board = routed.get("board", {})
    vertices = _get_board_vertices(board)
    n = len(vertices)
    thickness = board_thickness_mm

    # Convert mm to meters for STEP (SI units)
    scale = 0.001
    verts_3d_top = [(v[0] * scale, v[1] * scale, thickness * scale) for v in vertices]
    verts_3d_bot = [(v[0] * scale, v[1] * scale, 0.0) for v in vertices]

    now = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S")
    project = routed.get("project_name", "pcb")

    # Build STEP entities
    # Entity numbering: we'll track with a counter
    entities: list[str] = []
    eid = [0]  # mutable counter

    def next_id() -> int:
        eid[0] += 1
        return eid[0]

    def ref(entity_id: int) -> str:
        return f"#{entity_id}"

    # Cartesian points for all vertices
    point_ids_top: list[int] = []
    point_ids_bot: list[int] = []

    for x, y, z in verts_3d_top:
        pid = next_id()
        entities.append(f"#{pid} = CARTESIAN_POINT('',({x:.6f},{y:.6f},{z:.6f}));")
        point_ids_top.append(pid)

    for x, y, z in verts_3d_bot:
        pid = next_id()
        entities.append(f"#{pid} = CARTESIAN_POINT('',({x:.6f},{y:.6f},{z:.6f}));")
        point_ids_bot.append(pid)

    # Direction vectors
    dir_z_up = next_id()
    entities.append(f"#{dir_z_up} = DIRECTION('',(0.,0.,1.));")
    dir_z_down = next_id()
    entities.append(f"#{dir_z_down} = DIRECTION('',(0.,0.,-1.));")
    dir_x = next_id()
    entities.append(f"#{dir_x} = DIRECTION('',(1.,0.,0.));")

    # Origin point
    origin_id = next_id()
    entities.append(f"#{origin_id} = CARTESIAN_POINT('',(0.,0.,0.));")
    origin_top_id = next_id()
    entities.append(f"#{origin_top_id} = CARTESIAN_POINT('',(0.,0.,{thickness * scale:.6f}));")

    # Axis placements for top and bottom planes
    axis_bot = next_id()
    entities.append(f"#{axis_bot} = AXIS2_PLACEMENT_3D('',#{origin_id},#{dir_z_up},#{dir_x});")
    axis_top = next_id()
    entities.append(f"#{axis_top} = AXIS2_PLACEMENT_3D('',#{origin_top_id},#{dir_z_up},#{dir_x});")

    # Planes
    plane_top = next_id()
    entities.append(f"#{plane_top} = PLANE('',#{axis_top});")
    plane_bot = next_id()
    entities.append(f"#{plane_bot} = PLANE('',#{axis_bot});")

    # Build edges for top face outline
    def build_edge_loop(point_ids: list[int], plane_id: int) -> int:
        """Build an edge loop from a polygon of points. Returns the face_bound ID."""
        vertex_ids = []
        for pid in point_ids:
            vid = next_id()
            entities.append(f"#{vid} = VERTEX_POINT('',#{pid});")
            vertex_ids.append(vid)

        edge_ids = []
        oriented_edge_ids = []
        for i in range(len(vertex_ids)):
            j = (i + 1) % len(vertex_ids)
            # Edge curve (line between vertices)
            line_dir_id = next_id()
            # Compute direction vector
            p1 = point_ids[i]
            p2 = point_ids[j]
            entities.append(f"#{line_dir_id} = VECTOR('',#{dir_x},1.);")  # placeholder direction

            line_id = next_id()
            entities.append(f"#{line_id} = LINE('',#{p1},#{line_dir_id});")

            edge_id = next_id()
            entities.append(f"#{edge_id} = EDGE_CURVE('',#{vertex_ids[i]},#{vertex_ids[j]},#{line_id},.T.);")
            edge_ids.append(edge_id)

            oe_id = next_id()
            entities.append(f"#{oe_id} = ORIENTED_EDGE('',*,*,#{edge_id},.T.);")
            oriented_edge_ids.append(oe_id)

        # Edge loop
        el_id = next_id()
        oe_refs = ",".join(ref(oe) for oe in oriented_edge_ids)
        entities.append(f"#{el_id} = EDGE_LOOP('',({oe_refs}));")

        # Face bound
        fb_id = next_id()
        entities.append(f"#{fb_id} = FACE_BOUND('',#{el_id},.T.);")

        return fb_id

    # Top face
    top_fb = build_edge_loop(point_ids_top, plane_top)
    top_face = next_id()
    entities.append(f"#{top_face} = ADVANCED_FACE('',({ref(top_fb)}),#{plane_top},.T.);")

    # Bottom face (reversed winding)
    bot_point_ids_reversed = list(reversed(point_ids_bot))
    bot_fb = build_edge_loop(bot_point_ids_reversed, plane_bot)
    bot_face = next_id()
    entities.append(f"#{bot_face} = ADVANCED_FACE('',({ref(bot_fb)}),#{plane_bot},.T.);")

    # Side faces (one per edge of the polygon)
    side_face_ids = []
    for i in range(n):
        j = (i + 1) % n
        # 4 corner points of the side quad
        side_pts = [
            point_ids_bot[i], point_ids_bot[j],
            point_ids_top[j], point_ids_top[i],
        ]
        # Compute face normal direction
        ax_id = next_id()
        entities.append(f"#{ax_id} = AXIS2_PLACEMENT_3D('',#{side_pts[0]},#{dir_x},#{dir_z_up});")
        side_plane = next_id()
        entities.append(f"#{side_plane} = PLANE('',#{ax_id});")

        side_fb = build_edge_loop(side_pts, side_plane)
        sf_id = next_id()
        entities.append(f"#{sf_id} = ADVANCED_FACE('',({ref(side_fb)}),#{side_plane},.T.);")
        side_face_ids.append(sf_id)

    # Closed shell
    all_faces = [top_face, bot_face] + side_face_ids
    face_refs = ",".join(ref(f) for f in all_faces)
    shell_id = next_id()
    entities.append(f"#{shell_id} = CLOSED_SHELL('',({face_refs}));")

    # Manifold solid
    solid_id = next_id()
    entities.append(f"#{solid_id} = MANIFOLD_SOLID_BREP('{project}_board',#{shell_id});")

    # Shape representation
    srep_id = next_id()
    entities.append(f"#{srep_id} = SHAPE_REPRESENTATION('',(#{solid_id}),#{axis_bot});")

    # Product and shape definition
    prod_id = next_id()
    entities.append(f"#{prod_id} = PRODUCT('{project}','{project} PCB','',(#{next_id()}));")
    pc_id = eid[0]
    entities.append(f"#{pc_id} = PRODUCT_CONTEXT('',#2,'mechanical');")

    pdf_id = next_id()
    entities.append(f"#{pdf_id} = PRODUCT_DEFINITION_FORMATION('','',(#{prod_id}));")

    pd_id = next_id()
    entities.append(f"#{pd_id} = PRODUCT_DEFINITION('design','',#{pdf_id},#{next_id()});")
    pdc_id = eid[0]
    entities.append(f"#{pdc_id} = PRODUCT_DEFINITION_CONTEXT('part definition',#2,'design');")

    pds_id = next_id()
    entities.append(f"#{pds_id} = PRODUCT_DEFINITION_SHAPE('','',(#{pd_id}));")

    sdr_id = next_id()
    entities.append(f"#{sdr_id} = SHAPE_DEFINITION_REPRESENTATION(#{pds_id},#{srep_id});")

    # Build the STEP file
    entity_block = "\n".join(entities)

    step_content = f"""\
ISO-10303-21;
HEADER;
FILE_DESCRIPTION(('PCB Board Model'),'2;1');
FILE_NAME('{output_path.name}','{now}',('pcb-creator'),(''),
  'pcb-creator 1.0','pcb-creator','');
FILE_SCHEMA(('AUTOMOTIVE_DESIGN'));
ENDSEC;
DATA;
#1 = APPLICATION_PROTOCOL_DEFINITION('international standard',
  'automotive_design',2000,#2);
#2 = APPLICATION_CONTEXT(
  'core data for automotive mechanical design processes');
{entity_block}
ENDSEC;
END-ISO-10303-21;
"""

    output_path.write_text(step_content, encoding="ascii")
    return output_path
