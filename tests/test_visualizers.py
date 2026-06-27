"""Coverage-driving tests for visualizers/placement_viewer.py and netlist_viewer.py.

Loads real project fixtures where one exists; fabricates small dicts only for
branches the real data doesn't exercise (polygon outline, inner planes, trace
overrides, unrouted nets, failing DRC).
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

import visualizers.placement_viewer as pv
import visualizers.netlist_viewer as nv

# Real fixtures live in the MAIN repo (worktree projects/ may be empty).
FIXTURE_DIR = Path(
    "/Users/James/ai-sandbox/Productizr/pcb-creator/projects/blink_3_leds_dc_power"
)


def _load(name: str) -> dict:
    return json.loads((FIXTURE_DIR / f"blink_3_leds_dc_power_{name}.json").read_text())


@pytest.fixture(scope="module")
def placement() -> dict:
    return _load("placement")


@pytest.fixture(scope="module")
def netlist() -> dict:
    return _load("netlist")


@pytest.fixture(scope="module")
def bom() -> dict:
    return _load("bom")


@pytest.fixture(scope="module")
def routed() -> dict:
    return _load("routed")


@pytest.fixture(scope="module")
def drc() -> dict:
    return _load("drc_report")


# --------------------------------------------------------------------------
# _build_bom_lookup
# --------------------------------------------------------------------------

def test_build_bom_lookup_none():
    assert pv._build_bom_lookup(None) == {}


def test_build_bom_lookup_empty():
    assert pv._build_bom_lookup({}) == {}


def test_build_bom_lookup_real(bom):
    lookup = pv._build_bom_lookup(bom)
    assert "J1" in lookup
    assert lookup["J1"]["value"] == "DC_Jack_2.1x5.5"


# --------------------------------------------------------------------------
# _format_specs
# --------------------------------------------------------------------------

def test_format_specs_priority_and_remaining():
    specs = {
        "tolerance": "5%",          # priority key
        "power_rating": "0.25W",    # priority key
        "custom_field": "x",        # remaining key -> included
        "material": "FR4",          # excluded key -> dropped
    }
    out = pv._format_specs(specs)
    assert "Tolerance: 5%" in out
    assert "Power Rating: 0.25W" in out
    assert "Custom Field: x" in out
    assert "FR4" not in out  # material is dropped


def test_format_specs_empty():
    assert pv._format_specs({}) == ""


# --------------------------------------------------------------------------
# _routing_stats_html / _per_net_stats_html
# --------------------------------------------------------------------------

def test_routing_stats_none():
    assert pv._routing_stats_html(None) == ""


def test_routing_stats_no_stats():
    assert pv._routing_stats_html({"routing": {}}) == ""


def test_routing_stats_real(routed, netlist):
    html = pv._routing_stats_html(routed, netlist)
    assert "Completion:" in html
    assert "100" in html  # real fixture is 100% complete
    assert "Copper fill:" in html  # real fixture has copper fills
    assert "Per-Net Details" in html  # netlist + traces -> per-net table


def test_routing_stats_unrouted_and_overrides_and_lowcompletion():
    """Hit the unrouted, overrides, mid-completion-color, and no-fill branches."""
    routed = {
        "routing": {
            "statistics": {
                "completion_pct": 60,
                "routed_nets": 6,
                "total_nets": 10,
                "total_trace_length_mm": 12.3,
                "via_count": 4,
                "layer_usage": {},
                "copper_fill_polygons": 0,
            },
            "traces": [{"net_id": "n1"}],
            "unrouted_nets": ["GND", "VCC"],
            "trace_width_overrides": {"n1": 0.5},
        }
    }
    html = pv._routing_stats_html(routed)  # netlist omitted -> skip per-net table
    assert "60%" in html
    assert "Unrouted:" in html
    assert "GND, VCC" in html
    assert "IPC-2221 upsizes:" in html
    assert "Per-Net Details" not in html


def test_routing_stats_low_completion_color():
    routed = {
        "routing": {
            "statistics": {"completion_pct": 10, "layer_usage": {}},
            "traces": [],
        }
    }
    html = pv._routing_stats_html(routed)
    assert "#ef4444" in html  # red for <50%
    assert "10%" in html


def test_per_net_stats_directly(netlist):
    """Drive _per_net_stats_html with fabricated traces/vias/fill to hit width
    formatting, the +N components overflow, fill badge, and via counting."""
    nets = [e for e in netlist["elements"] if e["element_type"] == "net"]
    # Pick a net that connects to many components (to trigger +N overflow path),
    # plus a fill net.
    nid = max(nets, key=lambda n: len(n.get("connected_port_ids", [])))["net_id"]
    routing = {
        "traces": [
            {"net_id": nid, "start_x_mm": 0, "start_y_mm": 0,
             "end_x_mm": 3, "end_y_mm": 4, "width_mm": 0.25},
            {"net_id": nid, "start_x_mm": 3, "start_y_mm": 4,
             "end_x_mm": 6, "end_y_mm": 8, "width_mm": 0.5},
            {"net_id": "unknown_net", "start_x_mm": 0, "start_y_mm": 0,
             "end_x_mm": 1, "end_y_mm": 0, "width_mm": 0.25},
        ],
        "vias": [{"net_id": nid}, {"net_id": "ignored"}],
        "copper_fills": [{"net_id": nid}, {"net_id": "fill_only_net"}],
    }
    html = pv._per_net_stats_html(routing, netlist)
    assert "Per-Net Details" in html
    assert "0.25/0.50" in html  # both widths formatted
    assert ">F<" in html  # fill badge present


# --------------------------------------------------------------------------
# _kicad_export_html
# --------------------------------------------------------------------------

def test_kicad_export_none_inputs(routed, netlist):
    assert pv._kicad_export_html(None, netlist) == ""
    assert pv._kicad_export_html(routed, None) == ""
    assert pv._kicad_export_html({"routing": {}}, netlist) == ""


def test_kicad_export_real(routed, netlist):
    html = pv._kicad_export_html(routed, netlist)
    assert "exportKicad" in html
    assert "routedData" in html
    assert "netlistData" in html
    # default filename derives from project_name
    assert f'{routed.get("project_name", "board")}.kicad_pcb' in html


# --------------------------------------------------------------------------
# _header_routing_stat / _progress_bar_html
# --------------------------------------------------------------------------

def test_header_routing_stat_none():
    assert pv._header_routing_stat(None) == ""


def test_header_routing_stat_full():
    out = pv._header_routing_stat({"routing": {"statistics": {"completion_pct": 100}}})
    assert "100%" in out and "#22c55e" in out


def test_header_routing_stat_mid():
    out = pv._header_routing_stat({"routing": {"statistics": {"completion_pct": 75}}})
    assert "#f59e0b" in out


def test_header_routing_stat_low():
    out = pv._header_routing_stat({"routing": {"statistics": {"completion_pct": 0}}})
    assert "#ef4444" in out


def test_progress_bar_none():
    assert pv._progress_bar_html(None) == ""


def test_progress_bar_full():
    out = pv._progress_bar_html({"routing": {"statistics": {"completion_pct": 100}}})
    assert "width:100%" in out and "#22c55e" in out


def test_progress_bar_mid():
    out = pv._progress_bar_html({"routing": {"statistics": {"completion_pct": 60}}})
    assert "#f59e0b" in out


def test_progress_bar_low():
    out = pv._progress_bar_html({"routing": {"statistics": {"completion_pct": 5}}})
    assert "#ef4444" in out


# --------------------------------------------------------------------------
# generate_svg
# --------------------------------------------------------------------------

def test_generate_svg_minimal(placement):
    svg = pv.generate_svg(placement)
    assert svg.startswith("<svg")
    assert svg.rstrip().endswith("</svg>")
    assert "U1" in svg  # a real designator
    assert "(0,0)" in svg  # origin marker


def test_generate_svg_distinct_outputs(placement, netlist, routed):
    bare = pv.generate_svg(placement)
    full = pv.generate_svg(placement, netlist=netlist, routed=routed)
    assert bare != full
    # routed has traces -> trace-hit hit targets present, ratsnest suppressed
    assert "trace-hit" in full
    assert "stroke-dasharray" not in full or "trace-hit" in full


def test_generate_svg_ratsnest_only(placement, netlist):
    """netlist with no routed traces -> ratsnest lines drawn."""
    svg = pv.generate_svg(placement, netlist=netlist)
    assert "stroke-dasharray" in svg  # ratsnest dashed lines


def test_generate_svg_with_bom(placement, bom):
    svg = pv.generate_svg(placement, bom=bom)
    assert "data-value=" in svg


def test_generate_svg_polygon_outline(placement):
    """Polygon board outline branch + via/fill/silk variety on a synthetic board."""
    p = copy.deepcopy(placement)
    p["board"]["outline_vertices"] = [[0, 0], [50, 0], [50, 35], [0, 35]]
    svg = pv.generate_svg(p)
    assert "<polygon" in svg


def test_generate_svg_inner_plane_and_silk_dot(placement, netlist):
    """Hit: inner plane fill (is_plane, only first polygon), copper fill render,
    vias, silkscreen text(anode)+dot, and rotation 180/270 pin-1 branches."""
    p = copy.deepcopy(placement)
    # Force some rotations to hit all pin-1 indicator branches (0/90/180/270).
    rots = [0, 90, 180, 270]
    for i, item in enumerate(p["placements"]):
        if item.get("component_type") != "fiducial":
            item["rotation_deg"] = rots[i % 4]
            item["layer"] = "bottom" if i % 2 else "top"
    routed = {
        "routing": {
            "traces": [
                {"net_id": "n_pwr", "layer": "top", "start_x_mm": 1, "start_y_mm": 1,
                 "end_x_mm": 5, "end_y_mm": 5, "width_mm": 0.3},
                {"net_id": "n_gnd", "layer": "bottom", "start_x_mm": 1, "start_y_mm": 1,
                 "end_x_mm": 5, "end_y_mm": 5, "width_mm": 0.3},
                {"net_id": "n_sig", "layer": "inner1", "start_x_mm": 1, "start_y_mm": 1,
                 "end_x_mm": 5, "end_y_mm": 5, "width_mm": 0.2},
                {"net_id": "n_sig2", "layer": "inner2", "start_x_mm": 1, "start_y_mm": 1,
                 "end_x_mm": 5, "end_y_mm": 5, "width_mm": 0.2},
            ],
            "vias": [{"net_id": "n_gnd", "x_mm": 10, "y_mm": 10,
                      "diameter_mm": 0.6, "drill_mm": 0.3}],
            "copper_fills": [
                {"net_id": "n_gnd", "net_name": "GND", "layer": "inner1",
                 "is_plane": True,
                 "polygons": [[[0, 0], [50, 0], [50, 35]], [[1, 1], [2, 2], [3, 1]]]},
                {"net_id": "n_pwr", "net_name": "VCC", "layer": "top",
                 "polygons": [[[0, 0], [50, 0], [50, 35]]]},
                {"net_id": "n_bad", "net_name": "X", "layer": "top",
                 "polygons": [[[0, 0], [1, 1]]]},  # <3 pts -> skipped
            ],
        },
        "silkscreen": [
            {"type": "text", "x_mm": 5, "y_mm": 5, "text": "+", "purpose": "anode",
             "font_height_mm": 1.0, "layer": "top_silk"},
            {"type": "text", "x_mm": 8, "y_mm": 8, "text": "R1",
             "layer": "bottom_silk"},
            {"type": "dot", "x_mm": 12, "y_mm": 12, "diameter_mm": 0.5,
             "layer": "top_silk"},
        ],
    }
    # net_class lookup: mark n_pwr power, n_gnd ground for the color branches.
    n = copy.deepcopy(netlist)
    n["elements"].extend([
        {"element_type": "net", "net_id": "n_pwr", "name": "VCC", "net_class": "power",
         "connected_port_ids": []},
        {"element_type": "net", "net_id": "n_gnd", "name": "GND", "net_class": "ground",
         "connected_port_ids": []},
        {"element_type": "net", "net_id": "n_sig", "name": "SIG", "net_class": "signal",
         "connected_port_ids": []},
    ])
    svg = pv.generate_svg(p, netlist=n, routed=routed)
    assert "copper-fill" in svg
    assert "class=\"via\"" in svg
    assert "silkscreen" in svg
    assert "#ffcc00" in svg  # anode marker color


def test_generate_svg_defaults_no_board():
    """No board key -> default 50x30; empty placements."""
    svg = pv.generate_svg({"placements": []})
    assert svg.startswith("<svg")
    assert "50.0mm" in svg and "30.0mm" in svg


def test_generate_svg_ratsnest_skips_under_two_pts(netlist):
    """A net whose designators resolve to <2 placed points hits the continue."""
    # Only one placed component; multi-pin nets resolve to <2 present points.
    placement = {
        "board": {"width_mm": 50, "height_mm": 35},
        "placements": [
            {"designator": "U1", "component_type": "ic", "package": "DIP8",
             "x_mm": 10, "y_mm": 10, "footprint_width_mm": 9.0,
             "footprint_height_mm": 6.0, "rotation_deg": 0, "layer": "top"},
        ],
    }
    svg = pv.generate_svg(placement, netlist=netlist)
    assert svg.startswith("<svg")  # no crash; ratsnest nets skipped


def test_generate_svg_pad_render_exception(placement, monkeypatch):
    """Force get_footprint_def to raise -> pad loop except branch swallows it."""
    def boom(*a, **k):
        raise RuntimeError("nope")
    monkeypatch.setattr(pv, "get_footprint_def", boom)
    svg = pv.generate_svg(placement)
    assert svg.startswith("<svg")  # exception swallowed, still renders


# --------------------------------------------------------------------------
# _actions_html
# --------------------------------------------------------------------------

def test_actions_html_none():
    assert pv._actions_html(None, None, "") == ""


def test_actions_html_real(routed):
    html = pv._actions_html(routed, None, "http://localhost:1234")
    assert "Export KiCad" in html
    assert "Continue to DRC" in html


# --------------------------------------------------------------------------
# _drc_panel_html
# --------------------------------------------------------------------------

def test_drc_panel_none():
    assert pv._drc_panel_html(None) == ""


def test_drc_panel_passed(drc):
    html = pv._drc_panel_html(drc)
    assert "PASSED" in html
    assert "DFM:" in html
    # real report passes, so no violations <details> block
    assert "Violations (" not in html


def test_drc_panel_failed_with_violations():
    """Failed report with a check having >5 violations -> '+N more' row."""
    drc = {
        "passed": False,
        "manufacturer": "jlcpcb",
        "summary": "2 errors",
        "statistics": {"errors": 2, "warnings": 1},
        "checks": [
            {"rule": "min_clearance", "category": "electrical", "passed": False,
             "violations": [
                 {"severity": "error", "message": f"clash {i}"} for i in range(7)
             ]},
            {"rule": "annular_ring", "category": "dfm", "passed": False,
             "violations": [{"severity": "warning", "message": "thin ring"}]},
            {"rule": "ok_rule", "category": "current", "passed": True,
             "violations": []},
        ],
    }
    html = pv._drc_panel_html(drc)
    assert "FAILED" in html
    assert "Electrical: 0/1" in html
    assert "DFM: 0/1" in html
    assert "Current: 1/1" in html
    assert "Violations (" in html
    assert "+2 more" in html  # 7 violations, 5 shown -> +2
    assert "DFM: jlcpcb" in html  # manufacturer fallback when no dfm_profile


# --------------------------------------------------------------------------
# generate_html
# --------------------------------------------------------------------------

def test_generate_html_minimal(placement):
    html = pv.generate_html(placement)
    assert html.startswith("<!DOCTYPE html>")
    assert "<svg" in html
    assert "U1" in html  # component table row
    assert "Ratsnest" not in html  # no netlist -> no ratsnest legend


def test_generate_html_full(placement, netlist, bom, routed, drc):
    html = pv.generate_html(
        placement, netlist=netlist, bom=bom, routed=routed,
        title="t", api_url="http://x", drc_report=drc,
    )
    assert "Ratsnest" in html  # netlist present
    assert "Routing" in html  # routed stats
    assert "DRC Report" in html  # drc panel
    assert "Actions" in html  # actions (not embed mode)
    assert "exportKicad" in html  # kicad export


def test_generate_html_embed_mode(placement, routed, netlist):
    """embed_mode suppresses the Actions block."""
    full = pv.generate_html(placement, netlist=netlist, routed=routed, embed_mode=False)
    embed = pv.generate_html(placement, netlist=netlist, routed=routed, embed_mode=True)
    assert "Actions" in full
    assert "Actions" not in embed
    assert full != embed


# --------------------------------------------------------------------------
# main (CLI)
# --------------------------------------------------------------------------

def test_main_minimal(tmp_path):
    out = tmp_path / "board.html"
    rc = pv.main([str(FIXTURE_DIR / "blink_3_leds_dc_power_placement.json"),
                  "-o", str(out)])
    assert rc == 0
    assert out.exists()
    assert out.read_text().startswith("<!DOCTYPE html>")


def test_main_all_inputs_default_output(tmp_path):
    """All optional inputs + default output path (placement stem + _view.html)."""
    placement_src = FIXTURE_DIR / "blink_3_leds_dc_power_placement.json"
    local = tmp_path / "blink_3_leds_dc_power_placement.json"
    local.write_text(placement_src.read_text())
    rc = pv.main([
        str(local),
        "--netlist", str(FIXTURE_DIR / "blink_3_leds_dc_power_netlist.json"),
        "--bom", str(FIXTURE_DIR / "blink_3_leds_dc_power_bom.json"),
        "--routed", str(FIXTURE_DIR / "blink_3_leds_dc_power_routed.json"),
    ])
    assert rc == 0
    assert (tmp_path / "blink_3_leds_dc_power_placement_view.html").exists()


def test_main_open_flag(tmp_path, monkeypatch):
    """--open triggers the webbrowser branch (stubbed)."""
    opened = {}
    import webbrowser
    monkeypatch.setattr(webbrowser, "open", lambda url: opened.setdefault("url", url))
    out = tmp_path / "b.html"
    rc = pv.main([str(FIXTURE_DIR / "blink_3_leds_dc_power_placement.json"),
                  "-o", str(out), "--open"])
    assert rc == 0
    assert "url" in opened


# ==========================================================================
# netlist_viewer
# ==========================================================================

def test_parse_netlist(netlist):
    components, ports, nets = nv._parse_netlist(netlist)
    assert len(components) == 14
    assert len(nets) == 10
    assert ports


def test_layout_components_with_ics(netlist):
    components, ports, _ = nv._parse_netlist(netlist)
    layouts, comp_ports = nv._layout_components(components, ports)
    assert layouts
    # real netlist has an IC (U1) -> three-column path
    assert any(c.get("component_type") in ("ic", "voltage_regulator")
               for c in components.values())


def test_layout_components_no_ics():
    """Two-column path when there are no ICs."""
    components = {
        "c1": {"component_id": "c1", "designator": "J1", "component_type": "connector"},
        "c2": {"component_id": "c2", "designator": "R1", "component_type": "resistor"},
    }
    ports = {
        "p1": {"port_id": "p1", "component_id": "c1", "pin_number": 1, "name": "1"},
        "p2": {"port_id": "p2", "component_id": "c2", "pin_number": 1, "name": "A"},
    }
    layouts, comp_ports = nv._layout_components(components, ports)
    assert "c1" in layouts and "c2" in layouts


def test_build_pin_positions(netlist):
    components, ports, _ = nv._parse_netlist(netlist)
    layouts, comp_ports = nv._layout_components(components, ports)
    pin_pos = nv._build_pin_positions(layouts, comp_ports)
    assert pin_pos
    sides = {v[2] for v in pin_pos.values()}
    assert "left" in sides  # at least left side populated


def test_generate_netlist_html_real(netlist):
    html = nv.generate_netlist_html(netlist)
    assert html.startswith("<!DOCTYPE html>")
    assert "Netlist:" in html
    assert "U1" in html  # IC designator label
    assert "components" in html  # stats line


def test_generate_netlist_html_with_bom(netlist, bom):
    html_no_bom = nv.generate_netlist_html(netlist)
    html_bom = nv.generate_netlist_html(netlist, bom=bom)
    assert html_no_bom != html_bom  # bom adds specs to tooltips


def test_generate_netlist_html_multipoint_and_values():
    """Drive: 3+ point net (junction dot), 2-point net, long value truncation,
    and a net with <2 resolvable pins (skipped)."""
    netlist = {
        "project_name": "tiny",
        "elements": [
            {"element_type": "component", "component_id": "c1", "designator": "U1",
             "component_type": "ic", "value": "ATmega328P-with-a-very-long-value"},
            {"element_type": "component", "component_id": "c2", "designator": "R1",
             "component_type": "resistor", "value": "10k", "package": "0805"},
            {"element_type": "component", "component_id": "c3", "designator": "C1",
             "component_type": "capacitor", "value": "100nF"},
            {"element_type": "port", "port_id": "p1", "component_id": "c1",
             "pin_number": 1, "name": "VCC", "electrical_type": "power_in"},
            {"element_type": "port", "port_id": "p2", "component_id": "c2",
             "pin_number": 1, "name": "1"},
            {"element_type": "port", "port_id": "p3", "component_id": "c3",
             "pin_number": 1, "name": "1"},
            # 3-point net -> junction dot path
            {"element_type": "net", "net_id": "n1", "name": "VCC", "net_class": "power",
             "connected_port_ids": ["p1", "p2", "p3"]},
            # 2-point net -> direct bezier
            {"element_type": "net", "net_id": "n2", "name": "SIG", "net_class": "signal",
             "connected_port_ids": ["p1", "p2"]},
            # under-2 resolvable -> skipped
            {"element_type": "net", "net_id": "n3", "name": "NC", "net_class": "signal",
             "connected_port_ids": ["p1"]},
        ],
    }
    html = nv.generate_netlist_html(netlist)
    assert "<circle" in html  # junction dot or pin dots
    assert ".." in html  # long value truncated
    assert "VCC" in html


def test_generate_netlist_html_empty():
    """Empty netlist -> default canvas size, no crash."""
    html = nv.generate_netlist_html({"elements": []})
    assert html.startswith("<!DOCTYPE html>")
    assert "0 components" in html


def test_bezier_path_left_and_right():
    """_bezier_path is a closure; exercise both side branches via a net whose
    pins exit left and right."""
    # left-side pin -> control point goes left; right-side -> goes right.
    # Reuse generate via a netlist that places pins on both sides.
    components = {"c1": {"component_id": "c1", "designator": "U1",
                         "component_type": "ic"}}
    # IC with 4 pins -> 2 left, 2 right
    ports = {
        f"p{i}": {"port_id": f"p{i}", "component_id": "c1", "pin_number": i, "name": str(i)}
        for i in range(1, 5)
    }
    netlist = {
        "project_name": "x",
        "elements": (
            [{"element_type": "component", **components["c1"]}]
            + [{"element_type": "port", **ports[f"p{i}"]} for i in range(1, 5)]
            + [{"element_type": "net", "net_id": "n1", "name": "N",
                "net_class": "signal",
                "connected_port_ids": ["p1", "p2", "p3"]}]  # 3 pts incl both sides
        ),
    }
    html = nv.generate_netlist_html(netlist)
    assert "<path" in html


def test_generate_netlist_html_two_point_left_to_right():
    """2-point net where pin1 is left and pin2 is right -> hits both s2 branches.

    A 2-pin component lays pin1 on the left and pin2 on the right; a net
    connecting both spans left->right (c2x = px + OFFSET branch)."""
    netlist = {
        "project_name": "lr",
        "elements": [
            {"element_type": "component", "component_id": "c1", "designator": "R1",
             "component_type": "resistor"},
            {"element_type": "port", "port_id": "p1", "component_id": "c1",
             "pin_number": 1, "name": "1"},
            {"element_type": "port", "port_id": "p2", "component_id": "c1",
             "pin_number": 2, "name": "2"},
            {"element_type": "net", "net_id": "n1", "name": "N", "net_class": "signal",
             "connected_port_ids": ["p1", "p2"]},
        ],
    }
    html = nv.generate_netlist_html(netlist)
    assert "<path" in html
