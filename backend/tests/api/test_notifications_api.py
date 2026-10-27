from datetime import UTC, datetime, timedelta

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from app.db.models import Business, Session, User, WorkerRun
from app.worker.jobs import MemoryWorkerRepository

NOW = datetime(2026, 3, 11, 9, tzinfo=UTC)


def notice(business_id: int, code: str, text: str, *, hours: int = 0, status: str = "ok", key: str = "") -> WorkerRun:
    return WorkerRun(
        kind="notification",
        business_id=business_id,
        slot_key=key or f"{code}:{hours}:{text}",
        started_at=NOW + timedelta(hours=hours),
        finished_at=NOW + timedelta(hours=hours),
        status=status,
        units_spent=0,
        log=[{"key": key, "reason_code": code, "text": text}],
    )


@pytest.fixture
def setup():
    from app.api import notifications
    from app.auth.sessions import CSRF_COOKIE, SESSION_COOKIE, get_repository

    repository = MemoryWorkerRepository()
    own, other = Business(id=1, user_id=1), Business(id=2, user_id=2)
    repository.save(own, other)

    class Auth:
        def get_session(self, value):
            return Session(user_id=1, csrf_secret="test-csrf") if value == "test-session" else None

        def get_user(self, user_id):
            return User(id=user_id)

        def get_business(self, user_id, business_id):
            if business_id not in (None, own.id):
                raise HTTPException(404, "Бизнес не найден.")
            return own

    app = FastAPI()
    app.include_router(notifications.router)
    app.dependency_overrides[get_repository] = Auth
    app.dependency_overrides[notifications.get_notifications_repository] = lambda: repository
    with TestClient(app, base_url="https://testserver") as client:
        client.cookies.set(SESSION_COOKIE, "test-session")
        client.cookies.set(CSRF_COOKIE, "test-csrf")
        client.headers["X-CSRF-Token"] = "test-csrf"
        yield client, repository


def test_lists_unread_owner_notices_newest_first_without_repeats(setup):
    client, repository = setup
    repository.save(
        notice(1, "MODERATION_REJECTED", "Объявление «А»: нужна лицензия.", hours=1),
        notice(1, "NOT_SHOWING", "Объявление одобрено, но показов нет.", hours=2),
        notice(1, "NOT_SHOWING", "Объявление одобрено, но показов нет.", hours=26),
        notice(1, "EXPERIMENT_TOO_LONG", "Пока недостаточно данных или бюджета для проверки.", hours=3),
        notice(1, "BUDGET_STOP", "Реклама остановлена.", hours=4, status="proposed"),
        notice(2, "BUDGET_STOP", "Чужое уведомление.", hours=5),
    )
    response = client.get("/api/notifications")
    assert response.status_code == 200
    items = response.json()
    assert [(item["kind"], item["created_at"]) for item in items] == [
        ("NOT_SHOWING", (NOW + timedelta(hours=26)).isoformat()),
        ("MODERATION_REJECTED", (NOW + timedelta(hours=1)).isoformat()),
    ]
    assert set(items[0]) == {"id", "kind", "created_at", "title", "text", "level"}
    assert items[0]["title"] == "Объявление не показывается" and items[0]["level"] == "warning"
    assert items[1]["text"] == "Объявление «А»: нужна лицензия."


def test_returns_at_most_twenty(setup):
    client, repository = setup
    repository.save(
        *(notice(1, "NOT_SHOWING", f"Объявление {index} не показывается.", hours=index) for index in range(25))
    )
    items = client.get("/api/notifications").json()
    assert len(items) == 20
    assert items[0]["text"] == "Объявление 24 не показывается."


def test_read_hides_notice_and_its_repeats(setup):
    client, repository = setup
    repository.save(
        notice(1, "NOT_SHOWING", "Объявление одобрено, но показов нет.", hours=1),
        notice(1, "NOT_SHOWING", "Объявление одобрено, но показов нет.", hours=25),
        notice(1, "ANOMALY", "Несколько дней нет обращений.", hours=2),
    )
    (latest, anomaly) = client.get("/api/notifications").json()
    assert client.post(f"/api/notifications/{latest['id']}/read").status_code == 204
    assert [item["id"] for item in client.get("/api/notifications").json()] == [anomaly["id"]]
    assert client.post(f"/api/notifications/{latest['id']}/read").status_code == 204


def test_read_requires_csrf_and_own_notice(setup):
    client, repository = setup
    repository.save(notice(1, "ANOMALY", "Своё.", hours=1), notice(2, "ANOMALY", "Чужое.", hours=1))
    own, foreign = repository.rows(WorkerRun, kind="notification")
    del client.headers["X-CSRF-Token"]
    assert client.post(f"/api/notifications/{own.id}/read").status_code == 403
    client.headers["X-CSRF-Token"] = "test-csrf"
    assert client.post(f"/api/notifications/{foreign.id}/read").status_code == 404
    assert client.post("/api/notifications/999/read").status_code == 404
    assert len(foreign.log) == 1


def test_login_required(setup):
    client, _ = setup
    client.cookies.clear()
    assert client.get("/api/notifications").status_code == 401
