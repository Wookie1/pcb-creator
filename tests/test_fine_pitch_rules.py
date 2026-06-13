"""Fine-pitch routing rules: a board with a tight-pitch part (≤0.8mm) drops
to the manufacturer-minimum trace/clearance so escape routing is feasible,
while ordinary boards keep the robust defaults. Also covers persisting the
actual routed clearance so DRC checks the same rule the router used."""

import json
import tempfile
from pathlib import Path

import pytest

from orchestrator.stages import _build_router_kwargs, _min_pad_pitch
from orchestrator.config import OrchestratorConfig
from optimizers.pad_geometry import configure_lookup, get_default_cache
from orchestrator.cache import ComponentCache


@pytest.fixture(autouse=True)
def _lookup(tmp_path):
    configure_lookup(kicad_index=None,
                     cache=ComponentCache(str(tmp_path / "cache.json")))


def _project(tmp_path, package, manufacturer="jlcpcb"):
    pdir = tmp_path / "proj"
    pdir.mkdir()
    nl = {"version": "1.0", "project_name": "proj", "elements": [
        {"element_type": "component", "component_id": "comp_j1",
         "designator": "J1", "component_type": "connector", "value": "x",
         "package": package},
        {"element_type": "port", "port_id": "port_j1_1",
         "component_id": "comp_j1", "pin_number": 1, "name": "1",
         "electrical_type": "signal"},
        {"element_type": "port", "port_id": "port_j1_2",
         "component_id": "comp_j1", "pin_number": 2, "name": "2",
         "electrical_type": "signal"},
    ]}
    (pdir / "proj_netlist.json").write_text(json.dumps(nl))
    (pdir / "proj_requirements.json").write_text(
        json.dumps({"manufacturing": {"manufacturer": manufacturer}}))
    return pdir


def test_fine_pitch_uses_dfm_minimum(tmp_path):
    get_default_cache().put_footprint(
        "FineConn_P0.5", {"1": [-0.25, 0.0], "2": [0.25, 0.0]}, [0.27, 1.2],
        source="t", needs_review=False)
    pdir = _project(tmp_path, "FineConn_P0.5")
    assert _min_pad_pitch(pdir, "proj") == pytest.approx(0.5)
    kw = _build_router_kwargs(pdir, "proj")
    # JLCPCB minimum, not the coarse 0.25/0.2 floor
    assert kw["trace_width_signal_mm"] == pytest.approx(0.127)
    assert kw["clearance_mm"] == pytest.approx(0.127)


def test_coarse_board_keeps_robust_defaults(tmp_path):
    pdir = _project(tmp_path, "0805")  # 2.0mm pad pitch
    assert _min_pad_pitch(pdir, "proj") > 0.8
    kw = _build_router_kwargs(pdir, "proj")
    assert kw["trace_width_signal_mm"] == pytest.approx(0.25)
    assert kw["clearance_mm"] == pytest.approx(0.2)


def test_fine_pitch_without_profile_uses_safe_fine_rules(tmp_path):
    get_default_cache().put_footprint(
        "FineConn_P0.5", {"1": [-0.25, 0.0], "2": [0.25, 0.0]}, [0.27, 1.2],
        source="t", needs_review=False)
    pdir = tmp_path / "proj"
    pdir.mkdir()
    nl = {"version": "1.0", "project_name": "proj", "elements": [
        {"element_type": "component", "component_id": "comp_j1",
         "designator": "J1", "component_type": "connector", "value": "x",
         "package": "FineConn_P0.5"},
        {"element_type": "port", "port_id": "port_j1_1",
         "component_id": "comp_j1", "pin_number": 1, "name": "1",
         "electrical_type": "signal"},
        {"element_type": "port", "port_id": "port_j1_2",
         "component_id": "comp_j1", "pin_number": 2, "name": "2",
         "electrical_type": "signal"},
    ]}
    (pdir / "proj_netlist.json").write_text(json.dumps(nl))  # no requirements file
    kw = _build_router_kwargs(pdir, "proj")
    assert kw["trace_width_signal_mm"] == pytest.approx(0.127)
    assert kw["clearance_mm"] == pytest.approx(0.127)
