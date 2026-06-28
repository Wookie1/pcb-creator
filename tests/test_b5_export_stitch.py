"""B5 (export layer): pour-and-stitch GND islands on the authoritative geometry.

The in-core grid rescue (router._add_rescue_vias) reduces GND islands but can't
match KiCad's poured geometry, so a residual island can survive into the exported
board. stitch_gnd_islands_pcbnew runs under pcbnew on the exported .kicad_pcb: it
pours, finds GND regions with no through-via to the plane, drops a clear GND
through-via into each, and re-pours. End-to-end coverage of a board that actually
fragments lives in test_integration_b3_carrier.py (opt-in); here we cover the
function contract + that a 4-layer export stays poured and connected.
"""
import json
import subprocess

import pytest

from exporters.kicad_exporter import (
    export_kicad_pcb, stitch_gnd_islands_pcbnew, _kicad_python_candidates,
)


def _pcbnew_python():
    for py in _kicad_python_candidates():
        try:
            r = subprocess.run([py, "-c", "import pcbnew"], capture_output=True, timeout=60)
        except (OSError, subprocess.SubprocessError):
            continue
        if r.returncode == 0:
            return py
    return None


def _four_layer_board():
    """Minimal 4-layer board (In1 GND plane) with GND copper fills already present."""
    netlist = {"version": "1.0", "elements": [
        {"element_type": "component", "component_id": "c_u1", "designator": "U1",
         "component_type": "ic", "value": "x", "package": "SOIC-8"},
        {"element_type": "port", "port_id": "p1", "component_id": "c_u1", "pin_number": 1},
        {"element_type": "port", "port_id": "p2", "component_id": "c_u1", "pin_number": 2},
        {"element_type": "net", "net_id": "net_gnd", "name": "GND",
         "connected_port_ids": ["p1", "p2"]},
    ]}
    outer = [(0.3, 0.3), (19.7, 0.3), (19.7, 19.7), (0.3, 19.7), (0.3, 0.3)]
    routed = {
        "board": {"width_mm": 20.0, "height_mm": 20.0, "layers": 4, "plane_layers": 2},
        "placements": [{"designator": "U1", "package": "SOIC-8", "component_type": "ic",
                        "x_mm": 10.0, "y_mm": 10.0, "rotation_deg": 0, "layer": "top",
                        "footprint_width_mm": 5.0, "footprint_height_mm": 4.0}],
        "routing": {"traces": [], "vias": [],
                    "copper_fills": [
                        {"layer": "top", "net_id": "net_gnd", "net_name": "GND",
                         "polygons": [outer]},
                        {"layer": "inner1", "net_id": "net_gnd", "net_name": "GND",
                         "is_plane": True, "polygons": [outer]}]},
    }
    return routed, netlist


def test_stitch_is_noop_safe_on_clean_board(tmp_path):
    if _pcbnew_python() is None:
        pytest.skip("no pcbnew-capable python available")
    routed, netlist = _four_layer_board()
    out = export_kicad_pcb(routed, netlist, tmp_path / "b.kicad_pcb")
    # export already ran the stitch for this 4-layer board; calling again must be a
    # safe no-op (nothing isolated) and return an int count.
    n = stitch_gnd_islands_pcbnew(out)
    assert isinstance(n, int) and n >= 0


def test_four_layer_export_is_poured_and_connected(tmp_path):
    if _pcbnew_python() is None:
        pytest.skip("no pcbnew-capable python available")
    from optimizers.route_cleanup import find_kicad_cli
    kcli = find_kicad_cli()
    if not kcli:
        pytest.skip("kicad-cli not available")
    routed, netlist = _four_layer_board()
    out = export_kicad_pcb(routed, netlist, tmp_path / "b.kicad_pcb")
    assert "(filled_polygon" in out.read_text(), "zones must be poured"
    rpt = tmp_path / "drc.json"
    subprocess.run([kcli, "pcb", "drc", "--severity-error", "--format", "json",
                    "-o", str(rpt), str(out)], capture_output=True, timeout=180)
    uc = json.loads(rpt.read_text()).get("unconnected_items", [])
    gnd_islands = [u for u in uc if all(
        "Zone" in i.get("description", "") and "GND" in i.get("description", "")
        for i in u.get("items", []))]
    assert not gnd_islands, f"4-layer GND must have no pour islands: {gnd_islands}"
