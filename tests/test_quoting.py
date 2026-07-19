"""Fab quote: deterministic price-tier math and the project quote report."""

import json

import pytest

from orchestrator import quoting
from orchestrator.quoting import estimate_board_price, quote_project


# ---------------------------------------------------------------------------
# estimate_board_price
# ---------------------------------------------------------------------------

def test_base_tiers_and_estimate_flag():
    q2 = estimate_board_price(50, 50, 2, qty=5)
    q4 = estimate_board_price(50, 50, 4, qty=5)
    assert q2["estimate"] is True and "source" in q2
    assert q4["total"] > q2["total"]          # 4-layer costs more
    assert q2["per_board"] == round(q2["total"] / 5, 2)


def test_quantity_scales_monotonically():
    totals = [estimate_board_price(50, 50, 2, qty=q)["total"]
              for q in (5, 10, 20, 30, 200)]
    assert totals == sorted(totals)
    # More boards is never cheaper per board... but per-board must not rise.
    per = [estimate_board_price(50, 50, 2, qty=q)["per_board"]
           for q in (5, 30, 200)]
    assert per[0] >= per[1] >= per[2]


def test_oversize_surcharge():
    small = estimate_board_price(100, 100, 2, qty=5)["total"]
    big = estimate_board_price(150, 150, 2, qty=5)["total"]
    assert big > small


def test_price_table_override(tmp_path, monkeypatch):
    table = dict(quoting._DEFAULT_PRICE_TABLE)
    table["base_price"] = {"2": 100.0, "4": 200.0}
    p = tmp_path / "prices.json"
    p.write_text(json.dumps(table))
    monkeypatch.setenv("PCB_PRICE_TABLE", str(p))
    assert estimate_board_price(50, 50, 2, qty=5)["total"] == 100.0
    # A broken override falls back to the default instead of failing the quote.
    p.write_text("not json")
    assert estimate_board_price(50, 50, 2, qty=5)["total"] < 100.0


# ---------------------------------------------------------------------------
# quote_project
# ---------------------------------------------------------------------------

@pytest.fixture()
def project(tmp_path):
    pdir = tmp_path / "proj"
    pdir.mkdir()
    (pdir / "quoteme_bom.json").write_text(json.dumps({"bom": [
        {"designator": "R1", "component_type": "resistor", "value": "10kohm",
         "package": "0805", "quantity": 1},
        {"designator": "J1", "component_type": "connector", "value": "weird",
         "package": "CUSTOM-9", "quantity": 1},
    ]}))
    (pdir / "quoteme_placement.json").write_text(json.dumps(
        {"board": {"width_mm": 40, "height_mm": 30, "layers": 2}}))
    return pdir


def test_quote_offline(project):
    r = quote_project(project, "quoteme", qty=5, live=False)
    assert r["success"] is True
    assert r["board_estimate"]["layers"] == 2
    by_des = {p["designator"]: p for p in r["parts"]}
    assert by_des["R1"]["lcsc"] == "C17414"      # curated fill happened
    assert r["unresolved"] == ["J1"]
    assert r["parts_total_usd"] is None          # no live prices offline


def test_quote_live_prices_and_mpn_cross_check(project, monkeypatch):
    def fake_info(lcsc_id):
        assert lcsc_id == "C17414"
        return {"lcsc": lcsc_id, "mpn": "SOMETHING-ELSE", "manufacturer": "X",
                "stock": 5000, "unit_price_usd": 0.002, "min_order": 100,
                "basic_part": True}
    monkeypatch.setattr("orchestrator.gather.easyeda_lookup.fetch_part_info",
                        fake_info)
    r = quote_project(project, "quoteme", qty=5, live=True)
    by_des = {p["designator"]: p for p in r["parts"]}
    assert by_des["R1"]["stock"] == 5000
    assert r["parts_total_usd"] == round(0.002 * 1 * 5, 2)
    # Fetched MPN disagrees with the BOM's — flagged for human review.
    assert by_des["R1"]["needs_review"] is True
    assert any("mpn" in n.lower() or "!=" in n for n in r["notes"])


def test_quote_flags_out_of_stock(project, monkeypatch):
    monkeypatch.setattr(
        "orchestrator.gather.easyeda_lookup.fetch_part_info",
        lambda lcsc_id: {"lcsc": lcsc_id, "mpn": "0805W8F1002T5E",
                         "manufacturer": "UNI-ROYAL", "stock": 0,
                         "unit_price_usd": None, "min_order": 100,
                         "basic_part": True})
    r = quote_project(project, "quoteme", qty=5, live=True)
    assert any("out of stock" in n for n in r["notes"])
    assert r["parts_total_usd"] is None          # no price came back


def test_quote_live_unavailable_falls_back(project, monkeypatch):
    monkeypatch.setattr(
        "orchestrator.gather.easyeda_lookup.fetch_part_info",
        lambda lcsc_id: None)
    r = quote_project(project, "quoteme", qty=5, live=True)
    assert r["success"] is True
    assert any("unavailable" in n for n in r["notes"])


def test_quote_without_bom_or_netlist(tmp_path):
    (tmp_path / "empty").mkdir()
    r = quote_project(tmp_path / "empty", "empty")
    assert r["success"] is False


def test_quote_synthesizes_bom_from_netlist(tmp_path):
    pdir = tmp_path / "n"
    pdir.mkdir()
    (pdir / "n_netlist.json").write_text(json.dumps({"elements": [
        {"element_type": "component", "component_id": "c1", "designator": "R1",
         "component_type": "resistor", "value": "1kohm", "package": "0603"},
    ]}))
    r = quote_project(pdir, "n", live=False)
    assert r["success"] is True
    assert r["parts"][0]["lcsc"] == "C21190"     # resolved via _bom_from_netlist
    assert r["board_estimate"] is None           # no placement yet
