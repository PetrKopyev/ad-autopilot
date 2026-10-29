import asyncio
from datetime import timedelta

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from app.db.models import Ad, Business, ChangeLog, Experiment, Session, StatsDaily, User, WorkerRun
from app.platforms.base import Operation, PlatformError
from app.services.changes import ChangeConflict
from app.services.rollback import RollbackService
from app.services.screens import ScreensService
from tests.integration.harness import make_world

ROUTES = [
    ("GET", "/api/autopilot/status", None),
    ("POST", "/api/autopilot/pause", None),
    ("POST", "/api/autopilot/resume", None),
    ("GET", "/api/ads", None),
    ("POST", "/api/ads/1/pause", None),
    ("POST", "/api/ads/1/resume", None),
    ("GET", "/api/experiments", None),
    ("GET", "/api/business", None),
    ("PUT", "/api/business/budget", {"month_budget_kop": 20_000_000}),
    ("PUT", "/api/business/settings", {"auto_continue": False, "avg_check_kop": 100_000}),
]


@pytest.fixture
def setup(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://test:test@invalid/test")
    monkeypatch.setenv("APP_BASE_URL", "https://testserver")
    monkeypatch.setenv("PLATFORM_MODE", "fake")
    monkeypatch.setenv("SECRET_KEY", "screens-tests-only-0123456789abcdef")
    monkeypatch.setenv("TOKEN_ENCRYPTION_KEY", "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=")
    from app.api import ads, autopilot, business, experiments, history
    from app.api.stats import get_today
    from app.auth.sessions import CSRF_COOKIE, SESSION_COOKIE, get_repository

    world = asyncio.run(make_world())
    app = FastAPI()
    for router in (autopilot.router, ads.router, experiments.router, business.router, history.router):
        app.include_router(router)

    class Auth:
        def get_session(self, value):
            return Session(user_id=1, csrf_secret="test-csrf") if value == "test-session" else None

        def get_user(self, user_id):
            return User(id=user_id)

        def get_business(self, user_id, business_id):
            if business_id not in (None, world.business.id) or user_id != world.business.user_id:
                raise HTTPException(404, "Бизнес не найден.")
            return world.business

    app.dependency_overrides[get_repository] = Auth
    app.dependency_overrides[autopilot.get_screens_service] = lambda: ScreensService(world.worker())
    app.dependency_overrides[autopilot.get_optional_screens] = lambda: ScreensService(world.worker())
    app.dependency_overrides[get_today] = lambda: world.clock.now.date()
    with TestClient(app, base_url="https://testserver") as client:
        client.cookies.set(SESSION_COOKIE, "test-session")
        client.cookies.set(CSRF_COOKIE, "test-csrf")
        client.headers["X-CSRF-Token"] = "test-csrf"
        yield client, world


@pytest.mark.parametrize("method,path,body", ROUTES)
def test_each_endpoint(setup, method, path, body):
    client, _ = setup
    expected = 409 if path == "/api/autopilot/resume" else 200
    assert client.request(method, path, json=body).status_code == expected


@pytest.mark.parametrize("method,path,body", ROUTES)
def test_login_required(setup, method, path, body):
    client, _ = setup
    client.cookies.clear()
    assert client.request(method, path, json=body).status_code == 401


@pytest.mark.parametrize("method,path,body", [row for row in ROUTES if row[0] != "GET"])
def test_csrf_required(setup, method, path, body):
    client, _ = setup
    del client.headers["X-CSRF-Token"]
    assert client.request(method, path, json=body).status_code == 403


@pytest.mark.parametrize("method,path,body", ROUTES)
def test_foreign_business(setup, method, path, body):
    client, _ = setup
    assert client.request(method, path + "?business_id=999", json=body).status_code == 404


def test_foreign_ad(setup):
    client, world = setup
    world.repository.save(Business(id=999, user_id=999), Ad(id=999, ad_group_id=999, status="active"))
    for action in ("pause", "resume"):
        assert client.post(f"/api/ads/999/{action}").status_code == 404


def test_pause_resume_only_kill_switch_and_repeated_pause(setup):
    client, world = setup
    world.clock.advance(days=2)
    worker = world.worker()
    ad = next(iter(world.ads.values()))
    asyncio.run(
        worker.apply(
            world.business,
            world.campaign,
            [Operation("set_status", "ad", ad.external_id, "status", "active", "paused_auto")],
            kind="pause",
            reason_code="TEST",
            reason_text="Обращения слишком дорогие.",
        )
    )
    assert client.post("/api/autopilot/pause").status_code == 200
    count = len(world.repository.rows(ChangeLog))
    assert world.campaign.status == "paused_owner"
    stop = next(row for row in world.repository.rows(ChangeLog) if row.reason_code == "OWNER_STOP")
    # Запись видна после выполнения: прошедшее время, а не «Останавливаем».
    assert stop.reason_text == "Остановили рекламу на всех площадках."
    assert client.post("/api/autopilot/pause").status_code == 200
    assert len(world.repository.rows(ChangeLog)) == count
    assert client.post("/api/autopilot/resume").status_code == 200
    assert world.campaign.status == "active"
    assert ad.status == "paused_auto"
    assert world.repository.rows(ChangeLog)[-1].actor == "owner"
    assert client.post("/api/autopilot/pause").status_code == 200
    assert world.campaign.status == "paused_owner"


def test_previously_paused_campaign_not_resumed(setup):
    client, world = setup
    world.campaign.status = "paused_auto"
    asyncio.run(
        world.adapter.apply(
            Operation(
                "set_status",
                "campaign",
                world.campaign.external_id,
                "status",
                "active",
                "paused_auto",
            )
        )
    )
    client.post("/api/autopilot/pause")
    client.post("/api/autopilot/resume")
    assert world.campaign.status == "paused_auto"


def test_owner_can_change_frozen_ad_and_autopilot_waits(setup):
    client, world = setup
    ad = next(iter(world.ads.values()))
    ad.frozen_until = world.clock.now + timedelta(days=7)
    paused = client.post(f"/api/ads/{ad.id}/pause").json()
    assert paused["status"] == "paused_owner" and paused["reason"] is None
    change = world.repository.rows(ChangeLog)[-1]
    assert change.actor == "owner"
    assert change.reason_text == f"Вы остановили объявление «{ad.title}»."
    asyncio.run(
        RollbackService(world.repository, dry_run=False).revert(
            change.id,
            business_id=1,
            adapter=world.adapter,
            now=world.clock.now,
        )
    )
    assert ad.status == "active"
    assert client.post(f"/api/ads/{ad.id}/pause").status_code == 200
    assert client.post(f"/api/ads/{ad.id}/resume").json()["status"] == "active"
    assert world.repository.rows(ChangeLog)[-1].reason_text == f"Вы включили объявление «{ad.title}»."
    ad.frozen_until = None
    with pytest.raises(ChangeConflict, match="24 часа"):
        world.repository.check_autopilot(
            1,
            world.campaign.platform,
            [Operation("set_status", "ad", ad.external_id, "status", "active", "paused_auto")],
            world.clock.now,
        )


def test_budget_stop_and_lower_than_spent(setup):
    client, world = setup
    ledger = world.ledger()[0]
    ledger.spent_kop = 1_500_000
    response = client.put("/api/business/budget", json={"month_budget_kop": 1_400_000})
    assert response.status_code == 409
    assert "потрачен" in response.json()["detail"]
    assert world.business.month_budget_kop == 10_000_000
    ledger.spent_kop = world.business.month_budget_kop
    client.post("/api/autopilot/pause")
    assert client.post("/api/autopilot/resume").status_code == 409
    assert client.post("/api/ads/1/resume").status_code == 409
    assert world.campaign.status == "paused_owner"


def test_status_contract_heartbeat_and_platform_error(setup):
    client, world = setup
    world.repository.save(
        WorkerRun(
            kind="heartbeat",
            business_id=None,
            status="ok",
            started_at=world.clock.now,
            finished_at=world.clock.now,
        )
    )
    response = client.get("/api/autopilot/status").json()
    assert set(response) == {"status", "last_check_at", "auto_continue", "needs_attention"}
    assert response["last_check_at"] is not None
    world.repository.save(
        WorkerRun(
            kind="budget_guard",
            business_id=1,
            status="failed",
            started_at=world.clock.now + timedelta(minutes=1),
        )
    )
    assert any(
        row["title"] == "Ошибка площадки" for row in client.get("/api/autopilot/status").json()["needs_attention"]
    )


def test_ads_period_metrics_and_contract(setup):
    client, world = setup
    ad = next(iter(world.ads.values()))
    for offset, cost in ((0, 900), (15, 2000)):
        world.repository.save(
            StatsDaily(
                business_id=1,
                campaign_id=world.campaign.id,
                ad_id=ad.id,
                platform=world.campaign.platform,
                date=world.clock.now.date() - timedelta(days=offset),
                cost_kop=cost,
                impressions=100,
                clicks=10,
                conversions={"1": 3, "999": 40},
                fetched_at=world.clock.now,
                is_settled=True,
            )
        )
    item = client.get("/api/ads").json()["items"][0]
    assert set(item) == {
        "id",
        "platform",
        "campaign_id",
        "campaign_name",
        "group_id",
        "group_name",
        "title",
        "text",
        "image_url",
        "status",
        "reason",
        "metrics",
    }
    assert item["metrics"]["cost_kop"] == 900
    assert item["metrics"]["leads"] == 3
    assert item["metrics"]["cost_per_lead_kop"] == 300
    campaign = next(row for row in client.get("/api/ads").json()["campaigns"] if row["id"] == world.campaign.id)
    assert set(campaign) == {"id", "platform", "name", "status", "daily"}
    assert len(campaign["daily"]) == 14
    assert campaign["daily"][-1] == {
        "date": world.clock.now.date().isoformat(),
        "cost_kop": 900,
        "leads": 3,
        "is_incomplete": True,
    }
    assert campaign["daily"][0]["cost_kop"] == 0 and not campaign["daily"][0]["is_incomplete"]
    response = client.get("/api/ads?from=2025-12-01&to=2025-12-31")
    assert response.json()["items"][0]["metrics"]["cost_kop"] == 2000
    assert response.json()["campaigns"][0]["daily"] == client.get("/api/ads").json()["campaigns"][0]["daily"]
    assert client.get("/api/ads?from=2026-02-01&to=2026-01-01").status_code == 422
    assert client.get("/api/ads?to=0001-01-03").status_code == 422
    assert client.get("/api/ads?from=0001-01-01&to=0001-01-03").status_code == 200


def test_experiments_and_safe_business(setup):
    client, world = setup
    control, variant = list(world.ads.values())[:2]
    for id_, status in ((1, "running"), (2, "concluded")):
        world.repository.save(
            Experiment(
                id=id_,
                business_id=1,
                ad_group_id=control.ad_group_id,
                hypothesis="text",
                control_ad_id=control.id,
                variant_ad_id=variant.id,
                planned_n_per_arm=2000,
                started_at=world.clock.now,
                deadline=world.clock.now + timedelta(days=14),
                status=status,
                stats={}
                if status == "running"
                else {
                    "result": {
                        "verdict": "no_difference",
                        "reason_code": "EXPERIMENT_NO_DIFF",
                    }
                },
            )
        )
    items = client.get("/api/experiments").json()["items"]
    assert len(items) == 2
    assert "Убедительной разницы" in items[0]["conclusion"]
    assert items[1]["remaining_impressions"] == 4000
    assert "/ads#" not in items[1]["comparing"]
    # Варианты экран показывает из arms; comparing их не повторяет, а говорит, что между ними меняется.
    assert items[1]["comparing"] == "Меняем только текст объявления, картинка та же. До конца проверки: 14 дней."
    assert items[0]["comparing"] == "Меняем только текст объявления, картинка та же."
    assert items[1]["hypothesis"] == "text"
    # Картинку объявлению выбирает запуск; экран отдаёт ссылку на неё у каждого варианта.
    assert control.asset_id and variant.asset_id
    assert items[1]["arms"] == [
        {"title": control.title, "text": control.text, "image": f"/api/plan/assets/{control.asset_id}/file"},
        {"title": variant.title, "text": variant.text, "image": f"/api/plan/assets/{variant.asset_id}/file"},
    ]


def test_experiment_arms_carry_own_text_and_missing_ad_is_empty(setup):
    client, world = setup
    control, variant = list(world.ads.values())[:2]
    variant.title, variant.text = control.title, "Другое начало текста"
    for id_, status, variant_id in ((1, "cancelled", variant.id), (2, "running", 999_999)):
        world.repository.save(
            Experiment(
                id=id_,
                business_id=1,
                ad_group_id=control.ad_group_id,
                hypothesis="text",
                control_ad_id=control.id,
                variant_ad_id=variant_id,
                planned_n_per_arm=2000,
                started_at=world.clock.now,
                deadline=world.clock.now + timedelta(days=14),
                status=status,
                stats={},
            )
        )
    running, cancelled = client.get("/api/experiments").json()["items"]
    assert cancelled["arms"][1] == {
        "title": control.title,
        "text": "Другое начало текста",
        "image": f"/api/plan/assets/{variant.asset_id}/file",
    }
    assert running["arms"][1] == {"title": "", "text": "", "image": None}
    result = client.get("/api/business").json()
    assert result["marking"] == {
        "subject_type": "ip",
        "inn": "",
        "name": "ИП Иванов",
        "phone": "",
        "confirmed_in_platform": {"direct": True},
        "saved": True,
        "inn_masked": "•••4689",
        "phone_masked": "+7•••1122",
    }
    assert "526317984689" not in client.get("/api/business").text
    assert "9001231122" not in client.get("/api/business").text


def test_budget_history_and_next_day(setup):
    client, world = setup
    response = client.put("/api/business/budget", json={"month_budget_kop": 20_000_000})
    assert response.status_code == 200
    assert world.business.month_budget_kop == 20_000_000
    assert world.business.settings["_worker"]["budget_change"] == {
        "before": 10_000_000,
        "effective_on": "2026-01-02",
    }
    change = next(row for row in world.repository.rows(ChangeLog) if row.reason_code == "OWNER_BUDGET")
    assert change.actor == "owner"
    assert "02.01.2026" in change.reason_text


def test_platform_failure_is_human_error(setup, monkeypatch):
    client, world = setup

    async def failed(*args):
        raise PlatformError("Площадка временно недоступна.")

    monkeypatch.setattr(world.adapter, "get_state", failed)
    response = client.post("/api/autopilot/pause")
    assert response.status_code == 409
    assert "подтвердили остановку" in response.json()["detail"]


def test_lower_budget_rebalances_caps_before_resume(setup):
    client, world = setup
    client.post("/api/autopilot/pause")
    old_daily = world.campaign.daily_limit_kop
    assert client.put("/api/business/budget", json={"month_budget_kop": 5_000_000}).status_code == 200
    assert client.post("/api/autopilot/resume").status_code == 200
    assert 0 < world.campaign.daily_limit_kop < old_daily
    assert world.campaign.period_cap_kop <= 5_000_000


def test_settings_values_are_saved(setup):
    client, world = setup
    response = client.put("/api/business/settings", json={"auto_continue": False, "avg_check_kop": None})
    assert response.status_code == 200
    assert world.business.auto_continue is False
    assert world.business.avg_check_kop is None
    assert client.get("/api/business").json()["auto_continue"] is False
    assert client.put("/api/business/settings", json={"auto_continue": True, "avg_check_kop": -1}).status_code == 422


@pytest.mark.parametrize("amount", [0, -1, 1_000_000_001])
def test_budget_bounds_are_validated_before_platform_calls(setup, monkeypatch, amount):
    from unittest.mock import AsyncMock

    client, world = setup
    limits = AsyncMock(side_effect=AssertionError("недопустимый бюджет"))
    monkeypatch.setattr(world.adapter, "min_budgets", limits)
    response = client.put("/api/business/budget", json={"month_budget_kop": amount})
    assert response.status_code == 422
    limits.assert_not_awaited()


def test_budget_below_direct_minimum_is_rejected_with_the_minimum(setup):
    """Директ обязателен: ниже его минимума бюджет не сохраняется. VK к минимуму не прибавляется — её доля, не
    набирающая минимум площадки, уходит в Директ (план), поэтому кабинет VK минимум не меняет."""
    from app.db.models import PlatformAccount

    client, world = setup
    world.repository.save(PlatformAccount(business_id=world.business.id, platform="vk", status="ok"))
    response = client.put("/api/business/budget", json={"month_budget_kop": 1_269_999})
    assert response.status_code == 422
    assert response.json()["detail"].startswith("Минимальный бюджет — 12 700 ₽ в месяц. Яндекс Директ требует")
    assert world.business.month_budget_kop == 10_000_000


def test_autopilot_stop_reason_does_not_repeat_status_label(setup):
    client, world = setup
    ad = next(iter(world.ads.values()))
    world.clock.advance(days=2)
    worker = world.worker()
    worker.step_key = "optimize:test"
    asyncio.run(
        worker.apply(
            world.business,
            world.campaign,
            [Operation("set_status", "ad", ad.external_id, "status", "active", "paused_auto")],
            reason_code="PAUSE_TOO_EXPENSIVE",
            kind="pause",
            reason_params={
                "period_start": "2026-01-01",
                "period_end": "2026-01-05",
                "spend": 21_500,
                "leads": 0,
                "ref_cpl": 10_000,
            },
        )
    )
    row = next(item for item in client.get("/api/ads").json()["items"] if item["id"] == ad.id)
    history = world.repository.rows(ChangeLog)[-1].reason_text
    assert row["status"] == "paused_auto"
    assert history.endswith("слишком дорого при ориентире 100 ₽ за обращение. Остановили объявление.")
    assert row["reason"] == history.removesuffix(" Остановили объявление.")


def test_business_returns_saved_setup_steps(setup):
    client, world = setup
    result = client.get("/api/business").json()
    assert result["metrica_counter_id"] == 12345
    assert result["lead_goal_ids"] == [1]
    assert result["description_approved"] is True
    assert result["medical_level"] == "none"
    world.business.description_approved_at = None
    assert client.get("/api/business").json()["description_approved"] is False


def test_budget_below_vk_minimum_is_saved_with_warning_and_vk_resumes_after_raise(setup):
    from unittest.mock import AsyncMock

    from app.services.optimize import rebalance_budget

    client, world = setup
    world.campaign.platform = "vk"
    world.adapter.min_budgets = AsyncMock(return_value={"daily_limit_kop": 100_000, "period_cap_kop": 30_000})
    response = client.put("/api/business/budget", json={"month_budget_kop": 3_000_000})
    assert response.status_code == 200, response.text
    assert response.json()["warning"] == (
        "Площадка ВКонтакте остановлена: при таком бюджете ей не хватает минимума 1 000 ₽ в день."
    )
    assert world.business.month_budget_kop == 3_000_000
    assert world.adapter._states[world.refs.campaign_id].status == "paused_auto"
    assert world.business.settings["_worker"]["below_minimum"] == [world.campaign.id]
    response = client.put("/api/business/budget", json={"month_budget_kop": 10_000_000})
    assert response.json()["warning"] is None
    world.clock.advance(days=1)
    asyncio.run(rebalance_budget(world.worker(), world.business))
    assert world.adapter._states[world.refs.campaign_id].status == "active"
    assert world.changes(reason_code="BUDGET_REBALANCE", kind="resume")
    assert world.business.settings["_worker"]["below_minimum"] == []
