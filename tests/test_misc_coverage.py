"""Targeted line-coverage tests for the residual gaps in dxf_parser,
kicad_exporter, kicad_drc, and escape_router. Only the lines NOT already
exercised by the existing suite are touched here."""

from __future__ import annotations

import ezdxf
import pytest

from exporters import dxf_parser
from exporters import kicad_exporter as kx
from optimizers import escape_router as er
from optimizers.pad_geometry import PadInfo
from validators import kicad_drc


# ---------------------------------------------------------------------------
# dxf_parser — drew shapes, assert geometry round-trips
# ---------------------------------------------------------------------------

def _doc():
    doc = ezdxf.new()
    return doc, doc.modelspace()


def test_dxf_lwpolyline_rectangle(tmp_path):
    doc, msp = _doc()
    # 30 x 20 rectangle, offset so normalization is exercised
    msp.add_lwpolyline(
        [(10, 5), (40, 5), (40, 25), (10, 25)], close=True)
    p = tmp_path / "rect.dxf"
    doc.saveas(p)

    verts, w, h = dxf_parser.parse_board_outline(p)
    assert w == 30.0 and h == 20.0
    # normalized to origin, same winding
    assert verts == [[0.0, 0.0], [30.0, 0.0], [30.0, 20.0], [0.0, 20.0]]


def test_dxf_old_polyline(tmp_path):
    doc, msp = _doc()
    pl = msp.add_polyline2d([(0, 0), (10, 0), (10, 8), (0, 8)], close=True)
    assert pl  # entity created
    p = tmp_path / "poly.dxf"
    doc.saveas(p)

    verts, w, h = dxf_parser.parse_board_outline(p)
    assert w == 10.0 and h == 8.0
    assert [0.0, 0.0] in verts and [10.0, 8.0] in verts


def test_dxf_line_chain_closed(tmp_path):
    doc, msp = _doc()
    # four LINEs forming a closed 12 x 6 box, deliberately not all head-to-tail
    # so the start/end-match branch of _chain_lines is hit.
    msp.add_line((0, 0), (12, 0))
    msp.add_line((12, 6), (12, 0))   # reversed: matches on end
    msp.add_line((12, 6), (0, 6))
    msp.add_line((0, 6), (0, 0))
    p = tmp_path / "lines.dxf"
    doc.saveas(p)

    verts, w, h = dxf_parser.parse_board_outline(p)
    assert w == 12.0 and h == 6.0
    assert len(verts) == 4


def test_dxf_largest_polygon_wins(tmp_path):
    doc, msp = _doc()
    msp.add_lwpolyline([(0, 0), (5, 0), (5, 5)], close=True)          # small
    msp.add_lwpolyline([(0, 0), (50, 0), (50, 40), (0, 40)], close=True)  # big
    p = tmp_path / "two.dxf"
    doc.saveas(p)

    _verts, w, h = dxf_parser.parse_board_outline(p)
    assert (w, h) == (50.0, 40.0)


def test_dxf_no_outline_raises(tmp_path):
    doc, _msp = _doc()  # empty model space
    p = tmp_path / "empty.dxf"
    doc.saveas(p)
    with pytest.raises(ValueError, match="No valid board outline"):
        dxf_parser.parse_board_outline(p)


def test_dxf_open_line_chain_is_not_a_candidate(tmp_path):
    doc, msp = _doc()
    # An open chain (gap) yields no closed polygon -> no candidates -> ValueError
    msp.add_line((0, 0), (10, 0))
    msp.add_line((10, 0), (10, 5))
    # missing the two return edges -> never closes
    p = tmp_path / "open.dxf"
    doc.saveas(p)
    with pytest.raises(ValueError):
        dxf_parser.parse_board_outline(p)


# ---------------------------------------------------------------------------
# kicad_exporter — hit the residual branches
# ---------------------------------------------------------------------------

def test_header_four_layer_inner():
    """143: 4-layer header declares In1/In2 signal layers."""
    h = kx._header(4)
    assert '(1 "In1.Cu" signal)' in h
    assert '(2 "In2.Cu" signal)' in h
    assert '(1 "In1.Cu" signal)' not in kx._header(2)


def test_footprint_fallback_when_unknown_package():
    """285: unknown package -> _generate_fallback_footprint path."""
    plc = {"designator": "U9", "package": "TOTALLY_UNKNOWN_PKG",
           "x_mm": 5.0, "y_mm": 5.0, "footprint_width_mm": 3.0,
           "footprint_height_mm": 2.0}
    ports = [{"pin_number": 1, "port_id": "p1"},
             {"pin_number": 2, "port_id": "p2"}]
    out = kx._footprint(plc, {}, {"C1": ports}, {"C1": {"designator": "U9"}})
    # fallback still produces SMD pads for the ports we declared
    assert 'pcb-creator:U9_TOTALLY_UNKNOWN_PKG' in out
    assert "smd rect" in out


def test_footprint_bottom_layer_mirrors_x():
    """363: B.Cu footprint negates pad dx."""
    plc = {"designator": "R1", "package": "0805", "x_mm": 10, "y_mm": 10,
           "layer": "bottom"}
    ports = [{"pin_number": 1, "port_id": "p1"},
             {"pin_number": 2, "port_id": "p2"}]
    out = kx._footprint(plc, {}, {"C1": ports}, {"C1": {"designator": "R1"}})
    assert '(layer "B.Cu")' in out
    # one pad sits at a negative x offset (mirrored), one positive
    assert "(at -" in out


def test_footprint_fiducial_pad():
    """397-399: fiducial -> smd circle with solder_mask_margin."""
    plc = {"designator": "FID1", "package": "Fiducial_1mm",
           "x_mm": 2, "y_mm": 2, "component_type": "fiducial"}
    ports = [{"pin_number": 1, "port_id": "p1"}]
    out = kx._footprint(plc, {}, {"C1": ports}, {"C1": {"designator": "FID1"}})
    assert "smd circle" in out
    assert "solder_mask_margin 1.0" in out


def test_copper_fills_dedupes_layer_net():
    """492: duplicate (layer, net) fill is skipped (one zone only)."""
    fills = [
        {"layer": "top", "net_id": "n1", "net_name": "GND"},
        {"layer": "top", "net_id": "n1", "net_name": "GND"},  # dup -> skipped
        {"layer": "bottom", "net_id": "n1", "net_name": "GND"},
    ]
    out = kx._copper_fills(fills, {"n1": 1}, {"width_mm": 50, "height_mm": 50})
    assert out.count("(zone ") == 2  # not 3


def test_silkscreen_dot():
    """532-535: silkscreen 'dot' -> filled gr_circle."""
    out = kx._silkscreen([{"type": "dot", "layer": "top_silk",
                           "x_mm": 3, "y_mm": 4, "diameter_mm": 0.6}])
    assert "gr_circle" in out
    assert "(center 3 4)" in out
    assert "(end 3.3 4)" in out  # x + r, r = 0.3


def test_fill_zones_pcbnew_no_python_returns_false():
    """670-671: every candidate Python raises/lacks pcbnew -> False."""
    import os
    old = os.environ.get("PCB_KICAD_PYTHON")
    os.environ["PCB_KICAD_PYTHON"] = "/nonexistent/python-binary-xyz"
    try:
        # /usr/bin/python3 + python3 won't have pcbnew either in CI/venv
        assert kx.fill_zones_pcbnew("/tmp/does-not-matter.kicad_pcb") is False
    finally:
        if old is None:
            os.environ.pop("PCB_KICAD_PYTHON", None)
        else:
            os.environ["PCB_KICAD_PYTHON"] = old


def test_export_kicad_pcb_swallows_fill_exception(tmp_path, monkeypatch):
    """785-786: a fill_zones_pcbnew exception is swallowed; export still writes."""
    def boom(_p):
        raise RuntimeError("pcbnew exploded")
    monkeypatch.setattr(kx, "fill_zones_pcbnew", boom)

    routed = {
        "board": {"width_mm": 20, "height_mm": 20, "layers": 2},
        "placements": [], "routing": {},
    }
    netlist = {"elements": [
        {"element_type": "net", "net_id": "n1", "name": "GND",
         "connected_port_ids": []},
    ]}
    out = kx.export_kicad_pcb(routed, netlist, tmp_path / "b.kicad_pcb")
    assert out.exists()
    assert "kicad_pcb" in out.read_text()


# ---------------------------------------------------------------------------
# kicad_drc — 142-144: export/DRC failure -> None
# ---------------------------------------------------------------------------

def test_run_kicad_drc_returns_none_on_export_failure(tmp_path):
    def export_fn(_routed, _netlist, _pcb):
        raise OSError("cannot export")
    res = kicad_drc.run_kicad_drc(
        {}, {}, "kicad-cli-not-real",
        export_fn=export_fn, project_name="x")
    assert res is None


def test_run_kicad_drc_returns_none_on_bad_json(tmp_path):
    # export writes nothing -> out.read_text() raises FileNotFoundError -> None
    def export_fn(_routed, _netlist, _pcb):
        return None
    res = kicad_drc.run_kicad_drc(
        {}, {}, "definitely-not-a-real-binary-zzz",
        export_fn=export_fn)
    assert res is None


# ---------------------------------------------------------------------------
# escape_router — 82, 184, 186, 261
# ---------------------------------------------------------------------------

def test_auto_drop_layer_fallback_to_signal0():
    """82: no preferred name matches -> returns signal[0].

    Force the order map to contain only an unconventional signal-layer name so
    the ('inner2','inner1','bottom','top') preference loop misses entirely."""
    er._LAYER_ORDER[3] = ["top", "weird_mid", "bottom"]
    try:
        # pad on 'top', planes consume 'bottom' -> only 'weird_mid' left, which
        # is not in the preference tuple -> falls through to signal[0].
        got = er._auto_drop_layer("top", 3, 0)
    finally:
        del er._LAYER_ORDER[3]
    assert got in ("bottom", "weird_mid")  # weird_mid is signal[0] here
    # tighten: with no planes, 'bottom' IS preferred, so test the real miss:
    er._LAYER_ORDER[3] = ["top", "weird_mid"]
    try:
        got2 = er._auto_drop_layer("top", 3, 0)
    finally:
        del er._LAYER_ORDER[3]
    assert got2 == "weird_mid"  # signal[0], none of the preferences present


def _pad(des, pin, net, x, y, w=1.3, h=0.3, layer="top"):
    return PadInfo(port_id=f"{des}.{pin}", designator=des, pin_number=pin,
                   net_id=net, x_mm=x, y_mm=y,
                   pad_width_mm=w, pad_height_mm=h, layer=layer)


def _fine_pitch_part(n=12, pitch=0.5, net_prefix="net"):
    """A single-row fine-pitch SMD part where every pin leaves the part."""
    pads = {}
    netlist_elems = [
        {"element_type": "component", "component_id": "CN1", "designator": "CN1"},
        {"element_type": "component", "component_id": "U2", "designator": "U2"},
    ]
    for i in range(n):
        net = f"{net_prefix}{i}"
        pads[f"CN1.{i}"] = _pad("CN1", i, net, x=i * pitch, y=10.0)
        # port on CN1 + a port on U2 so the net "leaves" CN1
        netlist_elems += [
            {"element_type": "port", "port_id": f"cn1p{i}",
             "component_id": "CN1"},
            {"element_type": "port", "port_id": f"u2p{i}", "component_id": "U2"},
            {"element_type": "net", "net_id": net, "name": net,
             "connected_port_ids": [f"cn1p{i}", f"u2p{i}"]},
        ]
    placement = {"board": {"width_mm": 30, "height_mm": 30}}
    netlist = {"elements": netlist_elems}
    return placement, netlist, pads


def test_escape_generates_dogbones():
    """Sanity: the happy path produces stubs + vias + fanouts."""
    placement, netlist, pads = _fine_pitch_part()
    res = er.generate_escape_routing(placement, netlist, pad_map=pads)
    roles = {t.get("escape_role") for t in res["traces"]}
    assert "stub" in roles and "fanout" in roles
    assert len(res["vias"]) >= 1


def _two_row_parts(ax, bx, board_w, pad_w=3.0):
    """Two single-row fine-pitch parts sharing one signal-collector U2. Each
    pin of each part leaves to U2, so both parts try to fan out. The geometry
    (set by the callers) decides whether their escapes collide."""
    pads = {}
    elems = [
        {"element_type": "component", "component_id": "A", "designator": "CN1"},
        {"element_type": "component", "component_id": "B", "designator": "CN2"},
        {"element_type": "component", "component_id": "U", "designator": "U2"},
    ]
    c = [0]

    def addpart(comp, des, x):
        for i in range(12):
            net = f"{des}n{i}"
            pads[f"{des}.{i}"] = _pad(des, i, net, x, 5 + i * 0.5,
                                      w=pad_w, h=0.3)
            a, b = f"p{c[0]}", f"p{c[0] + 1}"
            c[0] += 2
            elems.extend([
                {"element_type": "port", "port_id": a, "component_id": comp},
                {"element_type": "port", "port_id": b, "component_id": "U"},
                {"element_type": "net", "net_id": net, "name": net,
                 "connected_port_ids": [a, b]},
            ])

    addpart("A", "CN1", ax)
    addpart("B", "CN2", bx)
    placement = {"board": {"width_mm": board_w, "height_mm": 40}}
    return placement, {"elements": elems}, pads


def test_escape_foreign_trace_collision_skips():
    """186 + 261: CN1 fans out first (wide pads → long fanout reach). CN2 sits
    so an escape via would land on a CN1 fanout trace of a DIFFERENT net while
    still clearing CN1's vias/stubs, so _via_clears_foreign_traces returns False
    (186) and that pad's escape is skipped (continue at 261)."""
    placement, netlist, pads = _two_row_parts(ax=3, bx=7.8, board_w=13)
    res = er.generate_escape_routing(placement, netlist, pad_map=pads)
    # Both parts have 12 escaping pins (24 max); the foreign-trace collisions
    # drop CN2's conflicting escapes below the conflict-free total.
    assert 0 < len(res["vias"]) < 24


def test_escape_same_net_trace_is_ignored():
    """184: a pin sharing a net with an earlier, distant pin sees that pin's
    same-net fanout in placed_traces and must NOT treat it as an obstacle (the
    `if tn == net_id: continue` skip), so both same-net escapes still place."""
    pads = {}
    elems = [
        {"element_type": "component", "component_id": "CN1", "designator": "CN1"},
        {"element_type": "component", "component_id": "U", "designator": "U2"},
    ]
    shared = "SHARED"
    net_ports: dict[str, list[str]] = {}
    for i in range(12):
        net = shared if i in (0, 11) else f"n{i}"   # pins 0 & 11 share a net
        pads[f"CN1.{i}"] = _pad("CN1", i, net, x=i * 0.5, y=10.0)
        a, b = f"a{i}", f"u{i}"
        elems += [
            {"element_type": "port", "port_id": a, "component_id": "CN1"},
            {"element_type": "port", "port_id": b, "component_id": "U"},
        ]
        net_ports.setdefault(net, []).extend([a, b])
    for net, ports in net_ports.items():
        elems.append({"element_type": "net", "net_id": net, "name": net,
                      "connected_port_ids": ports})
    placement = {"board": {"width_mm": 40, "height_mm": 40}}
    res = er.generate_escape_routing(placement, {"elements": elems},
                                     pad_map=pads)
    # The shared net escapes from BOTH its pins (the same-net trace did not
    # block the second one): two stubs carry net_name == shared.
    shared_stubs = [t for t in res["traces"]
                    if t.get("escape_role") == "stub" and t["net_name"] == shared]
    assert len(shared_stubs) == 2
