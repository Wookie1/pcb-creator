"""Export BOM and pick-and-place (CPL) files as manufacturer-ready CSV.

BOM format follows JLCPCB conventions:
  Designator, Value, Package, Quantity, Description

Pick-and-place (CPL) format follows JLCPCB conventions:
  Designator, Val, Package, Mid X, Mid Y, Rotation, Layer
"""

from __future__ import annotations

import csv
from pathlib import Path


def export_bom_csv(
    bom: dict,
    output_path: Path,
) -> Path:
    """Export BOM as a manufacturer-compatible CSV file.

    Args:
        bom: BOM dict with "bom" array of component entries.
        output_path: Where to write the CSV.

    Returns:
        Path to the written file.
    """
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    items = bom.get("bom", [])

    with open(output_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow([
            "Designator", "Value", "Package", "Quantity",
            "Description", "Specs", "Notes",
        ])

        for item in sorted(items, key=lambda x: x.get("designator", "")):
            # Format specs as semicolon-separated key=value pairs
            specs = item.get("specs", {})
            specs_str = "; ".join(
                f"{k}={v}" for k, v in specs.items() if v
            ) if specs else ""

            writer.writerow([
                item.get("designator", ""),
                item.get("value", ""),
                item.get("package", ""),
                item.get("quantity", 1),
                item.get("description", ""),
                specs_str,
                item.get("notes", ""),
            ])

    return output_path


def export_pick_and_place(
    placement: dict,
    output_path: Path,
    bom: dict | None = None,
) -> Path:
    """Export pick-and-place (component placement list) as CSV.

    JLCPCB CPL format: Designator, Val, Package, Mid X, Mid Y, Rotation, Layer

    Includes fiducials (critical for pick-and-place machine alignment).

    Args:
        placement: Routed or placement dict containing "placements" array.
        output_path: Where to write the CSV.
        bom: Optional BOM dict for component values.

    Returns:
        Path to the written file.
    """
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    items = placement.get("placements", [])

    # Build value lookup from BOM
    bom_values: dict[str, str] = {}
    if bom:
        for entry in bom.get("bom", []):
            bom_values[entry.get("designator", "")] = entry.get("value", "")

    with open(output_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow([
            "Designator", "Val", "Package",
            "Mid X", "Mid Y", "Rotation", "Layer",
        ])

        for item in sorted(items, key=lambda x: x.get("designator", "")):
            des = item.get("designator", "")
            value = bom_values.get(des, item.get("value", ""))
            # Fiducials get a special value
            if item.get("component_type") == "fiducial":
                value = "Fiducial"

            writer.writerow([
                des,
                value,
                item.get("package", ""),
                f"{item['x_mm']:.4f}",
                f"{item['y_mm']:.4f}",
                item.get("rotation_deg", 0),
                item.get("layer", "top"),
            ])

    return output_path
