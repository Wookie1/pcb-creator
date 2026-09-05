"""Pad-row escape detection (audit finding F2 regression).

The audit L298N board routed IN2 straight through the two pad rows of U1
(Multiwatt-15), clipping three pads by 0.495mm; on re-route the same class of
threading persisted and a dangling-stub net was strung across the package.
These tests pin the structural rule: a trace whose both endpoints float free
of the package and still crosses its inter-row channel is impossible copper,
not routing.
"""

import os
import sys
from types import SimpleNamespace

import pytest

_VAL = os.path.join(os.path.dirname(__file__), "..", "validators")
sys.path.insert(0, _VAL)
_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, _ROOT)

from validate_routing import (  # noqa: E402
    _check_pad_row_escape,
    _find_pad_row_channels,
    validate_routing,
)

_AUDIT = os.path.expanduser(
    "~/.pcb-creator/projects/audit_l298n/audit_l298n_routed.json")
_AUDIT_NL = os.path.expanduser(
    "~/.pcb-creator/projects/audit_l298n/audit_l298n_netlist.json")


def _pad(x, y, ref="U1", pin="1", size=1.5, layer="all", net=None):
    return SimpleNamespace(designator=ref, pin_number=pin, x_mm=x, y_mm=y,
                           layer=layer, pad_width_mm=size, pad_height_mm=size,
                           net_id=net)


def _multiwatt_pad_map():
    """Multiwatt-15 geometry, as in the audit board: odd pins along y=30.99
    (x 9.27..33.07 step 3.4), even pins along y=33.69 (x 10.97..31.37)."""
    pads = {}
    for i in range(8):
        p = _pad(9.27 + 3.4 * i, 30.99, pin=str(2 * i + 1))
        pads[f"U1.{p.pin_number}"] = p
    for i in range(7):
        p = _pad(10.97 + 3.4 * i, 33.69, pin=str(2 * i + 2))
        pads[f"U1.{p.pin_number}"] = p
    return pads


def _routed(*traces):
    return {"routing": {"config": {"trace_clearance_mm": 0.2},
                        "traces": list(traces)}}


def _trace(net, layer, x0, y0, x1, y1, w=0.25):
    return {"net_id": f"net_{net.lower()}", "net_name": net, "layer": layer,
            "start_x_mm": x0, "start_y_mm": y0,
            "end_x_mm": x1, "end_y_mm": y1, "width_mm": w}


# ---------------------------------------------------------------------------
# Channel detection
# ---------------------------------------------------------------------------

def test_two_row_channel_detected_with_overlap_geometry():
    chans = _find_pad_row_channels(_multiwatt_pad_map())
    assert len(chans) == 1
    assert chans[0]["ref"] == "U1"
    rect = chans[0]["rect"]
    # y band between the two row centrelines; x limited to where the rows
    # face each other (the overlap), not the staggered outer tips.
    assert rect[1] == pytest.approx(30.99)
    assert rect[3] == pytest.approx(33.69)
    assert rect[0] == pytest.approx(10.97)
    assert rect[2] == pytest.approx(31.37)


def test_single_row_header_has_no_channel():
    pads = {f"J1.{i}": _pad(10 + 2.54 * i, 30.0, ref="J1", pin=str(i),
                            size=1.7)
            for i in range(8)}
    assert _find_pad_row_channels(pads) == []


def test_two_row_narrow_gap_not_treated_as_routable_channel():
    # 2.54mm-pitch dual-row header: inner gap 0.84mm — too tight to thread,
    # and physically a legal place to run wires. Must not form a channel.
    pads = {f"CN1.{i}": _pad(20.0, 10 + 2.54 * (i % 3), ref="CN1",
                              pin=str(i), size=1.7)
            for i in range(3)}
    pads.update({f"CN1.p{i}": _pad(22.54, 10 + 2.54 * (i % 3), ref="CN1",
                                    pin=f"p{i}", size=1.7)
                 for i in range(3)})
    assert _find_pad_row_channels(pads) == []


def test_rotated_part_channel_found_on_x_axis():
    pads = {f"U9.{i}": _pad(20.0, 10 + 2.54 * i, ref="U9", pin=str(i),
                            size=1.7)
            for i in range(4)}
    pads.update({f"U9.h{i}": _pad(23.0, 10 + 2.54 * i, ref="U9",
                                   pin=f"h{i}", size=1.7)
                 for i in range(4)})
    chans = _find_pad_row_channels(pads)
    assert len(chans) == 1
    x0, y0, x1, y1 = chans[0]["rect"]
    assert (x0, x1) == (pytest.approx(20.0), pytest.approx(23.0))


# ---------------------------------------------------------------------------
# Escape vs threading
# ---------------------------------------------------------------------------

@pytest.fixture
def mw(monkeypatch):
    """Route the checker's pad-map build straight to the Multiwatt geometry."""
    import optimizers.pad_geometry as pg
    monkeypatch.setattr(pg, "build_pad_map",
                        lambda *_a, **_k: _multiwatt_pad_map())


def test_threading_trace_along_channel_is_flagged(mw):
    # The audit's IN2 bottom escape, lifted from audit_l298n verbatim.
    res = _check_pad_row_escape(
        _routed(_trace("IN2", "bottom", 1.75, 33.31, 18.813, 33.31)),
        {"elements": []})
    assert len(res[0]) == 1
    assert "Pad-row escape on bottom" in res[0][0]
    assert "IN2" in res[0][0] and "U1" in res[0][0]


def test_escape_past_the_row_end_is_clean(mw):
    # Audit regression: the OUT4 run past x=32.5 leaves the package past the
    # end of the top row — a legal escape, must NOT be flagged.
    res = _check_pad_row_escape(
        _routed(_trace("OUT4", "top", 32.542, 33.61, 36.57, 33.61)),
        {"elements": []})
    assert res[0] == []


def test_threading_trace_across_channel_is_flagged(mw):
    res = _check_pad_row_escape(
        _routed(_trace("SIGX", "top", 16.07, 34.5, 16.07, 29.0)),
        {"elements": []})
    assert len(res[0]) == 1


def test_pin_to_pin_link_through_channel_is_clean(mw):
    # Both endpoints sit on pads -> a connection through the package, not an
    # escape. (Endpoint test, not clearance: here it happens to clear pads.)
    res = _check_pad_row_escape(
        _routed(_trace("LINK", "bottom", 10.97, 33.69, 9.27, 30.99)),
        {"elements": []})
    assert res[0] == []


def test_inner_layer_threading_is_ignored(mw):
    # Under-package routing on internal copper is legal on 4-layer boards.
    res = _check_pad_row_escape(
        _routed(_trace("PWR", "inner1", 12.0, 32.34, 28.0, 32.34)),
        {"elements": []})
    assert res[0] == []


def test_multi_flag_dedupes_per_net_and_layer(mw):
    # Two separate IN2 threading runs on the bottom layer -> one message; the
    # same net on top is a second message.
    res = _check_pad_row_escape(
        _routed(_trace("IN2", "bottom", 12.0, 32.34, 18.0, 32.34),
                _trace("IN2", "bottom", 20.0, 32.34, 26.0, 32.34),
                _trace("IN2", "top", 12.0, 32.34, 18.0, 32.34)),
        {"elements": []})
    assert len(res[0]) == 2


def test_single_row_header_copper_untouched(monkeypatch):
    import optimizers.pad_geometry as pg
    pads = {f"J1.{i}": _pad(10 + 2.54 * i, 30.0, ref="J1", pin=str(i),
                            size=1.7)
            for i in range(8)}
    monkeypatch.setattr(pg, "build_pad_map", lambda *_a, **_k: pads)
    routed = {"routing": {"config": {}, "traces": [
        _trace("S", "top", 9.0, 30.0, 27.0, 30.0)]}}
    assert _check_pad_row_escape(routed, {"elements": []}) == ([], [])


def test_no_netlist_skips_quietly():
    res = _check_pad_row_escape(_routed(_trace("X", "top", 0, 0, 1, 1)), None)
    assert res == ([], [])


def test_broken_pad_map_warns(monkeypatch):
    import optimizers.pad_geometry as pg

    def _boom(*_a, **_k):
        raise RuntimeError("corrupt footprint data")

    monkeypatch.setattr(pg, "build_pad_map", _boom)
    res = _check_pad_row_escape(
        _routed(_trace("IN2", "bottom", 1.75, 33.31, 18.813, 33.31)),
        {"elements": []})
    assert res[0] == []
    assert res[1] == ["Pad-row escape check: could not build pad map"]


# ---------------------------------------------------------------------------
# End-to-end on the audit fixture
# ---------------------------------------------------------------------------

@pytest.mark.skipif(not os.path.exists(_AUDIT),
                    reason="audit_l298n fixture not present")
def test_audit_l298n_flags_in2_without_flagging_the_out4_escape():
    result = validate_routing(_AUDIT, _AUDIT_NL)
    assert not result["valid"]
    pad_row = [e for e in result["errors"] if "Pad-row escape" in e]
    assert any("IN2" in e and "U1" in e for e in pad_row)
    assert not any("OUT4" in e for e in pad_row)