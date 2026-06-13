"""Tests for fine-pitch escape / fanout pre-routing (escape_router).

Geometry is exercised via an injected pad_map (a synthetic single-row 0.5mm
connector) so the tests are independent of footprint resolution.
"""

import math

from optimizers.escape_router import generate_escape_routing, EscapeConfig
from optimizers.pad_geometry import PadInfo


def _connector_padmap(n=16, pitch=0.5, x0=10.0, y=5.0, layer="top",
                      net_prefix="sig", leaving=True):
    """A single-row connector: n pads along x at `pitch`, row at y=`y`."""
    pads = {}
    for i in range(n):
        pid = f"cn1_{i + 1}"
        pads[pid] = PadInfo(
            port_id=pid, designator="CN1", pin_number=i + 1,
            net_id=f"{net_prefix}_{i + 1}",
            x_mm=x0 + i * pitch, y_mm=y,
            pad_width_mm=0.3, pad_height_mm=1.0, layer=layer)
    return pads


def _netlist_for(padmap, leaving=True):
    """Build a netlist where each pad's net also touches another component
    (so the net 'leaves' CN1) unless leaving=False."""
    elements = [{"element_type": "component", "component_id": "c_cn1",
                 "designator": "CN1", "component_type": "connector",
                 "package": "FPC", "value": "x"}]
    for pid, pad in padmap.items():
        elements.append({"element_type": "port", "port_id": pid,
                         "component_id": "c_cn1", "pin_number": pad.pin_number,
                         "name": str(pad.pin_number)})
    # a sink component to receive the leaving nets
    elements.append({"element_type": "component", "component_id": "c_u1",
                     "designator": "U1", "component_type": "ic",
                     "package": "QFP", "value": "x"})
    for i, (pid, pad) in enumerate(padmap.items()):
        sink = f"u1_{i + 1}"
        elements.append({"element_type": "port", "port_id": sink,
                         "component_id": "c_u1", "pin_number": i + 1,
                         "name": str(i + 1)})
        ports = [pid, sink] if leaving else [pid]
        elements.append({"element_type": "net", "net_id": pad.net_id,
                         "name": pad.net_id, "net_class": "signal",
                         "connected_port_ids": ports})
    return {"version": "1.0", "project_name": "t", "elements": elements}


def _placement():
    return {"version": "1.0", "project_name": "t",
            "board": {"width_mm": 40, "height_mm": 30}, "placements": []}


class TestEscapeGeneration:
    def test_generates_one_escape_per_signal_pad(self):
        pm = _connector_padmap(16)
        nl = _netlist_for(pm)
        out = generate_escape_routing(_placement(), nl, pad_map=pm)
        assert len(out["vias"]) == 16
        assert len(out["traces"]) == 16
        # each trace starts at a pad and ends at its via
        for t, v in zip(out["traces"], out["vias"]):
            assert (t["end_x_mm"], t["end_y_mm"]) == (v["x_mm"], v["y_mm"])
            assert t["net_id"] == v["net_id"]

    def test_vias_are_collision_free(self):
        cfg = EscapeConfig()
        pm = _connector_padmap(16, pitch=0.5)
        out = generate_escape_routing(_placement(), _netlist_for(pm),
                                      config=cfg, pad_map=pm)
        centers = [(v["x_mm"], v["y_mm"]) for v in out["vias"]]
        min_sep = cfg.via_diameter_mm + cfg.clearance_mm
        for i in range(len(centers)):
            for j in range(i + 1, len(centers)):
                d = math.hypot(centers[i][0] - centers[j][0],
                               centers[i][1] - centers[j][1])
                assert d >= min_sep - 1e-6, f"vias {i},{j} too close: {d:.3f}"

    def test_escapes_staggered_two_rows(self):
        """Adjacent pads escape to alternating distances (two via rows)."""
        pm = _connector_padmap(16, pitch=0.5, y=5.0)
        out = generate_escape_routing(_placement(), _netlist_for(pm), pad_map=pm)
        # row at y=5, board center y=15 → escape +y; vias at two distinct y's
        ys = sorted({round(v["y_mm"], 3) for v in out["vias"]})
        assert len(ys) == 2, f"expected two staggered via rows, got {ys}"

    def test_via_drops_to_configured_layer(self):
        pm = _connector_padmap(12, layer="top")
        out = generate_escape_routing(_placement(), _netlist_for(pm),
                                      config=EscapeConfig(drop_layer="bottom"),
                                      pad_map=pm)
        assert all(v["from_layer"] == "top" and v["to_layer"] == "bottom"
                   for v in out["vias"])


class TestGuards:
    def test_coarse_pitch_part_skipped(self):
        pm = _connector_padmap(16, pitch=2.54)  # 2.54mm header — not fine-pitch
        out = generate_escape_routing(_placement(), _netlist_for(pm), pad_map=pm)
        assert out["vias"] == [] and out["traces"] == []

    def test_few_pin_part_skipped(self):
        pm = _connector_padmap(4, pitch=0.5)  # below ESCAPE_MIN_PINS
        out = generate_escape_routing(_placement(), _netlist_for(pm), pad_map=pm)
        assert out["vias"] == []

    def test_excluded_and_internal_nets_not_escaped(self):
        pm = _connector_padmap(16, pitch=0.5)
        # exclude one net by name; make another internal (doesn't leave)
        nl = _netlist_for(pm, leaving=True)
        out = generate_escape_routing(_placement(), nl, pad_map=pm,
                                      exclude_nets=("sig_1",))
        assert all(v["net_id"] != "sig_1" for v in out["vias"])

    def test_internal_net_skipped(self):
        pm = _connector_padmap(16, pitch=0.5)
        nl = _netlist_for(pm, leaving=False)  # nets touch only CN1
        out = generate_escape_routing(_placement(), nl, pad_map=pm)
        assert out["vias"] == [], "nets that don't leave the part need no escape"

    def test_multirow_part_skipped(self):
        # two rows → not a single-row part (v1 leaves these to the autorouter)
        pm = _connector_padmap(8, pitch=0.5, y=5.0)
        pm2 = _connector_padmap(8, pitch=0.5, y=6.0, net_prefix="sigb")
        for k, v in pm2.items():
            pm[k + "b"] = v
        out = generate_escape_routing(_placement(), _netlist_for(pm), pad_map=pm)
        assert out["vias"] == []
