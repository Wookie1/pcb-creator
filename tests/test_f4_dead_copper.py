"""F4 regressions: dead copper must not reach manufacturing.

Audit board F4 had three coupled failures, each invisible to the internal
validator but caught by kicad DRC on the shipped board:

(a) the exporter shipped TH pin-1 as a *square* pad while the DSN padstack
    (what Freerouting routed against) and the internal pad-clearance model
    were *circles* — a 45-degree trace could legally clip the square corner,
    failing trace clearance + solder_mask_bridge in kicad DRC;
(b) Freerouting emitted redundant scribbles — U-turn back-traces, duplicate
    wires over the same span, and the same short link drawn once per copper
    layer — whose free ends mutually "supported" each other, so the
    dangling-stub pass could never collapse them (import_ses now dedups);
(c) the dangling-stub pass treated any-layer copper as support, so a stub
    riding on a different layer than the copper beneath it survived
    (_remove_dangling_traces is now layer-aware).
"""
from types import SimpleNamespace

from exporters.kicad_exporter import export_kicad_pcb
from exporters.ses_importer import import_ses
from optimizers.router import _remove_dangling_traces


# ---------------------------------------------------------------------------
# (b) SES import dedup: exact reverse pairs and cross-layer clones dropped
# ---------------------------------------------------------------------------

_NETLIST = {"version": "1.0", "elements": [
    {"element_type": "component", "component_id": "c1", "designator": "J1",
     "component_type": "connector", "package": "PinHeader_1x02_P2.54mm"},
    {"element_type": "port", "port_id": "p1", "component_id": "c1", "pin_number": 1},
    {"element_type": "port", "port_id": "p2", "component_id": "c1", "pin_number": 2},
    {"element_type": "net", "net_id": "net_addr", "name": "ADDR",
     "connected_port_ids": ["p1", "p2"]},
]}

_PLACEMENT = {"board": {"width_mm": 30.0, "height_mm": 30.0, "layers": 2},
              "placements": []}


def _import(ses: str, tmp_path):
    path = tmp_path / "s.ses"
    path.write_text(ses)
    return import_ses(path, _PLACEMENT, _NETLIST)


def _spans(routed):
    return {(t["layer"], tuple(sorted([(t["start_x_mm"], t["start_y_mm"]),
                                       (t["end_x_mm"], t["end_y_mm"])])))
            for t in routed["routing"]["traces"]}


def test_ses_cross_layer_clone_dropped(tmp_path):
    """Same 0.46mm span drawn on BOTH layers (the shipped ADDR stub): only the
    first wire may survive — the clone anchors the original's free end, so
    KiCad DRC reports the pair as track_dangling."""
    routed = _import("""
(session "x"
 (resolution mm 1000)
 (router)
 (routes
  (library)
  (network_out
   (net ADDR
    (wire (path F.Cu 128 5402 4087 4941 4087))
    (wire (path B.Cu 128 5402 4087 4941 4087))
   )
  )
 )
)
""", tmp_path)
    assert _spans(routed) == {("top", ((4.941, 4.087), (5.402, 4.087)))}


def test_ses_uturn_and_clone_dropped_legit_zigzag_kept(tmp_path):
    """A wire that back-traces over itself and a second wire duplicating its
    first leg must collapse to the real geometry; the genuine third leg and
    non-coincident segments must survive."""
    routed = _import("""
(session "x"
 (resolution mm 1000)
 (router)
 (routes
  (library)
  (network_out
   (net ADDR
    (wire (path F.Cu 128 5402 4087 4941 4087 5402 4087 6000 4200))
    (wire (path F.Cu 128 5402 4087 4941 4087))
    (wire (path B.Cu 128 6000 4200 7000 4300))
   )
  )
 )
)
""", tmp_path)
    spans = _spans(routed)
    assert ("top", ((4.941, 4.087), (5.402, 4.087))) in spans
    assert ("top", ((5.402, 4.087), (6.0, 4.2))) in spans
    assert ("bottom", ((6.0, 4.2), (7.0, 4.3))) in spans
    assert len(spans) == 3, spans


# ---------------------------------------------------------------------------
# (c) layer-aware dangling-stub pruning
# ---------------------------------------------------------------------------

def _pad(x, y, net="net_0", layer="bottom"):
    return SimpleNamespace(x_mm=x, y_mm=y, pad_width_mm=1.7, pad_height_mm=1.7,
                           layer=layer, net_id=net)


def _trace(x1, y1, x2, y2, layer, net="net_0"):
    return {"start_x_mm": x1, "start_y_mm": y1, "end_x_mm": x2, "end_y_mm": y2,
            "width_mm": 0.128, "layer": layer, "net_id": net}


def test_cross_layer_clone_pair_pruned():
    """Two identical free-floating chords (top + bottom) offset 0.35mm above a
    bottom main: with any-layer support they anchored each other AND borrowed
    support from the other layer's main; layer-aware, every end is free → both
    go. (A *collinear* clone is different: its ends lie on the main trace,
    which is a genuine electrical connection, not a stub.)"""
    routing = {"traces": [
        _trace(0.0, 0.0, 10.0, 0.0, "bottom"),
        _trace(3.0, 0.35, 4.5, 0.35, "top"),
        _trace(3.0, 0.35, 4.5, 0.35, "bottom"),
    ]}
    pad_map = {"a": _pad(0.0, 0.0), "b": _pad(10.0, 0.0)}
    removed = _remove_dangling_traces(routing, pad_map)
    assert removed == 2
    assert len(routing["traces"]) == 1
    assert routing["traces"][0]["end_x_mm"] == 10.0


def test_top_stub_over_bottom_main_pruned():
    """A top stub whose ends only touch OTHER-layer copper is dead copper."""
    routing = {"traces": [
        _trace(0.0, 0.0, 10.0, 0.0, "bottom"),
        _trace(3.0, 0.2, 7.0, 0.2, "top"),
    ]}
    pad_map = {"a": _pad(0.0, 0.0), "b": _pad(10.0, 0.0)}
    assert _remove_dangling_traces(routing, pad_map) == 1


def test_layer_supported_copper_kept():
    """Support still works on a shared layer (through-hole pad = all layers):
    a top trace whose two ends land inside the TH pad copper at each end
    (0.5mm from each pad centre, within half-size+tol) must survive even
    though it runs above other-layer copper for most of its length."""
    routing = {"traces": [
        _trace(0.0, 0.0, 10.0, 0.0, "bottom"),
        _trace(-0.5, 0.0, 10.5, 0.0, "top"),
    ]}
    pad_map = {"a": _pad(0.0, 0.0, layer="all"), "b": _pad(10.0, 0.0, layer="all")}
    assert _remove_dangling_traces(routing, pad_map) == 0


# ---------------------------------------------------------------------------
# (a) exported TH pads are circles — matching the DSN padstack
# ---------------------------------------------------------------------------

def _th_board():
    netlist = {"version": "1.0", "elements": [
        {"element_type": "component", "component_id": "c_j1", "designator": "J1",
         "component_type": "connector", "package": "PinHeader_1x02_P2.54mm"},
        {"element_type": "port", "port_id": "p_j1_1", "component_id": "c_j1",
         "pin_number": 1},
        {"element_type": "port", "port_id": "p_j1_2", "component_id": "c_j1",
         "pin_number": 2},
        {"element_type": "net", "net_id": "net_addr", "name": "ADDR",
         "net_class": "signal", "connected_port_ids": ["p_j1_1", "p_j1_2"]},
    ]}
    routed = {
        "board": {"width_mm": 30.0, "height_mm": 30.0, "layers": 2},
        "placements": [{"designator": "J1", "package": "PinHeader_1x02_P2.54mm",
                        "component_type": "connector", "x_mm": 15.0, "y_mm": 15.0,
                        "rotation_deg": 0, "layer": "top",
                        "footprint_width_mm": 2.54, "footprint_height_mm": 2.54}],
        "routing": {"traces": [], "vias": [], "copper_fills": []},
    }
    return routed, netlist


def test_export_emits_no_rectangular_th_pads(tmp_path):
    routed, netlist = _th_board()
    text = export_kicad_pcb(routed, netlist, tmp_path / "th.kicad_pcb").read_text()
    assert "thru_hole rect" not in text, (
        "rectangular TH pin-1 is wider at its corners than the circle the "
        "router routed against — shipped copper can fail kicad clearance")
    assert text.count("thru_hole circle") == 2
    assert "(drill" in text
