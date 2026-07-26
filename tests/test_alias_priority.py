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


# A DIP-8 IC (4 pads/side, 2.54mm pitch → 7.62mm long) versus a socket that
# accepts DIP-8 *through DIP-16* (same 8 numbered pads, but spread over a
# 17.78mm body). Both filenames yield the alias "DIP-8"; the socket used to win,
# and its 2.4x-too-long pad field is what made neopixel_driver unplaceable.
_DIP8 = """(footprint "DIP-8_W7.62mm" (layer "F.Cu")
  (pad "1" thru_hole circle (at 0 0) (size 1.6 1.6) (layers "*.Cu"))
  (pad "2" thru_hole circle (at 0 -2.54) (size 1.6 1.6) (layers "*.Cu"))
  (pad "3" thru_hole circle (at 0 -5.08) (size 1.6 1.6) (layers "*.Cu"))
  (pad "4" thru_hole circle (at 0 -7.62) (size 1.6 1.6) (layers "*.Cu"))
  (pad "5" thru_hole circle (at 7.62 -7.62) (size 1.6 1.6) (layers "*.Cu"))
  (pad "6" thru_hole circle (at 7.62 -5.08) (size 1.6 1.6) (layers "*.Cu"))
  (pad "7" thru_hole circle (at 7.62 -2.54) (size 1.6 1.6) (layers "*.Cu"))
  (pad "8" thru_hole circle (at 7.62 0) (size 1.6 1.6) (layers "*.Cu"))
)
"""
_DIP8_16_SOCKET = """(footprint "DIP-8-16_W7.62mm_Socket" (layer "F.Cu")
  (pad "1" thru_hole circle (at 0 0) (size 1.6 1.6) (layers "*.Cu"))
  (pad "2" thru_hole circle (at 0 -2.54) (size 1.6 1.6) (layers "*.Cu"))
  (pad "3" thru_hole circle (at 0 -15.24) (size 1.6 1.6) (layers "*.Cu"))
  (pad "4" thru_hole circle (at 0 -17.78) (size 1.6 1.6) (layers "*.Cu"))
  (pad "5" thru_hole circle (at 7.62 -17.78) (size 1.6 1.6) (layers "*.Cu"))
  (pad "6" thru_hole circle (at 7.62 -15.24) (size 1.6 1.6) (layers "*.Cu"))
  (pad "7" thru_hole circle (at 7.62 -2.54) (size 1.6 1.6) (layers "*.Cu"))
  (pad "8" thru_hole circle (at 7.62 0) (size 1.6 1.6) (layers "*.Cu"))
)
"""


def test_underscore_variant_beats_hyphen_continuation(tmp_path):
    # "AAA_" sorts first so the socket is seen first; the "_"-separated
    # dimension variant must still win the bare "DIP-8" key.
    (tmp_path / "AAA_first.pretty").mkdir()
    (tmp_path / "AAA_first.pretty" / "DIP-8-16_W7.62mm_Socket.kicad_mod").write_text(
        _DIP8_16_SOCKET)
    (tmp_path / "ZZZ_last.pretty").mkdir()
    (tmp_path / "ZZZ_last.pretty" / "DIP-8_W7.62mm.kicad_mod").write_text(_DIP8)

    fp = KiCadLibraryIndex(tmp_path).get_footprint("DIP-8", pin_count=8)
    assert fp is not None
    ys = [o[1] for o in fp.pin_offsets.values()]
    assert max(ys) - min(ys) == 7.62, "resolved the DIP-8..16 socket, not a DIP-8"
    # The socket is still reachable by its full name.
    sock = KiCadLibraryIndex(tmp_path).get_footprint(
        "DIP-8-16_W7.62mm_Socket", pin_count=8)
    sys_ = [o[1] for o in sock.pin_offsets.values()]
    assert max(sys_) - min(sys_) == 17.78


def test_plainest_variant_wins_the_bare_name(tmp_path):
    # A bare package name asks for the ordinary part, not a socket variant.
    (tmp_path / "DIP-8_W8.89mm_SMDSocket_LongPads.kicad_mod").write_text(_DIP8)
    (tmp_path / "DIP-8_W7.62mm.kicad_mod").write_text(_DIP8)
    idx = KiCadLibraryIndex(tmp_path)
    assert idx._ensure_index()["DIP-8"].stem == "DIP-8_W7.62mm"
