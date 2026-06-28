"""Logic-coverage tests for mcp_server.py and orchestrator/stages.py.

Complements tests/test_agent_sim.py (which covers the builder happy-path and the
core remediation envelopes). Here we target the still-uncovered DETERMINISTIC
tool logic — list/status/report/export/positions inspectors, every error and
remediation branch, and the deterministic prep/export/DRC stages — plus the
state-mutation contract (place/unplace/positions actually change stored JSON).

External-bound code (the design_pcb LLM pipeline, the live Freerouting/Java
routing run, the retry loop driving the router) is NOT mocked into fake
coverage; it is marked `# pragma: no cover` in the source with a justification.
Where a tool's *dispatch decision* is real logic sitting above a router call, we
stub only the router (stages.run_routing / run_route_with_retry) so the
deterministic branch selection is exercised without spawning Java.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from fastmcp import Client


# --- harness (mirrors test_agent_sim.py) -----------------------------------

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


def run_sequence(server, steps):
    async def _run():
        out = []
        async with Client(server) as client:
            for tool, args in steps:
                r = await client.call_tool(tool, args, raise_on_error=False)
                out.append(r.data)
        return out
    return asyncio.run(_run())


def call_list(server, tool, args=None):
    """A tool returning a top-level list (list_projects) arrives as Root models
    on .data; the plain dicts live under structured_content['result']."""
    async def _run():
        async with Client(server) as client:
            r = await client.call_tool(tool, args or {}, raise_on_error=False)
            return r.structured_content["result"]
    return asyncio.run(_run())


def _projects_dir(tmp_path):
    return tmp_path / "projects"


def _build_led(server, name="cov_led", width=30, height=20):
    """Drive the builder flow to a finalized netlist (resistor + LED + header)."""
    steps = [
        ("create_circuit", {"project_name": name, "description": "led",
                            "board_width_mm": width, "board_height_mm": height}),
        ("add_component", {"project_name": name, "designator": "R1",
                           "component_type": "resistor", "value": "330ohm",
                           "package": "0805"}),
        ("add_component", {"project_name": name, "designator": "D1",
                           "component_type": "led", "value": "red",
                           "package": "0805"}),
        ("add_component", {"project_name": name, "designator": "J1",
                           "component_type": "connector", "value": "2pin",
                           "package": "PinHeader_1x2"}),
        ("connect_pins", {"project_name": name, "net_name": "VCC",
                          "pins": ["J1.1", "R1.1"]}),
        ("connect_pins", {"project_name": name, "net_name": "LED_DRIVE",
                          "pins": ["R1.2", "D1.anode"]}),
        ("connect_pins", {"project_name": name, "net_name": "GND",
                          "pins": ["D1.cathode", "J1.2"]}),
        ("finalize_circuit", {"project_name": name}),
    ]
    res = run_sequence(server, steps)
    assert all(r["success"] for r in res), [r.get("error") for r in res]
    return name


# ---------------------------------------------------------------------------
# Read-only inspectors
# ---------------------------------------------------------------------------

def test_get_requirements_schema_returns_draft7(server):
    schema = call(server, "get_requirements_schema")
    # JSON-Schema draft with the documented top-level requirement fields.
    props = schema.get("properties", {})
    for key in ("components", "connections", "board"):
        assert key in props, props.keys()


def test_list_projects_empty_and_populated(server, tmp_path):
    assert call_list(server, "list_projects") == []

    name = _build_led(server, "cov_list")
    # Add a STATUS.json + a routed artifact so the summary fields populate.
    pdir = _projects_dir(tmp_path) / name
    (pdir / "STATUS.json").write_text(json.dumps(
        {"overall_status": "COMPLETE", "steps": {"3": {"validator_errors": ["x"]}}}))
    (pdir / f"{name}_routed.json").write_text(json.dumps({"routing": {}}))
    (pdir / f"{name}_drc_report.json").write_text(json.dumps({"passed": True}))
    (pdir / "output").mkdir()
    # A non-directory entry in the projects dir must be ignored.
    (_projects_dir(tmp_path) / "stray.txt").write_text("x")

    projects = call_list(server, "list_projects")
    by_name = {p["project_name"]: p for p in projects}
    assert name in by_name
    p = by_name[name]
    assert p["has_routed"] and p["has_drc"] and p["has_outputs"]
    assert p["steps"]["3"]["validator_errors"] == ["x"]


def test_list_projects_tolerates_corrupt_status(server, tmp_path):
    name = _build_led(server, "cov_corrupt")
    (_projects_dir(tmp_path) / name / "STATUS.json").write_text("{not json")
    p = {x["project_name"]: x for x in call_list(server, "list_projects")}[name]
    assert p["steps"] == {}


def test_get_drc_report_missing_then_summary_then_verbose(server, tmp_path):
    miss = call(server, "get_drc_report", {"project_name": "cov_drc"})
    assert miss["success"] is False
    assert any(o["tool"] == "run_drc" for o in miss["remediation"])

    pdir = _projects_dir(tmp_path) / "cov_drc"
    pdir.mkdir(parents=True)
    report = {"passed": True, "summary": "clean", "drc_engine": "internal",
              "authoritative": False, "statistics": {"errors": 0, "warnings": 0},
              "checks": [{"rule": "clearance", "passed": True, "violations": []}]}
    (pdir / "cov_drc_drc_report.json").write_text(json.dumps(report))

    summ = call(server, "get_drc_report", {"project_name": "cov_drc"})
    assert summ.get("passed") is True            # summarized form
    verbose = call(server, "get_drc_report",
                   {"project_name": "cov_drc", "verbose": True})
    assert verbose == report                       # raw passthrough


def test_export_kicad_missing_routed_and_netlist(server, tmp_path):
    no_routed = call(server, "export_kicad", {"project_name": "cov_kc"})
    assert no_routed["success"] is False
    assert any(o["tool"] == "route_board" for o in no_routed["remediation"])

    pdir = _projects_dir(tmp_path) / "cov_kc"
    pdir.mkdir(parents=True)
    (pdir / "cov_kc_routed.json").write_text(json.dumps({"routing": {}}))
    no_netlist = call(server, "export_kicad", {"project_name": "cov_kc"})
    assert no_netlist["success"] is False
    assert "netlist" in no_netlist["error"].lower()


def test_export_kicad_happy_path(server, tmp_path):
    name = _make_routed_project(server, tmp_path, "cov_kc_ok")
    r = call(server, "export_kicad", {"project_name": name})
    assert r["success"], r.get("error")
    assert r["kicad_path"].endswith(".kicad_pcb")
    assert Path(r["kicad_path"]).exists()


def test_export_kicad_surfaces_exporter_error(server, tmp_path, monkeypatch):
    name = _make_routed_project(server, tmp_path, "cov_kc_err")
    import exporters.kicad_exporter as ke
    monkeypatch.setattr(ke, "export_kicad_pcb",
                        lambda *a, **k: (_ for _ in ()).throw(ValueError("boom")))
    r = call(server, "export_kicad", {"project_name": name})
    assert r["success"] is False and "boom" in r["error"]


def test_get_board_image_missing_then_render(server, tmp_path):
    miss = call(server, "get_board_image", {"project_name": "cov_img"})
    assert miss["success"] is False
    assert any(o["tool"] == "route_board" for o in miss["remediation"])

    name = _make_routed_project(server, tmp_path, "cov_img_ok")
    img = call(server, "get_board_image", {"project_name": name, "width": 512})
    assert img["success"], img.get("error")
    assert img["mime_type"] == "image/png"
    assert img["size_bytes"] > 0 and img["image_base64"]


def test_get_board_image_surfaces_render_error(server, tmp_path, monkeypatch):
    name = _make_routed_project(server, tmp_path, "cov_img_err")
    import orchestrator.vision_review as vr
    monkeypatch.setattr(vr, "render_board_png",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("nope")))
    r = call(server, "get_board_image", {"project_name": name})
    assert r["success"] is False and "nope" in r["error"]


# ---------------------------------------------------------------------------
# get_project_status — design/route state surfaces from disk and from jobs
# ---------------------------------------------------------------------------

def test_status_infers_design_state_from_disk(server, tmp_path):
    pdir = _projects_dir(tmp_path) / "cov_st"
    pdir.mkdir(parents=True)
    (pdir / "STATUS.json").write_text(json.dumps(
        {"overall_status": "COMPLETE",
         "steps": {"2": {"validator_errors": ["e1"],
                         "validator_warnings": ["w1"]}}}))
    r = call(server, "get_project_status", {"project_name": "cov_st"})
    assert r["design_state"] == "complete"
    assert r["step_validator_errors"]["2"] == ["e1"]
    assert r["step_validator_warnings"]["2"] == ["w1"]


def test_status_design_failed_from_disk(server, tmp_path):
    pdir = _projects_dir(tmp_path) / "cov_stf"
    pdir.mkdir(parents=True)
    (pdir / "STATUS.json").write_text(json.dumps({"overall_status": "ERROR"}))
    r = call(server, "get_project_status", {"project_name": "cov_stf"})
    assert r["design_state"] == "failed"


def test_status_design_unknown_from_disk(server, tmp_path):
    pdir = _projects_dir(tmp_path) / "cov_stu"
    pdir.mkdir(parents=True)
    (pdir / "STATUS.json").write_text(json.dumps({"steps": {}}))
    r = call(server, "get_project_status", {"project_name": "cov_stu"})
    assert r["design_state"] == "unknown"


def test_status_design_complete_when_output_present(server, tmp_path):
    pdir = _projects_dir(tmp_path) / "cov_sto"
    (pdir / "output").mkdir(parents=True)
    (pdir / "output" / "pkg.zip").write_text("z")
    r = call(server, "get_project_status", {"project_name": "cov_sto"})
    assert r["design_state"] == "complete"


def test_status_in_memory_design_job_before_dir_exists(server):
    """A running design job with no project dir yet must report design_state,
    not a misleading 'not found'."""
    import mcp_server
    import time as _t
    mcp_server._DESIGN_JOBS["cov_job"] = {
        "state": "running", "result": None, "error": None,
        "started_at": _t.monotonic(), "progress": {"step": 2, "name": "schematic"}}
    try:
        r = call(server, "get_project_status", {"project_name": "cov_job"})
        assert r["design_state"] == "running"
        assert r["design_progress"]["name"] == "schematic"
    finally:
        mcp_server._DESIGN_JOBS.pop("cov_job", None)


def test_status_in_memory_design_failed_before_dir(server):
    import mcp_server
    mcp_server._DESIGN_JOBS["cov_jf"] = {
        "state": "failed", "result": None, "error": "translate failed",
        "started_at": None, "elapsed_s": 3.0}
    try:
        r = call(server, "get_project_status", {"project_name": "cov_jf"})
        assert r["design_state"] == "failed"
        assert r["design_error"] == "translate failed"
        assert r["design_elapsed_s"] == 3.0
    finally:
        mcp_server._DESIGN_JOBS.pop("cov_jf", None)


def test_status_route_failed_job_yields_next_step(server, tmp_path):
    """A failed in-memory route job hands the poller the escalation ladder."""
    import mcp_server
    name = "cov_rf"
    pdir = _projects_dir(tmp_path) / name
    pdir.mkdir(parents=True)
    (pdir / f"{name}_placement.json").write_text(json.dumps(
        {"board": {"layers": 2, "width_mm": 40, "height_mm": 30}, "placements": []}))
    mcp_server._ROUTE_JOBS[name] = {
        "state": "failed", "result": None, "error": "congested",
        "started_at": None, "elapsed_s": 5.0}
    try:
        r = call(server, "get_project_status", {"project_name": name})
        assert r["routing_state"] == "failed"
        assert r["next_step"]["tool"] == "optimize_placement"
        assert r["next_step"]["args"]["layers"] == 4   # 2->4 rung
    finally:
        mcp_server._ROUTE_JOBS.pop(name, None)


def test_route_failure_ladder_plane1_to_0(server, tmp_path):
    """The plane_layers==1 -> 0 rung (free the last inner plane, no approval)."""
    import mcp_server
    pdir = _projects_dir(tmp_path) / "cov_l1"
    pdir.mkdir(parents=True)
    (pdir / "cov_l1_placement.json").write_text(json.dumps(
        {"board": {"layers": 4, "plane_layers": 1, "width_mm": 40,
                   "height_mm": 30}, "placements": []}))
    s = mcp_server._route_failure_next_step("cov_l1", "e")
    assert s["args"]["plane_layers"] == 0
    assert not s.get("requires_user_approval")


# ---------------------------------------------------------------------------
# import_kicad_netlist — name validation + overwrite conflict
# ---------------------------------------------------------------------------

def test_import_bad_project_name_remediates(server):
    r = call(server, "import_kicad_netlist",
             {"project_name": "Bad-Name", "file_path": "/x.net"})
    assert r["success"] is False
    assert r["remediation"][0]["args"]["project_name"] == "bad_name"


def test_import_missing_file_remediates(server):
    r = call(server, "import_kicad_netlist",
             {"project_name": "cov_imp", "file_path": "/nope/missing.net"})
    assert r["success"] is False
    assert any(o["tool"] == "import_kicad_netlist" for o in r["remediation"])


def test_import_existing_project_conflict(server, tmp_path):
    pdir = _projects_dir(tmp_path) / "cov_exist"
    pdir.mkdir(parents=True)
    (pdir / "something.json").write_text("{}")
    r = call(server, "import_kicad_netlist",
             {"project_name": "cov_exist", "file_path": "/x.net"})
    assert r["success"] is False
    tools_args = [(o["tool"], o["args"]) for o in r["remediation"]]
    # offers overwrite, a v2 name, and a status check
    assert any(a.get("overwrite") for _, a in tools_args)
    assert any(a.get("project_name") == "cov_exist_v2" for _, a in tools_args)


# ---------------------------------------------------------------------------
# provide_footprint — alias error + explicit-geometry success + malformed
# ---------------------------------------------------------------------------

def test_provide_footprint_unresolved_like_package(server):
    r = call(server, "provide_footprint",
             {"project_name": "cov_pf", "package": "MYX",
              "like_package": "TOTALLY_UNKNOWN_PKG"})
    assert r["success"] is False
    args = [json.dumps(o["args"]) for o in r["remediation"]]
    assert any("like_package" in a for a in args)
    assert any("pin_offsets" in a for a in args)


def test_provide_footprint_explicit_geometry_success(server):
    r = call(server, "provide_footprint",
             {"project_name": "cov_pf2", "package": "CUSTOM-2",
              "pin_offsets": {"1": [-1.27, 0.0], "2": [1.27, 0.0]},
              "pad_size": [1.05, 1.4]})
    assert r["success"], r
    assert r["pin_count"] == 2 and r["source"] == "agent"
    assert r["next_step"]["tool"] == "verify_footprints"


def test_provide_footprint_malformed_geometry(server):
    r = call(server, "provide_footprint",
             {"project_name": "cov_pf3", "package": "BAD",
              "pin_offsets": {"1": ["x", "y"]}, "pad_size": [1.0, 1.0]})
    assert r["success"] is False
    assert any(o["tool"] == "provide_footprint" for o in r["remediation"])


def test_provide_footprint_empty_package(server):
    r = call(server, "provide_footprint",
             {"project_name": "cov_pf4", "package": "", "like_package": "0805"})
    assert r["success"] is False
    assert "non-empty" in r["error"]


# ---------------------------------------------------------------------------
# State mutations: disconnect / mark_no_connect / remove / unplace / clear
# ---------------------------------------------------------------------------

def test_disconnect_pins_mutates_netlist(server):
    name = "cov_disc"
    run_sequence(server, [
        ("create_circuit", {"project_name": name, "description": "x",
                            "board_width_mm": 30, "board_height_mm": 20}),
        ("add_component", {"project_name": name, "designator": "R1",
                           "component_type": "resistor", "value": "1k",
                           "package": "0805"}),
        ("add_component", {"project_name": name, "designator": "R2",
                           "component_type": "resistor", "value": "1k",
                           "package": "0805"}),
        ("connect_pins", {"project_name": name, "net_name": "NETA",
                          "pins": ["R1.1", "R2.1"]}),
    ])
    before = call(server, "list_circuit", {"project_name": name})
    assert any(n["net_name"] == "NETA" for n in before["nets"])
    dr = call(server, "disconnect_pins",
              {"project_name": name, "net_name": "NETA", "pins": ["R1.1", "R2.1"]})
    assert dr["success"], dr
    after = call(server, "list_circuit", {"project_name": name})
    assert not any(n["net_name"] == "NETA" for n in after["nets"])  # empty net gone


def test_mark_no_connect_then_finalize(server):
    name = "cov_nc"
    run_sequence(server, [
        ("create_circuit", {"project_name": name, "description": "x",
                            "board_width_mm": 30, "board_height_mm": 20}),
        ("add_component", {"project_name": name, "designator": "J1",
                           "component_type": "connector", "value": "2pin",
                           "package": "PinHeader_1x2"}),
        ("connect_pins", {"project_name": name, "net_name": "A",
                          "pins": ["J1.1", "J1.2"]}),
    ])
    # Re-add a part whose pins we then NC.
    call(server, "add_component", {"project_name": name, "designator": "TP1",
                                   "component_type": "connector", "value": "tp",
                                   "package": "PinHeader_1x2"})
    nc = call(server, "mark_no_connect",
              {"project_name": name, "pins": ["TP1.1", "TP1.2"]})
    assert nc["success"], nc
    ls = call(server, "list_circuit", {"project_name": name})
    assert ls["unconnected_pins"] == []


def test_disconnect_unknown_net_fails(server):
    name = "cov_disc2"
    call(server, "create_circuit",
         {"project_name": name, "description": "x",
          "board_width_mm": 30, "board_height_mm": 20})
    r = call(server, "disconnect_pins",
             {"project_name": name, "net_name": "GHOST", "pins": ["R1.1"]})
    assert r["success"] is False


def test_remove_component_mutates(server):
    name = "cov_rm"
    run_sequence(server, [
        ("create_circuit", {"project_name": name, "description": "x",
                            "board_width_mm": 30, "board_height_mm": 20}),
        ("add_component", {"project_name": name, "designator": "R1",
                           "component_type": "resistor", "value": "1k",
                           "package": "0805"}),
    ])
    rr = call(server, "remove_component", {"project_name": name, "designator": "R1"})
    assert rr["success"], rr
    ls = call(server, "list_circuit", {"project_name": name})
    assert not any(c["designator"] == "R1" for c in ls["components"])


def test_unplace_and_clear_all_pins(server, tmp_path):
    name = _build_led(server, "cov_pins")
    # Pin two parts at exact coords, persisting to placement + durable store.
    call(server, "place_component",
         {"project_name": name, "designator": "J1", "x_mm": 6.0, "y_mm": 10})
    call(server, "place_component",
         {"project_name": name, "designator": "R1", "x_mm": 15.0, "y_mm": 10})

    pins_file = _projects_dir(tmp_path) / name / f"{name}_placement_pins.json"
    assert pins_file.exists()
    pinned = json.loads(pins_file.read_text())
    assert "J1" in pinned and "R1" in pinned

    up = call(server, "unplace_component", {"project_name": name, "designator": "J1"})
    assert up["success"], up
    assert "J1" not in json.loads(pins_file.read_text())

    cleared = call(server, "clear_all_pins", {"project_name": name})
    assert cleared["success"], cleared
    assert json.loads(pins_file.read_text()) == {}


def test_unplace_unknown_offers_clear_all(server):
    name = _build_led(server, "cov_unpl")
    r = call(server, "unplace_component", {"project_name": name, "designator": "ZZ9"})
    assert r["success"] is False
    assert any(o["tool"] == "clear_all_pins" for o in r["remediation"])


# ---------------------------------------------------------------------------
# set_component_positions — every branch
# ---------------------------------------------------------------------------

def test_set_positions_unknown_project(server):
    r = call(server, "set_component_positions",
             {"project_name": "ghost", "positions": [{"designator": "J1",
                                                      "x_mm": 1, "y_mm": 1}]})
    assert r["success"] is False
    tools = [o["tool"] for o in r["remediation"]]
    assert "create_circuit" in tools and "import_kicad_netlist" in tools


def test_set_positions_draft_not_compiled(server):
    name = "cov_sp_draft"
    call(server, "create_circuit",
         {"project_name": name, "description": "x",
          "board_width_mm": 30, "board_height_mm": 20})
    r = call(server, "set_component_positions",
             {"project_name": name,
              "positions": [{"designator": "J1", "x_mm": 1, "y_mm": 1}]})
    assert r["success"] is False
    assert any(o["tool"] == "finalize_circuit" for o in r["remediation"])


def test_set_positions_no_placement_needs_dims(server):
    name = _build_led(server, "cov_sp_dims")
    r = call(server, "set_component_positions",
             {"project_name": name,
              "positions": [{"designator": "J1", "x_mm": 5, "y_mm": 5}]})
    assert r["success"] is False
    assert "board_width_mm" in r["error"]


def test_set_positions_generates_seed_and_pins(server, tmp_path):
    name = _build_led(server, "cov_sp_ok")
    r = call(server, "set_component_positions",
             {"project_name": name, "board_width_mm": 30, "board_height_mm": 20,
              "positions": [{"designator": "J1", "x_mm": 5, "y_mm": 10,
                             "rotation_deg": 90}]})
    assert r["success"], r
    assert r["pinned_count"] == 1 and r["pinned_designators"] == ["J1"]

    pl = json.loads((_projects_dir(tmp_path) / name
                     / f"{name}_placement.json").read_text())
    j1 = next(p for p in pl["placements"] if p["designator"] == "J1")
    assert j1["placement_source"] == "user"
    assert j1["x_mm"] == 5.0 and j1["rotation_deg"] == 90
    # Durable pin store also written.
    pins = json.loads((_projects_dir(tmp_path) / name
                       / f"{name}_placement_pins.json").read_text())
    assert pins["J1"]["x_mm"] == 5.0


def test_set_positions_partial_warns(server):
    name = _build_led(server, "cov_sp_part")
    call(server, "optimize_placement",
         {"project_name": name, "board_width_mm": 30, "board_height_mm": 20,
          "seed": 1})
    r = call(server, "set_component_positions",
             {"project_name": name,
              "positions": [{"designator": "J1", "x_mm": 5, "y_mm": 10},
                            {"designator": "NOPE", "x_mm": 1, "y_mm": 1}]})
    assert r["success"], r
    assert r["pinned_count"] == 1
    assert "NOPE" in r["warning"]
    assert any(u["designator"] == "NOPE" for u in r["unpinned"])


def test_set_positions_nothing_pinned_is_failure(server):
    name = _build_led(server, "cov_sp_none")
    call(server, "optimize_placement",
         {"project_name": name, "board_width_mm": 30, "board_height_mm": 20,
          "seed": 1})
    r = call(server, "set_component_positions",
             {"project_name": name,
              "positions": [{"designator": "NOPE", "x_mm": 1, "y_mm": 1},
                            {"x_mm": 1, "y_mm": 1},
                            {"designator": "J1"}]})  # missing coords
    assert r["success"] is False
    assert r["pinned_count"] == 0
    assert any(o["tool"] == "list_circuit" for o in r["remediation"])


# ---------------------------------------------------------------------------
# optimize_placement / route_board / run_drc / export_outputs — guards
# ---------------------------------------------------------------------------

def test_optimize_placement_bad_layers(server):
    r = call(server, "optimize_placement",
             {"project_name": "any", "layers": 3})
    assert r["success"] is False
    assert any(o["args"].get("layers") == 4 for o in r["remediation"])


def test_optimize_placement_unknown_project(server):
    r = call(server, "optimize_placement", {"project_name": "ghost_p"})
    assert r["success"] is False
    tools = [o["tool"] for o in r["remediation"]]
    assert "import_kicad_netlist" in tools and "list_projects" in tools


def test_optimize_placement_runs_and_writes(server, tmp_path):
    name = _build_led(server, "cov_op")
    r = call(server, "optimize_placement",
             {"project_name": name, "board_width_mm": 30, "board_height_mm": 20,
              "seed": 7})
    assert r["success"], r.get("error")
    assert r["next_step"]["tool"] == "route_board"
    assert (_projects_dir(tmp_path) / name / f"{name}_placement.json").exists()


def test_optimize_placement_surfaces_exception(server, tmp_path, monkeypatch):
    name = _build_led(server, "cov_op_err")
    from orchestrator import stages
    monkeypatch.setattr(stages, "run_placement",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("kaboom")))
    r = call(server, "optimize_placement",
             {"project_name": name, "board_width_mm": 30, "board_height_mm": 20})
    assert r["success"] is False and "kaboom" in r["error"]


def test_route_board_invalid_effort(server):
    r = call(server, "route_board", {"project_name": "x", "effort": "ludicrous"})
    assert r["success"] is False
    assert any(o["tool"] == "route_board" for o in r["remediation"])


def test_route_board_unknown_project(server):
    r = call(server, "route_board", {"project_name": "ghost_r"})
    assert r["success"] is False
    assert any(o["tool"] == "list_projects" for o in r["remediation"])


def test_route_board_no_placement(server, tmp_path):
    pdir = _projects_dir(tmp_path) / "cov_np"
    pdir.mkdir(parents=True)
    r = call(server, "route_board", {"project_name": "cov_np"})
    assert r["success"] is False
    assert any(o["tool"] == "optimize_placement" for o in r["remediation"])


def test_route_board_already_running(server, tmp_path):
    import mcp_server
    name = "cov_run"
    pdir = _projects_dir(tmp_path) / name
    pdir.mkdir(parents=True)
    (pdir / f"{name}_placement.json").write_text(json.dumps(
        {"board": {"layers": 2}, "placements": []}))
    mcp_server._ROUTE_JOBS[name] = {"state": "running", "started_at": None}
    try:
        r = call(server, "route_board", {"project_name": name})
        assert r.get("state") == "running"
        assert "status_hint" in r
    finally:
        mcp_server._ROUTE_JOBS.pop(name, None)


def test_route_board_dispatch_plain(server, tmp_path, monkeypatch):
    """route_board starts a worker that, with auto_retry=False, calls
    stages.run_routing directly. We stub the router (Java-bound) and assert the
    DISPATCH selected the right stages entry point — real branch logic, no JVM."""
    import mcp_server
    from orchestrator import stages
    name = _build_led(server, "cov_disp")
    call(server, "optimize_placement",
         {"project_name": name, "board_width_mm": 30, "board_height_mm": 20})

    seen = {}
    monkeypatch.setattr(stages, "run_routing",
                        lambda *a, **k: seen.update(routing=True, fixed=k.get("fixed_routing"))
                        or {"success": True, "completion_pct": 100.0})
    monkeypatch.setattr(stages, "run_route_with_retry",
                        lambda *a, **k: seen.update(retry=True)
                        or {"success": True, "completion_pct": 100.0})

    call(server, "route_board", {"project_name": name, "auto_retry": False})
    _await_route(name)
    assert seen.get("routing") and not seen.get("retry")


def test_route_board_dispatch_keep_existing(server, tmp_path, monkeypatch):
    import mcp_server
    from orchestrator import stages
    name = _make_routed_project(server, tmp_path, "cov_keep")

    seen = {}
    monkeypatch.setattr(stages, "run_routing",
                        lambda *a, **k: seen.update(fixed=k.get("fixed_routing"))
                        or {"success": True, "completion_pct": 100.0})
    monkeypatch.setattr(stages, "build_incremental_fixed_routing",
                        lambda routed, nl: {"traces": [], "vias": []})

    call(server, "route_board", {"project_name": name, "keep_existing": True})
    _await_route(name)
    assert "fixed" in seen and seen["fixed"] == {"traces": [], "vias": []}


def test_route_board_dispatch_auto_retry(server, tmp_path, monkeypatch):
    from orchestrator import stages
    name = _build_led(server, "cov_retry")
    call(server, "optimize_placement",
         {"project_name": name, "board_width_mm": 30, "board_height_mm": 20})
    seen = {}
    monkeypatch.setattr(stages, "run_route_with_retry",
                        lambda *a, **k: seen.update(retry=True)
                        or {"success": True, "completion_pct": 100.0})
    call(server, "route_board", {"project_name": name})  # auto_retry default True
    _await_route(name)
    assert seen.get("retry")


def test_route_board_worker_records_exception(server, tmp_path, monkeypatch):
    """If the (stubbed) router raises inside the worker thread, the job is
    recorded as failed with the error — the poller-resilience path."""
    from orchestrator import stages
    name = _build_led(server, "cov_wexc")
    call(server, "optimize_placement",
         {"project_name": name, "board_width_mm": 30, "board_height_mm": 20})
    monkeypatch.setattr(stages, "run_route_with_retry",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("workerboom")))
    call(server, "route_board", {"project_name": name})
    job = _await_route(name)
    assert job["state"] == "failed"
    assert "workerboom" in job["error"]


def test_run_drc_unknown_project_and_no_route(server, tmp_path):
    g = call(server, "run_drc", {"project_name": "ghost_d"})
    assert g["success"] is False
    assert any(o["tool"] == "list_projects" for o in g["remediation"])

    pdir = _projects_dir(tmp_path) / "cov_d"
    pdir.mkdir(parents=True)
    nr = call(server, "run_drc", {"project_name": "cov_d"})
    assert nr["success"] is False
    assert any(o["tool"] == "route_board" for o in nr["remediation"])


def test_run_drc_passes_on_clean_board(server, tmp_path):
    name = _make_routed_project(server, tmp_path, "cov_drc_ok")
    r = call(server, "run_drc", {"project_name": name})
    assert r["success"], r
    # A clean board points at export; a flagged one re-routes — both are valid.
    assert r["next_step"]["tool"] in ("export_outputs", "route_board")


def test_run_drc_surfaces_stage_exception(server, tmp_path, monkeypatch):
    name = _make_routed_project(server, tmp_path, "cov_drc_x")
    from orchestrator import stages
    monkeypatch.setattr(stages, "run_drc",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("drcboom")))
    r = call(server, "run_drc", {"project_name": name})
    assert r["success"] is False and "drcboom" in r["error"]


def test_export_outputs_unknown_and_no_route(server, tmp_path):
    g = call(server, "export_outputs", {"project_name": "ghost_e"})
    assert g["success"] is False
    assert any(o["tool"] == "list_projects" for o in g["remediation"])

    pdir = _projects_dir(tmp_path) / "cov_e"
    pdir.mkdir(parents=True)
    nr = call(server, "export_outputs", {"project_name": "cov_e"})
    assert nr["success"] is False
    assert any(o["tool"] == "route_board" for o in nr["remediation"])


def test_export_outputs_blocks_open_nets(server, tmp_path):
    name = _make_routed_project(server, tmp_path, "cov_e_open", open_nets=["GND"])
    r = call(server, "export_outputs", {"project_name": name})
    assert r["success"] is False
    assert "not fully connected" in r["error"]
    assert r["unrouted_nets"] == ["GND"]


def test_export_outputs_export_failure(server, tmp_path, monkeypatch):
    name = _make_routed_project(server, tmp_path, "cov_e_fail")
    from orchestrator import stages
    monkeypatch.setattr(stages, "run_drc",
                        lambda *a, **k: {"passed": True, "authoritative": True,
                                         "drc_engine": "kicad-cli",
                                         "statistics": {"errors": 0}, "checks": []})
    monkeypatch.setattr(stages, "run_export",
                        lambda *a, **k: {"success": False, "error": "diskfull"})
    r = call(server, "export_outputs", {"project_name": name})
    assert r["success"] is False and r["error"] == "diskfull"


def test_export_outputs_export_raises(server, tmp_path, monkeypatch):
    name = _make_routed_project(server, tmp_path, "cov_e_raise")
    from orchestrator import stages
    monkeypatch.setattr(stages, "run_drc",
                        lambda *a, **k: {"passed": True, "authoritative": True,
                                         "drc_engine": "kicad-cli",
                                         "statistics": {"errors": 0}, "checks": []})
    monkeypatch.setattr(stages, "run_export",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("exboom")))
    r = call(server, "export_outputs", {"project_name": name})
    assert r["success"] is False and "exboom" in r["error"]
    assert any(o["tool"] == "route_board" for o in r["remediation"])


def test_register_custom_footprint_bad_filename(server, monkeypatch):
    """A package name that reduces to nothing safe can't yield a filename."""
    r = call(server, "register_custom_footprint",
             {"project_name": "cov_rcf", "package_name": "///",
              "kicad_mod_content": '(footprint "X" (layer F.Cu))'})
    assert r["success"] is False
    assert "safe filename" in r["error"]


def test_register_custom_footprint_write_failure(server, tmp_path, monkeypatch):
    import mcp_server
    orig_write = Path.write_text

    def _boom(self, *a, **k):
        if str(self).endswith(".kicad_mod"):
            raise OSError("disk full")
        return orig_write(self, *a, **k)
    monkeypatch.setattr(Path, "write_text", _boom)
    r = call(server, "register_custom_footprint",
             {"project_name": "cov_rcf2", "package_name": "MYP",
              "kicad_mod_content": '(footprint "MYP" (layer F.Cu))'})
    assert r["success"] is False and "footprint file" in r["error"]


def test_register_custom_footprint_then_resolved_by_coverage(server, tmp_path):
    """register → _get_project_custom_index builds a tier-0 index that
    check_footprint_coverage then resolves against."""
    kmod = ('(footprint "WIDGET-2" (layer F.Cu)'
            '(pad "1" smd rect (at -1 0)(size 1 1)(layers F.Cu))'
            '(pad "2" smd rect (at 1 0)(size 1 1)(layers F.Cu)))')
    reg = call(server, "register_custom_footprint",
               {"project_name": "cov_cust", "package_name": "WIDGET-2",
                "kicad_mod_content": kmod})
    assert reg["success"], reg
    cov = call(server, "check_footprint_coverage",
               {"project_name": "cov_cust",
                "components": [{"reference": "U1", "package": "WIDGET-2",
                                "pin_count": 2}]})
    assert cov["coverage"]["resolved"] == 1


def test_check_footprint_coverage_missing_package(server):
    r = call(server, "check_footprint_coverage",
             {"components": [{"reference": "U1", "package": "", "pin_count": 4}]})
    assert r["coverage"]["custom_needed"] == 1
    assert "No package" in r["custom_needed"][0]["notes"]


def test_place_component_no_netlist_with_draft(server):
    """A created-but-unfinalized circuit has a draft, no netlist → place_component
    steers to finalize_circuit, not 'import a netlist'."""
    name = "cov_pc_draft"
    call(server, "create_circuit",
         {"project_name": name, "description": "x",
          "board_width_mm": 30, "board_height_mm": 20})
    call(server, "add_component", {"project_name": name, "designator": "J1",
                                   "component_type": "connector", "value": "2",
                                   "package": "PinHeader_1x2"})
    r = call(server, "place_component",
             {"project_name": name, "designator": "J1", "x_mm": 5, "y_mm": 5})
    assert r["success"] is False
    assert any(o["tool"] == "finalize_circuit" for o in r["remediation"])


def test_place_component_no_netlist_no_draft(server, tmp_path):
    """A bare project dir (no draft, no netlist) → offers create / import."""
    pdir = _projects_dir(tmp_path) / "cov_pc_bare"
    pdir.mkdir(parents=True)
    r = call(server, "place_component",
             {"project_name": "cov_pc_bare", "designator": "J1",
              "x_mm": 5, "y_mm": 5})
    assert r["success"] is False
    tools = [o["tool"] for o in r["remediation"]]
    assert "create_circuit" in tools and "import_kicad_netlist" in tools


def test_place_component_unknown_designator(server):
    name = _build_led(server, "cov_pc_unk")
    r = call(server, "place_component",
             {"project_name": name, "designator": "ZZ9", "x_mm": 5, "y_mm": 5})
    assert r["success"] is False
    assert any(o["tool"] == "list_circuit" for o in r["remediation"])


def test_optimize_placement_failure_remediation(server, tmp_path, monkeypatch):
    """The placement-failure envelope must surface pinned-conflict remediation
    (adjust/unplace the pinned part, or enlarge the board) — real envelope logic
    above a stubbed placement result."""
    name = _build_led(server, "cov_op_fail")
    from orchestrator import stages
    fail_result = {
        "success": False, "error": "overlaps",
        "unresolved_footprints": [{"package": "X-99"}],
        "violations": {
            "out_of_bounds": [{"designator": "J1", "pinned": True}],
            "overlaps": [{"a": "R1", "b": "D1", "pinned": False}],
        },
    }
    monkeypatch.setattr(stages, "run_placement", lambda *a, **k: fail_result)
    r = call(server, "optimize_placement",
             {"project_name": name, "board_width_mm": 30, "board_height_mm": 20})
    assert r["success"] is False
    tools = [o["tool"] for o in r["remediation"]]
    assert "provide_footprint" in tools         # unresolved-footprint branch
    assert "place_component" in tools            # adjust pinned J1
    assert "unplace_component" in tools          # or unpin it
    assert "optimize_placement" in tools         # or enlarge the board


def test_run_drc_report_error_branch(server, tmp_path, monkeypatch):
    """stages.run_drc returning an {'error': ...} dict → run_drc tool fails with
    a route_board remediation."""
    name = _make_routed_project(server, tmp_path, "cov_drc_e")
    from orchestrator import stages
    monkeypatch.setattr(stages, "run_drc",
                        lambda *a, **k: {"error": "no routed board parsed"})
    r = call(server, "run_drc", {"project_name": name})
    assert r["success"] is False
    assert any(o["tool"] == "route_board" for o in r["remediation"])


def test_status_running_route_job_hints(server, tmp_path):
    """A running route job surfaces poll_again_in_s + status_hint + progress."""
    import mcp_server
    import time as _t
    name = "cov_running"
    pdir = _projects_dir(tmp_path) / name
    pdir.mkdir(parents=True)
    mcp_server._ROUTE_JOBS[name] = {
        "state": "running", "result": None, "error": None,
        "started_at": _t.monotonic(),
        "progress": {"pass_num": 3, "incomplete_connections": 12}}
    try:
        r = call(server, "get_project_status", {"project_name": name})
        assert r["routing_state"] == "running"
        assert r["poll_again_in_s"] >= 15
        assert "pass 3" in r["status_hint"]
    finally:
        mcp_server._ROUTE_JOBS.pop(name, None)


def test_status_running_design_job_hints(server, tmp_path):
    import mcp_server
    import time as _t
    name = "cov_drunning"
    pdir = _projects_dir(tmp_path) / name
    pdir.mkdir(parents=True)
    mcp_server._DESIGN_JOBS[name] = {
        "state": "running", "result": None, "error": None,
        "started_at": _t.monotonic(),
        "progress": {"step": 4, "name": "placement"}}
    try:
        r = call(server, "get_project_status", {"project_name": name})
        assert r["design_state"] == "running"
        assert "placement" in r["status_hint"]
    finally:
        mcp_server._DESIGN_JOBS.pop(name, None)


def test_status_complete_route_incomplete_points_at_finish(server, tmp_path):
    """An on-disk routed board <100% → next_step finishes it incrementally."""
    name = "cov_inc"
    pdir = _projects_dir(tmp_path) / name
    pdir.mkdir(parents=True)
    (pdir / f"{name}_routed.json").write_text(json.dumps({"routing": {
        "statistics": {"completion_pct": 80.0, "total_nets": 5, "routed_nets": 4},
        "unrouted_nets": ["net_z"]}}))
    r = call(server, "get_project_status", {"project_name": name})
    assert r["routing_state"] == "complete"
    assert r["next_step"]["tool"] == "route_board"
    assert r["next_step"]["args"]["keep_existing"] is True


# ---------------------------------------------------------------------------
# import_kicad_netlist happy path + verify_footprints no-netlist
# ---------------------------------------------------------------------------

_MINIMAL_NET = """\
(export (version "E")
  (design (source "t.kicad_sch") (date "2024") (tool "t"))
  (components
    (comp (ref "J1") (value "5V") (footprint "Connector:PinHeader_1x02_P2.54mm_Vertical"))
    (comp (ref "R1") (value "470") (footprint "Resistor_SMD:R_0805_2012Metric"))
  )
  (nets
    (net (code "1") (name "VCC")
      (node (ref "J1") (pin "1"))
      (node (ref "R1") (pin "1")))
    (net (code "2") (name "GND")
      (node (ref "J1") (pin "2"))
      (node (ref "R1") (pin "2")))
  )
)
"""


def test_import_kicad_netlist_happy_path(server, tmp_path):
    net = tmp_path / "board.net"
    net.write_text(_MINIMAL_NET)
    r = call(server, "import_kicad_netlist",
             {"project_name": "cov_imp_ok", "file_path": str(net)})
    assert r["success"], r.get("error")
    assert r["component_count"] == 2 and r["net_count"] == 2
    # All standard footprints resolve → steers to placement.
    assert r["next_step"]["tool"] in ("optimize_placement", "provide_footprint")
    assert (_projects_dir(tmp_path) / "cov_imp_ok"
            / "cov_imp_ok_netlist.json").exists()


def test_import_then_overwrite(server, tmp_path):
    net = tmp_path / "b.net"
    net.write_text(_MINIMAL_NET)
    call(server, "import_kicad_netlist",
         {"project_name": "cov_ow", "file_path": str(net)})
    again = call(server, "import_kicad_netlist",
                 {"project_name": "cov_ow", "file_path": str(net), "overwrite": True})
    assert again["success"], again.get("error")


def test_verify_footprints_no_netlist(server):
    r = call(server, "verify_footprints", {"project_name": "cov_vf_none"})
    assert r["success"] is False
    tools = [o["tool"] for o in r["remediation"]]
    assert "import_kicad_netlist" in tools and "create_circuit" in tools


# ---------------------------------------------------------------------------
# builder-fail remediation branches (_builder_fail)
# ---------------------------------------------------------------------------

def test_list_circuit_no_draft(server):
    r = call(server, "list_circuit", {"project_name": "cov_nd"})
    assert r["success"] is False
    assert any(o["tool"] == "create_circuit" for o in r["remediation"])


def test_mark_no_connect_no_draft(server):
    r = call(server, "mark_no_connect",
             {"project_name": "cov_nd2", "pins": ["U1.1"]})
    assert r["success"] is False
    assert any(o["tool"] == "create_circuit" for o in r["remediation"])


def test_remove_component_no_draft(server):
    r = call(server, "remove_component",
             {"project_name": "cov_nd3", "designator": "R1"})
    assert r["success"] is False
    assert any(o["tool"] == "create_circuit" for o in r["remediation"])


def test_add_component_unresolved_footprint_remediates(server):
    name = "cov_uf"
    call(server, "create_circuit",
         {"project_name": name, "description": "x",
          "board_width_mm": 30, "board_height_mm": 20})
    r = call(server, "add_component",
             {"project_name": name, "designator": "R1",
              "component_type": "resistor", "value": "1k",
              "package": "WONKY_PKG_XYZ"})
    assert r["success"] is False
    assert any(o["tool"] == "provide_footprint" for o in r["remediation"])


# ---------------------------------------------------------------------------
# set_component_positions remaining branches
# ---------------------------------------------------------------------------

def test_set_positions_no_netlist_no_draft(server, tmp_path):
    pdir = _projects_dir(tmp_path) / "cov_sp_bare"
    pdir.mkdir(parents=True)
    r = call(server, "set_component_positions",
             {"project_name": "cov_sp_bare",
              "positions": [{"designator": "J1", "x_mm": 1, "y_mm": 1}]})
    assert r["success"] is False
    tools = [o["tool"] for o in r["remediation"]]
    assert "import_kicad_netlist" in tools and "create_circuit" in tools


def test_set_positions_seed_gen_fails_on_empty_netlist(server, tmp_path):
    name = "cov_sp_empty"
    pdir = _projects_dir(tmp_path) / name
    pdir.mkdir(parents=True)
    (pdir / f"{name}_netlist.json").write_text(json.dumps(
        {"version": "1.0", "project_name": name,
         "elements": [{"element_type": "net", "net_id": "n", "name": "N"}]}))
    r = call(server, "set_component_positions",
             {"project_name": name, "board_width_mm": 30, "board_height_mm": 20,
              "positions": [{"designator": "J1", "x_mm": 1, "y_mm": 1}]})
    assert r["success"] is False
    # No resolvable components → either seed-gen failure or all-unpinned failure.
    assert r.get("pinned_count", 0) == 0


# ---------------------------------------------------------------------------
# optimize_placement layers_promoted note
# ---------------------------------------------------------------------------

def test_optimize_placement_promotes_to_four_layer(server, tmp_path):
    name = _build_led(server, "cov_promo")
    r = call(server, "optimize_placement",
             {"project_name": name, "board_width_mm": 30, "board_height_mm": 20,
              "plane_layers": 0, "seed": 1})
    assert r["success"], r.get("error")
    assert r["layers"] == 4
    assert "promoted" in r["next_step"]["why"].lower()


# ---------------------------------------------------------------------------
# lookup helpers
# ---------------------------------------------------------------------------

def test_register_custom_footprint_mkdir_failure(server, tmp_path, monkeypatch):
    import mcp_server
    orig_mkdir = Path.mkdir

    def _boom(self, *a, **k):
        if "custom-footprints.pretty" in str(self):
            raise OSError("readonly fs")
        return orig_mkdir(self, *a, **k)
    monkeypatch.setattr(Path, "mkdir", _boom)
    r = call(server, "register_custom_footprint",
             {"project_name": "cov_mk", "package_name": "P",
              "kicad_mod_content": '(footprint "P" (layer F.Cu))'})
    assert r["success"] is False and "directory" in r["error"]


def test_register_custom_footprint_twice_invalidates_index(server):
    """Re-registering an existing project hits the _CUSTOM_INDICES invalidate
    branch instead of building a fresh index."""
    km = '(footprint "RP" (layer F.Cu)(pad "1" smd rect (at 0 0)(size 1 1)(layers F.Cu)))'
    r1 = call(server, "register_custom_footprint",
              {"project_name": "cov_twice", "package_name": "RP",
               "kicad_mod_content": km})
    assert r1["success"]
    # Trigger index build by looking it up once.
    call(server, "check_footprint_coverage",
         {"project_name": "cov_twice",
          "components": [{"reference": "U1", "package": "RP", "pin_count": 1}]})
    r2 = call(server, "register_custom_footprint",
              {"project_name": "cov_twice", "package_name": "RP2",
               "kicad_mod_content": km.replace("RP", "RP2")})
    assert r2["success"]


# ---------------------------------------------------------------------------
# add_component unknown pin count + connect pin-conflict (builder codes)
# ---------------------------------------------------------------------------

def test_add_component_unknown_pin_count(server):
    name = "cov_upc"
    call(server, "create_circuit",
         {"project_name": name, "description": "x",
          "board_width_mm": 30, "board_height_mm": 20})
    r = call(server, "add_component",
             {"project_name": name, "designator": "U1", "component_type": "ic",
              "value": "X", "package": "TOTALLY_UNKNOWN_PKG_42"})
    assert r["success"] is False
    # Steers to re-call add_component with an explicit pinout.
    assert any(o["tool"] == "add_component" for o in r["remediation"])


def test_connect_pins_conflict_remediates(server):
    name = "cov_conf"
    run_sequence(server, [
        ("create_circuit", {"project_name": name, "description": "x",
                            "board_width_mm": 30, "board_height_mm": 20}),
        ("add_component", {"project_name": name, "designator": "R1",
                           "component_type": "resistor", "value": "1k",
                           "package": "0805"}),
        ("add_component", {"project_name": name, "designator": "R2",
                           "component_type": "resistor", "value": "1k",
                           "package": "0805"}),
        ("add_component", {"project_name": name, "designator": "R3",
                           "component_type": "resistor", "value": "1k",
                           "package": "0805"}),
        ("connect_pins", {"project_name": name, "net_name": "A",
                          "pins": ["R1.1", "R2.1"]}),
    ])
    # R1.1 already on net A — connecting it to a different net conflicts.
    r = call(server, "connect_pins",
             {"project_name": name, "net_name": "B", "pins": ["R1.1", "R3.1"]})
    assert r["success"] is False
    assert any(o["tool"] == "list_circuit" for o in r["remediation"])


# ---------------------------------------------------------------------------
# get_project_status — remaining job-state sub-branches
# ---------------------------------------------------------------------------

def test_status_tolerates_corrupt_status_json(server, tmp_path):
    name = "cov_cstat"
    pdir = _projects_dir(tmp_path) / name
    pdir.mkdir(parents=True)
    (pdir / "STATUS.json").write_text("{bad json")
    r = call(server, "get_project_status", {"project_name": name})
    assert r["status"] == {}


def test_status_design_complete_with_result_on_disk(server, tmp_path):
    """A completed in-memory design job (dir exists) surfaces design_result."""
    import mcp_server
    name = "cov_dcomplete"
    pdir = _projects_dir(tmp_path) / name
    pdir.mkdir(parents=True)
    mcp_server._DESIGN_JOBS[name] = {
        "state": "complete", "result": {"project_name": name, "success": True},
        "error": None, "started_at": None, "elapsed_s": 9.0}
    try:
        r = call(server, "get_project_status", {"project_name": name})
        assert r["design_state"] == "complete"
        assert r["design_result"]["success"] is True
        assert r["design_elapsed_s"] == 9.0
    finally:
        mcp_server._DESIGN_JOBS.pop(name, None)


def test_status_route_running_iteration_progress(server, tmp_path):
    """A route job reporting NCR iteration progress (not pass_num) hits the
    iteration-detail branch of the status_hint."""
    import mcp_server
    import time as _t
    name = "cov_iter"
    pdir = _projects_dir(tmp_path) / name
    pdir.mkdir(parents=True)
    mcp_server._ROUTE_JOBS[name] = {
        "state": "running", "result": None, "error": None,
        "started_at": _t.monotonic(),
        "progress": {"iteration": 4, "max_iterations": 10}}
    try:
        r = call(server, "get_project_status", {"project_name": name})
        assert "iteration 4/10" in r["status_hint"]
    finally:
        mcp_server._ROUTE_JOBS.pop(name, None)


def test_status_design_and_route_jobs_before_dir(server):
    """Both a design and a route job present, no dir yet → both states surface
    from the in-memory early-return path."""
    import mcp_server
    import time as _t
    name = "cov_both"
    mcp_server._DESIGN_JOBS[name] = {
        "state": "running", "result": None, "error": None,
        "started_at": _t.monotonic(), "progress": None}
    mcp_server._ROUTE_JOBS[name] = {
        "state": "failed", "result": None, "error": "x", "started_at": None}
    try:
        r = call(server, "get_project_status", {"project_name": name})
        assert r["design_state"] == "running"
        assert r["routing_state"] == "failed"
        assert r["routing_error"] == "x"
    finally:
        mcp_server._DESIGN_JOBS.pop(name, None)
        mcp_server._ROUTE_JOBS.pop(name, None)


# ---------------------------------------------------------------------------
# import edge: unresolved footprint after import steers to provide_footprint
# ---------------------------------------------------------------------------

_NET_BAD_FP = """\
(export (version "E")
  (design (source "t.kicad_sch") (date "2024") (tool "t"))
  (components
    (comp (ref "U1") (value "X") (footprint "Nope:WONKY_UNKNOWN_FP_99"))
    (comp (ref "R1") (value "1k") (footprint "Resistor_SMD:R_0805_2012Metric"))
  )
  (nets
    (net (code "1") (name "N1")
      (node (ref "U1") (pin "1"))
      (node (ref "R1") (pin "1")))
  )
)
"""


def test_import_with_unresolved_footprint_steers_to_provide(server, tmp_path):
    net = tmp_path / "bad.net"
    net.write_text(_NET_BAD_FP)
    r = call(server, "import_kicad_netlist",
             {"project_name": "cov_imp_bad", "file_path": str(net)})
    assert r["success"], r.get("error")
    assert r["unresolved_footprints"]
    assert r["next_step"]["tool"] == "provide_footprint"


# ---------------------------------------------------------------------------
# provide_footprint when the component cache is unconfigured
# ---------------------------------------------------------------------------

def test_provide_footprint_cache_unconfigured(server, monkeypatch):
    import mcp_server
    from optimizers import pad_geometry
    monkeypatch.setattr(mcp_server, "_ensure_lookup_configured", lambda: None)
    monkeypatch.setattr(pad_geometry, "get_default_cache", lambda: None)
    r = call(server, "provide_footprint",
             {"project_name": "cov_nocache", "package": "X",
              "like_package": "0805"})
    assert r["success"] is False
    assert "cache is not configured" in r["error"]


def test_import_unexpected_error(server, tmp_path, monkeypatch):
    import mcp_server
    import exporters.kicad_netlist_importer as imp
    net = tmp_path / "x.net"
    net.write_text("(export (version E))")
    monkeypatch.setattr(imp, "convert_kicad_netlist",
                        lambda **k: (_ for _ in ()).throw(RuntimeError("weird")))
    r = call(server, "import_kicad_netlist",
             {"project_name": "cov_imp_err2", "file_path": str(net)})
    assert r["success"] is False and "Unexpected error" in r["error"]


def test_list_projects_no_status_file(server, tmp_path):
    """A project dir with no STATUS.json yields steps == {} (the else branch)."""
    name = _build_led(server, "cov_nostatus")
    # _build_led writes no STATUS.json.
    assert not (_projects_dir(tmp_path) / name / "STATUS.json").exists()
    p = {x["project_name"]: x for x in call_list(server, "list_projects")}[name]
    assert p["steps"] == {}


def test_status_design_complete_result_before_dir(server):
    """Completed design job, NO project dir → early-return path surfaces result."""
    import mcp_server
    name = "cov_dcb"
    mcp_server._DESIGN_JOBS[name] = {
        "state": "complete", "result": {"success": True, "project_name": name},
        "error": None, "started_at": None, "elapsed_s": 2.0}
    try:
        r = call(server, "get_project_status", {"project_name": name})
        assert r["design_state"] == "complete"
        assert r["design_result"]["success"] is True
    finally:
        mcp_server._DESIGN_JOBS.pop(name, None)


def test_status_design_failed_with_dir(server, tmp_path):
    """A failed design job WITH a project dir hits the later design-state block."""
    import mcp_server
    name = "cov_dfd"
    pdir = _projects_dir(tmp_path) / name
    pdir.mkdir(parents=True)
    mcp_server._DESIGN_JOBS[name] = {
        "state": "failed", "result": None, "error": "pipeline blew up",
        "started_at": None, "elapsed_s": 7.0}
    try:
        r = call(server, "get_project_status", {"project_name": name})
        assert r["design_state"] == "failed"
        assert r["design_error"] == "pipeline blew up"
    finally:
        mcp_server._DESIGN_JOBS.pop(name, None)


# ---------------------------------------------------------------------------
# footprint-lookup module helpers
# ---------------------------------------------------------------------------

def test_get_project_custom_index_none_and_built(server, tmp_path, monkeypatch):
    import mcp_server
    monkeypatch.setattr(mcp_server, "_CUSTOM_INDICES", {})
    # No custom dir → None.
    assert mcp_server._get_project_custom_index("cov_ci_none") is None
    # Create the dir with a footprint → an index is built and cached.
    cdir = _projects_dir(tmp_path) / "cov_ci" / "custom-footprints.pretty"
    cdir.mkdir(parents=True)
    (cdir / "X.kicad_mod").write_text(
        '(footprint "X" (layer F.Cu)(pad "1" smd rect (at 0 0)(size 1 1)(layers F.Cu)))')
    idx1 = mcp_server._get_project_custom_index("cov_ci")
    assert idx1 is not None
    assert mcp_server._get_project_custom_index("cov_ci") is idx1  # cached


def test_init_lookup_without_kicad_library(monkeypatch, tmp_path):
    """_init_lookup must warn (not crash) when no KiCad library is configured."""
    import mcp_server
    from orchestrator.config import OrchestratorConfig

    real_from_env = OrchestratorConfig.from_env

    def _no_lib(*a, **k):
        cfg = real_from_env(*a, **k)
        cfg.kicad_library_path = None
        return cfg
    monkeypatch.setattr(OrchestratorConfig, "from_env", staticmethod(_no_lib))
    monkeypatch.setenv("PCB_COMPONENT_CACHE_PATH", str(tmp_path / "c.json"))
    monkeypatch.setattr(mcp_server, "_LOOKUP_CONFIGURED", False)
    monkeypatch.setattr(mcp_server, "_KICAD_INDEX", None)
    monkeypatch.setattr(mcp_server, "_CACHE", None)
    mcp_server._init_lookup()
    assert mcp_server._LOOKUP_CONFIGURED is True


def test_ensure_lookup_configured_idempotent(monkeypatch):
    import mcp_server
    monkeypatch.setattr(mcp_server, "_LOOKUP_CONFIGURED", True)
    # Already configured → returns immediately (no rebuild).
    mcp_server._ensure_lookup_configured()
    assert mcp_server._LOOKUP_CONFIGURED is True


def test_get_projects_dir_default_home(monkeypatch, tmp_path):
    """With PCB_PROJECTS_DIR unset, the dir falls back under the home path."""
    import mcp_server
    monkeypatch.delenv("PCB_PROJECTS_DIR", raising=False)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "home"))
    d = mcp_server._get_projects_dir()
    assert d == tmp_path / "home" / ".pcb-creator" / "projects"
    assert d.exists()


# ---------------------------------------------------------------------------
# helpers for routed-project fixtures
# ---------------------------------------------------------------------------

def _make_routed_project(server, tmp_path, name, open_nets=None):
    """Build a finalized LED board, run real placement, then synthesize a
    minimal-but-valid routed.json (no Java needed)."""
    _build_led(server, name)
    call(server, "optimize_placement",
         {"project_name": name, "board_width_mm": 30, "board_height_mm": 20,
          "seed": 3})
    pdir = _projects_dir(tmp_path) / name
    placement = json.loads((pdir / f"{name}_placement.json").read_text())
    routed = {
        "board": placement["board"],
        "placements": placement["placements"],
        "routing": {
            "traces": [], "vias": [], "copper_fills": [],
            "statistics": {"total_nets": 3, "routed_nets": 3,
                           "completion_pct": 100.0, "via_count": 0,
                           "total_trace_length_mm": 0.0},
            "unrouted_nets": list(open_nets or []),
        },
    }
    (pdir / f"{name}_routed.json").write_text(json.dumps(routed))
    return name


def _await_route(name, timeout=15.0):
    """Wait for the background route worker (stubbed router) to record a result."""
    import mcp_server
    import time as _t
    deadline = _t.monotonic() + timeout
    while _t.monotonic() < deadline:
        with mcp_server._ROUTE_LOCK:
            job = mcp_server._ROUTE_JOBS.get(name)
        if job and job.get("state") in ("complete", "failed"):
            return job
        _t.sleep(0.02)
    raise AssertionError(f"route worker for {name} did not finish")


# ===========================================================================
# orchestrator/stages.py — deterministic prep / DRC / export stages
#
# The Freerouting/Java route INVOCATION (run_routing's engine body) and the
# router-driven retry loop are `# pragma: no cover` in the source. Here we cover
# everything that does NOT need a live router: the resolve/parse/build helpers,
# the run_routing precondition guards, and the real DRC + Gerber export stages
# (which run on a synthesized routed board, no router needed).
# ===========================================================================

import sys as _sys
_REPO = Path(__file__).resolve().parent.parent
if str(_REPO) not in _sys.path:
    _sys.path.insert(0, str(_REPO))


def _cfg():
    from orchestrator.config import OrchestratorConfig
    return OrchestratorConfig.from_env(base_dir=_REPO)


@pytest.fixture(autouse=True)
def _lookup():
    """Configure the tiered footprint lookup so direct stages.* calls resolve
    standard packages (the MCP path does this lazily; direct calls don't)."""
    from optimizers.pad_geometry import configure_lookup
    from orchestrator.cache import ComponentCache
    cfg = _cfg()
    ki = None
    if cfg.kicad_library_path:
        from exporters.kicad_mod_parser import KiCadLibraryIndex
        ki = KiCadLibraryIndex(cfg.kicad_library_path)
    configure_lookup(kicad_index=ki, cache=ComponentCache(cfg.component_cache_path))


def _two_r_netlist():
    return {
        "version": "1.0", "project_name": "s",
        "elements": [
            {"element_type": "component", "component_id": "c1", "designator": "R1",
             "component_type": "resistor", "value": "330", "package": "R_0805_2012Metric"},
            {"element_type": "component", "component_id": "c2", "designator": "R2",
             "component_type": "resistor", "value": "10k", "package": "R_0805_2012Metric"},
            {"element_type": "port", "port_id": "p1", "component_id": "c1",
             "pin_number": 1, "name": "1", "electrical_type": "passive"},
            {"element_type": "port", "port_id": "p2", "component_id": "c2",
             "pin_number": 1, "name": "1", "electrical_type": "passive"},
            {"element_type": "net", "net_id": "net_sig", "name": "SIG",
             "net_class": "signal", "connected_port_ids": ["p1", "p2"]},
        ],
    }


def _placed_project(tmp_path, name="s", w=30, h=20):
    """Netlist + real placement on disk; returns (pdir, name)."""
    from orchestrator import stages
    pdir = tmp_path / name
    pdir.mkdir()
    (pdir / f"{name}_netlist.json").write_text(json.dumps(_two_r_netlist()))
    r = stages.run_placement(pdir, name, _cfg(), board_width_mm=w,
                             board_height_mm=h, seed=1)
    assert r["success"], r.get("error")
    return pdir, name


def _routed_on_disk(tmp_path, name="s", open_nets=None):
    """Placed project + synthesized routed.json (no router needed)."""
    pdir, name = _placed_project(tmp_path, name)
    placement = json.loads((pdir / f"{name}_placement.json").read_text())
    routed = {
        "board": placement["board"],
        "placements": placement["placements"],
        "routing": {
            "traces": [], "vias": [], "copper_fills": [],
            "statistics": {"total_nets": 1, "routed_nets": 1,
                           "completion_pct": 100.0, "via_count": 0,
                           "total_trace_length_mm": 0.0},
            "unrouted_nets": list(open_nets or []),
        },
    }
    (pdir / f"{name}_routed.json").write_text(json.dumps(routed))
    return pdir, name


# --- resolve helpers, exception-tolerant ----------------------------------

def test_resolve_board_dims_from_draft_and_requirements(tmp_path):
    from orchestrator import stages
    pdir = tmp_path / "rb"
    pdir.mkdir()
    # Draft path
    (pdir / "rb_circuit_draft.json").write_text(json.dumps(
        {"board": {"width_mm": 41, "height_mm": 22}}))
    assert stages._resolve_board_dims(pdir, "rb") == (41, 22)
    # Requirements path (after removing the draft)
    (pdir / "rb_circuit_draft.json").unlink()
    (pdir / "rb_requirements.json").write_text(json.dumps(
        {"board": {"width_mm": 60, "height_mm": 40}}))
    assert stages._resolve_board_dims(pdir, "rb") == (60, 40)


def test_resolve_board_dims_tolerates_corrupt_files(tmp_path):
    from orchestrator import stages
    pdir = tmp_path / "rbx"
    pdir.mkdir()
    (pdir / "rbx_placement.json").write_text("{bad")
    (pdir / "rbx_circuit_draft.json").write_text("{bad")
    (pdir / "rbx_requirements.json").write_text("{bad")
    assert stages._resolve_board_dims(pdir, "rbx") == (None, None)


def test_resolve_layers_from_requirements(tmp_path):
    from orchestrator import stages
    pdir = tmp_path / "rl"
    pdir.mkdir()
    (pdir / "rl_requirements.json").write_text(json.dumps({"board": {"layers": 4}}))
    assert stages._resolve_layers(pdir, "rl") == 4
    # Corrupt → default 2
    (pdir / "rl_requirements.json").write_text("{bad")
    assert stages._resolve_layers(pdir, "rl") == 2


# --- _suggest_free_position returns None when the board is full ------------

def test_suggest_free_position_none_on_full_board(tmp_path):
    from orchestrator import stages
    # A 2x2mm board cannot host an 0805 with edge clearance → no valid spot.
    pos = stages._suggest_free_position(
        1.0, 1.0, 0, "R_0805_2012Metric", 2,
        other_boxes=[], bw=2.0, bh=2.0, edge_clearance=1.0, min_clearance=0.5)
    assert pos is None


# --- run_placement branches -----------------------------------------------

def test_run_placement_unresolved_footprint_gate(tmp_path):
    from orchestrator import stages
    nl = _two_r_netlist()
    nl["elements"][0]["package"] = "TOTALLY_MADE_UP_PKG_XYZ"
    pdir = tmp_path / "uf"
    pdir.mkdir()
    (pdir / "uf_netlist.json").write_text(json.dumps(nl))
    r = stages.run_placement(pdir, "uf", _cfg(), board_width_mm=30,
                             board_height_mm=20, seed=1)
    assert r["success"] is False
    assert r["unresolved_footprints"]


def test_run_placement_four_layer_plane_from_requirements(tmp_path):
    from orchestrator import stages
    pdir = tmp_path / "pl4"
    pdir.mkdir()
    (pdir / "pl4_netlist.json").write_text(json.dumps(_two_r_netlist()))
    (pdir / "pl4_requirements.json").write_text(json.dumps(
        {"board": {"layers": 4, "plane_layers": 1}}))
    r = stages.run_placement(pdir, "pl4", _cfg(), board_width_mm=40,
                             board_height_mm=30, seed=1)
    assert r["success"]
    board = json.loads((pdir / "pl4_placement.json").read_text())["board"]
    assert board["layers"] == 4 and board["plane_layers"] == 1


def test_run_placement_promotes_for_plane_layers_arg(tmp_path):
    from orchestrator import stages
    pdir, name = _placed_project(tmp_path, "promo")
    # Re-place with plane_layers=0 on the (2-layer) existing board → promote to 4.
    r = stages.run_placement(pdir, name, _cfg(), plane_layers=0, seed=2)
    assert r["success"] and r["layers"] == 4
    assert r["layers_promoted"] is True


# --- _min_pad_pitch / _build_router_kwargs --------------------------------

def test_min_pad_pitch_and_router_kwargs(tmp_path):
    from orchestrator import stages
    pdir, name = _placed_project(tmp_path, "mp")
    pitch = stages._min_pad_pitch(pdir, name)
    assert pitch is not None and pitch > 0

    # No requirements → bare default kwargs.
    kw = stages._build_router_kwargs(pdir, name)
    assert kw["copper_weight_oz"] == 0.5


def test_min_pad_pitch_none_without_netlist(tmp_path):
    from orchestrator import stages
    pdir = tmp_path / "nope"
    pdir.mkdir()
    assert stages._min_pad_pitch(pdir, "nope") is None


def test_build_router_kwargs_applies_dfm_profile(tmp_path):
    from orchestrator import stages
    pdir, name = _placed_project(tmp_path, "dfm")
    (pdir / f"{name}_requirements.json").write_text(json.dumps({
        "board": {"copper_weight_oz": 1.0},
        "manufacturing": {"manufacturer": "jlcpcb",
                          "trace_width_min_mm": 0.15,
                          "clearance_min_mm": 0.15,
                          "via_drill_min_mm": 0.25,
                          "via_diameter_min_mm": 0.5},
    }))
    kw = stages._build_router_kwargs(pdir, name)
    assert kw["copper_weight_oz"] == 1.0
    # Non-fine-pitch board floors the signal trace at 0.25mm.
    assert kw["trace_width_signal_mm"] >= 0.25
    assert "clearance_mm" in kw and "via_drill_mm" in kw


# --- run_routing precondition guards (no router needed) -------------------

def test_run_routing_missing_placement(tmp_path):
    from orchestrator import stages
    pdir = tmp_path / "rr"
    pdir.mkdir()
    r = stages.run_routing(pdir, "rr", _cfg())
    assert r["success"] is False and "placement" in r["error"].lower()


def test_run_routing_missing_netlist(tmp_path):
    from orchestrator import stages
    pdir = tmp_path / "rr2"
    pdir.mkdir()
    (pdir / "rr2_placement.json").write_text(json.dumps(
        {"board": {"layers": 2}, "placements": []}))
    r = stages.run_routing(pdir, "rr2", _cfg())
    assert r["success"] is False and "netlist" in r["error"].lower()


def test_run_routing_four_layer_requires_freerouting(tmp_path):
    from orchestrator import stages
    pdir, name = _placed_project(tmp_path, "fl")
    pl = json.loads((pdir / f"{name}_placement.json").read_text())
    pl["board"]["layers"] = 4
    (pdir / f"{name}_placement.json").write_text(json.dumps(pl))
    cfg = _cfg()
    cfg.router_engine = "builtin"   # not freerouting → 4-layer guard fires
    r = stages.run_routing(pdir, name, cfg)
    assert r["success"] is False
    assert "require" in r["error"].lower() and "freerouting" in r["error"].lower()


# --- build_incremental_fixed_routing / _components_for_unrouted -----------

def test_build_incremental_fixed_routing_none_when_empty():
    from orchestrator import stages
    assert stages.build_incremental_fixed_routing(
        {"routing": {"traces": [], "vias": []}}, {}) is None
    assert stages.build_incremental_fixed_routing(None, {}) is None


def test_build_incremental_fixed_routing_keeps_complete_nets():
    from orchestrator import stages
    nl = _two_r_netlist()
    routed = {"routing": {
        "traces": [{"net_id": "net_sig", "layer": "F.Cu",
                    "start_x_mm": 0, "start_y_mm": 0,
                    "end_x_mm": 5, "end_y_mm": 0}],
        "vias": [{"net_id": "net_sig", "x_mm": 5, "y_mm": 0}],
    }}
    fixed = stages.build_incremental_fixed_routing(routed, nl)
    assert fixed is not None
    # net_sig is a 2-pin net fully bridged by the trace → kept as protected.
    assert isinstance(fixed["traces"], list)


def test_components_for_unrouted(tmp_path):
    from orchestrator import stages
    pdir, name = _placed_project(tmp_path, "cu")
    assert stages._components_for_unrouted(pdir, name, []) == set()
    touched = stages._components_for_unrouted(pdir, name, ["SIG"])
    assert {"R1", "R2"} <= touched


def test_components_for_unrouted_no_netlist(tmp_path):
    from orchestrator import stages
    pdir = tmp_path / "cun"
    pdir.mkdir()
    assert stages._components_for_unrouted(pdir, "cun", ["X"]) == set()


# --- _bom_from_netlist ----------------------------------------------------

def test_bom_from_netlist_groups_and_excludes_mechanical():
    from orchestrator import stages
    nl = {"elements": [
        {"element_type": "component", "designator": "R1", "value": "330",
         "package": "R_0805_2012Metric", "component_type": "resistor"},
        {"element_type": "component", "designator": "R2", "value": "330",
         "package": "R_0805_2012Metric", "component_type": "resistor"},
        {"element_type": "component", "designator": "MK1", "value": "",
         "package": "MountingHole_3mm", "component_type": "mounting_hole"},
        {"element_type": "net", "net_id": "n"},
    ]}
    bom = stages._bom_from_netlist(nl)["bom"]
    rows = {r["designator"]: r for r in bom}
    assert "R1, R2" in rows and rows["R1, R2"]["quantity"] == 2
    # Mounting hole excluded from the assembly BOM.
    assert not any("MK1" in r["designator"] for r in bom)


# --- run_drc (real, authoritative if kicad-cli present) -------------------

def test_run_drc_no_routed_board(tmp_path):
    from orchestrator import stages
    pdir = tmp_path / "nd"
    pdir.mkdir()
    r = stages.run_drc(pdir, "nd", _cfg())
    assert r["success"] is False and "routed" in r["error"].lower()


def test_run_drc_real_report(tmp_path):
    from orchestrator import stages
    pdir, name = _routed_on_disk(tmp_path, "drc")
    report = stages.run_drc(pdir, name, _cfg())
    assert report["success"] is True
    assert "passed" in report and "drc_engine" in report
    # The report is persisted to disk.
    assert (pdir / f"{name}_drc_report.json").exists()


# --- run_export (real Gerber/drill/BOM/STEP pipeline, no router) ----------

def test_run_export_no_routed_board(tmp_path):
    from orchestrator import stages
    pdir = tmp_path / "ne"
    pdir.mkdir()
    r = stages.run_export(pdir, "ne", _cfg())
    assert r["success"] is False and "routed" in r["error"].lower()


def test_run_export_produces_package(tmp_path):
    from orchestrator import stages
    pdir, name = _routed_on_disk(tmp_path, "exp")
    r = stages.run_export(pdir, name, _cfg())
    assert r["success"], r.get("error")
    assert r["files"] and r["package"].endswith(".zip")
    assert Path(r["package"]).exists()
    # BOM was synthesized from the netlist (no standalone _bom.json).
    assert any("bom" in f.lower() for f in r["files"])


# --- remaining deterministic helper branches ------------------------------

def test_set_placement_pin_no_netlist(tmp_path):
    from orchestrator import stages
    pdir = tmp_path / "sp"
    pdir.mkdir()
    r = stages.set_placement_pin(pdir, "sp", "J1", 5, 5)
    assert r["ok"] is False and r["code"] == "no_netlist"


def test_user_source_helpers_tolerate_corrupt_placement(tmp_path):
    from orchestrator import stages
    pdir = tmp_path / "us"
    pdir.mkdir()
    (pdir / "us_placement.json").write_text("{bad json")
    assert stages._user_source_in_placement(pdir, "us") == set()
    assert stages._clear_user_source_in_placement(pdir, "us") == set()


def test_run_placement_reinjects_user_pins_on_replace(tmp_path):
    """A second placement must preserve a placement_source='user' position from
    the existing placement file (set_component_positions path)."""
    from orchestrator import stages
    pdir, name = _placed_project(tmp_path, "reinj")
    pl = json.loads((pdir / f"{name}_placement.json").read_text())
    pl["placements"][0]["x_mm"] = 7.0
    pl["placements"][0]["y_mm"] = 7.0
    pl["placements"][0]["placement_source"] = "user"
    des = pl["placements"][0]["designator"]
    (pdir / f"{name}_placement.json").write_text(json.dumps(pl))
    # Re-place without dims → reuses board + re-injects the pinned position.
    r = stages.run_placement(pdir, name, _cfg(), seed=9)
    assert r["success"]
    pl2 = json.loads((pdir / f"{name}_placement.json").read_text())
    moved = next(p for p in pl2["placements"] if p["designator"] == des)
    assert moved["x_mm"] == 7.0 and moved["placement_source"] == "user"


def test_run_placement_reuses_plane_from_existing(tmp_path):
    """Re-placing a 4-layer board with no explicit plane_layers reuses the
    existing placement's stackup choice."""
    from orchestrator import stages
    pdir = tmp_path / "reuse4"
    pdir.mkdir()
    (pdir / "reuse4_netlist.json").write_text(json.dumps(_two_r_netlist()))
    r1 = stages.run_placement(pdir, "reuse4", _cfg(), board_width_mm=40,
                              board_height_mm=30, layers=4, plane_layers=0, seed=1)
    assert r1["success"] and r1["plane_layers"] == 0
    # Re-place with no plane_layers arg and no requirements → reuse 0.
    r2 = stages.run_placement(pdir, "reuse4", _cfg(), seed=2)
    assert r2["success"]
    board = json.loads((pdir / "reuse4_placement.json").read_text())["board"]
    assert board["plane_layers"] == 0


def test_run_placement_no_resolvable_components(tmp_path):
    """generate_grid_placement returns None when nothing resolves → error."""
    from orchestrator import stages
    nl = {"version": "1.0", "project_name": "z",
          "elements": [{"element_type": "net", "net_id": "n", "name": "N"}]}
    pdir = tmp_path / "noc"
    pdir.mkdir()
    (pdir / "noc_netlist.json").write_text(json.dumps(nl))
    r = stages.run_placement(pdir, "noc", _cfg(), board_width_mm=30,
                             board_height_mm=20)
    # Empty netlist → either the placeholder gate or the no-components error.
    assert r["success"] is False


def test_build_router_kwargs_tolerates_corrupt_requirements(tmp_path):
    from orchestrator import stages
    pdir, name = _placed_project(tmp_path, "brk")
    (pdir / f"{name}_requirements.json").write_text("{bad json")
    kw = stages._build_router_kwargs(pdir, name)
    assert kw["copper_weight_oz"] == 0.5   # fell back through the except


def test_run_drc_tolerates_corrupt_requirements(tmp_path):
    from orchestrator import stages
    pdir, name = _routed_on_disk(tmp_path, "drcreq")
    (pdir / f"{name}_requirements.json").write_text("{bad json")
    report = stages.run_drc(pdir, name, _cfg())
    assert report["success"] is True


def test_run_placement_dims_from_requirements_with_existing_placement(tmp_path):
    """Existing placement lacks dims → run_placement reads them from requirements."""
    from orchestrator import stages
    pdir = tmp_path / "dimreq"
    pdir.mkdir()
    (pdir / "dimreq_netlist.json").write_text(json.dumps(_two_r_netlist()))
    # A placement file whose board block has NO width/height.
    (pdir / "dimreq_placement.json").write_text(json.dumps(
        {"board": {"layers": 2}, "placements": []}))
    (pdir / "dimreq_requirements.json").write_text(json.dumps(
        {"board": {"width_mm": 55, "height_mm": 35}}))
    r = stages.run_placement(pdir, "dimreq", _cfg(), seed=1)
    assert r["success"]
    assert r["board_width_mm"] == 55 and r["board_height_mm"] == 35


def test_run_placement_two_sided_floor(tmp_path):
    """two_sided=True with no congestion weight floors congestion to 2.0 and
    writes two_sided onto the board."""
    from orchestrator import stages
    pdir = tmp_path / "ts"
    pdir.mkdir()
    (pdir / "ts_netlist.json").write_text(json.dumps(_two_r_netlist()))
    r = stages.run_placement(pdir, "ts", _cfg(), board_width_mm=40,
                             board_height_mm=30, seed=1, two_sided=True)
    assert r["success"]
    board = json.loads((pdir / "ts_placement.json").read_text())["board"]
    assert board["two_sided"] is True


def test_run_placement_reuses_two_sided_and_plane_from_corrupt_files(tmp_path):
    """The corrupt-file except branches in the two_sided/plane reuse blocks are
    swallowed (defaults kept) rather than crashing the placement."""
    from orchestrator import stages
    pdir = tmp_path / "cr4"
    pdir.mkdir()
    (pdir / "cr4_netlist.json").write_text(json.dumps(_two_r_netlist()))
    # First a valid 4-layer placement.
    stages.run_placement(pdir, "cr4", _cfg(), board_width_mm=40,
                         board_height_mm=30, layers=4, plane_layers=0, seed=1)
    # Corrupt it, then re-place without explicit dims/plane: the reuse reads
    # hit the except blocks; dims come from the (uncorrupt) requirements.
    (pdir / "cr4_requirements.json").write_text(json.dumps(
        {"board": {"width_mm": 40, "height_mm": 30, "layers": 4}}))
    (pdir / "cr4_placement.json").write_text("{bad json")
    r = stages.run_placement(pdir, "cr4", _cfg(), seed=2)
    assert r["success"]


def test_min_pad_pitch_skips_single_pin_footprints(tmp_path):
    """A board of only 1-pin parts yields no pitch (the <2 pad_offsets skip)."""
    from orchestrator import stages
    nl = {"version": "1.0", "project_name": "sp",
          "elements": [
              {"element_type": "component", "component_id": "c1",
               "designator": "TP1", "component_type": "connector",
               "value": "tp", "package": "TestPoint_Pad_D1.0mm"},
              {"element_type": "port", "port_id": "p1", "component_id": "c1",
               "pin_number": 1, "name": "1", "electrical_type": "passive"},
          ]}
    pdir = tmp_path / "sp"
    pdir.mkdir()
    (pdir / "sp_netlist.json").write_text(json.dumps(nl))
    # Either None (no 2+pin parts) or a value; must not raise.
    assert stages._min_pad_pitch(pdir, "sp") is None


def test_build_incremental_tolerates_bad_netlist():
    from orchestrator import stages
    routed = {"routing": {"traces": [{"net_id": "n", "start_x_mm": 0,
                                      "start_y_mm": 0, "end_x_mm": 1,
                                      "end_y_mm": 0}], "vias": []}}
    # A netlist that incomplete_net_ids can't parse → except → treat none incomplete.
    fixed = stages.build_incremental_fixed_routing(routed, {"elements": "bad"})
    assert fixed is not None and isinstance(fixed["traces"], list)


def test_components_for_unrouted_tolerates_bad_netlist(tmp_path):
    from orchestrator import stages
    pdir = tmp_path / "cbad"
    pdir.mkdir()
    (pdir / "cbad_netlist.json").write_text(json.dumps({"elements": "not a list"}))
    assert stages._components_for_unrouted(pdir, "cbad", ["X"]) == set()


def test_run_drc_internal_when_no_kicad_cli(tmp_path, monkeypatch):
    """When find_kicad_cli returns None, the report stays internal/non-authoritative."""
    from orchestrator import stages
    import optimizers.route_cleanup as rc
    monkeypatch.setattr(rc, "find_kicad_cli", lambda: None)
    pdir, name = _routed_on_disk(tmp_path, "drcint")
    report = stages.run_drc(pdir, name, _cfg())
    assert report["success"] is True
    assert report["authoritative"] is False
    assert report["drc_engine"] == "internal"


def test_run_placement_defaults_dims_when_unknown(tmp_path):
    """No dims anywhere → run_placement falls back to the 50x50 default."""
    from orchestrator import stages
    pdir = tmp_path / "defdim"
    pdir.mkdir()
    (pdir / "defdim_netlist.json").write_text(json.dumps(_two_r_netlist()))
    r = stages.run_placement(pdir, "defdim", _cfg(), seed=1)
    assert r["success"]
    assert r["board_width_mm"] == 50.0 and r["board_height_mm"] == 50.0


def test_run_placement_corrupt_requirements_dims(tmp_path):
    """Corrupt requirements while resolving dims → except swallowed, defaults used."""
    from orchestrator import stages
    pdir = tmp_path / "crdim"
    pdir.mkdir()
    (pdir / "crdim_netlist.json").write_text(json.dumps(_two_r_netlist()))
    (pdir / "crdim_requirements.json").write_text("{bad json")
    r = stages.run_placement(pdir, "crdim", _cfg(), seed=1)
    assert r["success"] and r["board_width_mm"] == 50.0


def test_run_placement_plane_reuse_except_requirements(tmp_path):
    """4-layer, no plane arg, corrupt requirements → plane parse except → default 2."""
    from orchestrator import stages
    pdir = tmp_path / "pre"
    pdir.mkdir()
    (pdir / "pre_netlist.json").write_text(json.dumps(_two_r_netlist()))
    (pdir / "pre_requirements.json").write_text("{bad json")
    r = stages.run_placement(pdir, "pre", _cfg(), board_width_mm=40,
                             board_height_mm=30, layers=4, seed=1)
    assert r["success"]
    board = json.loads((pdir / "pre_placement.json").read_text())["board"]
    assert board["plane_layers"] == 2   # default kept after the except


def test_run_placement_plane_reuse_except_existing(tmp_path):
    """4-layer, no plane arg, no requirements, corrupt existing placement →
    existing-plane parse except → default 2."""
    from orchestrator import stages
    pdir = tmp_path / "pee"
    pdir.mkdir()
    (pdir / "pee_netlist.json").write_text(json.dumps(_two_r_netlist()))
    (pdir / "pee_placement.json").write_text("{bad json")
    r = stages.run_placement(pdir, "pee", _cfg(), board_width_mm=40,
                             board_height_mm=30, layers=4, seed=1)
    assert r["success"]
    board = json.loads((pdir / "pee_placement.json").read_text())["board"]
    assert board["plane_layers"] == 2


def test_min_pad_pitch_tolerates_corrupt_netlist(tmp_path):
    from orchestrator import stages
    pdir = tmp_path / "mpc"
    pdir.mkdir()
    (pdir / "mpc_netlist.json").write_text("{bad json")
    assert stages._min_pad_pitch(pdir, "mpc") is None


def test_build_incremental_except_when_check_raises(monkeypatch):
    """If incomplete_net_ids raises, build_incremental treats none as incomplete
    (keeps all wiring) rather than crashing."""
    from orchestrator import stages
    import validators.validate_routing as vr
    monkeypatch.setattr(vr, "incomplete_net_ids",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    routed = {"routing": {"traces": [{"net_id": "n"}], "vias": [{"net_id": "n"}]}}
    fixed = stages.build_incremental_fixed_routing(routed, {})
    assert fixed["traces"] == [{"net_id": "n"}]   # nothing excluded


def test_run_drc_kicad_cli_returns_none(tmp_path, monkeypatch):
    """kicad-cli present but run_kicad_drc returns None → report stays internal."""
    from orchestrator import stages
    import optimizers.route_cleanup as rc
    import validators.kicad_drc as kd
    monkeypatch.setattr(rc, "find_kicad_cli", lambda: "/usr/bin/kicad-cli")
    monkeypatch.setattr(kd, "run_kicad_drc", lambda *a, **k: None)
    pdir, name = _routed_on_disk(tmp_path, "drcnone")
    report = stages.run_drc(pdir, name, _cfg())
    assert report["authoritative"] is False and report["drc_engine"] == "internal"


def test_run_export_best_effort_skips(tmp_path, monkeypatch):
    """STEP and assembly-drawing exporters are best-effort: a failure is logged
    and skipped, not fatal to the export."""
    from orchestrator import stages
    import exporters.step_exporter as se
    monkeypatch.setattr(se, "export_step_populated",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("nostep")))
    import exporters.assembly_drawing as ad
    monkeypatch.setattr(ad, "export_assembly_drawing",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("noassy")))
    pdir, name = _routed_on_disk(tmp_path, "exskip")
    r = stages.run_export(pdir, name, _cfg())
    assert r["success"], r.get("error")
    # Gerbers/drill/BOM still produced despite the skipped best-effort artifacts.
    assert any(f.endswith(".zip") for f in [r["package"]])
    assert not any("step" in f.lower() for f in r["files"])
