"""Agent-simulation tests: drive the MCP server in-process the way a small
client model would, asserting the response-envelope contract.

Contract under test (mcp_envelope.py):
- success → 'next_step' with a concrete {tool, args} when a follow-up exists
- failure → 'error' plus machine-readable 'remediation' [{option, tool, args}]
- async tools → state 'running' + poll guidance
"""

import asyncio
import json
import shutil
import time

import pytest

from fastmcp import Client


@pytest.fixture()
def server(tmp_path, monkeypatch):
    """Fresh MCP server view onto an isolated projects dir."""
    monkeypatch.setenv("PCB_PROJECTS_DIR", str(tmp_path / "projects"))
    import mcp_server
    return mcp_server.mcp


def call(server, tool, args=None):
    async def _run():
        async with Client(server) as client:
            r = await client.call_tool(tool, args or {}, raise_on_error=False)
            return r.data
    return asyncio.run(_run())


def run_sequence(server, steps):
    """Run [(tool, args), ...] in one client session; return all results."""
    async def _run():
        out = []
        async with Client(server) as client:
            for tool, args in steps:
                r = await client.call_tool(tool, args, raise_on_error=False)
                out.append(r.data)
        return out
    return asyncio.run(_run())


def assert_fail_with_remediation(result, expected_tools):
    """A failure must carry an error and remediation pointing at the right tools."""
    assert result["success"] is False
    assert result["error"]
    tools = [r["tool"] for r in result.get("remediation", [])]
    for t in expected_tools:
        assert t in tools, f"expected remediation via {t}, got {tools}"


# ---------------------------------------------------------------------------
# Cold-start guidance
# ---------------------------------------------------------------------------

def test_workflow_guide_lists_all_flows(server):
    guide = call(server, "get_workflow_guide")
    flows = guide["workflows"]
    assert set(flows) == {"build_from_scratch", "import_kicad", "autonomous"}
    for flow in flows.values():
        orders = [s["order"] for s in flow["steps"]]
        assert orders == sorted(orders)
        assert all(s.get("tool") for s in flow["steps"])


# ---------------------------------------------------------------------------
# Flawed sequences — every error must be actionable
# ---------------------------------------------------------------------------

def test_route_before_place_remediates(server):
    r = call(server, "create_circuit",
             {"project_name": "flawed1", "description": "x",
              "board_width_mm": 30, "board_height_mm": 20})
    assert r["success"]
    r = call(server, "route_board", {"project_name": "flawed1"})
    assert_fail_with_remediation(r, ["optimize_placement"])


def test_place_unknown_project_remediates(server):
    r = call(server, "optimize_placement", {"project_name": "nonexistent"})
    assert_fail_with_remediation(r, ["import_kicad_netlist", "create_circuit"])


def test_connect_unknown_pin_lists_valid_pins(server):
    run_sequence(server, [
        ("create_circuit", {"project_name": "flawed2", "description": "x",
                            "board_width_mm": 30, "board_height_mm": 20}),
        ("add_component", {"project_name": "flawed2", "designator": "D1",
                           "component_type": "led", "value": "red",
                           "package": "0805"}),
    ])
    r = call(server, "connect_pins",
             {"project_name": "flawed2", "net_name": "X",
              "pins": ["D1.kathode", "D1.1"]})
    assert r["success"] is False
    # The error must teach the agent the valid pins
    assert "anode" in r["error"] and "cathode" in r["error"]


def test_provide_footprint_no_args_offers_both_modes(server):
    r = call(server, "provide_footprint",
             {"project_name": "any", "package": "MYSTERY-4"})
    assert_fail_with_remediation(r, ["provide_footprint"])
    # Both modes offered: alias and explicit geometry
    args = [json.dumps(o["args"]) for o in r["remediation"]]
    assert any("like_package" in a for a in args)
    assert any("pin_offsets" in a for a in args)


def test_add_component_bad_designator(server):
    call(server, "create_circuit",
         {"project_name": "flawed3", "description": "x",
          "board_width_mm": 30, "board_height_mm": 20})
    r = call(server, "add_component",
             {"project_name": "flawed3", "designator": "led1",
              "component_type": "led", "value": "red", "package": "0805"})
    assert r["success"] is False
    assert "R1" in r["error"] or "designator" in r["error"].lower()


def test_create_circuit_bad_name(server):
    r = call(server, "create_circuit",
             {"project_name": "Bad-Name", "description": "x",
              "board_width_mm": 30, "board_height_mm": 20})
    assert r["success"] is False


def test_finalize_unconnected_pins_remediates(server):
    run_sequence(server, [
        ("create_circuit", {"project_name": "flawed4", "description": "x",
                            "board_width_mm": 30, "board_height_mm": 20}),
        ("add_component", {"project_name": "flawed4", "designator": "R1",
                           "component_type": "resistor", "value": "330ohm",
                           "package": "0805"}),
    ])
    r = call(server, "finalize_circuit", {"project_name": "flawed4"})
    assert_fail_with_remediation(r, ["connect_pins", "mark_no_connect"])
    assert r["unconnected_pins"] == ["R1.1", "R1.2"]


def test_drc_before_route_remediates(server):
    call(server, "create_circuit",
         {"project_name": "flawed5", "description": "x",
          "board_width_mm": 30, "board_height_mm": 20})
    r = call(server, "run_drc", {"project_name": "flawed5"})
    assert_fail_with_remediation(r, ["route_board"])


def test_status_unknown_project_remediates(server):
    r = call(server, "get_project_status", {"project_name": "ghost"})
    assert_fail_with_remediation(r, ["list_projects"])


def test_route_invalid_effort(server):
    r = call(server, "route_board",
             {"project_name": "any", "effort": "turbo"})
    assert_fail_with_remediation(r, ["route_board"])


def test_place_component_out_of_bounds_remediates(server):
    run_sequence(server, [
        ("create_circuit", {"project_name": "flawed6", "description": "x",
                            "board_width_mm": 30, "board_height_mm": 20}),
        ("add_component", {"project_name": "flawed6", "designator": "J1",
                           "component_type": "connector", "value": "2-pin",
                           "package": "PinHeader_1x2"}),
        ("connect_pins", {"project_name": "flawed6", "net_name": "VCC",
                          "pins": ["J1.1", "J1.2"]}),
        ("finalize_circuit", {"project_name": "flawed6"}),
    ])
    # Pads would hang past the left edge — must fail at pin time
    r = call(server, "place_component",
             {"project_name": "flawed6", "designator": "J1",
              "x_mm": 0.5, "y_mm": 10})
    assert_fail_with_remediation(r, ["place_component"])
    assert "edge clearance" in r["error"]

    # Valid position is accepted and survives optimize_placement
    r = call(server, "place_component",
             {"project_name": "flawed6", "designator": "J1",
              "x_mm": 6.0, "y_mm": 10, "rotation_deg": 90})
    assert r["success"], r
    assert r["next_step"]["tool"] == "optimize_placement"


# ---------------------------------------------------------------------------
# Correct sequence — builder flow end-to-end
# ---------------------------------------------------------------------------

LED_STEPS = [
    ("create_circuit", {"project_name": "sim_led", "description":
                        "One red LED with resistor on 5V",
                        "board_width_mm": 30, "board_height_mm": 20}),
    ("add_component", {"project_name": "sim_led", "designator": "R1",
                       "component_type": "resistor", "value": "330ohm",
                       "package": "0805"}),
    ("add_component", {"project_name": "sim_led", "designator": "D1",
                       "component_type": "led", "value": "red",
                       "package": "0805"}),
    ("add_component", {"project_name": "sim_led", "designator": "J1",
                       "component_type": "connector", "value": "2-pin header",
                       "package": "PinHeader_1x2"}),
    ("connect_pins", {"project_name": "sim_led", "net_name": "VCC",
                      "pins": ["J1.1", "R1.1"]}),
    ("connect_pins", {"project_name": "sim_led", "net_name": "LED_DRIVE",
                      "pins": ["R1.2", "D1.anode"]}),
    ("connect_pins", {"project_name": "sim_led", "net_name": "GND",
                      "pins": ["D1.cathode", "J1.2"]}),
    ("finalize_circuit", {"project_name": "sim_led"}),
]


def test_builder_flow_envelope_chain(server):
    results = run_sequence(server, LED_STEPS)
    for (tool, _), r in zip(LED_STEPS, results):
        assert r["success"], f"{tool} failed: {r.get('error')}"

    # add_component returns the pin table the agent connects by
    add_d1 = results[2]
    assert {"pin": 1, "name": "anode"} in add_d1["pins"]

    # connect_pins infers net classes from names
    assert results[4]["net_class"] == "power"
    assert results[6]["net_class"] == "ground"

    # finalize points at placement with the draft's board dimensions
    fin = results[7]
    assert fin["next_step"]["tool"] == "optimize_placement"
    assert fin["next_step"]["args"]["board_width_mm"] == 30

    # list_circuit now says: finalize (nothing unconnected)
    ls = call(server, "list_circuit", {"project_name": "sim_led"})
    assert ls["unconnected_pins"] == []
    assert ls["next_step"]["tool"] == "finalize_circuit"


@pytest.mark.skipif(shutil.which("java") is None,
                    reason="Freerouting requires Java")
def test_builder_flow_through_routing_and_drc(server):
    results = run_sequence(server, LED_STEPS)
    assert all(r["success"] for r in results)

    place = call(server, "optimize_placement",
                 {"project_name": "sim_led", "board_width_mm": 30,
                  "board_height_mm": 20, "seed": 42})
    assert place["success"], place.get("error")
    assert place["next_step"]["tool"] == "route_board"

    start = call(server, "route_board",
                 {"project_name": "sim_led", "effort": "fast"})
    assert start["success"] and start["state"] == "running"
    assert "status_hint" in start

    # Poll like an agent would
    deadline = time.time() + 180
    status = None
    while time.time() < deadline:
        status = call(server, "get_project_status", {"project_name": "sim_led"})
        if status.get("routing_state") in ("complete", "failed"):
            break
        # While running the anti-abandonment fields must be present
        assert status.get("poll_again_in_s")
        assert "status_hint" in status
        time.sleep(1)
    assert status["routing_state"] == "complete", status.get("routing_error")
    assert status["routing_result"]["completion_pct"] == 100.0

    drc = call(server, "run_drc", {"project_name": "sim_led"})
    assert drc["success"]
    assert drc["passed"], drc
    assert drc["next_step"]["tool"] == "export_outputs"

    out = call(server, "export_outputs", {"project_name": "sim_led"})
    assert out["success"], out.get("error")
    assert out.get("files") or out.get("output_dir")
