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
    own, other = Business(id=1, user_id=1, settings={}), Business(id=2, user_id=2, settings={})
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


def test_interrupted_launch_is_shown_as_danger(setup):
    client, repository = setup
    repository.save(notice(1, "LAUNCH_INTERRUPTED", "Запуск рекламы не завершился. Всё остановили."))
    (item,) = client.get("/api/notifications").json()
    assert (item["kind"], item["title"], item["level"]) == (
        "LAUNCH_INTERRUPTED",
        "Запуск рекламы не завершился",
        "danger",
    )
    assert item["text"] == "Запуск рекламы не завершился. Всё остановили."


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


def test_daily_repeats_of_one_theme_show_as_one_actual_notice(setup):
    client, repository = setup
    for day in range(15):
        run = notice(1, "BUDGET_OPPORTUNITY", f"При увеличении на {100 + day} ₽ — ещё обращения.", hours=24 * day)
        run.log = [run.log[0] | {"target_id": "business"}]
        repository.save(run)
    repository.save(notice(1, "ANOMALY", "Несколько дней нет обращений.", hours=1))
    items = client.get("/api/notifications").json()
    assert [item["kind"] for item in items] == ["BUDGET_OPPORTUNITY", "ANOMALY"]
    assert "114 ₽" in items[0]["text"]
    assert client.post(f"/api/notifications/{items[0]['id']}/read").status_code == 204
    assert [item["kind"] for item in client.get("/api/notifications").json()] == ["ANOMALY"]


def test_read_rejects_service_and_proposed_notices(setup):
    client, repository = setup
    repository.save(
        notice(1, "EXPERIMENT_TOO_LONG", "Служебное.", hours=1),
        notice(1, "BUDGET_STOP", "Предложение.", hours=2, status="proposed"),
    )
    service, proposed = repository.rows(WorkerRun, kind="notification")
    assert client.post(f"/api/notifications/{service.id}/read").status_code == 404
    assert client.post(f"/api/notifications/{proposed.id}/read").status_code == 404
    assert len(service.log) == 1 and len(proposed.log) == 1


def test_notice_with_deadline_expires_by_demo_time(setup, monkeypatch):
    # Демо-бизнес перемотан за срок уведомления: по его времени уведомление уже неправда, хотя по часам сервера — нет.
    from app.config import settings

    client, repository = setup
    expires_at = datetime.now(UTC) + timedelta(days=1)
    entry = notice(1, "MONTH_CONTINUE_NOTICE", "1 февраля начнётся новый месяц.")
    entry.log = [entry.log[0] | {"expires_at": expires_at.isoformat()}]
    repository.save(entry)
    own = repository.business(1)
    own.settings = {"_demo": {"now": (expires_at + timedelta(days=1)).isoformat()}}
    assert [item["kind"] for item in client.get("/api/notifications").json()] == ["MONTH_CONTINUE_NOTICE"]
    monkeypatch.setattr(settings, "demo_login", True)
    assert client.get("/api/notifications").json() == []
