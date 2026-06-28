"""B5 regression: a 4-layer GND fill island is rescued to the In1 plane.

_add_rescue_vias only rescued a top-layer GND island when bottom-layer fill sat
underneath it (`filled_bottom`) — a 2-layer assumption. On a 4-layer board the
In1.Cu GND plane is solid, so a through-via in the island reaches GND directly,
no bottom fill required. Without that, an outer GND pour fragment with signal on
the bottom underneath it stayed isolated from the plane (kicad-cli:
"Zone [GND] on In1.Cu / Zone [GND] on F.Cu" unconnected at 100% complete).
"""
from optimizers.router import _add_rescue_vias, RoutingGrid, RouterConfig

FILL_NET = 1


def _island_grid():
    """11x11 grid with a 3x3 top-layer fill island NOT tied to GND (disconnected),
    and NO bottom fill anywhere."""
    grid = RoutingGrid(10.0, 10.0, 1.0)
    n = grid.cols * grid.rows
    filled_top = [False] * n
    filled_bottom = [False] * n          # nothing on the bottom layer
    for r in range(3, 6):
        for c in range(3, 6):
            filled_top[r * grid.cols + c] = True   # island cells, left as EMPTY net
    return grid, filled_top, filled_bottom


def test_island_not_rescued_without_inner_plane():
    grid, ft, fb = _island_grid()
    vias = _add_rescue_vias(ft, fb, grid, FILL_NET, RouterConfig(),
                            inner_gnd_plane=False)
    assert vias == [], "2-layer: no bottom fill underneath -> cannot rescue (unchanged)"


def test_island_rescued_to_inner_gnd_plane():
    grid, ft, fb = _island_grid()
    vias = _add_rescue_vias(ft, fb, grid, FILL_NET, RouterConfig(),
                            inner_gnd_plane=True)
    assert len(vias) == 1, "4-layer: a through-via must rescue the island to In1 GND"
    v = vias[0]
    assert v.from_layer == "top" and v.to_layer == "bottom"   # through via spans In1
