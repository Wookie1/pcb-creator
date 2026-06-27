"""Coverage + geometry tests for optimizers/ipc7351.py (IPC-7351B generator)."""

import math

import pytest

from optimizers.ipc7351 import (
    ipc7351_lookup,
    make_bga,
    make_dfn,
    make_do214,
    make_dpak,
    make_esp12,
    make_mc306,
    make_mounting_hole,
    make_multiwatt,
    make_qfn,
    make_qfp,
    make_radial_electrolytic,
    make_screw_terminal,
    make_smd_2pad_body,
    make_sod,
    make_sop,
    make_sot89,
    make_sot223,
    make_trimmer_3mm,
)


def _pads_positive(fp):
    assert fp.pad_size[0] > 0 and fp.pad_size[1] > 0


# --------------------------------------------------------------------------- QFN
def test_qfn_basic_counts_and_symmetry():
    # 16 signal + 1 EP = 17 pads, 4 pins per side
    fp = make_qfn(17, body_mm=3.0, pitch_mm=0.5, exposed_pad=True)
    assert len(fp.pin_offsets) == 17
    assert fp.pin_offsets[17] == (0.0, 0.0)  # EP at centre
    _pads_positive(fp)
    # Pad centre reflects body size: half_body - pad_len/2 + 0.25
    expected = 3.0 / 2 - 0.8 / 2 + 0.25
    assert fp.pin_offsets[1][0] == pytest.approx(-expected)


def test_qfn_no_ep():
    fp = make_qfn(16, exposed_pad=False)
    assert len(fp.pin_offsets) == 16


def test_qfn_invalid_signal_count():
    # 17 signal pins (no EP) is not divisible by 4
    assert make_qfn(17, exposed_pad=False) is None
    # too few
    assert make_qfn(4, exposed_pad=True) is None  # 3 signal pins


# --------------------------------------------------------------------------- DFN
def test_dfn_basic():
    fp = make_dfn(6, body_mm=3.0, pitch_mm=0.5)
    assert len(fp.pin_offsets) == 6
    # left pads negative x, right pads positive x
    assert fp.pin_offsets[1][0] < 0
    assert fp.pin_offsets[4][0] > 0
    _pads_positive(fp)


def test_dfn_with_ep():
    fp = make_dfn(7, exposed_pad=True)
    assert len(fp.pin_offsets) == 7
    assert fp.pin_offsets[7] == (0.0, 0.0)


def test_dfn_invalid():
    assert make_dfn(3) is None  # odd signal count


# --------------------------------------------------------------------------- SOP
def test_sop_families_and_geometry():
    fp = make_sop(8, family="SOIC")
    assert len(fp.pin_offsets) == 8
    # DIP-style: pin 1 left, pin 8 right
    assert fp.pin_offsets[1][0] < 0
    assert fp.pin_offsets[8][0] > 0
    _pads_positive(fp)


def test_sop_body_width_override():
    fp = make_sop(8, family="SOIC", body_width_mm=3.9)
    # half_row = (3.9 + pad_height 1.5) / 2 = 2.7
    assert fp.pin_offsets[1][0] == pytest.approx(-2.7)


def test_sop_custom_pitch():
    fp = make_sop(4, family="TSSOP", pitch_mm=0.4)
    span = (2 - 1) * 0.4
    assert fp.pin_offsets[1][1] == pytest.approx(-span / 2)


def test_sop_invalid():
    assert make_sop(3) is None  # odd
    assert make_sop(8, family="NOPE") is None  # unknown family


# ------------------------------------------------------------------- SOT-223/89
def test_sot223():
    fp = make_sot223()
    assert len(fp.pin_offsets) == 4
    assert fp.pin_offsets[4] == (0.0, 3.15)  # tab
    _pads_positive(fp)


def test_sot89():
    fp = make_sot89()
    assert len(fp.pin_offsets) == 3
    _pads_positive(fp)


# --------------------------------------------------------------------------- BGA
def test_bga_grid():
    fp = make_bga(rows=8, cols=8, pitch_mm=0.8)
    assert len(fp.pin_offsets) == 64
    # symmetric grid centred on origin
    xs = [p[0] for p in fp.pin_offsets.values()]
    assert min(xs) == pytest.approx(-max(xs))
    assert fp.pad_size == (pytest.approx(0.4), pytest.approx(0.4))


def test_bga_invalid():
    assert make_bga(0, 4) is None
    assert make_bga(4, 0) is None


# --------------------------------------------------------------------------- QFP
def test_qfp_four_sides():
    fp = make_qfp(32, body_mm=7.0, pitch_mm=0.8)
    assert len(fp.pin_offsets) == 32
    assert len(fp.pin_offsets) % 4 == 0
    _pads_positive(fp)


def test_qfp_defaults_lookup():
    fp = make_qfp(100)  # uses _QFP_DEFAULTS
    assert len(fp.pin_offsets) == 100


def test_qfp_derived_body():
    # pin count not in defaults -> derive body from pitch
    fp = make_qfp(12)
    assert len(fp.pin_offsets) == 12


def test_qfp_invalid():
    assert make_qfp(6) is None  # < 8
    assert make_qfp(10) is None  # not divisible by 4


# --------------------------------------------------------------------------- SOD
def test_sod():
    fp = make_sod("123")
    assert len(fp.pin_offsets) == 2
    assert fp.pin_offsets[1][0] == pytest.approx(-1.7)
    assert make_sod("999") is None


# ------------------------------------------------------------------------- DO214
def test_do214():
    fp = make_do214("smb")
    assert len(fp.pin_offsets) == 2
    assert make_do214("nope") is None


# --------------------------------------------------------------------------- ESP
def test_esp12_22():
    fp = make_esp12(22)
    assert len(fp.pin_offsets) == 22


def test_esp12_16():
    fp = make_esp12(16)
    assert len(fp.pin_offsets) == 16


# ------------------------------------------------------------------------- MC306
def test_mc306():
    fp = make_mc306()
    assert len(fp.pin_offsets) == 2


# ------------------------------------------------------------------- DPAK/D2PAK
def test_dpak_leads():
    fp = make_dpak(3, d2=False)
    assert len(fp.pin_offsets) == 4  # 3 leads + tab
    assert fp.pin_offsets[4][1] < 0  # tab below leads


def test_dpak_many_leads():
    fp = make_dpak(5, d2=False)
    assert len(fp.pin_offsets) == 6


def test_d2pak_leads():
    fp = make_dpak(3, d2=True)
    assert len(fp.pin_offsets) == 4


def test_d2pak_many_leads():
    fp = make_dpak(7, d2=True)
    assert len(fp.pin_offsets) == 8


def test_dpak_invalid():
    assert make_dpak(1) is None
    assert make_dpak(10) is None


# -------------------------------------------------------------- screw terminals
def test_screw_terminal():
    fp = make_screw_terminal(3, pitch_mm=5.0)
    assert len(fp.pin_offsets) == 3
    assert fp.pin_offsets[1][0] == pytest.approx(-5.0)
    assert make_screw_terminal(0) is None
    assert make_screw_terminal(25) is None


# --------------------------------------------------------------------- multiwatt
def test_multiwatt_staggered():
    fp = make_multiwatt(15)
    assert len(fp.pin_offsets) == 15
    # odd index (pin 2, i=1) staggered back
    assert fp.pin_offsets[2][1] == pytest.approx(2.7)
    assert fp.pin_offsets[1][1] == pytest.approx(0.0)


def test_multiwatt_invalid():
    assert make_multiwatt(8) is None
    assert make_multiwatt(26) is None


# ----------------------------------------------------------- generic 2-pad SMD
def test_smd_2pad_body():
    fp = make_smd_2pad_body(12.0, 12.0)
    assert len(fp.pin_offsets) == 2
    assert fp.pin_offsets[1][0] < 0 < fp.pin_offsets[2][0]


def test_smd_2pad_body_invalid():
    assert make_smd_2pad_body(0.5, 5) is None
    assert make_smd_2pad_body(70, 5) is None
    assert make_smd_2pad_body(5, 0.5) is None
    assert make_smd_2pad_body(5, 70) is None


# ------------------------------------------------------- radial electrolytic
@pytest.mark.parametrize("dia,spacing", [(4, 2.0), (6, 2.5), (7, 3.5), (12, 5.0)])
def test_radial_electrolytic(dia, spacing):
    fp = make_radial_electrolytic(dia)
    assert fp.is_through_hole is True
    assert fp.pin_offsets[2][0] == pytest.approx(spacing / 2)


def test_radial_electrolytic_invalid():
    assert make_radial_electrolytic(2) is None
    assert make_radial_electrolytic(50) is None


# -------------------------------------------------------------- mounting hole
def test_mounting_hole():
    fp = make_mounting_hole(3.2)
    assert fp.pad_size == (pytest.approx(6.4), pytest.approx(6.4))


# ------------------------------------------------------------------- trimmer
def test_trimmer():
    fp = make_trimmer_3mm()
    assert len(fp.pin_offsets) == 3


# =========================================================================
# Dispatch: ipc7351_lookup — one representative per family
# =========================================================================
@pytest.mark.parametrize(
    "pkg,pins,expected_count",
    [
        ("QFN-16", 0, 17),          # 16 signal + EP
        ("QFN-16-EP", 0, 17),
        ("QFN-32_5x5mm", 0, 33),
        ("DFN-8", 0, 8),
        ("DFN-8", 10, 10),          # caller pin_count wins
        ("TSSOP-20", 0, 20),
        ("SSOP-16", 0, 16),
        ("MSOP-8", 0, 8),
        ("SOP-8", 0, 8),
        ("SOT-223", 0, 4),
        ("SOT-89", 0, 3),
        ("TQFP-32", 0, 32),
        ("LQFP-48", 0, 48),
        ("QFP-100", 0, 100),
        ("QFP-100", 64, 64),        # caller pin_count wins
        ("SOD-123", 0, 2),
        ("SMA", 0, 2),
        ("DO-214AC", 0, 2),
        ("DO-214AA", 0, 2),
        ("DO-214AB", 0, 2),
        ("ESP-12F", 0, 22),
        ("ESP-12", 16, 16),
        ("ESP-07", 0, 16),
        ("MC-306", 0, 2),
        ("TO-263-3", 0, 4),
        ("D2PAK", 0, 4),            # default 3 leads + tab
        ("TO-252-3", 0, 4),
        ("DPAK", 0, 4),
        ("ScrewTerminal_1x2_5mm", 0, 2),
        ("ScrewTerminal_1x3", 0, 3),
        ("Multiwatt-15", 0, 15),
        ("trimmer-3mm", 0, 3),
        ("MountingHole_3.2mm_M3", 0, 1),
        ("electrolytic_10x12", 0, 2),
        ("CP_Radial_D10.0mm", 0, 2),
        ("12x12mm", 0, 2),
        ("BGA-64", 0, 64),
        ("BGA-4x8", 0, 32),
    ],
)
def test_lookup_families(pkg, pins, expected_count):
    fp = ipc7351_lookup(pkg, pins)
    assert fp is not None, pkg
    assert len(fp.pin_offsets) == expected_count, pkg
    _pads_positive(fp)


def test_lookup_body_size_extraction():
    """Body size parsed from the package string drives pad coordinates."""
    small = ipc7351_lookup("QFN-16_3x3mm")
    big = ipc7351_lookup("QFN-16_5x5mm")
    assert abs(big.pin_offsets[1][0]) > abs(small.pin_offsets[1][0])


def test_lookup_pitch_extraction():
    fp = ipc7351_lookup("QFN-16_3x3mm_P0.5mm")
    assert fp is not None
    assert len(fp.pin_offsets) == 17


def test_lookup_smd_body_rejects_high_pincount():
    assert ipc7351_lookup("12x12mm", pin_count=5) is None


def test_lookup_bga_square_from_pincount():
    fp = ipc7351_lookup("BGA-100", pin_count=100)
    side = math.ceil(math.sqrt(100))
    assert len(fp.pin_offsets) == side * side


def test_lookup_no_match():
    assert ipc7351_lookup("WIDGET-9000") is None
    assert ipc7351_lookup("") is None
