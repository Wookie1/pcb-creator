"""B2 regression: footprint lookup must not reject a valid footprint just
because some pads are unconnected.

get_footprint compared the footprint's full pad count to pin_count (the number
of *connected* pins from the netlist) with strict `!=`, so any part with NC pins
(a TO-220 with one unused pin, a 6-pin Molex with 2 wired, an FH35 with 28 of 30)
failed to resolve even with the correct footprint registered. A footprint with
*at least* the connected pin count must resolve; only fewer pads than needed is
a real mismatch.
"""

from exporters.kicad_mod_parser import KiCadLibraryIndex

_TO220_3 = """(footprint "TO-220-3_Vertical" (layer "F.Cu")
  (pad "1" thru_hole rect (at 0 0) (size 1.7 1.7) (drill 1.0))
  (pad "2" thru_hole oval (at 2.54 0) (size 1.7 1.7) (drill 1.0))
  (pad "3" thru_hole oval (at 5.08 0) (size 1.7 1.7) (drill 1.0))
)
"""


def _index(tmp_path):
    (tmp_path / "TO-220-3_Vertical.kicad_mod").write_text(_TO220_3)
    return KiCadLibraryIndex(tmp_path)


def test_resolves_when_fewer_pins_connected_than_pads(tmp_path):
    idx = _index(tmp_path)
    # 3-pad footprint, only 2 pins connected (one NC) — must resolve.
    fp = idx.get_footprint("TO-220-3_Vertical", pin_count=2)
    assert fp is not None
    assert len(fp.pin_offsets) == 3


def test_resolves_when_all_pins_connected(tmp_path):
    fp = _index(tmp_path).get_footprint("TO-220-3_Vertical", pin_count=3)
    assert fp is not None and len(fp.pin_offsets) == 3


def test_rejects_when_more_pins_than_pads(tmp_path):
    # Design connects more pins than the footprint has pads → genuine mismatch.
    assert _index(tmp_path).get_footprint("TO-220-3_Vertical", pin_count=4) is None


def test_resolves_with_no_pin_count_constraint(tmp_path):
    assert _index(tmp_path).get_footprint("TO-220-3_Vertical") is not None


# Guard the B2 relaxation against alias-collision over-matches: a short name
# ("SOT-23") must NOT resolve to a larger different footprint ("SOT-23-5") just
# because it has >= the connected pin count. Extra pads are trusted only on an
# exact full-name match (real NC-pin parts), not a degenerate short alias.
_SOT235 = """(footprint "SOT-23-5_HandSoldering" (layer "F.Cu")
  (pad "1" smd roundrect (at 0 0) (size 0.6 0.9))
  (pad "2" smd roundrect (at 0.95 0) (size 0.6 0.9))
  (pad "3" smd roundrect (at 1.9 0) (size 0.6 0.9))
  (pad "4" smd roundrect (at 1.9 1.8) (size 0.6 0.9))
  (pad "5" smd roundrect (at 0 1.8) (size 0.6 0.9))
)
"""


def test_short_alias_does_not_overmatch_larger_footprint(tmp_path):
    (tmp_path / "SOT-23-5_HandSoldering.kicad_mod").write_text(_SOT235)
    idx = KiCadLibraryIndex(tmp_path)
    # "SOT-23" (3-pin part) must NOT silently get the 5-pad SOT-23-5 footprint.
    assert idx.get_footprint("SOT-23", pin_count=3) is None
    # Exact pin count still resolves via the alias (no over-match involved).
    assert idx.get_footprint("SOT-23", pin_count=5) is not None
