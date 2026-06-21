"""Footprint resolution must not hinge on PCB_KICAD_LIBRARY_PATH.

When that env var is unset (e.g. an MCP respawn lost the ambient value),
OrchestratorConfig auto-detects the system KiCad footprint library so standard
footprints still resolve instead of every board blocking on "unresolved
footprints".
"""

from pathlib import Path

import orchestrator.config as cfgmod
from orchestrator.config import OrchestratorConfig, _autodetect_kicad_library


def _make_lib(tmp_path):
    lib = tmp_path / "footprints"
    (lib / "Resistor_SMD.pretty").mkdir(parents=True)
    return lib


def test_autodetect_finds_a_candidate(tmp_path, monkeypatch):
    lib = _make_lib(tmp_path)
    monkeypatch.setattr(cfgmod, "_KICAD_LIBRARY_CANDIDATES", (str(lib),))
    assert _autodetect_kicad_library() == str(lib)


def test_autodetect_skips_dirs_without_pretty(tmp_path, monkeypatch):
    empty = tmp_path / "empty"
    empty.mkdir()
    monkeypatch.setattr(cfgmod, "_KICAD_LIBRARY_CANDIDATES", (str(empty),))
    assert _autodetect_kicad_library() is None


def test_autodetect_none_when_no_candidate(monkeypatch):
    monkeypatch.setattr(cfgmod, "_KICAD_LIBRARY_CANDIDATES",
                        ("/nonexistent/kicad/footprints",))
    assert _autodetect_kicad_library() is None


def test_from_env_falls_back_to_autodetect(tmp_path, monkeypatch):
    lib = _make_lib(tmp_path)
    monkeypatch.delenv("PCB_KICAD_LIBRARY_PATH", raising=False)
    monkeypatch.setattr(cfgmod, "_KICAD_LIBRARY_CANDIDATES", (str(lib),))
    c = OrchestratorConfig.from_env(base_dir=Path(__file__).resolve().parent.parent)
    assert c.kicad_library_path == str(lib)


def test_env_var_takes_precedence(tmp_path, monkeypatch):
    lib = _make_lib(tmp_path)
    monkeypatch.setenv("PCB_KICAD_LIBRARY_PATH", "/explicit/path")
    monkeypatch.setattr(cfgmod, "_KICAD_LIBRARY_CANDIDATES", (str(lib),))
    c = OrchestratorConfig.from_env(base_dir=Path(__file__).resolve().parent.parent)
    assert c.kicad_library_path == "/explicit/path"
