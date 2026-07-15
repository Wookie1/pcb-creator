"""Security-fix regression checks (path traversal, XSS escaping, JAR pin).

Covers the audit fixes: project-name/attachment traversal guards, viewer HTML
escaping, and the Freerouting JAR integrity check. Pure-function assertions plus
one monkeypatched download; no network, no sockets.
"""

import copy
import json
from pathlib import Path

import pytest

import mcp_server
import optimizers.freerouter as fr
from visualizers import placement_viewer, netlist_viewer

# Real render fixtures (absent in bare worktrees → those render tests skip).
_FIXTURE_DIR = Path(__file__).resolve().parent.parent / "projects" / "blink_3_leds_dc_power"
_XSS = 'X"><script>alert(1)</script>'


def _fixture(name):
    p = _FIXTURE_DIR / f"blink_3_leds_dc_power_{name}.json"
    if not p.exists():
        pytest.skip(f"render fixture {p} not present")
    return json.loads(p.read_text())


def test_validate_project_name_rejects_traversal():
    for bad in ["../etc", "..", "a/b", "a.b", "", "/x", "A_upper", "x y"]:
        with pytest.raises(ValueError):
            mcp_server._validate_project_name(bad)
    for good in ["led_blinker", "3v3_reg", "a", "x-y_1"]:
        mcp_server._validate_project_name(good)  # no raise


def test_safe_name_strips_directories():
    assert mcp_server._safe_name("../../evil") == "evil"
    assert mcp_server._safe_name("/etc/passwd") == "passwd"
    for bad in ["../../../.bashrc", "..", ".", "", "foo/"]:
        with pytest.raises(ValueError):
            mcp_server._safe_name(bad)


def test_project_dir_blocks_traversal(tmp_path, monkeypatch):
    monkeypatch.setattr(mcp_server, "_get_projects_dir", lambda: tmp_path)
    with pytest.raises(ValueError):
        mcp_server._project_dir("../../evil")
    assert mcp_server._project_dir("ok_proj") == tmp_path / "ok_proj"


def test_viewer_escaping_neutralizes_script():
    payload = '</text><script>alert(1)</script>'
    for esc in (placement_viewer._esc, netlist_viewer._esc):
        out = esc(payload)
        assert "<script" not in out
        assert "&lt;script&gt;" in out
    # '&' escaped first, not double-encoded
    assert placement_viewer._esc("a&b") == "a&amp;b"


def test_placement_viewer_escapes_designator():
    """A malicious designator must not survive as live markup in the SVG the
    approval server serves to the browser (unit test on _esc won't catch a
    render site that forgets to call it)."""
    placement = _fixture("placement")
    key = "placements" if "placements" in placement else "placement"
    placement = copy.deepcopy(placement)
    placement[key][0]["designator"] = _XSS
    svg = placement_viewer.generate_svg(placement)
    assert "<script>alert(1)" not in svg
    assert "&lt;script&gt;" in svg


def test_netlist_viewer_escapes_strings():
    netlist = copy.deepcopy(_fixture("netlist"))
    netlist["project_name"] = _XSS
    for c in (netlist.get("components") or [])[:1]:
        c["designator"] = _XSS
        c["value"] = _XSS
    for n in (netlist.get("nets") or [])[:1]:
        n["name"] = _XSS
    html = netlist_viewer.generate_netlist_html(netlist)
    assert "<script>alert(1)" not in html
    assert "&lt;script&gt;" in html


def test_freerouting_jar_rejected_on_hash_mismatch(tmp_path, monkeypatch):
    monkeypatch.setattr(fr, "DEFAULT_CACHE_DIR", tmp_path)

    def fake_download(url, dest):
        # attacker-substituted bytes that don't match the pinned hash
        with open(dest, "wb") as f:
            f.write(b"not the real jar")

    monkeypatch.setattr(fr.urllib.request, "urlretrieve", fake_download)
    with pytest.raises(RuntimeError, match="integrity"):
        fr.ensure_jar()
    # tampered file must not survive for `java -jar` to pick up
    assert not (tmp_path / fr.FREEROUTING_JAR_NAME).exists()


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
