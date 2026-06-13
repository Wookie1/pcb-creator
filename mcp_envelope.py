"""Uniform response envelope for MCP tools.

Every tool returns one of three shapes so a small client model always knows
what to do next:

  ok(...)      → {"success": True,  ..., "next_step": {"tool", "args", "why"}}
  fail(...)    → {"success": False, "error": str,
                  "remediation": [{"option", "tool", "args"}, ...]}
  working(...) → {"success": True, "state": "running", "progress": {...},
                  "poll_again_in_s": int, "status_hint": str}

The `next_step` and `remediation` fields are machine-readable: a client agent
can copy `tool` + `args` directly into its next call without parsing prose.
"""

from __future__ import annotations


def next_step(tool: str, args: dict | None = None, why: str = "") -> dict:
    """Build a next_step descriptor: the concrete call the agent should make next."""
    step = {"tool": tool, "args": args or {}}
    if why:
        step["why"] = why
    return step


def option(description: str, tool: str, args: dict | None = None) -> dict:
    """Build one remediation option: a concrete alternative the agent can try."""
    return {"option": description, "tool": tool, "args": args or {}}


def ok(data: dict | None = None, step: dict | str | None = None) -> dict:
    """Success envelope. `step` is a next_step() dict, or a plain string hint."""
    out = {"success": True}
    if data:
        out.update(data)
    if step is not None:
        out["next_step"] = step
    return out


def fail(error: str, remediation: list[dict] | None = None,
         data: dict | None = None) -> dict:
    """Failure envelope. `remediation` lists concrete recovery options
    (built with option()), most-recommended first."""
    out = {"success": False, "error": error}
    if data:
        out.update(data)
    if remediation:
        out["remediation"] = remediation
    return out


def working(progress: dict | None = None, poll_again_in_s: int = 15,
            status_hint: str = "", data: dict | None = None) -> dict:
    """In-progress envelope for async operations.

    `status_hint` should state what is happening and tell the agent to keep
    polling — e.g. "Routing in progress (pass 3/20, 7 connections incomplete).
    Poll get_project_status again in ~15s. Do not run other tools or external
    CLIs while routing is active."
    """
    out = {"success": True, "state": "running"}
    if data:
        out.update(data)
    if progress:
        out["progress"] = progress
    out["poll_again_in_s"] = poll_again_in_s
    if status_hint:
        out["status_hint"] = status_hint
    return out
