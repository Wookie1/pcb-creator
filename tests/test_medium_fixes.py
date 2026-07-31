"""Unit checks for the MEDIUM fixes from PCB_CREATOR_ISSUES_REPORT.md.

#9 regulator pin typing, #10 resistance parsing, #11 mechanical parts in the
builder, #20 self-colliding footprint diagnosis. (#12/#13 are a tool/docstring
wiring; #12's error path is exercised here, #13 is docs-only.)
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))


# --- #9 regulator input pins are not phantom sources ------------------------

def test_regulator_power_pin_defaults_to_sink():
    from validators.net_classes import infer_electrical_type
    # A connector supplies; a regulator must NOT blanket-source on a power net,
    # or its input reads as a second source on the supply.
    assert infer_electrical_type("power", "connector") == "power_out"
    assert infer_electrical_type("power", "voltage_regulator") == "power_in"


def test_builder_keeps_explicit_regulator_input_type():
    from optimizers.pad_geometry import get_footprint_def
    from orchestrator import circuit_builder as cb
    import json
    d = Path(tempfile.mkdtemp())
    n = "reg"
    cb.create_draft(d, n, "t", 30, 20, layers=2)
    cb.add_component(d, n, "J1", "connector", "hdr", "PinHeader_1x2",
                     footprint_lookup=get_footprint_def)
    cb.add_component(d, n, "U1", "voltage_regulator", "LM1117", "SOT-223",
                     pinout="1:GND 2:OUT 3:IN", footprint_lookup=get_footprint_def)
    cb.add_component(d, n, "R1", "resistor", "10k", "0603",
                     footprint_lookup=get_footprint_def)
    # VIN net: connector supply + regulator IN. Must have exactly ONE source.
    cb.connect_pins(d, n, "VIN", ["J1.1", "U1.3"])
    cb.connect_pins(d, n, "GND", ["J1.2", "U1.1", "R1.2"])
    cb.connect_pins(d, n, "VOUT", ["U1.2", "R1.1"])
    fin = cb.finalize(d, n)
    assert fin["ok"], fin
    nl = json.loads((d / f"{n}_netlist.json").read_text())
    by_id = {e["port_id"]: e for e in nl["elements"]
             if e.get("element_type") == "port"}
    # U1 pin 3 = IN → power_in (sink), not the phantom power_out.
    assert by_id["port_u1_3"]["electrical_type"] == "power_in"


# --- #10 resistance parser: R-notation + bare numbers -----------------------

def test_parse_resistance_r_notation_and_bare():
    from validators.engineering_constants import parse_resistance
    cases = {"4k7": 4700, "2R2": 2.2, "R47": 0.47, "470": 470,
             "470R": 470, "0R": 0, "1k5": 1500, "4.7k": 4700, "10M": 1e7}
    for text, expect in cases.items():
        assert abs(parse_resistance(text) - expect) < 1e-9, text


# --- #11 mechanical parts flow through the builder --------------------------

def test_builder_accepts_mounting_hole_and_fiducial():
    from optimizers.pad_geometry import get_footprint_def
    from orchestrator import circuit_builder as cb
    from validators.validate_netlist import validate_referential_integrity
    import json
    d = Path(tempfile.mkdtemp())
    n = "mech"
    cb.create_draft(d, n, "t", 30, 20, layers=2)
    cb.add_component(d, n, "R1", "resistor", "1k", "0603",
                     footprint_lookup=get_footprint_def)
    cb.add_component(d, n, "R2", "resistor", "1k", "0603",
                     footprint_lookup=get_footprint_def)
    h = cb.add_component(d, n, "H1", "mounting_hole", "M3",
                         "MountingHole_3.2mm_M3", footprint_lookup=get_footprint_def)
    assert h["ok"] and h["pin_count"] == 0 and h["pins"] == []
    f = cb.add_component(d, n, "FID1", "fiducial", "fid", "Fiducial_1mm",
                         footprint_lookup=get_footprint_def)
    assert f["ok"]
    # Mechanical parts take no pins.
    bad = cb.add_component(d, n, "H2", "mounting_hole", "M3",
                           "MountingHole_3.2mm_M3", pin_count=1,
                           footprint_lookup=get_footprint_def)
    assert not bad["ok"] and bad["code"] == "mechanical_no_pins"

    cb.connect_pins(d, n, "A", ["R1.1", "R2.1"])
    cb.connect_pins(d, n, "B", ["R1.2", "R2.2"])
    fin = cb.finalize(d, n)
    assert fin["ok"], fin
    # The portless mounting hole must not trip the "component has no ports" check.
    nl = json.loads((d / f"{n}_netlist.json").read_text())
    errors, _ = validate_referential_integrity(nl)
    assert not any("no ports" in str(e) for e in errors), errors


# --- #20 self-colliding footprints are diagnosed ----------------------------

def test_verify_footprints_flags_self_overlap(monkeypatch):
    from optimizers.pad_geometry import FootprintDef, footprint_pad_overlaps
    import optimizers.pad_geometry as pg
    # A hand-broken footprint: two pads 0.3mm apart but 0.8mm wide → overlap.
    bad = FootprintDef(pin_offsets={1: (-0.15, 0.0), 2: (0.15, 0.0)},
                       pad_size=(0.8, 0.8))
    assert footprint_pad_overlaps(bad) == [(1, 2)]

    monkeypatch.setattr(pg, "get_footprint_def", lambda pkg, pc, **k: bad)
    from validators import verify_footprints as vf
    netlist = {"elements": [
        {"element_type": "component", "component_id": "comp_u1",
         "designator": "U1", "component_type": "ic", "package": "BADPKG"},
        {"element_type": "port", "port_id": "p1", "component_id": "comp_u1",
         "pin_number": 1, "name": "1", "electrical_type": "signal"},
        {"element_type": "port", "port_id": "p2", "component_id": "comp_u1",
         "pin_number": 2, "name": "2", "electrical_type": "signal"},
    ]}
    issues = vf.verify_footprints(netlist)
    assert any("overlapping pads" in i["reason"] for i in issues), issues
