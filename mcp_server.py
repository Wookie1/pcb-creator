#!/usr/bin/env python3
"""MCP server for PCB-Creator.

Exposes the AI-driven PCB design pipeline as MCP tools so any AI agent
can design PCBs programmatically. Runs headless with vision-based approval.

Usage:
    python mcp_server.py                  # stdio transport (default)
    pcb-creator-mcp                       # via installed entry point

Configuration (environment variables):
    PCB_PROJECTS_DIR    — Persistent projects directory (default: ~/.pcb-creator/projects/)
    PCB_LLM_API_KEY         — LLM API key
    PCB_LLM_API_BASE        — LLM API base URL
    PCB_GENERATE_MODEL  — Model for generation steps
    PCB_VISION_MODEL    — Model for vision-based board review
"""

from __future__ import annotations

import base64
import json
import logging
import os
import re
import sys
import threading
from pathlib import Path

from fastmcp import FastMCP

# Ensure the repo root is on sys.path so orchestrator/ imports work
_repo_root = Path(__file__).resolve().parent
if str(_repo_root) not in sys.path:  # pragma: no cover - import-time sys.path guard (repo already on path under pytest)
    sys.path.insert(0, str(_repo_root))

from orchestrator.config import OrchestratorConfig
from optimizers.routed_board import routing_stats
from mcp_envelope import ok, fail, working, next_step, option

logger = logging.getLogger(__name__)

mcp = FastMCP(
    "pcb-creator",
    instructions=(
        "PCB design tools. Call get_workflow_guide() FIRST to see the exact tool "
        "order for the three workflows: (a) build a circuit from scratch — "
        "build_circuit in one call, or create_circuit/add_component/connect_pins "
        "step by step, (b) import an existing KiCad "
        "netlist with import_kicad_netlist, or (c) one-shot autonomous design_pcb. "
        "Every tool response includes 'next_step' (the call to make next) and, on "
        "failure, 'remediation' (concrete recovery options). Long operations "
        "(design_pcb, route_board) return immediately and run in the background — "
        "poll get_project_status until done; its 'status_hint' tells you what is "
        "happening. Never fall back to external CAD tools; every fix can be made "
        "through these tools."
    ),
)

# In-memory routing job registry (project_name -> job dict).  route_board runs
# routing on a background thread so the MCP call returns immediately; clients
# poll get_project_status for routing_state.  Reconciled with the on-disk
# _routed.json so state survives even if this registry is empty (e.g. restart).
_ROUTE_JOBS: dict[str, dict] = {}
_ROUTE_LOCK = threading.Lock()

# In-memory design job registry (project_name -> job dict).  design_pcb runs the
# full pipeline (requirements → schematic → BOM → placement → routing → DRC →
# outputs) on a background thread so the MCP call returns immediately and never
# hits the client timeout.  Clients poll get_project_status and read
# 'design_state' (running → complete | failed).  Single-flight: a second
# design_pcb for a project already running returns the in-progress job instead of
# launching a duplicate pipeline.  Reconciled with on-disk STATUS.json so a
# respawned server can still report design state.
_DESIGN_JOBS: dict[str, dict] = {}
_DESIGN_LOCK = threading.Lock()

# Poll throttle: last get_project_status response time per project (monotonic).
# While a route/design job is running, a too-soon poll is HELD until the
# recommended interval elapses (capped at _MAX_POLL_BLOCK_S so a single call
# never approaches a client's per-tool timeout). Agents routinely ignore the
# advisory poll_again_in_s; this enforces it server-side without speeding the
# job up or starving well-behaved pollers (who pay nothing).
_LAST_POLL: dict[str, float] = {}
_POLL_LOCK = threading.Lock()
_MAX_POLL_BLOCK_S = float(os.environ.get("PCB_MAX_POLL_BLOCK_S", "25"))

# Footprint lookup globals — initialised once by _init_lookup() in main().
# Per-project custom indexes are built lazily in _get_project_custom_index().
_KICAD_INDEX: "Any | None" = None   # KiCadLibraryIndex for the system KiCad library
_CACHE: "Any | None" = None          # ComponentCache
_CUSTOM_INDICES: dict[str, "Any"] = {}   # project_name → KiCadLibraryIndex
_CUSTOM_LOCK = threading.Lock()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _init_lookup() -> None:
    """Initialise footprint resolution at MCP server startup.

    Builds the system KiCad library index (if PCB_KICAD_LIBRARY_PATH is set)
    and the component cache, then calls configure_lookup() so every subsequent
    pad-map / placement call has real footprint data.  Without this, the KiCad
    tier is silently skipped even when the env var is configured.
    """
    global _KICAD_INDEX, _CACHE, _LOOKUP_CONFIGURED
    from orchestrator.cache import ComponentCache
    from optimizers.pad_geometry import configure_lookup

    config = OrchestratorConfig.from_env(base_dir=_repo_root)
    _CACHE = ComponentCache(config.component_cache_path)

    if config.kicad_library_path:
        from exporters.kicad_mod_parser import KiCadLibraryIndex
        _KICAD_INDEX = KiCadLibraryIndex(config.kicad_library_path)
        logger.info("Footprint lookup: KiCad library at %s",
                    config.kicad_library_path)
    else:
        # No system KiCad library found (env unset AND auto-detect missed every
        # known location). Standard footprints (R_0805, C_0805, …) will NOT
        # resolve, so placement/routing/export will block with "unresolved
        # footprints" on essentially every board. Make this loud — it is an
        # environment misconfiguration, not a board problem.
        logger.warning(
            "Footprint lookup: NO KiCad library found (PCB_KICAD_LIBRARY_PATH "
            "unset and no system library at the known paths). Standard "
            "footprints will not resolve — set PCB_KICAD_LIBRARY_PATH to the "
            "footprints root (the dir containing the *.pretty folders).")

    configure_lookup(kicad_index=_KICAD_INDEX, cache=_CACHE, custom_index=None)
    # Mark configured so the lazy _ensure_lookup_configured() doesn't build a
    # second, separate cache — there is one cache, stored in _CACHE.
    _LOOKUP_CONFIGURED = True


def _get_project_custom_index(project_name: str) -> "Any | None":
    """Return (building lazily) a KiCadLibraryIndex for the project's custom
    footprints directory, or None if it does not exist.

    The directory is ``<project_dir>/custom-footprints.pretty/``.  Agents write
    .kicad_mod files there via ``register_custom_footprint``; the index is
    invalidated on every write so new files are visible immediately.
    """
    custom_dir = _project_dir(project_name) / "custom-footprints.pretty"
    if not custom_dir.is_dir():
        return None
    with _CUSTOM_LOCK:
        if project_name not in _CUSTOM_INDICES:
            from exporters.kicad_mod_parser import KiCadLibraryIndex
            _CUSTOM_INDICES[project_name] = KiCadLibraryIndex(custom_dir)
        return _CUSTOM_INDICES[project_name]


def _activate_project_lookup(project_name: str) -> None:
    """Update the module-level footprint lookup to include this project's
    custom footprints as tier 0.

    Call this at the start of any tool that performs footprint resolution
    (optimize_placement, export_outputs, design_pcb worker thread) so that
    agent-registered footprints are visible to the placement engine.
    """
    from optimizers.pad_geometry import configure_lookup
    # Ensure the base lookup (KiCad index + component cache) exists first, so we
    # never reconfigure with a None cache and silently wipe it — that broke the
    # provide_footprint → verify_footprints loop when _init_lookup() (main only)
    # had not run, e.g. on the directly-callable / fastmcp-Client surface.
    _ensure_lookup_configured()
    custom = _get_project_custom_index(project_name)
    configure_lookup(kicad_index=_KICAD_INDEX, cache=_CACHE, custom_index=custom)

def _get_projects_dir() -> Path:
    """Resolve the persistent projects directory."""
    env = os.environ.get("PCB_PROJECTS_DIR")
    if env:
        p = Path(env).expanduser()
    else:
        p = Path.home() / ".pcb-creator" / "projects"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _get_config() -> OrchestratorConfig:
    """Build config from env vars with MCP-appropriate defaults."""
    config = OrchestratorConfig.from_env(base_dir=_repo_root)
    config.agent_mode = True
    config.skip_qa = True  # Calling agent reviews via get_project_status/get_board_image
    config.max_rework_attempts = 3  # Limit rework loops in MCP mode (agent can retry)
    config.llm_timeout = 300  # 5 min per LLM call (fail fast, agent can retry)
    # Point projects_dir to persistent location
    config.projects_dir = str(_get_projects_dir())
    return config


_LOOKUP_CONFIGURED = False
_LOOKUP_LOCK = threading.Lock()


def _ensure_lookup_configured() -> None:
    """Install the tiered footprint lookup (KiCad library + component cache).

    The CLI and Gradio entry points call ``configure_lookup`` at startup, but the
    MCP server is a separate process — without this, the KiCad-library tier and
    the component cache are disabled and verbose KiCad footprint names fall back
    to placeholders.  Idempotent and thread-safe.
    """
    global _LOOKUP_CONFIGURED, _CACHE, _KICAD_INDEX
    if _LOOKUP_CONFIGURED:
        return
    with _LOOKUP_LOCK:
        if _LOOKUP_CONFIGURED:  # pragma: no cover - double-checked-lock race (2nd thread configured first)
            return
        from optimizers.pad_geometry import configure_lookup
        from orchestrator.cache import ComponentCache

        config = _get_config()
        cache = ComponentCache(config.component_cache_path)

        kicad_index = None
        if config.kicad_library_path:
            try:
                from exporters.kicad_mod_parser import KiCadLibraryIndex
                kicad_index = KiCadLibraryIndex(config.kicad_library_path)
            except Exception:  # pragma: no cover - defensive: KiCad library index build failure
                kicad_index = None

        # Store as the module-level source of truth so _activate_project_lookup
        # can re-apply the same cache/index (plus a custom tier) without ever
        # passing a None cache to configure_lookup.
        _CACHE = cache
        _KICAD_INDEX = kicad_index
        configure_lookup(kicad_index=_KICAD_INDEX, cache=_CACHE)
        _LOOKUP_CONFIGURED = True


def _slugify(text: str) -> str:
    """Convert description to a filesystem-safe project name."""
    slug = re.sub(r"[^a-z0-9]+", "_", text.lower().strip())
    slug = slug.strip("_")[:60]
    return slug or "pcb_project"


# Project names become directory names, so anything with a path separator or a
# ".." component is a traversal attempt. Tool args originate from the LLM agent,
# which is steerable by injected content — validate at the single choke point
# every path flows through rather than trusting each of ~30 callers.
_PROJECT_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")


def _validate_project_name(project_name: str) -> None:
    """Reject project names that could escape the projects directory.

    Raises ValueError (which fastmcp surfaces as a tool error) on a bad name.
    """
    if not _PROJECT_NAME_RE.match(project_name or ""):
        raise ValueError(
            f"Invalid project_name {project_name!r}: use lowercase letters, "
            "digits, '_' and '-' only (must start with a letter or digit)."
        )


def _safe_name(filename: str) -> str:
    """Strip any directory component from a user-supplied filename.

    ``os.path.basename`` neutralizes ``../`` and absolute paths; we additionally
    reject empty / dot names so an attachment can only land inside its project
    directory.
    """
    name = os.path.basename(filename or "")
    if not name or name in (".", "..") or name.startswith("."):
        raise ValueError(f"Unsafe attachment filename: {filename!r}")
    return name


def _reject_bad_project_name(project_name: str) -> dict | None:
    """Graceful ``fail`` envelope for a traversal-unsafe name, else None.

    Project-creation tools call this first so a bad name yields a structured
    error (with a safe suggestion) instead of the hard ValueError that
    ``_project_dir`` raises as its last-line backstop. Same regex as the
    backstop, so anything this accepts is safe to hand to ``_project_dir``.
    """
    if _PROJECT_NAME_RE.match(project_name or ""):
        return None
    return fail(
        f"Invalid project_name {project_name!r}: use lowercase letters, digits, "
        "'_' and '-' only (must start with a letter or digit).",
        remediation=[option(
            f"Retry with the safe name '{_slugify(project_name)}'",
            "create_circuit", {"project_name": _slugify(project_name)},
        )],
    )


def _project_dir(project_name: str) -> Path:
    """Get the project directory path (validated against traversal)."""
    _validate_project_name(project_name)
    return _get_projects_dir() / project_name


def _read_project_json(project_name: str, suffix: str) -> dict | None:
    """Read a project JSON file by suffix (e.g. '_drc_report.json')."""
    path = _project_dir(project_name) / f"{project_name}{suffix}"
    if path.exists():
        return json.loads(path.read_text())
    return None


def _electrical_gate(project_name: str, next_tool: str) -> dict | None:
    """Refuse expensive work on a circuit with known electrical errors.

    Placement and routing cost minutes; routing a board whose MISO is shorted
    to a USB data line only produces a wrong board more slowly. Returns a fail
    envelope to short-circuit on, or None when it is safe to proceed.

    Only *errors* block. Warnings and anything indeterminate (see
    validators/circuit_report.py) pass through, so an unmodeled IC never
    stops the pipeline.
    """
    netlist = _read_project_json(project_name, "_netlist.json")
    if netlist is None:
        return None  # nothing to check yet; the caller has its own guard

    sys.path.insert(0, str(_repo_root / "validators"))
    from validators.circuit_report import build_report
    from validators.engineering_constants import parse_supply_voltage

    reqs = _read_project_json(project_name, "_requirements.json") or {}
    raw = (reqs.get("power") or {}).get("voltage")
    volts = parse_supply_voltage(raw) if raw else None

    try:
        report = build_report(
            netlist.get("elements") or [], supply_voltage=volts,
            supply_source="requirements_file",
        )
    except Exception:  # pragma: no cover - a checker bug must not block the pipeline
        logger.exception("check_circuit gate failed; continuing")
        return None

    errors = [i for i in report["issues"] if i["severity"] == "error"]
    if not errors:
        return None

    return fail(
        f"{len(errors)} electrical error(s) in the circuit — fix these before "
        f"{next_tool}, which takes minutes and would only produce a wrong "
        f"board more slowly. " + errors[0]["detail"],
        remediation=[
            option("See every finding with the fix for each", "check_circuit",
                   {"project_name": project_name}),
            option(errors[0]["detail"], "connect_pins",
                   {"project_name": project_name}),
        ],
        data={"electrical_errors": [e["detail"] for e in errors]},
    )


# Placement and routing overwrite in place, and the autorouter is
# nondeterministic — a re-run can land WORSE than what it replaced. Both files
# are snapshotted together so a revert always restores a coherent pair.
_SNAPSHOT_SUFFIXES = ("_placement.json", "_routed.json")


def _snapshot_board(project_name: str) -> None:
    """Save the current placement+routed pair into the single revert slot.

    ponytail: one slot, overwritten by every mutating run — no history stack.
    """
    pdir = _project_dir(project_name)
    for suffix in _SNAPSHOT_SUFFIXES:
        src = pdir / f"{project_name}{suffix}"
        dst = pdir / f"{project_name}{suffix}.prev"
        if src.exists():
            dst.write_bytes(src.read_bytes())
        elif dst.exists():
            # The file does not exist NOW, so the slot must not claim it did —
            # otherwise a revert resurrects a board from two runs ago.
            dst.unlink()


def _poll_interval(elapsed_s: float | None) -> int:
    """Adaptive poll cadence for a background job, in seconds.

    Routing/design run in the background and polling does NOT speed them up — a
    route can take 15+ minutes. Start at 15s (a short route may finish quickly),
    then back off so an over-eager agent isn't told to hammer get_project_status
    every fraction of a second on a multi-minute job."""
    e = elapsed_s or 0
    if e < 30:
        return 15
    if e < 180:
        return 30
    return 60


def _throttle_poll(project_name: str, rjob: dict | None,
                   djob: dict | None) -> None:
    """Enforce the poll cadence server-side while a job runs.

    If a route/design job is running and the caller polls sooner than the
    adaptive interval, sleep the remainder (capped at _MAX_POLL_BLOCK_S, so a
    single call never nears a client's per-tool timeout) before returning. A
    caller that already waited the recommended interval is not delayed at all.
    No-op when nothing is running. Caller should re-read the job registry after
    this returns so the response carries fresh progress."""
    import time as _time
    job = None
    if rjob is not None and rjob.get("state") == "running":
        job = rjob
    elif djob is not None and djob.get("state") == "running":
        job = djob
    if job is None:
        return
    started = job.get("started_at")
    elapsed = (_time.monotonic() - started) if started is not None else None
    desired = _poll_interval(elapsed)
    with _POLL_LOCK:
        last = _LAST_POLL.get(project_name)
        wait = 0.0
        if last is not None:
            wait = min(max(0.0, desired - (_time.monotonic() - last)),
                       _MAX_POLL_BLOCK_S)
    if wait > 0:
        _time.sleep(wait)
    with _POLL_LOCK:
        _LAST_POLL[project_name] = _time.monotonic()


def _route_failure_next_step(project_name: str, err: str) -> dict:
    """Escalation ladder for a failed route. Add routing CAPACITY first, and
    change physically-constrained things last and only with the USER's approval.

    Rungs, chosen from the current placement so the ladder advances as the agent
    acts:
      1. 2-layer  -> 4-layer, plane_layers=2   (cost/stackup change: ASK USER)
      2. plane_layers=2 -> 1                    (reallocate an inner plane: free)
      3. plane_layers=1 -> 0                    (free the last inner plane: free)
      4. plane_layers=0 -> larger board         (size often fixed: ASK USER)

    next_step gets requires_user_approval=True on the rungs (1 and 4) that change
    something the user likely constrained — board layer count or dimensions — so
    an agent prepares the exact call but does not run it without confirmation.
    """
    board = (_read_project_json(project_name, "_placement.json") or {}).get("board", {})
    layers = board.get("layers", 2)
    plane_layers = board.get("plane_layers")
    w, h = board.get("width_mm"), board.get("height_mm")

    if layers < 4:
        # 2-layer -> 4-layer raises board cost and changes the stackup: gate it.
        step = next_step(
            "optimize_placement",
            {"project_name": project_name, "layers": 4, "plane_layers": 2},
            f"Routing failed ({err}) The fix is more routing capacity, but the "
            "next step promotes this 2-layer board to a 4-layer stackup, which "
            "raises board cost and changes manufacturing. ASK THE USER to approve "
            "going to 4 layers first; only with their approval run this "
            "optimize_placement (layers=4, plane_layers=2 — GND + power planes "
            "free both outer layers for signal) and route again. Do NOT promote "
            "to 4 layers on your own — the tool enforces this and will refuse "
            "without approved=True, which you may pass ONLY after the user "
            "agrees.",
        )
        step["requires_user_approval"] = True
        return step
    if plane_layers is None or plane_layers >= 2:
        return next_step(
            "optimize_placement",
            {"project_name": project_name, "plane_layers": 1},
            f"Routing failed ({err}) Reallocate an inner plane to signal: "
            "plane_layers=1 (In1 stays a GND plane, In2 becomes a 3rd signal "
            "layer — same 4-layer board, no cost change), then route again. Keep "
            "the layer count and board size.",
        )
    if plane_layers == 1:
        return next_step(
            "optimize_placement",
            {"project_name": project_name, "plane_layers": 0},
            f"Routing failed ({err}) Free the last inner plane for signal "
            "(plane_layers=0 → all inner layers route; still a 4-layer board), "
            "then route again. Keep the board size — enlarging it is the next and "
            "final step only if this still fails.",
        )
    # plane_layers == 0: every inner layer is already signal. Only board area is
    # left, and that is often fixed by an enclosure/mating part -> ask the user.
    args = {"project_name": project_name}
    if w and h:
        args["board_width_mm"] = round(w * 1.15, 1)
        args["board_height_mm"] = round(h * 1.15, 1)
    step = next_step(
        "optimize_placement", args,
        f"Routing failed ({err}) All inner layers are already signal "
        "(plane_layers=0), so the only lever left is board area. Board size is "
        "often fixed by an enclosure or mating part, so ASK THE USER before "
        "enlarging it; only with their approval re-run optimize_placement ~15% "
        "larger and route again. The tool enforces this and will refuse "
        "without approved=True, which you may pass ONLY after the user agrees.",
    )
    step["requires_user_approval"] = True
    return step


def _requires_approval(project_name: str, layers: int | None,
                       plane_layers: int | None,
                       board_width_mm: float | None,
                       board_height_mm: float | None) -> str | None:
    """Reason a placement change needs explicit user approval, or None.

    Hard, in-code version of the route-failure ladder's two "ASK THE USER"
    rungs: promoting a placed 2-layer board to 4 layers (cost/stackup) and
    enlarging a placed board (enclosure/mating fit). The FIRST placement is
    never gated — those are up-front design choices, not mid-flow escalations
    — and neither are plane_layers reallocations on an already-4-layer board,
    same-size re-placements, or shrinks.
    """
    board = (_read_project_json(project_name, "_placement.json") or {}).get("board", {})
    if not board:
        return None
    cur_layers = board.get("layers", 2)
    # plane_layers on a 2-layer board implies 4-layer promotion (see the
    # optimize_placement docstring), so it gates exactly like layers=4.
    if cur_layers == 2 and (layers == 4 or
                            (layers is None and plane_layers is not None)):
        return ("promoting this placed 2-layer board to 4 layers changes the "
                "stackup and raises fab cost")
    cur_w, cur_h = board.get("width_mm"), board.get("height_mm")
    grew_w = board_width_mm and cur_w and board_width_mm > cur_w + 1e-6
    grew_h = board_height_mm and cur_h and board_height_mm > cur_h + 1e-6
    if grew_w or grew_h:
        return (f"enlarging the board beyond its current {cur_w}x{cur_h} mm "
                "may break enclosure or mating-part fit")
    return None


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

@mcp.tool()
def design_pcb(  # pragma: no cover - spawns the background LLM design pipeline (needs a configured LLM + Freerouting); covered end-to-end only in the manual flow
    description: str,
    project_name: str | None = None,
    requirements_json: dict | None = None,
    settings: dict | None = None,
    attachments: list[dict] | None = None,
) -> dict:
    """Design a complete PCB autonomously from a description (async, one-shot).

    Runs the full LLM pipeline (requirements → schematic → BOM → placement →
    routing → DRC → outputs) on a background thread and returns immediately.
    Poll get_project_status until 'design_state' is 'complete' or 'failed'.
    Calling again while running returns the in-progress job (no duplicates).

    Prefer requirements_json (full schema from get_requirements_schema) over a
    plain description — it skips LLM translation. Shape:

        {"project_name": ..., "description": ...,
         "power": {"voltage": "5V", "source": "external_dc"},
         "board": {"width_mm": 30, "height_mm": 20, "layers": 2},
         "components": [{"ref": "R1", "type": "resistor", "value": "330ohm",
                         "package": "0805"}, ...],
         "connections": [{"net_name": "VCC", "net_class": "power",
                          "pins": ["J1.1", "R1.1"]}, ...]}

    settings overrides: {"model", "max_rework_attempts",
    "skip_qa"}. attachments: list of {"filename", "content_base64", "type",
    "purpose", "used_by_steps"} (e.g. a "board_outline" DXF for step 3).
    """
    import time as _time

    if not project_name:
        project_name = _slugify(description)

    bad = _reject_bad_project_name(project_name)
    if bad:
        return bad

    # Single-flight: don't launch a duplicate pipeline for a project that is
    # already running. A second call returns the in-progress job to poll.
    with _DESIGN_LOCK:
        current = _DESIGN_JOBS.get(project_name)
        if current and current["state"] == "running":
            return working(
                data={"project_name": project_name},
                poll_again_in_s=15,
                status_hint=(
                    "Design already in progress for this project. Check "
                    "get_project_status for 'design_state' in ~15s, then wait "
                    "the 'poll_again_in_s' it returns between checks; do not "
                    "poll faster and do not launch another design_pcb."
                ),
            )
        _DESIGN_JOBS[project_name] = {
            "state": "running", "result": None, "error": None,
            "started_at": _time.monotonic(), "progress": None,
        }

    def _on_progress(p: dict) -> None:
        with _DESIGN_LOCK:
            job = _DESIGN_JOBS.get(project_name)
            if job and job["state"] == "running":
                job["progress"] = p

    def _worker() -> None:
        # Activate project-local custom footprints (tier 0) so the pipeline
        # finds any agent-registered .kicad_mod files during placement/export.
        _activate_project_lookup(project_name)
        try:
            result = _design_pcb_sync(
                description, project_name, requirements_json, settings,
                attachments, progress_cb=_on_progress,
            )
            state = "complete" if result.get("success") else "failed"
            err = None if result.get("success") else (
                "; ".join(result.get("errors", [])) or "pipeline did not complete"
            )
            with _DESIGN_LOCK:
                started = _DESIGN_JOBS.get(project_name, {}).get("started_at")
                _DESIGN_JOBS[project_name] = {
                    "state": state, "result": result, "error": err,
                    "started_at": started, "progress": None,
                    "elapsed_s": round(_time.monotonic() - started, 1) if started else None,
                }
        except Exception as exc:  # noqa: BLE001 — surface any failure to the poller
            with _DESIGN_LOCK:
                started = _DESIGN_JOBS.get(project_name, {}).get("started_at")
                _DESIGN_JOBS[project_name] = {
                    "state": "failed", "result": None, "error": str(exc),
                    "started_at": started, "progress": None,
                    "elapsed_s": round(_time.monotonic() - started, 1) if started else None,
                }

    threading.Thread(target=_worker, daemon=True).start()

    return working(
        data={
            "project_name": project_name,
            "next_step": next_step(
                "get_project_status", {"project_name": project_name},
                "Check get_project_status until 'design_state' is 'complete' or "
                "'failed', waiting the 'poll_again_in_s' it returns between "
                "checks; 'design_progress' shows the live step, 'design_result' "
                "the final output.",
            ),
        },
        poll_again_in_s=15,
        status_hint=(
            "Full design pipeline started in the background (this can take "
            "many minutes). Check get_project_status in ~15s, then wait the "
            "'poll_again_in_s' it returns between checks — do NOT poll faster, "
            "it does not speed the pipeline up. Do not run other tools or "
            "external CLIs for this project while it runs."
        ),
    )


def _design_pcb_sync(  # pragma: no cover - full LLM pipeline worker (requirements->schematic->...->export); needs a configured LLM + Freerouting
    description: str,
    project_name: str | None = None,
    requirements_json: dict | None = None,
    settings: dict | None = None,
    attachments: list[dict] | None = None,
    progress_cb=None,
) -> dict:
    """Synchronous design pipeline worker (run on a background thread by design_pcb).

    Design a PCB from a circuit description or structured requirements.

    Runs the full pipeline: requirements → schematic → BOM → placement →
    routing → DRC → output generation. Uses vision-based autonomous review.

    Two input modes:
    1. **Structured (preferred for agents):** Pass requirements_json directly —
       skips LLM translation entirely. Call get_requirements_schema() first to
       get the expected format.
    2. **Natural language:** Pass a plain-text description — translated to
       structured requirements via LLM automatically.

    Args:
        description: Circuit description in plain English, or a short summary
            when using requirements_json. Used for project name generation if
            project_name is omitted.

            Example: "A green LED controlled by a pushbutton, powered by 3.3V"
        project_name: Optional project slug. Auto-generated from description if omitted.
        requirements_json: Structured requirements dict matching the schema from
            get_requirements_schema(). When provided, the LLM translation step is
            skipped entirely — faster, cheaper, and more deterministic. Must include
            at minimum: components (list) and connections (list).
        settings: Optional config overrides: {"model": "...",
            "max_rework_attempts": 5, "skip_qa": false}. QA reviews are skipped by
            default in MCP mode; set skip_qa to false to re-enable them.
        attachments: Optional list of file attachments. Each dict has:
            - "filename": Name for the file (e.g., "board_outline.dxf")
            - "content_base64": Base64-encoded file content
            - "type": Attachment type — "board_outline", "sketch", "photo", "datasheet", "other"
            - "purpose": Description of what the file is for
            - "used_by_steps": List of step numbers that use this file (e.g., [3] for layout)

            For DXF board outlines: set type to "board_outline" and used_by_steps
            to [3]. The pipeline will automatically extract the outline polygon and
            board dimensions from the DXF file — you do not need to specify
            width_mm/height_mm. If providing structured JSON requirements, set
            board.outline_type to "dxf".

    Returns:
        Dict with success status, project name, routing stats, DRC summary,
        and list of output file paths.
    """
    import tempfile

    config = _get_config()

    # Apply optional settings overrides
    if settings:
        if "model" in settings:
            config.generate_model = settings["model"]
            config.review_model = settings["model"]
        if "max_rework_attempts" in settings:
            config.max_rework_attempts = int(settings["max_rework_attempts"])
        if "skip_qa" in settings:
            config.skip_qa = bool(settings["skip_qa"])

    # Generate project name if not provided
    if not project_name:
        project_name = _slugify(description)

    # Resolve requirements: structured JSON (fast path) or NL translation
    from orchestrator.gather.schema import validate_requirements, auto_fix_duplicate_pins

    if requirements_json is not None:
        # Fast path: agent provided structured requirements directly
        requirements = requirements_json
        errors = validate_requirements(requirements)
        if errors:
            requirements, fix_warnings = auto_fix_duplicate_pins(requirements)
            for w in fix_warnings:
                logger.info(f"  MCP auto-fix: {w}")
            remaining = validate_requirements(requirements)
            if remaining:
                return {
                    "success": False,
                    "errors": [f"Requirements validation failed: {e}" for e in remaining],
                }
    else:
        # Try parsing description as JSON; fall back to LLM translation
        try:
            requirements = json.loads(description)
        except (json.JSONDecodeError, TypeError):
            from orchestrator.gather.conversation import RequirementsGatherer
            from orchestrator.llm.litellm_client import LiteLLMClient
            from orchestrator.prompts.builder import PromptBuilder
            _llm = LiteLLMClient(
                config.generate_model,
                api_base=config.api_base,
                api_key=config.api_key,
                extra_body=config.llm_extra_body,
                timeout=config.llm_timeout,
            )
            _gatherer = RequirementsGatherer(_llm, PromptBuilder(config.base_dir))

            # Translate with validation + rework loop
            requirements = _gatherer.translate(description)
            if requirements is not None:
                for _retry in range(3):
                    errors = validate_requirements(requirements)
                    if not errors:
                        break
                    logger.info(f"  MCP translate: {len(errors)} validation errors, retrying...")
                    requirements = _gatherer.translate(
                        description,
                        feedback="Fix these validation errors:\n" + "\n".join(
                            f"- {e}" for e in errors
                        ),
                        previous_json=json.dumps(requirements, indent=2),
                    )
                    if requirements is None:
                        break

                # Last resort: auto-fix duplicate pins
                if requirements is not None:
                    errors = validate_requirements(requirements)
                    if errors:
                        requirements, fix_warnings = auto_fix_duplicate_pins(requirements)
                        for w in fix_warnings:
                            logger.info(f"  MCP auto-fix: {w}")
                        remaining = validate_requirements(requirements)
                        if remaining:
                            logger.info(f"  MCP auto-fix: {len(remaining)} errors remain")

            if requirements is None:
                return {
                    "success": False,
                    "errors": ["Failed to translate natural language to requirements JSON"],
                }

    _validate_project_name(project_name)
    projects_dir = _get_projects_dir()
    project_dir = projects_dir / project_name
    project_dir.mkdir(parents=True, exist_ok=True)

    # Handle file attachments
    if attachments:
        att_metadata = []
        for att in attachments:
            filename = att.get("filename", "attachment")
            content_b64 = att.get("content_base64", "")
            att_type = att.get("type", "other")
            purpose = att.get("purpose", "")
            used_by = att.get("used_by_steps", [3])

            # Write file to project directory (basename-only: never escape it)
            filename = _safe_name(filename)
            file_path = project_dir / filename
            file_path.write_bytes(base64.b64decode(content_b64))

            att_metadata.append({
                "filename": filename,
                "type": att_type,
                "purpose": purpose,
                "used_by_steps": used_by,
            })

        # Merge attachment metadata into requirements
        existing_atts = requirements.get("attachments", [])
        existing_atts.extend(att_metadata)
        requirements["attachments"] = existing_atts

    req_path = project_dir / f"{project_name}_requirements_input.json"
    req_path.write_text(json.dumps(requirements, indent=2))

    # Run the streaming pipeline, collecting events
    from orchestrator.runner import run_workflow_streaming

    steps_completed = []
    errors = []
    last_event = None

    try:
        for event in run_workflow_streaming(
            req_path, project_name, config, progress_callback=progress_cb,
        ):
            ev = event.get("event", "")
            if ev == "step_done":
                steps_completed.append({
                    "step": event.get("step"),
                    "name": event.get("name"),
                    "success": event.get("success", False),
                })
                if progress_cb is not None:
                    progress_cb({
                        "phase": "pipeline",
                        "step": event.get("step"),
                        "name": event.get("name"),
                        "steps_done": len(steps_completed),
                    })
            elif ev == "error":
                errors.append(event.get("message", "Unknown error"))
            elif ev == "approval_needed":
                # In MCP mode with agent_mode=True, this means vision review
                # escalated. We can't do human approval in MCP, so continue.
                pass
            last_event = event
    except Exception as exc:
        errors.append(f"Pipeline crashed: {exc}")
        try:
            from orchestrator.project import ProjectManager as _PM
            _proj = _PM(project_name, projects_dir)
            _proj.update_status(-1, "ERROR")
        except Exception:
            pass

    success = last_event and last_event.get("event") == "complete" and last_event.get("success", False)

    # Annotate steps_completed with validator errors from STATUS.json
    status_data: dict = {}
    try:
        status_path = project_dir / "STATUS.json"
        if status_path.exists():
            status_data = json.loads(status_path.read_text())
    except Exception:
        pass
    step_status = status_data.get("steps", {})
    for step_entry in steps_completed:
        skey = str(step_entry["step"])
        if skey in step_status:
            v_errs = step_status[skey].get("validator_errors")
            v_warns = step_status[skey].get("validator_warnings")
            if v_errs:
                step_entry["validator_errors"] = v_errs
            if v_warns:
                step_entry["validator_warnings"] = v_warns

    # Gather output info
    result = {
        "success": success,
        "project_name": project_name,
        "project_dir": str(project_dir),
        "steps_completed": steps_completed,
        "errors": errors,
    }

    # Add routing stats if available. Stats live under routing.statistics; the
    # old top-level lookup always missed them, so the agent saw completion_pct=0
    # on a 93.6%-routed board and couldn't tell which nets were open.
    routed = _read_project_json(project_name, "_routed.json")
    if routed:
        routing = routed.get("routing", {})
        stats = routing_stats(routed)
        unrouted = routing.get("unrouted_nets")
        if not isinstance(unrouted, (list, tuple)):
            unrouted = []
        result["routing_stats"] = {
            "completion_pct": stats.get("completion_pct", 0),
            "total_nets": stats.get("total_nets", 0),
            "routed_nets": stats.get("routed_nets", 0),
            "via_count": stats.get("via_count", 0),
            "trace_length_mm": stats.get("total_trace_length_mm", 0),
            # The exact open nets — so the agent can target recovery
            # (route_board keep_existing / re-place these) instead of guessing.
            "unrouted_nets": list(unrouted),
        }

    # Add DRC summary. authoritative tells the agent whether this verdict came
    # from kicad-cli (trustworthy) or the internal heuristic fallback (which
    # misses THT-pad shorts/mask bridges — a clean result there is NOT a
    # manufacturability guarantee).
    drc = _read_project_json(project_name, "_drc_report.json")
    if drc:
        result["drc_summary"] = {
            "passed": drc.get("passed", False),
            "authoritative": drc.get("authoritative", False),
            "drc_engine": drc.get("drc_engine", "internal"),
            "summary": drc.get("summary", ""),
            "errors": drc.get("statistics", {}).get("errors", 0),
            "warnings": drc.get("statistics", {}).get("warnings", 0),
        }

    # List output files
    output_dir = project_dir / "output"
    if output_dir.exists():
        result["output_files"] = [
            str(f.relative_to(project_dir)) for f in sorted(output_dir.iterdir())
            if f.is_file()
        ]

    return result


@mcp.tool()
def get_requirements_schema() -> dict:
    """Get the JSON schema for structured PCB requirements.

    Returns the full JSON Schema (Draft-7) that describes the format expected
    by design_pcb's requirements_json parameter. Call this once to understand
    the structure, then pass conforming dicts to design_pcb directly — no LLM
    translation needed.

    Key top-level fields: project_name, description, power, components,
    connections, board, manufacturing, placement_hints, calculations.
    """
    from orchestrator.gather.schema import REQUIREMENTS_SCHEMA
    return REQUIREMENTS_SCHEMA


@mcp.tool()
def get_workflow_guide() -> dict:
    """Get the step-by-step tool order for each PCB design workflow.

    Call this first if you are unsure which tool to use. Returns three
    workflows; pick ONE and follow its steps in order. Each step lists the
    tool, an args template, what to wait for, and what to do on failure.
    """
    poll_routing = {
        "then_poll": "get_project_status",
        "wait_for": "routing_state == 'complete' (poll every ~15s; "
                    "'routing_progress' and 'status_hint' show live progress)",
        "on_failure": "Read 'routing_error'; re-run optimize_placement with a "
                      "larger board, then route_board again.",
        "if_incomplete": (
            "If the route comes back valid but <100% (or DRC shows disconnected "
            "nets), do NOT just enlarge the board — that rarely helps. In order: "
            "(1) route_board(keep_existing=True) to INCREMENTALLY finish the "
            "residual — the autorouter is nondeterministic, so finishing the few "
            "remaining nets while protecting the routed majority usually closes "
            "them; (2) on a 4-layer board, re-run optimize_placement with "
            "plane_layers=1 (frees a 3rd signal layer) or plane_layers=0 (all "
            "inner layers signal) to add routing CAPACITY, then route again — "
            "this is the lever for dense / fine-pitch-connector boards; "
            "(3) only then consider a larger board. A handful of fine-pitch "
            "fanout nets may finish best by hand."),
    }
    return {
        "workflows": {
            "build_from_scratch": {
                "when": "You are designing a new circuit and can describe its "
                        "components and connections.",
                "shortcut": {
                    "tool": "build_circuit",
                    "note": "If you already know the whole circuit, build_circuit "
                            "does steps 1-4 in ONE call (draft + every component "
                            "+ every net + compile) and reports per-item "
                            "failures. Steps 1-4 below remain the way to edit an "
                            "existing draft.",
                    "args_template": {
                        "project_name": "my_board", "description": "...",
                        "board_width_mm": 50, "board_height_mm": 40,
                        "components": [{"designator": "U1",
                                        "component_type": "ic",
                                        "value": "NE555", "package": "DIP-8",
                                        "pinout": "1:GND 2:TRIG 3:OUT 4:RESET "
                                                  "5:CTRL 6:THRES 7:DISCH 8:VCC"}],
                        "nets": [{"net_name": "GND", "pins": ["U1.1", "C1.2"]}],
                    },
                },
                "steps": [
                    {"order": 1, "tool": "create_circuit",
                     "args_template": {"project_name": "my_board",
                                       "description": "...",
                                       "board_width_mm": 50, "board_height_mm": 40}},
                    {"order": 2, "tool": "add_component",
                     "args_template": {"project_name": "my_board",
                                       "designator": "U1", "component_type": "ic",
                                       "value": "NE555", "package": "DIP-8",
                                       "functional_group": "timer"},
                     "note": "Repeat per component. The response lists the pins "
                             "you can connect. Pass functional_group (a short "
                             "block label like 'power'/'mcu'/'usb', grouped by "
                             "role) so related parts place together and route "
                             "easier — optional but recommended; same label for "
                             "every part in a block."},
                    {"order": 3, "tool": "connect_pins",
                     "args_template": {"project_name": "my_board",
                                       "net_name": "VCC",
                                       "pins": ["U1.8", "C1.1"]},
                     "note": "Repeat per net. Unknown pins return the valid pin "
                             "list."},
                    {"order": 4, "tool": "finalize_circuit",
                     "args_template": {"project_name": "my_board"},
                     "on_failure": "Fix the reported issues with "
                                   "connect_pins/remove_component, then re-run."},
                    {"order": 4.5, "tool": "check_circuit",
                     "args_template": {"project_name": "my_board",
                                       "supply_voltage": "5V"},
                     "note": "Checks the circuit ELECTRICALLY before you spend "
                             "time laying it out: solves the DC operating point "
                             "and reports over-current LEDs, over-dissipating "
                             "resistors, parts fed the wrong rail voltage, "
                             "shorted pins, and isolated sections. Read "
                             "'verdict' — 'no_issues_found' means no check "
                             "failed, NOT that the circuit works, and anything "
                             "in 'not_checked' was not verified at all.",
                     "on_failure": "Each error carries a concrete fix; apply it "
                                   "with connect_pins/add_component, then "
                                   "re-run."},
                    {"order": 5, "tool": "place_component",
                     "args_template": {"project_name": "my_board",
                                       "designator": "J1", "x_mm": 2.5,
                                       "y_mm": 20, "rotation_deg": 90},
                     "note": "OPTIONAL — only for components that must sit at "
                             "exact coordinates (edge connectors, mounting "
                             "holes). Validated immediately; pinned parts are "
                             "never moved."},
                    {"order": 6, "tool": "optimize_placement",
                     "args_template": {"project_name": "my_board",
                                       "board_width_mm": 50,
                                       "board_height_mm": 40},
                     "on_failure": "If 'violations' lists pinned components, "
                                   "adjust them with place_component / "
                                   "unplace_component; otherwise enlarge the "
                                   "board and re-run."},
                    {"order": 7, "tool": "get_board_image",
                     "args_template": {"project_name": "my_board"},
                     "note": "LOOK at the placement before routing it — routing "
                             "is the slow step, and a connector facing the wrong "
                             "way or a crammed corner is obvious in the image "
                             "and cheap to fix now with place_component."},
                    {"order": 8, "tool": "route_board",
                     "args_template": {"project_name": "my_board"}, **poll_routing},
                    {"order": 9, "tool": "run_drc",
                     "args_template": {"project_name": "my_board"},
                     "on_failure": "Review violations; re-place on a larger "
                                   "board or re-route, then re-run. If the "
                                   "re-route lands worse, revert_board restores "
                                   "the previous board."},
                    {"order": 10, "tool": "export_outputs",
                     "args_template": {"project_name": "my_board"}},
                    {"order": 11, "tool": "get_fab_quote",
                     "args_template": {"project_name": "my_board", "quantity": 5},
                     "optional": True,
                     "note": "Fab cost estimate + BOM part availability. Record "
                             "missing/wrong part ids with set_part_number."},
                ],
            },
            "import_kicad": {
                "when": "You already have a KiCad schematic/netlist file.",
                "steps": [
                    {"order": 1, "tool": "import_kicad_netlist",
                     "args_template": {"project_name": "my_board",
                                       "file_path": "/abs/path/board.net"}},
                    {"order": 2, "tool": "verify_footprints",
                     "args_template": {"project_name": "my_board"},
                     "on_failure": "Call provide_footprint for each unresolved "
                                   "package, then re-run."},
                    {"order": 3, "tool": "optimize_placement",
                     "args_template": {"project_name": "my_board",
                                       "board_width_mm": 50,
                                       "board_height_mm": 40}},
                    {"order": 4, "tool": "route_board",
                     "args_template": {"project_name": "my_board"}, **poll_routing},
                    {"order": 5, "tool": "run_drc",
                     "args_template": {"project_name": "my_board"}},
                    {"order": 6, "tool": "export_outputs",
                     "args_template": {"project_name": "my_board"}},
                    {"order": 7, "tool": "get_fab_quote",
                     "args_template": {"project_name": "my_board", "quantity": 5},
                     "optional": True,
                     "note": "Fab cost estimate + BOM part availability. Record "
                             "missing/wrong part ids with set_part_number."},
                ],
            },
            "autonomous": {
                "when": "You want pcb-creator's own LLM pipeline to do "
                        "everything from a text description (requires a "
                        "configured LLM).",
                "steps": [
                    {"order": 1, "tool": "design_pcb",
                     "args_template": {"description": "A 555 LED blinker at 1Hz "
                                                      "powered by 9V"}},
                    {"order": 2, "tool": "get_project_status",
                     "wait_for": "design_state == 'complete' (poll every ~20s)",
                     "on_failure": "Read 'design_error' and "
                                   "'step_validator_errors'; fix the description "
                                   "or switch to the build_from_scratch flow."},
                ],
            },
        },
        "rules": [
            "Pick one workflow and follow it in order; every response's "
            "'next_step' tells you the next call.",
            "While routing or designing, keep polling get_project_status — "
            "'status_hint' always reports forward progress.",
            "Never use external CAD tools or CLIs; every fix is possible "
            "through these tools.",
            "Look at the board with get_board_image — after placement (before "
            "paying for a route) and after routing.",
            "A placement or route that came back worse than the last one is "
            "undone with revert_board (one step of history).",
        ],
        "reference": {
            "routing_capacity": (
                "Route finished <100%, or DRC reports disconnected nets. In "
                "order: (1) route_board(keep_existing=True) — the autorouter is "
                "nondeterministic, so finishing the residual nets while "
                "protecting the routed majority usually closes them; (2) add "
                "routing CAPACITY via the stackup (see 'stackup'); (3) only then "
                "a larger board — enlarging rarely helps a dense / fine-pitch "
                "board, and its size is often fixed by the enclosure, so ask the "
                "user. A handful of fine-pitch fanout nets may finish best by "
                "hand. Advanced: PCB_ESCAPE_FANOUT=true pre-generates dog-bone "
                "escapes for single-row fine-pitch parts as protected wiring "
                "before routing."),
            "stackup": (
                "optimize_placement(layers=, plane_layers=). 2 layers is the "
                "default and cheapest; going to 4 changes cost and stackup, so "
                "ASK THE USER first (the tool enforces approved=True). On 4 "
                "layers, plane_layers trades power integrity for signal "
                "capacity: 2 = GND + power planes (best integrity), 1 = GND "
                "plane and a 3rd SIGNAL layer (power routed as traces), 0 = all "
                "inner layers signal. Escalate 2 → 1 → 0 for a dense board with "
                "many signals, e.g. a fine-pitch connector with lots of GPIO."),
            "export_refusal": (
                "export_outputs refuses anything that cannot be built, with no "
                "override. Open/disconnected nets → route_board("
                "keep_existing=True). DRC errors → read 'reroute_cleanable': "
                "True means every error is clearance/short/mask geometry that a "
                "keep_existing re-route rips up and clears on its own (do not "
                "hand-edit or re-pour), False means at least one is structural "
                "(annular ring, hole spacing, edge clearance, trace width) and "
                "must be fixed at its source — placement, via or board rules — "
                "first. DRC not certifiable → install/locate kicad-cli "
                "(PCB_KICAD_CLI). To inspect an imperfect board instead, use "
                "export_kicad or get_board_image."),
        },
    }


@mcp.tool()
def list_projects() -> list[dict]:
    """List all PCB design projects with their current status.

    Returns:
        List of dicts with project_name, status info, and last modified time.
    """
    projects_dir = _get_projects_dir()
    results = []

    for entry in sorted(projects_dir.iterdir()):
        if not entry.is_dir():
            continue

        project_name = entry.name
        info: dict = {"project_name": project_name}

        # Read STATUS.json
        status_path = entry / "STATUS.json"
        if status_path.exists():
            try:
                status = json.loads(status_path.read_text())
                info["steps"] = status.get("steps", {})
                info["last_updated"] = status_path.stat().st_mtime
            except (json.JSONDecodeError, OSError):
                info["steps"] = {}
        else:
            info["steps"] = {}

        # Check for key outputs
        info["has_routed"] = (entry / f"{project_name}_routed.json").exists()
        info["has_drc"] = (entry / f"{project_name}_drc_report.json").exists()
        info["has_outputs"] = (entry / "output").exists()

        results.append(info)

    return results


@mcp.tool()
def get_project_status(project_name: str) -> dict:
    """Get detailed status for a specific PCB project.

    Args:
        project_name: The project slug/name.

    Returns:
        Dict with step status, routing statistics, and DRC pass/fail.
    """
    pdir = _project_dir(project_name)

    # Check in-memory design/route jobs BEFORE checking disk.
    # A background design_pcb thread may not have created the project
    # directory yet (or crashed before mkdir), and callers need to see
    # running/failed state instead of a misleading "not found".
    with _DESIGN_LOCK:
        djob = dict(_DESIGN_JOBS.get(project_name)) if project_name in _DESIGN_JOBS else None
    with _ROUTE_LOCK:
        rjob = dict(_ROUTE_JOBS.get(project_name)) if project_name in _ROUTE_JOBS else None

    # Enforce the poll cadence: if a job is running and this poll is too soon,
    # hold it (capped) so the agent physically can't hammer the registry. Then
    # re-read the registry so the response reflects progress made during the wait.
    _throttle_poll(project_name, rjob, djob)
    with _DESIGN_LOCK:
        djob = dict(_DESIGN_JOBS.get(project_name)) if project_name in _DESIGN_JOBS else None
    with _ROUTE_LOCK:
        rjob = dict(_ROUTE_JOBS.get(project_name)) if project_name in _ROUTE_JOBS else None

    if not pdir.exists():
        # No directory on disk yet — a background design_pcb thread may not have
        # created it (or crashed before mkdir). Report in-memory job state instead
        # of a misleading "not found".
        if djob or rjob:
            import time as _time
            result: dict = {"project_name": project_name}
            if djob is not None:
                result["design_state"] = djob["state"]
                dstarted = djob.get("started_at")
                if djob["state"] == "running" and dstarted is not None:
                    result["design_elapsed_s"] = round(_time.monotonic() - dstarted, 1)
                elif djob.get("elapsed_s") is not None:
                    result["design_elapsed_s"] = djob["elapsed_s"]
                if djob["state"] == "running" and djob.get("progress") is not None:
                    result["design_progress"] = djob["progress"]
                if djob["state"] == "complete" and djob.get("result"):
                    result["design_result"] = djob["result"]
                elif djob["state"] == "failed":
                    result["design_error"] = djob.get("error")
            if rjob is not None:
                result["routing_state"] = rjob["state"]
                if rjob["state"] == "failed":
                    result["routing_error"] = rjob.get("error")
            return result
        return fail(
            f"Project '{project_name}' not found.",
            remediation=[option("List existing projects to find the right name",
                                "list_projects", {})],
        )

    result: dict = {"project_name": project_name}

    # STATUS.json — include per-step validator errors for agent diagnostics
    status_path = pdir / "STATUS.json"
    if status_path.exists():
        try:
            status_data = json.loads(status_path.read_text())
            result["status"] = status_data
            # Surface a flat list of all step errors for easy scanning
            step_errors: dict[str, list[str]] = {}
            step_warnings: dict[str, list[str]] = {}
            for skey, sinfo in status_data.get("steps", {}).items():
                if sinfo.get("validator_errors"):
                    step_errors[skey] = sinfo["validator_errors"]
                if sinfo.get("validator_warnings"):
                    step_warnings[skey] = sinfo["validator_warnings"]
            if step_errors:
                result["step_validator_errors"] = step_errors
            if step_warnings:
                result["step_validator_warnings"] = step_warnings
        except json.JSONDecodeError:
            result["status"] = {}

    # Routing stats. The statistics live under routing.statistics and the
    # unrouted-net LIST under routing.unrouted_nets (statistics.unrouted_nets is
    # a count). Reading the top level returned all-zeros from disk — so any
    # status query that reconciled from disk (e.g. after a server restart, or any
    # project not in the in-memory job registry) reported a fully-routed board as
    # 0% / 0 nets. Tolerate a flat shape too, just in case.
    routed = _read_project_json(project_name, "_routed.json")
    if routed:
        routing = routed.get("routing", routed)
        stats = routing_stats(routed)
        result["routing_stats"] = {
            "completion_pct": stats.get("completion_pct", 0),
            "total_nets": stats.get("total_nets", 0),
            "routed_nets": stats.get("routed_nets", 0),
            "via_count": stats.get("via_count", 0),
            "trace_length_mm": stats.get("total_trace_length_mm", 0),
            "unrouted_nets": routing.get("unrouted_nets",
                                         stats.get("unrouted_nets", [])),
        }

    # routing_state: in-memory job wins; else infer from on-disk artifact.
    import time as _time
    if rjob is not None:
        result["routing_state"] = rjob["state"]
        # Elapsed time: live during run, final after completion/failure
        started = rjob.get("started_at")
        if rjob["state"] == "running" and started is not None:
            result["routing_elapsed_s"] = round(_time.monotonic() - started, 1)
        elif rjob.get("elapsed_s") is not None:
            result["routing_elapsed_s"] = rjob["elapsed_s"]
        # Live NCR iteration progress (only meaningful while running)
        if rjob["state"] == "running" and rjob.get("progress") is not None:
            result["routing_progress"] = rjob["progress"]
        if rjob["state"] == "complete" and rjob.get("result"):
            result["routing_result"] = rjob["result"]
        elif rjob["state"] == "failed":
            result["routing_error"] = rjob.get("error")
    else:
        result["routing_state"] = "complete" if routed else "none"

    # Design job state (already fetched above for early-return).
    if djob is not None:
        result["design_state"] = djob["state"]
        dstarted = djob.get("started_at")
        if djob["state"] == "running" and dstarted is not None:
            result["design_elapsed_s"] = round(_time.monotonic() - dstarted, 1)
        elif djob.get("elapsed_s") is not None:
            result["design_elapsed_s"] = djob["elapsed_s"]
        if djob["state"] == "running" and djob.get("progress") is not None:
            result["design_progress"] = djob["progress"]
        if djob["state"] == "complete" and djob.get("result"):
            result["design_result"] = djob["result"]
        elif djob["state"] == "failed":
            result["design_error"] = djob.get("error")
    else:
        # No in-memory job (e.g. server restarted). Infer from disk.
        st = result.get("status") or {}
        overall = str(
            st.get("overall_status") or st.get("overall") or st.get("state") or ""
        ).upper()
        if overall in ("COMPLETE", "DONE", "SUCCESS", "OK"):
            result["design_state"] = "complete"
        elif overall in ("ERROR", "FAILED", "FAIL"):
            result["design_state"] = "failed"
        elif (pdir / "output").exists() and any((pdir / "output").iterdir()):
            result["design_state"] = "complete"
        elif status_path.exists():
            result["design_state"] = "unknown"
        else:
            result["design_state"] = "none"

    # DRC summary
    drc = _read_project_json(project_name, "_drc_report.json")
    if drc:
        result["drc"] = {
            "passed": drc.get("passed", False),
            "summary": drc.get("summary", ""),
            "errors": drc.get("statistics", {}).get("errors", 0),
            "warnings": drc.get("statistics", {}).get("warnings", 0),
        }

    # Output files
    output_dir = pdir / "output"
    if output_dir.exists():
        result["output_files"] = [
            str(f.relative_to(pdir)) for f in sorted(output_dir.iterdir())
            if f.is_file()
        ]

    # Anti-abandonment: while a background job runs, always tell the agent
    # what is happening and to keep polling.
    if result.get("routing_state") == "running":
        prog = result.get("routing_progress") or {}
        if prog.get("pass_num") is not None:
            detail = (f"pass {prog['pass_num']}"
                      + (f", {prog['incomplete_connections']} connections "
                         f"incomplete" if prog.get("incomplete_connections")
                         is not None else ""))
        elif prog.get("iteration") is not None:
            detail = (f"iteration {prog['iteration']}"
                      + (f"/{prog['max_iterations']}"
                         if prog.get("max_iterations") else ""))
        else:
            detail = f"{result.get('routing_elapsed_s', 0)}s elapsed"
        wait = _poll_interval(result.get("routing_elapsed_s"))
        result["poll_again_in_s"] = wait
        result["status_hint"] = (
            f"Routing in progress ({detail}). It runs in the background and a "
            f"route can take many minutes — check get_project_status again in "
            f"~{wait}s. Do NOT poll faster (it does not speed routing up); if "
            "you do, the call will simply block until the interval elapses. Do "
            "not run other tools or external CLIs for this project."
        )
    elif result.get("design_state") == "running":
        prog = result.get("design_progress") or {}
        detail = (f"step {prog.get('step')}: {prog.get('name')}"
                  if prog.get("name")
                  else f"{result.get('design_elapsed_s', 0)}s elapsed")
        wait = _poll_interval(result.get("design_elapsed_s"))
        result["poll_again_in_s"] = wait
        result["status_hint"] = (
            f"Design pipeline in progress ({detail}). It runs in the background "
            f"and can take many minutes — check get_project_status again in "
            f"~{wait}s. Do NOT poll faster (it does not speed the pipeline up); "
            "if you do, the call will simply block until the interval elapses. "
            "Do not run other tools or external CLIs for this project."
        )
    elif result.get("routing_state") == "failed":
        # A failed route must hand the poller a concrete recovery, not a raw
        # error to parse. Escalate routing CAPACITY first (plane_layers=1 → 0),
        # and leave board enlargement for last (size is usually fixed by
        # mechanics). The rung is read from the current placement, so it advances
        # as the agent follows each step.
        err = result.get("routing_error") or "routing failed."
        result["status_hint"] = f"Routing failed: {err}"
        result["next_step"] = _route_failure_next_step(project_name, err)
    elif result.get("routing_state") == "complete" and "next_step" not in result:
        # A finished route must point the poller at the next stage instead of
        # leaving it to guess. Progressive: finish an incomplete route, then DRC,
        # then export, then done.
        rr = result.get("routing_result") or {}
        stats = result.get("routing_stats") or {}
        comp = rr.get("completion_pct")
        if comp is None:
            comp = stats.get("completion_pct")
        unrouted = rr.get("unrouted_nets") or stats.get("unrouted_nets") or []
        if comp is not None and comp < 100:
            result["next_step"] = next_step(
                "route_board",
                {"project_name": project_name, "keep_existing": True,
                 "effort": "best"},
                f"Route finished at {comp}% with {len(unrouted)} net(s) still "
                "unrouted. Finish them with keep_existing=True (protects the "
                "routed majority); if it won't close, add routing capacity — on a "
                "2-layer board ASK THE USER before going to 4 layers, then "
                "plane_layers 2 → 1 → 0, and enlarge the board only as a last "
                "resort (also with user approval, since its size may be fixed).",
            )
        elif result.get("output_files"):
            result["next_step"] = next_step(
                "get_board_image", {"project_name": project_name},
                "Routed, DRC'd, and exported — the manufacturing package is "
                "ready. Optionally fetch a final board image to review.",
            )
        elif result.get("drc", {}).get("passed"):
            result["next_step"] = next_step(
                "export_outputs", {"project_name": project_name},
                "Routing complete and DRC passed — generate the manufacturing "
                "outputs.",
            )
        else:
            result["next_step"] = next_step(
                "run_drc", {"project_name": project_name},
                "Routing complete — run design-rule checks before export.",
            )

    return result


@mcp.tool()
def get_drc_report(project_name: str, verbose: bool = False) -> dict:
    """Get the DRC (Design Rule Check) report for a project.

    By default returns the agent-friendly summary: severity-ranked top
    violations, per-rule counts, and a remediation hint per failing rule.
    Pass verbose=True for the full report (every check, every violation).

    Args:
        project_name: The project slug/name.
        verbose: Return the complete raw report instead of the summary.
    """
    report = _read_project_json(project_name, "_drc_report.json")
    if report is None:
        return fail(
            f"No DRC report found for project '{project_name}'.",
            remediation=[option("Run DRC first", "run_drc",
                                {"project_name": project_name})],
        )
    if verbose:
        return report
    from validators.drc_report import summarize_drc
    return summarize_drc(report)


@mcp.tool()
def export_kicad(project_name: str) -> dict:
    """Export a completed PCB project to KiCad format (.kicad_pcb).

    Zones are poured on export via KiCad's pcbnew (so the file opens filled and
    headless DRC sees connected copper); on 4-layer boards a follow-up pcbnew
    pass ties any isolated GND pour island to the inner GND plane with a
    clearance-checked through-via. Both are best-effort: without a
    pcbnew-capable python the board is still written, just unpoured.

    Args:
        project_name: The project slug/name.

    Returns:
        Dict with success status and path to the generated KiCad file.
    """
    routed = _read_project_json(project_name, "_routed.json")
    netlist = _read_project_json(project_name, "_netlist.json")

    if not routed:
        return fail(
            f"No routed board found for project '{project_name}'.",
            remediation=[option("Route the board first", "route_board",
                                {"project_name": project_name})],
        )
    if not netlist:
        return fail(f"No netlist found for project '{project_name}'.")

    from exporters.kicad_exporter import export_kicad_pcb

    pdir = _project_dir(project_name)
    output_path = pdir / "output" / f"{project_name}.kicad_pcb"
    output_path.parent.mkdir(parents=True, exist_ok=True)

    # Same footprint geometry placement/routing used (see run_drc).
    _activate_project_lookup(project_name)

    try:
        result_path = export_kicad_pcb(routed, netlist, output_path)
        return ok({"kicad_path": str(result_path)})
    except Exception as e:
        return fail(str(e))


@mcp.tool()
def get_board_image(project_name: str, width: int = 1024):
    """Look at the board as a PNG image — the routed board, or the PLACEMENT
    if it is not routed yet.

    Returns a real image you can see, so review placement BEFORE paying for a
    route (components off-board, connectors facing inward, a crammed corner)
    and review traces/fills after. 'stage' in the accompanying text says which
    one you got.

    width is the pixel width (default 1024, capped at 2048). Bigger is not
    better — a wider image costs proportionally more to look at and is
    downsampled anyway; raise it only to inspect fine-pitch detail.
    """
    width = max(256, min(int(width), 2048))
    board = _read_project_json(project_name, "_routed.json")
    stage = "routed"
    if not board:
        board = _read_project_json(project_name, "_placement.json")
        stage = "placement"
    if not board:
        return fail(
            f"No routed board or placement found for project '{project_name}'.",
            remediation=[option("Place the components first", "optimize_placement",
                                {"project_name": project_name,
                                 "board_width_mm": "<width>",
                                 "board_height_mm": "<height>"})],
        )

    netlist = _read_project_json(project_name, "_netlist.json")
    bom = _read_project_json(project_name, "_bom.json")

    # Render the pads the board actually has (see run_drc).
    _activate_project_lookup(project_name)

    from orchestrator.vision_review import render_board_png
    from fastmcp.utilities.types import Image

    try:
        png_bytes = render_board_png(board, netlist, bom, width=width)
    except Exception as e:
        return fail(f"Failed to render board image: {e}")

    if stage == "placement":
        step = next_step("route_board", {"project_name": project_name},
                         "This is the unrouted placement — route it once the "
                         "layout looks right.")
    else:
        step = next_step("run_drc", {"project_name": project_name},
                         "Routed board — check design rules next.")
    # The Image content block is what the agent can actually SEE; the dict
    # rides alongside as text. Returning base64 in the dict instead would cost
    # ~47k tokens at width=2048 and still be unreadable.
    return [Image(data=png_bytes, format="png"),
            ok({"project_name": project_name, "stage": stage, "width": width,
                "size_bytes": len(png_bytes)}, step)]


# ---------------------------------------------------------------------------
# KiCad import
# ---------------------------------------------------------------------------

@mcp.tool()
def import_kicad_netlist(
    project_name: str,
    file_path: str,
    description: str = "",
    overwrite: bool = False,
) -> dict:
    """Import a KiCad schematic netlist into pcb-creator to continue a mid-stream project.

    Converts a KiCad netlist export (.net) or schematic (.kicad_sch) into
    pcb-creator's internal circuit_schema format and saves it as the project
    netlist.  After this call succeeds the project is ready for placement and
    routing — call design_pcb with skip_to="routing" or use get_project_status
    to confirm, then export_kicad / get_board_image when done.

    Accepted file types
    -------------------
    .net        KiCad netlist export.  Export from KiCad Schematic Editor:
                File → Export → Netlist → KiCad format.  This is the most
                reliable input.
    .kicad_sch  KiCad schematic file.  A sibling .net file with the same stem
                must exist in the same directory (pcb-creator uses it for
                connectivity; the schematic is used only for component metadata).

    Args:
        project_name: Slug for the project (lowercase, underscores).
                      A new project directory is created; an existing project
                      is refused unless overwrite=True.
        file_path:    Absolute path to the .net or .kicad_sch file.
        description:  Optional human-readable description written into the netlist.
        overwrite:    Default False. True replaces an existing project of this
                      name with a clean import — its placement and routing are
                      discarded, so re-place and re-route afterwards.

    Returns:
        On success:
            {
                "success": True,
                "project_name": str,
                "netlist_path": str,      # where the netlist JSON was written
                "component_count": int,
                "net_count": int,
                "warnings": [str, ...],   # non-fatal issues (empty list = clean)
                "next_step": str,         # human-readable hint
            }
        On failure:
            {"success": False, "error": str}
    """
    from exporters.kicad_netlist_importer import convert_kicad_netlist

    # Validate project name
    if not re.match(r"^[a-z][a-z0-9_]*$", project_name):
        suggested = _slugify(project_name)
        return fail(
            f"Invalid project_name '{project_name}'. "
            "Use lowercase letters, digits, and underscores only (must start with a letter).",
            remediation=[option(
                f"Retry with the corrected name '{suggested}'",
                "import_kicad_netlist",
                {"project_name": suggested, "file_path": file_path},
            )],
        )

    # Existing-project conflict: refuse unless overwrite=True (then start clean).
    pdir = _project_dir(project_name)
    if pdir.exists() and any(pdir.iterdir()):
        if not overwrite:
            return fail(
                f"Project '{project_name}' already exists at {pdir} — not "
                "overwriting it. Re-import over it with overwrite=True (replaces "
                "the existing project), or import under a NEW project_name to "
                "keep it. Do NOT switch to design_pcb — that is the autonomous "
                "LLM pipeline, not a KiCad import, and won't help here.",
                remediation=[
                    option("Re-import over the existing project (replaces it)",
                           "import_kicad_netlist",
                           {"project_name": project_name, "file_path": file_path,
                            "overwrite": True}),
                    option("Import under a new name (keeps the existing project)",
                           "import_kicad_netlist",
                           {"project_name": f"{project_name}_v2",
                            "file_path": file_path}),
                    option("Check the existing project's state first",
                           "get_project_status", {"project_name": project_name}),
                ],
            )
        import shutil
        shutil.rmtree(pdir, ignore_errors=True)

    try:
        result = convert_kicad_netlist(
            source_path=file_path,
            project_name=project_name,
            description=description,
        )
    except (FileNotFoundError, ValueError) as exc:
        return fail(str(exc), remediation=[option(
            "Verify the file path and re-export the netlist from KiCad "
            "(Schematic Editor: File > Export > Netlist > KiCad format), then retry",
            "import_kicad_netlist",
            {"project_name": project_name, "file_path": "<corrected path>"},
        )])
    except Exception as exc:
        return fail(f"Unexpected error during import: {exc}")

    netlist = result["netlist"]
    warnings = result["warnings"]

    # Write netlist JSON into the project directory
    pdir.mkdir(parents=True, exist_ok=True)
    netlist_path = pdir / f"{project_name}_netlist.json"
    netlist_path.write_text(json.dumps(netlist, indent=2), encoding="utf-8")

    # Count elements for the summary
    elements = netlist.get("elements", [])
    n_comp = sum(1 for e in elements if e["element_type"] == "component")
    n_net  = sum(1 for e in elements if e["element_type"] == "net")

    # Verify every footprint resolves now, so the agent can fix packages
    # immediately instead of discovering placeholders after placement.
    _ensure_lookup_configured()
    from validators.verify_footprints import verify_footprints
    unresolved = verify_footprints(netlist)

    if unresolved:
        first = unresolved[0]
        step = next_step(
            "provide_footprint",
            {"project_name": project_name, "package": first["package"],
             "like_package": "<a recognized package, e.g. 0805, SOIC-8>"},
            f"{len(unresolved)} component(s) have unresolved footprints "
            f"(see unresolved_footprints). Placement is BLOCKED until every "
            f"footprint resolves; fix each, then call "
            f"verify_footprints('{project_name}') to confirm.",
        )
    else:
        step = next_step(
            "optimize_placement",
            {"project_name": project_name, "board_width_mm": "<width>",
             "board_height_mm": "<height>"},
            f"Netlist imported ({n_comp} components, {n_net} nets), all "
            "footprints resolved. Board dimensions are required on the first "
            "placement.",
        )

    return ok({
        "project_name":          project_name,
        "netlist_path":          str(netlist_path),
        "component_count":       n_comp,
        "net_count":             n_net,
        "warnings":              warnings,
        "unresolved_footprints": unresolved,
    }, step)


# ---------------------------------------------------------------------------
# Footprint verification + remediation (agent-driven footprint review)
# ---------------------------------------------------------------------------

@mcp.tool()
def verify_footprints(project_name: str) -> dict:
    """Check that every component's footprint resolves to real pad geometry.

    This is the deterministic gate that placement enforces. A component whose
    package cannot be resolved through any library tier (KiCad library →
    IPC-7351 → cache → built-in → normalized name) would silently become a 3mm
    placeholder — so placement refuses to run until this returns clean.

    Call after import_kicad_netlist, and again after each provide_footprint /
    package-name fix, until ``unresolved`` is empty.

    Args:
        project_name: Project slug (must already have a netlist).

    Returns:
        {
            "success": True,
            "resolved": bool,                 # True when nothing is unresolved
            "component_count": int,
            "unresolved_count": int,
            "unresolved_footprints": [        # empty when resolved
                {"designator", "package", "pin_count", "reason"}, ...
            ],
        }  or  {"success": False, "error": str}
    """
    pdir = _project_dir(project_name)
    netlist = _read_project_json(project_name, "_netlist.json")
    if netlist is None:
        return fail(
            f"No netlist for '{project_name}'.",
            remediation=[
                option("Import a KiCad netlist", "import_kicad_netlist",
                       {"project_name": project_name, "file_path": "<path to .net>"}),
                option("Build a circuit from scratch", "create_circuit",
                       {"project_name": project_name, "description": "<circuit description>"}),
            ],
        )

    _ensure_lookup_configured()
    _activate_project_lookup(project_name)
    from validators.verify_footprints import verify_footprints as _verify

    unresolved = _verify(netlist)
    n_comp = sum(1 for e in netlist.get("elements", [])
                 if e.get("element_type") == "component")
    if unresolved:
        first = unresolved[0]
        step = next_step(
            "provide_footprint",
            {"project_name": project_name, "package": first["package"],
             "like_package": "<recognized package, e.g. 0805, SOIC-8, SOT-23>"},
            f"{len(unresolved)} footprint(s) unresolved — fix each (alias via "
            "like_package, or pin_offsets + pad_size), then re-run "
            "verify_footprints.",
        )
    else:
        step = next_step(
            "optimize_placement",
            {"project_name": project_name, "board_width_mm": "<width>",
             "board_height_mm": "<height>"},
            "All footprints resolved — the placement gate is clear.",
        )
    return ok({
        "resolved": not unresolved,
        "component_count": n_comp,
        "unresolved_count": len(unresolved),
        "unresolved_footprints": unresolved,
    }, step)


@mcp.tool()
def provide_footprint(
    project_name: str,
    package: str,
    like_package: str | None = None,
    pin_offsets: dict | None = None,
    pad_size: list | None = None,
) -> dict:
    """Supply footprint geometry for a package the libraries don't know.

    Use exactly ONE of two modes:

    Mode 1 — alias a verbose/unknown name to a recognized package:

        provide_footprint("my_board", "R_0805_2012Metric_Pad1.05x1.40mm",
                          like_package="0805")

    Mode 2 — explicit geometry from the datasheet. pin_offsets maps pin number
    (string) to [dx_mm, dy_mm] from the component center at rotation 0;
    pad_size is [width_mm, height_mm]:

        provide_footprint("my_board", "CUSTOM-4",
                          pin_offsets={"1": [-1.27, 1.0], "2": [-1.27, -1.0],
                                       "3": [1.27, -1.0], "4": [1.27, 1.0]},
                          pad_size=[1.05, 1.4])

    The entry persists in the shared component cache for all later runs.
    After calling this, run verify_footprints to confirm the gate is clear.
    """
    _ensure_lookup_configured()
    from optimizers.pad_geometry import get_footprint_def, get_default_cache

    _verify_step = next_step(
        "verify_footprints", {"project_name": project_name},
        "Confirm the footprint gate is now clear.",
    )

    cache = get_default_cache()
    if cache is None:
        return fail("Component cache is not configured; cannot persist footprint.")

    if not package:
        return fail("package must be a non-empty string.")

    # Mode 1: alias to a recognized package.
    if like_package:
        ref = get_footprint_def(like_package, 0)
        if ref is None:
            return fail(
                f"like_package '{like_package}' is itself unresolved.",
                remediation=[
                    option("Alias to a recognized package instead",
                           "provide_footprint",
                           {"project_name": project_name, "package": package,
                            "like_package": "<one of: 0402, 0603, 0805, 1206, "
                                            "SOIC-8, SOT-23, DIP-8, TQFP-32>"}),
                    option("Supply explicit geometry from the datasheet",
                           "provide_footprint",
                           {"project_name": project_name, "package": package,
                            "pin_offsets": {"1": [-1.27, 0.0], "2": [1.27, 0.0]},
                            "pad_size": [1.05, 1.4]}),
                ],
            )
        offsets = {str(k): [float(v[0]), float(v[1])]
                   for k, v in ref.pin_offsets.items()}
        cache.put_footprint(package, offsets, list(ref.pad_size),
                            source="agent", needs_review=True)
        return ok({"package": package,
                   "source": f"agent (alias of {like_package})",
                   "pin_count": len(offsets)}, _verify_step)

    # Mode 2: explicit geometry.
    if pin_offsets and pad_size:
        try:
            offsets = {str(k): [float(v[0]), float(v[1])]
                       for k, v in pin_offsets.items()}
            psize = [float(pad_size[0]), float(pad_size[1])]
        except (TypeError, ValueError, IndexError, KeyError) as exc:
            return fail(
                f"Malformed pin_offsets/pad_size: {exc}.",
                remediation=[option(
                    "Retry with the exact shapes shown in args: pin_offsets maps "
                    "pin number to [dx_mm, dy_mm]; pad_size is [width_mm, height_mm]",
                    "provide_footprint",
                    {"project_name": project_name, "package": package,
                     "pin_offsets": {"1": [-1.27, 0.0], "2": [1.27, 0.0]},
                     "pad_size": [1.05, 1.4]},
                )],
            )
        cache.put_footprint(package, offsets, psize,
                            source="agent", needs_review=True)
        return ok({"package": package, "source": "agent",
                   "pin_count": len(offsets)}, _verify_step)

    return fail(
        "Provide either like_package, or pin_offsets + pad_size.",
        remediation=[
            option("Alias to a recognized package", "provide_footprint",
                   {"project_name": project_name, "package": package,
                    "like_package": "0805"}),
            option("Supply explicit geometry", "provide_footprint",
                   {"project_name": project_name, "package": package,
                    "pin_offsets": {"1": [-1.27, 0.0], "2": [1.27, 0.0]},
                    "pad_size": [1.05, 1.4]}),
        ],
    )


# ---------------------------------------------------------------------------
# Incremental circuit builder (design from scratch with small validated calls)
# ---------------------------------------------------------------------------

def _builder_fail(result: dict, project_name: str) -> dict:
    """Map a circuit_builder error result onto the failure envelope."""
    code = result.get("code", "")
    rem = []
    if code == "no_draft":
        rem.append(option("Create the circuit draft first", "create_circuit",
                          {"project_name": project_name,
                           "description": "<circuit description>",
                           "board_width_mm": 50, "board_height_mm": 40}))
    elif code == "unresolved_footprint":
        rem.append(option(
            "Supply geometry for the unknown package, then retry add_component",
            "provide_footprint",
            {"project_name": project_name, "package": result.get("package"),
             "like_package": "<recognized package, e.g. 0805, SOIC-8, SOT-23>"},
        ))
    elif code == "unknown_pin_count":
        rem.append(option(
            "Retry with an explicit pinout string", "add_component",
            {"project_name": project_name,
             "pinout": "1:GND 2:TRIG 3:OUT 4:RESET 5:CTRL 6:THRES 7:DISCH 8:VCC"},
        ))
    elif code == "unconnected_pins":
        first = (result.get("unconnected_pins") or ["U1.1"])[0]
        rem.append(option("Connect the listed pins", "connect_pins",
                          {"project_name": project_name, "net_name": "<net>",
                           "pins": [first, "<other pin>"]}))
        rem.append(option("Mark truly unused pins as no-connect",
                          "mark_no_connect",
                          {"project_name": project_name,
                           "pins": result.get("unconnected_pins", [])[:12]}))
    elif code in ("pin_conflict", "single_pin_nets"):
        rem.append(option("Review the current circuit state", "list_circuit",
                          {"project_name": project_name}))
    data = {k: v for k, v in result.items()
            if k not in ("ok", "error", "code")}
    return fail(result.get("error", "Operation failed."),
                remediation=rem or None, data=data or None)


@mcp.tool()
def create_circuit(project_name: str, description: str,
                   board_width_mm: float, board_height_mm: float,
                   layers: int = 2, overwrite: bool = False) -> dict:
    """Start a new circuit design from scratch (step 1 of the builder flow).

    Creates an empty draft you then fill with add_component and connect_pins,
    and compile with finalize_circuit. Each call is small and validated — no
    big JSON needed.

    If the project already exists, this fails unless overwrite=True (which
    replaces the existing project with a fresh draft). Do not switch to
    design_pcb to dodge the conflict — use a new project_name or overwrite=True.

    layers: 2 (default) or 4 copper layers. A 4-layer board can also be
    promoted later by passing plane_layers to optimize_placement.

    Example: create_circuit("led_blinker", "555 LED blinker at 1Hz",
                            board_width_mm=40, board_height_mm=30)
    """
    bad = _reject_bad_project_name(project_name)
    if bad:
        return bad
    from orchestrator import circuit_builder as cb
    result = cb.create_draft(_project_dir(project_name), project_name,
                             description, board_width_mm, board_height_mm,
                             layers, overwrite=overwrite)
    if not result.pop("ok"):
        return _builder_fail(result, project_name)
    return ok(result, next_step(
        "add_component",
        {"project_name": project_name, "designator": "U1",
         "component_type": "ic", "value": "<part>", "package": "<package>"},
        "Add each component; the response lists its pins for connect_pins.",
    ))


@mcp.tool()
def add_component(project_name: str, designator: str, component_type: str,
                  value: str, package: str, pinout: str | None = None,
                  pin_count: int | None = None,
                  functional_group: str | None = None) -> dict:
    """Add one component to the circuit draft. Returns its pin table.

    component_type: resistor, capacitor, inductor, led, diode, transistor_npn,
    transistor_pnp, transistor_nmos, transistor_pmos, ic, connector, switch,
    voltage_regulator, crystal, fuse, relay.

    The package is resolved to a real footprint immediately — unknown packages
    fail here (fix with provide_footprint) instead of blocking placement later.
    For ICs, pass pinout so pins get names you can use in connect_pins:

        add_component("my_board", "U1", "ic", "NE555", "DIP-8",
                      pinout="1:GND 2:TRIG 3:OUT 4:RESET 5:CTRL 6:THRES "
                             "7:DISCH 8:VCC")

    LEDs/diodes get pin names anode (1) / cathode (2) automatically;
    transistors get base/emitter/collector or gate/source/drain (SOT-23
    convention); 3-pin regulators get IN/GND/OUT. pin_count overrides the
    count derived from the package name when they disagree.

    functional_group: optional short lowercase label for the functional block
    this part belongs to (e.g. "power", "mcu", "usb", "analog"). Parts sharing
    a label are placed close together, which reduces inter-block crossings and
    eases routing. Group by role, not type — a decoupling cap belongs to its
    IC's block. Use a handful of meaningful blocks; do NOT put everything in
    one group or invent a unique group per part. Omitting it is safe — the
    optimizer falls back to its shared-net heuristic.
    """
    _ensure_lookup_configured()
    from orchestrator import circuit_builder as cb
    from optimizers.pad_geometry import get_footprint_def
    result = cb.add_component(_project_dir(project_name), project_name,
                              designator, component_type, value, package,
                              pinout=pinout, pin_count=pin_count,
                              functional_group=functional_group,
                              footprint_lookup=get_footprint_def)
    if not result.pop("ok"):
        return _builder_fail(result, project_name)
    return ok(result, next_step(
        "connect_pins",
        {"project_name": project_name, "net_name": "<net>",
         "pins": [f"{designator}.1", "<other pin>"]},
        "Add more components, or start connecting pins into nets.",
    ))


@mcp.tool()
def connect_pins(project_name: str, net_name: str, pins: list[str],
                 net_class: str | None = None) -> dict:
    """Connect component pins into a named net (creates the net if new).

    pins use DESIGNATOR.PIN form — pin number or pin name:

        connect_pins("my_board", "VCC", ["U1.8", "C1.1", "J1.1"])
        connect_pins("my_board", "LED_DRIVE", ["R1.2", "D1.anode"])

    net_class (signal | power | ground) is auto-inferred from the net name
    (VCC/5V → power, GND → ground) — pass it only to override. Idempotent:
    re-connecting the same pin to the same net is a no-op; a pin already on a
    DIFFERENT net is an error (disconnect_pins first).
    """
    from orchestrator import circuit_builder as cb
    result = cb.connect_pins(_project_dir(project_name), project_name,
                             net_name, pins, net_class)
    if not result.pop("ok"):
        return _builder_fail(result, project_name)
    return ok(result, next_step(
        "list_circuit", {"project_name": project_name},
        "Connect remaining nets, then list_circuit to see unconnected pins, "
        "then finalize_circuit.",
    ))


@mcp.tool()
def disconnect_pins(project_name: str, net_name: str,
                    pins: list[str]) -> dict:
    """Remove pins from a net (the net is deleted when it becomes empty).

    Example: disconnect_pins("my_board", "VCC", ["U1.8"])
    """
    from orchestrator import circuit_builder as cb
    result = cb.disconnect_pins(_project_dir(project_name), project_name,
                                net_name, pins)
    if not result.pop("ok"):
        return _builder_fail(result, project_name)
    return ok(result)


@mcp.tool()
def mark_no_connect(project_name: str, pins: list[str]) -> dict:
    """Mark pins as intentionally unused (finalize_circuit requires every pin
    to be connected or explicitly no-connect).

    Example: mark_no_connect("my_board", ["U1.5", "U1.4"])
    """
    from orchestrator import circuit_builder as cb
    result = cb.mark_no_connect(_project_dir(project_name), project_name, pins)
    if not result.pop("ok"):
        return _builder_fail(result, project_name)
    return ok(result)


@mcp.tool()
def remove_component(project_name: str, designator: str) -> dict:
    """Remove a component from the draft (also detaches it from all nets).

    Example: remove_component("my_board", "R3")
    """
    from orchestrator import circuit_builder as cb
    result = cb.remove_component(_project_dir(project_name), project_name,
                                 designator)
    if not result.pop("ok"):
        return _builder_fail(result, project_name)
    return ok(result)


@mcp.tool()
def list_circuit(project_name: str) -> dict:
    """Show the current circuit draft: components, nets, no-connects, and —
    importantly — any pins still unconnected (these block finalize_circuit).
    """
    from orchestrator import circuit_builder as cb
    draft = cb.load_draft(_project_dir(project_name), project_name)
    if draft is None:
        return _builder_fail({"code": "no_draft",
                              "error": f"No circuit draft for '{project_name}'. "
                                       "Call create_circuit first."},
                             project_name)
    result = cb.list_circuit(draft)
    result.pop("ok", None)
    unconnected = result.get("unconnected_pins", [])
    if unconnected:
        step = next_step(
            "connect_pins",
            {"project_name": project_name, "net_name": "<net>",
             "pins": unconnected[:2]},
            f"{len(unconnected)} pin(s) still unconnected — connect them or "
            "mark_no_connect, then finalize_circuit.",
        )
    else:
        step = next_step("finalize_circuit", {"project_name": project_name},
                         "All pins are accounted for — compile and validate "
                         "the netlist.")
    return ok(result, step)


@mcp.tool()
def finalize_circuit(project_name: str) -> dict:
    """Compile the draft into the project netlist and validate it fully
    (schema, referential integrity, electrical DRC, footprint gate).

    On success the project is ready for optimize_placement (the next_step
    includes your board dimensions). On failure, 'errors' lists exactly what
    to fix with connect_pins / remove_component / add_component.
    """
    _ensure_lookup_configured()
    from orchestrator import circuit_builder as cb
    result = cb.finalize(_project_dir(project_name), project_name)
    if not result.pop("ok"):
        return _builder_fail(result, project_name)
    board = result.get("board", {})
    return ok(result, next_step(
        "optimize_placement",
        {"project_name": project_name,
         "board_width_mm": board.get("width_mm"),
         "board_height_mm": board.get("height_mm")},
        "Netlist is valid — place the components next.",
    ))


_COMPONENT_KEYS = {"designator", "component_type", "value", "package",
                   "pinout", "pin_count", "functional_group"}
_NET_KEYS = {"net_name", "pins", "net_class"}


@mcp.tool()
def build_circuit(project_name: str, description: str,
                  board_width_mm: float, board_height_mm: float,
                  components: list[dict], nets: list[dict],
                  no_connect: list[str] | None = None,
                  layers: int = 2, overwrite: bool = False) -> dict:
    """Build a whole circuit in ONE call: draft + all components + all nets +
    compile. The bulk form of create_circuit → add_component → connect_pins →
    finalize_circuit (which stay available for incremental edits).

    Use this whenever you already know the full circuit — a 40-part board is
    one call here instead of ~60.

    components: list of add_component arg dicts —
        {"designator": "U1", "component_type": "ic", "value": "NE555",
         "package": "DIP-8", "pinout": "1:GND 2:TRIG ...",   # optional
         "pin_count": 8, "functional_group": "timer"}        # optional
    nets: list of connect_pins arg dicts —
        {"net_name": "VCC", "pins": ["U1.8", "C1.1"], "net_class": "power"}
        (net_class optional — inferred from the name.)
    no_connect: pins that are intentionally unused, e.g. ["U1.5"].
    layers: 2 (default) or 4 copper layers, as in create_circuit.
    overwrite: default False; True replaces an existing project of this name.

    EVERY item is attempted; the draft keeps whatever succeeded. On failure the
    response lists 'failed' per item (each with its own error and remediation)
    plus 'added'/'connected' counts — fix just the failures with add_component /
    connect_pins and call finalize_circuit, no need to rebuild from scratch.

    Example: build_circuit("blinker", "555 blinker", 40, 30,
        components=[{"designator": "U1", "component_type": "ic",
                     "value": "NE555", "package": "DIP-8",
                     "pinout": "1:GND 2:TRIG 3:OUT 4:RESET 5:CTRL 6:THRES "
                               "7:DISCH 8:VCC"}],
        nets=[{"net_name": "GND", "pins": ["U1.1"]}])
    """
    created = create_circuit(project_name, description, board_width_mm,
                             board_height_mm, layers=layers, overwrite=overwrite)
    if not created.get("success"):
        return created

    failed: list[dict] = []

    def _record(item: dict | str, result: dict) -> bool:
        if result.get("success"):
            return True
        entry = {"item": item, "error": result.get("error", "failed")}
        if result.get("remediation"):
            entry["remediation"] = result["remediation"]
        failed.append(entry)
        return False

    added = 0
    for comp in components:
        bad_keys = set(comp) - _COMPONENT_KEYS
        if bad_keys:
            failed.append({"item": comp,
                           "error": f"Unknown key(s) {sorted(bad_keys)}; valid "
                                    f"keys are {sorted(_COMPONENT_KEYS)}."})
            continue
        try:
            r = add_component(project_name, **comp)
        except TypeError as exc:  # missing required key
            failed.append({"item": comp, "error": str(exc)})
            continue
        added += _record(comp, r)

    connected = 0
    for net in nets:
        bad_keys = set(net) - _NET_KEYS
        if bad_keys:
            failed.append({"item": net,
                           "error": f"Unknown key(s) {sorted(bad_keys)}; valid "
                                    f"keys are {sorted(_NET_KEYS)}."})
            continue
        try:
            r = connect_pins(project_name, **net)
        except TypeError as exc:
            failed.append({"item": net, "error": str(exc)})
            continue
        connected += _record(net, r)

    if no_connect:
        _record(no_connect, mark_no_connect(project_name, no_connect))

    if failed:
        return fail(
            f"{len(failed)} of {len(components) + len(nets)} item(s) failed; "
            f"{added} component(s) and {connected} net(s) were applied and are "
            "kept in the draft. Fix only the failures, then finalize_circuit.",
            data={"failed": failed, "added": added, "connected": connected},
            remediation=[option("Review the draft and what is still unconnected",
                                "list_circuit", {"project_name": project_name})],
        )

    return finalize_circuit(project_name)


@mcp.tool()
def place_component(project_name: str, designator: str, x_mm: float,
                    y_mm: float, rotation_deg: int = 0,
                    layer: str = "top") -> dict:
    """Fix a component at exact board coordinates (e.g. a connector that must
    sit on an edge, or a mounting hole matching an enclosure).

    Validated immediately: the position must keep the component's PADS inside
    the board (1mm edge clearance) and clear of other pinned components —
    invalid coordinates fail here, not as silent overlaps later. When a
    position is rejected, the failure carries a concrete free coordinate
    (`suggested_x_mm`/`suggested_y_mm`, and a ready-to-run remediation entry) —
    retry there instead of guessing. Pinned components are never moved by
    optimize_placement; everything else is placed around them. Coordinates are
    mm from the top-left board corner (x right, y down). Re-calling replaces
    the pin; undo with unplace_component.

    layer: "top" (default) or "bottom" — anything else is rejected. This is the
    side the part is pinned to, and it holds regardless of two_sided (which only
    governs which parts the optimizer may move by itself).

    Example: place_component("my_board", "J1", x_mm=2.5, y_mm=20,
                             rotation_deg=90)
    """
    from orchestrator import stages
    _ensure_lookup_configured()
    result = stages.set_placement_pin(_project_dir(project_name), project_name,
                                      designator, x_mm, y_mm, rotation_deg,
                                      layer)
    if not result.pop("ok"):
        rem = []
        code = result.get("code")
        if code in ("out_of_bounds", "pin_overlap"):
            # If the validator found a concrete free spot, offer it as a
            # ready-to-run retry so the agent doesn't have to guess (and loop).
            sx, sy = result.get("suggested_x_mm"), result.get("suggested_y_mm")
            if sx is not None and sy is not None:
                rem.append(option(
                    f"Retry at the suggested free position ({sx}, {sy})",
                    "place_component",
                    {"project_name": project_name, "designator": designator,
                     "x_mm": sx, "y_mm": sy, "rotation_deg": rotation_deg,
                     "layer": layer}))
            rem.append(option("Retry with adjusted coordinates",
                              "place_component",
                              {"project_name": project_name,
                               "designator": designator,
                               "x_mm": "<new x>", "y_mm": "<new y>"}))
        elif code == "no_netlist":
            # A builder draft exists but was never compiled — steer to
            # finalize_circuit rather than leaving the agent to guess.
            if (_project_dir(project_name)
                    / f"{project_name}_circuit_draft.json").exists():
                rem.append(option("Compile the draft into a netlist first",
                                  "finalize_circuit",
                                  {"project_name": project_name}))
            else:
                rem.append(option("Build a circuit from scratch", "create_circuit",
                                  {"project_name": project_name,
                                   "description": "<circuit description>",
                                   "board_width_mm": 50, "board_height_mm": 40}))
                rem.append(option("Import a KiCad netlist", "import_kicad_netlist",
                                  {"project_name": project_name,
                                   "file_path": "<path to .net>"}))
        elif code == "unknown_designator":
            rem.append(option("List the circuit to get valid designators",
                              "list_circuit", {"project_name": project_name}))
        return fail(result.get("error", "place_component failed."),
                    remediation=rem or None)
    return ok(result, next_step(
        "optimize_placement", {"project_name": project_name},
        "Pin more components, or run placement — pinned components stay "
        "fixed and everything else is placed around them.",
    ))


@mcp.tool()
def unplace_component(project_name: str, designator: str) -> dict:
    """Remove a component's fixed-position pin so optimize_placement may move
    it again. Clears the pin from BOTH sources — the durable pin store and the
    placement file's user flag — so the next placement does not resurrect it.
    To unpin everything at once, use clear_all_pins.

    Example: unplace_component("my_board", "J1")
    """
    from orchestrator import stages
    result = stages.clear_placement_pin(_project_dir(project_name),
                                        project_name, designator)
    if not result.pop("ok"):
        rem = [option("Unpin every component instead", "clear_all_pins",
                      {"project_name": project_name})]
        return fail(result.get("error", "unplace_component failed."),
                    remediation=rem)
    return ok(result, next_step("optimize_placement",
                                {"project_name": project_name},
                                "Re-run placement to apply the change."))


@mcp.tool()
def clear_all_pins(project_name: str) -> dict:
    """Unpin EVERY component so optimize_placement is free to move all of them.

    Wipes the durable pin store AND resets every placement_source="user" flag
    in the placement file — the single call to use when you want a clean slate
    rather than unplace_component'ing parts one at a time. Returns the list of
    designators that were unpinned.

    Example: clear_all_pins("my_board")
    """
    from orchestrator import stages
    result = stages.clear_all_placement_pins(_project_dir(project_name),
                                             project_name)
    result.pop("ok", None)
    return ok(result, next_step(
        "optimize_placement", {"project_name": project_name},
        "All pins cleared — re-run placement; every component is free to move."))


# ---------------------------------------------------------------------------
# Granular deterministic stages (agent-driven flow — no LLM, no vision critic)
# ---------------------------------------------------------------------------

@mcp.tool()
def optimize_placement(
    project_name: str,
    board_width_mm: float | None = None,
    board_height_mm: float | None = None,
    seed: int | None = None,
    two_sided: bool | None = None,
    plane_layers: int | None = None,
    layers: int | None = None,
    approved: bool = False,
) -> dict:
    """Place components deterministically and optimize the layout (no LLM).

    Runs deterministic grid placement → overlap repair → simulated-annealing
    optimization (wirelength + signal-net crossings). Reads the project netlist,
    writes the project placement. Returns quickly.

    Call this after import_kicad_netlist (or after design_pcb has produced a
    netlist). On the first placement you must supply board dimensions — a KiCad
    netlist carries no board outline. On a re-run, dimensions are reused from the
    existing placement if omitted.

    seed: fixes the annealing RNG so a placement is reproducible; omit for a
    fresh layout each run. Different seeds do give genuinely different layouts,
    but do not seed-hunt for routability: re-routing one unchanged placement
    varies by more completion points than most seeds differ by, so a "better"
    seed is usually a luckier route.

    two_sided=True lets the optimizer move small SMD passives to the BOTTOM —
    use it when parts do not FIT on top, not when routing is the problem (on
    2-layer boards the bottom is the router's escape layer, so it can REDUCE
    completion). Connectors, ICs, LEDs, through-hole and pinned parts stay put.
    Omit it to inherit whatever the project already specified; pass False to
    force single-sided, which moves everything back to the top.

    layers: 2 (default) or 4 copper layers. plane_layers (4-layer only): how
    many inner layers are solid planes — 2 (default) = GND + power planes,
    1 = GND plane + a 3rd signal layer, 0 = all inner layers signal. Both
    persist for re-placements; passing plane_layers promotes a 2-layer board to
    4 layers. See get_workflow_guide()['reference']['stackup'] for which to
    pick when a board won't route.

    approved: two changes the user likely constrained are ENFORCED to need
    their explicit approval once a placement exists — promoting a 2-layer
    board to 4 layers (cost/stackup) and enlarging the board (enclosure /
    mating fit). Such a call fails until you re-run it with approved=True,
    which you may pass ONLY after the user has agreed in conversation. The
    first placement is never gated.

    Example: optimize_placement("my_board", board_width_mm=45,
                                board_height_mm=18, layers=4, plane_layers=1)
    """
    from orchestrator import stages

    if layers is not None and layers not in (2, 4):
        return fail(
            f"layers must be 2 or 4 (got {layers}).",
            remediation=[option(
                "Re-run with layers=4 for a dense board", "optimize_placement",
                {"project_name": project_name, "layers": 4})],
        )

    pdir = _project_dir(project_name)
    if not pdir.exists():
        return fail(
            f"Project '{project_name}' not found.",
            remediation=[
                option("Import a KiCad netlist first", "import_kicad_netlist",
                       {"project_name": project_name, "file_path": "<path to .net>"}),
                option("Build a circuit from scratch", "create_circuit",
                       {"project_name": project_name, "description": "<circuit description>"}),
                option("List existing projects to find the right name", "list_projects", {}),
            ],
        )

    blocked = _electrical_gate(project_name, "placement")
    if blocked:
        return blocked

    # Activate project-local custom footprints (tier 0) before placement so
    # agent-registered .kicad_mod files are visible to the placement engine.
    if not approved:
        reason = _requires_approval(project_name, layers, plane_layers,
                                    board_width_mm, board_height_mm)
        if reason:
            args = {k: v for k, v in {
                "project_name": project_name,
                "board_width_mm": board_width_mm,
                "board_height_mm": board_height_mm,
                "seed": seed, "two_sided": two_sided,
                "plane_layers": plane_layers, "layers": layers,
            }.items() if v is not None}
            args["approved"] = True
            return fail(
                f"This change requires explicit user approval: {reason}. "
                "ASK THE USER first — do not decide this on your own. Only "
                "after they agree, re-run with approved=True.",
                remediation=[option(
                    "Re-run once the user has approved the change",
                    "optimize_placement", args)],
            )

    _activate_project_lookup(project_name)
    _snapshot_board(project_name)

    try:
        result = stages.run_placement(
            pdir, project_name, _get_config(),
            board_width_mm=board_width_mm,
            board_height_mm=board_height_mm,
            seed=seed,
            two_sided=two_sided,
            plane_layers=plane_layers,
            layers=layers,
        )
    except Exception as exc:
        return fail(f"Placement failed: {exc}")

    if not result.get("success"):
        rem = []
        if result.get("unresolved_footprints"):
            first = result["unresolved_footprints"][0]
            rem.append(option(
                "Resolve the blocked footprints, then re-run placement",
                "provide_footprint",
                {"project_name": project_name, "package": first.get("package"),
                 "like_package": "<recognized package>"},
            ))
        if result.get("violations"):
            v = result["violations"]
            pinned_dess = sorted({e["designator"] for e in v["out_of_bounds"]
                                  if e["pinned"]}
                                 | {d for o in v["overlaps"] if o["pinned"]
                                    for d in (o["a"], o["b"])})
            if pinned_dess:
                rem.append(option(
                    f"Adjust the fixed position of {pinned_dess[0]} (it "
                    "conflicts and is never moved automatically)",
                    "place_component",
                    {"project_name": project_name,
                     "designator": pinned_dess[0],
                     "x_mm": "<new x>", "y_mm": "<new y>"}))
                rem.append(option(
                    "Or unpin it and let the optimizer place it",
                    "unplace_component",
                    {"project_name": project_name,
                     "designator": pinned_dess[0]}))
            rem.append(option(
                "Re-place on a larger board", "optimize_placement",
                {"project_name": project_name,
                 "board_width_mm": "<larger width>",
                 "board_height_mm": "<larger height>"}))
        return fail(result.get("error", "Placement failed."),
                    remediation=rem or None, data=result)

    promo = ""
    if result.get("layers_promoted"):
        promo = (f" NOTE: promoted to a {result.get('layers')}-layer board "
                 f"(plane_layers={result.get('plane_layers')}) because an inner-"
                 "plane stackup was requested.")
    return ok(result, next_step(
        "route_board", {"project_name": project_name},
        f"Placement done ({result.get('layers')}-layer): wire length "
        f"{result.get('wire_length_mm')}mm, {result.get('crossings')} crossings."
        f"{promo} Routing runs in the background; poll get_project_status "
        "afterwards.",
    ))


@mcp.tool()
def route_board(project_name: str, effort: str = "normal",
                max_seconds: int | None = None, auto_retry: bool = True,
                allow_grow: bool = False, keep_existing: bool = False) -> dict:
    """Start routing the placed board (deterministic). Returns immediately.

    Routing runs on a background thread. Poll get_project_status(project_name)
    and read 'routing_state' (running → complete | failed); while running,
    'routing_progress' and 'status_hint' report live pass-by-pass progress.
    When complete, 'routing_result' holds the stats (completion_pct,
    routed_nets, via_count, unrouted_nets, valid). completion_pct is
    PAD-level: 100% means every pad is actually connected (reconciled against
    the connectivity validator), not just that the autorouter counted its net
    as done. A power-plane SMD pad with no clear stitching-via site keeps its
    net in unrouted_nets and is named in routing.unstitched_plane_pads.

    effort controls routing quality vs wait time:
      "fast"   — quick first result (~2 min cap), fewer optimization passes.
      "normal" — default balance (~5 min cap).
      "best"   — maximum optimization (~15 min cap, auto-retries on timeout).
    max_seconds overrides the effort level's time cap when given.

    auto_retry (default true): if the route is incomplete, automatically
    re-place once with extra component clearance and re-route, keeping the
    better result. allow_grow additionally permits a 10% board-size increase
    for that retry.

    keep_existing=True does INCREMENTAL routing: the project's current routed
    board is kept as protected wiring and only the UNROUTED nets are routed —
    use it to finish a partly-routed board (e.g. one imported from KiCad or a
    prior incomplete route) instead of redoing it. Placement is not changed
    (so existing traces stay valid) and auto_retry is ignored.

    If a route lands <100% (or worse than the last one), get_project_status'
    next_step carries the recovery ladder; revert_board restores the previous
    board. get_workflow_guide()['reference']['routing_capacity'] has the full
    escalation.

    Requires a placement — call optimize_placement first.

    Example: route_board("my_board", effort="best", keep_existing=True)
    """
    if effort not in ("fast", "normal", "best"):
        return fail(
            f"Invalid effort '{effort}'.",
            remediation=[option(
                "Use one of: fast, normal, best", "route_board",
                {"project_name": project_name, "effort": "normal"},
            )],
        )
    from orchestrator import stages

    pdir = _project_dir(project_name)
    if not pdir.exists():
        return fail(
            f"Project '{project_name}' not found.",
            remediation=[option("List existing projects", "list_projects", {})],
        )
    if not (pdir / f"{project_name}_placement.json").exists():
        return fail(
            "No placement found.",
            remediation=[option(
                "Place the components first", "optimize_placement",
                {"project_name": project_name, "board_width_mm": "<width>",
                 "board_height_mm": "<height>"},
            )],
        )

    blocked = _electrical_gate(project_name, "routing")
    if blocked:
        return blocked

    # Routing (and its escape/fill/cleanup passes) is driven entirely by pad
    # geometry — activate the project's footprint lookup before the worker
    # thread starts, so a cold server routes the same board a warm one does.
    _activate_project_lookup(project_name)

    import time as _time

    with _ROUTE_LOCK:
        current = _ROUTE_JOBS.get(project_name)
        if current and current["state"] == "running":
            return working(
                data={"project_name": project_name},
                poll_again_in_s=15,
                status_hint=(
                    "Routing already in progress. Check get_project_status for "
                    "'routing_state' in ~15s (and follow its poll_again_in_s "
                    "thereafter); do not poll faster and do not start another "
                    "route_board."
                ),
            )
        _ROUTE_JOBS[project_name] = {
            "state": "running", "result": None, "error": None,
            "started_at": _time.monotonic(), "progress": None,
        }

    config = _get_config()

    def _on_progress(p: dict) -> None:
        with _ROUTE_LOCK:
            job = _ROUTE_JOBS.get(project_name)
            if job and job["state"] == "running":
                job["progress"] = p

    # Incremental: protect the existing routed traces/vias and route only the
    # nets that still need work. Read before the worker so a missing/empty board
    # is caught. Crucially, we protect only FULLY-CONNECTED nets — a net that is
    # routed but split into >1 group must be left unprotected so Freerouting
    # re-routes it (protected wiring is treated as done, so protecting a
    # disconnected net would leave it disconnected forever).
    fixed_routing = None
    if keep_existing:
        existing = _read_project_json(project_name, "_routed.json")
        netlist = _read_project_json(project_name, "_netlist.json")
        fixed_routing = stages.build_incremental_fixed_routing(existing, netlist)

    # Snapshot before the worker overwrites the board: a re-route can come back
    # worse than the one it replaced (revert_board restores this).
    _snapshot_board(project_name)

    def _worker() -> None:
        try:
            if keep_existing:
                # No re-placement (would invalidate existing traces); route
                # only the remaining nets with the rest held as protected wiring.
                result = stages.run_routing(pdir, project_name, config,
                                            progress_callback=_on_progress,
                                            effort=effort, max_seconds=max_seconds,
                                            fixed_routing=fixed_routing)
            elif auto_retry:
                result = stages.run_route_with_retry(
                    pdir, project_name, config,
                    progress_callback=_on_progress,
                    effort=effort, max_seconds=max_seconds,
                    allow_grow=allow_grow)
            else:
                result = stages.run_routing(pdir, project_name, config,
                                            progress_callback=_on_progress,
                                            effort=effort, max_seconds=max_seconds)
            state = "complete" if result.get("success") else "failed"
            with _ROUTE_LOCK:
                started = _ROUTE_JOBS.get(project_name, {}).get("started_at")
                _ROUTE_JOBS[project_name] = {
                    "state": state, "result": result, "error": result.get("error"),
                    "started_at": started, "progress": None,
                    "elapsed_s": round(_time.monotonic() - started, 1) if started else None,
                }
        except Exception as exc:  # noqa: BLE001 — surface any failure to the poller
            with _ROUTE_LOCK:
                started = _ROUTE_JOBS.get(project_name, {}).get("started_at")
                _ROUTE_JOBS[project_name] = {
                    "state": "failed", "result": None, "error": str(exc),
                    "started_at": started, "progress": None,
                    "elapsed_s": round(_time.monotonic() - started, 1) if started else None,
                }

    threading.Thread(target=_worker, daemon=True).start()

    return working(
        data={
            "project_name": project_name,
            "next_step": next_step(
                "get_project_status", {"project_name": project_name},
                "Check get_project_status until 'routing_state' is 'complete' or "
                "'failed', waiting the 'poll_again_in_s' it returns between "
                "checks; 'routing_progress' shows live progress.",
            ),
        },
        poll_again_in_s=15,
        status_hint=(
            "Routing started in the background (can take seconds to many "
            "minutes). Check get_project_status in ~15s, then wait the "
            "'poll_again_in_s' it returns between checks — do NOT poll faster, "
            "it does not speed routing up. After the main route, a short-cleanup "
            "phase may rip and re-route a few shorting/incomplete nets (a brief "
            "gap with no pass progress is normal); wait for 'routing_state' to "
            "reach 'complete'. Do not run other tools or external CLIs for this "
            "project while routing is active."
        ),
    )


@mcp.tool()
def revert_board(project_name: str) -> dict:
    """Undo the last optimize_placement or route_board, restoring the previous
    placement + routed board.

    Routing is nondeterministic: a re-route (or a stackup/board-size change you
    hoped would help) can come back WORSE than the one it replaced. Call this to
    get the better board back instead of re-routing hopefully.

    Exactly ONE step of history is kept, taken at the start of each placement or
    routing run — reverting twice does not go back two steps. The stale DRC
    report is dropped, so re-run run_drc after reverting.
    """
    pdir = _project_dir(project_name)
    if not pdir.exists():
        return fail(
            f"Project '{project_name}' not found.",
            remediation=[option("List existing projects", "list_projects", {})],
        )

    with _ROUTE_LOCK:
        job = _ROUTE_JOBS.get(project_name)
    if job and job["state"] == "running":
        return working(
            data={"project_name": project_name},
            poll_again_in_s=15,
            status_hint=("Routing is still running — it would overwrite the "
                         "restored board. Poll get_project_status until "
                         "'routing_state' is 'complete' or 'failed', then "
                         "revert_board."),
        )

    snapshots = {s: pdir / f"{project_name}{s}.prev" for s in _SNAPSHOT_SUFFIXES}
    if not any(p.exists() for p in snapshots.values()):
        return fail(
            f"Nothing to revert for '{project_name}' — no previous placement or "
            "routed board was saved (only optimize_placement and route_board "
            "create a revert point, and only for their own last run).",
            remediation=[option("Check the current state", "get_project_status",
                                {"project_name": project_name})],
        )

    restored = []
    for suffix, snap in snapshots.items():
        live = pdir / f"{project_name}{suffix}"
        if snap.exists():
            live.write_bytes(snap.read_bytes())
            restored.append(live.name)
        elif live.exists():
            live.unlink()  # the snapshot predates this file — remove it too

    drc = pdir / f"{project_name}_drc_report.json"
    if drc.exists():
        drc.unlink()

    routed = _read_project_json(project_name, "_routed.json")
    stats = routing_stats(routed) if routed else {}
    completion = stats.get("completion_pct")
    return ok(
        {"project_name": project_name, "restored": restored,
         "completion_pct": completion},
        next_step(
            "run_drc" if routed else "route_board", {"project_name": project_name},
            (f"Restored the previous board ({completion}% routed). The DRC "
             "report was dropped — re-run DRC on the restored board."
             if routed else
             "Restored the previous placement; there is no routed board in this "
             "snapshot, so route it again."),
        ),
    )


@mcp.tool()
def run_drc(project_name: str) -> dict:
    """Run deterministic design-rule checks on the routed board (no LLM).

    14 manufacturability/electrical checks (clearances, trace widths, annular
    rings, connectivity, shorts, IPC-2221 current capacity, etc.). Returns an
    agent-friendly summary: 'passed', severity-ranked 'top_violations',
    'failing_rules' each with a concrete 'remediation_hint', and a 'next_step'.
    Call get_drc_report(project_name, verbose=True) for the full raw report.

    Requires a routed board — call route_board and wait for routing_state
    "complete" first.
    """
    from orchestrator import stages

    pdir = _project_dir(project_name)
    if not pdir.exists():
        return fail(
            f"Project '{project_name}' not found.",
            remediation=[option("List existing projects", "list_projects", {})],
        )
    if not (pdir / f"{project_name}_routed.json").exists():
        return fail(
            "No routed board found.",
            remediation=[option("Route the board first", "route_board",
                                {"project_name": project_name})],
        )

    # DRC exports the board to run kicad-cli on it, so it needs the SAME
    # footprint geometry placement/routing used. Without this the pad geometry
    # falls back to a different tier and DRC invents violations that are not on
    # the board: on a cold server (no placement/route in this process yet) a
    # clean 4-layer board reported 24 shorts + 15 clearance + 8 connectivity
    # errors that all vanished once the lookup was activated. A warm server hid
    # the bug because optimize_placement/route_board had already set it.
    _activate_project_lookup(project_name)

    try:
        report = stages.run_drc(pdir, project_name, _get_config())
    except Exception as exc:
        return fail(
            f"DRC failed: {exc}",
            remediation=[option(
                "The routed board may be incomplete or corrupt — re-route it, "
                "then run DRC again", "route_board",
                {"project_name": project_name, "effort": "best"})],
        )

    if report.get("error"):
        return fail(report["error"], remediation=[option(
            "Route the board first", "route_board", {"project_name": project_name})])

    from validators.drc_report import summarize_drc
    summary = summarize_drc(report)

    if report.get("passed"):
        step = next_step("export_outputs", {"project_name": project_name},
                         "DRC passed — generate manufacturing outputs.")
    else:
        first = summary["failing_rules"][0] if summary["failing_rules"] else {}
        step = next_step(
            "route_board", {"project_name": project_name, "effort": "best"},
            f"DRC failed ({summary['error_count']} errors). Each failing rule "
            "has a remediation_hint; most routing violations clear with a "
            "best-effort re-route. "
            + (first.get("remediation_hint", "") if first else ""),
        )
    return ok(summary, step)


@mcp.tool()
def check_circuit(
    project_name: str,
    supply_voltage: str | None = None,
    models: dict | None = None,
    rails: dict | None = None,
) -> dict:
    """Check whether a circuit is electrically sound, before it is laid out (no LLM).

    Solves the DC operating point of the netlist and reports what is actually
    wrong: resistors over their package power rating, LEDs over their forward
    current, parts fed a supply outside their datasheet range, shorted pins,
    and isolated sections with no path to ground. Currents come from a real
    nodal solve, so shared, series, and divider topologies are handled
    correctly rather than guessed from what sits next to what.

    Run this after finalize_circuit and before optimize_placement — it checks
    the schematic, while run_drc checks the manufactured board.

    This is NOT a simulation and it does not model ICs, transistors, or
    regulators: the suite has no device models for them. Those parts terminate
    analysis, and every net whose behavior depends on one is listed in
    `not_checked` rather than being silently assumed. Read `verdict`:

      issues_found            — real defects; each has a concrete fix
      not_enough_information  — some of the circuit could not be evaluated
      no_issues_found         — no check failed; NOT proof the circuit works

    Args:
        project_name: Project to check. Reads <project>_netlist.json.
        supply_voltage: Input rail, e.g. "5V". Defaults to power.voltage from
            the project's requirements. Required if that is absent or names
            more than one voltage.
        models: Per-part data the agent knows, e.g.
            {"U1": {"vcc_min": "3.0V", "vcc_max": "3.6V"}}. Supported keys:
            vcc_min, vcc_max, vf, if, voltage_rating.
        rails: Explicit net voltages when the input rail is ambiguous, e.g.
            {"VCC": "5V"}. Keyed by net name.
    """
    sys.path.insert(0, str(_repo_root / "validators"))
    from validators.circuit_report import build_report
    from validators.engineering_constants import parse_supply_voltage

    netlist = _read_project_json(project_name, "_netlist.json")
    if netlist is None:
        pdir = _project_dir(project_name)
        if not pdir.exists():
            return fail(
                f"Project '{project_name}' not found.",
                remediation=[option("List existing projects", "list_projects", {})],
            )
        return fail(
            f"No netlist found for '{project_name}'.",
            remediation=[
                option("Finish the circuit first", "finalize_circuit",
                       {"project_name": project_name}),
                option("Check what stage this project is at", "get_project_status",
                       {"project_name": project_name}),
            ],
        )

    # Supply: explicit argument wins, else the requirements file. Either way a
    # value naming more than one voltage ("adjustable (1.23V-37V output)") is
    # refused rather than silently read as its first number, which would seed
    # the solve with a wrong rail and produce confident wrong currents.
    source = "argument"
    volts = parse_supply_voltage(supply_voltage) if supply_voltage else None
    if volts is None and supply_voltage:
        return fail(
            f"supply_voltage {supply_voltage!r} does not name a single voltage.",
            remediation=[
                option("Pass one scalar voltage", "check_circuit",
                       {"project_name": project_name, "supply_voltage": "5V"}),
            ],
        )
    if volts is None:
        reqs = _read_project_json(project_name, "_requirements.json") or {}
        raw = (reqs.get("power") or {}).get("voltage")
        volts = parse_supply_voltage(raw) if raw else None
        source = "requirements_file"

    report = build_report(
        netlist.get("elements") or [],
        supply_voltage=volts,
        supply_source=source,
        models=models,
        rails=rails,
    )

    if report["verdict"] == "issues_found" and report["counts"]["errors"]:
        return fail(
            report["headline"],
            remediation=[
                option(i["detail"], "check_circuit", {"project_name": project_name})
                for i in report["issues"][:5]
                if i["severity"] == "error"
            ],
            data=report,
        )

    step = next_step(
        "optimize_placement",
        {"project_name": project_name},
        "Circuit checks are clean enough to lay out. Anything in not_checked "
        "was not verified either way.",
    )
    return ok(report, step)


@mcp.tool()
def export_outputs(project_name: str) -> dict:
    """Generate manufacturing outputs from the routed board (no LLM).

    Produces Gerbers, Excellon drill, BOM CSV, pick-and-place (CPL), populated
    STEP model, and a ZIP package — all written into the project's output/ dir.

    Requires a routed board that passes DRC. **Refuses to export otherwise** —
    there is no override, because manufacturing files are only ever wanted for a
    board that can actually be built. A refusal names the blocking violations
    and carries a 'reroute_cleanable' flag: True means every error is
    clearance/short/mask geometry that route_board(keep_existing=True) clears on
    its own, False means at least one is structural (annular ring, hole spacing,
    edge clearance, trace width) and must be fixed at the source first. See
    get_workflow_guide()['reference']['export_refusal'].

    To inspect an imperfect board instead of manufacturing it, use export_kicad
    (writes the .kicad_pcb) or get_board_image.

    Returns:
        {success, output_dir, files: [...], package: <zip path>}  or  {success: False, error}
    """
    from orchestrator import stages

    pdir = _project_dir(project_name)
    if not pdir.exists():
        return fail(
            f"Project '{project_name}' not found.",
            remediation=[option("List existing projects", "list_projects", {})],
        )
    if not (pdir / f"{project_name}_routed.json").exists():
        return fail(
            "No routed board found.",
            remediation=[option("Route the board first", "route_board",
                                {"project_name": project_name})],
        )

    # Activate project-local custom footprints so Gerber export uses the same
    # footprint geometry as placement/routing.
    _activate_project_lookup(project_name)

    # Connectivity gate. A board with open nets is electrically incomplete: its
    # gerbers describe a board that physically cannot work, so they are never
    # emitted. (The agent had reached for an allow_drc_errors override to ship a
    # 95.8%-routed board with 2 open nets; that override is now removed entirely —
    # there is no board state where forcing manufacturing files is correct.)
    # Use the router's reconciled unrouted_nets — the honest field that matches
    # the authoritative kicad-cli connectivity (2 on v18r8). NOT a fresh
    # incomplete_net_ids() call: that internal union-find false-positives on
    # plane_layers=0 / surface-pour boards (43 vs the real 2) and would block
    # every board.
    routed_data = _read_project_json(project_name, "_routed.json")
    open_nets = list((routed_data or {}).get("routing", {}).get("unrouted_nets") or [])
    if open_nets:
        shown = ", ".join(open_nets[:6]) + ("…" if len(open_nets) > 6 else "")
        return fail(
            f"Refusing to export: {len(open_nets)} net(s) are not fully connected "
            f"({shown}). A board with missing connections is not manufacturable; "
            "manufacturing files are only produced for a buildable board. Finish "
            "the open nets first.",
            data={"unrouted_nets": open_nets},
            remediation=[
                option("Incrementally finish the open nets (protects the routed "
                       "majority)", "route_board",
                       {"project_name": project_name, "keep_existing": True}),
                option("On 4-layer: free an inner signal layer for capacity, then "
                       "re-route", "optimize_placement",
                       {"project_name": project_name, "plane_layers": 1}),
            ])

    # DRC gate — FAIL CLOSED. Manufacturing files must never leave on a board we
    # cannot certify. Two refusal cases:
    #   (1) authoritative DRC found errors  -> board is not fabricable
    #   (2) DRC could not run authoritatively (kicad-cli missing / export failed)
    #       -> we CANNOT certify the board, so we must not ship it. The old gate
    #          failed OPEN here (exception -> drc=None -> export proceeded), which
    #          is exactly how a 7-error board shipped: the internal validator said
    #          "clean" because kicad-cli never ran.
    try:
        drc = stages.run_drc(pdir, project_name, _get_config())
    except Exception as exc:
        drc = {"_run_error": str(exc)}

    if not drc.get("authoritative"):
        why = drc.get("_run_error") or (
            "kicad-cli (the authoritative DRC engine) is not available, so "
            "geometry shorts/clearance/thermal could not be checked")
        return fail(
            "Refusing to export: DRC could not be verified authoritatively "
            f"({why}). The internal heuristic check is NOT a manufacturability "
            "guarantee — it misses through-hole-pad shorts, mask bridges and "
            "starved thermals. Install/locate kicad-cli (set PCB_KICAD_CLI) so "
            "the board can be certified, then export.",
            data={"drc_engine": drc.get("drc_engine", "internal"),
                  "authoritative": False},
            remediation=[
                option("Review the DRC report", "get_drc_report",
                       {"project_name": project_name, "verbose": True}),
            ])

    if not drc.get("passed", True):
        n = drc.get("statistics", {}).get("errors", 0)
        failing_checks = [c for c in drc.get("checks", []) if not c.get("passed")]
        failing = sorted({c["rule"] for c in failing_checks})
        # Surface the actual violations (rule / message / location) in the
        # refusal so the agent can act without a get_drc_report round-trip — and
        # so it stops improvising manual re-pours to see what/where.
        top_violations = [
            {"rule": v.get("rule"), "message": v.get("message"),
             "location": v.get("location")}
            for c in failing_checks for v in c.get("violations", [])
            if v.get("severity") == "error"
        ][:6]
        # If EVERY failing rule is one that rip-up+re-route fixes on its own,
        # steer straight to the built-in auto-clean instead of leaving the agent
        # to invent a fix (the "re-pour cleaned the clearance" improvisation).
        from validators.kicad_drc import reroute_cleanable_rules
        auto = bool(failing) and set(failing) <= reroute_cleanable_rules()
        data = {"drc_errors": n, "failing_rules": failing,
                "top_violations": top_violations,
                "reroute_cleanable": auto}
        if auto:
            return fail(
                f"Refusing to export: {n} DRC error(s) ({', '.join(failing)}) — "
                "but all are clearance/short/mask geometry that "
                "route_board(keep_existing=True) rips up and re-routes clear "
                "automatically (it runs the same kicad-cli DRC internally). Do "
                "NOT hand-edit or re-pour — just re-route, then export.",
                data=data,
                remediation=[
                    option("Auto-clean the geometry (protects all good wiring), "
                           "then export", "route_board",
                           {"project_name": project_name, "keep_existing": True}),
                    option("Review the full DRC report", "get_drc_report",
                           {"project_name": project_name, "verbose": True}),
                ])
        return fail(
            f"Refusing to export: the board has {n} DRC error(s) "
            f"({', '.join(failing)}). Manufacturing files from a board with "
            "shorts, disconnected nets, or plane-clearance errors are not "
            "fabricable — do not ship or commit them. Some failing rules are "
            "structural (annular ring, hole spacing, edge clearance, trace "
            "width) and are NOT fixed by re-routing — see top_violations for "
            "each rule/location and fix at the source (placement, via/board "
            "rules) before exporting.",
            data=data,
            remediation=[
                option("Finish residual connectivity / auto-clean the "
                       "reroute-fixable ones", "route_board",
                       {"project_name": project_name, "keep_existing": True}),
                option("Review the full DRC report", "get_drc_report",
                       {"project_name": project_name, "verbose": True}),
            ])

    try:
        result = stages.run_export(pdir, project_name, _get_config())
    except Exception as exc:
        return fail(
            f"Export failed: {exc}",
            remediation=[option(
                "The routed board may be incomplete or corrupt — re-route it, "
                "then export again", "route_board",
                {"project_name": project_name, "effort": "best"})],
        )

    if not result.get("success"):
        return fail(result.get("error", "Export failed."), data=result)
    return ok(result, next_step(
        "get_fab_quote", {"project_name": project_name},
        "Done — the ZIP package is ready for manufacturer upload. Optionally "
        "get a fab cost estimate + part availability with get_fab_quote, and "
        "call get_board_image for a final visual check."))


@mcp.tool()
def get_fab_quote(project_name: str, quantity: int = 5,
                  live: bool = True) -> dict:
    """Estimate fabrication cost and check BOM part availability (no LLM).

    Combines a deterministic board price estimate (from board size, layer
    count, and quantity — JLCPCB published-pricing ballpark, clearly marked
    estimate=True) with per-line part status: each BOM line's LCSC/MPN part
    number (auto-filled for common jellybean parts), and — with live=True —
    current LCSC stock and USD unit price per unique part id, plus an MPN
    cross-check that flags lines worth a human look before ordering.

    With live=True, the manufacturer part number (MPN) + manufacturer fetched
    from LCSC are written back into the BOM for any line that has an LCSC id
    but no MPN yet — turning an LCSC-only part into a distributor-agnostic
    identity you can search on Digikey/Mouser. Existing MPNs are never
    overwritten. Re-run export_outputs afterward to refresh the BOM CSV's MPN
    column.

    Works any time after a netlist exists; the board price needs a placement
    or routed board for dimensions. Lines listed in 'unresolved' have no part
    number yet — record each with set_part_number (prefer the manufacturer
    part number mpn=; never guess an LCSC 'C####' id), then re-run this tool
    to verify stock/price.

    Args:
        project_name: The project slug/name.
        quantity: Number of boards to price (default 5).
        live: Also query LCSC for stock/price (rate-limited; falls back to
              catalog-only silently when offline).
    """
    pdir = _project_dir(project_name)
    if not pdir.exists():
        return fail(
            f"Project '{project_name}' not found.",
            remediation=[option("List existing projects", "list_projects", {})],
        )
    from orchestrator.quoting import quote_project
    try:
        result = quote_project(pdir, project_name, qty=quantity, live=live)
    except Exception as exc:
        return fail(f"Quote failed: {exc}")
    if not result.get("success"):
        return fail(
            result.get("error", "Quote failed."),
            remediation=[option(
                "Build or import the circuit first", "get_workflow_guide", {})],
        )
    nxt = None
    if result.get("unresolved"):
        first = str(result["unresolved"][0]).split(",")[0].strip()
        nxt = next_step(
            "set_part_number",
            {"project_name": project_name, "designator": first,
             "mpn": "<manufacturer part number>"},
            f"{len(result['unresolved'])} BOM line(s) have no part number. "
            "Record the MANUFACTURER part number (mpn=) — it is the reliable, "
            "distributor-agnostic id you can derive from the part/datasheet "
            "(e.g. IRLML2502 -> IRLML2502TRPBF) and search on Digikey/Mouser/"
            "LCSC. Do NOT guess an LCSC 'C####' id from memory — a wrong guess "
            "silently points at a different part; set lcsc= only if you have "
            "actually looked it up. Then re-run get_fab_quote to verify.")
    elif (pdir / "output" / f"{project_name}_bom.csv").exists():
        # Part numbers are all resolved but a manufacturing package was already
        # exported — its BOM CSV predates these ids and is now stale. Steer to a
        # re-export so the file the user uploads carries the part numbers.
        nxt = next_step(
            "export_outputs", {"project_name": project_name},
            "All BOM lines have part numbers. The exported BOM CSV predates "
            "them — re-run export_outputs to refresh the manufacturing package "
            "(CSV/ZIP) with the finalized part numbers before ordering.")
    return ok(result, nxt)


@mcp.tool()
def set_part_number(project_name: str, designator: str,
                    lcsc: str | None = None, mpn: str | None = None,
                    manufacturer: str | None = None) -> dict:
    """Record an orderable part number on one BOM line (no LLM).

    The write half of the get_fab_quote loop: when a line shows up in the
    quote's 'unresolved' list (or carries a wrong id), record the part here.

    PREFER the manufacturer part number (mpn). It is the reliable,
    distributor-agnostic identity you can derive from the part or its
    datasheet (IRLML2502 -> "IRLML2502TRPBF", 1N4007 -> "1N4007") and search
    on Digikey, Mouser, or LCSC. The LCSC 'C####' id is an opaque catalog
    number: DO NOT guess one from memory — a wrong guess (e.g. C2146 is an
    S8050 BJT, not the MOSFET you meant) silently records a completely
    different part. Set lcsc= only when you have actually looked it up (a live
    get_fab_quote back-fills the MPN from a correct LCSC id for you).

    The value lands in <project>_bom.json, flows into the exported BOM CSV's
    'MPN' / 'LCSC Part #' columns, and is cached by type:value:package so the
    same part resolves automatically in future projects. Grouped lines match by
    membership ("D2" updates the "D1, D2, D3, D4" line); provided fields
    overwrite, omitted fields are left unchanged.

    Args:
        project_name: The project slug/name.
        designator: Any designator on the target BOM line (e.g. "R1", "CN1").
        mpn: Manufacturer part number, e.g. "1N4007" (preferred).
        manufacturer: Manufacturer name, e.g. "onsemi".
        lcsc: LCSC part id, e.g. "C2286" (validated: C + digits). Only if
              looked up — never recalled from memory.

    Example: set_part_number("my_board", "Q1", mpn="IRLML2502TRPBF",
                             manufacturer="Infineon")
    """
    pdir = _project_dir(project_name)
    if not pdir.exists():
        return fail(
            f"Project '{project_name}' not found.",
            remediation=[option("List existing projects", "list_projects", {})],
        )
    from orchestrator.quoting import set_part_number as _set
    try:
        result = _set(pdir, project_name, designator,
                      lcsc=lcsc, mpn=mpn, manufacturer=manufacturer)
    except Exception as exc:
        return fail(f"set_part_number failed: {exc}")
    if not result.get("success"):
        remediation = []
        if result.get("known_designators"):
            remediation.append(option(
                "Check the BOM's designator groups in the quote",
                "get_fab_quote", {"project_name": project_name, "live": False}))
        return fail(result.get("error", "set_part_number failed."),
                    data={k: v for k, v in result.items()
                          if k == "known_designators"},
                    remediation=remediation or None)
    return ok(result, next_step(
        "get_fab_quote", {"project_name": project_name},
        "Part recorded. Re-run get_fab_quote (live) to verify LCSC stock/price "
        "and the MPN cross-check before ordering."))


# ---------------------------------------------------------------------------
# Component pre-positioning (pin edge connectors before auto-placement)
# ---------------------------------------------------------------------------

@mcp.tool()
def set_component_positions(
    project_name: str,
    positions: list[dict],
    board_width_mm: float | None = None,
    board_height_mm: float | None = None,
    approved: bool = False,
) -> dict:
    """Pre-position components with placement_source='user' so optimize_placement
    treats them as fixed anchors and only moves everything else.

    Use this BEFORE optimize_placement to lock edge connectors (FFC ZIF,
    terminal blocks, headers, debug ports) at their spec-defined board positions.
    The SA optimizer skips components with placement_source='user', so they stay
    exactly where you put them.

    If no placement file exists yet for the project, a full grid placement is
    generated automatically from the netlist and then the specified components
    are pinned.  Call import_kicad_netlist first to ensure a netlist is available.

    Args:
        project_name:    Project slug.
        positions:       List of component position dicts, each with:
                           "designator"   (str, required) — e.g. "J1", "U3"
                           "x_mm"         (float, required) — X from board origin
                           "y_mm"         (float, required) — Y from board origin
                           "rotation_deg" (int, optional, default 0)
                           "layer"        (str, optional, "top" or "bottom", default "top")
        board_width_mm:  Board width (mm). Required when no placement exists yet.
        board_height_mm: Board height (mm). Required when no placement exists yet.
        approved:        Growing an already-placed board is enforced to need
                         the user's explicit approval (enclosure/mating fit) —
                         pass approved=True ONLY after they agree.

    Returns:
        {success: True, pinned_count: int, total_components: int,
         placement_path: str, notes: [str]}
        or {success: False, error: str}
    """
    if not approved:
        reason = _requires_approval(project_name, None, None,
                                    board_width_mm, board_height_mm)
        if reason:
            return fail(
                f"This change requires explicit user approval: {reason}. "
                "ASK THE USER first — do not decide this on your own. Only "
                "after they agree, re-run with approved=True.",
                remediation=[option(
                    "Re-run once the user has approved the change",
                    "set_component_positions",
                    {"project_name": project_name, "positions": positions,
                     "board_width_mm": board_width_mm,
                     "board_height_mm": board_height_mm, "approved": True})],
            )

    pdir = _project_dir(project_name)
    draft_path = pdir / f"{project_name}_circuit_draft.json"
    netlist_path = pdir / f"{project_name}_netlist.json"
    placement_path = pdir / f"{project_name}_placement.json"

    if not pdir.exists():
        return fail(
            f"Project '{project_name}' not found.",
            remediation=[
                option("Build a circuit from scratch", "create_circuit",
                       {"project_name": project_name,
                        "description": "<circuit description>",
                        "board_width_mm": 50, "board_height_mm": 40}),
                option("Import a KiCad netlist", "import_kicad_netlist",
                       {"project_name": project_name, "file_path": "<path to .net>"}),
            ],
        )

    if not netlist_path.exists():
        # A builder draft that has not been compiled yet has no netlist. Steer to
        # finalize_circuit instead of the misleading "import a netlist" — the
        # agent already built the circuit, it just hasn't compiled it.
        if draft_path.exists():
            return fail(
                f"Circuit '{project_name}' is not compiled yet (no netlist). "
                "Compile and validate the draft, then set positions.",
                remediation=[option("Compile the draft into a netlist",
                                    "finalize_circuit",
                                    {"project_name": project_name})],
            )
        return fail(
            f"No netlist for '{project_name}'.",
            remediation=[
                option("Import a KiCad netlist", "import_kicad_netlist",
                       {"project_name": project_name, "file_path": "<path to .net>"}),
                option("Build a circuit from scratch", "create_circuit",
                       {"project_name": project_name,
                        "description": "<circuit description>"}),
            ],
        )

    # Load or generate placement
    if placement_path.exists():
        placement = json.loads(placement_path.read_text())
    else:
        # Need board dimensions to generate a seed placement
        if board_width_mm is None or board_height_mm is None:
            return fail(
                "No placement exists yet and board_width_mm/board_height_mm were "
                "not provided, so no seed placement can be generated.",
                remediation=[
                    option("Provide board dimensions so a seed placement is generated",
                           "set_component_positions",
                           {"project_name": project_name, "positions": positions,
                            "board_width_mm": "<width>", "board_height_mm": "<height>"}),
                    option("Or run optimize_placement first, then pin positions",
                           "optimize_placement",
                           {"project_name": project_name,
                            "board_width_mm": "<width>", "board_height_mm": "<height>"}),
                ],
            )
        from optimizers.initial_placement import generate_grid_placement
        netlist = json.loads(netlist_path.read_text())
        _activate_project_lookup(project_name)
        placement = generate_grid_placement(netlist, board_width_mm,
                                            board_height_mm, project_name)
        if placement is None:
            return fail(
                "Could not generate a seed placement — check the netlist has "
                "components with resolvable footprints.",
                remediation=[option("Verify every footprint resolves",
                                    "verify_footprints",
                                    {"project_name": project_name})],
            )

    # Build a lookup from designator → placement item index
    des_index: dict[str, int] = {
        item["designator"]: i
        for i, item in enumerate(placement.get("placements", []))
    }
    known = sorted(des_index)

    pinned: list[str] = []
    unpinned: list[dict] = []   # every requested entry that did NOT pin, with why
    for pos in positions:
        des = pos.get("designator", "")
        if not des:
            unpinned.append({"designator": None, "reason": "entry has no 'designator'"})
            continue
        x = pos.get("x_mm")
        y = pos.get("y_mm")
        if x is None or y is None:
            unpinned.append({"designator": des, "reason": "missing x_mm or y_mm"})
            continue
        if des not in des_index:
            unpinned.append({"designator": des,
                             "reason": "not a component in this circuit"})
            continue
        idx = des_index[des]
        placement["placements"][idx]["x_mm"] = float(x)
        placement["placements"][idx]["y_mm"] = float(y)
        placement["placements"][idx]["rotation_deg"] = int(pos.get("rotation_deg", 0))
        placement["placements"][idx]["layer"] = pos.get("layer", "top")
        placement["placements"][idx]["placement_source"] = "user"
        pinned.append(des)

    # No-op guard: a request that pins NOTHING is a failure, not a hidden success.
    # This is the "silent no-op" that cost an agent 30+ tool calls — the call
    # used to return success:True with pinned_count:0 and a next_step that said
    # "pinned components will stay fixed", with the real reason buried in 'notes'.
    # Write nothing on this path so a failed call never mutates placement state.
    if not pinned:
        detail = "; ".join(f"{u['designator']}: {u['reason']}"
                           for u in unpinned) or "no positions given"
        return fail(
            f"None of the {len(positions)} requested component(s) were pinned "
            f"({detail}). Valid designators: {', '.join(known) or '(none)'}.",
            remediation=[
                option("Inspect the circuit to get the real designators",
                       "list_circuit", {"project_name": project_name}),
                option("Retry with a valid designator and explicit coordinates",
                       "set_component_positions",
                       {"project_name": project_name,
                        "positions": [{"designator": (known[0] if known else "J1"),
                                       "x_mm": "<x>", "y_mm": "<y>"}]}),
            ],
            data={"pinned_count": 0, "pinned_designators": [],
                  "unpinned": unpinned, "known_designators": known},
        )

    placement_path.write_text(json.dumps(placement, indent=2))

    # Persist to the DURABLE pin store (placement_pins.json) too — the same
    # store place_component writes and that run_placement re-applies on every
    # optimize. Without this, these pins lived only in placement.json's
    # placement_source flags and were silently lost whenever the placement was
    # regenerated, so a later optimize_placement scattered them. Writing both
    # stores makes batch pins as durable as single place_component pins.
    from orchestrator.stages import load_placement_pins, _pins_path
    durable = load_placement_pins(pdir, project_name)
    for des in pinned:
        it = placement["placements"][des_index[des]]
        durable[des] = {"x_mm": it["x_mm"], "y_mm": it["y_mm"],
                        "rotation_deg": it.get("rotation_deg", 0),
                        "layer": it.get("layer", "top")}
    _pins_path(pdir, project_name).write_text(json.dumps(durable, indent=2))

    data = {
        "pinned_count": len(pinned),
        "pinned_designators": pinned,
        "total_components": len(placement.get("placements", [])),
        "placement_path": str(placement_path),
    }
    why = "Pinned components stay fixed; everything else is placed around them."
    if unpinned:
        # Partial success must be LOUD: surface the failures at top level, not
        # buried in notes, so the agent fixes the typo'd designators.
        data["unpinned"] = unpinned
        names = ", ".join(str(u["designator"]) for u in unpinned)
        data["warning"] = (
            f"{len(unpinned)} requested component(s) were NOT pinned: {names}. "
            f"Valid designators: {', '.join(known)}.")
        why = "WARNING: " + data["warning"] + " " + why
    return ok(data, next_step("optimize_placement",
                              {"project_name": project_name}, why))


# ---------------------------------------------------------------------------
# Footprint coverage assessment and custom footprint registration
# ---------------------------------------------------------------------------

@mcp.tool()
def check_footprint_coverage(
    components: list[dict],
    project_name: str | None = None,
) -> dict:
    """Check footprint library coverage for a BOM before launching placement.

    Run this BEFORE design_pcb / optimize_placement to identify which components
    need custom footprints.  Components that miss all resolution tiers will cause
    placement failures or silent perimeter-approximation fallbacks that produce
    wrong pad geometry.

    Resolution tiers checked (in order):
      0. project-local custom footprints (if project_name given and has any)
      1. system KiCad library (~50 K authoritative footprints)
      2. IPC-7351B parametric (QFN, BGA, SOP, TSSOP, DFN, …)
      3. local component cache (prior EasyEDA / LLM lookups)
      4. built-in approximations

    Args:
        components:   List of component dicts, each with "reference" (e.g. "U1"),
                      "package" (e.g. "QFN-32"), "pin_count", and optionally
                      "value".
        project_name: Optional project slug.  When given, project-local custom
                      footprints registered via register_custom_footprint are
                      checked as tier 0.

    Returns:
        {"coverage": {total, resolved, custom_needed},
         "resolved": [{reference, package, pin_count, tier}, ...],
         "custom_needed": [{reference, package, pin_count, value, notes}, ...]}
    """
    from optimizers.pad_geometry import check_footprint_tier

    custom = _get_project_custom_index(project_name) if project_name else None

    resolved = []
    custom_needed = []

    for comp in components:
        ref = comp.get("reference", "?")
        pkg = comp.get("package", "")
        pins = int(comp.get("pin_count", 0))
        val = comp.get("value", "")

        if not pkg:
            custom_needed.append({
                "reference": ref,
                "package": "",
                "pin_count": pins,
                "value": val,
                "notes": "No package specified — cannot resolve footprint.",
            })
            continue

        tier = check_footprint_tier(pkg, pins, custom_index=custom)

        if tier is not None:
            resolved.append({
                "reference": ref,
                "package": pkg,
                "pin_count": pins,
                "tier": tier,
            })
        else:
            custom_needed.append({
                "reference": ref,
                "package": pkg,
                "pin_count": pins,
                "value": val,
                "notes": (
                    f"Package '{pkg}' with {pins} pins not found in any tier "
                    "(KiCad library, IPC-7351B, cache, built-ins). "
                    "Create a .kicad_mod and call register_custom_footprint."
                ),
            })

    out = {
        "coverage": {
            "total": len(components),
            "resolved": len(resolved),
            "custom_needed": len(custom_needed),
        },
        "resolved": resolved,
        "custom_needed": custom_needed,
    }
    if custom_needed:
        out["next_step"] = next_step(
            "register_custom_footprint",
            {"project_name": project_name or "<project>",
             "package_name": custom_needed[0]["package"],
             "kicad_mod_content": "<.kicad_mod S-expression>"},
            f"{len(custom_needed)} component(s) have no library footprint — "
            "create and register a .kicad_mod for each, then re-check.",
        )
    else:
        out["next_step"] = next_step(
            "optimize_placement", {"project_name": project_name or "<project>"},
            "All footprints resolve — placement can proceed.",
        )
    return out


@mcp.tool()
def register_custom_footprint(
    project_name: str,
    package_name: str,
    kicad_mod_content: str,
) -> dict:
    """Register a custom .kicad_mod footprint for a project.

    Writes the footprint to the project's ``custom-footprints.pretty/``
    directory, where it is searched BEFORE the system KiCad library (tier 0).
    After registration, check_footprint_coverage, optimize_placement, and
    export_outputs will find it automatically.

    The project directory is created if it does not yet exist, so footprints
    can be pre-registered before the full pipeline runs.

    Args:
        project_name:      Project slug (lowercase letters, digits, underscores).
        package_name:      Package identifier matching what the netlist uses
                           (e.g. "QFN-48", "MY_CONNECTOR_4P").  Case-insensitive
                           during lookup.  The .kicad_mod filename is derived
                           from this (non-alphanumeric chars → underscores).
        kicad_mod_content: Full .kicad_mod file content in KiCad S-expression
                           format.  Must start with ``(footprint`` or
                           ``(module``.

    Returns:
        {success: True, path: str, package_name: str}
        or {success: False, error: str}
    """
    # Basic content sanity check
    stripped = kicad_mod_content.strip()
    if not (stripped.startswith("(footprint") or stripped.startswith("(module")):
        return fail(
            "kicad_mod_content must be a valid KiCad S-expression starting "
            "with '(footprint ...' or '(module ...'. Got: " + stripped[:60],
            remediation=[option(
                "Pass the full .kicad_mod file content (starts with '(footprint')",
                "register_custom_footprint",
                {"project_name": project_name, "package_name": package_name,
                 "kicad_mod_content": "(footprint \"NAME\" (layer F.Cu) "
                 "(pad \"1\" smd rect (at 0 0)(size 1 1)(layers F.Cu)) ...)"},
            )],
        )

    # Build a filesystem-safe filename from the package name
    safe_name = re.sub(r"[^a-zA-Z0-9_\-\.]", "_", package_name).strip("_")
    if not safe_name:
        return {"success": False, "error": f"Cannot derive a safe filename from package_name '{package_name}'."}

    bad = _reject_bad_project_name(project_name)
    if bad:
        return bad

    # Ensure the project custom-footprints.pretty directory exists
    custom_dir = _project_dir(project_name) / "custom-footprints.pretty"
    try:
        custom_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return {"success": False, "error": f"Could not create custom footprint directory: {exc}"}

    # Write the .kicad_mod file
    fp_path = custom_dir / f"{safe_name}.kicad_mod"
    try:
        fp_path.write_text(kicad_mod_content)
    except OSError as exc:
        return {"success": False, "error": f"Could not write footprint file: {exc}"}

    # Invalidate (or build) the cached index for this project so the new file
    # is visible on the next lookup without a server restart.
    with _CUSTOM_LOCK:
        if project_name in _CUSTOM_INDICES:
            _CUSTOM_INDICES[project_name].invalidate()
        else:
            from exporters.kicad_mod_parser import KiCadLibraryIndex
            _CUSTOM_INDICES[project_name] = KiCadLibraryIndex(custom_dir)

    return ok({
        "path": str(fp_path),
        "package_name": package_name,
        "message": (
            f"Registered '{package_name}' as tier-0 custom footprint for project "
            f"'{project_name}'. It will be found by check_footprint_coverage and "
            "optimize_placement immediately."
        ),
    }, next_step(
        "verify_footprints", {"project_name": project_name},
        "Confirm the registered footprint clears the placement gate.",
    ))


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():  # pragma: no cover - server entry point: starts the stdio MCP loop and installs the Freerouting JVM-cleanup signal hooks
    """Run the MCP server (stdio transport)."""
    # Pipeline modules log via logging — route to stderr so the stdio
    # JSON-RPC stream on stdout is never corrupted.
    logging.basicConfig(level=logging.INFO, format="%(message)s", stream=sys.stderr)
    # Ensure CWD exists — Hermes worker scratch dirs can be deleted
    # out from under us, and pathlib.Path.cwd() will raise
    # FileNotFoundError if the process CWD is gone.
    try:
        os.getcwd()
    except FileNotFoundError:
        # Private per-process dir, not world-writable /tmp, so relative-path
        # resolution can't be influenced by other local users.
        os.chdir(_get_projects_dir())

    # Initialise footprint lookup globals so the KiCad library tier is active
    # for all placement/export calls in this server process.
    _init_lookup()

    # Install atexit + SIGTERM/SIGINT hooks (main thread) so a graceful shutdown
    # of the server kills any in-flight Freerouting JVM instead of orphaning it.
    # Routing runs on a worker thread where signal handlers can't be set, so it
    # must be installed here.
    try:
        from optimizers.freerouter import install_process_cleanup
        install_process_cleanup()
    except Exception:  # noqa: BLE001 — cleanup hooks are best-effort
        pass

    mcp.run()


if __name__ == "__main__":
    main()
