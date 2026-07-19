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


def test_provide_footprint_clears_unresolved_loop(server, tmp_path, monkeypatch):
    """The footprint-resolution loop must actually CLEAR: verify (calls
    _activate_project_lookup) → provide_footprint (persists) → verify resolved.
    Regression for _activate_project_lookup wiping the component cache (it passed
    a None cache to configure_lookup when _init_lookup had not run, so the very
    next provide_footprint failed with 'cache is not configured')."""
    import mcp_server
    # Hermetic cache + a fresh, unconfigured lookup (the path that exposed the bug).
    monkeypatch.setenv("PCB_COMPONENT_CACHE_PATH", str(tmp_path / "cache.json"))
    monkeypatch.setattr(mcp_server, "_LOOKUP_CONFIGURED", False)
    monkeypatch.setattr(mcp_server, "_CACHE", None)
    monkeypatch.setattr(mcp_server, "_KICAD_INDEX", None)

    proj = "fploop"
    pdir = tmp_path / "projects" / proj
    pdir.mkdir(parents=True)
    elements = [{"element_type": "component", "component_id": "comp_u1",
                 "designator": "U1", "component_type": "ic", "value": "WONKY",
                 "package": "WONKY-8_DIPLIKE"}]
    elements += [{"element_type": "port", "port_id": f"p{i}",
                  "component_id": "comp_u1", "pin_number": i, "name": str(i),
                  "electrical_type": "passive"} for i in range(1, 9)]
    elements.append({"element_type": "net", "net_id": "net_a", "name": "A",
                     "net_class": "signal", "connected_port_ids": ["p1", "p2"]})
    (pdir / f"{proj}_netlist.json").write_text(
        json.dumps({"version": "1.0", "project_name": proj, "elements": elements}))

    v1 = call(server, "verify_footprints", {"project_name": proj})
    assert v1["resolved"] is False and v1["unresolved_count"] == 1

    pf = call(server, "provide_footprint",
              {"project_name": proj, "package": "WONKY-8_DIPLIKE",
               "like_package": "DIP-8"})
    assert pf["success"], pf          # must NOT be "cache is not configured"

    v2 = call(server, "verify_footprints", {"project_name": proj})
    assert v2["resolved"] is True and v2["unresolved_count"] == 0
    assert v2["next_step"]["tool"] == "optimize_placement"


def test_register_custom_footprint_guidance(server):
    bad = call(server, "register_custom_footprint",
               {"project_name": "rcf", "package_name": "X",
                "kicad_mod_content": "not an s-expr"})
    assert_fail_with_remediation(bad, ["register_custom_footprint"])
    good = call(server, "register_custom_footprint",
                {"project_name": "rcf", "package_name": "MY2P",
                 "kicad_mod_content": '(footprint "MY2P" (layer F.Cu)'
                 '(pad "1" smd rect (at 0 0)(size 1 1)(layers F.Cu)))'})
    assert good["success"]
    assert good["next_step"]["tool"] == "verify_footprints"


def test_check_footprint_coverage_guidance(server):
    needs = call(server, "check_footprint_coverage",
                 {"components": [{"reference": "U1", "package": "NOPE-99",
                                  "pin_count": 99}], "project_name": "cov"})
    assert needs["coverage"]["custom_needed"] == 1
    assert needs["next_step"]["tool"] == "register_custom_footprint"
    okc = call(server, "check_footprint_coverage",
               {"components": [{"reference": "R1", "package": "0805",
                                "pin_count": 2}], "project_name": "cov"})
    assert okc["coverage"]["custom_needed"] == 0
    assert okc["next_step"]["tool"] == "optimize_placement"


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


def _write_routed(tmp_path, proj, completion, unrouted=()):
    pdir = tmp_path / "projects" / proj
    pdir.mkdir(parents=True, exist_ok=True)
    (pdir / f"{proj}_routed.json").write_text(json.dumps({
        "routing": {"traces": [], "vias": [],
                    "statistics": {"completion_pct": completion, "total_nets": 5,
                                   "routed_nets": 5, "unrouted_nets": list(unrouted)},
                    "unrouted_nets": list(unrouted)}}))
    return pdir


def test_status_complete_incomplete_route_offers_finish(server, tmp_path):
    """A finished-but-incomplete route must tell the poller to finish it with
    keep_existing, not leave it thinking the board is done."""
    _write_routed(tmp_path, "incpoll", 80.0, ["net_x"])
    r = call(server, "get_project_status", {"project_name": "incpoll"})
    assert r["routing_state"] == "complete"
    assert r["next_step"]["tool"] == "route_board"
    assert r["next_step"]["args"]["keep_existing"] is True


def test_status_surfaces_completion_and_unrouted_nets(server, tmp_path):
    """routing_stats must read from routing.statistics (not a missing top-level
    key) and list the exact open nets, so the agent targets recovery instead of
    seeing a false 0% and an empty net list."""
    _write_routed(tmp_path, "statpoll", 93.6, ["net_a", "net_b", "net_c"])
    r = call(server, "get_project_status", {"project_name": "statpoll"})
    rs = r["routing_stats"]
    assert rs["completion_pct"] == 93.6
    assert rs["unrouted_nets"] == ["net_a", "net_b", "net_c"]


def test_status_complete_route_points_at_drc_then_export(server, tmp_path):
    """After a 100% route the poller is steered run_drc → export_outputs →
    (done) get_board_image as artifacts appear, instead of having to guess."""
    pdir = _write_routed(tmp_path, "donepoll", 100.0)
    r = call(server, "get_project_status", {"project_name": "donepoll"})
    assert r["routing_state"] == "complete"
    assert r["next_step"]["tool"] == "run_drc"

    (pdir / "donepoll_drc_report.json").write_text(
        json.dumps({"passed": True, "summary": "ok", "statistics": {}}))
    r = call(server, "get_project_status", {"project_name": "donepoll"})
    assert r["next_step"]["tool"] == "export_outputs"

    out = pdir / "output"
    out.mkdir()
    (out / "donepoll_gerbers.zip").write_text("zip")
    r = call(server, "get_project_status", {"project_name": "donepoll"})
    assert r["next_step"]["tool"] == "get_board_image"


def _write_placement(tmp_path, proj, layers, plane_layers, w=40, h=30):
    pdir = tmp_path / "projects" / proj
    pdir.mkdir(parents=True, exist_ok=True)
    (pdir / f"{proj}_placement.json").write_text(json.dumps(
        {"board": {"layers": layers, "plane_layers": plane_layers,
                   "width_mm": w, "height_mm": h}, "placements": []}))


def test_route_failure_escalation_ladder(server, tmp_path):
    """A failed route escalates routing CAPACITY before board size, and gates
    the user-constrained changes (layer count, board dimensions) on approval:
      2-layer -> 4-layer plane_layers=2 (ASK USER)
      plane_layers=2 -> 1 -> 0          (free, no approval)
      plane_layers=0 -> larger board    (ASK USER)."""
    import mcp_server

    _write_placement(tmp_path, "lad2", 2, None)
    s = mcp_server._route_failure_next_step("lad2", "e")
    assert s["args"]["layers"] == 4 and s["args"]["plane_layers"] == 2
    assert s.get("requires_user_approval") is True   # 2->4 needs permission

    _write_placement(tmp_path, "lad4b", 4, 2)
    s = mcp_server._route_failure_next_step("lad4b", "e")
    assert s["args"]["plane_layers"] == 1
    assert not s.get("requires_user_approval")        # same 4-layer board

    _write_placement(tmp_path, "lad4a", 4, 1)
    s = mcp_server._route_failure_next_step("lad4a", "e")
    assert s["args"]["plane_layers"] == 0
    assert not s.get("requires_user_approval")

    _write_placement(tmp_path, "lad4z", 4, 0, w=40, h=30)
    s = mcp_server._route_failure_next_step("lad4z", "e")
    assert "plane_layers" not in s["args"]            # board size is the LAST lever
    assert s["args"]["board_width_mm"] > 40 and s["args"]["board_height_mm"] > 30
    assert s.get("requires_user_approval") is True   # resizing needs permission


def test_poll_interval_backs_off():
    """Background-job poll cadence backs off so an over-eager agent isn't told to
    hammer get_project_status on a multi-minute route."""
    import mcp_server
    assert mcp_server._poll_interval(None) == 15
    assert mcp_server._poll_interval(0) == 15
    assert mcp_server._poll_interval(60) == 30
    assert mcp_server._poll_interval(300) == 60


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
    # The failure offers a concrete, ready-to-run free position (not just
    # "<new x>") so the agent retries instead of guessing in a loop.
    concrete = [o for o in r["remediation"]
                if o["tool"] == "place_component"
                and isinstance(o["args"].get("x_mm"), (int, float))]
    assert concrete, r["remediation"]
    sug = concrete[0]["args"]
    r2 = call(server, "place_component",
              {"project_name": "flawed6", "designator": "J1",
               "x_mm": sug["x_mm"], "y_mm": sug["y_mm"],
               "rotation_deg": sug.get("rotation_deg", 0)})
    assert r2["success"], r2

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


# ---------------------------------------------------------------------------
# Code-enforced approval gate (layer promotion / board enlargement)
# ---------------------------------------------------------------------------

def test_layer_promotion_requires_approval(server, tmp_path):
    """Promoting a placed 2-layer board to 4 layers must fail without
    approved=True, and the remediation must carry the ready-to-run call."""
    _write_placement(tmp_path, "gate2l", 2, None)
    r = call(server, "optimize_placement",
             {"project_name": "gate2l", "layers": 4, "plane_layers": 2})
    assert r["success"] is False and "approval" in r["error"].lower()
    rem = r["remediation"][0]
    assert rem["tool"] == "optimize_placement"
    assert rem["args"]["approved"] is True and rem["args"]["layers"] == 4

    # plane_layers alone implies promotion on a 2-layer board — also gated.
    r = call(server, "optimize_placement",
             {"project_name": "gate2l", "plane_layers": 1})
    assert r["success"] is False and "approval" in r["error"].lower()

    # With approved=True the gate opens (fails later on the missing netlist,
    # NOT on approval).
    r = call(server, "optimize_placement",
             {"project_name": "gate2l", "layers": 4, "approved": True})
    assert "approval" not in (r.get("error") or "").lower()


def test_board_enlargement_requires_approval(server, tmp_path):
    _write_placement(tmp_path, "gategrow", 2, None, w=40, h=30)
    r = call(server, "optimize_placement",
             {"project_name": "gategrow", "board_width_mm": 60,
              "board_height_mm": 30})
    assert r["success"] is False and "approval" in r["error"].lower()
    # Same size / shrink stays free.
    r = call(server, "optimize_placement",
             {"project_name": "gategrow", "board_width_mm": 40,
              "board_height_mm": 30})
    assert "approval" not in (r.get("error") or "").lower()
    r = call(server, "optimize_placement",
             {"project_name": "gategrow", "board_width_mm": 35,
              "board_height_mm": 25})
    assert "approval" not in (r.get("error") or "").lower()


def test_first_placement_and_plane_reallocation_ungated(server, tmp_path):
    # No placement yet: 4-layer from the start is a design choice, not gated.
    (tmp_path / "projects" / "gatenew").mkdir(parents=True)
    r = call(server, "optimize_placement",
             {"project_name": "gatenew", "layers": 4, "plane_layers": 2,
              "board_width_mm": 50, "board_height_mm": 40})
    assert "approval" not in (r.get("error") or "").lower()
    # Already 4-layer: plane_layers reallocation (capacity, free) is ungated.
    _write_placement(tmp_path, "gate4l", 4, 2)
    r = call(server, "optimize_placement",
             {"project_name": "gate4l", "plane_layers": 1})
    assert "approval" not in (r.get("error") or "").lower()


def test_set_component_positions_enlargement_gated(server, tmp_path):
    _write_placement(tmp_path, "gatepos", 2, None, w=40, h=30)
    pos = [{"designator": "J1", "x_mm": 5, "y_mm": 5}]
    r = call(server, "set_component_positions",
             {"project_name": "gatepos", "positions": pos,
              "board_width_mm": 80, "board_height_mm": 30})
    assert r["success"] is False and "approval" in r["error"].lower()
    assert r["remediation"][0]["args"]["approved"] is True
    # Approved: gate opens (later failure, if any, is about the netlist).
    r = call(server, "set_component_positions",
             {"project_name": "gatepos", "positions": pos,
              "board_width_mm": 80, "board_height_mm": 30, "approved": True})
    assert "approval" not in (r.get("error") or "").lower()


def test_fab_quote_tool(server, tmp_path):
    """get_fab_quote returns a marked estimate + resolved jellybean parts."""
    pdir = tmp_path / "projects" / "quoted"
    pdir.mkdir(parents=True)
    (pdir / "quoted_bom.json").write_text(json.dumps({"bom": [
        {"designator": "R1", "component_type": "resistor", "value": "10kohm",
         "package": "0805", "quantity": 1}]}))
    (pdir / "quoted_placement.json").write_text(json.dumps(
        {"board": {"width_mm": 40, "height_mm": 30, "layers": 2}}))
    r = call(server, "get_fab_quote", {"project_name": "quoted", "live": False})
    assert r["success"] is True
    assert r["board_estimate"]["estimate"] is True
    assert r["parts"][0]["lcsc"] == "C17414"

    r = call(server, "get_fab_quote", {"project_name": "nosuchproj"})
    assert_fail_with_remediation(r, ["list_projects"])


def test_fab_quote_failure_paths(server, tmp_path, monkeypatch):
    # No BOM and no netlist → structured failure steering to the guide.
    (tmp_path / "projects" / "quotempty").mkdir(parents=True)
    r = call(server, "get_fab_quote", {"project_name": "quotempty"})
    assert_fail_with_remediation(r, ["get_workflow_guide"])

    # An unexpected exception is wrapped in the envelope, not raised.
    import orchestrator.quoting as quoting
    def boom(*a, **k):
        raise RuntimeError("nope")
    monkeypatch.setattr(quoting, "quote_project", boom)
    r = call(server, "get_fab_quote", {"project_name": "quotempty"})
    assert r["success"] is False and "nope" in r["error"]


def test_create_circuit_duplicate_without_overwrite_fails(server):
    args = {"project_name": "dupdraft", "description": "d",
            "board_width_mm": 30, "board_height_mm": 20}
    assert call(server, "create_circuit", args)["success"] is True
    r = call(server, "create_circuit", args)     # same project, no overwrite
    assert r["success"] is False


def test_register_custom_footprint_rejects_bad_project_name(server):
    r = call(server, "register_custom_footprint",
             {"project_name": "../evil", "package_name": "PKG-1",
              "kicad_mod_content": "(footprint \"PKG-1\")"})
    assert r["success"] is False
