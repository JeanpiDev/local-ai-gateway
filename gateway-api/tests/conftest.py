"""Configuración común de los tests."""
import pytest


@pytest.fixture(autouse=True)
def guard_enabled(monkeypatch):
    """Activa el master switch del guard para los tests (etapas que lo consultan)."""
    monkeypatch.setenv("GATEWAY_GUARD_ENABLED", "true")
    from app.config import get_settings
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()
