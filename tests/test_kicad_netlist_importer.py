"""Tests for exporters/kicad_netlist_importer.py"""

from __future__ import annotations

import json
import sys
import textwrap
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from exporters.kicad_netlist_importer import (
    convert_kicad_netlist,
    _infer_component_type,
    _infer_net_class,
    _infer_electrical_type,
    _strip_footprint_library,
    _net_id,
)


# ---------------------------------------------------------------------------
# Minimal .net fixture (mirrors the led_blinker_smd example)
# ---------------------------------------------------------------------------

MINIMAL_NET = textwrap.dedent("""\
    (export (version "E")
      (design
        (source "test.kicad_sch")
        (date "2024-01-01 00:00:00")
        (tool "test")
      )
      (components
        (comp (ref "J1")
          (value "5V_IN")
          (footprint "TerminalBlock:TerminalBlock_2.54mm_2x1")
        )
        (comp (ref "U1")
          (value "ATtiny13A")
          (footprint "Package_SO:SOIC-8_3.9x4.9mm_P1.27mm")
        )
        (comp (ref "C1")
          (value "100nF")
          (footprint "Capacitor_SMD:C_0805_2012Metric")
        )
        (comp (ref "R1")
          (value "470")
          (footprint "Resistor_SMD:R_0805_2012Metric")
        )
        (comp (ref "LED1")
          (value "LED")
          (footprint "LED_SMD:LED_0805_2012Metric")
        )
      )
      (nets
        (net (code "1") (name "VCC")
          (node (ref "J1") (pin "1"))
          (node (ref "U1") (pin "8"))
          (node (ref "C1") (pin "1"))
          (node (ref "R1") (pin "1"))
        )
        (net (code "2") (name "GND")
          (node (ref "J1") (pin "2"))
          (node (ref "U1") (pin "4"))
          (node (ref "C1") (pin "2"))
        )
        (net (code "3") (name "LED_SIG")
          (node (ref "U1") (pin "5"))
          (node (ref "R1") (pin "2"))
          (node (ref "LED1") (pin "2"))
        )
        (net (code "4") (name "BTN")
          (node (ref "U1") (pin "6"))
        )
      )
    )
""")


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def _convert_string(net_text: str, project_name: str = "test_proj", tmp_path=None) -> dict:
    """Write net_text to a temp file and run convert_kicad_netlist."""
    p = tmp_path / "board.net"
    p.write_text(net_text)
    return convert_kicad_netlist(str(p), project_name=project_name)


# ---------------------------------------------------------------------------
# Unit tests: inference helpers
# ---------------------------------------------------------------------------

class TestInferComponentType:
    def test_resistor(self):   assert _infer_component_type("R1")    == "resistor"
    def test_capacitor(self):  assert _infer_component_type("C12")   == "capacitor"
    def test_inductor(self):   assert _infer_component_type("L3")    == "inductor"
    def test_led(self):        assert _infer_component_type("LED1")  == "led"
    def test_diode(self):      assert _infer_component_type("D1")    == "diode"
    def test_transistor(self): assert _infer_component_type("Q2")    == "transistor_npn"
    def test_ic(self):         assert _infer_component_type("U1")    == "ic"
    def test_ic2(self):        assert _infer_component_type("IC3")   == "ic"
    def test_crystal(self):    assert _infer_component_type("Y1")    == "crystal"
    def test_crystal_x(self):  assert _infer_component_type("X1")   == "crystal"
    def test_connector_j(self):assert _infer_component_type("J1")   == "connector"
    def test_connector_p(self):assert _infer_component_type("P2")   == "connector"
    def test_switch(self):     assert _infer_component_type("SW1")  == "switch"
    def test_relay(self):      assert _infer_component_type("K1")   == "relay"
    def test_fuse(self):       assert _infer_component_type("F1")   == "fuse"
    def test_unknown(self):    assert _infer_component_type("MODULE1") == "ic"

    # Connector designator prefixes beyond J/P/CN (morgan_carrier_v14 had these
    # mis-classified as "ic", so the optimizer relocated them off the edge).
    def test_terminal_block(self): assert _infer_component_type("TB1")  == "connector"
    def test_header(self):         assert _infer_component_type("HDR1") == "connector"
    def test_swd(self):            assert _infer_component_type("SWD1") == "connector"
    def test_swd_not_switch(self): assert _infer_component_type("SWD2") != "switch"

    # Footprint keywords override the designator prefix — even an unconventional
    # designator is a connector if its footprint is a TerminalBlock/Connector/FFC.
    def test_pkg_terminalblock(self):
        assert _infer_component_type("X9",
            "TerminalBlock_Phoenix_MKDS-1,5-2-5.08_1x02_P5.08mm_Horizontal") == "connector"
    def test_pkg_ffc_hirose(self):
        assert _infer_component_type("CN1", "FH35-30S-0.5SV_52") == "connector"
    def test_pkg_molex(self):
        assert _infer_component_type("TB1",
            "Molex_Micro-Fit_3.0_43650-0600_1x06_P3.00mm_Horizontal") == "connector"
    def test_pkg_does_not_override_ic(self):
        # A normal IC footprint keeps the prefix-based type.
        assert _infer_component_type("U1", "TI_SO-PowerPAD-8") == "ic"


class TestInferNetClass:
    def test_gnd(self):        assert _infer_net_class("GND")    == "ground"
    def test_agnd(self):       assert _infer_net_class("AGND")   == "ground"
    def test_vcc(self):        assert _infer_net_class("VCC")    == "power"
    def test_vdd(self):        assert _infer_net_class("/VDD")   == "power"
    def test_5v(self):         assert _infer_net_class("+5V")    == "power"
    def test_3v3(self):        assert _infer_net_class("3V3")    == "power"
    def test_signal(self):     assert _infer_net_class("SCK")    == "signal"
    def test_net_name(self):   assert _infer_net_class("LED_SIG") == "signal"


class TestInferElectricalType:
    def test_ground_pin(self):
        assert _infer_electrical_type("ground", "ic") == "ground"
    def test_power_ic(self):
        assert _infer_electrical_type("power", "ic") == "power_in"
    def test_power_connector(self):
        assert _infer_electrical_type("power", "connector") == "power_out"
    def test_signal_passive(self):
        assert _infer_electrical_type("signal", "resistor") == "passive"
    def test_signal_ic(self):
        assert _infer_electrical_type("signal", "ic") == "signal"


class TestStripFootprintLibrary:
    def test_strips(self):
        assert _strip_footprint_library("Resistor_SMD:R_0805_2012Metric") == "R_0805_2012Metric"
    def test_no_prefix(self):
        assert _strip_footprint_library("R_0805_2012Metric") == "R_0805_2012Metric"
    def test_empty(self):
        assert _strip_footprint_library("") == ""


class TestNetId:
    def test_basic(self):
        seen: set[str] = set()
        assert _net_id("VCC", seen) == "net_vcc"
    def test_special_chars(self):
        # "+5V" → sanitized to "5v" (non-alphanum stripped) → starts with digit → "n5v"
        seen: set[str] = set()
        assert _net_id("+5V", seen) == "net_n5v"
    def test_uniqueness(self):
        seen: set[str] = set()
        id1 = _net_id("SIG", seen)
        id2 = _net_id("SIG", seen)
        assert id1 != id2
        assert id2 == "net_sig_2"
    def test_leading_slash(self):
        seen: set[str] = set()
        assert _net_id("/VDD", seen) == "net_vdd"


# ---------------------------------------------------------------------------
# Integration tests: .net conversion
# ---------------------------------------------------------------------------

class TestDotNetConversion:
    def test_returns_dict_with_expected_keys(self, tmp_path):
        result = _convert_string(MINIMAL_NET, tmp_path=tmp_path)
        assert "netlist" in result
        assert "warnings" in result
        assert "source" in result

    def test_schema_top_level(self, tmp_path):
        netlist = _convert_string(MINIMAL_NET, tmp_path=tmp_path)["netlist"]
        assert netlist["version"] == "1.0"
        assert netlist["project_name"] == "test_proj"
        assert isinstance(netlist["elements"], list)
        assert len(netlist["elements"]) > 0

    def test_component_count(self, tmp_path):
        elements = _convert_string(MINIMAL_NET, tmp_path=tmp_path)["netlist"]["elements"]
        comps = [e for e in elements if e["element_type"] == "component"]
        assert len(comps) == 5  # J1 U1 C1 R1 LED1

    def test_component_ids_unique(self, tmp_path):
        elements = _convert_string(MINIMAL_NET, tmp_path=tmp_path)["netlist"]["elements"]
        ids = [e["component_id"] for e in elements if e["element_type"] == "component"]
        assert len(ids) == len(set(ids))

    def test_component_types(self, tmp_path):
        elements = _convert_string(MINIMAL_NET, tmp_path=tmp_path)["netlist"]["elements"]
        by_des = {e["designator"]: e for e in elements if e["element_type"] == "component"}
        assert by_des["J1"]["component_type"] == "connector"
        assert by_des["U1"]["component_type"] == "ic"
        assert by_des["C1"]["component_type"] == "capacitor"
        assert by_des["R1"]["component_type"] == "resistor"
        assert by_des["LED1"]["component_type"] == "led"

    def test_footprint_library_stripped(self, tmp_path):
        elements = _convert_string(MINIMAL_NET, tmp_path=tmp_path)["netlist"]["elements"]
        by_des = {e["designator"]: e for e in elements if e["element_type"] == "component"}
        assert by_des["C1"]["package"] == "C_0805_2012Metric"
        assert by_des["R1"]["package"] == "R_0805_2012Metric"

    def test_net_count(self, tmp_path):
        # BTN has only 1 node → skipped; VCC, GND, LED_SIG have 2+ → 3 nets
        elements = _convert_string(MINIMAL_NET, tmp_path=tmp_path)["netlist"]["elements"]
        nets = [e for e in elements if e["element_type"] == "net"]
        assert len(nets) == 3

    def test_net_classes(self, tmp_path):
        elements = _convert_string(MINIMAL_NET, tmp_path=tmp_path)["netlist"]["elements"]
        by_name = {e["name"]: e for e in elements if e["element_type"] == "net"}
        assert by_name["VCC"]["net_class"] == "power"
        assert by_name["GND"]["net_class"] == "ground"
        assert by_name["LED_SIG"]["net_class"] == "signal"

    def test_net_ids_unique(self, tmp_path):
        elements = _convert_string(MINIMAL_NET, tmp_path=tmp_path)["netlist"]["elements"]
        ids = [e["net_id"] for e in elements if e["element_type"] == "net"]
        assert len(ids) == len(set(ids))

    def test_port_ids_unique(self, tmp_path):
        elements = _convert_string(MINIMAL_NET, tmp_path=tmp_path)["netlist"]["elements"]
        ids = [e["port_id"] for e in elements if e["element_type"] == "port"]
        assert len(ids) == len(set(ids))

    def test_port_ids_referenced_in_nets(self, tmp_path):
        elements = _convert_string(MINIMAL_NET, tmp_path=tmp_path)["netlist"]["elements"]
        known_ports = {e["port_id"] for e in elements if e["element_type"] == "port"}
        for net in elements:
            if net["element_type"] != "net":
                continue
            for pid in net["connected_port_ids"]:
                assert pid in known_ports, f"Net '{net['name']}' references unknown port '{pid}'"

    def test_electrical_types_refined(self, tmp_path):
        """Ports on GND net should be typed 'ground', VCC → 'power_in' for ICs."""
        elements = _convert_string(MINIMAL_NET, tmp_path=tmp_path)["netlist"]["elements"]
        by_id = {e["port_id"]: e for e in elements if e["element_type"] == "port"}
        # U1 pin 4 is on GND
        assert by_id["port_u1_4"]["electrical_type"] == "ground"
        # U1 pin 8 is on VCC → IC receives power
        assert by_id["port_u1_8"]["electrical_type"] == "power_in"
        # J1 pin 1 is on VCC → connector supplies power
        assert by_id["port_j1_1"]["electrical_type"] == "power_out"

    def test_single_node_net_warned(self, tmp_path):
        """BTN net has only 1 node → should appear in warnings, not in netlist."""
        result = _convert_string(MINIMAL_NET, tmp_path=tmp_path)
        nets = [e for e in result["netlist"]["elements"] if e["element_type"] == "net"]
        net_names = {n["name"] for n in nets}
        assert "BTN" not in net_names
        assert any("BTN" in w for w in result["warnings"])

    def test_pwr_symbols_skipped(self, tmp_path):
        """KiCad #PWR symbols must not appear as components."""
        net_with_pwr = MINIMAL_NET.replace(
            "(comp (ref \"J1\")",
            "(comp (ref \"#PWR01\")\n          (value \"VCC\")\n          (footprint \"\")\n        )\n        (comp (ref \"J1\")",
        )
        elements = _convert_string(net_with_pwr, tmp_path=tmp_path)["netlist"]["elements"]
        refs = [e["designator"] for e in elements if e["element_type"] == "component"]
        assert "#PWR01" not in refs


# ---------------------------------------------------------------------------
# .kicad_sch path — requires sibling .net
# ---------------------------------------------------------------------------

class TestKicadSchPath:
    def test_sch_with_sibling_net(self, tmp_path):
        """Should succeed when a sibling .net exists."""
        (tmp_path / "board.net").write_text(MINIMAL_NET)
        (tmp_path / "board.kicad_sch").write_text("(kicad_sch (version 20240108))")
        result = convert_kicad_netlist(str(tmp_path / "board.kicad_sch"), project_name="sch_test")
        assert result["netlist"]["project_name"] == "sch_test"
        assert len(result["warnings"]) >= 1  # sibling-net notice

    def test_sch_without_net_raises(self, tmp_path):
        """Should raise ValueError with helpful message when no sibling .net exists."""
        (tmp_path / "board.kicad_sch").write_text("(kicad_sch (version 20240108))")
        with pytest.raises(ValueError, match="File → Export → Netlist"):
            convert_kicad_netlist(str(tmp_path / "board.kicad_sch"), project_name="sch_test")


# ---------------------------------------------------------------------------
# Error cases
# ---------------------------------------------------------------------------

class TestErrors:
    def test_missing_file(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            convert_kicad_netlist(str(tmp_path / "nonexistent.net"))

    def test_unsupported_extension(self, tmp_path):
        p = tmp_path / "board.brd"
        p.write_text("dummy")
        with pytest.raises(ValueError, match="Unsupported file type"):
            convert_kicad_netlist(str(p))

    def test_empty_net_file(self, tmp_path):
        p = tmp_path / "empty.net"
        p.write_text("(export (version \"E\") (components) (nets))")
        with pytest.raises(ValueError, match="No components"):
            convert_kicad_netlist(str(p))


# ---------------------------------------------------------------------------
# Round-trip: output is valid JSON matching schema constraints
# ---------------------------------------------------------------------------

class TestSchemaConstraints:
    def test_component_id_pattern(self, tmp_path):
        import re
        elements = _convert_string(MINIMAL_NET, tmp_path=tmp_path)["netlist"]["elements"]
        for e in elements:
            if e["element_type"] == "component":
                assert re.match(r"^comp_[a-z][a-z0-9_]*$", e["component_id"]), e
            elif e["element_type"] == "port":
                assert re.match(r"^port_[a-z][a-z0-9_]*$", e["port_id"]), e
            elif e["element_type"] == "net":
                assert re.match(r"^net_[a-z0-9][a-z0-9_]*$", e["net_id"]), e

    def test_component_type_in_enum(self, tmp_path):
        from exporters.kicad_netlist_importer import _VALID_COMPONENT_TYPES
        elements = _convert_string(MINIMAL_NET, tmp_path=tmp_path)["netlist"]["elements"]
        for e in elements:
            if e["element_type"] == "component":
                assert e["component_type"] in _VALID_COMPONENT_TYPES, e

    def test_net_class_in_enum(self, tmp_path):
        elements = _convert_string(MINIMAL_NET, tmp_path=tmp_path)["netlist"]["elements"]
        for e in elements:
            if e["element_type"] == "net":
                assert e["net_class"] in ("signal", "power", "ground"), e

    def test_electrical_type_in_enum(self, tmp_path):
        elements = _convert_string(MINIMAL_NET, tmp_path=tmp_path)["netlist"]["elements"]
        valid = {"power_in", "power_out", "signal", "ground", "passive", "no_connect"}
        for e in elements:
            if e["element_type"] == "port":
                assert e["electrical_type"] in valid, e

    def test_serializable_to_json(self, tmp_path):
        netlist = _convert_string(MINIMAL_NET, tmp_path=tmp_path)["netlist"]
        dumped = json.dumps(netlist)
        reloaded = json.loads(dumped)
        assert reloaded["version"] == "1.0"

    def test_real_file_if_available(self):
        """Smoke-test against the real led_blinker_smd.net if present."""
        real = Path(__file__).parent.parent.parent / "pcb-design-debug" / "led_blinker_smd" / "led_blinker_smd.net"
        if not real.exists():
            pytest.skip("led_blinker_smd.net not available")
        result = convert_kicad_netlist(str(real), project_name="led_blinker_smd")
        nl = result["netlist"]
        comps = [e for e in nl["elements"] if e["element_type"] == "component"]
        nets  = [e for e in nl["elements"] if e["element_type"] == "net"]
        assert len(comps) == 6   # J1 U1 SW1 C1 R1 LED1
        assert len(nets)  == 3   # VCC GND LED (BTN has 1 node → skipped)
