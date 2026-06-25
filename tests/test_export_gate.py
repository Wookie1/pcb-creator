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
