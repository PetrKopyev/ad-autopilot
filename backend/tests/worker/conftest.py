import pytest


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line("markers", "db: requires an explicit TEST_DATABASE_URL and PostgreSQL")
