import pytest


@pytest.fixture(autouse=True)
def _isolated_component_cache(tmp_path, monkeypatch):
    """Keep the suite from reading/writing the real ~/.pcb-creator cache.

    Part-number resolution (orchestrator.quoting) runs inside run_export and
    writes through ComponentCache; without this, export tests would mutate the
    developer's live component cache. Tests that need a specific cache path
    monkeypatch their own on top.
    """
    monkeypatch.setenv("PCB_COMPONENT_CACHE_PATH",
                       str(tmp_path / "component_cache.json"))
