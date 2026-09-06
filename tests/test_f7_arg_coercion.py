"""F7 regression: MCP argument typing traps.

Two traps the 2026-09-05 audit caught, both fixed by coercion instead of
docs-only changes:

1. dict/list-typed tool args (requirements_json, settings, models, rails,
   pin_offsets, pad_size, positions, components, nets) rejected JSON *strings*
   with an opaque pydantic ``dict_type`` validation error. They now accept
   str and parse it; malformed or wrong-shape input fails through the normal
   fail envelope (message + remediation) rather than the schema validator.

2. set_component_positions rejected the ``layers``/``plane_layers`` keys that
   clients copy from the workflow-guide stackup docs ("unexpected keyword
   argument"). The keys are now accepted: they must match the board's current
   stackup (then ignored) or the call fails and redirects to
   optimize_placement, the tool that owns the stackup (and its approval gate).
"""

from __future__ import annotations

import asyncio

import pytest

from fastmcp import Client


@pytest.fixture()
def server(tmp_path, monkeypatch):
    monkeypatch.setenv("PCB_PROJECTS_DIR", str(tmp_path / "projects"))
    import mcp_server
    return mcp_server.mcp


def call(server, tool, args=None):
    async def _run():
        async with Client(server) as client:
            r = await client.call_tool(tool, args or {}, raise_on_error=False)
            return r.data
    return asyncio.run(_run())


def build_minimal(server, name):
    """create → add R1+J1 → connect → finalize (netlist available, no LLM)."""
    steps = [
        ("create_circuit", {"project_name": name, "description": "mini",
                            "board_width_mm": 30, "board_height_mm": 20}),
        ("add_component", {"project_name": name, "designator": "R1",
                           "component_type": "resistor", "value": "330ohm",
                           "package": "0805"}),
        ("add_component", {"project_name": name, "designator": "J1",
                           "component_type": "connector", "value": "2pin",
                           "package": "PinHeader_1x2"}),
        ("connect_pins", {"project_name": name, "net_name": "VCC",
                          "pins": ["J1.1", "R1.1"]}),
        ("connect_pins", {"project_name": name, "net_name": "GND",
                          "pins": ["R1.2", "J1.2"]}),
        ("finalize_circuit", {"project_name": name}),
    ]
    res = [call(server, tool, args) for tool, args in steps]
    assert all(r and r.get("success") for r in res), [r.get("error") for r in res]
    return name


# --- 1. JSON-string coercion for dict/list args -----------------------------

def test_dict_arg_json_string_reaches_the_tool(server):
    # Before the fix this died at pydantic with a dict_type error; now the
    # string is parsed and the tool's OWN check runs (project not found).
    r = call(server, "check_circuit",
             {"project_name": "ghost", "models": '{"R1": {"vcc_max": "5V"}}'})
    assert r["success"] is False
    assert "not found" in r["error"]
    assert "dict_type" not in r["error"]


def test_dict_arg_malformed_json_string_fails_cleanly(server):
    r = call(server, "check_circuit",
             {"project_name": "ghost", "models": '{"broken'})
    assert r["success"] is False
    assert "not valid JSON" in r["error"]


def test_dict_arg_wrong_shape_json_string(server):
    # Parses fine but as an array; models must be an object.
    r = call(server, "check_circuit",
             {"project_name": "ghost", "models": "[1, 2]"})
    assert r["success"] is False
    assert "expected a JSON object" in r["error"]


def test_check_circuit_string_models_used(server):
    # The bare R1-between-rails circuit legitimately fails the electrical
    # checks; what this pins is that a stringified rails payload PARSES and
    # reaches the solver (verdict issued, nets evaluated) instead of dying as
    # a schema error.
    name = build_minimal(server, "f7_cc_str")
    r = call(server, "check_circuit",
             {"project_name": name, "supply_voltage": "5V",
              "rails": '{"VCC": "5V"}'})
    assert r.get("verdict") in ("issues_found", "not_enough_information",
                                "no_issues_found")
    assert "2 of 2 nets were evaluated" in str(r)


def test_design_pcb_malformed_args_no_thread_started(server):
    r = call(server, "design_pcb",
             {"description": "x", "project_name": "f7_dp",
              "requirements_json": "{oops"})
    assert r["success"] is False
    assert "requirements_json" in r["error"]


def test_provide_footprint_string_geometry_accepted(server):
    r = call(server, "provide_footprint",
             {"project_name": "f7_fp", "package": "CUSTOM-2",
              "pin_offsets": '{"1": [-1.27, 0.0], "2": [1.27, 0.0]}',
              "pad_size": "[1.05, 1.4]"})
    assert r["success"] is True, r
    assert r["package"] == "CUSTOM-2"


def test_build_circuit_string_components_nets(server):
    r = call(server, "build_circuit",
             {"project_name": "f7_bc", "description": "rc",
              "board_width_mm": 30, "board_height_mm": 20,
              "components": ('[{"designator": "R1", "component_type": '
                             '"resistor", "value": "330", "package": "0805"},'
                             '{"designator": "J1", "component_type": '
                             '"connector", "value": "2pin",'
                             '"package": "PinHeader_1x2"}]'),
              "nets": ('[{"net_name": "VCC", "pins": ["J1.1", "R1.1"]},'
                       '{"net_name": "GND", "pins": ["R1.2", "J1.2"]}]')})
    assert r["success"] is True, r


# --- 2. layers/plane_layers tolerance on set_component_positions ------------

def test_set_positions_string_positions_accepted(server):
    name = build_minimal(server, "f7_sp_str")
    r = call(server, "set_component_positions",
             {"project_name": name,
              "positions": '[{"designator": "J1", "x_mm": 5, "y_mm": 5}]',
              "board_width_mm": 30, "board_height_mm": 20})
    assert r["success"] is True, r
    assert r["pinned_count"] == 1


def test_set_positions_layers_mismatch_redirects(server):
    name = build_minimal(server, "f7_sp_l4")
    call(server, "optimize_placement",
         {"project_name": name, "board_width_mm": 30, "board_height_mm": 20})
    r = call(server, "set_component_positions",
             {"project_name": name,
              "positions": [{"designator": "J1", "x_mm": 5, "y_mm": 5}],
              "layers": 4})
    assert r["success"] is False
    assert "never changes the layer stackup" in r["error"]
    redirect = [o for o in r["remediation"] if o["tool"] == "optimize_placement"]
    assert redirect and redirect[0]["args"]["layers"] == 4


def test_set_positions_layers_match_ignored(server):
    name = build_minimal(server, "f7_sp_l2")
    call(server, "optimize_placement",
         {"project_name": name, "board_width_mm": 30, "board_height_mm": 20})
    r = call(server, "set_component_positions",
             {"project_name": name,
              "positions": [{"designator": "J1", "x_mm": 5, "y_mm": 5}],
              "layers": 2, "plane_layers": None})
    assert r["success"] is True, r


def test_set_positions_no_placement_layers_change_redirects(server):
    # No placement file yet: the implicit 2-layer stackup must not be flipped
    # through this tool either — first placement belongs to optimize_placement.
    name = build_minimal(server, "f7_sp_nopl")
    r = call(server, "set_component_positions",
             {"project_name": name,
              "positions": [{"designator": "J1", "x_mm": 5, "y_mm": 5}],
              "board_width_mm": 30, "board_height_mm": 20, "layers": "4"})
    assert r["success"] is False
    assert any(o["tool"] == "optimize_placement" for o in r["remediation"])


def test_set_positions_invalid_layers_values(server):
    name = build_minimal(server, "f7_sp_bad")
    for bad in ({"layers": 3}, {"plane_layers": 5}, {"layers": "4.5"},
                {"plane_layers": "-1"}):
        r = call(server, "set_component_positions",
                 {"project_name": name,
                  "positions": [{"designator": "J1", "x_mm": 5, "y_mm": 5}],
                  "board_width_mm": 30, "board_height_mm": 20, **bad})
        assert r["success"] is False
        assert ("must be 2 or 4" in r["error"]
                or "must be integers" in r["error"])


def test_set_positions_malformed_positions_string(server):
    name = build_minimal(server, "f7_sp_badstr")
    r = call(server, "set_component_positions",
             {"project_name": name, "positions": '[{"designator"'})
    assert r["success"] is False
    assert "not valid JSON" in r["error"]