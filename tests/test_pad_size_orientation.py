"""A footprint's pads may hold more than one orientation.

An LQFP-48 is 24 pads at 0.3x1.475 and 24 at 1.475x0.3 — its sides are rotated
90° from each other. The parser used to take the median of widths and the median
of heights INDEPENDENTLY, so both landed on 1.475 and every pin became a 1.475mm
SQUARE: the union of both orientations, a pad that does not exist on the part.

On 0.5mm pitch that overlaps each neighbour by 0.975mm, merging all 12 pads of a
side into one copper blob. Freerouting saw the nets pre-shorted and routed 0% of
the board; kicad-cli reported clearance 0.0000mm and 140 errors. Correcting the
geometry took the same board to 90.9%.
"""

import pytest

pad_geometry = pytest.importorskip("optimizers.pad_geometry")


@pytest.fixture(scope="module")
def kicad_lookup():
    """Real KiCad library, or skip — this bug only shows on real footprints."""
    import pathlib
    from orchestrator.config import OrchestratorConfig
    cfg = OrchestratorConfig.from_env(
        base_dir=pathlib.Path(__file__).resolve().parent.parent)
    if not cfg.kicad_library_path:
        pytest.skip("no system KiCad footprint library")
    from exporters.kicad_mod_parser import KiCadLibraryIndex
    from orchestrator.cache import ComponentCache
    prev = (pad_geometry._KICAD_INDEX_DEFAULT, pad_geometry._CACHE_DEFAULT) \
        if hasattr(pad_geometry, "_KICAD_INDEX_DEFAULT") else None
    pad_geometry.configure_lookup(
        kicad_index=KiCadLibraryIndex(cfg.kicad_library_path),
        cache=ComponentCache(cfg.component_cache_path))
    yield
    if prev:
        pad_geometry.configure_lookup(kicad_index=prev[0], cache=prev[1])


def test_lqfp48_pads_do_not_overlap_their_neighbours(kicad_lookup):
    fp = pad_geometry.get_footprint_def("LQFP-48", 48)
    assert fp is not None

    # Left side: pins share an x, so pitch is the y-spacing and the pad's
    # HEIGHT is what must fit inside it.
    left = sorted((dy, n) for n, (dx, dy) in fp.pin_offsets.items() if dx < -3)
    assert len(left) >= 2
    pitch = abs(left[1][0] - left[0][0])
    _, height = fp.pad_size_for(left[0][1])
    assert height < pitch, (
        f"pad height {height}mm >= pitch {pitch}mm — adjacent pads overlap, "
        "which shorts every pin on the side together")

    # Bottom side is rotated 90°, so there the WIDTH must fit the pitch.
    bottom = sorted((dx, n) for n, (dx, dy) in fp.pin_offsets.items() if dy < -3)
    pitch_b = abs(bottom[1][0] - bottom[0][0])
    width, _ = fp.pad_size_for(bottom[0][1])
    assert width < pitch_b, (
        f"pad width {width}mm >= pitch {pitch_b}mm on the rotated side")


def test_quad_pack_keeps_both_pad_orientations(kicad_lookup):
    fp = pad_geometry.get_footprint_def("LQFP-48", 48)
    sizes = {fp.pad_size_for(n) for n in fp.pin_offsets}
    assert len(sizes) == 2, f"expected two rotated pad shapes, got {sizes}"
    # The two are each other's transpose, never a square.
    (w1, h1), (w2, h2) = sorted(sizes)
    assert (w1, h1) == (h2, w2)
    assert w1 != h1, "a square pad means the orientations were merged"


def test_uniform_footprint_has_no_per_pin_table(kicad_lookup):
    """Passives keep the single representative size — no needless per-pin dict."""
    fp = pad_geometry.get_footprint_def("0402", 2)
    assert fp is not None
    assert fp.pin_pad_sizes is None
    assert fp.pad_size_for(1) == fp.pad_size


def test_pad_map_uses_per_pin_size(kicad_lookup):
    """The bug reached copper through build_pad_map, so pin it there too."""
    netlist = {"version": "1.0", "project_name": "p", "elements": [
        {"element_type": "component", "component_id": "comp_u1",
         "designator": "U1", "component_type": "ic", "value": "x",
         "package": "LQFP-48"},
    ]}
    # All 48 — build_pad_map derives pin_count from the declared ports, and a
    # short list resolves a different (generic) footprint entirely.
    for pin in range(1, 49):
        netlist["elements"].append(
            {"element_type": "port", "port_id": f"port_u1_{pin}",
             "component_id": "comp_u1", "pin_number": pin, "name": str(pin),
             "electrical_type": "signal"})
    placement = {"version": "1.0", "project_name": "p",
                 "board": {"width_mm": 30.0, "height_mm": 30.0,
                           "outline_type": "rectangle", "origin": [0, 0]},
                 "placements": [{"designator": "U1", "component_type": "ic",
                                 "package": "LQFP-48",
                                 "footprint_width_mm": 9.0,
                                 "footprint_height_mm": 9.0,
                                 "x_mm": 15.0, "y_mm": 15.0, "rotation_deg": 0,
                                 "layer": "top",
                                 "placement_source": "algorithm"}]}
    pads = pad_geometry.build_pad_map(placement, netlist)
    side = pads["port_u1_1"]      # left side  — long in x
    bottom = pads["port_u1_13"]   # bottom side — long in y
    assert side.pad_width_mm > side.pad_height_mm
    assert bottom.pad_height_mm > bottom.pad_width_mm


# --- the exported board, not just the router's model -----------------------
# build_pad_map and the DSN were fixed first; kicad_exporter still wrote one
# size for every pad, so the board kicad-cli actually checks had half an
# LQFP-48's pads 1.475mm wide on 0.5mm pitch. Routed geometry measured clean
# while kicad-cli reported 60+ clearance violations and tracks shorting to
# <no net> pads. Fixing it took that board from 72 DRC errors to 1.

def test_exported_kicad_pads_use_per_pin_size(kicad_lookup, tmp_path):
    import re
    from exporters.kicad_exporter import export_kicad_pcb

    netlist = {"version": "1.0", "project_name": "p", "elements": [
        {"element_type": "component", "component_id": "comp_u1",
         "designator": "U1", "component_type": "ic", "value": "x",
         "package": "LQFP-48"},
    ]}
    for pin in range(1, 49):
        netlist["elements"].append(
            {"element_type": "port", "port_id": f"port_u1_{pin}",
             "component_id": "comp_u1", "pin_number": pin, "name": str(pin),
             "electrical_type": "signal"})
    routed = {"board": {"width_mm": 30.0, "height_mm": 30.0, "layers": 2},
              "placements": [{"designator": "U1", "component_type": "ic",
                              "package": "LQFP-48", "footprint_width_mm": 9.0,
                              "footprint_height_mm": 9.0, "x_mm": 15.0,
                              "y_mm": 15.0, "rotation_deg": 0, "layer": "top",
                              "placement_source": "algorithm"}],
              "routing": {"traces": [], "vias": [], "copper_fills": [],
                          "unrouted_nets": [], "statistics": {}}}

    out = tmp_path / "b.kicad_pcb"
    export_kicad_pcb(routed, netlist, out)
    text = out.read_text()

    sizes = {tuple(sorted((float(a), float(b))))
             for a, b in re.findall(r'\(size ([\d.]+) ([\d.]+)\)', text)}
    # Both rotated orientations must be present, and neither may be square.
    assert (0.3, 1.475) in sizes, f"narrow LQFP pad missing from export: {sizes}"
    assert (1.475, 1.475) not in sizes, (
        "a 1.475mm SQUARE pad was exported — the per-footprint pad_size leaked "
        "back in, and on 0.5mm pitch that overlaps every neighbour")
