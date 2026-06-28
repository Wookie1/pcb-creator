"""Regression: 4-layer requirement must not silently route as 2-layer.

A live LLM run produced a 4-layer board (board.layers=4 in requirements) but
routed it as 2-layer with no inner planes, because the LLM-generated placement
JSON omits board.layers and run_routing defaulted the count to 2 instead of
falling back to the requirements. _routing_layer_count is that fallback.
"""

import json

from orchestrator.stages import _routing_layer_count


def _write(project_dir, name, suffix, obj):
    (project_dir / f"{name}_{suffix}.json").write_text(json.dumps(obj))


def test_falls_back_to_requirements_when_placement_omits_layers(tmp_path):
    name = "p"
    _write(tmp_path, name, "requirements", {"board": {"layers": 4}})
    placement = {"board": {"width_mm": 40, "height_mm": 30}}  # no 'layers' (LLM drop)
    assert _routing_layer_count(tmp_path, name, placement) == 4


def test_placement_layers_take_precedence(tmp_path):
    name = "p"
    _write(tmp_path, name, "requirements", {"board": {"layers": 4}})
    placement = {"board": {"layers": 2, "width_mm": 40}}
    assert _routing_layer_count(tmp_path, name, placement) == 2


def test_defaults_to_two_when_nothing_specifies(tmp_path):
    assert _routing_layer_count(tmp_path, "p", {"board": {}}) == 2


def test_ignores_bogus_placement_layer_value(tmp_path):
    name = "p"
    _write(tmp_path, name, "requirements", {"board": {"layers": 4}})
    placement = {"board": {"layers": 3}}  # invalid -> fall back, not trust it
    assert _routing_layer_count(tmp_path, name, placement) == 4
