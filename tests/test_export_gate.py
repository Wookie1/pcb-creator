"""export_outputs must refuse to emit manufacturing files for a board with DRC
errors (the agent kept shipping boards with shorts / disconnected nets), with an
explicit allow_drc_errors override for a knowingly-preliminary quote."""
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


def test_override_forces_export(tmp_path, monkeypatch):
    proj = _project(tmp_path, monkeypatch)
    drc_called = {"v": False}
    monkeypatch.setattr(stages, "run_drc",
                        lambda *a, **k: drc_called.update(v=True) or _FAIL_DRC)
    monkeypatch.setattr(stages, "run_export",
                        lambda *a, **k: {"success": True, "files": []})
    r = mcp_server.export_outputs(proj, allow_drc_errors=True)
    assert r["success"] is True
    assert drc_called["v"] is False                   # gate skipped entirely


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


def test_open_nets_block_export_even_with_override(tmp_path, monkeypatch):
    """A board with unrouted nets is electrically incomplete — refused even with
    allow_drc_errors=True (the override is only for cosmetic/clearance DRC).
    This is the stop for the agent shipping a 95.8%-routed board via the flag."""
    proj = _project(tmp_path, monkeypatch)
    # routed.json with 2 open nets
    (tmp_path / proj / f"{proj}_routed.json").write_text(json.dumps(
        {"routing": {"traces": [], "vias": [],
                     "unrouted_nets": ["net_a", "net_b"],
                     "statistics": {"completion_pct": 95.8}}}))
    exported = {"v": False}
    monkeypatch.setattr(stages, "run_export",
                        lambda *a, **k: exported.update(v=True) or {"success": True})
    r = mcp_server.export_outputs(proj, allow_drc_errors=True)
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
