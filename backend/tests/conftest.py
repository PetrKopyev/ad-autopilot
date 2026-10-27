import os
import sys
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

BACKEND = Path(__file__).resolve().parents[1]
TEST_ENVIRONMENT = {
    "DATABASE_URL": "postgresql+psycopg://test:test@invalid/test",
    "APP_BASE_URL": "https://testserver",
    "SECRET_KEY": "tests-only-secret-key-0123456789abcdef",
    "TOKEN_ENCRYPTION_KEY": "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=",
    "PLATFORM_MODE": "fake",
    "BUDGET_RESERVE_PCT": "8",
    "DEMO_LOGIN": "0",
}


@pytest.fixture(autouse=True)
def settings_environment(monkeypatch):
    # Unit tests use synthetic configuration and never need a local .env or database.
    for name, value in TEST_ENVIRONMENT.items():
        monkeypatch.setenv(name, value)


@pytest.fixture(autouse=True)
def process_memory():
    """Лимиты частоты и справочники регионов живут в памяти процесса: каждый тест начинает с чистых."""
    yield
    if limits := sys.modules.get("app.api.limits"):
        for limit in limits.RateLimit.limits:
            limit.reset()
    for name, cache in (("app.platforms.direct", "GEO_REGIONS"), ("app.platforms.vk", "REGIONS")):
        if module := sys.modules.get(name):
            getattr(module, cache).clear()


@pytest.fixture(scope="session", autouse=True)
def migrated_test_database(request):
    """Схему для db-тестов создают миграции: перед ними TEST_DATABASE_URL доводится до head."""
    url = os.environ.get("TEST_DATABASE_URL")
    if not url or not any(item.get_closest_marker("db") for item in request.session.items):
        return
    from alembic.config import Config

    from alembic import command

    with pytest.MonkeyPatch.context() as monkeypatch:
        for name, value in TEST_ENVIRONMENT.items():
            monkeypatch.setenv(name, value)
        engine = create_engine(url)
        try:
            with engine.begin() as connection:
                config = Config(str(BACKEND / "alembic.ini"))
                config.attributes["connection"] = connection
                command.upgrade(config, "head")
        finally:
            engine.dispose()


@pytest.fixture
def db_session():
    """Сессия PostgreSQL внутри внешней транзакции: commit кода — точка сохранения, в конце всё откатывается."""
    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL is required")
    engine = create_engine(url)
    try:
        with engine.connect() as connection:
            transaction = connection.begin()
            try:
                with Session(bind=connection, join_transaction_mode="create_savepoint", expire_on_commit=False) as db:
                    yield db
            finally:
                transaction.rollback()
    finally:
        engine.dispose()
