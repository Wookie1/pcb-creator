"""Connectors are distributed around the board perimeter without overlapping.

Regression for the morgan placement bug: the grid placer stacked all connectors
on the left edge and collapsed any overflow onto a single right-edge point (8
connectors landed 3 at the identical spot), and positioned by body centre so
terminal-block / FFC pads overhung the edge clearance — and since connectors are
pinned, repair could never fix it, so placement failed on a 22%-full board.
"""

from optimizers.initial_placement import generate_grid_placement
from optimizers.placement_optimizer import (
    find_placement_violations, MIN_CLEARANCE_MM,
)


def _netlist(n_connectors: int) -> dict:
    # Non-resolving package names → uniform fallback footprints, so the test is
    # deterministic regardless of which KiCad libraries are installed.
    elements = []
    for i in range(1, n_connectors + 1):
        cid = f"comp_J{i}"
        elements.append({"element_type": "component", "component_id": cid,
                         "designator": f"J{i}", "component_type": "connector",
                         "value": "conn", "package": f"GENERIC_CONN_{i}"})
        for p in range(1, 3):
            elements.append({"element_type": "port", "port_id": f"p_{cid}_{p}",
                             "component_id": cid, "pin_number": p, "name": f"P{p}"})
    return {"version": "1.0", "project_name": "t", "elements": elements}


class TestConnectorDistribution:
    def test_no_connector_overlaps_or_oob(self):
        # A morgan-like load (8 connectors that comfortably fit the perimeter):
        # they must spread across edges without overlapping or overhanging.
        nl = _netlist(8)
        pl = generate_grid_placement(nl, 100.0, 50.0, "t", layers=2)
        assert pl is not None
        v = find_placement_violations(pl, nl, clearance=MIN_CLEARANCE_MM)
        # No connector should be out of bounds (pads must clear the edge), and no
        # two pinned connectors should overlap (the collapse-onto-one-point bug).
        conn = {it["designator"] for it in pl["placements"]
                if it["component_type"] == "connector"}
        oob_conn = [e for e in v["out_of_bounds"] if e["designator"] in conn]
        assert not oob_conn, f"connectors out of bounds: {oob_conn}"
        conn_overlaps = [o for o in v["overlaps"]
                         if o["a"] in conn and o["b"] in conn]
        assert not conn_overlaps, f"connectors overlap: {conn_overlaps}"

    def test_connectors_distinct_positions(self):
        nl = _netlist(10)
        pl = generate_grid_placement(nl, 100.0, 50.0, "t", layers=2)
        pos = [(round(it["x_mm"], 1), round(it["y_mm"], 1))
               for it in pl["placements"] if it["component_type"] == "connector"]
        assert len(set(pos)) == len(pos), "connectors share identical positions"
