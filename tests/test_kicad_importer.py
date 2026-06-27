"""Tests for exporters/kicad_importer.py — S-expr parsing + .kicad_pcb import."""

from __future__ import annotations

import sys
import textwrap
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from exporters.kicad_importer import (
    _tokenize,
    _parse_sexpr,
    parse_kicad_sexpr,
    _find_field,
    _find_all,
    _to_float,
    _LAYER_REVERSE,
    import_kicad_pcb,
)


# ---------------------------------------------------------------------------
# Minimal .kicad_pcb fixture: 2 segments (F.Cu/B.Cu), 1 via, 1 zone with poly.
# Net 1 = GND (resolves), net 2 = VCC (resolves), net 9 = unknown (no name map).
# ---------------------------------------------------------------------------

MINIMAL_PCB = textwrap.dedent("""\
    (kicad_pcb (version 20221018)
      (net 0 "")
      (net 1 "GND")
      (net 2 "VCC")
      (net 9 "ORPHAN")
      (segment (start 1.0 2.0) (end 3.0 2.0) (width 0.25) (layer "F.Cu") (net 1))
      (segment (start 5.0 5.0) (end 5.0 8.0) (width 0.3) (layer "B.Cu") (net 2))
      (via (at 4.0 4.0) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") (net 1))
      (zone (net 1) (net_name "GND") (layer "F.Cu")
        (filled_polygon (layer "F.Cu")
          (pts (xy 0 0) (xy 10 0) (xy 10 10) (xy 0 10))
        )
      )
    )
    """)


def _netlist():
    return {
        "elements": [
            {"element_type": "net", "net_id": "n1", "name": "GND"},
            {"element_type": "net", "net_id": "n2", "name": "VCC"},
            {"element_type": "net", "net_id": "n3", "name": "SIG"},  # never routed
            {"element_type": "component", "ref": "R1"},  # ignored (not a net)
        ]
    }


def _original():
    return {
        "version": "2.0",
        "project_name": "demo",
        "source_netlist": "demo.net",
        "source_bom": "demo.csv",
        "board": {"width_mm": 50},
        "placements": [{"ref": "R1"}],
        "silkscreen": [{"text": "R1"}],
        "routing": {"config": {"grid": 0.1}},
    }


# ---------------------------------------------------------------------------
# _tokenize
# ---------------------------------------------------------------------------

def test_tokenize_parens_bare_and_quoted():
    toks = _tokenize('(a "hello world" 1.5)')
    assert toks == ["(", "a", '"hello world"', "1.5", ")"]


def test_tokenize_escaped_quote_inside_string():
    toks = _tokenize(r'("a\"b")')
    assert toks == ["(", r'"a\"b"', ")"]


def test_tokenize_whitespace_skipped():
    assert _tokenize("  \t\n a \r ") == ["a"]


# ---------------------------------------------------------------------------
# _parse_sexpr / parse_kicad_sexpr
# ---------------------------------------------------------------------------

def test_parse_nesting_and_quote_strip():
    parsed = parse_kicad_sexpr('(outer (inner "txt") bare)')
    assert parsed == [["outer", ["inner", "txt"], "bare"]]


def test_parse_sexpr_unterminated_returns_partial():
    # No closing paren -> falls out of loop (covers the `return result, i` tail).
    tokens = _tokenize("(a b")
    result, pos = _parse_sexpr(tokens, 0)
    assert result == [["a", "b"]]
    assert pos == len(tokens)


# ---------------------------------------------------------------------------
# _find_field / _find_all
# ---------------------------------------------------------------------------

def test_find_field_hit_and_miss():
    sx = ["segment", ["start", "1", "2"], ["end", "3", "4"]]
    assert _find_field(sx, "start") == ["start", "1", "2"]
    assert _find_field(sx, "nope") is None


def test_find_field_non_list_returns_none():
    assert _find_field("not-a-list", "start") is None


def test_find_all_hits_and_non_list():
    sx = ["pts", ["xy", "0", "0"], ["xy", "1", "1"], "junk"]
    assert _find_all(sx, "xy") == [["xy", "0", "0"], ["xy", "1", "1"]]
    assert _find_all("not-a-list", "xy") == []


# ---------------------------------------------------------------------------
# _to_float
# ---------------------------------------------------------------------------

def test_to_float_valid_and_invalid():
    assert _to_float("1.5") == 1.5
    assert _to_float("nan-ish") == 0.0
    assert _to_float(None) == 0.0


def test_layer_reverse_map():
    assert _LAYER_REVERSE["F.Cu"] == "top"
    assert _LAYER_REVERSE["B.Cu"] == "bottom"


# ---------------------------------------------------------------------------
# import_kicad_pcb — full round trip
# ---------------------------------------------------------------------------

def test_import_round_trip(tmp_path):
    pcb = tmp_path / "board.kicad_pcb"
    pcb.write_text(MINIMAL_PCB, encoding="utf-8")

    result = import_kicad_pcb(pcb, _original(), _netlist())

    r = result["routing"]
    # Exactly 2 traces, 1 via, 1 fill.
    assert len(r["traces"]) == 2
    assert len(r["vias"]) == 1
    assert len(r["copper_fills"]) == 1

    # Trace coords + layer mapping + net resolution.
    t0 = r["traces"][0]
    assert t0["start_x_mm"] == 1.0 and t0["end_x_mm"] == 3.0
    assert t0["layer"] == "top"
    assert t0["net_id"] == "n1" and t0["net_name"] == "GND"
    assert r["traces"][1]["layer"] == "bottom"
    assert r["traces"][1]["net_id"] == "n2"

    # Via.
    v = r["vias"][0]
    assert v["x_mm"] == 4.0 and v["diameter_mm"] == 0.6 and v["drill_mm"] == 0.3
    assert v["from_layer"] == "top" and v["to_layer"] == "bottom"
    assert v["net_id"] == "n1"

    # Fill polygon.
    f = r["copper_fills"][0]
    assert f["layer"] == "top" and f["net_id"] == "n1" and f["net_name"] == "GND"
    assert f["polygons"] == [[[0.0, 0.0], [10.0, 0.0], [10.0, 10.0], [0.0, 10.0]]]

    # Unrouted = n3 (SIG, in netlist but never routed).
    assert r["unrouted_nets"] == ["n3"]

    # Statistics.
    s = r["statistics"]
    assert s["total_nets"] == 3
    assert s["routed_nets"] == 2
    assert s["unrouted_nets"] == 1
    assert s["via_count"] == 1
    assert s["total_trace_length_mm"] == 5.0  # 2.0 top + 3.0 bottom
    assert s["layer_usage"]["top_trace_length_mm"] == 2.0
    assert s["layer_usage"]["bottom_trace_length_mm"] == 3.0
    assert s["copper_fill_polygons"] == 1
    assert s["copper_fill_layers"] == ["top"]

    # Original passthrough fields preserved.
    assert result["version"] == "2.0"
    assert result["project_name"] == "demo"
    assert result["placements"] == [{"ref": "R1"}]
    assert r["config"] == {"grid": 0.1}


def test_import_unwrapped_and_empty_netlist(tmp_path):
    # No (kicad_pcb ...) wrapper, empty original/netlist -> defaults branch.
    pcb = tmp_path / "raw.kicad_pcb"
    pcb.write_text(
        '(segment (start 0 0) (end 1 1) (width 0.2) (layer "F.Cu") (net 5))\n',
        encoding="utf-8",
    )
    result = import_kicad_pcb(pcb, {}, {})

    # Defaults from empty original.
    assert result["version"] == "1.0"
    assert result["project_name"] == ""
    assert result["board"] == {}
    r = result["routing"]
    assert len(r["traces"]) == 1
    # net 5 has no declaration and no netlist mapping -> empty id/name.
    assert r["traces"][0]["net_id"] == ""
    assert r["traces"][0]["net_name"] == ""
    # total_nets == 0 -> completion 0.0 branch.
    assert r["statistics"]["completion_pct"] == 0.0
    assert r["statistics"]["total_nets"] == 0


def test_import_edge_branches(tmp_path):
    """Missing fields skipped; unknown layer -> 'top'; via/seg/zone with no net;
    net declared in pcb but absent from netlist -> name falls back to kicad name."""
    pcb_text = textwrap.dedent("""\
        (kicad_pcb
          (net 1 "GND")
          (segment (start 0 0) (end 1 0) (width 0.2) (layer "In1.Cu"))
          (segment (start 0 0) (end 1 0) (width 0.2) (layer "F.Cu"))
          (segment (start 0 0) (width 0.2) (layer "F.Cu"))
          (via (at 1 1) (size 0.6) (drill 0.3))
          (via (at 2 2) (size 0.6))
          (zone (net 1) (layer "F.Cu"))
          (zone (net 9))
          (zone (layer "B.Cu")
            (filled_polygon (layer "B.Cu") (pts (xy 0 0) (xy 1 1)))
          )
          (zone (net 1) (layer "F.Cu")
            (filled_polygon (layer "F.Cu"))
          )
        )
        """)
    pcb = tmp_path / "edge.kicad_pcb"
    pcb.write_text(pcb_text, encoding="utf-8")

    # netlist has GND but under a different id so kicad net 1 resolves to it.
    netlist = {"elements": [{"element_type": "net", "net_id": "g", "name": "GND"}]}
    result = import_kicad_pcb(pcb, {}, netlist)
    r = result["routing"]

    # First segment missing net AND unknown layer "In1.Cu" -> 'top', net "".
    assert r["traces"][0]["layer"] == "top"
    assert r["traces"][0]["net_id"] == ""
    # Two kept; the third (missing `end`) skipped via the required-fields guard.
    assert len(r["traces"]) == 2

    # First via complete (no net -> net 0). Second via missing drill -> skipped.
    assert len(r["vias"]) == 1
    assert r["vias"][0]["net_id"] == ""

    # Zones: #1 no filled_polygon -> skipped. #2 no layer field -> skipped
    # (covers the `if not layer_field: continue` guard). #3 has poly on B.Cu,
    # net absent. #4 has filled_polygon but no pts -> poly skipped -> zone dropped.
    assert len(r["copper_fills"]) == 1
    fill = r["copper_fills"][0]
    assert fill["layer"] == "bottom"
    assert fill["net_name"] == ""  # net absent -> kicad name "" -> stays empty

    # A zone whose net resolves would mark it routed; here net 1 -> "g".
    # GND ("g") is unrouted because no element actually carried it.
    assert "g" in r["unrouted_nets"]


def test_extract_nets_bad_entries(tmp_path):
    # (net x "name") with non-int num -> ValueError swallowed; short net ignored.
    pcb = tmp_path / "nets.kicad_pcb"
    pcb.write_text(
        '(kicad_pcb (net x "BAD") (net 1) (net 2 "OK")\n'
        '  (segment (start 0 0) (end 1 0) (width 0.2) (layer "F.Cu") (net 2)))\n',
        encoding="utf-8",
    )
    netlist = {"elements": [{"element_type": "net", "net_id": "ok", "name": "OK"}]}
    result = import_kicad_pcb(pcb, {}, netlist)
    assert result["routing"]["traces"][0]["net_id"] == "ok"
