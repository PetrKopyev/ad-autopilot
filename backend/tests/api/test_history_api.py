"""К1/В1 круга 3: после отката SCALE пересчёт лимитов кампаний идёт только при работающей рекламе.

К1: force-rebalance обходит страж business.status/kill_switch и возобновлял кампании, которые
«Остановить всю рекламу» пропустила (например стоявшие на паузе по минимуму площадки).
В1: сбой пересчёта не должен превращаться в ошибку отката — откат к этому моменту уже применён.
"""

import asyncio
from collections.abc import Iterator
from dataclasses import asdict
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import history
from app.auth.sessions import CSRF_COOKIE, SESSION_COOKIE, get_repository
from app.db.models import Ad, AdGroup, Campaign, ChangeLog, Session, Target, User
from app.platforms.base import ExternalRefs, Operation, PlatformError
from tests.services.test_onboarding import NOW, TODAY, make_world, onboard

SESSION = "history-session"


@pytest.fixture
def api(tmp_path, monkeypatch) -> Iterator[tuple[TestClient, SimpleNamespace]]:
    world = make_world(tmp_path)
    asyncio.run(onboard(world))
    plan = asyncio.run(world.plans.get_plan(world.business.id, NOW, TODAY))
    result = asyncio.run(world.launches.launch(world.business.id, plan["id"], True, NOW, TODAY))
    assert result["status"] == "launched"

    class Auth:
        def get_session(self, value):
            if value == SESSION:
                return Session(id=value, user_id=1, csrf_secret="test-csrf")
            return None

        def get_user(self, user_id):
            return User(id=user_id)

        def get_business(self, user_id, business_id):
            return world.business

    app = FastAPI()
    app.include_router(history.router)
    app.dependency_overrides[get_repository] = Auth
    app.dependency_overrides[history.get_history_repository] = lambda: world.repository
    app.state.platform_adapters = {
        (world.business.id, platform): adapter for platform, adapter in world.adapters.items()
    }
    calls: list[bool] = []
    real_rebalance = history.rebalance_budget

    async def rebalance_spy(worker, business, *, force: bool = False):
        calls.append(force)
        return await real_rebalance(worker, business, force=force)

    monkeypatch.setattr(history, "rebalance_budget", rebalance_spy)
    with TestClient(app, base_url="https://testserver") as client:
        client.cookies.set(SESSION_COOKIE, SESSION)
        client.cookies.set(CSRF_COOKIE, "test-csrf")
        client.headers["X-CSRF-Token"] = "test-csrf"
        yield client, SimpleNamespace(world=world, calls=calls)


def scale_change(world) -> ChangeLog:
    """Применённый SCALE по группе с group_limits в evidence — ветка отката, после которой
    конечная точка пересчитывает лимиты кампаний. Операции согласованы с текущим состоянием
    площадки, чтобы откат проходил без подтверждения."""
    campaign = world.repository.rows(Campaign, business_id=world.business.id)[0]
    group = world.repository.rows(AdGroup, campaign_id=campaign.id)[0]
    group_ids = {row.id for row in world.repository.rows(AdGroup, campaign_id=campaign.id)}
    refs = ExternalRefs(
        campaign.external_id,
        {row.external_id: row.external_id for row in world.repository.rows(AdGroup, campaign_id=campaign.id)},
        {
            row.external_id: row.external_id
            for row in world.repository.rows(Ad)
            if row.ad_group_id in group_ids
        },
        {
            row.external_id: row.external_id
            for row in world.repository.rows(Target)
            if row.ad_group_id in group_ids
        },
    )
    state = asyncio.run(world.adapters[campaign.platform].get_state(refs))[campaign.external_id]
    change = ChangeLog(
        business_id=world.business.id,
        actor="autopilot",
        kind="budget",
        platform=campaign.platform,
        entity_type="group",
        entity_id=group.external_id,
        operations=[
            Operation("set_budget", "campaign", campaign.external_id, "daily_limit_kop", 5_000, state.daily_limit_kop),
            Operation("set_budget", "campaign", campaign.external_id, "period_cap_kop", 150_000, state.period_cap_kop),
        ],
        reason_code="SCALE_UP",
        reason_params={},
        reason_text="Бюджет перенесён на эффективную рекламу.",
        evidence={
            "group_limits": {"before": {group.external_id: 5_000}, "after": {group.external_id: 7_000}},
            "_service": {"refs": asdict(refs)},
        },
        status="applied",
        idempotency_key="scale-1",
    )
    world.repository.add(change)
    # SCALE уже применён: текущие лимиты групп — «after», откат вернёт «before».
    world.business.settings = {"_worker": {"group_limits": {group.external_id: 7_000}}}
    return change


def test_rebalance_after_scale_revert_skipped_when_advertising_stopped(api) -> None:
    client, context = api
    change = scale_change(context.world)
    # Владелец нажал «Остановить всю рекламу»: kill_switch, статус остаётся "running".
    meta = context.world.business.settings.get("_worker", {})
    context.world.business.settings = {"_worker": meta | {"kill_switch": True}}
    response = client.post(f"/api/history/{change.id}/revert")
    assert response.status_code == 200, response.text
    assert context.calls == [], "пересчёт при остановленной рекламе возобновил бы кампании (К1)"


def test_rebalance_after_scale_revert_runs_when_advertising_active(api) -> None:
    client, context = api
    change = scale_change(context.world)
    assert context.world.business.status == "running"
    response = client.post(f"/api/history/{change.id}/revert")
    assert response.status_code == 200, response.text
    assert context.calls == [True], "при работающей рекламе лимиты кампаний должны пересчитаться сразу"


def test_rebalance_failure_does_not_fail_applied_revert(api, monkeypatch) -> None:
    client, context = api
    change = scale_change(context.world)

    async def broken(worker, business, *, force: bool = False):
        raise PlatformError("Площадка недоступна.")

    monkeypatch.setattr(history, "rebalance_budget", broken)
    response = client.post(f"/api/history/{change.id}/revert")
    assert response.status_code == 200, response.text
    reverted = context.world.repository.rows(ChangeLog, reverts_id=change.id)
    assert reverted and reverted[0].status == "applied"


def test_failed_scale_revert_does_not_rebalance(api, monkeypatch) -> None:
    """В2 круга 4: площадка не приняла откат SCALE (failed) — пересчёта лимитов кампаний нет:
    лимиты групп не возвращены, а force-rebalance пересчитал бы бюджет из несостоявшегося отката.
    В записи SCALE — также операция паузы объявления: её inverse (включение) площадка «не принимает»."""
    client, context = api
    change = scale_change(context.world)
    campaign = context.world.repository.rows(Campaign, business_id=context.world.business.id)[0]
    group = context.world.repository.rows(AdGroup, campaign_id=campaign.id)[0]
    ad = context.world.repository.rows(Ad, ad_group_id=group.id)[0]
    adapter = context.world.adapters[campaign.platform]
    # Объявление реально остановлено: ожидаемое состояние в записи совпадает с площадкой, откат без
    # подтверждения; inverse — включение, сломанный apply оставляет на площадке «paused» → failed.
    asyncio.run(adapter.apply(Operation("set_status", "ad", ad.external_id, "status", "active", "paused_auto")))
    change.operations = [
        asdict(Operation("set_status", "ad", ad.external_id, "status", "active", "paused_auto")),
        *change.operations,
    ]

    async def broken(operation):
        raise PlatformError("outage")

    monkeypatch.setattr(adapter, "apply", broken)
    response = client.post(f"/api/history/{change.id}/revert")
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "failed"
    reverted = context.world.repository.rows(ChangeLog, reverts_id=change.id)
    assert reverted and reverted[0].status == "failed"
    group_id = change.entity_id
    assert context.world.business.settings["_worker"]["group_limits"] == {group_id: 7_000}
    assert context.calls == [], "пересчёт после failed-отката ушёл бы в несостоявшийся откат (В2)"


def test_pending_scale_revert_does_not_rebalance(api, monkeypatch) -> None:
    """В2 круга 4: откат остался pending (площадка не ответила, прошёл ли он) — пересчёта нет: до applied записи
    на площадке не подтверждена, лимиты групп в business не возвращены."""
    client, context = api
    change = scale_change(context.world)
    campaign = context.world.repository.rows(Campaign, business_id=context.world.business.id)[0]
    group = context.world.repository.rows(AdGroup, campaign_id=campaign.id)[0]
    ad = context.world.repository.rows(Ad, ad_group_id=group.id)[0]
    adapter = context.world.adapters[campaign.platform]
    asyncio.run(adapter.apply(Operation("set_status", "ad", ad.external_id, "status", "active", "paused_auto")))
    change.operations = [
        asdict(Operation("set_status", "ad", ad.external_id, "status", "active", "paused_auto")),
        *change.operations,
    ]

    apply, get_state = adapter.apply, adapter.get_state
    sent = []

    async def silent_after_apply(refs):
        if sent:
            raise PlatformError("Площадка не ответила.")
        return await get_state(refs)

    async def remember(operation):
        sent.append(operation)
        await apply(operation)

    monkeypatch.setattr(adapter, "apply", remember)
    monkeypatch.setattr(adapter, "get_state", silent_after_apply)
    response = client.post(f"/api/history/{change.id}/revert")
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "pending"
    reverted = context.world.repository.rows(ChangeLog, reverts_id=change.id)
    assert reverted and reverted[0].status == "pending"
    group_id = change.entity_id
    assert context.world.business.settings["_worker"]["group_limits"] == {group_id: 7_000}
    assert context.calls == [], "пересчёт до подтверждения отката пересчитал бы бюджет из несостоявшегося отката"
