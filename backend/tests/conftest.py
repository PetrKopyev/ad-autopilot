import pytest


@pytest.fixture(autouse=True)
def settings_environment(monkeypatch):
    # Unit tests use synthetic configuration and never need a local .env or database.
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://test:test@invalid/test")
    monkeypatch.setenv("APP_BASE_URL", "https://testserver")
    monkeypatch.setenv("SECRET_KEY", "tests-only-secret-key-0123456789abcdef")
    monkeypatch.setenv("TOKEN_ENCRYPTION_KEY", "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=")
    monkeypatch.setenv("PLATFORM_MODE", "fake")
    monkeypatch.setenv("BUDGET_RESERVE_PCT", "8")
    monkeypatch.setenv("DEMO_LOGIN", "0")
