"""Agent-facing UX contracts: bulk circuit entry, seeing the board, and undo.

These cover the three things an agent driving the server needs that the
per-item builder tools do not give it: one call for a circuit it already knows
(build_circuit), an image it can actually LOOK at — before routing, not just
after (get_board_image), and a way back from a re-place/re-route that landed
worse than what it replaced (revert_board).
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

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


def call_content(server, tool, args=None):
    async def _run():
        async with Client(server) as client:
            r = await client.call_tool(tool, args or {}, raise_on_error=False)
            return r.content
    return asyncio.run(_run())


def _projects_dir(tmp_path):
    return tmp_path / "projects"


LED_COMPONENTS = [
    {"designator": "R1", "component_type": "resistor", "value": "330ohm",
     "package": "0805", "functional_group": "led"},
    {"designator": "D1", "component_type": "led", "value": "red",
     "package": "0805", "functional_group": "led"},
    {"designator": "J1", "component_type": "connector", "value": "2pin",
     "package": "PinHeader_1x2", "functional_group": "power"},
]
LED_NETS = [
    {"net_name": "VCC", "pins": ["J1.1", "R1.1"], "net_class": "power"},
    {"net_name": "LED_DRIVE", "pins": ["R1.2", "D1.anode"]},
    {"net_name": "GND", "pins": ["D1.cathode", "J1.2"]},
]


def _build_led(server, name="ux_led", **overrides):
    args = {"project_name": name, "description": "led", "board_width_mm": 30,
            "board_height_mm": 20, "components": LED_COMPONENTS,
            "nets": LED_NETS}
    args.update(overrides)
    return call(server, "build_circuit", args)


def _place(server, name, width=30, height=20, seed=3, approved=False):
    r = call(server, "optimize_placement",
             {"project_name": name, "board_width_mm": width,
              "board_height_mm": height, "seed": seed, "approved": approved})
    assert r["success"], r.get("error")
    return r


# ---------------------------------------------------------------------------
# build_circuit — the whole circuit in one call
# ---------------------------------------------------------------------------

def test_build_circuit_compiles_a_whole_board_in_one_call(server, tmp_path):
    r = _build_led(server, "ux_one_call")
    assert r["success"], r.get("error")
    # It finalizes: the netlist is on disk and the next step is placement.
    assert (_projects_dir(tmp_path) / "ux_one_call" /
            "ux_one_call_netlist.json").exists()
    assert r["next_step"]["tool"] == "optimize_placement"

    listed = call(server, "list_circuit", {"project_name": "ux_one_call"})
    assert len(listed["components"]) == 3
    assert not listed["unconnected_pins"]


def test_build_circuit_reports_each_failure_and_keeps_what_worked(server):
    r = _build_led(
        server, "ux_partial",
        components=LED_COMPONENTS + [
            {"designator": "U9", "component_type": "ic", "value": "???",
             "package": "NOT_A_REAL_PACKAGE_XYZ"},
            {"designator": "R9", "component_type": "resistor", "value": "1k",
             "package": "0805", "colour": "red"},          # unknown key
        ],
        nets=LED_NETS + [{"net_name": "NOWHERE", "pins": ["R1.99", "D1.1"]}],
    )
    assert r["success"] is False
    assert r["added"] == 3 and r["connected"] == 3

    failures = {json.dumps(f["item"], sort_keys=True): f for f in r["failed"]}
    assert len(failures) == 3
    assert any("colour" in f["error"] for f in r["failed"])
    # Every failure is individually actionable, and the good items survived —
    # the agent fixes 3 things, not 7.
    listed = call(server, "list_circuit", {"project_name": "ux_partial"})
    assert {c["designator"] for c in listed["components"]} == {"R1", "D1", "J1"}


def test_build_circuit_refuses_to_clobber_an_existing_project(server):
    assert _build_led(server, "ux_exists")["success"]
    again = _build_led(server, "ux_exists")
    assert again["success"] is False
    assert "exists" in again["error"].lower()
    assert _build_led(server, "ux_exists", overwrite=True)["success"]


# ---------------------------------------------------------------------------
# get_board_image — a real image, and one before routing
# ---------------------------------------------------------------------------

def _image_blocks(server, name, **args):
    blocks = call_content(server, "get_board_image",
                          {"project_name": name, **args})
    images = [b for b in blocks if b.type == "image"]
    meta = json.loads([b for b in blocks if b.type == "text"][0].text)
    return images, meta


def test_board_image_renders_the_placement_before_any_routing(server):
    _build_led(server, "ux_img")
    _place(server, "ux_img")

    images, meta = _image_blocks(server, "ux_img", width=512)
    assert len(images) == 1 and images[0].mimeType == "image/png"
    assert meta["stage"] == "placement"
    assert meta["next_step"]["tool"] == "route_board"


def test_board_image_width_is_capped(server):
    _build_led(server, "ux_img_cap")
    _place(server, "ux_img_cap")
    _, meta = _image_blocks(server, "ux_img_cap", width=99999)
    assert meta["width"] == 2048


def test_board_image_without_placement_points_at_placement(server, tmp_path):
    _build_led(server, "ux_img_none")
    r = call(server, "get_board_image", {"project_name": "ux_img_none"})
    assert r["success"] is False
    assert any(o["tool"] == "optimize_placement" for o in r["remediation"])


# ---------------------------------------------------------------------------
# revert_board — one step back from a worse placement/route
# ---------------------------------------------------------------------------

def _write_routed(tmp_path, name, completion):
    pdir = _projects_dir(tmp_path) / name
    placement = json.loads((pdir / f"{name}_placement.json").read_text())
    (pdir / f"{name}_routed.json").write_text(json.dumps({
        "board": placement["board"],
        "placements": placement["placements"],
        "routing": {"traces": [], "vias": [], "copper_fills": [],
                    "statistics": {"completion_pct": completion},
                    "unrouted_nets": []},
    }))


def test_revert_board_restores_the_previous_placement_and_route(server, tmp_path):
    name = "ux_revert"
    _build_led(server, name)
    _place(server, name, width=30, height=20)
    _write_routed(tmp_path, name, 100.0)
    pdir = _projects_dir(tmp_path) / name
    (pdir / f"{name}_drc_report.json").write_text(json.dumps({"passed": True}))

    # Re-place on a bigger board (snapshots the 30x20 + 100% pair), then land a
    # worse route on it. approved=True stands in for the user agreeing to the
    # enlargement the gate insists on.
    _place(server, name, width=40, height=30, seed=7, approved=True)
    _write_routed(tmp_path, name, 62.5)

    r = call(server, "revert_board", {"project_name": name})
    assert r["success"], r.get("error")
    assert r["completion_pct"] == 100.0
    assert r["next_step"]["tool"] == "run_drc"

    placement = json.loads((pdir / f"{name}_placement.json").read_text())
    assert placement["board"]["width_mm"] == 30
    # The DRC report described the board we just threw away.
    assert not (pdir / f"{name}_drc_report.json").exists()


def test_revert_board_does_not_resurrect_a_route_the_snapshot_never_had(
        server, tmp_path):
    name = "ux_revert_none"
    _build_led(server, name)
    _place(server, name)                    # snapshot: no placement, no route
    _place(server, name, seed=9)            # snapshot: placement only
    _write_routed(tmp_path, name, 100.0)    # a route the snapshot predates

    r = call(server, "revert_board", {"project_name": name})
    assert r["success"], r.get("error")
    assert r["completion_pct"] is None
    assert r["next_step"]["tool"] == "route_board"
    assert not (_projects_dir(tmp_path) / name / f"{name}_routed.json").exists()


def test_revert_board_with_nothing_to_revert(server):
    _build_led(server, "ux_revert_empty")
    r = call(server, "revert_board", {"project_name": "ux_revert_empty"})
    assert r["success"] is False and "Nothing to revert" in r["error"]

    missing = call(server, "revert_board", {"project_name": "ux_no_such"})
    assert missing["success"] is False and "not found" in missing["error"]


def test_revert_board_waits_for_a_running_route(server, tmp_path):
    name = "ux_revert_busy"
    _build_led(server, name)
    _place(server, name)
    _place(server, name, seed=5)

    import mcp_server
    with mcp_server._ROUTE_LOCK:
        mcp_server._ROUTE_JOBS[name] = {"state": "running", "result": None,
                                        "error": None, "started_at": 0.0,
                                        "progress": None}
    try:
        r = call(server, "revert_board", {"project_name": name})
    finally:
        with mcp_server._ROUTE_LOCK:
            mcp_server._ROUTE_JOBS.pop(name, None)
    # Restoring under a live router would just be overwritten by it.
    assert r["state"] == "running"
    assert "routing_state" in r["status_hint"]
