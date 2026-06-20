"""Freerouting subprocess-lifecycle hygiene: never orphan a JVM.

A plain Popen child does not die with its parent — on an abrupt exit it
reparents to init and keeps holding the JVM heap (the documented "port
conflict / weird project state after a crash"). These tests pin the two
defences: a route-start reap of orphaned JVMs, and a registry that terminates
live JVMs on graceful shutdown.
"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import optimizers.freerouter as fr


class TestReapOrphans:
    """_reap_orphaned_freerouting must target ONLY orphaned (ppid 1) JVMs
    running our jar — never a live route, never arbitrary java, never itself."""

    def _run_reap(self, monkeypatch, ps_table):
        killed = []
        monkeypatch.setattr(fr.os, "name", "posix")
        monkeypatch.setattr(fr.subprocess, "run",
                            lambda *a, **k: type("R", (), {"stdout": ps_table})())
        monkeypatch.setattr(fr.time, "sleep", lambda *_: None)
        monkeypatch.setattr(fr.os, "kill",
                            lambda pid, sig: killed.append((pid, sig)))
        n = fr._reap_orphaned_freerouting()
        return n, {p for p, _ in killed}

    def test_kills_only_orphaned_our_jar(self, monkeypatch):
        me = fr.os.getpid()
        jar = "/home/u/.cache/pcb-creator/freerouting-2.1.0.jar"
        ps = "\n".join([
            f"100 1 /usr/bin/java -Xmx2048m -jar {jar} -de /tmp/x/input.dsn",   # orphan, ours -> KILL
            f"200 500 /usr/bin/java -jar {jar} -de /tmp/y/input.dsn",            # live owner -> skip
            "300 1 /usr/bin/java -jar /opt/other/app.jar",                       # orphan, not ours -> skip
            "400 1 /usr/bin/python3 some_script.py",                             # orphan, not java -> skip
            f"{me} 1 /usr/bin/java -jar {jar}",                                  # ourselves -> skip
        ])
        n, killed = self._run_reap(monkeypatch, ps)
        assert n == 1
        assert killed == {100}
        assert 200 not in killed and 300 not in killed
        assert 400 not in killed and me not in killed

    def test_no_orphans_is_noop(self, monkeypatch):
        jar = "freerouting-2.1.0.jar"
        ps = f"200 500 java -jar {jar} -de /tmp/y/input.dsn"   # only a live route
        n, killed = self._run_reap(monkeypatch, ps)
        assert n == 0 and killed == set()

    def test_version_independent_match(self, monkeypatch):
        ps = "100 1 java -jar /c/freerouting-2.2.4.jar -de /tmp/x.dsn"
        n, killed = self._run_reap(monkeypatch, ps)
        assert n == 1 and killed == {100}


class TestTerminateAndRegistry:
    """Live children are terminated; the registry cleanup kills them on exit."""

    def test_terminate_kills_live_child(self):
        p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        assert p.poll() is None
        fr._terminate_proc(p)
        assert p.poll() is not None        # actually dead, not just abandoned

    def test_terminate_is_idempotent_on_dead(self):
        p = subprocess.Popen([sys.executable, "-c", "pass"])
        p.wait()
        fr._terminate_proc(p)              # must not raise on an already-dead proc
        assert p.poll() is not None

    def test_registry_cleanup_terminates_all(self):
        p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        fr._register_proc(p)
        try:
            assert p in fr._LIVE_PROCS
            fr._cleanup_all_procs()
            time.sleep(0.5)
            assert p.poll() is not None
        finally:
            fr._unregister_proc(p)
            if p.poll() is None:
                p.kill()
