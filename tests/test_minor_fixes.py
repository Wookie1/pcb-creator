"""Unit checks for the MINOR fixes from PCB_CREATOR_ISSUES_REPORT.md.

#14 export refreshes a stale hand-off .kicad_pcb; #17 the title/rev silk block
stays inside the board with an edge margin. (#15/#16 are docstring/guidance
strings — verified by reading, no behavioural test.)
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))


# --- #14 export_outputs keeps a hand-off .kicad_pcb current ------------------

def test_run_export_refreshes_existing_kicad_pcb(tmp_path):
    from orchestrator import stages
    # Reuse the coverage suite's proven placed+routed scaffolding.
    from tests.test_mcp_stages_coverage import _routed_on_disk, _cfg
    pdir, name = _routed_on_disk(tmp_path, "kcref")
    # A prior export_kicad left a STALE .kicad_pcb in output/.
    out = pdir / "output"
    out.mkdir(exist_ok=True)
    stale = out / f"{name}.kicad_pcb"
    stale.write_text("(kicad_pcb STALE)")
    r = stages.run_export(pdir, name, _cfg())
    assert r["success"], r.get("error")
    # It was regenerated from the current board, not left stale.
    text = stale.read_text()
    assert text != "(kicad_pcb STALE)"
    assert "kicad_pcb" in text


def test_run_export_does_not_create_kicad_pcb_when_absent(tmp_path):
    from orchestrator import stages
    from tests.test_mcp_stages_coverage import _routed_on_disk, _cfg
    pdir, name = _routed_on_disk(tmp_path, "kcabs")
    stages.run_export(pdir, name, _cfg())
    # The KiCad path was never used, so export must not invent a .kicad_pcb.
    assert not (pdir / "output" / f"{name}.kicad_pcb").exists()


# --- #17 title/rev silk block stays inside the outline ----------------------

def test_title_block_stays_within_board_margin():
    from optimizers.router import (
        _generate_silkscreen, _silk_text_bbox, SILK_EDGE_MARGIN_MM,
    )
    from tests.test_router_coverage import _two_pad_board
    from optimizers.pad_geometry import build_pad_map

    placement, netlist = _two_pad_board()
    placement["project_name"] = "a_long_board_name"  # forces truncation + edge
    board = placement["board"]
    bw, bh = board["width_mm"], board["height_mm"]
    pad_map = build_pad_map(placement, netlist)
    silk = _generate_silkscreen(placement, netlist, pad_map)

    m = SILK_EDGE_MARGIN_MM
    for item in silk:
        if item.get("type") != "text":
            continue
        bb = _silk_text_bbox(item["x_mm"], item["y_mm"], item["text"],
                             item.get("font_height_mm", 1.0),
                             item.get("anchor", "center"), item.get("angle", 0))
        assert bb[0] >= m - 1e-6 and bb[1] >= m - 1e-6, (item, bb)
        assert bb[2] <= bw - m + 1e-6 and bb[3] <= bh - m + 1e-6, (item, bb)
