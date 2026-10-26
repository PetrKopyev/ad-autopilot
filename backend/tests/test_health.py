from collections.abc import Iterator
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import OperationalError


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://test:test@invalid/test")
    monkeypatch.setenv("APP_BASE_URL", "http://testserver")
    monkeypatch.setenv("SECRET_KEY", "test-only")
    monkeypatch.setenv("TOKEN_ENCRYPTION_KEY", "test-only")
    monkeypatch.setenv("PLATFORM_MODE", "fake")

    from app.main import app

    with TestClient(app) as test_client:
        yield test_client


@pytest.mark.parametrize("db_state", ["available", "connection_error", "query_error"])
def test_health(client: TestClient, monkeypatch: pytest.MonkeyPatch, db_state: str) -> None:
    from app.db.session import engine

    connect = MagicMock()
    connection = connect.return_value.__enter__.return_value
    error = OperationalError("SELECT 1", {}, RuntimeError("Database unavailable"))
    if db_state == "connection_error":
        connect.side_effect = error
    elif db_state == "query_error":
        connection.execute.side_effect = error
    monkeypatch.setattr(engine, "connect", connect)

    response = client.get("/api/health")

    assert response.status_code == 200
    assert response.json() == {"ok": True, "db": db_state == "available", "mode": "fake"}
    connect.assert_called_once_with()
    if db_state != "connection_error":
        connection.execute.assert_called_once()
        assert str(connection.execute.call_args.args[0]) == "SELECT 1"
