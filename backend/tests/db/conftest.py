import pytest
from cryptography.fernet import Fernet


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line("markers", "db: requires an explicit TEST_DATABASE_URL and PostgreSQL")


@pytest.fixture
def test_settings(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://test:test@invalid/test")
    monkeypatch.setenv("APP_BASE_URL", "http://testserver")
    monkeypatch.setenv("SECRET_KEY", "test-only")
    monkeypatch.setenv("TOKEN_ENCRYPTION_KEY", Fernet.generate_key().decode("ascii"))
    monkeypatch.setenv("PLATFORM_MODE", "fake")

    from app.config import Settings

    settings = Settings()
    monkeypatch.setattr("app.config.settings", settings)
    return settings
