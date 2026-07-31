"""Unit checks for the high-severity fixes from PCB_CREATOR_ISSUES_REPORT.md.

Pure-function coverage (no Java/kicad-cli): the staleness signatures (#4), the
synthesized-BOM re-sync helpers (#5), and the footprint tier order (#19). The
gate/validation fixes (#7, #8) and the unstitched-pad surfacing (#6) are covered
at the MCP boundary in test_mcp_stages_coverage.py.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))


# --- #4 staleness signatures ------------------------------------------------

def test_netlist_sig_changes_on_package_swap():
    from orchestrator.stages import netlist_sig
    base = {"elements": [{"element_type": "component", "designator": "R3",
                          "value": "470", "package": "0603"}]}
    swapped = json.loads(json.dumps(base))
    swapped["elements"][0]["package"] = "1206"
    assert netlist_sig(base) == netlist_sig(json.loads(json.dumps(base)))  # stable
    assert netlist_sig(base) != netlist_sig(swapped)                       # sensitive


def test_placement_sig_changes_on_move():
    from orchestrator.stages import placement_sig
    a = {"board": {"width_mm": 30, "height_mm": 20, "layers": 2},
         "placements": [{"designator": "R3", "x_mm": 1, "y_mm": 2,
                         "rotation_deg": 0, "layer": "top"}]}
    b = json.loads(json.dumps(a))
    b["placements"][0]["x_mm"] = 5.0
    assert placement_sig(a) != placement_sig(b)


def test_staleness_reason_flags_edit_and_tolerates_unstamped(tmp_path):
    from orchestrator import stages
    name = "p"
    netlist = {"elements": [{"element_type": "component", "designator": "R3",
                             "value": "470", "package": "0603"}]}
    placement = {"board": {"width_mm": 30, "height_mm": 20, "layers": 2},
                 "placements": []}
    (tmp_path / f"{name}_netlist.json").write_text(json.dumps(netlist))
    (tmp_path / f"{name}_placement.json").write_text(json.dumps(placement))
    stamped = {"routing": {},
               "source_sig": {"netlist": stages.netlist_sig(netlist),
                              "placement": stages.placement_sig(placement)}}
    (tmp_path / f"{name}_routed.json").write_text(json.dumps(stamped))
    assert stages.staleness_reason(tmp_path, name) is None

    # Edit the netlist (package swap) — routed board is now stale.
    netlist["elements"][0]["package"] = "1206"
    (tmp_path / f"{name}_netlist.json").write_text(json.dumps(netlist))
    assert "netlist" in (stages.staleness_reason(tmp_path, name) or "")

    # A routed board with no stamp can't be judged — must not false-alarm.
    (tmp_path / f"{name}_routed.json").write_text(json.dumps({"routing": {}}))
    assert stages.staleness_reason(tmp_path, name) is None


# --- #5 synthesized-BOM re-sync --------------------------------------------

def test_bom_from_netlist_is_tagged():
    from orchestrator.stages import _bom_from_netlist
    nl = {"elements": [{"element_type": "component", "component_type": "resistor",
                        "designator": "R3", "value": "470", "package": "1206"}]}
    bom = _bom_from_netlist(nl)
    assert bom["synthesized_from_netlist"] is True
    assert bom["bom"][0]["package"] == "1206"


def test_carry_bom_part_numbers_matches_by_key():
    from orchestrator.stages import _carry_bom_part_numbers
    old = {"bom": [{"component_type": "resistor", "value": "470",
                    "package": "0603", "lcsc": "C1", "mpn": "M1"}]}
    new = {"bom": [{"component_type": "resistor", "value": "470", "package": "0603"},
                   {"component_type": "resistor", "value": "470", "package": "1206"}]}
    _carry_bom_part_numbers(old, new)
    # Same type/value/package → part number carried across the rebuild.
    assert new["bom"][0]["lcsc"] == "C1" and new["bom"][0]["mpn"] == "M1"
    # Genuinely different part (package changed) → NOT carried.
    assert "lcsc" not in new["bom"][1]


# --- #19 footprint tier order ----------------------------------------------

class _Miss:
    def get_footprint(self, package, pin_count):
        return None


class _FakeCache:
    """Agent-supplied geometry (provide_footprint) lives in the cache tier."""
    def get_footprint(self, package):
        return {"pin_offsets": {"1": [-1.0, 0.0], "2": [1.0, 0.0]},
                "pad_size": [0.3, 1.2]}  # correct fine-pitch pad, long axis outward


def test_cache_geometry_beats_ipc_generator():
    from optimizers.pad_geometry import get_footprint_def
    # "MSOP-14" matches the IPC name regex and would otherwise get the broken
    # self-overlapping generated footprint. The agent-supplied cache entry must win.
    fp = get_footprint_def("MSOP-14", 14, custom_index=_Miss(),
                           kicad_index=_Miss(), cache=_FakeCache())
    assert fp is not None
    assert tuple(fp.pad_size) == (0.3, 1.2)
