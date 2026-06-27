"""Snap-to-pad helper for the built-in router (route_net / route_net_congestion).

Covers _snap_endpoints_to_pads, the shared tail extracted from both routers.
Also guards the via-snap branch's field names (from_layer/to_layer) — that
branch has no end-to-end coverage, and a typo there (v.layer_from) silently
raised AttributeError only when a via landed near a pad.
"""

from optimizers.router import _snap_endpoints_to_pads, TraceSegment, Via
from optimizers.pad_geometry import PadInfo


def _pad(x, y, layer="top"):
    return PadInfo(
        port_id="p", designator="R1", pin_number=1, net_id="net_0",
        x_mm=x, y_mm=y, pad_width_mm=1.0, pad_height_mm=1.0, layer=layer,
    )


def test_trace_endpoints_snap_to_pad_centres():
    pad_a, pad_b = _pad(1.0, 1.0), _pad(5.0, 5.0)
    # Endpoints grid-snapped slightly off the true pad centres.
    traces = [
        TraceSegment(0.95, 1.05, 3.0, 3.0, 0.2, "top", "net_0", "N0"),
        TraceSegment(3.0, 3.0, 5.05, 4.95, 0.2, "top", "net_0", "N0"),
    ]
    _snap_endpoints_to_pads(traces, [], pad_a, pad_b, grid_res=0.2)

    assert (traces[0].start_x_mm, traces[0].start_y_mm) == (1.0, 1.0)
    assert (traces[-1].end_x_mm, traces[-1].end_y_mm) == (5.0, 5.0)
    # Interior endpoints untouched.
    assert (traces[0].end_x_mm, traces[0].end_y_mm) == (3.0, 3.0)


def test_via_near_pad_snaps_and_preserves_layer_span():
    pad_a, pad_b = _pad(1.0, 1.0), _pad(5.0, 5.0)
    # Via within snap_tol (grid_res*1.5 = 0.3) of pad_a; layer span must survive.
    via = Via(1.1, 0.9, 0.3, 0.6, "top", "bottom", "net_0", "N0")
    vias = [via]
    _snap_endpoints_to_pads([], vias, pad_a, pad_b, grid_res=0.2)

    assert (vias[0].x_mm, vias[0].y_mm) == (1.0, 1.0)        # snapped to pad_a
    assert vias[0].from_layer == "top"                       # span preserved
    assert vias[0].to_layer == "bottom"                      # (regression: was AttributeError)


def test_via_far_from_pads_is_left_alone():
    pad_a, pad_b = _pad(1.0, 1.0), _pad(5.0, 5.0)
    via = Via(3.0, 3.0, 0.3, 0.6, "top", "bottom", "net_0", "N0")
    vias = [via]
    _snap_endpoints_to_pads([], vias, pad_a, pad_b, grid_res=0.2)

    assert (vias[0].x_mm, vias[0].y_mm) == (3.0, 3.0)        # unchanged


if __name__ == "__main__":
    test_trace_endpoints_snap_to_pad_centres()
    test_via_near_pad_snaps_and_preserves_layer_span()
    test_via_far_from_pads_is_left_alone()
    print("ok")
