"""Orderable BOM: curated part-number lookup, resolver tiers, and CSV columns."""

import csv
import json

from exporters.bom_csv_exporter import export_bom_csv
from orchestrator.gather.curated_specs import lookup_part_number
from orchestrator.quoting import resolve_part_numbers


# ---------------------------------------------------------------------------
# Curated lookup
# ---------------------------------------------------------------------------

def test_resistor_matches_on_parsed_value_and_package():
    part = lookup_part_number("resistor", "220ohm", "0805")
    assert part["lcsc"] == "C17557"
    # Same electrical value, different notation
    assert lookup_part_number("resistor", "220 ohm", "0805")["lcsc"] == "C17557"
    # Same value, other package → different part
    assert lookup_part_number("resistor", "220ohm", "0603")["lcsc"] == "C22962"


def test_capacitor_matches_on_farads():
    assert lookup_part_number("capacitor", "100nF", "0603")["lcsc"] == "C14663"
    assert lookup_part_number("capacitor", "0.1uF", "0603")["lcsc"] == "C14663"


def test_named_part_rejects_conflicting_package():
    assert lookup_part_number("ic", "NE555", "SOIC-8")["lcsc"] == "C7593"
    # The curated NE555 is the SOIC-8 part — a DIP-8 BOM line must not get it.
    assert lookup_part_number("ic", "NE555", "DIP-8") is None


def test_unknown_values_return_none():
    assert lookup_part_number("resistor", "217ohm", "0805") is None
    assert lookup_part_number("resistor", "garbage", "0805") is None
    assert lookup_part_number("ic", "XYZZY99", "SOIC-8") is None
    assert lookup_part_number("capacitor", "garbage", "0603") is None
    assert lookup_part_number("capacitor", "47uF", "0603") is None


def test_named_part_suffix_variant_matches():
    # NE555P → NE555 via the startswith fallback (same behavior lookup_specs has)
    assert lookup_part_number("ic", "NE555P", "SOIC-8")["lcsc"] == "C7593"


# ---------------------------------------------------------------------------
# Resolver
# ---------------------------------------------------------------------------

def _bom(*items):
    return {"bom": [dict(i) for i in items]}


def test_resolver_fills_curated_and_counts():
    bom = _bom({"designator": "R1", "component_type": "resistor",
                "value": "10kohm", "package": "0805", "quantity": 1},
               {"designator": "R2", "component_type": "resistor",
                "value": "217ohm", "package": "0805", "quantity": 1})
    assert resolve_part_numbers(bom) == 1
    assert bom["bom"][0]["lcsc"] == "C17414"
    assert bom["bom"][0]["manufacturer"] == "UNI-ROYAL"
    assert "lcsc" not in bom["bom"][1]


def test_resolver_never_overwrites_existing_values():
    bom = _bom({"designator": "R1", "component_type": "resistor",
                "value": "10kohm", "package": "0805", "quantity": 1,
                "lcsc": "C999999", "mpn": "USER-CHOSEN"})
    assert resolve_part_numbers(bom) == 0
    assert bom["bom"][0]["lcsc"] == "C999999"
    assert bom["bom"][0]["mpn"] == "USER-CHOSEN"


def test_resolver_uses_cache_for_non_curated_parts(tmp_path, monkeypatch):
    cache_path = tmp_path / "cache.json"
    monkeypatch.setenv("PCB_COMPONENT_CACHE_PATH", str(cache_path))
    # Seed the cache the way an agent/user would (hand-added part number).
    cache_path.write_text(json.dumps({"footprints": {}, "specs": {
        "PART:IC:ESP32-WROOM-32:SMD-38": {
            "lcsc": "C82899", "mpn": "ESP32-WROOM-32",
            "source": "agent", "needs_review": True},
    }}))
    bom = _bom({"designator": "U1", "component_type": "ic",
                "value": "ESP32-WROOM-32", "package": "SMD-38", "quantity": 1})
    assert resolve_part_numbers(bom) == 1
    assert bom["bom"][0]["lcsc"] == "C82899"


# ---------------------------------------------------------------------------
# CSV
# ---------------------------------------------------------------------------

def test_bom_csv_has_lcsc_part_column(tmp_path):
    bom = _bom({"designator": "R1", "component_type": "resistor",
                "value": "10kohm", "package": "0805", "quantity": 1,
                "lcsc": "C17414", "mpn": "0805W8F1002T5E",
                "manufacturer": "UNI-ROYAL"})
    out = export_bom_csv(bom, tmp_path / "bom.csv")
    rows = list(csv.DictReader(out.open()))
    assert rows[0]["LCSC Part #"] == "C17414"
    assert rows[0]["MPN"] == "0805W8F1002T5E"
    assert rows[0]["Manufacturer"] == "UNI-ROYAL"
