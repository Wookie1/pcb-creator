"""Line-coverage tests for orchestrator/{circuit_builder,config,cache}.py.

Targets uncovered branches not already exercised by the placement/agent-sim
tests: builder validation/error paths and pin-name resolution, config env-var
overrides + KiCad-library autodetect, and the component cache round-trip /
corrupt-file handling. Pure data manipulation — no LLM.
"""

import json
from pathlib import Path

import pytest

from orchestrator import circuit_builder as cb
from orchestrator.config import OrchestratorConfig, _autodetect_kicad_library
from orchestrator.cache import ComponentCache
from optimizers.pad_geometry import get_footprint_def


# ---------------------------------------------------------------------------
# circuit_builder
# ---------------------------------------------------------------------------

def _fp(package, pin_count):
    """Footprint lookup that always resolves (so add_component passes the gate)."""
    return get_footprint_def(package, pin_count) or get_footprint_def("0805", 2)


class TestCreateDraft:
    def test_bad_project_name(self, tmp_path):
        r = cb.create_draft(tmp_path, "Bad-Name", "x", 30, 20)
        assert not r["ok"] and r["code"] == "bad_project_name"

    def test_bad_layers(self, tmp_path):
        r = cb.create_draft(tmp_path, "p", "x", 30, 20, layers=3)
        assert not r["ok"] and r["code"] == "bad_layers"

    def test_bad_board_not_number(self, tmp_path):
        r = cb.create_draft(tmp_path, "p", "x", "wide", 20)
        assert not r["ok"] and r["code"] == "bad_board"

    def test_bad_board_out_of_range(self, tmp_path):
        r = cb.create_draft(tmp_path, "p", "x", 2, 20)
        assert not r["ok"] and r["code"] == "bad_board"

    def test_exists_without_overwrite(self, tmp_path):
        assert cb.create_draft(tmp_path, "p", "x", 30, 20)["ok"]
        r = cb.create_draft(tmp_path, "p", "x", 30, 20)
        assert not r["ok"] and r["code"] == "draft_exists"

    def test_overwrite_replaces(self, tmp_path):
        pdir = tmp_path / "proj"
        assert cb.create_draft(pdir, "p", "first", 30, 20)["ok"]
        (pdir / "stale.txt").write_text("old")
        r = cb.create_draft(pdir, "p", "second", 40, 30, overwrite=True)
        assert r["ok"]
        assert not (pdir / "stale.txt").exists()  # dir was wiped
        assert cb.load_draft(pdir, "p")["description"] == "second"


class TestAddComponent:
    def test_no_draft(self, tmp_path):
        r = cb.add_component(tmp_path, "p", "R1", "resistor", "1k", "0805")
        assert not r["ok"] and r["code"] == "no_draft"

    def _draft(self, tmp_path):
        cb.create_draft(tmp_path, "p", "x", 30, 20)

    def test_bad_designator(self, tmp_path):
        self._draft(tmp_path)
        r = cb.add_component(tmp_path, "p", "1R", "resistor", "1k", "0805")
        assert not r["ok"] and r["code"] == "bad_designator"

    def test_duplicate_designator(self, tmp_path):
        self._draft(tmp_path)
        assert cb.add_component(tmp_path, "p", "R1", "resistor", "1k", "0805",
                                footprint_lookup=_fp)["ok"]
        r = cb.add_component(tmp_path, "p", "R1", "resistor", "1k", "0805",
                             footprint_lookup=_fp)
        assert not r["ok"] and r["code"] == "duplicate_designator"

    def test_bad_type(self, tmp_path):
        self._draft(tmp_path)
        r = cb.add_component(tmp_path, "p", "R1", "widget", "1k", "0805")
        assert not r["ok"] and r["code"] == "bad_type"

    def test_bad_value(self, tmp_path):
        self._draft(tmp_path)
        r = cb.add_component(tmp_path, "p", "R1", "resistor", "", "0805")
        assert not r["ok"] and r["code"] == "bad_value"

    def test_bad_package(self, tmp_path):
        self._draft(tmp_path)
        r = cb.add_component(tmp_path, "p", "R1", "resistor", "1k", "")
        assert not r["ok"] and r["code"] == "bad_package"

    def test_bad_pinout(self, tmp_path):
        self._draft(tmp_path)
        r = cb.add_component(tmp_path, "p", "U1", "ic", "NE555", "DIP-8",
                             pinout="garbage", footprint_lookup=_fp)
        assert not r["ok"] and r["code"] == "bad_pinout"

    def test_pinout_resolves_names_and_count(self, tmp_path):
        self._draft(tmp_path)
        r = cb.add_component(tmp_path, "p", "U1", "ic", "NE555", "DIP-8",
                             pinout="1:GND 2:TRIG 3:OUT 4:RESET 5:CTRL "
                                    "6:THRES 7:DISCH 8:VCC",
                             footprint_lookup=_fp)
        assert r["ok"] and r["pin_count"] == 8
        # pin names came through
        assert any(p.get("name") == "GND" for p in r["pins"])

    def test_bad_pin_count_not_int(self, tmp_path):
        self._draft(tmp_path)
        r = cb.add_component(tmp_path, "p", "U1", "ic", "x", "DIP-8",
                             pin_count="eight", footprint_lookup=_fp)
        assert not r["ok"] and r["code"] == "bad_pin_count"

    def test_bad_pin_count_range(self, tmp_path):
        self._draft(tmp_path)
        r = cb.add_component(tmp_path, "p", "U1", "ic", "x", "DIP-8",
                             pin_count=0, footprint_lookup=_fp)
        assert not r["ok"] and r["code"] == "bad_pin_count"

    def test_unknown_pin_count(self, tmp_path):
        self._draft(tmp_path)
        # ic has no default pin count and 'MYSTERY' won't resolve a count
        r = cb.add_component(tmp_path, "p", "U1", "ic", "x", "MYSTERY")
        assert not r["ok"] and r["code"] == "unknown_pin_count"

    def test_unresolved_footprint(self, tmp_path):
        self._draft(tmp_path)
        # lookup that never resolves → footprint gate fires
        r = cb.add_component(tmp_path, "p", "R1", "resistor", "1k", "WEIRD-PKG",
                             pin_count=2, footprint_lookup=lambda p, n: None)
        assert not r["ok"] and r["code"] == "unresolved_footprint"

    def test_functional_group_carried(self, tmp_path):
        self._draft(tmp_path)
        r = cb.add_component(tmp_path, "p", "U1", "ic", "x", "DIP-8",
                             pin_count=8, functional_group=" power ",
                             footprint_lookup=_fp)
        assert r["ok"]
        comp = cb.load_draft(tmp_path, "p")["components"]["U1"]
        assert comp["functional_group"] == "power"  # stripped

    def test_default_pin_count_from_type(self, tmp_path):
        self._draft(tmp_path)
        # resistor has a type default of 2; package won't resolve a count
        r = cb.add_component(tmp_path, "p", "R1", "resistor", "1k", "MYSTERY",
                             footprint_lookup=_fp)
        assert r["ok"] and r["pin_count"] == 2


class TestRemoveComponent:
    def test_no_draft(self, tmp_path):
        r = cb.remove_component(tmp_path, "p", "R1")
        assert not r["ok"] and r["code"] == "no_draft"

    def test_unknown_designator(self, tmp_path):
        cb.create_draft(tmp_path, "p", "x", 30, 20)
        r = cb.remove_component(tmp_path, "p", "R1")
        assert not r["ok"] and r["code"] == "unknown_designator"

    def test_removes_and_prunes_nets(self, tmp_path):
        cb.create_draft(tmp_path, "p", "x", 30, 20)
        cb.add_component(tmp_path, "p", "R1", "resistor", "1k", "0805",
                         footprint_lookup=_fp)
        cb.add_component(tmp_path, "p", "R2", "resistor", "2k", "0805",
                         footprint_lookup=_fp)
        cb.connect_pins(tmp_path, "p", "N1", ["R1.1", "R2.1"])
        cb.mark_no_connect(tmp_path, "p", ["R1.2"])
        r = cb.remove_component(tmp_path, "p", "R1")
        assert r["ok"] and "N1" in r["removed_from_nets"]
        draft = cb.load_draft(tmp_path, "p")
        # net N1 had only R1.1 + R2.1; R1.1 removed leaves R2.1 (still present)
        assert "N1" in draft["nets"]
        assert draft["no_connect"] == []  # R1.2 pruned

    def test_net_deleted_when_emptied(self, tmp_path):
        cb.create_draft(tmp_path, "p", "x", 30, 20)
        cb.add_component(tmp_path, "p", "R1", "resistor", "1k", "0805",
                         footprint_lookup=_fp)
        cb.connect_pins(tmp_path, "p", "N1", ["R1.1", "R1.2"])
        cb.remove_component(tmp_path, "p", "R1")
        assert "N1" not in cb.load_draft(tmp_path, "p")["nets"]


class TestConnectPins:
    def _two_res(self, tmp_path):
        cb.create_draft(tmp_path, "p", "x", 30, 20)
        cb.add_component(tmp_path, "p", "R1", "resistor", "1k", "0805",
                         footprint_lookup=_fp)
        cb.add_component(tmp_path, "p", "R2", "resistor", "2k", "0805",
                         footprint_lookup=_fp)

    def test_no_draft(self, tmp_path):
        r = cb.connect_pins(tmp_path, "p", "N1", ["R1.1"])
        assert not r["ok"] and r["code"] == "no_draft"

    def test_bad_net_name(self, tmp_path):
        self._two_res(tmp_path)
        r = cb.connect_pins(tmp_path, "p", "  ", ["R1.1", "R2.1"])
        assert not r["ok"] and r["code"] == "bad_net_name"

    def test_bad_net_class(self, tmp_path):
        self._two_res(tmp_path)
        r = cb.connect_pins(tmp_path, "p", "N1", ["R1.1"], net_class="bogus")
        assert not r["ok"] and r["code"] == "bad_net_class"

    def test_no_pins(self, tmp_path):
        self._two_res(tmp_path)
        r = cb.connect_pins(tmp_path, "p", "N1", [])
        assert not r["ok"] and r["code"] == "no_pins"

    def test_bad_pin_token(self, tmp_path):
        self._two_res(tmp_path)
        r = cb.connect_pins(tmp_path, "p", "N1", ["bogus"])
        assert not r["ok"] and r["code"] == "bad_pin"

    def test_pin_conflict(self, tmp_path):
        self._two_res(tmp_path)
        cb.connect_pins(tmp_path, "p", "N1", ["R1.1", "R2.1"])
        r = cb.connect_pins(tmp_path, "p", "N2", ["R1.1", "R2.2"])
        assert not r["ok"] and r["code"] == "pin_conflict"
        assert r["existing_net"] == "N1"

    def test_idempotent_and_explicit_class_update(self, tmp_path):
        self._two_res(tmp_path)
        cb.connect_pins(tmp_path, "p", "N1", ["R1.1", "R2.1"])
        # re-connect same pins → already_connected, and update net_class
        r = cb.connect_pins(tmp_path, "p", "N1", ["R1.1"], net_class="power")
        assert r["ok"] and r["already_connected"] == ["R1.1"]
        assert r["net_class"] == "power"

    def test_clears_no_connect(self, tmp_path):
        self._two_res(tmp_path)
        cb.mark_no_connect(tmp_path, "p", ["R1.1"])
        cb.connect_pins(tmp_path, "p", "N1", ["R1.1", "R2.1"])
        assert "R1.1" not in cb.load_draft(tmp_path, "p")["no_connect"]


class TestDisconnectPins:
    def test_no_draft(self, tmp_path):
        r = cb.disconnect_pins(tmp_path, "p", "N1", ["R1.1"])
        assert not r["ok"] and r["code"] == "no_draft"

    def test_unknown_net(self, tmp_path):
        cb.create_draft(tmp_path, "p", "x", 30, 20)
        r = cb.disconnect_pins(tmp_path, "p", "N1", ["R1.1"])
        assert not r["ok"] and r["code"] == "unknown_net"

    def test_bad_pin(self, tmp_path):
        cb.create_draft(tmp_path, "p", "x", 30, 20)
        cb.add_component(tmp_path, "p", "R1", "resistor", "1k", "0805",
                         footprint_lookup=_fp)
        cb.add_component(tmp_path, "p", "R2", "resistor", "2k", "0805",
                         footprint_lookup=_fp)
        cb.connect_pins(tmp_path, "p", "N1", ["R1.1", "R2.1"])
        r = cb.disconnect_pins(tmp_path, "p", "N1", ["bogus"])
        assert not r["ok"] and r["code"] == "bad_pin"

    def test_disconnect_and_delete_empty_net(self, tmp_path):
        cb.create_draft(tmp_path, "p", "x", 30, 20)
        cb.add_component(tmp_path, "p", "R1", "resistor", "1k", "0805",
                         footprint_lookup=_fp)
        cb.connect_pins(tmp_path, "p", "N1", ["R1.1", "R1.2"])
        r = cb.disconnect_pins(tmp_path, "p", "N1", ["R1.1", "R1.2"])
        assert r["ok"] and set(r["removed"]) == {"R1.1", "R1.2"}
        assert r["net_deleted"]


class TestMarkNoConnect:
    def test_no_draft(self, tmp_path):
        r = cb.mark_no_connect(tmp_path, "p", ["R1.1"])
        assert not r["ok"] and r["code"] == "no_draft"

    def test_bad_pin(self, tmp_path):
        cb.create_draft(tmp_path, "p", "x", 30, 20)
        r = cb.mark_no_connect(tmp_path, "p", ["bogus"])
        assert not r["ok"] and r["code"] == "bad_pin"

    def test_pin_connected_rejected(self, tmp_path):
        cb.create_draft(tmp_path, "p", "x", 30, 20)
        cb.add_component(tmp_path, "p", "R1", "resistor", "1k", "0805",
                         footprint_lookup=_fp)
        cb.connect_pins(tmp_path, "p", "N1", ["R1.1", "R1.2"])
        r = cb.mark_no_connect(tmp_path, "p", ["R1.1"])
        assert not r["ok"] and r["code"] == "pin_connected"


class TestResolvePinToken:
    """Drive _resolve_pin_token edge cases via connect_pins error text."""

    def _ic(self, tmp_path):
        cb.create_draft(tmp_path, "p", "x", 30, 20)
        # Two VCC pins + alt name to exercise ordinal + alt + prefix paths
        cb.add_component(tmp_path, "p", "U1", "ic", "x", "DIP-8",
                         pinout="1:GND 2:IN+ 3:OUT 4:NC 5:NC 6:VCC 7:VCC 8:EN/CS",
                         footprint_lookup=_fp)
        cb.add_component(tmp_path, "p", "R1", "resistor", "1k", "0805",
                         footprint_lookup=_fp)

    def test_malformed_token(self, tmp_path):
        self._ic(tmp_path)
        r = cb.connect_pins(tmp_path, "p", "N", ["U1"])
        assert "malformed" in r["error"]

    def test_unknown_component(self, tmp_path):
        self._ic(tmp_path)
        r = cb.connect_pins(tmp_path, "p", "N", ["Z9.1"])
        assert "Unknown component" in r["error"]

    def test_pin_number_out_of_range(self, tmp_path):
        self._ic(tmp_path)
        r = cb.connect_pins(tmp_path, "p", "N", ["U1.99"])
        assert "out of" in r["error"]

    def test_exact_name(self, tmp_path):
        self._ic(tmp_path)
        r = cb.connect_pins(tmp_path, "p", "GND", ["U1.GND", "R1.1"])
        assert r["ok"] and "U1.1" in r["net_pins"]

    def test_ordinal_duplicate_name(self, tmp_path):
        self._ic(tmp_path)
        # VCC2 → the 2nd pin named VCC (pin 7)
        r = cb.connect_pins(tmp_path, "p", "PWR", ["U1.VCC2", "R1.1"])
        assert r["ok"] and "U1.7" in r["net_pins"]

    def test_unique_prefix(self, tmp_path):
        self._ic(tmp_path)
        # 'EN' is a unique prefix of 'EN/CS' (pin 8)
        r = cb.connect_pins(tmp_path, "p", "ENA", ["U1.EN", "R1.1"])
        assert r["ok"] and "U1.8" in r["net_pins"]

    def test_ambiguous_prefix(self, tmp_path):
        self._ic(tmp_path)
        # 'VC' prefixes both VCC pins → ambiguous
        r = cb.connect_pins(tmp_path, "p", "N", ["U1.VC"])
        assert not r["ok"] and "ambiguous" in r["error"]

    def test_no_such_name(self, tmp_path):
        self._ic(tmp_path)
        r = cb.connect_pins(tmp_path, "p", "N", ["U1.FOOBAR"])
        assert not r["ok"] and "no pin named" in r["error"]

    def test_unique_prefix_no_exact(self, tmp_path):
        # 'RES' is not an exact name but uniquely prefixes 'RESET' (pin 4),
        # so it resolves via the prefix branch (no ambiguity, no exact match).
        cb.create_draft(tmp_path, "p", "x", 30, 20)
        cb.add_component(tmp_path, "p", "U1", "ic", "x", "DIP-8",
                         pinout="1:GND 2:TRIG 3:OUT 4:RESET 5:CTRL "
                                "6:THRES 7:DISCH 8:VCC",
                         footprint_lookup=_fp)
        cb.add_component(tmp_path, "p", "R1", "resistor", "1k", "0805",
                         footprint_lookup=_fp)
        r = cb.connect_pins(tmp_path, "p", "N", ["U1.RES", "R1.1"])
        assert r["ok"] and "U1.4" in r["net_pins"]

    def test_alt_name_match(self, tmp_path):
        cb.create_draft(tmp_path, "p", "x", 30, 20)
        # slash form → parse_pinout records ADJ as an alt name of pin 2
        cb.add_component(tmp_path, "p", "U1", "voltage_regulator", "LM7805",
                         "TO-220", pinout="1:IN 2:GND/ADJ 3:OUT",
                         footprint_lookup=_fp)
        cb.add_component(tmp_path, "p", "R1", "resistor", "1k", "0805",
                         footprint_lookup=_fp)
        r = cb.connect_pins(tmp_path, "p", "N", ["U1.ADJ", "R1.1"])
        assert r["ok"] and "U1.2" in r["net_pins"]


class TestListAndFinalize:
    def _good_circuit(self, tmp_path):
        cb.create_draft(tmp_path, "p", "led", 30, 20)
        cb.add_component(tmp_path, "p", "R1", "resistor", "330ohm", "0805",
                         footprint_lookup=_fp)
        cb.add_component(tmp_path, "p", "D1", "led", "red", "0805",
                         footprint_lookup=_fp)
        cb.add_component(tmp_path, "p", "J1", "connector", "hdr",
                         "PinHeader_1x2", footprint_lookup=_fp)
        cb.connect_pins(tmp_path, "p", "VCC", ["J1.1", "R1.1"])
        cb.connect_pins(tmp_path, "p", "LED_DRIVE", ["R1.2", "D1.anode"])
        cb.connect_pins(tmp_path, "p", "GND", ["D1.cathode", "J1.2"])

    def test_list_circuit(self, tmp_path):
        self._good_circuit(tmp_path)
        out = cb.list_circuit(cb.load_draft(tmp_path, "p"))
        assert out["ok"]
        assert {c["designator"] for c in out["components"]} == {"R1", "D1", "J1"}
        assert out["unconnected_pins"] == []

    def test_finalize_no_draft(self, tmp_path):
        r = cb.finalize(tmp_path, "p")
        assert not r["ok"] and r["code"] == "no_draft"

    def test_finalize_empty(self, tmp_path):
        cb.create_draft(tmp_path, "p", "x", 30, 20)
        r = cb.finalize(tmp_path, "p")
        assert not r["ok"] and r["code"] == "empty"

    def test_finalize_unconnected_pins(self, tmp_path):
        cb.create_draft(tmp_path, "p", "x", 30, 20)
        cb.add_component(tmp_path, "p", "R1", "resistor", "1k", "0805",
                         footprint_lookup=_fp)
        r = cb.finalize(tmp_path, "p")
        assert not r["ok"] and r["code"] == "unconnected_pins"

    def test_finalize_single_pin_net(self, tmp_path):
        cb.create_draft(tmp_path, "p", "x", 30, 20)
        cb.add_component(tmp_path, "p", "R1", "resistor", "1k", "0805",
                         footprint_lookup=_fp)
        # connect both pins so none unconnected, but make a 1-pin net by
        # connecting then leave the other pin no-connect → can't; instead
        # build a genuine single-pin net by direct draft edit.
        draft = cb.load_draft(tmp_path, "p")
        draft["nets"]["SOLO"] = {"net_class": "signal", "pins": ["R1.1"]}
        draft["no_connect"] = ["R1.2"]
        cb._save_draft(tmp_path, "p", draft)
        r = cb.finalize(tmp_path, "p")
        assert not r["ok"] and r["code"] == "single_pin_nets"

    def test_finalize_validation_failed(self, tmp_path, monkeypatch):
        # add_component gates footprints/structure, so a clean draft normally
        # passes. Force the post-compile validator to report invalid to cover
        # the validation_failed return branch.
        self._good_circuit(tmp_path)
        monkeypatch.setattr(
            "validators.validate_netlist.validate_netlist",
            lambda path: {"valid": False, "errors": ["boom"], "warnings": []})
        r = cb.finalize(tmp_path, "p")
        assert not r["ok"] and r["code"] == "validation_failed"
        assert r["errors"] == ["boom"]

    def test_finalize_success(self, tmp_path):
        self._good_circuit(tmp_path)
        r = cb.finalize(tmp_path, "p")
        assert r["ok"], r
        assert r["component_count"] == 3 and r["net_count"] == 3
        nl = json.loads(Path(r["netlist_path"]).read_text())
        kinds = [e["element_type"] for e in nl["elements"]]
        assert kinds.count("net") == 3
        # functional_group emitted when present
        cb_comp = next(e for e in nl["elements"]
                       if e.get("designator") == "R1")
        assert cb_comp["component_type"] == "resistor"

    def test_finalize_net_id_digit_prefix_collision_nc_and_group(self, tmp_path):
        """Exercise _net_id digit-prefix (n-prefix), id-collision dedup,
        functional_group emission, and a no_connect port in finalize."""
        cb.create_draft(tmp_path, "p", "x", 30, 20)
        cb.add_component(tmp_path, "p", "U1", "ic", "x", "DIP-8", pin_count=8,
                         functional_group="power", footprint_lookup=_fp)
        cb.add_component(tmp_path, "p", "R1", "resistor", "1k", "0805",
                         footprint_lookup=_fp)
        # Two net names that normalise to the same id "net_3v3" → forces the
        # collision-dedup loop; name starts with a digit → forces n-prefix.
        cb.connect_pins(tmp_path, "p", "3V3", ["U1.1", "R1.1"])
        cb.connect_pins(tmp_path, "p", "3V3+", ["U1.2", "R1.2"])
        cb.connect_pins(tmp_path, "p", "GND", ["U1.3", "U1.4"])
        cb.connect_pins(tmp_path, "p", "S1", ["U1.5", "U1.6"])
        # one no-connect pin → finalize emits an element_type=port no_connect
        cb.mark_no_connect(tmp_path, "p", ["U1.7", "U1.8"])
        r = cb.finalize(tmp_path, "p")
        assert r["ok"], r
        nl = json.loads(Path(r["netlist_path"]).read_text())
        net_ids = [e["net_id"] for e in nl["elements"]
                   if e["element_type"] == "net"]
        assert "net_n3v3" in net_ids and "net_n3v3_2" in net_ids
        u1 = next(e for e in nl["elements"] if e.get("designator") == "U1")
        assert u1["functional_group"] == "power"
        nc = [e for e in nl["elements"]
              if e["element_type"] == "port"
              and e.get("electrical_type") == "no_connect"]
        assert len(nc) == 2


# ---------------------------------------------------------------------------
# config
# ---------------------------------------------------------------------------

# PCB_* env vars cleared so the host environment can't perturb assertions.
_PCB_VARS = [
    "PCB_GENERATE_MODEL", "PCB_REVIEW_MODEL", "PCB_GATHER_MODEL",
    "PCB_LLM_API_BASE", "PCB_LLM_API_KEY", "PCB_LLM_MAX_TOKENS",
    "PCB_LLM_TIMEOUT", "PCB_MAX_REWORK", "PCB_MODEL_PROFILE", "PCB_SKIP_QA",
    "PCB_ENABLE_OPTIMIZER", "PCB_OPTIMIZER_ITERATIONS", "PCB_OPTIMIZER_SEED",
    "PCB_ROUTER_ENGINE", "PCB_ESCAPE_FANOUT", "PCB_SHORT_CLEANUP",
    "PCB_FREEROUTING_JAR", "PCB_FREEROUTING_TIMEOUT", "PCB_KICAD_LIBRARY_PATH",
    "PCB_COMPONENT_CACHE_PATH", "PCB_LLM_ENRICHMENT_WORKERS", "PCB_VISION_MODEL",
    "PCB_VISION_MAX_ATTEMPTS", "PCB_3D_MODELS_DIR", "PCB_PROJECTS_DIR",
]


@pytest.fixture()
def clean_env(monkeypatch):
    for v in _PCB_VARS:
        monkeypatch.delenv(v, raising=False)
    return monkeypatch


class TestConfig:
    def test_defaults_and_resolve(self, clean_env, tmp_path):
        # no .env in tmp_path; no autodetect interference
        clean_env.setattr("orchestrator.config._autodetect_kicad_library",
                          lambda: None)
        cfg = OrchestratorConfig.from_env(base_dir=tmp_path)
        assert cfg.base_dir == tmp_path
        assert cfg.model_profile == "normal"
        assert cfg.short_cleanup is True
        assert cfg.escape_fanout is None
        assert cfg.kicad_library_path is None
        assert cfg.resolve("projects/x") == tmp_path / "projects/x"

    def test_env_overrides(self, clean_env, tmp_path):
        clean_env.setattr("orchestrator.config._autodetect_kicad_library",
                          lambda: None)
        env = {
            "PCB_GENERATE_MODEL": "gen-m", "PCB_REVIEW_MODEL": "rev-m",
            "PCB_GATHER_MODEL": "gat-m", "PCB_LLM_API_BASE": "http://x/v1",
            "PCB_LLM_API_KEY": "sk-1", "PCB_LLM_MAX_TOKENS": "1234",
            "PCB_LLM_TIMEOUT": "60", "PCB_MAX_REWORK": "9",
            "PCB_MODEL_PROFILE": "SMALL", "PCB_SKIP_QA": "yes",
            "PCB_ENABLE_OPTIMIZER": "false", "PCB_OPTIMIZER_ITERATIONS": "500",
            "PCB_OPTIMIZER_SEED": "42", "PCB_ROUTER_ENGINE": "builtin",
            "PCB_ESCAPE_FANOUT": "true", "PCB_SHORT_CLEANUP": "0",
            "PCB_FREEROUTING_JAR": "/jars/fr.jar",
            "PCB_FREEROUTING_TIMEOUT": "120",
            "PCB_KICAD_LIBRARY_PATH": "/libs/kicad",
            "PCB_COMPONENT_CACHE_PATH": "/cache/c.json",
            "PCB_LLM_ENRICHMENT_WORKERS": "8", "PCB_VISION_MODEL": "vm",
            "PCB_VISION_MAX_ATTEMPTS": "7", "PCB_3D_MODELS_DIR": "/models",
            "PCB_PROJECTS_DIR": "myprojects",
        }
        for k, v in env.items():
            clean_env.setenv(k, v)
        cfg = OrchestratorConfig.from_env(base_dir=tmp_path)
        assert cfg.generate_model == "gen-m"
        assert cfg.review_model == "rev-m"
        assert cfg.gather_model == "gat-m"
        assert cfg.api_base == "http://x/v1"
        assert cfg.api_key == "sk-1"
        assert cfg.max_tokens == 1234
        assert cfg.llm_timeout == 60
        assert cfg.max_rework_attempts == 9
        assert cfg.model_profile == "small"
        assert cfg.skip_qa is True
        assert cfg.enable_optimizer is False
        assert cfg.optimizer_iterations == 500
        assert cfg.optimizer_seed == 42
        assert cfg.router_engine == "builtin"
        assert cfg.escape_fanout is True
        assert cfg.short_cleanup is False
        assert cfg.freerouting_jar_path == Path("/jars/fr.jar")
        assert cfg.freerouting_timeout_s == 120
        assert cfg.kicad_library_path == "/libs/kicad"
        assert cfg.component_cache_path == "/cache/c.json"
        assert cfg.llm_enrichment_workers == 8
        assert cfg.vision_model == "vm"
        assert cfg.vision_max_review_attempts == 7
        assert cfg.projects_dir == "myprojects"

    def test_bad_model_profile_falls_back(self, clean_env, tmp_path):
        clean_env.setattr("orchestrator.config._autodetect_kicad_library",
                          lambda: None)
        clean_env.setenv("PCB_MODEL_PROFILE", "gigantic")
        cfg = OrchestratorConfig.from_env(base_dir=tmp_path)
        assert cfg.model_profile == "normal"  # unchanged default

    def test_dotenv_loaded(self, clean_env, tmp_path):
        clean_env.setattr("orchestrator.config._autodetect_kicad_library",
                          lambda: None)
        (tmp_path / ".env").write_text(
            "# comment\n"
            "\n"
            "PCB_GENERATE_MODEL=\"dotenv-model\"\n"
            "PCB_MAX_REWORK='3'\n"
            "NOEQUALSLINE\n"
        )
        cfg = OrchestratorConfig.from_env(base_dir=tmp_path)
        assert cfg.generate_model == "dotenv-model"
        assert cfg.max_rework_attempts == 3

    def test_dotenv_does_not_override_existing_env(self, clean_env, tmp_path):
        clean_env.setattr("orchestrator.config._autodetect_kicad_library",
                          lambda: None)
        clean_env.setenv("PCB_GENERATE_MODEL", "from-env")
        (tmp_path / ".env").write_text("PCB_GENERATE_MODEL=from-dotenv\n")
        cfg = OrchestratorConfig.from_env(base_dir=tmp_path)
        assert cfg.generate_model == "from-env"

    def test_autodetect_finds_library(self, clean_env, tmp_path, monkeypatch):
        lib = tmp_path / "kfp"
        (lib / "Resistor_SMD.pretty").mkdir(parents=True)
        monkeypatch.setattr("orchestrator.config._KICAD_LIBRARY_CANDIDATES",
                            (str(lib),))
        assert _autodetect_kicad_library() == str(lib)
        # and from_env wires it in when the env var is absent
        cfg = OrchestratorConfig.from_env(base_dir=tmp_path / "other")
        assert cfg.kicad_library_path == str(lib)

    def test_dotenv_read_error_swallowed(self, clean_env, tmp_path):
        # .env is a *directory* → read_text raises → best-effort handler
        # swallows it (covers the except branch in _load_dotenv).
        clean_env.setattr("orchestrator.config._autodetect_kicad_library",
                          lambda: None)
        (tmp_path / ".env").mkdir()
        cfg = OrchestratorConfig.from_env(base_dir=tmp_path)
        assert cfg.generate_model  # no crash; defaults intact

    def test_autodetect_none_when_no_pretty(self, monkeypatch, tmp_path):
        empty = tmp_path / "empty"
        empty.mkdir()
        monkeypatch.setattr("orchestrator.config._KICAD_LIBRARY_CANDIDATES",
                            (str(empty), str(tmp_path / "missing")))
        assert _autodetect_kicad_library() is None


# ---------------------------------------------------------------------------
# cache
# ---------------------------------------------------------------------------

class TestCache:
    def test_footprint_round_trip(self, tmp_path):
        c = ComponentCache(tmp_path / "c.json")
        assert c.get_footprint("0805") is None  # miss
        c.put_footprint("0805", {"1": [-1.0, 0.0], "2": [1.0, 0.0]},
                        (0.6, 1.0), source="kicad", needs_review=True)
        got = c.get_footprint("  0805  ")  # key normalised (strip+upper)
        assert got["source"] == "kicad"
        assert got["pad_size"] == [0.6, 1.0]
        assert got["needs_review"] is True
        assert "resolved" in got

    def test_specs_round_trip(self, tmp_path):
        c = ComponentCache(tmp_path / "c.json")
        assert c.get_specs("NE555") is None
        c.put_specs("ne555", {"package": "DIP-8", "value": "timer"},
                    source="llm")
        got = c.get_specs("NE555")
        assert got["package"] == "DIP-8"
        assert got["source"] == "llm"
        assert got["needs_review"] is False

    def test_persistence_across_instances(self, tmp_path):
        path = tmp_path / "c.json"
        ComponentCache(path).put_footprint(
            "R_0603", {"1": [0, 0]}, (0.5, 0.5), source="ipc")
        # new instance reads from disk
        assert ComponentCache(path).get_footprint("R_0603")["source"] == "ipc"

    def test_corrupted_file_recovers(self, tmp_path):
        path = tmp_path / "c.json"
        path.write_text("{ not valid json")
        c = ComponentCache(path)
        assert c.get_footprint("anything") is None  # no crash
        # still usable after recovery
        c.put_specs("X", {"v": 1}, source="s")
        assert c.get_specs("X")["v"] == 1

    def test_partial_file_gets_sections(self, tmp_path):
        path = tmp_path / "c.json"
        path.write_text(json.dumps({"footprints": {"A": {"x": 1}}}))  # no specs
        c = ComponentCache(path)
        assert c.get_specs("Y") is None  # specs section synthesized
        assert c.get_footprint("A") == {"x": 1}

    def test_default_path(self):
        # no path → expanded default under ~/.pcb-creator (don't write to it)
        c = ComponentCache()
        assert str(c._path).endswith("component_cache.json")
