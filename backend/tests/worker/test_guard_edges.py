from dataclasses import replace
from datetime import UTC, date, datetime
from unittest.mock import AsyncMock

import pytest
from cryptography.fernet import InvalidToken
from integration.harness import BUDGET_KOP, make_world

from app.db.models import BudgetLedger, Campaign, ChangeLog
from app.domain.budget import stop_margin
from app.platforms.base import AuthExpired, PlatformError
from app.platforms.fake import FakeAdapter
from app.services.budget_guard import stop_all
from app.services.changes import ChangeConflict


class SecondPlatform(FakeAdapter):
    """VK рядом с Директом: может отказать в остановке или потерять токен между записью и сверкой."""

    refuse = False
    expire_after_apply = False

    async def apply(self, operation) -> None:
        if self.refuse:
            raise PlatformError("VK отклонил изменение.", status_code=400)
        await super().apply(operation)
        if self.expire_after_apply:
            self._expired = True

    async def get_state(self, refs):
        if self._expired:
            raise AuthExpired()
        return await super().get_state(refs)


async def with_vk(world) -> tuple[SecondPlatform, Campaign]:
    vk = SecondPlatform(clock=world.clock)
    refs = await vk.create_campaign_tree(replace(vk.scenario.plan, id="vk"))
    campaign = Campaign(
        business_id=1,
        platform="vk",
        external_id=refs.campaign_id,
        status="active",
        owner_paused=False,
        daily_limit_kop=100_000,
        period_cap_kop=3_000_000,
    )
    world.repository.save(campaign)
    return vk, campaign


def worker_for(world, vk: SecondPlatform, *, vk_connected: list[bool] | None = None, closed: Exception | None = None):
    worker = world.worker()
    connected = vk_connected if vk_connected is not None else [True]

    def resolve(business_id: int, platform: str):
        if platform == "direct":
            return world.adapter
        if not connected[0]:
            raise closed or PlatformError("Кабинет площадки не подключён или требует входа заново.")
        return vk

    worker.resolve_adapter = resolve
    return worker


def status(adapter: FakeAdapter, campaign_id: str) -> str:
    return adapter._states[campaign_id].status


async def test_stop_all_with_platform_refusal_stops_others_and_retries_next_tick() -> None:
    world = await make_world()
    vk, campaign = await with_vk(world)
    vk.refuse = True
    worker = worker_for(world, vk)
    world.business.month_budget_kop = 0
    with pytest.raises(ChangeConflict, match="Повторим в следующем тике"):
        await worker.run("budget_guard", 1)
    assert status(world.adapter, world.refs.campaign_id) == "paused_auto"
    assert status(vk, campaign.external_id) == "active"
    assert world.business.status == "running"
    assert worker.metadata(world.business)["pending_stop"]["budget_stop"] is True
    assert [item.status for item in world.changes(platform="vk")] == ["failed"]
    vk.refuse = False
    world.clock.advance(hours=0.5)
    await worker.run("budget_guard", 1)
    assert status(vk, campaign.external_id) == "paused_auto"
    assert world.business.status == "budget_stop"
    assert worker.metadata(world.business)["pending_stop"] is None


async def test_vk_token_expiring_mid_tick_keeps_pending_until_reconnect() -> None:
    world = await make_world()
    vk, campaign = await with_vk(world)
    vk.expire_after_apply = True
    connected = [True]
    worker = worker_for(world, vk, vk_connected=connected)
    world.business.month_budget_kop = 0
    with pytest.raises(ChangeConflict):
        await worker.run("budget_guard", 1)
    pending = world.changes(platform="vk")
    assert [item.status for item in pending] == ["pending"]
    assert status(vk, campaign.external_id) == "paused_auto" and world.business.status == "running"
    connected[0] = False
    world.clock.advance(hours=0.5)
    with pytest.raises(ChangeConflict):
        await worker.run("budget_guard", 1)
    assert [item.status for item in world.changes(platform="vk")] == ["pending"]
    assert world.business.status == "running"
    connected[0], vk._expired, vk.expire_after_apply = True, False, False
    world.clock.advance(hours=0.5)
    await worker.run("budget_guard", 1)
    assert pending[0].status == "applied"
    assert world.business.status == "budget_stop"


@pytest.mark.parametrize("over,stopped", [(0, True), (-1, False)], ids=["at_threshold", "kopeck_below"])
async def test_guard_threshold_is_inclusive(monkeypatch, over, stopped) -> None:
    from app.config import get_domain_settings

    world = await make_world()
    margin = stop_margin(world.campaign.daily_limit_kop, get_domain_settings())
    spent = BUDGET_KOP - margin + over
    monkeypatch.setattr("app.services.budget_guard.read_spend", AsyncMock(return_value=({"direct": spent}, {})))
    await world.worker().run("budget_guard", 1)
    assert (world.business.status == "budget_stop") is stopped
    assert (status(world.adapter, world.refs.campaign_id) == "paused_auto") is stopped


async def test_spend_equal_to_budget_stops_and_is_recorded(monkeypatch) -> None:
    world = await make_world()
    monkeypatch.setattr("app.services.budget_guard.read_spend", AsyncMock(return_value=({"direct": BUDGET_KOP}, {})))
    await world.worker().run("budget_guard", 1)
    assert world.business.status == "budget_stop"
    ledger = world.ledger()[0]
    assert ledger.spent_kop == BUDGET_KOP and ledger.hard_stop_at == world.clock.now
    assert world.changes(reason_code="BUDGET_STOP", status="applied")


async def test_reverting_owner_stop_resumes_business() -> None:
    """Мелочь 3 круга 7: отмена «Остановить всю рекламу» из истории возвращает бизнес в работу — кампании
    включены, а kill_switch и пауза не остаются: иначе главная показывала «на паузе», хотя расход идёт."""
    from app.services.rollback import RollbackService

    world = await make_world()
    worker = world.worker()
    await stop_all(world.business, worker=worker, actor="owner")
    assert world.business.status == "paused"
    assert worker.metadata(world.business)["kill_switch"] is True
    (stop,) = world.changes(reason_code="OWNER_STOP")
    await RollbackService(world.repository).revert(
        stop.id, business_id=world.business.id, adapter=world.adapter
    )
    assert status(world.adapter, world.refs.campaign_id) == "active"
    assert "kill_switch" not in worker.metadata(world.business)
    assert world.business.status == "running"


async def test_guard_survives_unconfirmed_pending_stop(monkeypatch) -> None:
    """В1 круга 7: страж бюджета не падает, пока остановка из pending_stop не подтверждена площадкой:
    расход читается, pending_stop остаётся до следующего тика."""
    world = await make_world()
    worker = world.worker()
    worker.remember(world.business, pending_stop={"reason_code": "OWNER_STOP", "budget_stop": False})
    monkeypatch.setattr(
        "app.services.budget_guard.stop_all", AsyncMock(side_effect=ChangeConflict("Площадки ещё подтверждают."))
    )
    read_spend = AsyncMock(return_value=({"direct": 0}, {}))
    monkeypatch.setattr("app.services.budget_guard.read_spend", read_spend)
    await worker.run("budget_guard", 1)
    read_spend.assert_awaited_once()
    assert worker.metadata(world.business)["pending_stop"]["reason_code"] == "OWNER_STOP"


async def test_new_month_resumes_campaign_ended_by_finish_date(monkeypatch) -> None:
    """В4 круга 7: кампания с State=ENDED (конец по дате окончания) на переходе месяца возобновляется,
    а не числится паузой владельца, которую month_rollover пропустил бы."""
    world = await make_world()
    worker = world.worker()
    real_get_state = world.adapter.get_state

    async def ended_state(refs):
        states = await real_get_state(refs)
        # ENDED держим, пока кампания реально не включилась (после продления и resume площадка отвечает active).
        return {
            key: replace(state, status="paused_auto", ended=True)
            if state.entity_type == "campaign" and world.adapter._states[key].status != "active"
            else state
            for key, state in states.items()
        }

    monkeypatch.setattr(world.adapter, "get_state", ended_state)
    # Кампания в кабинете закончилась по дате (Direct State=ENDED маппится в paused_auto + ended).
    world.adapter._states[world.refs.campaign_id] = replace(
        world.adapter._states[world.refs.campaign_id], status="paused_auto"
    )
    move_to(world, datetime(2026, 1, 31, 21, tzinfo=UTC))
    await worker.run("month_rollover", 1)
    assert status(world.adapter, world.refs.campaign_id) == "active"
    assert not world.campaign.owner_paused
    assert world.changes(reason_code="MONTH_RESUME", status="applied")


@pytest.mark.parametrize("hold", ["owner_paused", "below_minimum"])
async def test_new_month_only_extends_campaign_held_by_owner_or_minimum(monkeypatch, hold) -> None:
    """В2 круга 8 + мелочь 3 круга 9: кампания, которую владелец остановил сам (owner_paused) или которая
    на паузе по минимуму площадки (below_minimum), и которая дошла до ENDED к концу месяца, на переходе
    месяца только продлевается, но не включается — включение решает владелец или пересчёт лимитов."""
    world = await make_world()
    worker = world.worker()
    real_get_state = world.adapter.get_state
    extended = []

    async def ended_state(refs):
        states = await real_get_state(refs)
        return {
            key: replace(state, status="paused_auto", ended=True)
            if state.entity_type == "campaign"
            else state
            for key, state in states.items()
        }

    async def extend_end_date(campaign_id):
        extended.append(campaign_id)

    monkeypatch.setattr(world.adapter, "get_state", ended_state)
    monkeypatch.setattr(world.adapter, "extend_end_date", extend_end_date, raising=False)
    world.adapter._states[world.refs.campaign_id] = replace(
        world.adapter._states[world.refs.campaign_id], status="paused_auto"
    )
    if hold == "owner_paused":
        world.campaign.owner_paused = True
    else:
        worker.remember(world.business, below_minimum=[world.campaign.id])
    move_to(world, datetime(2026, 1, 31, 21, tzinfo=UTC))
    await worker.run("month_rollover", 1)
    assert extended == [world.refs.campaign_id], "дату окончания продлили"
    assert status(world.adapter, world.refs.campaign_id) == "paused_auto"
    assert not world.changes(reason_code="MONTH_RESUME"), "включения нет: кампанию держат паузой"


def move_to(world, moment: datetime) -> None:
    world.clock.advance(hours=(moment - world.clock.now).total_seconds() / 3600)
    assert world.clock.now == moment


async def test_new_month_starts_at_midnight_moscow_time() -> None:
    world = await make_world()
    worker = world.worker()
    await stop_all(
        world.business,
        worker=worker,
        budget_stop=True,
        reason_code="BUDGET_STOP",
        reason_params={"spent": 0, "budget": BUDGET_KOP, "margin": 0},
    )
    assert world.business.status == "budget_stop"
    move_to(world, datetime(2026, 1, 31, 20, 59, 59, tzinfo=UTC))
    await worker.run("month_rollover", 1)
    assert worker.metadata(world.business)["month"] == "2026-01-01"
    assert world.business.status == "budget_stop"
    assert status(world.adapter, world.refs.campaign_id) == "paused_auto"
    move_to(world, datetime(2026, 1, 31, 21, tzinfo=UTC))
    await worker.run("month_rollover", 1)
    assert worker.metadata(world.business)["month"] == "2026-02-01"
    assert world.business.status == "running"
    assert status(world.adapter, world.refs.campaign_id) == "active"
    assert world.changes(reason_code="BUDGET_RESUME", status="applied")


async def test_guard_books_spend_to_new_month_after_moscow_midnight(monkeypatch) -> None:
    world = await make_world()
    worker = world.worker()
    monkeypatch.setattr("app.services.budget_guard.read_spend", AsyncMock(return_value=({"direct": 1_000}, {})))
    move_to(world, datetime(2026, 1, 31, 20, 59, tzinfo=UTC))
    await worker.run("budget_guard", 1)
    move_to(world, datetime(2026, 1, 31, 21, 1, tzinfo=UTC))
    await worker.run("budget_guard", 1)
    months = sorted(row.month for row in world.repository.rows(BudgetLedger, 1))
    assert months == [date(2026, 1, 1), date(2026, 2, 1)]
    assert not world.repository.rows(ChangeLog, 1, reason_code="BUDGET_STOP")


@pytest.mark.parametrize("closed", [None, KeyError("vk"), InvalidToken()], ids=["expired", "deleted", "undecryptable"])
async def test_closed_vk_account_does_not_switch_off_direct_budget_guard(closed) -> None:
    world = await make_world()
    vk, campaign = await with_vk(world)
    worker = worker_for(world, vk, vk_connected=[False], closed=closed)
    world.business.month_budget_kop = 0
    with pytest.raises(ChangeConflict):
        await worker.run("budget_guard", 1)
    assert status(world.adapter, world.refs.campaign_id) == "paused_auto"
    assert status(vk, campaign.external_id) == "active" and world.business.status == "running"
    assert worker.metadata(world.business)["pending_stop"]["budget_stop"] is True


async def test_guard_margin_counts_only_campaigns_of_current_plan(monkeypatch) -> None:
    """Кампания прежнего плана остановлена: её дневной лимит не тратится и не раздувает запас стража, иначе он
    останавливал бы рекламу раньше времени."""
    from app.config import get_domain_settings
    from app.db.models import Plan

    world = await make_world()
    vk, campaign = await with_vk(world)
    campaign.status = "paused_auto"
    world.repository.save(
        Plan(business_id=1, version=0, status="failed", payload={"launch": {"campaigns": {"vk": campaign.id}}})
    )
    margin = stop_margin(world.campaign.daily_limit_kop, get_domain_settings())
    spent = BUDGET_KOP - margin - 1
    monkeypatch.setattr("app.services.budget_guard.read_spend", AsyncMock(return_value=({"direct": spent}, {})))
    await worker_for(world, vk).run("budget_guard", 1)
    assert world.business.status == "running"
    assert status(world.adapter, world.refs.campaign_id) == "active"


async def test_late_spend_does_not_turn_owner_pause_into_budget_stop(monkeypatch) -> None:
    """Владелец остановил рекламу, потом площадка уточнила расход до бюджета: страж не меняет паузу на «бюджет
    израсходован», а новый месяц и «Продолжить» ведут себя как при обычной паузе."""
    from app.services.screens import ScreensService

    world = await make_world()
    worker = world.worker()
    await stop_all(world.business, worker=worker, actor="owner")
    assert world.business.status == "paused"
    spend = AsyncMock(return_value=({"direct": BUDGET_KOP}, {}))
    monkeypatch.setattr("app.services.budget_guard.read_spend", spend)
    await worker.run("budget_guard", 1)
    assert world.business.status == "paused"
    assert "израсходован" not in str(ScreensService(worker).status(world.business))
    assert status(world.adapter, world.refs.campaign_id) == "paused_owner"
    move_to(world, datetime(2026, 2, 1, 6, tzinfo=UTC))
    spend.return_value = ({"direct": 0}, {})
    await worker.run("month_rollover", 1)
    assert world.business.status == "paused"
    assert status(world.adapter, world.refs.campaign_id) == "paused_owner"
    await ScreensService(worker).resume(world.business)
    assert world.business.status == "running"
    assert status(world.adapter, world.refs.campaign_id) == "active"
