from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from app.db.models import Ad, BudgetLedger, Business, ChangeLog, Experiment, Session, User, WorkerRun
from app.platforms.fake import VirtualClock
from app.services.demo import DemoService, prepare_business, read_demo_site
from app.services.experiments import create_experiment
from app.services.registry import DemoArt, DemoLLM
from app.services.screens import ScreensService
from app.worker.jobs import MemoryWorkerRepository, Worker
from tests.services.test_onboarding import make_world


@pytest.fixture
def demo(monkeypatch, tmp_path, request):
    from app.api import ads, auth, autopilot, business, experiments, history, plan
    from app.api.demo import get_demo_service
    from app.api.stats import get_today
    from app.auth.sessions import CSRF_COOKIE, SESSION_COOKIE, get_repository
    from app.config import settings

    monkeypatch.setattr(settings, "platform_mode", "fake")
    monkeypatch.setattr(settings, "demo_login", True)
    monkeypatch.setattr(settings, "dry_run", False)
    world = make_world(tmp_path, platforms=())
    clock = VirtualClock(getattr(request, "param", datetime(2026, 1, 1, tzinfo=UTC)))
    for adapter in world.adapters.values():
        adapter.clock = clock
    world.onboarding.reader = read_demo_site
    world.onboarding.llm = world.plans.llm = DemoLLM()
    world.plans.art_factory = lambda budget, record: DemoArt(budget)

    class Repository(MemoryWorkerRepository):
        def rows(self, model, business_id=None):
            if model is Business and business_id is None:
                business_id = world.business.id
            return super().rows(model, business_id)

        def get_owned(self, change_id, user_id):
            change = self.find(ChangeLog, id=change_id)
            return change if change and self.business(change.business_id).user_id == user_id else None

        def history(self, user_id, *, business_id=None, platform=None, actor=None, page=1, page_size=20):
            items = [
                row
                for row in self.rows(ChangeLog, business_id)
                if self.business(row.business_id).user_id == user_id
                and (not platform or row.platform == platform)
                and (not actor or row.actor == actor)
            ]
            items.sort(key=lambda row: (row.created_at, row.id), reverse=True)
            return items[(page - 1) * page_size : page * page_size], len(items)

    repository = Repository()
    repository.tables = world.repository.tables
    worker = Worker(
        repository,
        lambda bid, platform: world.adapters[platform],
        clock=clock,
        dry_run=False,
        experiment_factory=create_experiment,
    )

    class Auth:
        def get_session(self, value):
            return Session(user_id=1, csrf_secret="test-csrf") if value == "test-session" else None

        def get_user(self, user_id):
            return User(id=user_id, login="demo")

        def get_business(self, user_id, business_id):
            if user_id != world.business.user_id or business_id not in (None, world.business.id):
                raise HTTPException(404, "Бизнес не найден.")
            return world.business

        def list_accounts(self, user_id):
            return world.repository.accounts(world.business.id)

    class DemoDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return world.adapters["direct"].clock.now.astimezone(tz or UTC)

    monkeypatch.setattr(business, "datetime", DemoDateTime)
    monkeypatch.setattr(plan, "datetime", DemoDateTime)

    @asynccontextmanager
    async def content_service(worker):
        yield world.plans

    monkeypatch.setattr("app.services.experiments.content_service", content_service)
    app = FastAPI()
    for router in (
        auth.router,
        business.router,
        plan.router,
        autopilot.router,
        ads.router,
        experiments.router,
        history.router,
    ):
        app.include_router(router)
    app.dependency_overrides[get_repository] = Auth
    app.dependency_overrides[business.get_onboarding_service] = lambda: world.onboarding
    app.dependency_overrides[plan.get_plan_service] = lambda: world.plans
    app.dependency_overrides[plan.get_launch_service] = lambda: world.launches
    app.dependency_overrides[get_demo_service] = lambda: DemoService(worker)
    app.dependency_overrides[history.get_history_repository] = lambda: repository
    app.dependency_overrides[history.get_adapter_resolver] = lambda: worker.resolve_adapter
    app.dependency_overrides[autopilot.get_screens_service] = lambda: ScreensService(worker)
    app.dependency_overrides[get_today] = lambda: world.adapters["direct"].clock.now.date()
    with TestClient(app, base_url="https://testserver") as client:
        client.cookies.set(SESSION_COOKIE, "test-session")
        client.cookies.set(CSRF_COOKIE, "test-csrf")
        client.headers["X-CSRF-Token"] = "test-csrf"
        yield client, world, worker


def launch(client):
    response = client.post(
        "/api/business",
        json={
            "site_url": "https://salon.example/",
            "city": "Москва",
            "month_budget_kop": 3_000_000,
        },
    )
    assert response.status_code == 200, response.text
    assert client.get("/api/me").json()["accounts"][0]["status"] == "ok"
    assert client.put("/api/business/metrica", json={"counter_id": 12345, "lead_goal_ids": [1]}).status_code == 200
    site = client.get("/api/business/site-check").json()
    assert site["status"] == "ready" and site["services_found"] > 0
    assert all(item["verified"] for item in site["description"].values())
    assert (
        client.put("/api/business/description", json={"approved": True, "description": site["description"]}).status_code
        == 200
    )
    assert (
        client.put(
            "/api/business/marking",
            json={
                "subject_type": "ip",
                "inn": "526317984689",
                "name": "Демо-салон",
                "phone": "+7 900 123-45-67",
                "confirmed_in_platform": {"direct": True},
            },
        ).status_code
        == 200
    )
    response = client.get("/api/plan")
    assert response.status_code == 200, response.text
    plan = response.json()
    assert plan["ready_to_launch"], plan
    response = client.post("/api/plan/launch", json={"plan_id": plan["id"]})
    assert response.status_code == 200, response.text


@pytest.mark.parametrize("mode,enabled", [("live", False), ("live", True), ("fake", False)])
def test_hidden_outside_demo(demo, monkeypatch, mode, enabled):
    client, _, _ = demo
    from app.config import settings

    monkeypatch.setattr(settings, "platform_mode", mode)
    monkeypatch.setattr(settings, "demo_login", enabled)
    client.cookies.clear()
    assert client.post("/api/demo/advance", json={"days": 1}).status_code == 404
    assert client.get("/api/demo/status").status_code == 404


def test_auth_csrf_and_foreign_business(demo):
    client, _, _ = demo
    assert client.post("/api/demo/advance?business_id=999", json={"days": 1}).status_code == 404
    del client.headers["X-CSRF-Token"]
    assert client.post("/api/demo/advance", json={"days": 1}).status_code == 403
    client.cookies.clear()
    assert client.post("/api/demo/advance", json={"days": 1}).status_code == 401


@pytest.mark.parametrize("days", [0, -1, 36, 121, 1.5, True, "7", None])
def test_days_validation(demo, days):
    client, _, _ = demo
    assert client.post("/api/demo/advance", json={"days": days}).status_code == 422


def test_requires_launch_and_total_limit(demo):
    client, world, _ = demo
    assert client.post("/api/demo/advance", json={"days": 1}).status_code == 409
    world.business.settings = {"_demo": {"days": 119}}
    world.business.status = "paused"
    assert client.post("/api/demo/advance", json={"days": 2}).status_code == 409
    assert client.post("/api/demo/advance", json={"days": 1}).status_code == 200
    assert client.post("/api/demo/advance", json={"days": 1}).status_code == 409
    assert world.business.settings["_demo"]["days"] == 120


def test_concurrent_advance_rejected(demo):
    from app.worker.main import demo_tick_lock

    client, _, _ = demo
    with demo_tick_lock:
        assert client.post("/api/demo/advance", json={"days": 1}).status_code == 409


@pytest.mark.parametrize("demo", [datetime(2026, 1, 1, tzinfo=UTC), datetime(2026, 10, 26, tzinfo=UTC)], indirect=True)
def test_thirty_days_real_plan_budget_and_autopilot(demo, monkeypatch):
    client, world, worker = demo
    start = worker.clock.now

    async def no_network(*args, **kwargs):
        raise AssertionError("Демо не должно обращаться в сеть.")

    monkeypatch.setattr("httpx.AsyncClient.request", no_network)
    launch(client)
    other = Business(id=999, user_id=999, status="running", settings={})
    worker.repository.save(other)
    response = client.post("/api/demo/advance", json={"days": 30})
    assert response.status_code == 200, response.text
    assert response.json()["total_days"] == 30
    assert client.get("/api/demo/status").json()["today"] == (start + timedelta(days=30)).date().isoformat()
    assert all(row.spent_kop <= world.business.month_budget_kop for row in worker.repository.rows(BudgetLedger))
    assert sum(world.adapters["direct"]._spent.values()) > 0
    monthly = {}
    for (_, day), spent in world.adapters["direct"]._spent_daily.items():
        month = day.replace(day=1)
        monthly[month] = monthly.get(month, 0) + spent
    assert all(spent <= world.business.month_budget_kop for spent in monthly.values())
    changes = worker.repository.rows(ChangeLog)
    assert any(row.actor == "autopilot" and row.status == "applied" for row in changes)
    assert any(row.status == "paused_auto" for row in worker.repository.rows(Ad))
    assert any(row.reason_code == "SCALE_UP" for row in changes)
    assert any(row.status == "concluded" for row in worker.repository.rows(Experiment))
    assert not other.settings
    assert not worker.repository.rows(WorkerRun, 999)
    history = client.get("/api/history?actor=autopilot&page_size=100").json()["items"]
    change = next(row for row in history if row["kind"] == "pause" and row["can_revert"])
    response = client.post(f"/api/history/{change['id']}/revert?confirm=true")
    assert response.status_code == 200, response.text
    assert response.json()["kind"] == "revert" and response.json()["actor"] == "owner"
    assert worker.repository.get(change["id"], world.business.id).status == "reverted"
    assert any(row.actor == "owner" and row.kind == "revert" for row in worker.repository.rows(ChangeLog))
    assert client.post("/api/autopilot/pause").json()["status"] == "paused"
    assert client.post("/api/autopilot/resume").json()["status"] == "running"


def test_fake_config_without_platform_credentials(monkeypatch):
    from app.config import Settings

    for name in (
        "YANDEX_CLIENT_ID",
        "YANDEX_CLIENT_SECRET",
        "YC_FOLDER_ID",
        "YC_API_KEY",
        "VK_CLIENT_ID",
        "VK_CLIENT_SECRET",
    ):
        monkeypatch.delenv(name, raising=False)
    settings = Settings()
    assert settings.platform_mode == "fake"
    assert not settings.yandex_client_id and not settings.vk_client_id and not settings.yc_folder_id


def test_demo_account_has_token_for_not_null_column():
    added = []

    class Repository:
        def accounts(self, business_id):
            return added

        def add(self, account):
            added.append(account)

    prepare_business(Repository(), Business(id=1))
    assert [account.access_token_enc for account in added] == ["demo"]
