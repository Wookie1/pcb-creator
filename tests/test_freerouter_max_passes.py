"""The pass budget must actually bound Freerouting.

Freerouting 2.1.0 accepts `-mp` and ignores it in headless mode: GlobalSettings
parses it into RouterSettings.maxPasses, but BatchAutorouter bounds its loop
with get_stop_pass_no() — a private transient field defaulting to 999 that only
the GUI classes ever set. An unbounded run matters because 2.1.0 writes the SES
only at end-of-passes, so a board it cannot finish burns the whole timeout and
returns *nothing* rather than a partial route.

route_with_freerouting therefore also sets FREEROUTING__ROUTER__STOP_PASS_NO,
which reaches the field the loop reads.
"""

import pytest

from optimizers import freerouter

# Just enough board for export_dsn to accept it — the JVM never runs here.
# Kept inline so the test does not depend on a generated project dir.
_PLACEMENT = {
    "version": "1.0", "project_name": "mp",
    "board": {"width_mm": 20, "height_mm": 20, "layers": 2},
    "placements": [
        {"designator": f"R{i}", "package": "0805", "component_type": "resistor",
         "x_mm": 3 + 3.5 * i, "y_mm": 5 + 6 * (i % 2), "rotation": 0,
         "side": "top"}
        for i in range(1, 6)
    ],
}
_NETLIST = {
    "version": "1.0", "project_name": "mp",
    "elements": (
        [{"element_type": "component", "component_id": f"c{i}",
          "designator": f"R{i}", "component_type": "resistor",
          "value": "1k", "package": "0805"} for i in range(1, 6)]
        + [{"element_type": "net", "net_id": f"n{i}", "name": f"N{i}",
            "connections": [{"component_id": f"c{i}", "pin": "2"},
                            {"component_id": f"c{i + 1}", "pin": "1"}]}
           for i in range(1, 5)]
    ),
}


def _fixture(name: str) -> dict:
    return _PLACEMENT if name == "placement" else _NETLIST


def test_pass_budget_reaches_the_field_freerouting_actually_reads(monkeypatch,
                                                                 tmp_path):
    """-mp alone is inert; the env override must go with it."""
    captured = {}

    class _Stop(RuntimeError):
        pass

    def fake_popen(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["env"] = kwargs.get("env")
        raise _Stop

    jar = tmp_path / "freerouting-2.1.0.jar"
    jar.write_bytes(b"")
    monkeypatch.setattr(freerouter, "ensure_java", lambda: "java")
    monkeypatch.setattr(freerouter, "ensure_jar", lambda p=None: jar)
    monkeypatch.setattr(freerouter, "_reap_orphaned_freerouting", lambda: None)
    monkeypatch.setattr(freerouter.subprocess, "Popen", fake_popen)

    with pytest.raises(_Stop):
        freerouter.route_with_freerouting(
            _fixture("placement"), _fixture("netlist"), max_passes=7)

    assert "-mp" in captured["cmd"], "keep -mp: it is the real bound on 2.2.4+"
    assert captured["cmd"][captured["cmd"].index("-mp") + 1] == "7"
    env = captured["env"]
    assert env["FREEROUTING__ROUTER__STOP_PASS_NO"] == "7"
    # A copy of the environment, not a bare dict — the JVM still needs PATH etc.
    assert len(env) > 1


# No live counterpart: any board small enough to belong in a test file finishes
# inside a plausible budget, so "stopped at the budget" is indistinguishable from
# "finished" — the assertion would pass with the override removed. What protects
# the live behaviour is the SHA-pinned JAR, so it cannot change underneath us.
# When FREEROUTING_VERSION is bumped, re-verify by hand on a board that needs
# several passes: route it with max_passes=2 and confirm the parsed pass numbers
# stop at 2 (on 2.1.0 without the override the same board reached pass 5).
