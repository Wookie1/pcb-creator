#!/usr/bin/env python3
"""Phase 0 spike: validate Freerouting end-to-end on an existing 2-layer project.

Usage:
    python scripts/spike_freerouting.py [project_dir]

Default project: projects/test_l298n_motor_driver
"""

import json
import sys
import time
from pathlib import Path

# Add repo root to path
REPO = Path(__file__).parent.parent
sys.path.insert(0, str(REPO))

from optimizers.freerouter import route_with_freerouting
from validators.validate_routing import validate_routing


def load(path: Path) -> dict:
    with open(path) as f:
        return json.load(f)


def main():
    project_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else REPO / "projects/test_l298n_motor_driver"
    name = project_dir.name

    placement_path = project_dir / f"{name}_placement.json"
    netlist_path = project_dir / f"{name}_netlist.json"

    if not placement_path.exists():
        print(f"ERROR: {placement_path} not found")
        sys.exit(1)
    if not netlist_path.exists():
        print(f"ERROR: {netlist_path} not found")
        sys.exit(1)

    placement = load(placement_path)
    netlist = load(netlist_path)

    print(f"\n=== Phase 0: Freerouting spike ===")
    print(f"Project : {name}")
    print(f"Comps   : {len(placement.get('placements', []))}")
    nets = [e for e in netlist.get('elements', []) if e.get('element_type') == 'net']
    print(f"Nets    : {len(nets)}")
    print()

    t0 = time.time()
    try:
        routed = route_with_freerouting(
            placement, netlist,
            timeout_s=120,
            exclude_nets=["GND"],
            dsn_config={
                "trace_width_mm": 0.25,
                "clearance_mm": 0.2,
                "via_drill_mm": 0.3,
                "via_diameter_mm": 0.6,
            },
        )
    except Exception as e:
        print(f"FAILED: {e}")
        sys.exit(1)

    elapsed = time.time() - t0
    stats = routed.get("routing", {}).get("statistics", {})
    print(f"\n--- Routing result ({elapsed:.1f}s) ---")
    print(f"Completion : {stats.get('completion_pct', 0):.1f}%")
    print(f"Routed nets: {stats.get('routed_nets', 0)}/{stats.get('total_nets', 0)}")
    print(f"Vias       : {stats.get('via_count', 0)}")
    traces = routed.get("routing", {}).get("traces", [])
    vias = routed.get("routing", {}).get("vias", [])
    layers_used = sorted({t.get("layer") for t in traces})
    via_layers = sorted({(v.get("from_layer"), v.get("to_layer")) for v in vias})
    print(f"Trace layers: {layers_used}")
    print(f"Via layer pairs: {via_layers}")

    # Apply copper fills (GND excluded from routing but connected via fill)
    print("\n--- Applying copper fills ---")
    try:
        from optimizers.router import apply_copper_fills, RouterConfig
        routed = apply_copper_fills(routed, netlist, RouterConfig())
        fill_layers = [f["layer"] for f in routed.get("routing", {}).get("copper_fills", [])]
        print(f"Fill layers: {fill_layers}")
    except Exception as e:
        print(f"  (copper fill skipped: {e})")

    # Routing validation — write to temp file since validate_routing takes paths
    import tempfile, os
    print("\n--- Validation ---")
    with tempfile.NamedTemporaryFile(mode='w', suffix='_routed.json', delete=False) as tf:
        json.dump(routed, tf)
        routed_tmp = tf.name
    with tempfile.NamedTemporaryFile(mode='w', suffix='_netlist.json', delete=False) as tf:
        json.dump(netlist, tf)
        netlist_tmp = tf.name
    try:
        result = validate_routing(routed_tmp, netlist_tmp)
    finally:
        os.unlink(routed_tmp)
        os.unlink(netlist_tmp)
    errors = result.get("errors", [])
    warnings = result.get("warnings", [])
    print(f"Errors   : {len(errors)}")
    print(f"Warnings : {len(warnings)}")
    for e in errors[:10]:
        print(f"  ERROR: {e}")
    for w in warnings[:5]:
        print(f"  WARN:  {w}")

    unrouted = routed.get("routing", {}).get("unrouted_nets", [])
    if unrouted:
        print(f"\nUnrouted nets ({len(unrouted)}): {unrouted[:10]}")

    passed = stats.get('completion_pct', 0) >= 95 and len(errors) == 0 and not unrouted
    print(f"\n{'PASS' if passed else 'FAIL'} — {'Freerouting is viable for 4-layer work' if passed else 'Issues found, review before proceeding'}")
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
