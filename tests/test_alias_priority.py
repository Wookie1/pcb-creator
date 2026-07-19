"""Alias-collision root cause: index build order must be deterministic.

_build_index used to be single-pass "first rglob file wins", so the "SOT-23"
alias generated from SOT-23-5_HandSoldering could shadow the real
SOT-23.kicad_mod depending on scan order. Exact filename stems are now
registered before generated short aliases, making resolution order-independent.
"""

from exporters.kicad_mod_parser import KiCadLibraryIndex

_SOT23 = """(footprint "SOT-23" (layer "F.Cu")
  (pad "1" smd roundrect (at 0 0) (size 0.6 0.9))
  (pad "2" smd roundrect (at 0.95 0) (size 0.6 0.9))
  (pad "3" smd roundrect (at 1.9 0) (size 0.6 0.9))
)
"""

_SOT235 = """(footprint "SOT-23-5_HandSoldering" (layer "F.Cu")
  (pad "1" smd roundrect (at 0 0) (size 0.6 0.9))
  (pad "2" smd roundrect (at 0.95 0) (size 0.6 0.9))
  (pad "3" smd roundrect (at 1.9 0) (size 0.6 0.9))
  (pad "4" smd roundrect (at 1.9 1.8) (size 0.6 0.9))
  (pad "5" smd roundrect (at 0 1.8) (size 0.6 0.9))
)
"""


def test_exact_stem_beats_generated_alias_any_scan_order(tmp_path):
    # "AAA_..." sorts (and typically rglobs) before "SOT-23.kicad_mod", so the
    # colliding file is seen first — the exact stem must still win the key.
    (tmp_path / "AAA_first.pretty").mkdir()
    (tmp_path / "AAA_first.pretty" / "SOT-23-5_HandSoldering.kicad_mod").write_text(_SOT235)
    (tmp_path / "ZZZ_last.pretty").mkdir()
    (tmp_path / "ZZZ_last.pretty" / "SOT-23.kicad_mod").write_text(_SOT23)

    idx = KiCadLibraryIndex(tmp_path)
    fp = idx.get_footprint("SOT-23")
    assert fp is not None and len(fp.pin_offsets) == 3
    # And with the pin-count constraint a 3-pin part resolves (pre-fix it hit
    # the 5-pad file and was rejected by the degenerate-alias guard).
    fp3 = idx.get_footprint("SOT-23", pin_count=3)
    assert fp3 is not None and len(fp3.pin_offsets) == 3
    # The full name still resolves to the 5-pad footprint.
    fp5 = idx.get_footprint("SOT-23-5_HandSoldering", pin_count=5)
    assert fp5 is not None and len(fp5.pin_offsets) == 5


def test_generated_alias_still_fills_empty_slot(tmp_path):
    # With no exact SOT-23 file, the generated alias keeps working.
    (tmp_path / "SOT-23-5_HandSoldering.kicad_mod").write_text(_SOT235)
    idx = KiCadLibraryIndex(tmp_path)
    fp = idx.get_footprint("SOT-23", pin_count=5)
    assert fp is not None and len(fp.pin_offsets) == 5
