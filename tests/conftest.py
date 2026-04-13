import pytest


@pytest.fixture(autouse=True)
def env_setup(monkeypatch):
    """Configura variables de entorno para tests."""
    monkeypatch.setenv("SUPABASE_URL", "https://test.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_KEY", "test-key")
    monkeypatch.setenv("DRY_RUN", "true")
    monkeypatch.setenv("LOG_LEVEL", "DEBUG")
