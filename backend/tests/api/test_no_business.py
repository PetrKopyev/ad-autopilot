from datetime import date

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from app.db.models import Session, User
from app.worker.jobs import MemoryWorkerRepository


@pytest.fixture
def client(monkeypatch):
    """Пользователь вошёл, но мастер ещё не прошёл: бизнеса у него нет."""
    from app.api import ads, autopilot, notifications
    from app.api.demo import router as demo_router
    from app.auth.sessions import CSRF_COOKIE, SESSION_COOKIE, get_repository
    from app.config import settings

    monkeypatch.setattr(settings, "platform_mode", "fake")
    monkeypatch.setattr(settings, "demo_login", True)

    class Auth:
        def get_session(self, value):
            return Session(user_id=1, csrf_secret="test-csrf") if value == "test-session" else None

        def get_user(self, user_id):
            return User(id=user_id)

        def get_business(self, user_id, business_id):
            raise HTTPException(404, "Сначала заполните сведения о бизнесе.")

    app = FastAPI()
    for router in (notifications.router, autopilot.router, ads.router):
        app.include_router(router)
    app.include_router(demo_router, prefix="/api")
    app.dependency_overrides[get_repository] = Auth
    app.dependency_overrides[notifications.get_notifications_repository] = MemoryWorkerRepository
    with TestClient(app, base_url="https://testserver") as client:
        client.cookies.set(SESSION_COOKIE, "test-session")
        client.cookies.set(CSRF_COOKIE, "test-csrf")
        client.headers["X-CSRF-Token"] = "test-csrf"
        yield client


def test_requests_sent_before_setup_answer_without_business(client) -> None:
    assert client.get("/api/notifications").json() == []
    assert client.get("/api/autopilot/status").json() == {
        "status": "no_business",
        "last_check_at": None,
        "auto_continue": False,
        "needs_attention": [],
    }
    demo = client.get("/api/demo/status").json()
    assert demo["total_days"] == 0 and demo["advance"] is None
    assert date.fromisoformat(demo["today"])


def test_business_screens_and_foreign_business_still_not_found(client) -> None:
    assert client.get("/api/ads").status_code == 404
    assert client.post("/api/autopilot/pause").status_code == 404
    assert client.post("/api/demo/advance", json={"days": 1}).status_code == 404
    for path in ("/api/notifications", "/api/autopilot/status", "/api/demo/status"):
        assert client.get(path, params={"business_id": 7}).status_code == 404
