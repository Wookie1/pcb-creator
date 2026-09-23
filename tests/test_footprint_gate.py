"""Routing and export refuse a board whose parts would use the generic
placeholder footprint. A script that skipped configure_lookup turned rev3's
SOIC-14 into perimeter placeholder pads, and every downstream check passed
because they all shared the same wrong pad map."""
import json

import optimizers.pad_geometry as pg
from orchestrator import stages


def _netlist(pkg):
    return {"elements": [
        {"element_type": "component", "component_id": "c1", "designator": "U1",
         "component_type": "ic", "package": pkg},
        *({"element_type": "port", "port_id": f"p{i}", "component_id": "c1",
           "pin_number": i, "name": str(i)} for i in range(1, 15)),
    ]}


def _project(tmp_path, pkg):
    (tmp_path / "t_netlist.json").write_text(json.dumps(_netlist(pkg)))
    (tmp_path / "t_placement.json").write_text(json.dumps(
        {"board": {"width_mm": 20, "height_mm": 20}, "placements": []}))
    return tmp_path


def test_unconfigured_lookup_blocks_route_and_export(tmp_path, monkeypatch):
    for name in ("_default_kicad_index", "_default_cache", "_default_custom_index"):
        monkeypatch.setattr(pg, name, None)
    # Configured with no tiers (conftest): stages won't auto-install, so this is
    # the "KiCad library not found anywhere" case — the gate is the backstop.
    pdir = _project(tmp_path, "SOIC-14")

    blocked = stages.export_blocked(pdir, "t", {"routing": {}})
    assert blocked["gate"] == "unresolved_footprints"
    assert blocked["unresolved_footprints"][0]["designator"] == "U1"

    r = stages.run_routing(pdir, "t", type("C", (), {"base_dir": "."})())
    assert r["success"] is False and r["gate"] == "unresolved_footprints"


def test_resolvable_footprints_pass_the_gate():
    two_pin = {"elements": [
        {"element_type": "component", "component_id": "c1", "designator": "R1",
         "component_type": "resistor", "package": "0805"},
        {"element_type": "port", "port_id": "a", "component_id": "c1", "pin_number": 1},
        {"element_type": "port", "port_id": "b", "component_id": "c1", "pin_number": 2},
    ]}
    assert stages._footprint_gate(two_pin, "route") is None


def _unconfigured(monkeypatch):
    for name in ("_default_kicad_index", "_default_cache", "_default_custom_index"):
        monkeypatch.setattr(pg, name, None)
    monkeypatch.setattr(pg, "_lookup_configured", False)   # standalone script


def test_stages_install_the_real_lookup_when_nobody_did(tmp_path, monkeypatch):
    """The rev3 trap, fixed at the root: a script that never configured the
    lookup now gets the real KiCad SOIC-14, not the placeholder."""
    import pytest
    from orchestrator.config import OrchestratorConfig
    cfg = OrchestratorConfig.from_env()
    if not cfg.kicad_library_path:
        pytest.skip("no KiCad footprint library on this machine")
    cfg.component_cache_path = str(tmp_path / "cache.json")
    _unconfigured(monkeypatch)
    assert stages._footprint_gate(_netlist("SOIC-14"), "export")   # placeholder
    stages._ensure_lookup(cfg, tmp_path)
    assert pg.lookup_configured()
    assert stages._footprint_gate(_netlist("SOIC-14"), "export") is None
    fp = pg.get_footprint_def("SOIC-14", 14)
    assert fp.pad_size[0] >= 1.5 or fp.pad_size[1] >= 1.5          # real SOIC pad


def test_existing_lookup_is_left_alone(tmp_path, monkeypatch):
    _unconfigured(monkeypatch)
    sentinel = object()
    pg.configure_lookup(cache=sentinel)          # e.g. eval's throwaway cache
    stages._ensure_lookup(type("C", (), {"component_cache_path": str(tmp_path / "c.json"),
                                         "kicad_library_path": None})(), tmp_path)
    assert pg.get_default_cache() is sentinel
