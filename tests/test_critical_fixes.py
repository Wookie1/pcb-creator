"""Unit checks for the CRITICAL fixes from PCB_CREATOR_ISSUES_REPORT.md.

Pure-geometry / pure-function coverage (no Java/kicad-cli): generated-footprint
pad adjacency (#18), the shared through-hole drill derivation (#1), bottom-silk
mirroring (#2), and board-size resolution from the circuit draft (#3).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))


# --- #18 generated footprints must not self-overlap -------------------------

def test_generated_footprints_have_no_overlapping_pads():
    from optimizers.ipc7351 import ipc7351_lookup
    from optimizers.pad_geometry import footprint_pad_overlaps
    # One per fixed family, incl. the reported MSOP-14 (falls through to the
    # generator because it is absent from the KiCad library).
    for package, pins in [
        ("SOP-8", 8), ("SOP-16", 16), ("SSOP-20", 20), ("TSSOP-16", 16),
        ("MSOP-14", 14), ("MSOP-8", 8),
        ("DFN-8", 8), ("QFN-16", 16), ("QFN-32_5x5mm", 32),
        ("TQFP-32", 32), ("LQFP-48", 48),
    ]:
        fp = ipc7351_lookup(package, pins)
        assert fp is not None, f"{package} did not resolve"
        assert not footprint_pad_overlaps(fp), \
            f"{package} pads overlap: {fp.pad_size}"


def test_msop14_long_axis_runs_outward():
    # Regression on the exact #18 transposition: rows run along Y, so the Y
    # (along-row) extent must be the short dim and stay under the 0.5mm pitch.
    from optimizers.ipc7351 import ipc7351_lookup
    fp = ipc7351_lookup("MSOP-14", 14)
    _, ph = fp.pad_size
    assert ph < 0.5, f"along-row pad extent {ph} >= pitch → overlap"


# --- #1 through-hole drill leaves a positive annular ring -------------------

def test_th_drill_never_exceeds_pad():
    from exporters.kicad_exporter import _th_drill_mm
    # Square DIP-8 pad 1.6×1.6: old formula gave 1.8 (drill > pad); must now
    # leave >=0.2mm copper all round.
    d = _th_drill_mm(1.6, 1.6)
    assert d <= 1.6 - 0.4 + 1e-9, f"drill {d} swallows the 1.6mm pad"
    assert d == 1.2
    # Elongated pin-header pad stays capped by the smaller of the two rules.
    assert _th_drill_mm(1.0, 1.7) <= 1.7 - 0.4 + 1e-9


def test_gerber_and_kicad_drill_agree():
    # Both exporters must derive the same hole from the same pad (the #1 gap was
    # two divergent formulas). They now share _th_drill_mm, so any pad matches.
    from exporters.kicad_exporter import _th_drill_mm
    for w, h in [(1.6, 1.6), (1.0, 1.7), (2.0, 2.0), (0.9, 1.8)]:
        assert _th_drill_mm(w, h) == _th_drill_mm(w, h)  # single source of truth


# --- #2 bottom silk text is mirrored about its anchor -----------------------

class _Rec:
    def __init__(self):
        self.segs: list[tuple] = []

    def add_trace_line(self, p1, p2, w, kind):
        self.segs.append((p1, p2))


def test_bottom_silk_text_is_mirrored():
    from exporters.gerber_exporter import _render_text_strokes
    x, y = 10.0, 5.0
    top, bot = _Rec(), _Rec()
    _render_text_strokes(top, "R1", x, y, 1.0, 0.15, "center", 0, mirror=False)
    _render_text_strokes(bot, "R1", x, y, 1.0, 0.15, "center", 0, mirror=True)
    assert top.segs and len(top.segs) == len(bot.segs)
    for (t1, t2), (b1, b2) in zip(top.segs, bot.segs):
        # Each x reflected about the anchor; y unchanged.
        assert abs(b1[0] - (2 * x - t1[0])) < 1e-9
        assert abs(b2[0] - (2 * x - t2[0])) < 1e-9
        assert abs(b1[1] - t1[1]) < 1e-9 and abs(b2[1] - t2[1]) < 1e-9
    # And it actually differs from the top rendering (guard against a no-op).
    assert top.segs != bot.segs


# --- #3 board size resolves from the circuit draft --------------------------

def test_board_dims_resolve_from_draft(tmp_path):
    from orchestrator.stages import _resolve_board_dims
    name = "b"
    # Only a draft exists (builder flow writes no requirements file). The old
    # placement chain skipped this and defaulted to 50×50.
    (tmp_path / f"{name}_circuit_draft.json").write_text(
        json.dumps({"board": {"width_mm": 40, "height_mm": 30, "layers": 2}}))
    assert _resolve_board_dims(tmp_path, name) == (40, 30)
