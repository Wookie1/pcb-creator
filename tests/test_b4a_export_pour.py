"""B4a regression: exported .kicad_pcb must ship POURED zones.

`_copper_fills` writes each zone as a board-outline polygon with `(fill yes)` but
no `(filled_polygon …)`, relying on a later pour. `export_kicad_pcb` already calls
`fill_zones_pcbnew`, but its python-candidate list only tried `/usr/bin/python3`
and `python3` — neither has `pcbnew` on macOS (it lives in KiCad.app's bundled
framework python), so the pour silently no-op'd and every exported board shipped
with 0 filled polygons → `kicad-cli pcb drc` reported a flood of false unconnected.
"""
import os
import subprocess

import pytest

from exporters.kicad_exporter import (
    export_kicad_pcb, _kicad_python_candidates,
)


def test_candidates_prefer_explicit_env(monkeypatch):
    monkeypatch.setenv("PCB_KICAD_PYTHON", "/custom/py")
    cands = _kicad_python_candidates()
    assert cands[0] == "/custom/py"
    assert len(cands) == len(set(cands)), "candidates must be de-duplicated"


def test_candidates_include_kicad_app_bundle(monkeypatch):
    """When KiCad.app is installed, its bundled python must be a candidate."""
    monkeypatch.delenv("PCB_KICAD_PYTHON", raising=False)
    cands = _kicad_python_candidates()
    if not os.path.isdir("/Applications/KiCad/KiCad.app"):
        pytest.skip("KiCad.app not installed")
    assert any("KiCad.app/Contents/Frameworks/Python.framework" in c for c in cands), \
        "bundled pcbnew interpreter must be probed on macOS"


def _pcbnew_python():
    """First candidate that can import pcbnew, or None."""
    for py in _kicad_python_candidates():
        try:
            r = subprocess.run([py, "-c", "import pcbnew"],
                               capture_output=True, timeout=60)
        except (OSError, subprocess.SubprocessError):
            continue
        if r.returncode == 0:
            return py
    return None


def _min_board():
    netlist = {"version": "1.0", "elements": [
        {"element_type": "component", "component_id": "c_r1", "designator": "R1",
         "component_type": "resistor", "value": "1k", "package": "R_0805_2012Metric"},
        {"element_type": "port", "port_id": "p_r1_1", "component_id": "c_r1", "pin_number": 1},
        {"element_type": "port", "port_id": "p_r1_2", "component_id": "c_r1", "pin_number": 2},
        {"element_type": "net", "net_id": "net_gnd", "name": "GND",
         "net_class": "power", "connected_port_ids": ["p_r1_1", "p_r1_2"]},
    ]}
    outer = [(0.3, 0.3), (19.7, 0.3), (19.7, 19.7), (0.3, 19.7), (0.3, 0.3)]
    routed = {
        "board": {"width_mm": 20.0, "height_mm": 20.0, "layers": 2},
        "placements": [{"designator": "R1", "package": "R_0805_2012Metric",
                        "component_type": "resistor", "x_mm": 10.0, "y_mm": 10.0,
                        "rotation_deg": 0, "layer": "top",
                        "footprint_width_mm": 2.0, "footprint_height_mm": 1.25}],
        "routing": {"traces": [], "vias": [],
                    "copper_fills": [{"layer": "top", "net_id": "net_gnd",
                                      "net_name": "GND", "polygons": [outer]}]},
    }
    return routed, netlist


def test_export_ships_poured_zones(tmp_path):
    py = _pcbnew_python()
    if py is None:
        pytest.skip("no pcbnew-capable python available")
    routed, netlist = _min_board()
    out = export_kicad_pcb(routed, netlist, tmp_path / "b.kicad_pcb")
    text = out.read_text()
    assert "(zone" in text, "a GND zone must be emitted"
    assert "(filled_polygon" in text, \
        "B4a: exported zones must be poured (filled_polygon present)"


# --- Authoritative integration check (the one the bug report asks for): reload
# the exported board with pcbnew and confirm every copper zone actually has fill
# geometry, then run kicad-cli DRC and confirm it sees the GND pads connected
# (0 unconnected) — the false-"unconnected" flood was the whole B4a symptom.
_PCBNEW_INSPECT = (
    "import sys, pcbnew\n"
    "b = pcbnew.LoadBoard(sys.argv[1])\n"
    "zones = list(b.Zones())\n"
    "filled = all(z.GetFilledPolysList(z.GetLayer()).OutlineCount() > 0 for z in zones)\n"
    "print('ZONES', len(zones), 'FILLED', filled)\n"
)


def test_exported_zones_have_fill_geometry_and_drc_connects(tmp_path):
    py = _pcbnew_python()
    if py is None:
        pytest.skip("no pcbnew-capable python available")
    routed, netlist = _min_board()
    out = export_kicad_pcb(routed, netlist, tmp_path / "b.kicad_pcb")

    # (1) pcbnew round-trip: every zone has a non-empty filled-polys list.
    r = subprocess.run([py, "-c", _PCBNEW_INSPECT, str(out)],
                       capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stderr
    line = next(l for l in r.stdout.splitlines() if l.startswith("ZONES"))
    _, nz, _, filled = line.split()
    assert int(nz) >= 1 and filled == "True", \
        f"B4a: every exported zone must be poured ({line})"

    # (2) kicad-cli DRC: with poured GND, both R1 pads are connected → 0 unconnected.
    from optimizers.route_cleanup import find_kicad_cli
    kcli = find_kicad_cli()
    if not kcli:
        pytest.skip("kicad-cli not available")
    import json
    rpt = tmp_path / "drc.json"
    subprocess.run([kcli, "pcb", "drc", "--severity-error", "--format", "json",
                    "-o", str(rpt), str(out)], capture_output=True, timeout=120)
    d = json.loads(rpt.read_text())
    assert len(d.get("unconnected_items", [])) == 0, \
        "B4a: poured GND zone must connect the pads (no false unconnected)"
