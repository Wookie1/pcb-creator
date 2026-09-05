"""Fill-short detection (audit finding F1 regression).

Copper fills are painted by gerber_exporter as solid regions; before
_check_fill_shorts existed, a GND pour could swallow any amount of
foreign-net copper and the board still validated clean (audit_lm2596 shipped
with 28 foreign trace endpoints under its bottom pour). These tests pin the
new geometry checks and the honest end-to-end verdict.
"""

import json
import os
import sys

import pytest

_VAL = os.path.join(os.path.dirname(__file__), "..", "validators")
sys.path.insert(0, _VAL)
_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, _ROOT)

from validate_routing import (  # noqa: E402
    _check_fill_shorts,
    _point_in_poly,
    _segment_to_poly_gap,
    _segments_cross,
    validate_routing,
)

# A 10x10 square pour covering x/y 0..10.
_SQ = [[0, 0], [10, 0], [10, 10], [0, 10]]

_AUDIT = os.path.expanduser(
    "~/.pcb-creator/projects/audit_lm2596/audit_lm2596_routed.json")


def _routed(*fills, traces=(), vias=(), clearance=0.2):
    return {"routing": {
        "config": {"trace_clearance_mm": clearance},
        "traces": list(traces),
        "vias": list(vias),
        "copper_fills": list(fills),
    }}


def _pour(layer, net, polys, is_plane=None):
    f = {"layer": layer, "net_id": net, "net_name": net, "polygons": polys}
    if is_plane is not None:
        f["is_plane"] = is_plane
    return f


def _trace(net, layer, x0, y0, x1, y1, w=0.25):
    return {"net_id": net, "net_name": net.upper(), "layer": layer,
            "start_x_mm": x0, "start_y_mm": y0,
            "end_x_mm": x1, "end_y_mm": y1, "width_mm": w}


def _via(net, x, y, d=0.6):
    return {"net_id": net, "net_name": net.upper(), "x_mm": x, "y_mm": y,
            "diameter_mm": d, "from_layer": "top", "to_layer": "bottom"}


# --------------------------------------------------------------------------
# Geometry primitives
# --------------------------------------------------------------------------

def test_point_in_poly():
    assert _point_in_poly(5, 5, _SQ)
    assert not _point_in_poly(11, 5, _SQ)
    assert not _point_in_poly(-1, -1, _SQ)


def test_segments_cross():
    assert _segments_cross(0, 0, 10, 10, 0, 10, 10, 0)          # proper cross
    assert not _segments_cross(0, 0, 1, 0, 5, 1, 6, 1)          # far apart
    assert not _segments_cross(0, 0, 1, 0, 0, 1, 1, 1)          # parallel


def test_segment_to_poly_gap_crossing_is_zero():
    assert _segment_to_poly_gap(5, -1, 5, 11, _SQ, True) == 0.0
    assert _segment_to_poly_gap(11, 5, 12, 5, _SQ, True) == pytest.approx(1.0)


# --------------------------------------------------------------------------
# Trace vs pour
# --------------------------------------------------------------------------

def test_trace_inside_pour_is_fill_short():
    routed = _routed(_pour("top", "GND", [_SQ]),
                     traces=[_trace("VCC", "top", 1, 5, 9, 5)])
    errors, warnings = _check_fill_shorts(routed, None)
    assert any(e.startswith("Fill short on top") and "VCC" in e
               for e in errors)
    assert not warnings


def test_trace_outside_at_clearance_is_clean():
    # 2mm from the pour edge, different net → nothing to report.
    routed = _routed(_pour("top", "GND", [_SQ]),
                     traces=[_trace("VCC", "top", 12, 5, 14, 5)])
    errors, warnings = _check_fill_shorts(routed, None)
    assert errors == [] and warnings == []


def test_trace_near_pour_edge_warns_but_no_error():
    # Vertical trace at x=10.2: copper gap = 0.2 - 0.125 = 0.075 < 0.15.
    routed = _routed(_pour("top", "GND", [_SQ]),
                     traces=[_trace("VCC", "top", 10.2, 2, 10.2, 8)])
    errors, warnings = _check_fill_shorts(routed, None)
    assert errors == []
    assert any("Fill clearance on top" in w for w in warnings)


def test_trace_crossing_pour_edge_is_error():
    routed = _routed(_pour("top", "GND", [_SQ]),
                     traces=[_trace("VCC", "top", 5, -2, 5, 2)])
    errors, _ = _check_fill_shorts(routed, None)
    assert any("Fill short" in e for e in errors)


def test_same_net_trace_in_pour_is_not_a_short():
    # The pour exists to carry GND — GND copper in it is a connection.
    routed = _routed(_pour("top", "GND", [_SQ]),
                     traces=[_trace("GND", "top", 1, 5, 9, 5)])
    errors, warnings = _check_fill_shorts(routed, None)
    assert errors == [] and warnings == []


def test_different_layer_is_ignored():
    routed = _routed(_pour("bottom", "GND", [_SQ]),
                     traces=[_trace("VCC", "top", 1, 5, 9, 5)])
    errors, warnings = _check_fill_shorts(routed, None)
    assert errors == [] and warnings == []


# --------------------------------------------------------------------------
# Via vs pour
# --------------------------------------------------------------------------

def test_via_inside_pour_is_fill_short():
    routed = _routed(_pour("top", "GND", [_SQ]),
                     vias=[_via("VCC", 5, 5)])
    errors, _ = _check_fill_shorts(routed, None)
    # Pour exists on top only -> exactly the top annular ring short.
    assert len(errors) == 1
    assert "Fill short on top" in errors[0] and "via(VCC)" in errors[0]


def test_via_clear_of_pour_is_clean():
    routed = _routed(_pour("top", "GND", [_SQ]),
                     vias=[_via("VCC", 20, 20)])
    errors, warnings = _check_fill_shorts(routed, None)
    assert errors == [] and warnings == []


# --------------------------------------------------------------------------
# Plane semantics: polygons[0] copper boundary, polygons[1:] hole cut-outs
# --------------------------------------------------------------------------

_OUTER = [[0, 0], [30, 0], [30, 30], [0, 30]]
_HOLE = [[5, 5], [25, 5], [25, 25], [5, 25]]


def test_plane_hole_void_is_not_copper():
    # Trace entirely inside the thermal void, clear of its edges.
    routed = _routed(_pour("inner1", "GND", [_OUTER, _HOLE], is_plane=True),
                     traces=[_trace("VCC", "inner1", 10, 15, 20, 15)])
    errors, warnings = _check_fill_shorts(routed, None)
    assert errors == [] and warnings == []


def test_trace_across_plane_copper_is_error():
    # From inside the hole, over the copper ring, out the other side.
    routed = _routed(_pour("inner1", "GND", [_OUTER, _HOLE], is_plane=True),
                     traces=[_trace("VCC", "inner1", 20, 15, 35, 15)])
    errors, _ = _check_fill_shorts(routed, None)
    assert any("Fill short on inner1" in e for e in errors)


def test_trace_in_plane_band_copper_is_error():
    # y=2 lies in the copper band between outer edge and hole.
    routed = _routed(_pour("inner1", "GND", [_OUTER, _HOLE], is_plane=True),
                     traces=[_trace("VCC", "inner1", 5, 2, 25, 2)])
    errors, _ = _check_fill_shorts(routed, None)
    assert any("Fill short" in e for e in errors)


# --------------------------------------------------------------------------
# Degenerate input must not crash
# --------------------------------------------------------------------------

def test_degenerate_fills_are_skipped():
    routed = _routed(
        {"layer": "top", "net_id": "GND", "polygons": []},
        {"net_id": "GND", "polygons": [[[0, 0], [1, 1]]]},
        {"layer": "top", "polygons": [[[0, 0], [1, 1], [2, 0]]]},
        traces=[_trace("VCC", "top", 1, 5, 9, 5)],
    )
    errors, warnings = _check_fill_shorts(routed, None)
    assert errors == [] and warnings == []


def test_no_fills_returns_immediately():
    assert _check_fill_shorts({"routing": {}}, None) == ([], [])


# --------------------------------------------------------------------------
# End-to-end: validate_routing + DRC report integration
# --------------------------------------------------------------------------

def test_validate_routing_reports_fill_short(tmp_path):
    # Full schema-valid board: a GND pour swallowing the whole VCC run.
    routed = {
        "version": "1.0",
        "project_name": "fillshort",
        "source_netlist": "fillshort_netlist.json",
        "source_bom": "fillshort_bom.json",
        "board": {"width_mm": 20, "height_mm": 20, "layers": 2},
        "placements": [{
            "designator": "R1", "component_type": "resistor",
            "package": "0402", "footprint_width_mm": 1.0,
            "footprint_height_mm": 0.5, "x_mm": 15, "y_mm": 5,
            "rotation_deg": 0, "layer": "top",
        }],
        "routing": {
            "traces": [{
                "start_x_mm": 1, "start_y_mm": 5, "end_x_mm": 9,
                "end_y_mm": 5, "width_mm": 0.25, "layer": "top",
                "net_id": "VCC", "net_name": "VCC",
            }],
            "vias": [],
            "statistics": {"total_nets": 1, "routed_nets": 1,
                           "completion_pct": 100},
            "config": {"trace_clearance_mm": 0.2},
            "copper_fills": [{
                "layer": "top", "net_id": "GND", "net_name": "GND",
                "polygons": [_SQ],
            }],
        },
        "silkscreen": [],
    }
    routed_path = tmp_path / "routed.json"
    routed_path.write_text(json.dumps(routed))
    result = validate_routing(str(routed_path))
    assert not result["valid"], result["errors"]
    assert not [e for e in result["errors"] if e.startswith("Schema")]
    assert any("Fill short" in e for e in result["errors"])


def test_drc_report_flags_fill_shorts_under_no_shorts():
    from validators.drc_report import run_drc
    routed = _routed(_pour("bottom", "GND", [_SQ]),
                     traces=[_trace("VCC", "bottom", 1, 5, 9, 5)])
    report = run_drc(routed, {"elements": []})
    ns = [c for c in report["checks"] if c["rule"] == "no_shorts"][0]
    assert not ns["passed"]
    assert any("Fill short" in v["message"] for v in ns["violations"])
    assert not report["passed"]
    assert report["statistics"]["errors"] >= 1


# --------------------------------------------------------------------------
# The real offender: audit_lm2596 (skipif-missing regression fixture)
# --------------------------------------------------------------------------

@pytest.mark.skipif(not os.path.exists(_AUDIT),
                    reason="audit_lm2596 fixture not present")
def test_audit_lm2596_fill_shorts_are_detected():
    result = validate_routing(_AUDIT)
    assert not result["valid"], "audit board must no longer validate clean"
    sw = [e for e in result["errors"]
          if e.startswith("Fill short") and "SW_NODE" in e]
    assert sw, "bottom pour covers the SW_NODE trace — must be flagged"
