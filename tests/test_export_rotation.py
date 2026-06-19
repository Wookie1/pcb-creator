"""Exported KiCad pad positions must match build_pad_map for ROTATED parts.

KiCad rotates a footprint clockwise for a positive angle; the rest of the
pipeline (build_pad_map / DSN / SES) rotates pad offsets counter-clockwise. The
exporter therefore writes the NEGATED angle so KiCad reproduces build_pad_map's
layout. Without it, every 90/270 part's pads were 180 off and the router's
traces connected to the wrong pad (the morgan Pad-Track shorts).
"""
import math
import re

from optimizers.pad_geometry import build_pad_map
from exporters.kicad_exporter import export_kicad_pcb


def _kicad_cw(x, y, deg):
    """KiCad's clockwise rotation of a local pad offset by `deg`."""
    r = math.radians(deg)
    return (x * math.cos(r) + y * math.sin(r),
            -x * math.sin(r) + y * math.cos(r))


def _build(rot):
    netlist = {"version": "1.0", "project_name": "t", "elements": [
        {"element_type": "component", "component_id": "c_u1", "designator": "U1",
         "component_type": "transistor_npn", "value": "x", "package": "SOT-23"},
        *[{"element_type": "port", "port_id": f"p{p}", "component_id": "c_u1",
           "pin_number": p, "name": f"P{p}"} for p in (1, 2, 3)],
        {"element_type": "net", "net_id": "n1", "name": "N1",
         "connected_port_ids": ["p1"]},
    ]}
    routed = {"version": "1.0", "project_name": "t",
              "board": {"width_mm": 20, "height_mm": 20, "layers": 2},
              "placements": [{"designator": "U1", "package": "SOT-23",
                              "component_type": "transistor_npn",
                              "x_mm": 10.0, "y_mm": 10.0, "rotation_deg": rot,
                              "layer": "top", "footprint_width_mm": 3,
                              "footprint_height_mm": 3}],
              "routing": {"traces": [], "vias": [], "unrouted_nets": []}}
    return netlist, routed


def _exported_pads(text):
    """Parse {pin: (center_x, center_y, angle, local_dx, local_dy)} from a
    one-footprint .kicad_pcb."""
    # The footprint origin is the first (at ...) inside the (footprint block.
    # Its angle is OPTIONAL: when the part is at 0°, pcbnew (which pours zones
    # on export) normalises "(at x y 0)" down to "(at x y)", so a 3-number regex
    # would skip it and wrongly latch onto the next property's (at x y 0).
    fpidx = text.index("(footprint")
    fp = re.search(r'\(at (-?[\d.]+) (-?[\d.]+)(?: (-?[\d.]+))?\)', text[fpidx:])
    cx, cy, ang = float(fp[1]), float(fp[2]), float(fp[3] or 0)
    pads = {}
    for m in re.finditer(r'\(pad "(\d+)"[^()]*\(at ([\d.-]+) ([\d.-]+)\)', text):
        pads[int(m[1])] = (cx, cy, ang, float(m[2]), float(m[3]))
    return pads


class TestExportRotationMatchesPadMap:
    def _check(self, rot, tmp_path):
        netlist, routed = _build(rot)
        pm = {p.pin_number: (p.x_mm, p.y_mm)
              for p in build_pad_map(routed, netlist).values()}
        out = tmp_path / "t.kicad_pcb"
        export_kicad_pcb(routed, netlist, out)
        pads = _exported_pads(out.read_text())
        assert pads, "no pads parsed"
        for pin, (cx, cy, ang, dx, dy) in pads.items():
            rx, ry = _kicad_cw(dx, dy, ang)
            kx, ky = cx + rx, cy + ry
            bx, by = pm[pin]
            assert math.hypot(kx - bx, ky - by) < 0.05, (
                f"pin {pin} rot={rot}: kicad=({kx:.3f},{ky:.3f}) "
                f"padmap=({bx:.3f},{by:.3f})")

    def test_rot_0(self, tmp_path):   self._check(0, tmp_path)
    def test_rot_90(self, tmp_path):  self._check(90, tmp_path)
    def test_rot_180(self, tmp_path): self._check(180, tmp_path)
    def test_rot_270(self, tmp_path): self._check(270, tmp_path)


def _exported_pad_sizes(text):
    """Parse {pin: (width, height)} of the exported SMD pads."""
    sizes = {}
    for m in re.finditer(r'\(pad "(\d+)".*?\(size ([\d.]+) ([\d.]+)\)', text, re.S):
        sizes.setdefault(int(m[1]), (float(m[2]), float(m[3])))
    return sizes


class TestExportPadSizeMatchesPadMap:
    """KiCad does NOT rotate an SMD pad's rectangle with the footprint angle —
    only the pad position rotates. So the exporter must pre-swap pad w/h for
    90/270 parts (mirroring build_pad_map), or a rotated fine-pitch part's long
    pads overlap their neighbours (the morgan CN1 pad-pad shorts)."""

    def _check(self, rot, tmp_path):
        netlist, routed = _build(rot)
        pm = {p.pin_number: (p.pad_width_mm, p.pad_height_mm)
              for p in build_pad_map(routed, netlist).values()}
        out = tmp_path / "t.kicad_pcb"
        export_kicad_pcb(routed, netlist, out)
        sizes = _exported_pad_sizes(out.read_text())
        assert sizes, "no pad sizes parsed"
        for pin, (ew, eh) in sizes.items():
            bw, bh = pm[pin]
            assert abs(ew - bw) < 1e-3 and abs(eh - bh) < 1e-3, (
                f"pin {pin} rot={rot}: exported size ({ew}x{eh}) "
                f"!= padmap ({bw}x{bh})")

    def test_rot_0(self, tmp_path):   self._check(0, tmp_path)
    def test_rot_90(self, tmp_path):  self._check(90, tmp_path)
    def test_rot_270(self, tmp_path): self._check(270, tmp_path)
