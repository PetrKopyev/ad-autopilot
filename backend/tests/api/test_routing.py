from contextlib import contextmanager

import pytest
from fastapi import APIRouter, Depends, FastAPI
from fastapi.testclient import TestClient

from app.api import routing
from app.api.routing import ReleasingRoute, request_session


class FakeSession:
    def __init__(self, events: list[str]) -> None:
        self.events = events

    def close(self) -> None:
        self.events.append("close")


def make_client(monkeypatch) -> tuple[TestClient, list[str], APIRouter]:
    events = []

    @contextmanager
    def session_local():
        yield FakeSession(events)
        events.append("exit")

    monkeypatch.setattr("app.db.session.SessionLocal", session_local)

    def get_db():
        with request_session() as db:
            yield db

    router = APIRouter(route_class=ReleasingRoute)

    @router.get("/sync")
    def sync_route(db: FakeSession = Depends(get_db)) -> dict:
        events.append("handler")
        return {}

    @router.get("/async")
    async def async_route(db: FakeSession = Depends(get_db)) -> dict:
        events.append("handler")
        return {}

    @router.get("/error")
    async def error_route(db: FakeSession = Depends(get_db)) -> dict:
        events.append("handler")
        raise RuntimeError("boom")

    app = FastAPI()
    app.include_router(router)
    return TestClient(app, raise_server_exceptions=False), events, router


@pytest.mark.parametrize("path,status", [("/sync", 200), ("/async", 200), ("/error", 500)])
def test_session_is_closed_right_after_handler(monkeypatch, path, status) -> None:
    client, events, _ = make_client(monkeypatch)
    assert client.get(path).status_code == status
    # Сессия закрыта сразу после обработчика, раньше выхода из зависимости (и при ошибке обработчика).
    assert events[:2] == ["handler", "close"] and events.count("close") == 1


async def test_request_sessions_do_not_leak_between_requests(monkeypatch) -> None:
    import httpx

    client, events, _ = make_client(monkeypatch)
    transport = httpx.ASGITransport(app=client.app)
    async with httpx.AsyncClient(transport=transport, base_url="https://testserver") as http:
        for path in ("/sync", "/async"):
            assert (await http.get(path)).status_code == 200
            # Список сессий запроса живёт только внутри запроса: в том же контексте следующий начинает с пустого.
            with pytest.raises(LookupError):
                routing._sessions.get()
    assert events.count("close") == 2


def test_auth_reads_return_connection_at_once() -> None:
    from types import SimpleNamespace

    from app.auth.sessions import AuthRepository
    from app.db.models import Business

    commits = []
    business = Business(id=1, user_id=1)
    db = SimpleNamespace(
        scalar=lambda statement: None,
        get=lambda model, key: None,
        scalars=lambda statement: [business],
        commit=lambda: commits.append(1),
    )
    repository = AuthRepository(db)
    repository.get_session("token")
    repository.get_user(1)
    assert repository.get_business(1, None) is business
    assert len(commits) == 3
