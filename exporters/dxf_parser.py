"""Parse DXF board outline files into vertex lists for the PCB pipeline."""

from __future__ import annotations

from pathlib import Path


def parse_board_outline(
    dxf_path: str | Path,
) -> tuple[list[list[float]], float, float]:
    """Extract board outline vertices from a DXF file.

    Looks for LWPOLYLINE or LINE entities in model space.
    If multiple closed polylines exist, picks the one with the largest area
    (assumed to be the board outline).

    Args:
        dxf_path: Path to the DXF file.

    Returns:
        Tuple of (vertices, width_mm, height_mm) where:
        - vertices: [[x, y], ...] polygon normalized so min corner is at origin
        - width_mm: bounding box width
        - height_mm: bounding box height

    Raises:
        ImportError: If ezdxf is not installed.
        ValueError: If no valid outline is found.
    """
    import ezdxf

    doc = ezdxf.readfile(str(dxf_path))
    msp = doc.modelspace()

    candidates: list[list[tuple[float, float]]] = []

    # Collect LWPOLYLINE entities (most common for board outlines)
    for entity in msp.query("LWPOLYLINE"):
        points = [(p[0], p[1]) for p in entity.get_points(format="xy")]
        if len(points) >= 3:
            candidates.append(points)

    # Collect POLYLINE entities (older DXF format)
    for entity in msp.query("POLYLINE"):
        points = [(v.dxf.location.x, v.dxf.location.y) for v in entity.vertices]
        if len(points) >= 3:
            candidates.append(points)

    # Collect LINE entities and try to form a closed polygon
    lines: list[tuple[tuple[float, float], tuple[float, float]]] = []
    for entity in msp.query("LINE"):
        start = (round(entity.dxf.start.x, 4), round(entity.dxf.start.y, 4))
        end = (round(entity.dxf.end.x, 4), round(entity.dxf.end.y, 4))
        lines.append((start, end))

    if lines:
        chain = _chain_lines(lines)
        if chain and len(chain) >= 3:
            candidates.append(chain)

    if not candidates:
        raise ValueError(f"No valid board outline found in {dxf_path}")

    # Pick the largest polygon by area
    best = max(candidates, key=_polygon_area)

    # Normalize: shift so min corner is at origin
    xs = [p[0] for p in best]
    ys = [p[1] for p in best]
    min_x, min_y = min(xs), min(ys)
    max_x, max_y = max(xs), max(ys)

    vertices = [[round(float(p[0] - min_x), 3), round(float(p[1] - min_y), 3)] for p in best]
    width = round(float(max_x - min_x), 3)
    height = round(float(max_y - min_y), 3)

    return vertices, width, height


def _polygon_area(points: list[tuple[float, float]]) -> float:
    """Compute area of a polygon using the shoelace formula."""
    n = len(points)
    area = 0.0
    for i in range(n):
        j = (i + 1) % n
        area += points[i][0] * points[j][1]
        area -= points[j][0] * points[i][1]
    return abs(area) / 2.0


def _chain_lines(
    lines: list[tuple[tuple[float, float], tuple[float, float]]],
) -> list[tuple[float, float]] | None:
    """Try to chain LINE entities into a closed polygon.

    Uses a greedy approach: start from the first line, find the next line
    whose start matches the current end, and repeat until closed or stuck.
    """
    if not lines:
        return None

    remaining = list(lines)
    chain = [remaining[0][0], remaining[0][1]]
    remaining.pop(0)

    tolerance = 0.01  # mm

    max_iterations = len(remaining) + 1
    for _ in range(max_iterations):
        current_end = chain[-1]
        found = False

        for i, (start, end) in enumerate(remaining):
            if _points_close(current_end, start, tolerance):
                chain.append(end)
                remaining.pop(i)
                found = True
                break
            elif _points_close(current_end, end, tolerance):
                chain.append(start)
                remaining.pop(i)
                found = True
                break

        if not found:
            break

    # Check if polygon is closed
    if _points_close(chain[0], chain[-1], tolerance):
        chain.pop()  # Remove duplicate closing point
        return chain

    return None


def _points_close(
    a: tuple[float, float], b: tuple[float, float], tolerance: float
) -> bool:
    """Check if two 2D points are within tolerance."""
    return abs(a[0] - b[0]) < tolerance and abs(a[1] - b[1]) < tolerance
