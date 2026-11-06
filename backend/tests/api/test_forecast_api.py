from collections.abc import Iterator

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from app.db.models import Business, Session, User
from app.services import forecast as module
from tests.services.test_forecast import world
from tests.services.test_onboarding import TODAY


@pytest.fixture
def client() -> Iterator[TestClient]:
    from app.api import plan
    from app.api.stats import get_today
    from app.auth.sessions import CSRF_COOKIE, SESSION_COOKIE, get_repository

    case = world(None, mode="fake")

    class Auth:
        def get_session(self, value):
            return Session(id=value, user_id=1, csrf_secret="test-csrf") if value == "test-session" else None

        def get_user(self, user_id):
            return User(id=user_id)

        def get_business(self, user_id, business_id):
            found = [item for item in case.repository.rows(Business, user_id=user_id) if business_id in (None, item.id)]
            if not found:
                raise HTTPException(404, "Сначала заполните сведения о бизнесе.")
            return found[0]

    app = FastAPI()
    app.include_router(plan.router)
    app.dependency_overrides[get_repository] = Auth
    app.dependency_overrides[plan.get_forecast_service] = lambda: case.service
    app.dependency_overrides[get_today] = lambda: TODAY
    with TestClient(app, base_url="https://testserver") as test_client:
        test_client.cookies.set(SESSION_COOKIE, "test-session")
        test_client.cookies.set(CSRF_COOKIE, "test-csrf")
        test_client.headers["X-CSRF-Token"] = "test-csrf"
        yield test_client
    module.clear()


def test_forecast_requires_login(client: TestClient) -> None:
    client.cookies.clear()

    assert client.get("/api/plan/forecast").status_code == 401
    assert client.post("/api/plan/forecast").status_code == 401


def test_forecast_format(client: TestClient) -> None:
    response = client.post("/api/plan/forecast")

    assert response.status_code == 200
    body = response.json()
    assert set(body) == {
        "status",
        "leads_min",
        "leads_max",
        "clicks_min",
        "clicks_max",
        "cpc_rub",
        "cpc_rub_max",
        "budget_rub",
        "conversion",
        "explanation",
        "explanation_by_ai",
        "sources",
        "computed_at",
        "demo",
        "vk",
    }
    assert body["status"] == "ready" and body["demo"] is True
    assert set(body["conversion"]) == {"min", "max", "source", "label"}
    assert all(set(source) == {"what", "source", "url"} for source in body["sources"])


def test_forecast_rate_limit(client: TestClient) -> None:
    statuses = [client.post("/api/plan/forecast").status_code for _ in range(31)]

    assert statuses[:30] == [200] * 30 and statuses[30] == 429


def test_forecast_get_only_reads_cache(client: TestClient) -> None:
    """GET не считает прогноз: пока POST его не посчитал, ответ пустой; после — отдаёт готовое из кэша."""
    assert client.get("/api/plan/forecast").status_code == 204
    computed = client.post("/api/plan/forecast").json()

    cached = client.get("/api/plan/forecast")

    assert cached.status_code == 200 and cached.json() == computed


def test_forecast_post_requires_csrf(client: TestClient) -> None:
    client.headers.pop("X-CSRF-Token")

    assert client.post("/api/plan/forecast").status_code == 403
    assert client.get("/api/plan/forecast").status_code == 204
