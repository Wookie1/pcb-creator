"""export_outputs must refuse to emit manufacturing files for a board with DRC
errors (the agent kept shipping boards with shorts / disconnected nets). There is
NO override — manufacturing files are only ever wanted for a buildable board."""
import inspect
import json
import mcp_server
from orchestrator import stages


def _project(tmp_path, monkeypatch):
    monkeypatch.setenv("PCB_PROJECTS_DIR", str(tmp_path))
    proj = "exp"
    pdir = tmp_path / proj
    pdir.mkdir()
    (pdir / f"{proj}_routed.json").write_text("{}")
    (pdir / f"{proj}_netlist.json").write_text("{}")
    monkeypatch.setattr(mcp_server, "_activate_project_lookup", lambda p: None)
    return proj


_FAIL_DRC = {"passed": False, "authoritative": True, "drc_engine": "kicad-cli",
             "statistics": {"errors": 3},
             "checks": [{"rule": "connectivity", "passed": False},
                        {"rule": "inner_plane_antipad", "passed": False}]}
_OK_DRC = {"passed": True, "authoritative": True, "drc_engine": "kicad-cli",
           "statistics": {"errors": 0}, "checks": []}
# kicad-cli unavailable: the internal heuristic "passed" but is NOT authoritative.
_UNVERIFIED_DRC = {"passed": True, "authoritative": False, "drc_engine": "internal",
                   "statistics": {"errors": 0}, "checks": []}


def test_export_blocked_on_drc_errors(tmp_path, monkeypatch):
    proj = _project(tmp_path, monkeypatch)
    monkeypatch.setattr(stages, "run_drc", lambda *a, **k: _FAIL_DRC)
    exported = {"v": False}
    monkeypatch.setattr(stages, "run_export",
                        lambda *a, **k: exported.update(v=True) or {"success": True})
    r = mcp_server.export_outputs(proj)
    assert r["success"] is False
    assert "Refusing to export" in r["error"]
    assert exported["v"] is False                     # export never ran
    assert r["drc_errors"] == 3                        # data merged at top level
    assert "connectivity" in r["failing_rules"]


def test_no_override_parameter_exists(tmp_path, monkeypatch):
    """There must be NO allow_drc_errors escape hatch — the agent abused it to
    ship a flawed board, and no board state justifies forcing the export."""
    assert "allow_drc_errors" not in inspect.signature(
        mcp_server.export_outputs).parameters


def test_export_proceeds_when_clean(tmp_path, monkeypatch):
    proj = _project(tmp_path, monkeypatch)
    monkeypatch.setattr(stages, "run_drc", lambda *a, **k: _OK_DRC)
    monkeypatch.setattr(stages, "run_export",
                        lambda *a, **k: {"success": True, "files": ["a.gbr"]})
    r = mcp_server.export_outputs(proj)
    assert r["success"] is True


def test_export_blocked_when_drc_unverifiable(tmp_path, monkeypatch):
    """Fail CLOSED: a non-authoritative 'pass' (kicad-cli unavailable) must NOT
    ship — this is the hole that let a 7-error board out the door."""
    proj = _project(tmp_path, monkeypatch)
    monkeypatch.setattr(stages, "run_drc", lambda *a, **k: _UNVERIFIED_DRC)
    exported = {"v": False}
    monkeypatch.setattr(stages, "run_export",
                        lambda *a, **k: exported.update(v=True) or {"success": True})
    r = mcp_server.export_outputs(proj)
    assert r["success"] is False
    assert "could not be verified" in r["error"]
    assert exported["v"] is False                     # export never ran
    assert r["authoritative"] is False


def test_open_nets_block_export(tmp_path, monkeypatch):
    """A board with unrouted nets is electrically incomplete — its gerbers are
    never emitted (the 95.8%-routed-board case the agent tried to force)."""
    proj = _project(tmp_path, monkeypatch)
    # routed.json with 2 open nets
    (tmp_path / proj / f"{proj}_routed.json").write_text(json.dumps(
        {"routing": {"traces": [], "vias": [],
                     "unrouted_nets": ["net_a", "net_b"],
                     "statistics": {"completion_pct": 95.8}}}))
    exported = {"v": False}
    monkeypatch.setattr(stages, "run_export",
                        lambda *a, **k: exported.update(v=True) or {"success": True})
    r = mcp_server.export_outputs(proj)
    assert r["success"] is False
    assert exported["v"] is False                      # gerbers never generated
    assert "not fully connected" in r["error"]
    assert r["unrouted_nets"] == ["net_a", "net_b"]


def test_export_blocked_when_drc_raises(tmp_path, monkeypatch):
    """An exception in DRC must fail CLOSED, not silently let the export through
    (the old `except: drc=None` then `if drc and ...` skipped the gate)."""
    proj = _project(tmp_path, monkeypatch)
    def _boom(*a, **k):
        raise RuntimeError("kicad exploded")
    monkeypatch.setattr(stages, "run_drc", _boom)
    exported = {"v": False}
    monkeypatch.setattr(stages, "run_export",
                        lambda *a, **k: exported.update(v=True) or {"success": True})
    r = mcp_server.export_outputs(proj)
    assert r["success"] is False
    assert exported["v"] is False
    assert "could not be verified" in r["error"]


# --- refusal detail: violations surfaced + reroute-cleanable steering --------

_CLEANABLE_DRC = {
    "passed": False, "authoritative": True, "drc_engine": "kicad-cli",
    "statistics": {"errors": 2},
    "checks": [
        {"rule": "clearance_min", "passed": False, "violations": [
            {"rule": "clearance_min", "severity": "error",
             "message": "Clearance 0.10 < 0.20mm", "location": {"x_mm": 5.0, "y_mm": 6.0}}]},
        {"rule": "no_shorts", "passed": False, "violations": [
            {"rule": "no_shorts", "severity": "error",
             "message": "Net A shorts Net B", "location": {"x_mm": 1.0, "y_mm": 2.0}}]},
    ]}

_STRUCTURAL_DRC = {
    "passed": False, "authoritative": True, "drc_engine": "kicad-cli",
    "statistics": {"errors": 2},
    "checks": [
        {"rule": "annular_ring", "passed": False, "violations": [
            {"rule": "annular_ring", "severity": "error",
             "message": "Annular 0.10 < 0.13mm", "location": {"x_mm": 3.0, "y_mm": 4.0}}]},
        {"rule": "clearance_min", "passed": False, "violations": [
            {"rule": "clearance_min", "severity": "error",
             "message": "Clearance 0.10 < 0.20mm", "location": None}]},
    ]}


def test_reroute_cleanable_rules_are_derived():
    from validators.kicad_drc import reroute_cleanable_rules
    # Derived from route_cleanup._FIXABLE_BY_REROUTE ∩ unambiguous report rules.
    assert reroute_cleanable_rules() == {"no_shorts", "clearance_min",
                                         "solder_mask_bridge"}


def test_all_cleanable_steers_to_route_board(tmp_path, monkeypatch):
    proj = _project(tmp_path, monkeypatch)
    monkeypatch.setattr(stages, "run_drc", lambda *a, **k: _CLEANABLE_DRC)
    monkeypatch.setattr(stages, "run_export", lambda *a, **k: {"success": True})
    r = mcp_server.export_outputs(proj)
    assert r["success"] is False
    assert r["reroute_cleanable"] is True
    assert "route_board" in r["error"]
    # Top violations surfaced with rule/message/location — no round-trip needed.
    rules = {v["rule"] for v in r["top_violations"]}
    assert rules == {"clearance_min", "no_shorts"}
    assert any(v["location"] == {"x_mm": 5.0, "y_mm": 6.0} for v in r["top_violations"])
    # First remediation is the auto-clean re-route.
    assert r["remediation"][0]["tool"] == "route_board"
    assert r["remediation"][0]["args"].get("keep_existing") is True


def test_structural_errors_not_marked_cleanable(tmp_path, monkeypatch):
    proj = _project(tmp_path, monkeypatch)
    monkeypatch.setattr(stages, "run_drc", lambda *a, **k: _STRUCTURAL_DRC)
    monkeypatch.setattr(stages, "run_export", lambda *a, **k: {"success": True})
    r = mcp_server.export_outputs(proj)
    assert r["success"] is False
    assert r["reroute_cleanable"] is False           # annular_ring is structural
    assert "structural" in r["error"].lower()
    assert {v["rule"] for v in r["top_violations"]} == {"annular_ring", "clearance_min"}


# --- stage-level gate ------------------------------------------------------
# The MCP tool is not the only door: orchestrator/runner.py (CLI steps 6) and
# scripts/ call stages.run_export directly and used to bypass every check above.
# scripts/test_stm32_4layer.py shipped a full Gerber set from a board with 140
# DRC errors and 11 unrouted nets. These pin the backstop in stages itself.

def _cfg():
    from pathlib import Path as _Path
    from orchestrator.config import OrchestratorConfig
    return OrchestratorConfig.from_env(
        base_dir=_Path(__file__).resolve().parent.parent)


def _routed(tmp_path, unrouted=(), drc=None):
    import json
    pdir = tmp_path / "p"
    pdir.mkdir(exist_ok=True)
    (pdir / "p_routed.json").write_text(json.dumps(
        {"board": {}, "placements": [],
         "routing": {"unrouted_nets": list(unrouted)}}))
    if drc is not None:
        (pdir / "p_drc_report.json").write_text(json.dumps(drc))
    return pdir


_CLEAN_DRC = {"passed": True, "authoritative": True, "checks": [],
              "statistics": {"errors": 0}}


def test_stage_gate_blocks_drc_errors(tmp_path):
    pdir = _routed(tmp_path, drc={
        "passed": False, "authoritative": True,
        "checks": [{"rule": "no_shorts", "passed": False}],
        "statistics": {"errors": 140}})
    import json
    blocked = stages.export_blocked(pdir, "p", json.loads((pdir / "p_routed.json").read_text()))
    assert blocked and blocked["gate"] == "drc_failed"
    assert blocked["drc_errors"] == 140


def test_stage_gate_blocks_open_nets(tmp_path):
    import json
    pdir = _routed(tmp_path, unrouted=["net_a", "net_b"], drc=_CLEAN_DRC)
    blocked = stages.export_blocked(pdir, "p", json.loads((pdir / "p_routed.json").read_text()))
    assert blocked and blocked["gate"] == "connectivity"


def test_stage_gate_blocks_when_drc_never_ran(tmp_path):
    """Absence of a report must block, not pass — that is how bad boards ship."""
    import json
    pdir = _routed(tmp_path, drc=None)
    blocked = stages.export_blocked(pdir, "p", json.loads((pdir / "p_routed.json").read_text()))
    assert blocked and blocked["gate"] == "drc_missing"


def test_stage_gate_blocks_non_authoritative_drc(tmp_path):
    import json
    pdir = _routed(tmp_path, drc={"passed": True, "authoritative": False,
                                  "drc_engine": "internal", "checks": [],
                                  "statistics": {"errors": 0}})
    blocked = stages.export_blocked(pdir, "p", json.loads((pdir / "p_routed.json").read_text()))
    assert blocked and blocked["gate"] == "drc_not_authoritative"


def test_stage_gate_allows_clean_board(tmp_path):
    import json
    pdir = _routed(tmp_path, drc=_CLEAN_DRC)
    assert stages.export_blocked(pdir, "p", json.loads((pdir / "p_routed.json").read_text())) is None


def test_run_export_refuses_and_writes_nothing(tmp_path):
    """The real entry point, not just the predicate: no output dir on refusal."""
    pdir = _routed(tmp_path, drc={
        "passed": False, "authoritative": True,
        "checks": [{"rule": "no_shorts", "passed": False}],
        "statistics": {"errors": 7}})
    r = stages.run_export(pdir, "p", _cfg())
    assert r["success"] is False
    assert r["gate"] == "drc_failed"
    assert not (pdir / "output").exists()
