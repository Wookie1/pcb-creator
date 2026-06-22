"""import_kicad_netlist / create_circuit refuse to clobber an existing project,
but accept overwrite=True for a deliberate fresh start — and steer the agent
away from reaching for design_pcb to dodge the conflict."""
import mcp_server
from orchestrator import circuit_builder as cb


class TestCreateDraftOverwrite:
    def test_refuses_existing_without_overwrite(self, tmp_path):
        pdir = tmp_path / "p"
        pdir.mkdir()
        assert cb.create_draft(pdir, "p", "x", 40, 30)["ok"]
        r = cb.create_draft(pdir, "p", "x", 40, 30)
        assert r["ok"] is False and r["code"] == "draft_exists"
        assert "overwrite=True" in r["error"] and "design_pcb" in r["error"]

    def test_overwrite_replaces_project(self, tmp_path):
        pdir = tmp_path / "p"
        pdir.mkdir()
        cb.create_draft(pdir, "p", "first", 40, 30)
        (pdir / "stale_placement.json").write_text("{}")  # stale derived file
        r = cb.create_draft(pdir, "p", "second", 50, 40, overwrite=True)
        assert r["ok"] is True
        assert not (pdir / "stale_placement.json").exists()  # cleared
        d = cb.load_draft(pdir, "p")
        assert d["description"] == "second" and d["board"]["width_mm"] == 50

    def test_overwrite_validates_before_deleting(self, tmp_path):
        # A bad-input overwrite call must NOT delete the existing project.
        pdir = tmp_path / "p"
        pdir.mkdir()
        cb.create_draft(pdir, "p", "keep", 40, 30)
        r = cb.create_draft(pdir, "p", "x", 1, 1, overwrite=True)  # board too small
        assert r["ok"] is False and r["code"] == "bad_board"
        assert cb.load_draft(pdir, "p")["description"] == "keep"  # survived


class TestImportOverwrite:
    def test_refuses_and_steers_away_from_design_pcb(self, tmp_path, monkeypatch):
        monkeypatch.setenv("PCB_PROJECTS_DIR", str(tmp_path))
        proj = "imp"
        pdir = tmp_path / proj
        pdir.mkdir()
        (pdir / f"{proj}_netlist.json").write_text("{}")  # existing content
        r = mcp_server.import_kicad_netlist(proj, "/tmp/whatever.net")
        assert r["success"] is False
        assert "design_pcb" in r["error"]  # explicitly steered away
        overwrite_opts = [o for o in r["remediation"]
                          if o["tool"] == "import_kicad_netlist"
                          and o["args"].get("overwrite") is True]
        assert overwrite_opts  # a ready-to-run overwrite option is offered
