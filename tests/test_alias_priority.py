"""Alias-collision root cause: index build order must be deterministic.

_build_index used to be single-pass "first rglob file wins", so the "SOT-23"
alias generated from SOT-23-5_HandSoldering could shadow the real
SOT-23.kicad_mod depending on scan order. Exact filename stems are now
registered before generated short aliases, making resolution order-independent.
"""

from exporters.kicad_mod_parser import KiCadLibraryIndex, _alias_tiers

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


_HDR = """(footprint "H" (layer "F.Cu")
  (pad "1" thru_hole circle (at 0 0) (size 1.7 1.7) (layers "*.Cu"))
  (pad "2" thru_hole circle (at 0 -{p}) (size 1.7 1.7) (layers "*.Cu"))
  (pad "3" thru_hole circle (at 0 -{p2}) (size 1.7 1.7) (layers "*.Cu"))
)
"""
_SOIC = """(footprint "S" (layer "F.Cu")
  (pad "1" smd rect (at 0 0) (size 1.95 0.6) (layers "F.Cu"))
  (pad "2" smd rect (at 0 -1.27) (size 1.95 0.6) (layers "F.Cu"))
)
"""


def test_pitch_ambiguous_alias_is_dropped(tmp_path):
    """Variants differing in PITCH are different parts, not variants.

    PinHeader_1x15 at 1.27mm instead of 2.54mm puts 15 pins in half the space
    with half-size pads — silently the wrong component. Better to leave the bare
    name unresolved and let the IPC-7351 / built-in tiers supply their default.
    """
    for pitch in ("1.00", "1.27", "2.54"):
        (tmp_path / f"PinHeader_1x3_P{pitch}mm_Vertical.kicad_mod").write_text(
            _HDR.format(p=pitch, p2=float(pitch) * 2))
    idx = KiCadLibraryIndex(tmp_path)
    assert idx.get_footprint("PinHeader_1x3") is None
    # The specific variants remain reachable by their full names.
    assert idx.get_footprint("PinHeader_1x3_P2.54mm_Vertical") is not None


def test_same_pitch_variants_resolve_deterministically(tmp_path):
    """Same pitch, different body size — any is a real part; pick one stably."""
    for body in ("3.9x4.9", "5.3x6.2", "5.3x5.3"):
        (tmp_path / f"SOIC-8_{body}mm_P1.27mm.kicad_mod").write_text(_SOIC)
    picked = {KiCadLibraryIndex(tmp_path)._ensure_index()["SOIC-8"].stem
              for _ in range(3)}
    assert picked == {"SOIC-8_3.9x4.9mm_P1.27mm"}, (
        "must not depend on directory order")


def test_continuation_tier_cannot_rescue_an_ambiguous_alias(tmp_path):
    """A "-N" file must not claim a name the "_" variants could not agree on.

    This is how SOIC-8 briefly resolved to an exposed-pad part: the plain
    variants were rejected, leaving the odd one as the only claimant.
    """
    # Two equally-plain "_" variants disagreeing on pitch -> ambiguous.
    for pitch in ("1.00", "2.54"):
        (tmp_path / f"PinHeader_1x3_P{pitch}mm_Vertical.kicad_mod").write_text(
            _HDR.format(p=pitch, p2=float(pitch) * 2))
    # A "-N" continuation file, strictly less plain, claiming only at tier 1.
    (tmp_path / "PinHeader_1x3-4_P0.50mm_VerticalOddball.kicad_mod").write_text(
        _HDR.format(p="0.50", p2=1.0))

    idx = KiCadLibraryIndex(tmp_path)
    dim_alias, cont_alias = _alias_tiers(
        "PinHeader_1x3-4_P0.50mm_VerticalOddball")
    assert cont_alias == "PINHEADER_1X3" and dim_alias != "PINHEADER_1X3", (
        "fixture must claim PinHeader_1x3 only at the continuation tier")
    assert idx.get_footprint("PinHeader_1x3") is None
