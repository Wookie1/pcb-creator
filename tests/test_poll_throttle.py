"""Tests for the server-side poll throttle (_throttle_poll).

Agents routinely ignore the advisory poll_again_in_s, so while a route/design
job is running the server HOLDS a too-soon poll until the interval elapses
(capped). A caller that already waited pays nothing; an idle project is never
throttled. time.sleep is patched so these tests don't actually wait."""

import time

import mcp_server


def _running_route(elapsed_s: float = 0.0) -> dict:
    return {"state": "running", "started_at": time.monotonic() - elapsed_s}


def test_idle_project_never_throttled(monkeypatch):
    slept = []
    monkeypatch.setattr(time, "sleep", lambda s: slept.append(s))
    mcp_server._LAST_POLL.clear()
    mcp_server._throttle_poll("p", None, None)                 # no job
    mcp_server._throttle_poll("p", {"state": "complete"}, None)  # finished job
    assert slept == []


def test_first_poll_not_delayed(monkeypatch):
    slept = []
    monkeypatch.setattr(time, "sleep", lambda s: slept.append(s))
    mcp_server._LAST_POLL.clear()
    mcp_server._throttle_poll("p", _running_route(), None)
    assert slept == []                       # nothing to space against yet
    assert "p" in mcp_server._LAST_POLL      # but the poll time is recorded


def test_immediate_second_poll_is_held(monkeypatch):
    slept = []
    monkeypatch.setattr(time, "sleep", lambda s: slept.append(s))
    monkeypatch.setattr(mcp_server, "_MAX_POLL_BLOCK_S", 25.0)
    mcp_server._LAST_POLL.clear()
    job = _running_route()
    mcp_server._throttle_poll("p", job, None)   # records
    mcp_server._throttle_poll("p", job, None)   # too soon → blocks
    assert len(slept) == 1
    assert 0 < slept[0] <= 25.0                  # ~the 15s starting interval


def test_block_is_capped(monkeypatch):
    slept = []
    monkeypatch.setattr(time, "sleep", lambda s: slept.append(s))
    monkeypatch.setattr(mcp_server, "_MAX_POLL_BLOCK_S", 5.0)
    mcp_server._LAST_POLL.clear()
    # elapsed > 180s → desired interval is 60s, but the block must cap at 5s so
    # a single call never approaches a client's per-tool timeout.
    job = _running_route(elapsed_s=200)
    mcp_server._throttle_poll("p", job, None)
    mcp_server._throttle_poll("p", job, None)
    assert len(slept) == 1
    assert slept[0] <= 5.0


def test_waited_long_enough_not_delayed(monkeypatch):
    slept = []
    monkeypatch.setattr(time, "sleep", lambda s: slept.append(s))
    mcp_server._LAST_POLL.clear()
    job = _running_route()
    mcp_server._throttle_poll("p", job, None)
    # Simulate the agent having already waited well past the interval.
    mcp_server._LAST_POLL["p"] = time.monotonic() - 100
    mcp_server._throttle_poll("p", job, None)
    assert slept == []


def test_design_job_also_throttled(monkeypatch):
    slept = []
    monkeypatch.setattr(time, "sleep", lambda s: slept.append(s))
    monkeypatch.setattr(mcp_server, "_MAX_POLL_BLOCK_S", 25.0)
    mcp_server._LAST_POLL.clear()
    djob = _running_route()
    mcp_server._throttle_poll("p", None, djob)
    mcp_server._throttle_poll("p", None, djob)
    assert len(slept) == 1 and slept[0] > 0
