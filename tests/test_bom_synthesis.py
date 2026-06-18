"""The manufacturing package always ships a BOM CSV.

Regression for the "no BOM" half of the incomplete-manufacturing-package report:
export only wrote a BOM when a standalone <project>_bom.json existed, so boards
imported from a KiCad netlist (no BOM file) silently shipped without one — even
though every component is in the netlist.
"""

from orchestrator.stages import _bom_from_netlist


def _nl(*comps):
    elements = []
    for des, value, pkg, ctype in comps:
        elements.append({"element_type": "component", "designator": des,
                         "value": value, "package": pkg, "component_type": ctype})
    return {"version": "1.0", "project_name": "t", "elements": elements}


class TestBomFromNetlist:
    def test_groups_by_value_and_package(self):
        nl = _nl(("R1", "10k", "R_0805", "resistor"),
                 ("R2", "10k", "R_0805", "resistor"),
                 ("R3", "1k", "R_0805", "resistor"))
        bom = _bom_from_netlist(nl)["bom"]
        by_val = {b["value"]: b for b in bom}
        assert by_val["10k"]["quantity"] == 2
        assert by_val["10k"]["designator"] == "R1, R2"
        assert by_val["1k"]["quantity"] == 1

    def test_excludes_mounting_holes_and_fiducials(self):
        nl = _nl(("U1", "MCU", "QFP-48", "ic"),
                 ("H1", "", "MountingHole_3.2mm_M3", "ic"),
                 ("FID1", "", "Fiducial_1mm", "fiducial"))
        bom = _bom_from_netlist(nl)["bom"]
        dess = {d.strip() for b in bom for d in b["designator"].split(",")}
        assert "U1" in dess
        assert "H1" not in dess and "FID1" not in dess

    def test_natural_designator_sort(self):
        nl = _nl(*[(f"R{i}", "10k", "R_0805", "resistor") for i in (1, 2, 10, 3)])
        bom = _bom_from_netlist(nl)["bom"]
        assert bom[0]["designator"] == "R1, R2, R3, R10"

    def test_empty_netlist(self):
        assert _bom_from_netlist({"elements": []})["bom"] == []
