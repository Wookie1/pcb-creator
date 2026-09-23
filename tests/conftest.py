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


@pytest.fixture(autouse=True)
def _suite_owns_footprint_lookup(monkeypatch):
    """Stages auto-install the real KiCad lookup when nothing configured one.
    Tests keep their explicit (usually built-in-only) lookup instead, so mark it
    configured; tests of the auto-install flip this back to False."""
    import optimizers.pad_geometry as pg
    monkeypatch.setattr(pg, "_lookup_configured", True)
