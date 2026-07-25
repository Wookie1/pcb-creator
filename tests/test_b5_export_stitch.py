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
from pathlib import Path

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


def test_candidates_derive_app_root_from_kicad_cli_env(monkeypatch):
    monkeypatch.setenv("PCB_KICAD_CLI",
                       "/opt/KiCad.app/Contents/MacOS/kicad-cli")
    cands = _kicad_python_candidates()
    assert isinstance(cands, list) and cands  # bundle root probed w/o error
    monkeypatch.setenv("PCB_KICAD_PYTHON", "/nonexistent/py")
    assert _kicad_python_candidates()[0] == "/nonexistent/py"


def test_stitch_returns_zero_when_no_interpreter_works(monkeypatch, tmp_path):
    import exporters.kicad_exporter as ke
    monkeypatch.setattr(ke, "_kicad_python_candidates",
                        lambda: ["/nonexistent/python-xyz"])
    assert ke.stitch_gnd_islands_pcbnew(tmp_path / "x.kicad_pcb") == {
        "added": [], "skipped": 0}


def test_stitch_is_noop_safe_on_clean_board(tmp_path):
    py = _pcbnew_python()
    if py is None:
        pytest.skip("no pcbnew-capable python available")
    routed, netlist = _four_layer_board()
    out = export_kicad_pcb(routed, netlist, tmp_path / "b.kicad_pcb")
    # Run the stitch script DIRECTLY so a crash fails the test (the wrapper's
    # best-effort 0 would mask it), then check the wrapper agrees.
    from exporters.kicad_exporter import _GND_STITCH_SCRIPT
    r = subprocess.run([py, _GND_STITCH_SCRIPT, str(out), "0.6", "0.3"],
                       capture_output=True, text=True, timeout=240)
    assert r.returncode == 0, f"stitch script crashed: {r.stderr[-2000:]}"
    assert json.loads(r.stdout.strip().splitlines()[-1])["added"] == [], \
        "clean board must need no stitch vias"
    assert stitch_gnd_islands_pcbnew(out)["added"] == []


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


# --- via-size floor + harvest (what ships == what passed DRC) --------------

def test_board_via_minima_floors_at_tightest_via():
    from exporters.kicad_exporter import board_via_minima
    routed = {"routing": {"config": {"via_diameter_mm": 0.6, "via_drill_mm": 0.3},
                          "vias": [{"diameter_mm": 0.6, "drill_mm": 0.3}]}}
    assert board_via_minima(routed) == (0.6, 0.3)
    # A fine-pitch escape via lowers the board's legal floor.
    routed["routing"]["vias"].append({"diameter_mm": 0.45, "drill_mm": 0.2})
    assert board_via_minima(routed) == (0.45, 0.2)


def test_stitch_is_capped_by_board_minima(monkeypatch, tmp_path):
    """The wrapper must pass the board's floor to the script — a 0.45/0.2 via
    on a 0.6/0.3 board is exactly the DRC failure this prevents."""
    import exporters.kicad_exporter as ke
    seen = {}

    class _R:
        returncode = 0
        stdout = json.dumps({"added": [], "skipped": 1})
        stderr = ""

    def fake_run(cmd, **kw):
        seen["cmd"] = cmd
        return _R()

    monkeypatch.setattr(ke, "_kicad_python_candidates", lambda: ["py"])
    monkeypatch.setattr(ke.subprocess, "run", fake_run)
    out = ke.stitch_gnd_islands_pcbnew(tmp_path / "b.kicad_pcb", 0.6, 0.3)
    assert seen["cmd"][-2:] == ["0.6", "0.3"]      # minima reach the script
    assert out == {"added": [], "skipped": 1}      # unstitchable island surfaced


def test_harvest_merges_stitch_vias_into_routed():
    """Harvested vias must land in routed.json so the Gerbers carry them."""
    from exporters.kicad_exporter import _harvest_stitch_vias
    routed = {"routing": {"vias": [{"x_mm": 1.0, "y_mm": 1.0, "drill_mm": 0.3,
                                    "diameter_mm": 0.6, "net_id": "net_a"}]}}
    nets = [{"element_type": "net", "net_id": "net_gnd_1", "name": "GND"}]
    n = _harvest_stitch_vias(routed, nets, {
        "added": [{"x_mm": 5.5, "y_mm": 6.25, "diameter_mm": 0.6, "drill_mm": 0.3}],
        "skipped": 2})
    assert n == 1
    v = routed["routing"]["vias"][-1]
    assert (v["x_mm"], v["y_mm"]) == (5.5, 6.25)
    assert v["net_id"] == "net_gnd_1" and v["net_name"] == "GND"
    assert v["from_layer"] == "top" and v["to_layer"] == "bottom"
    # Unstitchable islands are surfaced, not silently dropped.
    assert routed["routing"]["unstitched_gnd_islands"] == 2
    # A later clean pass clears the flag and adds nothing.
    assert _harvest_stitch_vias(routed, nets, {"added": [], "skipped": 0}) == 0
    assert "unstitched_gnd_islands" not in routed["routing"]


def test_harvest_dedupes_by_position():
    """Re-harvesting the same via must not ship a duplicate drill hit."""
    from exporters.kicad_exporter import _harvest_stitch_vias
    routed = {"routing": {"vias": []}}
    nets = [{"net_id": "net_gnd", "name": "GND"}]
    same = {"added": [{"x_mm": 4.0, "y_mm": 5.0,
                       "diameter_mm": 0.6, "drill_mm": 0.3}]}
    assert _harvest_stitch_vias(routed, nets, same) == 1
    assert _harvest_stitch_vias(routed, nets, same) == 0   # idempotent
    assert len(routed["routing"]["vias"]) == 1


def test_harvested_vias_are_schema_valid():
    import jsonschema
    from exporters.kicad_exporter import _harvest_stitch_vias
    schema = json.loads((Path(__file__).parent.parent /
                         "schemas" / "routed_schema.json").read_text())
    routed = {"routing": {"vias": []}}
    _harvest_stitch_vias(routed, [{"net_id": "net_gnd", "name": "GND"}],
                         {"added": [{"x_mm": 2.0, "y_mm": 3.0,
                                     "diameter_mm": 0.6, "drill_mm": 0.3}]})
    jsonschema.validate(routed["routing"]["vias"][0],
                        schema["definitions"]["via"])
