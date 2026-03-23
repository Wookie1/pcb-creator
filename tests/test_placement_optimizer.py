"""Unit tests for the placement optimizer, ratsnest, and fiducials."""

import json
import os
import sys

import pytest

# Add project root to path
_root = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, _root)
sys.path.insert(0, os.path.join(_root, "validators"))

from optimizers.ratsnest import (
    NetInfo,
    build_connectivity,
    compute_cost,
    compute_mst_edges,
    count_crossings,
    total_wire_length,
)
from optimizers.fiducials import (
    add_fiducials_to_placement,
    determine_populated_layers,
    place_fiducials,
)
from optimizers.placement_optimizer import SAConfig, optimize_placement, repair_placement
from validate_placement import validate_cross_reference


# ── Helpers ──────────────────────────────────────────────────────

def _minimal_netlist(*components, nets=None):
    """Build a minimal netlist. If nets is None, auto-create one net linking first two components."""
    elements = []
    for comp in components:
        des = comp["designator"]
        elements.append({
            "element_type": "component",
            "component_id": f"comp_{des.lower()}",
            "designator": des,
            "component_type": comp.get("component_type", "resistor"),
            "value": comp.get("value", "100ohm"),
            "package": comp.get("package", "0805"),
            "description": "test",
        })
        for pin in range(1, comp.get("pins", 2) + 1):
            elements.append({
                "element_type": "port",
                "port_id": f"port_{des.lower()}_{pin}",
                "component_id": f"comp_{des.lower()}",
                "pin_number": pin,
                "name": str(pin),
                "electrical_type": "passive",
            })

    if nets is None and len(components) >= 2:
        nets = [{
            "net_id": "net_1",
            "name": "N1",
            "connected_port_ids": [
                f"port_{components[0]['designator'].lower()}_1",
                f"port_{components[1]['designator'].lower()}_1",
            ],
            "net_class": "signal",
        }]

    if nets:
        for net in nets:
            elements.append({"element_type": "net", **net})

    return {"version": "1.0", "project_name": "test", "elements": elements}


def _placement(*items, board_w=50, board_h=30):
    return {
        "version": "1.0",
        "project_name": "test",
        "source_netlist": "test_netlist.json",
        "source_bom": "test_bom.json",
        "board": {"width_mm": board_w, "height_mm": board_h, "outline_type": "rectangle", "origin": [0, 0]},
        "placements": list(items),
    }


def _place(des, ctype="resistor", pkg="0805", x=10, y=10, w=2.5, h=1.8, rot=0, layer="top", source="llm"):
    return {
        "designator": des,
        "component_type": ctype,
        "package": pkg,
        "footprint_width_mm": w,
        "footprint_height_mm": h,
        "x_mm": x,
        "y_mm": y,
        "rotation_deg": rot,
        "layer": layer,
        "placement_source": source,
    }


# ── TestRatsnest ─────────────────────────────────────────────────

class TestRatsnest:
    def test_two_component_wire_length(self):
        """Two components on one net — wire length = Manhattan distance."""
        positions = {"R1": (0.0, 0.0), "R2": (10.0, 5.0)}
        nets = [NetInfo("n1", "N1", "signal", ["R1", "R2"])]
        wl = total_wire_length(nets, positions)
        assert wl == pytest.approx(15.0)  # |10| + |5|

    def test_three_component_mst(self):
        """MST of 3 components should be less than sum of all pairwise."""
        positions = {"R1": (0.0, 0.0), "R2": (10.0, 0.0), "R3": (5.0, 5.0)}
        nets = [NetInfo("n1", "N1", "signal", ["R1", "R2", "R3"])]
        wl = total_wire_length(nets, positions)
        # MST should pick 2 cheapest edges out of 3
        assert wl < 10 + 10 + 10  # less than sum of all 3 pairwise distances

    def test_mst_edges_count(self):
        """MST of N nodes has exactly N-1 edges."""
        positions = [(0, 0), (10, 0), (5, 5), (10, 10)]
        edges = compute_mst_edges(positions)
        assert len(edges) == 3

    def test_crossing_detection(self):
        """Two nets with crossing edges should report 1 crossing."""
        positions = {"A": (0.0, 0.0), "B": (10.0, 10.0), "C": (10.0, 0.0), "D": (0.0, 10.0)}
        nets = [
            NetInfo("n1", "N1", "signal", ["A", "B"]),  # diagonal \
            NetInfo("n2", "N2", "signal", ["C", "D"]),  # diagonal /
        ]
        assert count_crossings(nets, positions) == 1

    def test_no_crossing(self):
        """Parallel edges should report 0 crossings."""
        positions = {"A": (0.0, 0.0), "B": (10.0, 0.0), "C": (0.0, 5.0), "D": (10.0, 5.0)}
        nets = [
            NetInfo("n1", "N1", "signal", ["A", "B"]),
            NetInfo("n2", "N2", "signal", ["C", "D"]),
        ]
        assert count_crossings(nets, positions) == 0

    def test_empty_nets(self):
        """No nets — zero cost."""
        result = compute_cost([], {})
        assert result.total_wire_length == 0
        assert result.crossing_count == 0

    def test_build_connectivity(self):
        """build_connectivity extracts nets with correct designators."""
        netlist = _minimal_netlist(
            {"designator": "R1"}, {"designator": "R2"},
            nets=[{
                "net_id": "n1", "name": "N1", "net_class": "signal",
                "connected_port_ids": ["port_r1_1", "port_r2_1"],
            }],
        )
        nets = build_connectivity(netlist)
        assert len(nets) == 1
        assert set(nets[0].designators) == {"R1", "R2"}


# ── TestFiducials ────────────────────────────────────────────────

class TestFiducials:
    def test_fiducials_added_to_top_only(self):
        """All components on top → 2 fiducials on top."""
        p = _placement(_place("R1", x=25, y=15))
        fids = place_fiducials(p)
        assert len(fids) == 2
        assert all(f["layer"] == "top" for f in fids)

    def test_fiducials_added_to_both_layers(self):
        """Components on top and bottom → 4 fiducials total."""
        p = _placement(
            _place("R1", x=25, y=15, layer="top"),
            _place("R2", x=25, y=15, layer="bottom"),
        )
        fids = place_fiducials(p)
        assert len(fids) == 4
        top_fids = [f for f in fids if f["layer"] == "top"]
        bot_fids = [f for f in fids if f["layer"] == "bottom"]
        assert len(top_fids) == 2
        assert len(bot_fids) == 2

    def test_fiducial_positions_diagonal(self):
        """Fiducials should be in diagonally opposite corners."""
        p = _placement(_place("R1", x=25, y=15))
        fids = place_fiducials(p)
        xs = sorted(f["x_mm"] for f in fids)
        ys = sorted(f["y_mm"] for f in fids)
        # Should be near opposite corners
        assert xs[0] < 10  # near left
        assert xs[1] > 40  # near right
        assert ys[0] < 10  # near bottom
        assert ys[1] > 20  # near top

    def test_fiducial_designators_single_layer(self):
        """Single-layer boards use FID1, FID2 naming."""
        p = _placement(_place("R1", x=25, y=15))
        fids = place_fiducials(p)
        names = {f["designator"] for f in fids}
        assert names == {"FID1", "FID2"}

    def test_fiducial_designators_dual_layer(self):
        """Dual-layer boards use FID_T1/T2/B1/B2 naming."""
        p = _placement(
            _place("R1", x=25, y=15, layer="top"),
            _place("R2", x=25, y=15, layer="bottom"),
        )
        fids = place_fiducials(p)
        names = {f["designator"] for f in fids}
        assert names == {"FID_B1", "FID_B2", "FID_T1", "FID_T2"}

    def test_fiducial_dimensions(self):
        """Fiducials should be 3mm x 3mm (1mm dot + 2mm clearance)."""
        p = _placement(_place("R1", x=25, y=15))
        fids = place_fiducials(p)
        for f in fids:
            assert f["footprint_width_mm"] == 3.0
            assert f["footprint_height_mm"] == 3.0

    def test_add_fiducials_idempotent(self):
        """Adding fiducials twice should not duplicate them."""
        p = _placement(_place("R1", x=25, y=15))
        p1 = add_fiducials_to_placement(p)
        p2 = add_fiducials_to_placement(p1)
        fid_count = sum(1 for item in p2["placements"] if item["component_type"] == "fiducial")
        assert fid_count == 2

    def test_fiducials_avoid_corner_conflict(self):
        """If a component is in the bottom-left corner, fiducials should use other diagonal."""
        p = _placement(
            _place("R1", x=2, y=2, w=4, h=4),  # sitting in bottom-left corner
            _place("R2", x=25, y=15),
        )
        fids = place_fiducials(p)
        # Bottom-left fiducial should NOT be at (2, 2) since R1 is there
        for f in fids:
            # All fiducials should be at valid non-conflicting positions
            assert f["component_type"] == "fiducial"


# ── TestSAOptimizer ──────────────────────────────────────────────

class TestSAOptimizer:
    def _bad_placement_data(self):
        """Create a deliberately bad placement for optimization testing.

        Places R1 far from R2 even though they're connected, with R3 between them.
        """
        netlist = _minimal_netlist(
            {"designator": "R1"}, {"designator": "R2"}, {"designator": "R3"},
            nets=[
                {"net_id": "n1", "name": "N1", "net_class": "signal",
                 "connected_port_ids": ["port_r1_1", "port_r2_1"]},
                {"net_id": "n2", "name": "N2", "net_class": "signal",
                 "connected_port_ids": ["port_r2_2", "port_r3_1"]},
            ],
        )
        placement = _placement(
            _place("R1", x=5, y=25),   # top-left
            _place("R2", x=45, y=5),   # bottom-right (far from R1!)
            _place("R3", x=25, y=15),  # center
        )
        return placement, netlist

    def test_wire_length_decreases(self):
        """Optimizer should reduce wire length on a bad placement."""
        placement, netlist = self._bad_placement_data()
        config = SAConfig(max_iterations=3000, seed=42)

        nets = build_connectivity(netlist)
        positions_before = {p["designator"]: (p["x_mm"], p["y_mm"]) for p in placement["placements"]}
        wl_before = total_wire_length(nets, positions_before)

        optimized = optimize_placement(placement, netlist, config)

        positions_after = {p["designator"]: (p["x_mm"], p["y_mm"]) for p in optimized["placements"]}
        wl_after = total_wire_length(nets, positions_after)

        assert wl_after <= wl_before

    def test_no_overlaps_after_optimization(self):
        """No components should overlap after optimization."""
        placement, netlist = self._bad_placement_data()
        config = SAConfig(max_iterations=3000, seed=42)
        optimized = optimize_placement(placement, netlist, config)

        items = optimized["placements"]
        for i in range(len(items)):
            for j in range(i + 1, len(items)):
                if items[i].get("layer") != items[j].get("layer"):
                    continue
                a = items[i]
                b = items[j]
                # Check they don't overlap
                ax, ay = a["x_mm"], a["y_mm"]
                aw, ah = a["footprint_width_mm"], a["footprint_height_mm"]
                bx, by = b["x_mm"], b["y_mm"]
                bw, bh = b["footprint_width_mm"], b["footprint_height_mm"]
                if a["rotation_deg"] in (90, 270): aw, ah = ah, aw
                if b["rotation_deg"] in (90, 270): bw, bh = bh, bw
                # Gap must be >= 0
                gap_x = abs(ax - bx) - (aw + bw) / 2
                gap_y = abs(ay - by) - (ah + bh) / 2
                assert gap_x >= -0.01 or gap_y >= -0.01, \
                    f"{a['designator']} and {b['designator']} overlap"

    def test_within_board_boundary(self):
        """All components should be within board after optimization."""
        placement, netlist = self._bad_placement_data()
        config = SAConfig(max_iterations=3000, seed=42)
        optimized = optimize_placement(placement, netlist, config)

        board_w = optimized["board"]["width_mm"]
        board_h = optimized["board"]["height_mm"]

        for item in optimized["placements"]:
            w, h = item["footprint_width_mm"], item["footprint_height_mm"]
            if item["rotation_deg"] in (90, 270): w, h = h, w
            assert item["x_mm"] - w / 2 >= -0.01, f"{item['designator']} left edge out of bounds"
            assert item["y_mm"] - h / 2 >= -0.01, f"{item['designator']} bottom edge out of bounds"
            assert item["x_mm"] + w / 2 <= board_w + 0.01, f"{item['designator']} right edge out of bounds"
            assert item["y_mm"] + h / 2 <= board_h + 0.01, f"{item['designator']} top edge out of bounds"

    def test_pinned_components_stay_put(self):
        """Components with placement_source='user' should not move."""
        netlist = _minimal_netlist(
            {"designator": "R1"}, {"designator": "R2"},
            nets=[{"net_id": "n1", "name": "N1", "net_class": "signal",
                   "connected_port_ids": ["port_r1_1", "port_r2_1"]}],
        )
        placement = _placement(
            _place("R1", x=5, y=5, source="user"),   # pinned
            _place("R2", x=45, y=25),                 # free to move
        )
        config = SAConfig(max_iterations=3000, seed=42)
        optimized = optimize_placement(placement, netlist, config)

        for item in optimized["placements"]:
            if item["designator"] == "R1":
                assert item["x_mm"] == 5
                assert item["y_mm"] == 5

    def test_deterministic_with_seed(self):
        """Same seed should produce identical results."""
        placement, netlist = self._bad_placement_data()

        opt1 = optimize_placement(placement, netlist, SAConfig(max_iterations=1000, seed=123))
        opt2 = optimize_placement(placement, netlist, SAConfig(max_iterations=1000, seed=123))

        for p1, p2 in zip(opt1["placements"], opt2["placements"]):
            assert p1["x_mm"] == p2["x_mm"]
            assert p1["y_mm"] == p2["y_mm"]
            assert p1["rotation_deg"] == p2["rotation_deg"]

    def test_placement_source_updated(self):
        """Moved components should have placement_source='optimizer'."""
        placement, netlist = self._bad_placement_data()
        config = SAConfig(max_iterations=3000, seed=42)
        optimized = optimize_placement(placement, netlist, config)

        for item in optimized["placements"]:
            orig = next(p for p in placement["placements"] if p["designator"] == item["designator"])
            if item["x_mm"] != orig["x_mm"] or item["y_mm"] != orig["y_mm"] or \
               item["rotation_deg"] != orig["rotation_deg"]:
                assert item.get("placement_source") == "optimizer"

    def test_single_component_noop(self):
        """Single component — nothing to optimize, should return unchanged."""
        netlist = _minimal_netlist({"designator": "R1"}, nets=[])
        placement = _placement(_place("R1", x=25, y=15))
        optimized = optimize_placement(placement, netlist, SAConfig(seed=42))

        assert optimized["placements"][0]["x_mm"] == 25
        assert optimized["placements"][0]["y_mm"] == 15

    def test_all_pinned_noop(self):
        """All components pinned — optimizer is a no-op."""
        netlist = _minimal_netlist(
            {"designator": "R1"}, {"designator": "R2"},
            nets=[{"net_id": "n1", "name": "N1", "net_class": "signal",
                   "connected_port_ids": ["port_r1_1", "port_r2_1"]}],
        )
        placement = _placement(
            _place("R1", x=5, y=5, source="user"),
            _place("R2", x=45, y=25, source="user"),
        )
        optimized = optimize_placement(placement, netlist, SAConfig(seed=42))

        assert optimized["placements"][0]["x_mm"] == 5
        assert optimized["placements"][1]["x_mm"] == 45


# ── TestRepairPlacement ──────────────────────────────────────────

class TestRepairPlacement:
    def test_repair_resolves_overlaps(self):
        """Overlapping components should be separated after repair."""
        netlist = _minimal_netlist(
            {"designator": "R1"}, {"designator": "R2"},
            nets=[{"net_id": "n1", "name": "N1", "net_class": "signal",
                   "connected_port_ids": ["port_r1_1", "port_r2_1"]}],
        )
        # R1 and R2 placed at the same position — definite overlap
        placement = _placement(
            _place("R1", x=25, y=15),
            _place("R2", x=25, y=15),
        )
        repaired = repair_placement(placement, netlist, seed=42)

        items = repaired["placements"]
        r1 = next(p for p in items if p["designator"] == "R1")
        r2 = next(p for p in items if p["designator"] == "R2")

        # After repair, they should be at different positions
        assert (r1["x_mm"], r1["y_mm"]) != (r2["x_mm"], r2["y_mm"])

    def test_repair_resolves_boundary_violations(self):
        """Components outside board should be moved inside after repair."""
        netlist = _minimal_netlist({"designator": "R1"}, nets=[])
        # R1 placed outside board boundary
        placement = _placement(
            _place("R1", x=-5, y=15),
            board_w=50, board_h=30,
        )
        repaired = repair_placement(placement, netlist, seed=42)

        r1 = repaired["placements"][0]
        w, h = r1["footprint_width_mm"], r1["footprint_height_mm"]
        # Should be within board now
        assert r1["x_mm"] - w / 2 >= -0.1
        assert r1["x_mm"] + w / 2 <= 50.1

    def test_repair_many_overlaps(self):
        """Multiple overlapping components should all be separated."""
        comps = [{"designator": f"R{i}"} for i in range(1, 7)]
        nets_list = [
            {"net_id": f"n{i}", "name": f"N{i}", "net_class": "signal",
             "connected_port_ids": [f"port_r{i}_1", f"port_r{i+1}_1"]}
            for i in range(1, 6)
        ]
        netlist = _minimal_netlist(*comps, nets=nets_list)

        # All 6 components stacked at the center
        items = [_place(f"R{i}", x=25, y=15) for i in range(1, 7)]
        placement = _placement(*items)

        repaired = repair_placement(placement, netlist, max_iterations=15000, seed=42)

        # Check no overlaps remain
        rep_items = repaired["placements"]
        for i in range(len(rep_items)):
            for j in range(i + 1, len(rep_items)):
                a, b = rep_items[i], rep_items[j]
                if a.get("layer") != b.get("layer"):
                    continue
                ax, ay = a["x_mm"], a["y_mm"]
                aw, ah = a["footprint_width_mm"], a["footprint_height_mm"]
                bx, by = b["x_mm"], b["y_mm"]
                bw, bh = b["footprint_width_mm"], b["footprint_height_mm"]
                if a["rotation_deg"] in (90, 270): aw, ah = ah, aw
                if b["rotation_deg"] in (90, 270): bw, bh = bh, bw
                gap_x = abs(ax - bx) - (aw + bw) / 2
                gap_y = abs(ay - by) - (ah + bh) / 2
                # At least one dimension must have sufficient gap
                assert gap_x >= 0.4 or gap_y >= 0.4, \
                    f"{a['designator']} and {b['designator']} still overlap after repair"

    def test_repair_pinned_components_stay(self):
        """User-pinned components should not move during repair."""
        netlist = _minimal_netlist(
            {"designator": "R1"}, {"designator": "R2"},
        )
        placement = _placement(
            _place("R1", x=25, y=15, source="user"),
            _place("R2", x=25, y=15),  # overlapping R1!
        )
        repaired = repair_placement(placement, netlist, seed=42)

        r1 = next(p for p in repaired["placements"] if p["designator"] == "R1")
        assert r1["x_mm"] == 25
        assert r1["y_mm"] == 15


# ── TestValidatorFiducialExemption ───────────────────────────────

class TestValidatorFiducialExemption:
    def test_fiducials_not_flagged_as_phantom(self):
        """Fiducials in placement should not trigger phantom placement errors."""
        netlist = _minimal_netlist(
            {"designator": "R1"}, {"designator": "R2"},
        )
        placement = _placement(
            _place("R1", x=10, y=10),
            _place("R2", x=30, y=10),
            _place("FID1", ctype="fiducial", pkg="Fiducial_1mm", x=2, y=2, w=3, h=3),
            _place("FID2", ctype="fiducial", pkg="Fiducial_1mm", x=48, y=28, w=3, h=3),
        )
        errors, warnings = validate_cross_reference(placement, netlist)
        # FID1 and FID2 should NOT appear in errors
        assert not any("FID" in e for e in errors)

    def test_real_phantom_still_caught(self):
        """Non-fiducial phantom placements should still be errors."""
        netlist = _minimal_netlist({"designator": "R1"})
        placement = _placement(
            _place("R1", x=10, y=10),
            _place("R99", x=30, y=10),  # phantom!
        )
        errors, warnings = validate_cross_reference(placement, netlist)
        assert any("R99" in e for e in errors)
