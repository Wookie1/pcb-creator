"""Exported KiCad pad positions must match build_pad_map for ROTATED parts,
in the correct frame, and must NOT be a mirror image of the real part.

pcb-creator's internal frame (routed.json, Gerbers) is Y-UP; KiCad's file frame
is Y-DOWN. The exporter writes y_kicad = board_h - y, negates footprint-local
pad Y, and writes the rotation angle as-is. An earlier version wrote internal Y
straight into the file and negated the angle: pads then matched positions (the
morgan 90/270 fix) but the whole .kicad_pcb was a mirror image — SOT-23 pins
1<->2 swapped as KiCad displayed it.
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
            by = routed["board"]["height_mm"] - by   # internal Y-up -> KiCad Y-down
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


def _winding(a, b, c):
    """>0 = counter-clockwise in a Y-UP (physical top view) frame."""
    return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])


class TestExportIsNotMirrored:
    """Pin winding of the exported part, as KiCad places it, must match the
    real device. Official KiCad SOT-23 (and every IC): pins run COUNTER-
    clockwise viewed from the top. A mirrored export runs them clockwise."""

    def _check(self, rot, tmp_path):
        netlist, routed = _build(rot)
        out = tmp_path / "t.kicad_pcb"
        export_kicad_pcb(routed, netlist, out)
        pads = _exported_pads(out.read_text())
        world = {}
        for pin, (cx, cy, ang, dx, dy) in pads.items():
            rx, ry = _kicad_cw(dx, dy, ang)
            world[pin] = (cx + rx, -(cy + ry))   # KiCad Y-down -> physical Y-up
        assert _winding(world[1], world[2], world[3]) > 0, (
            f"rot={rot}: exported SOT-23 pins run clockwise — mirror image")

    def test_rot_0(self, tmp_path):   self._check(0, tmp_path)
    def test_rot_90(self, tmp_path):  self._check(90, tmp_path)
    def test_rot_180(self, tmp_path): self._check(180, tmp_path)
    def test_rot_270(self, tmp_path): self._check(270, tmp_path)


def test_export_reproduces_official_kicad_footprints(tmp_path):
    """With the real KiCad library, an exported part at 0 deg must carry the
    OFFICIAL footprint's pad positions exactly (KiCad frame) — for the SOT-23,
    SOT-23-5 and SOIC-14 on parking_flasher. The old export wrote them
    Y-negated: a mirror image of the real device in KiCad."""
    import pathlib
    import pytest
    from exporters.kicad_mod_parser import KiCadLibraryIndex
    from optimizers import pad_geometry
    lib = pathlib.Path("/Applications/KiCad/KiCad.app/Contents/SharedSupport/footprints")
    if not lib.is_dir():
        pytest.skip("no system KiCad footprint library")
    prev = (pad_geometry._default_kicad_index, pad_geometry._default_cache,
            pad_geometry._default_custom_index)
    pad_geometry.configure_lookup(kicad_index=KiCadLibraryIndex(str(lib)))
    try:
        cases = {"SOT-23": ("Package_TO_SOT_SMD.pretty/SOT-23.kicad_mod", 3),
                 "SOT-23-5": ("Package_TO_SOT_SMD.pretty/SOT-23-5.kicad_mod", 5),
                 "SOIC-14": ("Package_SO.pretty/SOIC-14_3.9x8.7mm_P1.27mm.kicad_mod", 14)}
        for pkg, (fname, n) in cases.items():
            official = {}
            for m in re.finditer(r'\(pad "(\d+)" smd \w+\s*\(at ([-\d.]+) ([-\d.]+)',
                                 (lib / fname).read_text()):
                official[int(m[1])] = (float(m[2]), float(m[3]))
            netlist = {"elements": [
                {"element_type": "component", "component_id": "c", "designator": "U1",
                 "component_type": "ic", "value": "x", "package": pkg},
                *[{"element_type": "port", "port_id": f"p{p}", "component_id": "c",
                   "pin_number": p, "name": f"P{p}"} for p in range(1, n + 1)]]}
            routed = {"board": {"width_mm": 30, "height_mm": 30, "layers": 2},
                      "placements": [{"designator": "U1", "package": pkg,
                                      "component_type": "ic", "x_mm": 15.0,
                                      "y_mm": 15.0, "rotation_deg": 0, "layer": "top",
                                      "footprint_width_mm": 5, "footprint_height_mm": 9}],
                      "routing": {"traces": [], "vias": [], "unrouted_nets": []}}
            out = tmp_path / f"{pkg}.kicad_pcb"
            export_kicad_pcb(routed, netlist, out)
            pads = _exported_pads(out.read_text())
            # footprints are re-centred on load, so compare offsets relative to pin 1
            ox, oy = official[1]; ex, ey = pads[1][3], pads[1][4]
            for pin, (x, y) in official.items():
                dx, dy = pads[pin][3] - ex, pads[pin][4] - ey
                assert abs(dx - (x - ox)) < 0.01 and abs(dy - (y - oy)) < 0.01, (
                    f"{pkg} pin {pin}: exported {dx:.3f},{dy:.3f} vs official "
                    f"{x - ox:.3f},{y - oy:.3f} — mirrored/rotated export")
    finally:
        pad_geometry.configure_lookup(kicad_index=prev[0], cache=prev[1],
                                      custom_index=prev[2])
