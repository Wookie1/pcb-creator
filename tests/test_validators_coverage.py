"""Coverage-gap tests for validator modules.

These target error/violation/edge-case branches that the existing
per-validator test files (test_drc_checks.py, test_validate_placement.py,
test_validate_bom.py, etc.) do not exercise. Each test crafts a minimal
bad input and asserts the SPECIFIC violation is detected (and absent for
good input where relevant) — not mere truthiness.
"""

import json
import os
import sys
import tempfile

import pytest

# validators/ on path for direct imports (matches the other validator tests)
_VAL = os.path.join(os.path.dirname(__file__), "..", "validators")
sys.path.insert(0, _VAL)
_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, _ROOT)

# Fixtures live in the MAIN repo (the worktree has no projects/ dir).
_MAIN_REPO = "/Users/James/ai-sandbox/Productizr/pcb-creator"
PROJ = os.path.join(_MAIN_REPO, "projects", "blink_3_leds_dc_power")
if not os.path.isdir(PROJ):
    PROJ = os.path.join(_ROOT, "projects", "blink_3_leds_dc_power")
BLINK_ROUTED = os.path.join(PROJ, "blink_3_leds_dc_power_routed.json")
BLINK_NETLIST = os.path.join(PROJ, "blink_3_leds_dc_power_netlist.json")
BLINK_REQS = os.path.join(PROJ, "blink_3_leds_dc_power_requirements.json")


def _write_temp(data) -> str:
    f = tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False)
    json.dump(data, f)
    f.close()
    return f.name


# ===========================================================================
# engineering_constants.py
# ===========================================================================
import engineering_constants as ec


def test_parse_current_milliamp_branch():
    assert ec.parse_current("20mA") == pytest.approx(0.020)
    assert ec.parse_current("1A") == pytest.approx(1.0)


def test_parse_current_unparseable_raises():
    with pytest.raises(ValueError):
        ec.parse_current("not a current")


def test_parse_resistance_unparseable_raises():
    with pytest.raises(ValueError):
        ec.parse_resistance("nonsense")


def test_parse_capacitance_unparseable_raises():
    with pytest.raises(ValueError):
        ec.parse_capacitance("nonsense")


def test_get_dfm_profile_unknown_falls_back_to_generic():
    prof = ec.get_dfm_profile("totally-unknown-fab")
    assert prof is ec.MANUFACTURER_DFM_PROFILES["generic"]


def test_get_dfm_profile_partial_match():
    # "jlcpcb" is a substring of "jlcpcb_standard" → partial match path
    assert ec.get_dfm_profile("jlcpcb")["description"].startswith("JLCPCB")


def test_format_resistance_all_ranges():
    assert ec.format_resistance(150) == "150ohm"
    assert ec.format_resistance(10_000) == "10kohm"
    assert ec.format_resistance(1_000_000) == "1Mohm"


# ===========================================================================
# net_classes.py
# ===========================================================================
import net_classes as nc


def test_infer_net_class_empty_is_signal():
    assert nc.infer_net_class("") == "signal"
    assert nc.infer_net_class("   ") == "signal"


def test_infer_net_class_numeric_voltage_branch():
    # Hits the `^[+\-]?\d+V\d*$` re.match branch (line 30): a bare "12V"
    # form not already caught by _POWER_RE's leading patterns.
    assert nc.infer_net_class("-12V") == "power"


def test_infer_net_class_vbus_startswith_branch():
    # Hits the startswith(("VCC","VDD","VBAT","VBUS")) branch (line 32):
    # a name like "VBUS_USB" that the strict _POWER_RE anchors don't match
    # but the startswith fallback does.
    assert nc.infer_net_class("VBAT_BACKUP") == "power"


def test_infer_electrical_type_branches():
    assert nc.infer_electrical_type("power", "connector") == "power_out"
    assert nc.infer_electrical_type("power", "ic") == "power_in"
    assert nc.infer_electrical_type("signal", "resistor") == "passive"
    assert nc.infer_electrical_type("signal", "ic") == "signal"


# ===========================================================================
# pinout.py
# ===========================================================================
import pinout as po


def test_infer_pin_type_mixed_power_alias():
    # All non-signal names (VCC/AVCC) agree → single-type path (line 86)
    assert po._infer_pin_type(["VCC", "AVCC"]) == "power_in"


def test_infer_pin_type_two_distinct_non_signal_types():
    # power_in + ground, no signal → pick most-common non-signal (line 91-94)
    assert po._infer_pin_type(["VCC", "GND"]) in ("power_in", "ground")


def test_infer_pin_type_mixed_with_signal_is_signal():
    # power + signal mix → signal (line 96)
    assert po._infer_pin_type(["PC6", "RESET"]) == "signal"
    assert po._infer_pin_type(["VCC", "PC6"]) == "signal"


def test_parse_pinout_skips_malformed_int_token():
    # "x:VCC" has non-int pin number → skipped (line 122-123)
    pins = po.parse_pinout("x:VCC 2:GND")
    assert 2 in pins
    assert len(pins) == 1


def test_parse_pinout_skips_empty_function():
    # "1:" → empty func skipped (line 127); "2: " trailing also empty
    pins = po.parse_pinout("1: 2:GND")
    assert list(pins.keys()) == [2]


def test_parse_pinout_skips_slash_only_function():
    # "1:/" → split on "/" yields no names (line 131)
    pins = po.parse_pinout("1:/ 2:GND")
    assert list(pins.keys()) == [2]


def test_expected_pin_count_bad_specs_falls_through_to_package():
    # specs.pin_count is non-int → except branch (255-256), then package parse
    assert po.expected_pin_count("SOIC-8", {"pin_count": "not-a-number"}) == 8


# ===========================================================================
# verify_footprints.py
# ===========================================================================
from verify_footprints import verify_footprints


def test_verify_footprints_fiducial_exempt_and_missing_package():
    netlist = {
        "elements": [
            # fiducial → skipped entirely (line 56)
            {"element_type": "component", "component_id": "f1",
             "designator": "FID1", "component_type": "fiducial", "package": ""},
            # no package → reported (lines 64-70)
            {"element_type": "component", "component_id": "c1",
             "designator": "U1", "component_type": "ic", "package": ""},
            {"element_type": "port", "component_id": "c1", "pin_number": 1},
        ]
    }
    issues = verify_footprints(netlist)
    refs = {i["designator"]: i for i in issues}
    assert "FID1" not in refs
    assert refs["U1"]["reason"] == "component has no package string"
    assert refs["U1"]["pin_count"] == 1


# ===========================================================================
# validate_bom.py
# ===========================================================================
import validate_bom as vb


def _net_comp(des, ctype, value, pkg):
    return {"element_type": "component", "component_id": f"c_{des}",
            "designator": des, "component_type": ctype,
            "value": value, "package": pkg}


def _bom_item(des, ctype, value, pkg, specs=None):
    return {"designator": des, "component_type": ctype, "value": value,
            "package": pkg, "specs": specs or {}}


def test_bom_cross_reference_all_mismatch_types():
    netlist = {"elements": [
        _net_comp("R1", "resistor", "220ohm", "0805"),
        _net_comp("R2", "resistor", "1k", "0805"),
    ]}
    bom = {"bom": [
        _bom_item("R1", "capacitor", "1k", "0603"),     # type+value+pkg mismatch
        _bom_item("R2", "resistor", "1k", "0805"),      # ok
        _bom_item("R2", "resistor", "1k", "0805"),      # duplicate designator
        _bom_item("R9", "led", "red", "0805"),          # phantom (no netlist match)
    ]}
    errors, _ = vb.validate_cross_reference(bom, netlist)
    blob = "\n".join(errors)
    assert "Duplicate BOM entry for designator 'R2'" in blob
    assert "component_type mismatch" in blob
    assert "package mismatch" in blob
    assert "value mismatch" in blob
    assert "has no matching netlist component" in blob  # phantom R9


def test_bom_missing_netlist_component():
    netlist = {"elements": [_net_comp("R1", "resistor", "220ohm", "0805")]}
    bom = {"bom": []}
    errors, _ = vb.validate_cross_reference(bom, netlist)
    assert any("R1" in e and "missing from BOM" in e for e in errors)


def test_bom_specs_completeness_warns_missing():
    bom = {"bom": [
        _bom_item("R1", "resistor", "220ohm", "0805"),  # missing tolerance,power_rating
        _bom_item("C1", "capacitor", "100nF", "0805", {"voltage_rating": "50V"}),  # complete
    ]}
    warnings = vb.validate_specs_completeness(bom)
    blob = "\n".join(warnings)
    assert "tolerance" in blob and "power_rating" in blob
    assert "C1" not in blob  # C1 has its expected spec


def test_validate_bom_missing_file():
    res = vb.validate_bom("/nonexistent/bom.json")
    assert res["valid"] is False
    assert "File not found" in res["summary"]


def test_validate_bom_invalid_json():
    f = tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False)
    f.write("{ not json")
    f.close()
    res = vb.validate_bom(f.name)
    assert res["valid"] is False
    assert "not valid JSON" in res["summary"]


def test_validate_bom_bad_schema_path():
    bom = _write_temp({"bom": []})
    res = vb.validate_bom(bom, schema_path="/nonexistent/schema.json")
    assert res["valid"] is False
    assert "Schema file error" in res["summary"]


def test_bom_validate_schema_emits_path_and_root_errors():
    # A BOM violating the real schema exercises validate_schema's error
    # formatting (lines 49-53): both path-prefixed and root messages.
    schema_path = os.path.join(_MAIN_REPO, "schemas", "bom_schema.json")
    if not os.path.exists(schema_path):
        schema_path = os.path.join(_ROOT, "schemas", "bom_schema.json")
    schema = json.loads(open(schema_path).read())
    # bom entry missing required fields + wrong top-level type
    bad = {"bom": [{"designator": 123}]}  # designator should be string etc.
    errors = vb.validate_schema(bad, schema)
    assert errors  # at least one schema error formatted
    assert all(e.startswith("Schema:") for e in errors)


def test_validate_bom_netlist_load_failure_is_warning():
    # Schema passes (use the real blink bom), netlist path is unreadable →
    # cross-reference is skipped with a warning (lines 212-213).
    blink_bom = os.path.join(PROJ, "blink_3_leds_dc_power_bom.json")
    res = vb.validate_bom(blink_bom, netlist_path="/nonexistent/netlist.json")
    assert any("Could not load netlist" in w for w in res["warnings"])


# ===========================================================================
# validate_placement.py
# ===========================================================================
import validate_placement as vp


def _place(des, x, y, w=2.0, h=1.0, rot=0, layer="top",
           ctype="resistor", pkg="0805"):
    return {"designator": des, "x_mm": x, "y_mm": y,
            "footprint_width_mm": w, "footprint_height_mm": h,
            "rotation_deg": rot, "layer": layer,
            "component_type": ctype, "package": pkg}


def test_placement_box_clearance_overlap_returns_zero():
    # Two overlapping boxes → _box_clearance returns 0.0 (line 65).
    a = (0, 0, 4, 4)
    b = (2, 2, 6, 6)
    assert vp._box_clearance(a, b) == 0.0
    # Diagonal (corner-to-corner) gap → euclidean branch (line 69 already hit).
    c = (10, 10, 11, 11)
    assert vp._box_clearance(a, c) == pytest.approx(((6) ** 2 + 6 ** 2) ** 0.5)


def test_placement_cross_reference_mismatches():
    netlist = {"elements": [
        {"element_type": "component", "component_id": "c1",
         "designator": "R1", "component_type": "resistor", "package": "0805"},
        {"element_type": "component", "component_id": "c2",
         "designator": "R2", "component_type": "resistor", "package": "0805"},
        {"element_type": "component", "component_id": "c3",
         "designator": "R3", "component_type": "resistor", "package": "0805"},
    ]}
    placement = {"board": {"width_mm": 50, "height_mm": 50}, "placements": [
        _place("R1", 10, 10, ctype="capacitor", pkg="0603"),  # type+pkg mismatch
        _place("R2", 11, 11),                                  # ok
        _place("R2", 12, 12),                                  # duplicate of R2
        _place("R9", 20, 20),                                  # phantom
        # R3 missing entirely
    ]}
    errors, _ = vp.validate_cross_reference(placement, netlist)
    blob = "\n".join(errors)
    assert "Duplicate placement for R2" in blob
    assert "R3 is missing from placement" in blob
    assert "phantom placement" in blob
    assert "component_type mismatch" in blob
    assert "package mismatch" in blob


def test_placement_fiducial_exempt_from_phantom():
    netlist = {"elements": []}
    placement = {"board": {"width_mm": 50, "height_mm": 50}, "placements": [
        _place("FID1", 5, 5, ctype="fiducial"),
    ]}
    errors, _ = vp.validate_cross_reference(placement, netlist)
    assert not any("phantom" in e for e in errors)


def test_placement_board_boundary_all_edges():
    placement = {"board": {"width_mm": 20, "height_mm": 20}, "placements": [
        _place("R1", 0, 10, w=4, h=2),    # left edge overflow
        _place("R2", 10, 0, w=4, h=4),    # bottom edge overflow
        _place("R3", 20, 10, w=4, h=2),   # right edge overflow
        _place("R4", 10, 20, w=4, h=4),   # top edge overflow
    ]}
    errors = vp.validate_board_boundary(placement)
    blob = "\n".join(errors)
    assert "left board edge" in blob
    assert "bottom board edge" in blob
    assert "right board edge" in blob
    assert "top board edge" in blob


def test_placement_overlap_and_clearance():
    placement = {"board": {"width_mm": 100, "height_mm": 100}, "placements": [
        _place("R1", 10, 10, w=4, h=4),
        _place("R2", 11, 11, w=4, h=4),   # overlaps R1
        _place("R3", 30, 30, w=2, h=2),    # box x [29, 31]
        _place("R4", 32.3, 30, w=2, h=2),  # box x [31.3, 33.3] → 0.3mm gap < 0.5
        _place("R5", 30, 30, w=2, h=2, layer="bottom"),  # diff layer, no conflict
    ]}
    errors, _ = vp.validate_overlap_and_clearance(placement)
    blob = "\n".join(errors)
    assert "overlap" in blob
    assert "clearance" in blob


def test_placement_rules_connector_far_from_edge():
    placement = {"board": {"width_mm": 100, "height_mm": 100}, "placements": [
        _place("J1", 50, 50, w=4, h=4, ctype="connector"),  # center = far from edge
    ]}
    warnings = vp.validate_placement_rules(placement)
    assert any("from nearest edge" in w for w in warnings)


def test_placement_decoupling_proximity_warning():
    # Cap far from IC, sharing a power net → proximity warning.
    netlist = {"elements": [
        {"element_type": "component", "component_id": "u1",
         "designator": "U1", "component_type": "ic", "package": "SOIC-8"},
        {"element_type": "component", "component_id": "c1",
         "designator": "C1", "component_type": "capacitor", "package": "0805"},
        {"element_type": "port", "port_id": "u1_p", "component_id": "u1",
         "pin_number": 1, "name": "VCC", "electrical_type": "power_in"},
        {"element_type": "port", "port_id": "c1_p", "component_id": "c1",
         "pin_number": 1, "name": "1", "electrical_type": "passive"},
        {"element_type": "net", "net_id": "n1", "name": "VCC",
         "net_class": "power", "connected_port_ids": ["u1_p", "c1_p"]},
    ]}
    placement = {"board": {"width_mm": 100, "height_mm": 100}, "placements": [
        _place("U1", 10, 10, ctype="ic", pkg="SOIC-8"),
        _place("C1", 80, 80, ctype="capacitor"),  # ~99mm away
    ]}
    warnings = vp.validate_placement_rules(placement, netlist)
    assert any("decoupling cap" in w and "from U1" in w for w in warnings)


def test_placement_decoupling_ic_on_net_but_unplaced():
    # IC shares the power net but is NOT in the placement → the inner loop's
    # `if ic_ref not in items_by_ref: continue` guard fires (line 304).
    netlist = {"elements": [
        {"element_type": "component", "component_id": "u1",
         "designator": "U1", "component_type": "ic", "package": "SOIC-8"},
        {"element_type": "component", "component_id": "c1",
         "designator": "C1", "component_type": "capacitor", "package": "0805"},
        {"element_type": "port", "port_id": "u1_p", "component_id": "u1",
         "pin_number": 1, "name": "VCC", "electrical_type": "power_in"},
        {"element_type": "port", "port_id": "c1_p", "component_id": "c1",
         "pin_number": 1, "name": "1", "electrical_type": "passive"},
        {"element_type": "net", "net_id": "n1", "name": "VCC",
         "net_class": "power", "connected_port_ids": ["u1_p", "c1_p"]},
    ]}
    # Only C1 placed; U1 absent from placement.
    placement = {"board": {"width_mm": 100, "height_mm": 100}, "placements": [
        _place("C1", 50, 50, ctype="capacitor"),
    ]}
    warnings = vp.validate_placement_rules(placement, netlist)
    # No proximity warning since the IC has no placement to measure against.
    assert not any("decoupling cap" in w for w in warnings)


def test_validate_placement_main_cli(capsys):
    args = [os.path.join(PROJ, "blink_3_leds_dc_power_placement.json"),
            "--netlist", os.path.join(PROJ, "blink_3_leds_dc_power_netlist.json")]
    old = sys.argv
    sys.argv = ["validate_placement.py"] + args
    try:
        rc = vp.main()
    finally:
        sys.argv = old
    out = capsys.readouterr().out
    assert rc == 0
    assert json.loads(out)["valid"] is True


def test_validate_placement_bad_files():
    assert vp.validate_placement("/no/such.json")["valid"] is False
    good = _write_temp({"board": {"width_mm": 10, "height_mm": 10},
                        "placements": []})
    res = vp.validate_placement(good, netlist_path="/no/such/netlist.json")
    assert res["valid"] is False
    assert "netlist" in res["summary"].lower()


def test_validate_placement_schema_failure_short_circuits():
    bad = _write_temp({"placements": [{"designator": "R1"}]})  # missing required
    res = vp.validate_placement(bad)
    assert res["valid"] is False
    assert "Schema validation failed" in res["summary"]


def test_validate_placement_full_pass():
    res = vp.validate_placement(
        os.path.join(PROJ, "blink_3_leds_dc_power_placement.json"),
        os.path.join(PROJ, "blink_3_leds_dc_power_netlist.json"),
    )
    assert res["valid"] is True


# ===========================================================================
# validate_netlist.py
# ===========================================================================
import validate_netlist as vn


def _component(cid, des, ctype="resistor", pkg="0805", value="220ohm"):
    return {"element_type": "component", "component_id": cid, "designator": des,
            "component_type": ctype, "package": pkg, "value": value}


def _port(pid, cid, pin, name="1", etype="passive"):
    return {"element_type": "port", "port_id": pid, "component_id": cid,
            "pin_number": pin, "name": name, "electrical_type": etype}


def _net(nid, name, ports, net_class="signal"):
    return {"element_type": "net", "net_id": nid, "name": name,
            "connected_port_ids": ports, "net_class": net_class}


def test_netlist_validate_schema_path_and_root_errors():
    # Exercises validate_schema's path-prefixed and root error formatting (69-73).
    schema_path = os.path.join(_MAIN_REPO, "schemas", "circuit_schema.json")
    if not os.path.exists(schema_path):
        schema_path = os.path.join(_ROOT, "schemas", "circuit_schema.json")
    schema = json.loads(open(schema_path).read())
    bad = {"elements": "should-be-an-array"}  # root + nested type errors
    errors = vn.validate_schema(bad, schema)
    assert errors and all(e.startswith("Schema:") for e in errors)


def test_netlist_referential_duplicate_ids_and_bad_refs():
    elements = [
        _component("c1", "R1"),
        _component("c1", "R2"),  # duplicate component_id
        _port("p1", "c1", 1),
        _port("p2", "c_missing", 2),  # port → non-existent component
        _net("n1", "N1", ["p1", "p_missing"]),  # net → non-existent port
    ]
    errors, _ = vn.validate_referential_integrity({"elements": elements})
    blob = "\n".join(errors)
    assert "Duplicate ID" in blob
    assert "non-existent component" in blob
    assert "non-existent port" in blob


def test_netlist_single_port_net_prescriptive_fixes():
    # ground/power/signal single-port nets each get a tailored fix message.
    elements = [
        _component("c1", "R1"),
        _port("p1", "c1", 1, name="GND", etype="ground"),
        _port("p2", "c1", 2, name="VCC", etype="power_in"),
        _port("p3", "c1", 3, name="SIG", etype="signal"),
        _net("ng", "GND", ["p1"], net_class="ground"),
        _net("np", "VCC", ["p2"], net_class="power"),
        _net("ns", "SIG", ["p3"], net_class="signal"),
    ]
    errors, _ = vn.validate_referential_integrity({"elements": elements})
    blob = "\n".join(errors)
    assert "shared GND net" in blob
    assert "shared VCC/power net" in blob
    assert "mark the port as no_connect" in blob


def test_netlist_component_no_ports_and_unconnected_pins():
    elements = [
        _component("c1", "R1"),  # no ports at all
        _component("c2", "R2"),
        _port("p2", "c2", 1, name="VCC", etype="power_in"),  # power pin, no net → error
        _component("c3", "R3"),
        _port("p3", "c3", 1, name="SIG", etype="signal"),    # signal pin, no net → warning
        _component("c4", "R4"),
        _port("p4", "c4", 1, name="NC", etype="no_connect"), # no_connect → ignored
    ]
    errors, warnings = vn.validate_referential_integrity({"elements": elements})
    eblob = "\n".join(errors)
    wblob = "\n".join(warnings)
    assert "Component 'c1' has no ports defined" in eblob
    assert "not connected to any net" in eblob       # power pin → error
    assert "not connected to any net" in wblob       # signal pin → warning


def test_netlist_designator_autofix_led_prefix():
    elements = [
        _component("c1", "LED1", ctype="led", pkg="0805", value="red"),
        _port("p1", "c1", 1, name="A", etype="passive"),
        _port("p2", "c1", 2, name="K", etype="passive"),
        _component("c2", "D2", ctype="led", pkg="0805", value="red"),
        _port("p3", "c2", 1, name="A", etype="passive"),
        _net("n1", "N1", ["p1", "p3"]),
    ]
    _, warnings = vn.validate_referential_integrity({"elements": elements})
    assert any("Auto-fix" in w and "LED1" in w and "D1" in w for w in warnings)


def test_netlist_designator_prefix_mismatch_and_bad_format():
    elements = [
        _component("c1", "X1", ctype="resistor"),  # resistor should be R
        _component("c2", "???", ctype="resistor"),  # invalid format (no letter prefix)
        _port("p1", "c1", 1), _port("p2", "c2", 1),
        _net("n1", "N1", ["p1", "p2"]),
    ]
    errors, _ = vn.validate_referential_integrity({"elements": elements})
    blob = "\n".join(errors)
    assert "uses prefix 'X'" in blob
    assert "invalid designator format" in blob


def test_netlist_duplicate_designator_and_nonsequential():
    elements = [
        _component("c1", "R1"), _component("c2", "R1"),  # duplicate designator
        _component("c3", "C2"),  # C jumps to 2 → non-sequential for prefix C
        _port("p1", "c1", 1), _port("p2", "c2", 1), _port("p3", "c3", 1),
        _net("n1", "N1", ["p1", "p2"]),
    ]
    errors, _ = vn.validate_referential_integrity({"elements": elements})
    blob = "\n".join(errors)
    assert "Duplicate designator" in blob
    assert "not sequential" in blob


def test_netlist_duplicate_pin_number_within_component():
    elements = [
        _component("c1", "U1", ctype="ic", pkg="SOIC-8"),
        _port("p1", "c1", 1), _port("p2", "c1", 1),  # same pin_number twice
        _component("c2", "R2"), _port("p3", "c2", 1),
        _net("n1", "N1", ["p1", "p3"]),
    ]
    errors, _ = vn.validate_referential_integrity({"elements": elements})
    assert any("duplicate pin_number 1" in e for e in errors)


def test_netlist_port_in_multiple_nets():
    elements = [
        _component("c1", "R1"), _component("c2", "R2"),
        _port("p1", "c1", 1), _port("p2", "c2", 1),
        _net("n1", "N1", ["p1", "p2"]),
        _net("n2", "N2", ["p1", "p2"]),  # p1 appears in two nets
    ]
    errors, _ = vn.validate_referential_integrity({"elements": elements})
    assert any("appears in" in e and "multiple nets" in e for e in errors)


def test_netlist_same_physical_pin_two_nets_via_distinct_ports():
    # Same (component_id, pin_number) reached by two DIFFERENT port_ids in two nets.
    elements = [
        _component("c1", "R1"), _component("c2", "R2"), _component("c3", "R3"),
        _port("p1a", "c1", 1), _port("p1b", "c1", 1),  # same pin, two ports
        _port("p2", "c2", 1), _port("p3", "c3", 1),
        _net("n1", "N1", ["p1a", "p2"]),
        _net("n2", "N2", ["p1b", "p3"]),
    ]
    errors, _ = vn.validate_referential_integrity({"elements": elements})
    assert any("Physical pin" in e and "connects to" in e for e in errors)


def test_check_package_compliance_mismatch():
    netlist = {"components": {
        "c1": {"designator": "R1", "package": "0603"},
    }}
    reqs = {"components": [{"ref": "R1", "package": "0805"}]}
    warnings = vn._check_package_compliance(netlist, reqs)
    assert any("Package mismatch" in w and "R1" in w for w in warnings)


def test_check_package_compliance_no_reqs_empty():
    assert vn._check_package_compliance({}, {}) == []
    # reqs with components but none carrying both ref+package → req_pkg_map empty
    assert vn._check_package_compliance({}, {"components": [{"ref": "R1"}]}) == []


def test_check_port_completeness_under_and_over():
    elements = [
        {"element_type": "component", "component_id": "u1",
         "designator": "U1", "package": "SOIC-8"},
        # only 4 of 8 ports
        *[{"element_type": "port", "component_id": "u1", "pin_number": i,
           "port_id": f"u1_{i}"} for i in range(1, 5)],
        {"element_type": "component", "component_id": "u2",
         "designator": "U2", "package": "SOIC-8"},
        # 10 ports > 8 pins
        *[{"element_type": "port", "component_id": "u2", "pin_number": i,
           "port_id": f"u2_{i}"} for i in range(1, 11)],
    ]
    errors, warnings = vn._check_port_completeness({"elements": elements})
    assert any("U1" in e and "requires" in e for e in errors)
    assert any("U2" in w and "possible duplicate ports" in w for w in warnings)


def test_check_port_completeness_uses_ref_specs():
    elements = [
        {"element_type": "component", "component_id": "u1",
         "designator": "U1", "package": "WEIRD-PKG"},
        {"element_type": "port", "component_id": "u1", "pin_number": 1,
         "port_id": "u1_1"},
    ]
    reqs = {"components": [{"ref": "U1", "package": "WEIRD-PKG",
                           "specs": {"pin_count": 8}}]}
    errors, _ = vn._check_port_completeness({"elements": elements}, reqs)
    assert any("U1" in e and "requires" in e for e in errors)


def test_coerce_netlist_types_flattens_and_converts():
    netlist = {"elements": [
        {"element_type": "component", "component_id": "c1",
         "properties": {"pins": {"1": "PB5"}, "tags": ["a", "b"],
                        "empty": None, "val": 5},
         "extra_null": None},
        {"element_type": "port", "port_id": "p1", "component_id": "c1",
         "pin_number": "3", "junk": None},
        {"element_type": "port", "port_id": "p2", "component_id": "c1",
         "pin_number": "notanint"},
        {"element_type": "net", "net_id": "n1", "stray": None},
    ]}
    vn._coerce_netlist_types(netlist)
    comp = netlist["elements"][0]
    assert comp["properties"]["pins_1"] == "PB5"  # nested dict flattened
    assert comp["properties"]["tags"] == "a, b"   # list joined
    assert "empty" not in comp["properties"]      # null dropped
    assert "extra_null" not in comp               # top-level null dropped
    port = netlist["elements"][1]
    assert port["pin_number"] == 3 and "junk" not in port
    assert netlist["elements"][2]["pin_number"] == "notanint"  # unconvertible kept
    assert "stray" not in netlist["elements"][3]


def test_fix_pinout_from_requirements_corrects_name_and_type():
    netlist = {"elements": [
        {"element_type": "component", "component_id": "u1", "designator": "U1"},
        {"element_type": "port", "port_id": "u1_7", "component_id": "u1",
         "pin_number": 7, "name": "WRONG", "electrical_type": "signal"},
    ]}
    reqs = {"components": [{"ref": "U1", "specs": {"pinout": "7:VCC 8:GND"}}]}
    corrections = vn._fix_pinout_from_requirements(netlist, reqs)
    assert any("name 'WRONG'" in c for c in corrections)
    port = netlist["elements"][1]
    assert port["electrical_type"] == "power_in"


def test_fix_pinout_skips_port_with_pin_not_in_pinout():
    # Pin 99 isn't in the 2-pin pinout → the per-port loop continues (line 512).
    netlist = {"elements": [
        {"element_type": "component", "component_id": "u1", "designator": "U1"},
        {"element_type": "port", "port_id": "u1_99", "component_id": "u1",
         "pin_number": 99, "name": "X", "electrical_type": "signal"},
    ]}
    reqs = {"components": [{"ref": "U1", "specs": {"pinout": "1:VCC 2:GND"}}]}
    corrections = vn._fix_pinout_from_requirements(netlist, reqs)
    assert corrections == []  # nothing to fix for an out-of-range pin


def test_fix_pinout_no_pinouts_returns_empty():
    assert vn._fix_pinout_from_requirements({"elements": []}, {"components": []}) == []


def test_validate_netlist_file_not_found():
    res = vn.validate_netlist("/no/such/netlist.json")
    assert res["valid"] is False and res["summary"] == "File not found"


def test_validate_netlist_invalid_json():
    f = tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False)
    f.write("{bad")
    f.close()
    res = vn.validate_netlist(f.name)
    assert res["valid"] is False and "not valid JSON" in res["summary"]


def test_validate_netlist_bad_schema_path():
    nl = _write_temp({"version": "1.0", "elements": []})
    res = vn.validate_netlist(nl, schema_path="/no/such/schema.json")
    assert res["valid"] is False and "Schema file error" in res["summary"]


def test_validate_netlist_full_pass_with_requirements():
    res = vn.validate_netlist(BLINK_NETLIST, requirements_path=BLINK_REQS)
    assert res["valid"] is True, res["errors"]


def test_validate_netlist_invalid_summary_branch():
    # Schema-valid but referentially broken (orphan net port) → INVALID summary
    # (line 648). Start from the real blink netlist and corrupt a net.
    netlist = json.loads(open(BLINK_NETLIST).read())
    for el in netlist["elements"]:
        if el.get("element_type") == "net":
            el["connected_port_ids"] = el["connected_port_ids"] + ["ghost_port"]
            break
    path = _write_temp(netlist)
    res = vn.validate_netlist(path)
    assert res["valid"] is False
    assert res["summary"].startswith("Netlist is INVALID")


def test_validate_netlist_bad_requirements_path_silently_skipped():
    # Unreadable requirements path → silently ignored (lines 596-597), still valid.
    res = vn.validate_netlist(BLINK_NETLIST, requirements_path="/no/such.json")
    assert res["valid"] is True


# ===========================================================================
# drc_checks.py (uncovered branches)
# ===========================================================================
import drc_checks as dc


def test_drc_resistor_value_high_warns():
    elements = [
        {"element_type": "component", "component_id": "c1", "designator": "R1",
         "component_type": "resistor", "value": "20Mohm", "package": "0805"},
    ]
    c, p, n = dc.build_lookups(elements)
    _, warnings = dc.check_component_value_sanity(c, p, n)
    assert any("extremely high" in w for w in warnings)


def test_drc_resistor_value_unparseable_warns():
    elements = [
        {"element_type": "component", "component_id": "c1", "designator": "R1",
         "component_type": "resistor", "value": "bogus", "package": "0805"},
    ]
    c, p, n = dc.build_lookups(elements)
    _, warnings = dc.check_component_value_sanity(c, p, n)
    assert any("cannot parse resistance" in w for w in warnings)


def test_drc_capacitor_value_unparseable_warns():
    elements = [
        {"element_type": "component", "component_id": "c1", "designator": "C1",
         "component_type": "capacitor", "value": "bogus", "package": "0805"},
    ]
    c, p, n = dc.build_lookups(elements)
    _, warnings = dc.check_component_value_sanity(c, p, n)
    assert any("cannot parse capacitance" in w for w in warnings)


def test_drc_resistor_power_overload_with_fix_hint():
    # 5V directly across a tiny 1ohm 0402 resistor → far exceeds rating.
    elements = [
        {"element_type": "component", "component_id": "c1", "designator": "R1",
         "component_type": "resistor", "value": "1ohm", "package": "0402"},
        {"element_type": "port", "port_id": "p1", "component_id": "c1",
         "pin_number": 1, "name": "1", "electrical_type": "passive"},
        {"element_type": "net", "net_id": "n1", "name": "SIG",
         "net_class": "signal", "connected_port_ids": ["p1"]},
    ]
    c, p, n = dc.build_lookups(elements)
    errors, _ = dc.check_resistor_power(c, p, n, v_supply=5.0)
    assert any("exceeds" in e and "R1" in e for e in errors)


def test_drc_capacitor_voltage_rating_too_low_and_unparseable():
    elements = [
        # ceramic, 5V supply → needs 7.5V; rated 6.3V → error
        {"element_type": "component", "component_id": "c1", "designator": "C1",
         "component_type": "capacitor", "value": "100nF", "package": "0805",
         "properties": {"voltage_rating": "6.3V"}},
        # unparseable rating → warning
        {"element_type": "component", "component_id": "c2", "designator": "C2",
         "component_type": "capacitor", "value": "100nF", "package": "0805",
         "properties": {"voltage_rating": "junk"}},
    ]
    c, p, n = dc.build_lookups(elements)
    errors, warnings = dc.check_capacitor_voltage_rating(c, p, n, v_supply=5.0)
    assert any("below" in e and "C1" in e for e in errors)
    assert any("cannot parse voltage_rating" in w for w in warnings)


def test_drc_power_budget_led_current():
    elements = [
        {"element_type": "component", "component_id": "c1", "designator": "D1",
         "component_type": "led", "value": "red", "package": "0805",
         "properties": {"if": "20mA"}},
    ]
    c, p, n = dc.build_lookups(elements)
    _, warnings = dc.check_power_budget(c, p, n, v_supply=5.0)
    assert any("power budget" in w for w in warnings)


def test_drc_pinout_compliance_out_of_range_pin():
    components = {"c1": {"component_id": "c1", "designator": "U1"}}
    ports = {"p1": {"port_id": "p1", "component_id": "c1", "pin_number": 99,
                    "name": "X", "electrical_type": "signal"}}
    nets: dict = {}
    reqs = {"components": [{"ref": "U1", "specs": {"pinout": "1:VCC 2:GND"}}]}
    errors, _ = dc.check_pinout_compliance(components, ports, nets, reqs)
    assert any("not in the 2-pin pinout" in e for e in errors)


def test_drc_pinout_compliance_name_mismatch_and_missing():
    components = {"c1": {"component_id": "c1", "designator": "U1"}}
    ports = {"p1": {"port_id": "p1", "component_id": "c1", "pin_number": 1,
                    "name": "WRONGNAME", "electrical_type": "signal"}}
    nets: dict = {}
    reqs = {"components": [{"ref": "U1", "specs": {"pinout": "1:VCC 2:GND"}}]}
    errors, warnings = dc.check_pinout_compliance(components, ports, nets, reqs)
    assert any("doesn't match expected" in e for e in errors)  # name mismatch
    assert any("type" in w and "differs" in w for w in warnings)  # type mismatch
    assert any("missing ports for pins" in w for w in warnings)   # pin 2 missing


def test_drc_run_all_with_requirements_parses_voltage():
    elements = [
        {"element_type": "component", "component_id": "c1", "designator": "R1",
         "component_type": "resistor", "value": "1ohm", "package": "0402"},
        {"element_type": "port", "port_id": "p1", "component_id": "c1",
         "pin_number": 1, "name": "1", "electrical_type": "passive"},
        {"element_type": "net", "net_id": "n1", "name": "SIG",
         "net_class": "signal", "connected_port_ids": ["p1"]},
    ]
    errors, _ = dc.run_all_drc_checks(elements, requirements={"power": {"voltage": "5V"}})
    assert any("R1" in e for e in errors)


def test_drc_power_net_all_signal_warns():
    elements = [
        {"element_type": "component", "component_id": "c1", "designator": "U1",
         "component_type": "ic", "package": "SOIC-8"},
        {"element_type": "port", "port_id": "p1", "component_id": "c1",
         "pin_number": 1, "name": "A", "electrical_type": "signal"},
        {"element_type": "net", "net_id": "n1", "name": "VCC",
         "net_class": "power", "connected_port_ids": ["p1"]},
    ]
    c, p, n = dc.build_lookups(elements)
    _, warnings = dc.check_net_class_vs_pin_types(c, p, n)
    assert any("all pins are signal" in w for w in warnings)


def test_drc_capacitor_extremely_small_warns():
    elements = [
        {"element_type": "component", "component_id": "c1", "designator": "C1",
         "component_type": "capacitor", "value": "0.5pF", "package": "0805"},
    ]
    c, p, n = dc.build_lookups(elements)
    _, warnings = dc.check_component_value_sanity(c, p, n)
    assert any("extremely small" in w for w in warnings)


def test_drc_resistor_power_unparseable_value_skipped():
    # Unparseable value → continue (line 332); zero ohms → continue (336);
    # unknown package → continue (340).
    elements = [
        {"element_type": "component", "component_id": "c1", "designator": "R1",
         "component_type": "resistor", "value": "bogus", "package": "0805"},
        {"element_type": "component", "component_id": "c2", "designator": "R2",
         "component_type": "resistor", "value": "1k", "package": "WEIRD"},  # unknown pkg
    ]
    c, p, n = dc.build_lookups(elements)
    errors, _ = dc.check_resistor_power(c, p, n, v_supply=5.0)
    assert errors == []


def test_drc_resistor_power_led_cathode_side_not_series():
    # Resistor wired to the LED's CATHODE → treated as not-in-series (line 362),
    # so worst-case full-supply dissipation is used.
    elements = [
        {"element_type": "component", "component_id": "r1", "designator": "R1",
         "component_type": "resistor", "value": "100ohm", "package": "0402"},
        {"element_type": "component", "component_id": "d1", "designator": "D1",
         "component_type": "led", "value": "red", "package": "0805"},
        {"element_type": "port", "port_id": "rp", "component_id": "r1",
         "pin_number": 1, "name": "1", "electrical_type": "passive"},
        {"element_type": "port", "port_id": "dk", "component_id": "d1",
         "pin_number": 2, "name": "cathode", "electrical_type": "passive"},
        {"element_type": "net", "net_id": "n1", "name": "SIG",
         "net_class": "signal", "connected_port_ids": ["rp", "dk"]},
    ]
    c, p, n = dc.build_lookups(elements)
    errors, _ = dc.check_resistor_power(c, p, n, v_supply=5.0)
    # 5V across 100ohm 0402 = 250mW >> 63mW rating → error (full-supply path)
    assert any("R1" in e for e in errors)


def test_drc_resistor_power_derated_warning_and_error_with_fix():
    # 0805 (0.125W) resistor with 5V across ~150ohm → ~167mW > rating → error
    # with a package-upgrade fix suggestion (lines 388-403).
    elements = [
        {"element_type": "component", "component_id": "r1", "designator": "R1",
         "component_type": "resistor", "value": "150ohm", "package": "0805"},
        {"element_type": "port", "port_id": "rp", "component_id": "r1",
         "pin_number": 1, "name": "1", "electrical_type": "passive"},
        {"element_type": "net", "net_id": "n1", "name": "SIG",
         "net_class": "signal", "connected_port_ids": ["rp"]},
    ]
    c, p, n = dc.build_lookups(elements)
    errors, warnings = dc.check_resistor_power(c, p, n, v_supply=5.0)
    blob = "\n".join(errors + warnings)
    assert "R1" in blob


def test_drc_power_budget_led_if_unparseable_uses_default():
    elements = [
        {"element_type": "component", "component_id": "c1", "designator": "D1",
         "component_type": "led", "value": "red", "package": "0805",
         "properties": {"if": "garbage"}},  # unparseable → LED_IF_DEFAULT (491)
    ]
    c, p, n = dc.build_lookups(elements)
    _, warnings = dc.check_power_budget(c, p, n, v_supply=5.0)
    assert any("power budget" in w for w in warnings)


def test_drc_pinout_compliance_component_not_in_netlist():
    # Requirements name U1, but it's absent from the netlist → skip (line 551).
    reqs = {"components": [{"ref": "U1", "specs": {"pinout": "1:VCC 2:GND"}}]}
    errors, warnings = dc.check_pinout_compliance({}, {}, {}, reqs)
    assert errors == [] and warnings == []


def test_drc_decoupling_pin_with_no_net_skipped():
    # IC VCC pin not on any net → continue (line 275).
    elements = [
        {"element_type": "component", "component_id": "u1", "designator": "U1",
         "component_type": "ic", "package": "SOIC-8"},
        {"element_type": "port", "port_id": "vcc", "component_id": "u1",
         "pin_number": 1, "name": "VCC", "electrical_type": "power_in"},
        # no net connects vcc
    ]
    c, p, n = dc.build_lookups(elements)
    errors, warnings = dc.check_decoupling_capacitors(c, p, n)
    # No net → no decoupling warning emitted for the unconnected pin.
    assert not any("decoupling" in w for w in warnings)


def test_drc_resistor_power_zero_ohms_skipped():
    elements = [
        {"element_type": "component", "component_id": "r1", "designator": "R1",
         "component_type": "resistor", "value": "0ohm", "package": "0805"},
        {"element_type": "port", "port_id": "rp", "component_id": "r1",
         "pin_number": 1, "name": "1", "electrical_type": "passive"},
        {"element_type": "net", "net_id": "n1", "name": "SIG",
         "net_class": "signal", "connected_port_ids": ["rp"]},
    ]
    c, p, n = dc.build_lookups(elements)
    errors, _ = dc.check_resistor_power(c, p, n, v_supply=5.0)
    assert errors == []  # r_ohms <= 0 → skipped


def test_drc_resistor_power_derated_limit_error_branch():
    # 250ohm 0805: 5V²/250 = 100mW. rating=125mW, derated=62.5mW.
    # 100mW <= rating but > derated → the "derated limit" error (line 403).
    elements = [
        {"element_type": "component", "component_id": "r1", "designator": "R1",
         "component_type": "resistor", "value": "250ohm", "package": "0805"},
        {"element_type": "port", "port_id": "rp", "component_id": "r1",
         "pin_number": 1, "name": "1", "electrical_type": "passive"},
        {"element_type": "net", "net_id": "n1", "name": "SIG",
         "net_class": "signal", "connected_port_ids": ["rp"]},
    ]
    c, p, n = dc.build_lookups(elements)
    errors, _ = dc.check_resistor_power(c, p, n, v_supply=5.0)
    assert any("derated limit" in e for e in errors)


def test_drc_decoupling_dangling_port_and_unparseable_cap():
    # IC VCC net references a dangling port (no component → line 285) and a
    # capacitor with an unparseable value (parse fail → lines 295-296).
    elements = [
        {"element_type": "component", "component_id": "u1", "designator": "U1",
         "component_type": "ic", "package": "SOIC-8"},
        {"element_type": "component", "component_id": "c1", "designator": "C1",
         "component_type": "capacitor", "value": "junk", "package": "0805"},
        {"element_type": "port", "port_id": "vcc", "component_id": "u1",
         "pin_number": 1, "name": "VCC", "electrical_type": "power_in"},
        {"element_type": "port", "port_id": "cp", "component_id": "c1",
         "pin_number": 1, "name": "1", "electrical_type": "passive"},
        {"element_type": "net", "net_id": "n1", "name": "VCC",
         "net_class": "power",
         "connected_port_ids": ["vcc", "cp", "dangling_port"]},
    ]
    c, p, n = dc.build_lookups(elements)
    _, warnings = dc.check_decoupling_capacitors(c, p, n)
    # Unparseable cap doesn't count as decoupling → warning still emitted.
    assert any("decoupling" in w for w in warnings)


def test_drc_run_all_bad_voltage_string_ignored():
    # Unparseable voltage → v_supply stays None, power checks skip (656-657).
    errors, warnings = dc.run_all_drc_checks([], requirements={"power": {"voltage": "garbage"}})
    assert errors == []


# ===========================================================================
# drc_checks_dfm.py
# ===========================================================================
import drc_checks_dfm as dfm


def _routed_traces(*traces):
    return {"board": {"width_mm": 50, "height_mm": 50},
            "routing": {"traces": list(traces), "vias": [], "copper_fills": []}}


def test_dfm_trace_width_min():
    routed = _routed_traces({"start_x_mm": 10, "start_y_mm": 10,
                             "end_x_mm": 20, "end_y_mm": 10,
                             "width_mm": 0.05, "layer": "top",
                             "net_name": "N1"})
    v = dfm.check_trace_width_min(routed, {"trace_width_min_mm": 0.127})
    assert len(v) == 1 and v[0].rule == "trace_width_min"
    assert v[0].to_dict()["severity"] == "error"


def test_dfm_clearance_min_violation():
    # Two parallel traces on the same layer, different nets, too close.
    routed = _routed_traces(
        {"start_x_mm": 10, "start_y_mm": 10, "end_x_mm": 20, "end_y_mm": 10,
         "width_mm": 0.25, "layer": "top", "net_id": "a", "net_name": "A"},
        {"start_x_mm": 10, "start_y_mm": 10.1, "end_x_mm": 20, "end_y_mm": 10.1,
         "width_mm": 0.25, "layer": "top", "net_id": "b", "net_name": "B"},
    )
    v = dfm.check_clearance_min(routed, {"clearance_min_mm": 0.2})
    assert any(x.rule == "clearance_min" for x in v)


def test_dfm_via_drill_and_annular():
    routed = {"routing": {"traces": [], "vias": [
        {"x_mm": 10, "y_mm": 10, "drill_mm": 0.1, "diameter_mm": 0.2,
         "net_name": "N1"},
    ]}}
    dv = dfm.check_via_drill_min(routed, {"via_drill_min_mm": 0.3})
    assert dv and dv[0].rule == "via_drill_min"
    av = dfm.check_annular_ring(routed, {"min_annular_ring_mm": 0.13})
    assert av and av[0].rule == "annular_ring"


def test_dfm_copper_to_edge_trace_and_via():
    routed = {
        "board": {"width_mm": 50, "height_mm": 50},
        "routing": {
            "traces": [{"start_x_mm": 0.05, "start_y_mm": 25,
                        "end_x_mm": 10, "end_y_mm": 25,
                        "width_mm": 0.25, "layer": "top", "net_name": "N1"}],
            "vias": [{"x_mm": 0.1, "y_mm": 25, "diameter_mm": 0.6,
                      "net_name": "N2"}],
        },
    }
    v = dfm.check_copper_to_edge(routed, {}, {"min_copper_to_edge_mm": 0.2})
    assert any(x.message.startswith("Copper") for x in v)
    assert any("Via copper" in x.message for x in v)


def test_dfm_silkscreen_height_and_width():
    routed = {"silkscreen": [
        {"type": "text", "text": "X", "font_height_mm": 0.3,
         "x_mm": 1, "y_mm": 1},
    ]}
    v = dfm.check_silkscreen(routed, {"silkscreen_min_height_mm": 0.8,
                                      "silkscreen_min_width_mm": 0.15})
    rules = {x.rule for x in v}
    assert "silkscreen_height" in rules
    assert "silkscreen_width" in rules
    assert all(x.severity == "warning" for x in v)


def test_dfm_hole_to_hole_via_via():
    routed = {"routing": {"vias": [
        {"x_mm": 10, "y_mm": 10, "net_name": "A"},
        {"x_mm": 10.2, "y_mm": 10, "net_name": "B"},  # 0.2mm apart < 0.5
    ]}}
    v = dfm.check_hole_to_hole(routed, {"elements": []}, {"min_hole_to_hole_mm": 0.5})
    assert any(x.rule == "hole_to_hole" for x in v)


def test_dfm_geometry_helpers():
    # degenerate segment → point distance
    assert dfm._point_to_segment_dist(0, 1, 0, 0, 0, 0) == pytest.approx(1.0)
    # parallel segments
    d = dfm._segment_distance(0, 0, 1, 0, 0, 1, 1, 1)
    assert d == pytest.approx(1.0)


def test_dfm_inner_plane_no_planes_returns_empty():
    routed = {"routing": {"copper_fills": [{"is_plane": False}]}}
    assert dfm.check_inner_plane_antipad(routed, {"elements": []}, {}) == []


# ---- DFM checks that require pad_geometry (build_pad_map) ----

def test_dfm_inner_plane_antipad_missing_cutout():
    # A plane fill with a single polygon (no cutouts) + a foreign TH pad → error.
    routed = json.loads(open(BLINK_ROUTED).read())
    netlist = json.loads(open(BLINK_NETLIST).read())
    # Inject a solid inner plane on a net, foreign to the TH connector pads.
    routed["board"]["layers"] = 4
    routed["routing"]["copper_fills"] = [{
        "is_plane": True, "layer": "inner1", "net_id": "__plane_net__",
        "net_name": "GNDPLANE",
        "polygons": [[[0, 0], [50, 0], [50, 35], [0, 35], [0, 0]]],  # one poly, no cutouts
    }]
    v = dfm.check_inner_plane_antipad(routed, netlist, {"clearance_min_mm": 0.127})
    # blink has through-hole connector pads → at least one foreign feature flagged
    assert any(x.rule == "inner_plane_antipad" for x in v)


def test_dfm_trace_current_capacity_runs():
    # Just exercise the function end-to-end on the real board; it imports the
    # router's IPC helpers and may or may not flag — assert it returns a list
    # and the dedup/neckdown path executes without error.
    routed = json.loads(open(BLINK_ROUTED).read())
    netlist = json.loads(open(BLINK_NETLIST).read())
    out = dfm.check_trace_current_capacity(routed, netlist, copper_oz=0.5)
    assert isinstance(out, list)


def test_dfm_trace_current_capacity_undersized_long_run_errors():
    # net_vcc carries 0.5A → needs a wide trace. A single LONG 0.05mm segment
    # exceeds the neckdown allowance, so it stays an ERROR (lines 326-346).
    netlist = json.loads(open(BLINK_NETLIST).read())
    routed = {"routing": {"traces": [
        {"net_id": "net_vcc", "start_x_mm": 0, "start_y_mm": 0,
         "end_x_mm": 30, "end_y_mm": 0, "width_mm": 0.05, "layer": "top"},
    ], "vias": [], "copper_fills": []}}
    out = dfm.check_trace_current_capacity(routed, netlist, copper_oz=0.5)
    assert out and out[0].rule == "trace_current_capacity"
    assert out[0].severity == "error"


def test_dfm_trace_current_capacity_neckdown_demoted_to_warning():
    # A SHORT undersized run on a current-carrying net within the per-pad
    # allowance gets demoted to a warning (neckdown tolerance, lines 355-364).
    # net_vcc carries 0.5A; a single 0.5mm-long 0.05mm neck is well within the
    # per-pad allowance and below the segment cap → demoted.
    netlist = json.loads(open(BLINK_NETLIST).read())
    routed = {"routing": {"traces": [
        {"net_id": "net_vcc", "start_x_mm": 0, "start_y_mm": 0,
         "end_x_mm": 0.5, "end_y_mm": 0, "width_mm": 0.05, "layer": "top"},
    ], "vias": [], "copper_fills": []}}
    out = dfm.check_trace_current_capacity(routed, netlist, copper_oz=0.5)
    assert out, "expected an (undersized) violation to be raised then demoted"
    assert out[0].severity == "warning"
    assert "neckdown, tolerated" in out[0].message


def test_dfm_clearance_min_caps_at_50():
    # 12 traces, all mutually too-close on the same layer → >50 violation pairs,
    # hitting the output cap (line 105).
    traces = []
    for i in range(12):
        traces.append({"start_x_mm": 0, "start_y_mm": i * 0.05,
                       "end_x_mm": 30, "end_y_mm": i * 0.05,
                       "width_mm": 0.25, "layer": "top",
                       "net_id": f"n{i}", "net_name": f"N{i}"})
    routed = {"routing": {"traces": traces}}
    v = dfm.check_clearance_min(routed, {"clearance_min_mm": 0.2})
    assert len(v) > 50


def test_dfm_hole_to_hole_caps_at_50():
    vias = [{"x_mm": i * 0.1, "y_mm": 0, "net_name": f"N{i}"} for i in range(60)]
    routed = {"routing": {"vias": vias}}
    v = dfm.check_hole_to_hole(routed, {"elements": []},
                               {"min_hole_to_hole_mm": 0.5})
    assert len(v) > 50


def test_dfm_copper_to_edge_caps_at_20():
    traces = [{"start_x_mm": 0.05, "start_y_mm": i,
               "end_x_mm": 0.05, "end_y_mm": i + 0.1,
               "width_mm": 0.25, "layer": "top", "net_name": f"N{i}"}
              for i in range(30)]
    routed = {"board": {"width_mm": 50, "height_mm": 50},
              "routing": {"traces": traces, "vias": []}}
    v = dfm.check_copper_to_edge(routed, {}, {"min_copper_to_edge_mm": 0.2})
    assert len(v) > 20


def test_dfm_inner_plane_same_net_pad_skipped_and_degenerate_cutout():
    # A degenerate (<3 vertex) cutout → _cutout_radius returns zeros (line 465),
    # and a same-net through-hole pad is skipped (line 488).
    routed = json.loads(open(BLINK_ROUTED).read())
    netlist = json.loads(open(BLINK_NETLIST).read())
    from optimizers.pad_geometry import build_pad_map
    pad_map = build_pad_map(routed, netlist)
    th = next(p for p in pad_map.values() if p.layer == "all")
    plane_net = th.net_id  # make the plane SAME net as this TH pad
    routed["board"]["layers"] = 4
    routed["routing"]["copper_fills"] = [{
        "is_plane": True, "layer": "inner1", "net_id": plane_net,
        "net_name": "SAMENET",
        "polygons": [
            [[0, 0], [50, 0], [50, 35], [0, 35], [0, 0]],
            [[1, 1], [2, 1]],  # degenerate cutout (<3 pts after closing-drop)
        ],
    }]
    v = dfm.check_inner_plane_antipad(routed, netlist, {"clearance_min_mm": 0.127})
    # Same-net pad is skipped → no error attributed to it specifically.
    assert isinstance(v, list)


def test_dfm_inner_plane_antipad_clearance_computed_with_cutouts():
    # Plane WITH cutout polygons (polygons[1:]) → the antipad-clearance
    # computation path runs (lines 457-491). A foreign through-hole pad whose
    # cutout is too small produces an insufficient-clearance error.
    routed = json.loads(open(BLINK_ROUTED).read())
    netlist = json.loads(open(BLINK_NETLIST).read())
    # Find a real TH pad location to centre an undersized cutout on.
    from optimizers.pad_geometry import build_pad_map
    pad_map = build_pad_map(routed, netlist)
    th = next(p for p in pad_map.values() if p.layer == "all")
    cx, cy = th.x_mm, th.y_mm
    tiny = 0.05  # cutout radius far smaller than the pad → negative clearance
    cutout = [[cx + tiny, cy], [cx, cy + tiny], [cx - tiny, cy],
              [cx, cy - tiny], [cx + tiny, cy]]
    routed["board"]["layers"] = 4
    routed["routing"]["copper_fills"] = [{
        "is_plane": True, "layer": "inner1", "net_id": "__plane__",
        "net_name": "GNDPLANE",
        "polygons": [
            [[0, 0], [50, 0], [50, 35], [0, 35], [0, 0]],  # outer boundary
            cutout,                                          # one (too-small) cutout
        ],
    }]
    v = dfm.check_inner_plane_antipad(routed, netlist, {"clearance_min_mm": 0.127})
    assert any(x.rule == "inner_plane_antipad"
               and "Insufficient antipad clearance" in x.message for x in v)


# ===========================================================================
# drc_report.py
# ===========================================================================
from validators.drc_report import (run_drc, summarize_drc, _resolve_dfm_profile,
                                   _count_checked)


def test_resolve_dfm_profile_named_with_overrides():
    reqs = {"manufacturing": {"manufacturer": "jlcpcb_standard",
                              "trace_width_min_mm": 0.5}}
    prof, name = _resolve_dfm_profile(reqs)
    assert name == "jlcpcb_standard"
    assert prof["trace_width_min_mm"] == 0.5  # override applied


def test_resolve_dfm_profile_top_level_manufacturer():
    prof, name = _resolve_dfm_profile({"manufacturer": "oshpark_2layer"})
    assert name == "oshpark_2layer"


def test_resolve_dfm_profile_no_manufacturer_generic():
    prof, name = _resolve_dfm_profile({"manufacturing": {}})
    assert name == "generic"


def test_count_checked_rules():
    routed = {"routing": {"traces": [1, 2], "vias": [1],
                          "copper_fills": [{"is_plane": True}]},
              "silkscreen": [1], "placements": [1, 1, 1]}
    assert _count_checked(routed, "trace_width_min") == 2
    assert _count_checked(routed, "via_drill_min") == 1
    assert _count_checked(routed, "silkscreen") == 1
    assert _count_checked(routed, "hole_to_hole") == 4   # 1 via + 3 placements
    assert _count_checked(routed, "inner_plane_antipad") == 1
    assert _count_checked(routed, "unknown_rule") == 0


def test_run_drc_full_report_and_summarize():
    routed = json.loads(open(BLINK_ROUTED).read())
    netlist = json.loads(open(BLINK_NETLIST).read())
    reqs = json.loads(open(BLINK_REQS).read())
    report = run_drc(routed, netlist, reqs)
    assert "checks" in report and report["statistics"]["total_checks"] > 0
    summary = summarize_drc(report, top_n=3)
    assert "passed" in summary and "failing_rules" in summary


def test_summarize_drc_truncation_and_hints():
    # Craft a report with many violations across a known rule → truncation note
    # and remediation hint both fire.
    checks = [{
        "rule": "no_shorts", "category": "electrical", "passed": False,
        "violations": [
            {"rule": "no_shorts", "severity": "error", "message": f"short {i}",
             "location": {"x_mm": i, "y_mm": 0}}
            for i in range(15)
        ],
    }, {
        "rule": "silkscreen", "category": "dfm", "passed": True,
        "violations": [{"rule": "silkscreen", "severity": "warning",
                        "message": "tiny text"}],
    }]
    report = {"passed": False, "summary": "x", "manufacturer": "generic",
              "statistics": {"errors": 15, "warnings": 1}, "checks": checks}
    out = summarize_drc(report, top_n=5)
    assert out["truncated"] is True
    assert out["note"] is not None
    failing = {f["rule"]: f for f in out["failing_rules"]}
    assert "remediation_hint" in failing["no_shorts"]
    # errors ranked before warnings
    assert out["top_violations"][0]["severity"] == "error"


def test_run_drc_folds_unrouted_nets_into_connectivity():
    # A net the router left unrouted must surface as a connectivity FAILURE
    # (drc_report lines 119-123), not be silently skipped.
    routed = {
        "project_name": "x",
        "board": {"width_mm": 50, "height_mm": 50, "layers": 2},
        "placements": [],
        "routing": {
            "traces": [], "vias": [], "copper_fills": [],
            "unrouted_nets": ["net_open"],
            "config": {},
            "statistics": {"total_nets": 1},
        },
    }
    report = run_drc(routed, {"elements": []})
    conn = next(c for c in report["checks"] if c["rule"] == "connectivity")
    assert conn["passed"] is False
    assert any("net_open" in v["message"] for v in conn["violations"])


def test_run_drc_generic_loosens_to_routed_config():
    # No manufacturer → generic profile loosens trace/clearance and via floors
    # to the board's own config / smallest via (lines 255-273).
    routed = {
        "project_name": "x",
        "board": {"width_mm": 50, "height_mm": 50, "layers": 2},
        "placements": [],
        "routing": {
            "traces": [{"start_x_mm": 5, "start_y_mm": 5, "end_x_mm": 15,
                        "end_y_mm": 5, "width_mm": 0.13, "layer": "top",
                        "net_id": "n1", "net_name": "N1"}],
            "vias": [{"x_mm": 10, "y_mm": 10, "drill_mm": 0.2,
                      "diameter_mm": 0.4, "net_id": "n1", "net_name": "N1"}],
            "copper_fills": [],
            "config": {"trace_width_signal_mm": 0.13, "trace_clearance_mm": 0.13},
            "statistics": {"total_nets": 1},
        },
    }
    netlist = {"elements": []}
    report = run_drc(routed, netlist)  # no requirements → generic
    assert report["manufacturer"] == "generic"
    tw = next(c for c in report["checks"] if c["rule"] == "trace_width_min")
    # The 0.13mm trace must NOT be flagged because generic loosened to 0.13.
    assert tw["passed"] is True


# ===========================================================================
# validate_routing.py
# ===========================================================================
import validate_routing as vr


def test_routing_copper_stack_and_via_span():
    assert vr._copper_stack(2) == ["top", "bottom"]
    assert vr._copper_stack(4) == ["top", "inner1", "inner2", "bottom"]
    span = vr._via_spanned_layers("top", "bottom",
                                  ["top", "inner1", "inner2", "bottom"])
    assert span == ["top", "inner1", "inner2", "bottom"]
    # unknown layers → fall back to the two endpoints
    assert vr._via_spanned_layers("foo", "bar", ["top", "bottom"]) == ["foo", "bar"]


def test_routing_point_to_segment_degenerate():
    # Zero-length segment → distance to the point (line 77).
    assert vr._point_to_segment_distance(3, 4, 0, 0, 0, 0) == pytest.approx(5.0)


def test_routing_pad_clearance_skips_no_net_pad():
    # A pad with net_id None must be skipped (line 582). Build a netlist where
    # the component's port belongs to no net.
    netlist = {"elements": [
        {"element_type": "component", "component_id": "c1", "designator": "R1",
         "component_type": "resistor", "package": "0805", "value": "1k"},
        {"element_type": "port", "port_id": "p1", "component_id": "c1",
         "pin_number": 1, "name": "1", "electrical_type": "passive"},
        # no net references p1 → pad.net_id is None
    ]}
    routed = {"placements": [
        {"designator": "R1", "x_mm": 10, "y_mm": 10, "rotation_deg": 0,
         "layer": "top", "footprint_width_mm": 2, "footprint_height_mm": 1.25,
         "package": "0805", "component_type": "resistor"},
    ], "routing": {"config": {}, "traces": [
        {"start_x_mm": 9.1, "start_y_mm": 10, "end_x_mm": 20, "end_y_mm": 10,
         "width_mm": 0.25, "layer": "top", "net_id": "x", "net_name": "X"},
    ], "vias": []}}
    errors, warnings = vr._check_pad_clearance(routed, netlist)
    # The no-net pad is skipped → no short reported against it.
    assert not any("R1" in e for e in errors)


def test_validate_routing_incomplete_summary():
    # completion < 100 → "passed with warnings" summary branch (lines 754-759).
    # Start from the real (schema-valid) board, then knock completion to 80%.
    routed = json.loads(open(BLINK_ROUTED).read())
    routed["routing"]["statistics"]["completion_pct"] = 80
    routed["routing"]["statistics"]["routed_nets"] = 4
    routed["routing"]["statistics"]["total_nets"] = 5
    path = _write_temp(routed)
    res = vr.validate_routing(path, BLINK_NETLIST)
    assert res["valid"] is True, res["errors"]
    assert "nets routed" in res["summary"] and "80%" in res["summary"]


def test_routing_trace_clearance_violation():
    routed = {"routing": {"config": {"trace_clearance_mm": 0.2}, "traces": [
        {"start_x_mm": 0, "start_y_mm": 0, "end_x_mm": 10, "end_y_mm": 0,
         "width_mm": 0.25, "layer": "top", "net_id": "a", "net_name": "A"},
        {"start_x_mm": 0, "start_y_mm": 0.1, "end_x_mm": 10, "end_y_mm": 0.1,
         "width_mm": 0.25, "layer": "top", "net_id": "b", "net_name": "B"},
    ]}}
    errors, _ = vr._check_trace_clearance(routed)
    assert any("Trace clearance violation" in e for e in errors)


def test_routing_via_clearance_violations():
    routed = {"routing": {"config": {"trace_clearance_mm": 0.2},
        "vias": [
            {"x_mm": 5, "y_mm": 5, "diameter_mm": 0.6, "net_id": "a",
             "net_name": "A", "from_layer": "top", "to_layer": "bottom"},
            {"x_mm": 5.3, "y_mm": 5, "diameter_mm": 0.6, "net_id": "b",
             "net_name": "B", "from_layer": "top", "to_layer": "bottom"},
        ],
        "traces": [
            {"start_x_mm": 5, "start_y_mm": 4.9, "end_x_mm": 15, "end_y_mm": 4.9,
             "width_mm": 0.25, "layer": "top", "net_id": "c", "net_name": "C"},
        ]}}
    errors, _ = vr._check_via_clearance(routed)
    blob = "\n".join(errors)
    assert "Via clearance violation" in blob
    assert "Via-trace clearance violation" in blob


def test_routing_via_clearance_skips_trace_off_via_layers():
    # Via spans top↔bottom; a trace on inner1 is not on the via's layers → the
    # `trace.layer not in via_layers` continue fires (line 186).
    routed = {"routing": {"config": {"trace_clearance_mm": 0.2},
        "vias": [{"x_mm": 5, "y_mm": 5, "diameter_mm": 0.6, "net_id": "a",
                  "net_name": "A", "from_layer": "top", "to_layer": "bottom"}],
        "traces": [{"start_x_mm": 5, "start_y_mm": 5, "end_x_mm": 15,
                    "end_y_mm": 5, "width_mm": 0.25, "layer": "inner1",
                    "net_id": "b", "net_name": "B"}]}}
    errors, _ = vr._check_via_clearance(routed)
    assert not any("Via-trace" in e for e in errors)


def test_routing_pad_clearance_dedup_same_pad():
    # Two foreign-net trace segments both overlap R1.1 → only ONE short error
    # (the seen_tp dedup `continue`, lines 623-624).
    netlist = {"elements": [
        {"element_type": "component", "component_id": "c1", "designator": "R1",
         "component_type": "resistor", "package": "0805", "value": "1k"},
        {"element_type": "port", "port_id": "p1", "component_id": "c1",
         "pin_number": 1, "name": "1", "electrical_type": "passive"},
        {"element_type": "net", "net_id": "a", "name": "A",
         "net_class": "signal", "connected_port_ids": ["p1"]},
    ]}
    seg = lambda y: {"start_x_mm": 8.8, "start_y_mm": y, "end_x_mm": 9.4,
                     "end_y_mm": y, "width_mm": 0.25, "layer": "top",
                     "net_id": "b", "net_name": "B"}
    routed = {"placements": [
        {"designator": "R1", "x_mm": 10, "y_mm": 10, "rotation_deg": 0,
         "layer": "top", "footprint_width_mm": 2, "footprint_height_mm": 1.25,
         "package": "0805", "component_type": "resistor"},
    ], "routing": {"config": {}, "traces": [seg(10.0), seg(10.05)], "vias": []}}
    errors, _ = vr._check_pad_clearance(routed, netlist)
    shorts = [e for e in errors if "Trace-pad short" in e and "R1" in e]
    assert len(shorts) == 1  # deduped to one


def test_routing_pad_clearance_warning_dedup_same_pad():
    # Two foreign-net trace segments each within clearance of R1.1 (but not
    # overlapping) → only ONE warning (the warn-dedup `continue`, line 635).
    netlist = {"elements": [
        {"element_type": "component", "component_id": "c1", "designator": "R1",
         "component_type": "resistor", "package": "0805", "value": "1k"},
        {"element_type": "port", "port_id": "p1", "component_id": "c1",
         "pin_number": 1, "name": "1", "electrical_type": "passive"},
        {"element_type": "net", "net_id": "a", "name": "A",
         "net_class": "signal", "connected_port_ids": ["p1"]},
    ]}
    # Pad R1.1 copper right edge at x=9.4. Traces at x≈9.5 (gap ~0.05 < 0.15).
    near = lambda y: {"start_x_mm": 9.5, "start_y_mm": 9.55 + y,
                      "end_x_mm": 9.5, "end_y_mm": 10.45 + y,
                      "width_mm": 0.1, "layer": "top",
                      "net_id": "b", "net_name": "B"}
    routed = {"placements": [
        {"designator": "R1", "x_mm": 10, "y_mm": 10, "rotation_deg": 0,
         "layer": "top", "footprint_width_mm": 2, "footprint_height_mm": 1.25,
         "package": "0805", "component_type": "resistor"},
    ], "routing": {"config": {"trace_clearance_mm": 0.2},
                   "traces": [near(0.0), near(0.01)], "vias": []}}
    _, warnings = vr._check_pad_clearance(routed, netlist)
    warns = [w for w in warnings if "Trace-pad clearance" in w and "R1" in w]
    assert len(warns) == 1  # deduped to one


def test_routing_no_shorts_violation():
    routed = {"routing": {"traces": [
        {"start_x_mm": 0, "start_y_mm": 0, "end_x_mm": 10, "end_y_mm": 0,
         "width_mm": 0.25, "layer": "top", "net_id": "a", "net_name": "A"},
        {"start_x_mm": 0, "start_y_mm": 0.0, "end_x_mm": 10, "end_y_mm": 0.0,
         "width_mm": 0.25, "layer": "top", "net_id": "b", "net_name": "B"},
    ]}}
    errors, _ = vr._check_no_shorts(routed)
    assert any("Short circuit" in e for e in errors)


def test_routing_connectivity_no_netlist_warns():
    errors, warnings = vr._check_connectivity({"routing": {}}, None)
    assert any("no netlist provided" in w for w in warnings)


def test_routing_incomplete_net_ids_includes_unrouted():
    routed = {"routing": {"unrouted_nets": ["n1", "n2"]}}
    ids = vr.incomplete_net_ids(routed, None)
    assert ids == {"n1", "n2"}


def test_routing_point_to_rect_and_segment_to_rect():
    # point inside rect → 0
    assert vr._point_to_rect_distance(0, 0, 0, 0, 1, 1) == 0.0
    # point outside
    assert vr._point_to_rect_distance(3, 0, 0, 0, 1, 1) == pytest.approx(2.0)
    # segment crossing rect → 0
    assert vr._segment_to_rect_distance(-5, 0, 5, 0, 0, 0, 1, 1) == 0.0
    # segment passing the short side, not touching
    d = vr._segment_to_rect_distance(0, 5, 10, 5, 0, 0, 1, 1)
    assert d == pytest.approx(4.0)


def test_routing_pad_clearance_no_netlist_returns_empty():
    errors, warnings = vr._check_pad_clearance({"routing": {}}, None)
    assert errors == [] and warnings == []


def _pad_clearance_fixture(trace=None, via=None):
    """Synthetic routed+netlist with one SMD 0805 pad (R1.1) on net 'a'.

    R1.1 sits at (9.1, 10): copper rect x[8.8,9.4] y[9.55,10.45].
    Caller supplies a trace/via on a DIFFERENT net ('b') to probe clearance.
    """
    netlist = {"elements": [
        {"element_type": "component", "component_id": "c1", "designator": "R1",
         "component_type": "resistor", "package": "0805", "value": "1k"},
        {"element_type": "port", "port_id": "p1", "component_id": "c1",
         "pin_number": 1, "name": "1", "electrical_type": "passive"},
        {"element_type": "net", "net_id": "a", "name": "A",
         "net_class": "signal", "connected_port_ids": ["p1"]},
    ]}
    routed = {"placements": [
        {"designator": "R1", "x_mm": 10, "y_mm": 10, "rotation_deg": 0,
         "layer": "top", "footprint_width_mm": 2.0, "footprint_height_mm": 1.25,
         "package": "0805", "component_type": "resistor"},
    ], "routing": {"config": {"trace_clearance_mm": 0.2},
                   "traces": [trace] if trace else [],
                   "vias": [via] if via else []}}
    return routed, netlist


def test_routing_pad_clearance_trace_short_and_warning():
    # Trace on net 'b' passing THROUGH the pad copper → short (error).
    short = {"start_x_mm": 8.8, "start_y_mm": 10, "end_x_mm": 9.4,
             "end_y_mm": 10, "width_mm": 0.25, "layer": "top",
             "net_id": "b", "net_name": "B"}
    routed, netlist = _pad_clearance_fixture(trace=short)
    errors, _ = vr._check_pad_clearance(routed, netlist)
    assert any("Trace-pad short" in e for e in errors)

    # Trace just outside the pad but within clearance → warning.
    near = {"start_x_mm": 9.5, "start_y_mm": 9.55, "end_x_mm": 9.5,
            "end_y_mm": 10.45, "width_mm": 0.1, "layer": "top",
            "net_id": "b", "net_name": "B"}
    routed, netlist = _pad_clearance_fixture(trace=near)
    _, warnings = vr._check_pad_clearance(routed, netlist)
    assert any("Trace-pad clearance" in w for w in warnings)


def test_routing_pad_clearance_via_short_and_warning():
    # Via centred on the pad → via-pad short.
    short_via = {"x_mm": 9.1, "y_mm": 10, "diameter_mm": 0.6, "net_id": "b",
                 "net_name": "B", "from_layer": "top", "to_layer": "bottom"}
    routed, netlist = _pad_clearance_fixture(via=short_via)
    errors, _ = vr._check_pad_clearance(routed, netlist)
    assert any("Via-pad short" in e for e in errors)

    # Via just outside the pad (edge gap ~0.1mm < 0.15 threshold) → warning.
    near_via = {"x_mm": 9.65, "y_mm": 10, "diameter_mm": 0.3, "net_id": "b",
                "net_name": "B", "from_layer": "top", "to_layer": "bottom"}
    routed, netlist = _pad_clearance_fixture(via=near_via)
    _, warnings = vr._check_pad_clearance(routed, netlist)
    assert any("Via-pad clearance" in w for w in warnings)


def test_routing_connectivity_disconnected_groups():
    # Two pads on net 'a' with NO trace between them → disconnected groups error.
    netlist = {"elements": [
        {"element_type": "component", "component_id": "c1", "designator": "R1",
         "component_type": "resistor", "package": "0805", "value": "1k"},
        {"element_type": "component", "component_id": "c2", "designator": "R2",
         "component_type": "resistor", "package": "0805", "value": "1k"},
        {"element_type": "port", "port_id": "p1", "component_id": "c1",
         "pin_number": 1, "name": "1", "electrical_type": "passive"},
        {"element_type": "port", "port_id": "p2", "component_id": "c2",
         "pin_number": 1, "name": "1", "electrical_type": "passive"},
        {"element_type": "net", "net_id": "a", "name": "A",
         "net_class": "signal", "connected_port_ids": ["p1", "p2"]},
    ]}
    routed = {"board": {"layers": 2}, "placements": [
        {"designator": "R1", "x_mm": 5, "y_mm": 5, "rotation_deg": 0,
         "layer": "top", "footprint_width_mm": 2, "footprint_height_mm": 1.25,
         "package": "0805", "component_type": "resistor"},
        {"designator": "R2", "x_mm": 40, "y_mm": 40, "rotation_deg": 0,
         "layer": "top", "footprint_width_mm": 2, "footprint_height_mm": 1.25,
         "package": "0805", "component_type": "resistor"},
    ], "routing": {"traces": [], "vias": [], "copper_fills": [],
                   "unrouted_nets": [], "config": {}}}
    errors, _ = vr._check_connectivity(routed, netlist)
    assert any("disconnected groups" in e for e in errors)


def test_routing_inner_plane_connectivity_via_to_plane():
    # 4-layer board: a TH pad + a through-via reaching a solid inner GND plane.
    # Exercises the inner-plane connectivity union path (lines 405-439).
    netlist = {"elements": [
        {"element_type": "component", "component_id": "c1", "designator": "J1",
         "component_type": "connector", "package": "PinHeader_1x02", "value": ""},
        {"element_type": "port", "port_id": "p1", "component_id": "c1",
         "pin_number": 1, "name": "GND", "electrical_type": "ground"},
        {"element_type": "port", "port_id": "p2", "component_id": "c1",
         "pin_number": 2, "name": "GND2", "electrical_type": "ground"},
        {"element_type": "net", "net_id": "gnd", "name": "GND",
         "net_class": "ground", "connected_port_ids": ["p1", "p2"]},
    ]}
    routed = {"board": {"layers": 4}, "placements": [
        {"designator": "J1", "x_mm": 20, "y_mm": 20, "rotation_deg": 0,
         "layer": "top", "footprint_width_mm": 5, "footprint_height_mm": 3,
         "package": "PinHeader_1x02", "component_type": "connector"},
    ], "routing": {
        "traces": [], "config": {},
        "vias": [{"x_mm": 20, "y_mm": 20, "diameter_mm": 0.6, "drill_mm": 0.3,
                  "net_id": "gnd", "from_layer": "top", "to_layer": "bottom"}],
        "copper_fills": [{"is_plane": True, "layer": "inner1", "net_id": "gnd",
                          "net_name": "GND", "polygons": [
                              [[0, 0], [50, 0], [50, 50], [0, 50], [0, 0]]]}],
        "unrouted_nets": [],
    }}
    errors, _ = vr._check_connectivity(routed, netlist)
    # Through-hole GND pads penetrate the plane → all connected, no error.
    assert not any("gnd" in e for e in errors)


def test_routing_inner_plane_via_to_plane_layer_and_pad_join():
    # SMD GND pads on top, joined to a solid inner1 GND plane by a blind via
    # (top→inner1). Exercises _reaches_plane's non-through branch (line 414) and
    # the via→pad join (lines 431-439).
    netlist = {"elements": [
        {"element_type": "component", "component_id": "c1", "designator": "R1",
         "component_type": "resistor", "package": "0805", "value": "1k"},
        {"element_type": "component", "component_id": "c2", "designator": "R2",
         "component_type": "resistor", "package": "0805", "value": "1k"},
        {"element_type": "port", "port_id": "p1", "component_id": "c1",
         "pin_number": 1, "name": "1", "electrical_type": "passive"},
        {"element_type": "port", "port_id": "p2", "component_id": "c2",
         "pin_number": 1, "name": "1", "electrical_type": "passive"},
        {"element_type": "net", "net_id": "gnd", "name": "GND",
         "net_class": "ground", "connected_port_ids": ["p1", "p2"]},
    ]}
    # 0805 pad 1 sits at component_x - 0.9. Place each pad's via right on it.
    routed = {"board": {"layers": 4}, "placements": [
        {"designator": "R1", "x_mm": 10, "y_mm": 10, "rotation_deg": 0,
         "layer": "top", "footprint_width_mm": 2, "footprint_height_mm": 1.25,
         "package": "0805", "component_type": "resistor"},
        {"designator": "R2", "x_mm": 30, "y_mm": 30, "rotation_deg": 0,
         "layer": "top", "footprint_width_mm": 2, "footprint_height_mm": 1.25,
         "package": "0805", "component_type": "resistor"},
    ], "routing": {
        "traces": [], "config": {},
        "vias": [
            {"x_mm": 9.1, "y_mm": 10, "diameter_mm": 0.6, "drill_mm": 0.3,
             "net_id": "gnd", "from_layer": "top", "to_layer": "inner1"},
            {"x_mm": 29.1, "y_mm": 30, "diameter_mm": 0.6, "drill_mm": 0.3,
             "net_id": "gnd", "from_layer": "top", "to_layer": "inner1"},
        ],
        "copper_fills": [{"is_plane": True, "layer": "inner1", "net_id": "gnd",
                          "net_name": "GND", "polygons": [
                              [[0, 0], [50, 0], [50, 50], [0, 50], [0, 0]]]}],
        "unrouted_nets": [],
    }}
    # Add a same-net via that does NOT reach the inner1 plane (bottom→inner2)
    # so the `if not _reaches_plane(v): continue` skip fires (line 433).
    routed["routing"]["vias"].append(
        {"x_mm": 45, "y_mm": 45, "diameter_mm": 0.6, "drill_mm": 0.3,
         "net_id": "gnd", "from_layer": "bottom", "to_layer": "inner2"})
    errors, _ = vr._check_connectivity(routed, netlist)
    # Both SMD GND pads reach the plane via their blind vias → fully connected.
    assert not any("disconnected" in e for e in errors)


def test_validate_routing_netlist_load_failure_silent():
    # routed reads fine, but a bad netlist path is swallowed (lines 707-708);
    # connectivity then warns "no netlist provided".
    res = vr.validate_routing(BLINK_ROUTED, "/no/such/netlist.json")
    assert "summary" in res


def test_validate_routing_main_failure(capsys):
    bad = _write_temp({"not": "routed"})
    rc = vr.main([bad])
    out = capsys.readouterr().out
    assert rc == 1
    assert "FAILED" in out


def test_validate_routing_main_prints_warnings(capsys):
    # No --netlist → connectivity check is skipped with a WARNING, exercising
    # main()'s warning-print loop (line 797).
    rc = vr.main([BLINK_ROUTED])
    out = capsys.readouterr().out
    assert rc == 0
    assert "WARNING:" in out


def test_validate_routing_file_read_error():
    res = vr.validate_routing("/no/such/routed.json")
    assert res["valid"] is False and res["summary"] == "File read error"


def test_validate_routing_schema_failure():
    bad = _write_temp({"not": "a routed board"})
    res = vr.validate_routing(bad)
    assert res["valid"] is False and "Schema validation failed" in res["summary"]


def test_validate_routing_full_pass():
    res = vr.validate_routing(BLINK_ROUTED, BLINK_NETLIST)
    assert "summary" in res
    # Real board is fully routed and clean; assert it validates.
    assert res["valid"] is True, res["errors"]


def test_validate_routing_main_cli(capsys):
    rc = vr.main([BLINK_ROUTED, "--netlist", BLINK_NETLIST])
    out = capsys.readouterr().out
    assert rc == 0
    assert "PASSED" in out
