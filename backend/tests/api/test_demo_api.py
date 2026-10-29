from collections import defaultdict
from contextlib import asynccontextmanager, contextmanager, nullcontext
from datetime import UTC, datetime, timedelta

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from app.db.models import (
    Ad,
    BudgetLedger,
    Business,
    ChangeLog,
    Experiment,
    PlatformAccount,
    Session,
    User,
    WorkerRun,
)
from app.demo_flags import ADVANCING, advance_jobs
from app.platforms.fake import VirtualClock
from app.services.changes import ChangeConflict
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
        def rows(self, model, business_id=None, **filters):
            if model is Business and business_id is None:
                business_id = world.business.id
            return super().rows(model, business_id, **filters)

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

        def has_business(self, user_id):
            return True

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
    app.dependency_overrides[get_demo_service] = lambda: lambda business_id: nullcontext(DemoService(worker))
    app.dependency_overrides[history.get_history_repository] = lambda: repository
    app.dependency_overrides[history.get_adapter_resolver] = lambda: worker.resolve_adapter
    app.dependency_overrides[autopilot.get_screens_service] = lambda: ScreensService(worker)
    app.dependency_overrides[autopilot.get_optional_screens] = lambda: ScreensService(worker)
    app.dependency_overrides[get_today] = lambda: world.adapters["direct"].clock.now.date()
    advance_jobs._items.clear()
    with TestClient(app, base_url="https://testserver") as client:
        client.cookies.set(SESSION_COOKIE, "test-session")
        client.cookies.set(CSRF_COOKIE, "test-csrf")
        client.headers["X-CSRF-Token"] = "test-csrf"
        yield client, world, worker
    advance_jobs._items.clear()


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
    assert client.post("/api/demo/advance", json={"days": 1}).status_code == 202
    assert client.post("/api/demo/advance", json={"days": 1}).status_code == 409
    assert world.business.settings["_demo"]["days"] == 120


def test_concurrent_advance_rejected_per_business(demo):
    client, world, _ = demo
    launch(client)
    advance_jobs.start(world.business.id, 1)
    response = client.post("/api/demo/advance", json={"days": 1})
    assert response.status_code == 409 and response.json()["detail"] == ADVANCING
    advance_jobs.forget(world.business.id)
    advance_jobs.start(world.business.id + 1, 1)
    assert client.post("/api/demo/advance", json={"days": 1}).status_code == 202
    assert client.get("/api/demo/status").json()["advance"] == {"state": "done", "days": 1, "done": 1, "error": ""}


def test_advance_left_by_closed_browser_finishes_and_does_not_block_next(demo):
    import asyncio
    import json

    from app.auth.sessions import CSRF_COOKIE, SESSION_COOKIE

    client, world, _ = demo
    launch(client)
    body = json.dumps({"days": 1}).encode()
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "https",
        "path": "/api/demo/advance",
        "raw_path": b"/api/demo/advance",
        "query_string": b"",
        "root_path": "",
        "server": ("testserver", 443),
        "client": ("127.0.0.1", 5000),
        "headers": [
            (b"host", b"testserver"),
            (b"content-type", b"application/json"),
            (b"content-length", str(len(body)).encode()),
            (b"x-csrf-token", b"test-csrf"),
            (b"cookie", f"{SESSION_COOKIE}=test-session; {CSRF_COOKIE}=test-csrf".encode()),
        ],
    }
    messages = [{"type": "http.request", "body": body, "more_body": False}]

    async def receive():
        return messages.pop(0) if messages else {"type": "http.disconnect"}

    async def closed(message):
        # Так uvicorn отвечает в закрытое соединение: молча, без ошибки.
        pass

    asyncio.run(client.app(scope, receive, closed))
    assert advance_jobs.get(world.business.id).state == "done"
    assert client.post("/api/demo/advance", json={"days": 1}).status_code == 202
    assert client.get("/api/demo/status").json()["total_days"] == 2


def test_advance_refusals_explain_why(demo):
    from app.demo_flags import MAX_ADVANCES

    client, world, _ = demo
    launch(client)
    abandoned = []
    for business_id in range(world.business.id + 1, world.business.id + 1 + MAX_ADVANCES):
        abandoned.append(advance_jobs.start(business_id, 1))
        advance_jobs.forget(business_id)
    response = client.post("/api/demo/advance", json={"days": 1})
    assert (response.status_code, response.json()["detail"]) == (503, "Сервер сейчас занят. Повторите через минуту.")
    for progress in abandoned:
        progress.state = "done"
    advance_jobs.start(world.business.id, 1)
    response = client.post("/api/demo/advance", json={"days": 1})
    assert (response.status_code, response.json()["detail"]) == (409, ADVANCING)


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
    assert response.status_code == 202, response.text
    assert response.json()["advance"] == {"state": "running", "days": 30, "done": 0, "error": ""}
    status = client.get("/api/demo/status").json()
    assert status["total_days"] == 30
    assert status["advance"] == {"state": "done", "days": 30, "done": 30, "error": ""}
    assert status["today"] == (start + timedelta(days=30)).date().isoformat()
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
    # Журнал называет, какой группе подняли лимит и какое объявление остановили.
    assert all("группы «" in row.reason_text for row in changes if row.reason_code == "SCALE_UP")
    # Рост лимита группы — настоящий рост лимита её кампании в кабинете.
    scaled = [item for row in changes if row.reason_code == "SCALE_UP" for item in row.operations]
    assert scaled and all(item["after"] > item["before"] for item in scaled)
    assert all("объявление «" in row.reason_text for row in changes if row.reason_code.startswith("PAUSE_"))
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


class BusyConnection:
    """Соединение, на котором блокировку бизнеса уже держит другой процесс."""

    engine = None

    def connect(self):
        return nullcontext(self)

    def scalar(self, *args):
        return False


def enable_demo(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "platform_mode", "fake")
    monkeypatch.setattr(settings, "demo_login", True)


def two_businesses(shifted: datetime) -> MemoryWorkerRepository:
    repository = MemoryWorkerRepository()
    repository.save(
        Business(id=1, user_id=1, status="running", settings={"_demo": {"days": 45, "now": shifted.isoformat()}}),
        Business(id=2, user_id=2, status="running", settings={}),
    )
    return repository


async def test_worker_runs_each_business_by_its_own_clock(monkeypatch):
    from app.worker.jobs import schedule_key

    enable_demo(monkeypatch)
    base = VirtualClock(datetime(2026, 1, 5, 7, tzinfo=UTC))
    shifted = datetime(2026, 2, 19, 7, tzinfo=UTC)
    repository = two_businesses(shifted)
    seen = []

    async def record(worker, business):
        seen.append((business.id, worker.clock.now))

    monkeypatch.setattr("app.worker.jobs.handlers", lambda: defaultdict(lambda: record))
    worker = Worker(repository, lambda business_id, platform: None, clock=base, dry_run=False)
    await worker.run_due()
    assert worker.clock is base
    assert {now for business_id, now in seen if business_id == 1} == {shifted}
    assert {now for business_id, now in seen if business_id == 2} == {base.now}
    for business_id, now in ((1, shifted), (2, base.now)):
        runs = repository.rows(WorkerRun, business_id)
        assert runs and all(run.log[0]["key"] == schedule_key(run.kind, now) for run in runs)
        assert all(run.started_at == now for run in runs)


async def test_background_tick_skips_businesses_frozen_in_demo(monkeypatch):
    from app.demo_flags import demo_frozen

    enable_demo(monkeypatch)
    base = VirtualClock(datetime(2026, 1, 5, 7, tzinfo=UTC))
    repository = two_businesses(datetime(2026, 2, 19, 7, tzinfo=UTC))
    plain = repository.business(2)
    assert demo_frozen(repository.business(1)) and not demo_frozen(plain)
    advance_jobs.start(2, 1)
    try:
        assert demo_frozen(plain)
    finally:
        advance_jobs.forget(2)

    async def record(worker, business):
        pass

    monkeypatch.setattr("app.worker.jobs.handlers", lambda: defaultdict(lambda: record))
    await Worker(repository, lambda business_id, platform: None, clock=base, dry_run=False).run_due(skip=demo_frozen)
    assert not repository.rows(WorkerRun, 1)
    assert repository.rows(WorkerRun, 2)


def test_other_actions_get_conflict_while_advance_holds_business(demo, monkeypatch):
    from app.worker.locks import business_lock

    client, world, worker = demo
    launch(client)
    advance_jobs.start(world.business.id, 1).locked = True
    monkeypatch.setattr(
        type(worker.repository),
        "business_lock",
        lambda self, business_id: business_lock(BusyConnection(), business_id),
    )
    for path in ("/api/autopilot/pause", "/api/autopilot/resume"):
        response = client.post(path)
        assert response.status_code == 409 and response.json()["detail"] == ADVANCING


def test_business_locks_name_the_advance():
    from types import SimpleNamespace

    from app.services.changes import ChangesRepository
    from app.worker.locks import business_lock

    def conflict(lock):
        with pytest.raises(ChangeConflict) as error, lock():
            pass
        return str(error.value)

    changes = ChangesRepository(SimpleNamespace(get_bind=BusyConnection))
    locks = (lambda: changes.business_lock(7), lambda: business_lock(BusyConnection(), 7))
    try:
        progress = advance_jobs.start(7, 1)
        assert ADVANCING not in {conflict(lock) for lock in locks}
        progress.locked = True
        assert {conflict(lock) for lock in locks} == {ADVANCING}
        progress.state = "done"
        assert ADVANCING not in {conflict(lock) for lock in locks}
    finally:
        advance_jobs.forget(7)


@pytest.mark.parametrize(
    "error,message",
    [
        (ChangeConflict("Сначала запустите рекламу."), "Сначала запустите рекламу."),
        (RuntimeError("secret"), "Перемотка прервалась. Попробуйте ещё раз."),
    ],
)
def test_background_advance_failure_is_reported(demo, monkeypatch, error, message):
    client, _, _ = demo
    launch(client)

    async def fail(self, business_id, progress):
        raise error

    monkeypatch.setattr(DemoService, "advance", fail)
    assert client.post("/api/demo/advance", json={"days": 7}).status_code == 202
    status = client.get("/api/demo/status").json()
    assert status["advance"] == {"state": "error", "days": 7, "done": 0, "error": message}
    assert client.post("/api/demo/advance", json={"days": 7}).status_code == 202


def test_advance_reports_progress_by_day(demo, monkeypatch):
    client, world, worker = demo
    launch(client)
    done = []
    run_business_due = Worker.run_business_due

    async def watch(self, business_id):
        done.append(advance_jobs.get(world.business.id).done)
        await run_business_due(self, business_id)

    monkeypatch.setattr(Worker, "run_business_due", watch)
    assert client.post("/api/demo/advance", json={"days": 3}).status_code == 202
    assert done == [0] * 48 + [1] * 48 + [2] * 48
    assert advance_jobs.get(world.business.id).done == 3


def test_advance_checks_demo_data_before_rewinding_and_asks_base_only_for_new_slots(demo, monkeypatch):
    """Демо-данные проверяются только до перемотки (запрос и check под блокировкой), а пройденные слоты перемотка
    помнит: каждые полчаса базу о них не спрашивает."""
    client, world, worker = demo
    launch(client)
    checks, slots = [], []
    demo_available, start_run = Worker.demo_available, type(worker.repository).start_run

    def counted_check(self, business):
        checks.append(business.id)
        return demo_available(self, business)

    def counted_start(self, business_id, kind, key, now):
        slots.append((kind, key))
        return start_run(self, business_id, kind, key, now)

    monkeypatch.setattr(Worker, "demo_available", counted_check)
    monkeypatch.setattr(type(worker.repository), "start_run", counted_start)
    assert client.post("/api/demo/advance", json={"days": 1}).status_code == 202
    assert checks == [world.business.id] * 2
    assert slots and len(slots) == len(set(slots))


ADVANCE_FAILED = "Перемотка прервалась. Попробуйте ещё раз."


def test_interrupted_advance_spends_only_passed_days_and_retry_adds_the_rest(demo, monkeypatch):
    client, world, worker = demo
    launch(client)
    calls = []
    totals = []
    run_business_due = Worker.run_business_due

    async def broken(self, business_id):
        calls.append(business_id)
        totals.append(client.get("/api/demo/status").json()["total_days"])
        if len(calls) == 48 * 2 + 10:
            raise RuntimeError("connection lost")
        await run_business_due(self, business_id)

    monkeypatch.setattr(Worker, "run_business_due", broken)
    assert client.post("/api/demo/advance", json={"days": 7}).status_code == 202
    status = client.get("/api/demo/status").json()
    assert status["advance"] == {"state": "error", "days": 7, "done": 2, "error": ADVANCE_FAILED}
    assert status["total_days"] == 2
    assert totals[0] == 0 and totals[48] == 1 and totals[96] == 2
    monkeypatch.setattr(Worker, "run_business_due", run_business_due)
    retry = status["advance"]["days"] - status["advance"]["done"]
    assert client.post("/api/demo/advance", json={"days": retry}).status_code == 202
    status = client.get("/api/demo/status").json()
    assert status["advance"] == {"state": "done", "days": 5, "done": 5, "error": ""}
    assert status["total_days"] == 7


def test_status_date_uses_configured_timezone(demo, monkeypatch):
    from app.api.stats import get_today
    from app.config import settings

    client, world, _ = demo
    client.app.dependency_overrides.pop(get_today)
    monkeypatch.setattr(settings, "tz_default", "Asia/Vladivostok")
    world.business.settings = {"_demo": {"days": 1, "now": "2026-01-01T20:30:00+00:00"}}
    status = client.get("/api/demo/status").json()
    assert status["today"] == "2026-01-02"
    assert status["now"] == "2026-01-01T20:30:00+00:00"


def test_demo_settings_are_reread_under_lock_and_merged(demo, monkeypatch):
    client, world, worker = demo
    launch(client)
    world.business.settings = world.business.settings | {"_demo": {"days": 2, "now": worker.clock.now.isoformat()}}
    depth = []

    @contextmanager
    def lock(self, business_id):
        depth.append(business_id)
        try:
            yield
        finally:
            depth.pop()

    def refresh(self, item):
        assert depth, "настройки перечитываются только под блокировкой бизнеса"
        # Другой процесс успел записать свои поля между шагами перемотки.
        item.settings = item.settings | {"owner": "kept", "_demo": item.settings["_demo"] | {"note": "kept"}}

    monkeypatch.setattr(type(worker.repository), "business_lock", lock)
    monkeypatch.setattr(type(worker.repository), "refresh", refresh)
    assert client.post("/api/demo/advance", json={"days": 1}).status_code == 202
    settings = world.business.settings
    assert settings["owner"] == "kept"
    assert settings["_demo"]["note"] == "kept" and settings["_demo"]["days"] == 3


@pytest.mark.parametrize(
    "url,demo_login,allowed",
    [
        ("https://localhost", True, True),
        ("http://localhost:8080", True, True),
        ("http://127.0.0.1:8000", True, True),
        ("https://autopilot.example", True, False),
        ("https://localhost.example", True, False),
        ("ftp://localhost", True, False),
        ("https://autopilot.example", False, True),
    ],
)
def test_demo_login_only_with_local_base_url(monkeypatch, url, demo_login, allowed):
    from pydantic import ValidationError

    from app.config import Settings

    monkeypatch.setenv("APP_BASE_URL", url)
    monkeypatch.setenv("DEMO_LOGIN", "1" if demo_login else "0")
    if allowed:
        assert Settings().app_base_url == url
    else:
        with pytest.raises(ValidationError, match="DEMO_LOGIN is allowed only on a local stand"):
            Settings()


async def test_scheduler_start_warns_about_demo(monkeypatch, caplog):
    from app.worker import main

    async def tick(registry, kind="due"):
        pass

    monkeypatch.setattr(main, "run_tick", tick)
    monkeypatch.setattr(main, "build_scheduler", lambda registry: type("Scheduler", (), {"start": lambda self: None})())
    for enabled in (False, True):
        enable_demo(monkeypatch)
        monkeypatch.setattr("app.config.settings.demo_login", enabled)
        caplog.clear()
        await main.start_scheduler(None)
        assert any(record.levelname == "WARNING" for record in caplog.records) is enabled


def test_budget_limits_are_applied_in_trial_mode(demo):
    """Пробный режим касается только решений автопилота: пересчёт лимитов и продление кампании идут в кабинет."""
    import asyncio

    from app.services.optimize import rebalance_budget

    client, world, worker = demo
    launch(client)
    worker.clock.advance(days=1)
    worker.step_key = "rebalance_budget:trial"
    world.business.settings = world.business.settings | {"dry_run": True}
    world.business.month_budget_kop = 2_000_000
    asyncio.run(rebalance_budget(worker, world.business))
    items = client.get("/api/history?page_size=100").json()["items"]
    statuses = [item["status"] for item in items if item["reason_code"] == "BUDGET_REBALANCE"]
    assert statuses and set(statuses) == {"applied"}


def test_business_screen_gets_only_description_fields(demo):
    client, _, _ = demo
    launch(client)
    description = client.get("/api/business").json()["description"]
    assert set(description) == {"fields"}
    assert set(description["fields"]) == {"services", "prices", "address", "difference"}
    assert all("value" in item for item in description["fields"].values())


@pytest.mark.db
async def test_advance_runs_only_its_business_and_writes_no_shared_heartbeat(db_session, monkeypatch):
    from uuid import uuid4

    from sqlalchemy import select

    from app.demo_flags import AdvanceProgress
    from app.platforms.fake import FakeAdapter
    from app.services.demo import DemoRepository
    from app.worker.jobs import WorkerRepository

    enable_demo(monkeypatch)
    start = datetime(2026, 1, 5, 7, tzinfo=UTC)
    businesses = []
    for _ in range(2):
        user = User(yandex_uid="demo-" + uuid4().hex, login="demo", consent_at=start)
        db_session.add(user)
        db_session.flush()
        business = Business(
            user_id=user.id,
            site_url="https://salon.example",
            city="Москва",
            month_budget_kop=3_000_000,
            status="running",
            settings={},
            lead_goal_ids=[1],
        )
        db_session.add(business)
        db_session.commit()
        businesses.append(business)
    first, second = businesses
    adapters = {
        (business.id, platform): FakeAdapter(clock=VirtualClock(start))
        for business in businesses
        for platform in ("direct", "vk")
    }
    worker = Worker(
        DemoRepository(db_session, first.id),
        lambda business_id, platform: adapters[business_id, platform],
        dry_run=False,
    )
    await DemoService(worker).advance(first.id, AdvanceProgress(1))

    def runs(*conditions):
        return db_session.scalars(select(WorkerRun).where(*conditions)).all()

    assert runs(WorkerRun.business_id == first.id)
    assert not runs(WorkerRun.business_id == second.id)
    assert not runs(WorkerRun.kind == "heartbeat")
    db_session.refresh(second)
    assert second.settings == {}
    assert all(adapters[second.id, platform].clock.now == start for platform in ("direct", "vk"))
    assert WorkerRepository(db_session).last_check(second.id) is None


def test_demo_reconnect_returns_account_and_resumes_campaigns_stopped_by_disconnect(demo):
    import asyncio
    from time import monotonic

    from app.services.budget_guard import stop_platform

    client, world, worker = demo
    launch(client)
    assert client.post("/api/demo/reconnect").status_code == 409
    worker.step_key = "owner:revoke"
    asyncio.run(
        stop_platform(
            world.business, worker=worker, platform="direct", reason_text="Отключение.", deadline=monotonic() + 30
        )
    )
    assert all(campaign.status == "paused_owner" for campaign in worker.current_campaigns(world.business))
    account = worker.repository.find(PlatformAccount, business_id=world.business.id, platform="direct")
    account.status, account.access_token_enc = "revoked", ""
    status = client.get("/api/autopilot/status").json()
    assert status["status"] == "paused"
    assert any(item["id"] == "revoked_direct" for item in status["needs_attention"])
    response = client.post("/api/demo/reconnect")
    assert response.status_code == 200, response.text
    assert account.status == "ok" and account.access_token_enc == "demo"
    assert client.get("/api/autopilot/status").json()["status"] == "running"
    assert all(campaign.status == "active" for campaign in worker.current_campaigns(world.business))
    assert any(row.reason_code == "OWNER_RESUME" and row.actor == "owner" for row in worker.repository.rows(ChangeLog))


def test_continue_resumes_campaigns_stopped_by_disconnect_after_account_is_back(demo):
    import asyncio
    from time import monotonic

    from app.services.budget_guard import resume_reconnected, stop_platform

    client, world, worker = demo
    launch(client)
    assert client.post("/api/demo/advance", json={"days": 1}).status_code == 202
    worker.step_key = "owner:revoke"
    asyncio.run(
        stop_platform(
            world.business, worker=worker, platform="direct", reason_text="Отключение.", deadline=monotonic() + 30
        )
    )
    # Реклама на паузе: подключение кабинета кампании не включает, их включает «Продолжить».
    world.business.status = "paused"
    asyncio.run(resume_reconnected(worker, world.business, "direct"))
    assert all(campaign.status == "paused_owner" for campaign in worker.current_campaigns(world.business))
    response = client.post("/api/autopilot/resume")
    assert response.status_code == 200, response.text
    assert world.business.status == "running"
    assert all(campaign.status == "active" for campaign in worker.current_campaigns(world.business))


def test_service_removed_from_description_stops_its_campaign_and_returns_with_it(demo):
    import asyncio

    from app.services.launch import sync_removed_services

    client, world, worker = demo
    launch(client)

    def services(value: str) -> None:
        fields = world.business.description["fields"]
        world.business.description = world.business.description | {
            "fields": fields | {"services": fields["services"] | {"value": value}}
        }
        worker.step_key = f"optimize:{value}"
        asyncio.run(sync_removed_services(worker, world.business))

    def statuses() -> dict[str, str]:
        return {campaign.name: campaign.status for campaign in worker.current_campaigns(world.business)}

    services("Маникюр\nНаращивание ногтей")
    pedicure = next(name for name in statuses() if "Педикюр" in name)
    assert statuses()[pedicure] == "paused_owner"
    assert all(status == "active" for name, status in statuses().items() if name != pedicure)
    removed = [row for row in worker.repository.rows(ChangeLog) if row.reason_code == "SERVICE_REMOVED"]
    assert len(removed) == 1 and removed[0].actor == "owner" and "Педикюр" in removed[0].reason_text
    services("Маникюр\nНаращивание ногтей")
    assert len([row for row in worker.repository.rows(ChangeLog) if row.reason_code == "SERVICE_REMOVED"]) == 1
    services("Маникюр\nПедикюр\nНаращивание ногтей")
    assert set(statuses().values()) == {"active"}
    assert any(row.reason_code == "SERVICE_RETURNED" for row in worker.repository.rows(ChangeLog))
