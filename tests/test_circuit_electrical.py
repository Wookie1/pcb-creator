"""Electrical checks driven by the DC nodal solve.

Two kinds of test here:

1. Closed-form cases where the right answer is arithmetic, not opinion. The
   shared-resistor, series-LED, divider and pull-up cases are exactly the ones
   the previous adjacency heuristic got wrong.
2. A corpus guard. Every netlist under projects/ passed DRC and reached
   routing, so a check that reports an *error* on one is presumptively a false
   positive. A weak local model that receives spurious errors learns to ignore
   the tool, which is worse than not shipping the check.
"""

import glob
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "validators"))
sys.path.insert(0, str(ROOT))

import drc_checks  # noqa: E402
from circuit_graph import solve  # noqa: E402
from circuit_report import build_report  # noqa: E402
from drc_checks import build_lookups, resolve_rails, solve_circuit  # noqa: E402
from engineering_constants import parse_supply_voltage  # noqa: E402


# ---------------------------------------------------------------------------
# Fixture builders
# ---------------------------------------------------------------------------

def _comp(des, ctype, value, pins, package="0805"):
    """pins: list of (pin_number, name)."""
    cid = f"comp_{des.lower()}"
    els = [{
        "element_type": "component", "component_id": cid, "designator": des,
        "component_type": ctype, "value": value, "package": package,
    }]
    for num, name in pins:
        els.append({
            "element_type": "port", "port_id": f"port_{des.lower()}_{num}",
            "component_id": cid, "pin_number": num, "name": name,
            "electrical_type": "passive",
        })
    return els


def _net(nid, ports, cls="signal"):
    return {
        "element_type": "net", "net_id": nid, "name": nid.replace("net_", "").upper(),
        "connected_port_ids": ports, "net_class": cls,
    }


def _res(des, ohms):
    return _comp(des, "resistor", ohms, [(1, "1"), (2, "2")])


def _led(des, color="red"):
    return _comp(des, "led", color, [(1, "anode"), (2, "cathode")])


def _solve(elements, volts=5.0):
    c, p, n = build_lookups(elements)
    return c, p, n, solve_circuit(c, p, n, volts)


def _current(sol, designator):
    return next(b.current for b in sol.branches if b.designator == designator)


# ---------------------------------------------------------------------------
# Closed-form solve cases
# ---------------------------------------------------------------------------

def test_single_led_with_series_resistor():
    """I = (5 - 2.0) / (220 + 10) = 13.04mA."""
    els = _res("R1", "220ohm") + _led("D1") + [
        _net("net_vcc", ["port_r1_1"], "power"),
        _net("net_mid", ["port_r1_2", "port_d1_1"]),
        _net("net_gnd", ["port_d1_2"], "ground"),
    ]
    _c, _p, _n, sol = _solve(els)
    assert _current(sol, "R1") == pytest.approx(0.01304, rel=1e-3)
    assert _current(sol, "D1") == pytest.approx(0.01304, rel=1e-3)


def test_two_leds_in_series_use_both_forward_drops():
    """I = (5 - 2*2.0) / (100 + 2*10) = 8.33mA.

    The old heuristic found one adjacent LED and used a single Vf, giving
    (5 - 2.0) / 100 = 30mA - 3.6x too high.
    """
    els = _res("R1", "100ohm") + _led("D1") + _led("D2") + [
        _net("net_vcc", ["port_r1_1"], "power"),
        _net("net_a", ["port_r1_2", "port_d1_1"]),
        _net("net_b", ["port_d1_2", "port_d2_1"]),
        _net("net_gnd", ["port_d2_2"], "ground"),
    ]
    _c, _p, _n, sol = _solve(els)
    assert _current(sol, "R1") == pytest.approx(0.00833, rel=1e-2)
    assert _current(sol, "D2") == pytest.approx(0.00833, rel=1e-2)


def test_three_leds_sharing_one_resistor_split_the_current():
    """Three parallel LEDs behind one 220ohm: each carries a third of it."""
    els = _res("R1", "220ohm") + _led("D1") + _led("D2") + _led("D3") + [
        _net("net_vcc", ["port_r1_1"], "power"),
        _net("net_mid", ["port_r1_2", "port_d1_1", "port_d2_1", "port_d3_1"]),
        _net("net_gnd", ["port_d1_2", "port_d2_2", "port_d3_2"], "ground"),
    ]
    _c, _p, _n, sol = _solve(els)
    total = _current(sol, "R1")
    for des in ("D1", "D2", "D3"):
        assert _current(sol, des) == pytest.approx(total / 3, rel=1e-6)


def test_divider_midpoint_and_power():
    """10k/10k on 5V: 2.5V midpoint, 0.25mA, 0.625mW each.

    The old fallback assumed the full supply across each resistor and
    reported 2.5mW - 4x over.
    """
    els = _res("R1", "10kohm") + _res("R2", "10kohm") + [
        _net("net_vcc", ["port_r1_1"], "power"),
        _net("net_mid", ["port_r1_2", "port_r2_1"]),
        _net("net_gnd", ["port_r2_2"], "ground"),
    ]
    _c, _p, _n, sol = _solve(els)
    assert sol.node_v["net_mid"] == pytest.approx(2.5)
    assert _current(sol, "R1") == pytest.approx(0.00025)
    branch = next(b for b in sol.branches if b.designator == "R1")
    assert branch.power == pytest.approx(0.000625)


def test_pullup_to_an_ic_pin_carries_no_current():
    """A pull-up sits at the rail and dissipates nothing (old code: 2.5mW)."""
    els = _res("R1", "10kohm") + _comp(
        "U1", "ic", "ATTINY85", [(1, "PB0"), (8, "VCC"), (4, "GND")], "SOIC-8"
    ) + [
        _net("net_vcc", ["port_r1_1", "port_u1_8"], "power"),
        _net("net_sig", ["port_r1_2", "port_u1_1"]),
        _net("net_gnd", ["port_u1_4"], "ground"),
    ]
    _c, _p, _n, sol = _solve(els)
    assert _current(sol, "R1") == pytest.approx(0.0, abs=1e-12)
    assert sol.node_v["net_sig"] == pytest.approx(5.0)


def test_reverse_biased_led_blocks():
    els = _res("R1", "220ohm") + _led("D1") + [
        _net("net_vcc", ["port_r1_1"], "power"),
        _net("net_mid", ["port_r1_2", "port_d1_2"]),   # cathode toward the rail
        _net("net_gnd", ["port_d1_1"], "ground"),
    ]
    _c, _p, _n, sol = _solve(els)
    d1 = next(b for b in sol.branches if b.designator == "D1")
    assert not d1.conducting
    assert d1.current == pytest.approx(0.0)


def test_capacitor_is_open_at_dc():
    """A decoupling cap must not become a short from the rail to ground."""
    els = _comp("C1", "capacitor", "100nF", [(1, "1"), (2, "2")]) + [
        _net("net_vcc", ["port_c1_1"], "power"),
        _net("net_gnd", ["port_c1_2"], "ground"),
    ]
    _c, _p, _n, sol = _solve(els)
    assert sol.branches == []


# ---------------------------------------------------------------------------
# Taint: the anti-false-confidence rule
# ---------------------------------------------------------------------------

def test_taint_propagates_downstream_of_an_unmodeled_part():
    """An LED behind a resistor behind an MCU pin is indeterminate, not 0mA.

    Without propagation the LED branch reads a trustworthy-looking 0.00mA and
    would be reported as "too dim to see".
    """
    els = _res("R1", "220ohm") + _led("D1") + _comp(
        "U1", "ic", "ATTINY85", [(1, "PB0"), (8, "VCC"), (4, "GND")], "SOIC-8"
    ) + [
        _net("net_vcc", ["port_u1_8"], "power"),
        _net("net_drive", ["port_u1_1", "port_r1_1"]),
        _net("net_led", ["port_r1_2", "port_d1_1"]),
        _net("net_gnd", ["port_d1_2", "port_u1_4"], "ground"),
    ]
    c, p, n, sol = _solve(els)
    assert sol.is_tainted("net_drive")
    assert sol.is_tainted("net_led"), "taint must cross the resistor"
    d1 = next(b for b in sol.branches if b.designator == "D1")
    assert sol.branch_is_tainted(d1)

    _errs, warns = drc_checks.check_led_current(c, p, n, 5.0, sol)
    assert not any("too dim" in w for w in warns)


def test_declared_rail_blocks_taint():
    """Every IC touches VCC; that must not make the whole board unknowable."""
    els = _res("R1", "220ohm") + _led("D1") + _comp(
        "U1", "ic", "ATTINY85", [(1, "PB0"), (8, "VCC"), (4, "GND")], "SOIC-8"
    ) + [
        _net("net_vcc", ["port_u1_8", "port_r1_1"], "power"),
        _net("net_led", ["port_r1_2", "port_d1_1"]),
        _net("net_gnd", ["port_d1_2", "port_u1_4"], "ground"),
    ]
    _c, _p, _n, sol = _solve(els)
    assert not sol.is_tainted("net_vcc")
    assert not sol.is_tainted("net_led")
    assert _current(sol, "D1") == pytest.approx(0.01304, rel=1e-3)


def test_multi_terminal_passive_becomes_a_boundary():
    """A 3-pin potentiometer typed as a resistor must taint, not vanish."""
    els = _comp("R2", "resistor", "10k linear", [(1, "1"), (2, "2"), (3, "3")]) + \
        _led("D1") + [
            _net("net_vcc", ["port_r2_1"], "power"),
            _net("net_wiper", ["port_r2_2", "port_d1_1"]),
            _net("net_gnd", ["port_r2_3", "port_d1_2"], "ground"),
        ]
    _c, _p, _n, sol = _solve(els)
    assert any(u["designator"] == "R2" for u in sol.unmodeled)
    assert sol.is_tainted("net_wiper")


# ---------------------------------------------------------------------------
# Structural checks
# ---------------------------------------------------------------------------

def test_port_on_two_nets_is_a_short():
    els = _res("R1", "220ohm") + _res("R2", "220ohm") + [
        _net("net_a", ["port_r1_1", "port_r2_1"], "signal"),
        _net("net_b", ["port_r1_1", "port_r2_2"], "signal"),
    ]
    c, p, n = build_lookups(els)
    errors, _w = drc_checks.check_circuit_integrity(c, p, n)
    assert any("shorted together" in e for e in errors)


def test_component_with_both_pins_on_one_net_is_reported():
    els = _res("R1", "220ohm") + _res("R2", "1kohm") + [
        _net("net_vcc", ["port_r1_1"], "power"),
        _net("net_gnd", ["port_r1_2", "port_r2_1", "port_r2_2"], "ground"),
    ]
    c, p, n, sol = _solve(els)
    assert any(s["designator"] == "R2" for s in sol.shorted)
    errors, _w = drc_checks.check_circuit_integrity(c, p, n, 5.0, sol)
    assert any("shorted out" in e for e in errors)


def test_isolated_island_is_reported_floating():
    """Two nets with 2 pins each, never reaching a rail - single-pin checks miss it."""
    els = _res("R1", "220ohm") + _res("R2", "1kohm") + _res("R3", "1kohm") + [
        _net("net_vcc", ["port_r1_1"], "power"),
        _net("net_gnd", ["port_r1_2"], "ground"),
        _net("net_x", ["port_r2_1", "port_r3_1"]),
        _net("net_y", ["port_r2_2", "port_r3_2"]),
    ]
    c, p, n, sol = _solve(els)
    assert sol.floating_nets == ["net_x", "net_y"]
    errors, _w = drc_checks.check_circuit_integrity(c, p, n, 5.0, sol)
    assert any("no DC path" in e for e in errors)


# ---------------------------------------------------------------------------
# Supply voltage parsing
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("text,expected", [
    ("5V", 5.0),
    ("3.3V", 3.3),
    ("3.3V regulated", 3.3),
    ("13.8V DC", 13.8),
    # Ambiguous: parse_voltage() would silently return the first number.
    ("adjustable (1.23V-37V output)", None),
    ("5V logic / 7-35V motor", None),
    ("", None),
])
def test_parse_supply_voltage_rejects_ambiguous_values(text, expected):
    assert parse_supply_voltage(text) == expected


# ---------------------------------------------------------------------------
# Report shape - the anti-false-confidence contract
# ---------------------------------------------------------------------------

def test_unmodeled_parts_prevent_a_clean_verdict():
    els = _res("R1", "10kohm") + _comp(
        "U1", "ic", "CH340C", [(1, "TXD"), (16, "VCC"), (8, "GND")], "SOP16"
    ) + [
        _net("net_vcc", ["port_r1_1", "port_u1_16"], "power"),
        _net("net_sig", ["port_r1_2", "port_u1_1"]),
        _net("net_gnd", ["port_u1_8"], "ground"),
    ]
    report = build_report(els, supply_voltage=5.0)
    assert report["verdict"] == "not_enough_information"
    assert report["not_checked"], "an unmodeled IC must be named"
    assert "NOT verified" in report["headline"]


def test_no_supply_voltage_reports_what_argument_to_pass():
    els = _res("R1", "220ohm") + _led("D1") + [
        _net("net_vcc", ["port_r1_1"], "power"),
        _net("net_mid", ["port_r1_2", "port_d1_1"]),
        _net("net_gnd", ["port_d1_2"], "ground"),
    ]
    report = build_report(els, supply_voltage=None)
    assert report["verdict"] == "not_enough_information"
    assert report["not_checked"][0]["to_check_this"]["args"] == {"supply_voltage": "5V"}


def test_verdict_never_claims_the_circuit_works():
    """A fully-modeled clean circuit is 'no_issues_found', never 'pass'."""
    els = _res("R1", "220ohm") + _led("D1") + [
        _net("net_vcc", ["port_r1_1"], "power"),
        _net("net_mid", ["port_r1_2", "port_d1_1"]),
        _net("net_gnd", ["port_d1_2"], "ground"),
    ]
    report = build_report(els, supply_voltage=5.0)
    assert report["verdict"] == "no_issues_found"
    assert "not proof the circuit works" in report["headline"]


def test_agent_supplied_model_is_labelled_as_such():
    els = _comp("U1", "ic", "MYSTERY", [(1, "VCC"), (2, "GND")], "SOT-23")
    els[1]["electrical_type"] = "power_in"
    els += [
        _net("net_vcc", ["port_u1_1"], "power"),
        _net("net_gnd", ["port_u1_2"], "ground"),
    ]
    report = build_report(
        els, supply_voltage=5.0, models={"U1": {"vcc_min": "3.0V", "vcc_max": "3.6V"}}
    )
    assert report["inputs_used"]["models"]["source"] == "agent"


def test_unsupported_model_key_is_reported_not_ignored():
    els = _res("R1", "220ohm") + [
        _net("net_vcc", ["port_r1_1"], "power"),
        _net("net_gnd", ["port_r1_2"], "ground"),
    ]
    report = build_report(els, supply_voltage=5.0, models={"R1": {"icc": "25mA"}})
    assert any("R1.icc" in e["what"] for e in report["not_checked"])


# ---------------------------------------------------------------------------
# Corpus guard
# ---------------------------------------------------------------------------

def _corpus():
    out = []
    for path in sorted(glob.glob(str(ROOT / "projects/**/*_netlist.json"), recursive=True)):
        try:
            data = json.loads(Path(path).read_text())
        except (OSError, ValueError):
            continue
        if data.get("elements"):
            out.append((Path(path).stem, path, data))
    return out


CORPUS = _corpus()

# These netlists have genuine defects, each confirmed by inspection. They are
# listed rather than accommodated: loosening a check to make the suite green
# would throw away the finding. Every entry is a pin physically on two nets, or
# a part shorted across itself - defects that reached routing unnoticed because
# nothing before this checked electrical topology.
KNOWN_BAD = {
    # J4 pins 3-6 and J5 pins 3-4 each sit on two nets, shorting Arduino
    # digital pins 4/8, 5/9, 6/10, 7/11 and analog A0/A4, A1/A5.
    "arduino_nano_but_type_c_netlist",
    # U1 pin 16 on both USB_D- and ICSP_MISO; pin 17 on both ICSP_MOSI and
    # ICSP_SCK; J2 pin 6 on both ICSP_MISO and ICSP_SCK.
    "arduino_uno_328_lqfp_netlist",
    # U1 pin 16 on both D9_PWM and D11_PWM.
    "test_gather_uno_typec_netlist",
    # J2 pin 2 on both ICSP_SCK and ICSP_MOSI.
    "test_gather_v6_netlist",
    # R2's two pins are both on NEOPIXEL_DATA, so the data-line series resistor
    # is bypassed and does nothing.
    "led_blinker_circuit_with_a_neopixel_and__netlist",
    # 5ohm series resistor drives a 20mA LED at 86.7mA on a 3.3V rail.
    "layout_test_simple2_netlist",
    "local_model_test_netlist",
}


@pytest.mark.parametrize("name,path,data", CORPUS, ids=[c[0] for c in CORPUS])
def test_shipped_netlists_report_no_new_errors(name, path, data):
    """Every netlist here passed DRC and routed, so an error is suspect."""
    components, ports, nets = build_lookups(data["elements"])

    req_path = Path(path).with_name(
        Path(path).name.replace("_netlist.json", "_requirements.json")
    )
    volts = None
    if req_path.exists():
        try:
            raw = (json.loads(req_path.read_text()).get("power") or {}).get("voltage")
            volts = parse_supply_voltage(raw) if raw else None
        except (OSError, ValueError):
            volts = None

    solution = solve_circuit(components, ports, nets, volts)
    errors = []
    for check in (
        drc_checks.check_resistor_power,
        drc_checks.check_led_current,
        drc_checks.check_rail_voltage,
        drc_checks.check_circuit_integrity,
    ):
        errs, _warns = check(components, ports, nets, volts, solution)
        errors.extend(errs)

    if name in KNOWN_BAD:
        assert errors, f"{name} is listed as known-bad but now reports clean"
    else:
        assert errors == [], f"{name} produced errors:\n  " + "\n  ".join(errors)


def test_corpus_is_actually_present():
    """Guard against the parametrised suite silently covering nothing."""
    assert len(CORPUS) > 40
