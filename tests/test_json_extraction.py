"""Tests for LLM-output JSON extraction: <think> stripping, duplicate-after-
think re-emission, markdown fences, and repair.

The duplicate-after-think case is the real failure mode observed with local
MLX Qwen/DeepSeek builds: the model emits the answer inside the think block,
closes </think>, then re-emits the same answer. Naive first-brace-to-last-
brace extraction spans both copies (glued by </think>) and is invalid JSON.
"""

import json

import pytest

from orchestrator.steps.step_1_schematic import (
    _strip_reasoning,
    extract_json,
    SchematicStep,
)

extract_array = SchematicStep._extract_json_array


class TestStripReasoning:
    def test_no_think_passthrough(self):
        assert _strip_reasoning('[{"a":1}]') == '[{"a":1}]'

    def test_empty_think_block(self):
        assert _strip_reasoning('<think>\n</think>\n[{"a":1}]') == '[{"a":1}]'

    def test_reasoning_then_answer(self):
        assert _strip_reasoning('<think>let me plan</think>\n{"x":1}') == '{"x":1}'

    def test_answer_inside_then_reemit(self):
        # The observed MLX shape — keep only the copy after </think>
        raw = '[{"a":1}]\n</think>\n\n[{"a":1}]'
        assert _strip_reasoning(raw) == '[{"a":1}]'

    def test_unclosed_think_keeps_body(self):
        assert _strip_reasoning('<think>\n[{"y":2}]') == '[{"y":2}]'


class TestExtractArrayWithThink:
    def test_duplicate_after_think_real_shape(self):
        net = ('{"element_type":"net","net_id":"net_gnd","name":"GND",'
               '"connected_port_ids":["port_u1_3","port_c1_2"],'
               '"net_class":"ground"}')
        raw = f'[{net}]\n</think>\n\n[{net}]'
        arr = json.loads(extract_array(raw))
        assert len(arr) == 1
        assert arr[0]["net_id"] == "net_gnd"

    def test_empty_think_then_array(self):
        assert json.loads(extract_array('<think>\n</think>\n[{"a":1}]')) == [{"a": 1}]

    def test_fenced_array_after_think(self):
        raw = '<think>x</think>\n```json\n[{"a":1}]\n```'
        assert json.loads(extract_array(raw)) == [{"a": 1}]


class TestExtractObjectWithThink:
    def test_object_after_think(self):
        raw = '<think>reasoning</think>\n{"version":"1.0","elements":[]}'
        assert json.loads(extract_json(raw)) == {"version": "1.0", "elements": []}

    def test_duplicate_object_after_think(self):
        obj = '{"version":"1.0","elements":[]}'
        assert json.loads(extract_json(f'{obj}</think>{obj}')) == json.loads(obj)


# ---------------------------------------------------------------------------
# Pad-clearance geometry: exact rectangle distance (regression for the
# circular-approximation false positive on elongated pads)
# ---------------------------------------------------------------------------

class TestPadClearanceGeometry:
    def test_trace_past_short_side_of_soic_pad_is_legal(self):
        """The exact soil_moisture false positive: a 1.5x0.6mm SOIC pad with a
        trace passing its short side at a true gap of 0.217mm (legal) was
        flagged because the pad was approximated as a 0.75mm-radius circle."""
        from validators.validate_routing import _segment_to_rect_distance
        # Pad at (25.18, 8.905), 1.5x0.6 -> hw=0.75, hh=0.3
        # Trace y=8.263, width 0.25 -> half 0.125
        d = _segment_to_rect_distance(26.072, 8.263, 23.739, 8.263,
                                      25.18, 8.905, 0.75, 0.3)
        gap = d - 0.125
        assert gap > 0.2, f"legal gap {gap:.3f} must not be flagged"

    def test_real_overlap_detected(self):
        from validators.validate_routing import _segment_to_rect_distance
        # Trace passing straight through the pad
        d = _segment_to_rect_distance(0, 0, 10, 0, 5, 0, 0.75, 0.3)
        assert d == 0.0

    def test_endpoint_inside_rect(self):
        from validators.validate_routing import _segment_to_rect_distance
        assert _segment_to_rect_distance(5, 0.1, 20, 0.1, 5, 0, 0.75, 0.3) == 0.0

    def test_point_to_rect(self):
        from validators.validate_routing import _point_to_rect_distance
        assert _point_to_rect_distance(5, 5, 5, 5, 1, 1) == 0.0  # inside
        assert abs(_point_to_rect_distance(7, 5, 5, 5, 1, 1) - 1.0) < 1e-9
        import math
        assert abs(_point_to_rect_distance(7, 7, 5, 5, 1, 1) - math.hypot(1, 1)) < 1e-9


class TestFootprintPadCoverage:
    def test_ports_beyond_footprint_pads_flagged(self):
        """Components with more ports than footprint pads previously produced
        phantom pads stacked at the component centre (the esp8266 22-vs-16
        failure) — verify_footprints must flag them."""
        from validators.verify_footprints import verify_footprints
        elements = [
            {"element_type": "component", "component_id": "comp_u1",
             "designator": "U1", "component_type": "ic",
             "value": "X", "package": "SOIC-8"},
        ]
        # 10 ports on an 8-pad SOIC
        for n in range(1, 11):
            elements.append({"element_type": "port",
                             "port_id": f"port_u1_{n}",
                             "component_id": "comp_u1", "pin_number": n,
                             "name": str(n), "electrical_type": "signal"})
        issues = verify_footprints({"version": "1.0", "elements": elements})
        assert issues, "10 ports on an 8-pad footprint must be flagged"
        assert "9" in issues[0]["reason"] and "10" in issues[0]["reason"]

    def test_esp12f_22_ports_now_covered(self):
        from validators.verify_footprints import verify_footprints
        elements = [
            {"element_type": "component", "component_id": "comp_u2",
             "designator": "U2", "component_type": "ic",
             "value": "ESP-12F", "package": "ESP-12F"},
        ]
        for n in range(1, 23):
            elements.append({"element_type": "port",
                             "port_id": f"port_u2_{n}",
                             "component_id": "comp_u2", "pin_number": n,
                             "name": str(n), "electrical_type": "signal"})
        assert verify_footprints({"version": "1.0", "elements": elements}) == []
