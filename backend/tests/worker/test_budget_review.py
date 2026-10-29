from dataclasses import asdict, replace
from datetime import date, timedelta
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest
from integration.harness import BUDGET_KOP, make_world

from app.db.models import Ad, AdGroup, Business, Campaign, ChangeLog, WorkerRun
from app.domain.budget import MINIMUM_MARGIN, campaign_limits, plan_budget
from app.platforms.base import Operation, PlatformError
from app.platforms.fake import FakeAdapter
from app.services.budget_guard import read_spend, stop_all
from app.services.changes import ChangeConflict
from app.services.experiments import group_available
from app.services.optimize import rebalance_budget, snapshot
from app.services.rollback import RollbackService
from app.services.screens import ScreensService
from app.worker.clock import today


def test_period_cap_preserves_spent_and_only_discounts_additional_money():
    assert campaign_limits(1_000, 9_000, date(2026, 1, 5), 50_000) == (800, 55_600)


@pytest.mark.parametrize("evil", [False, True])
async def test_launch_caps_survive_worker_outage_from_first_minute(evil):
    world = await make_world(evil=evil)
    daily, cap = campaign_limits(BUDGET_KOP * 92 // 100 // 31, BUDGET_KOP * 92 // 100, date(2026, 1, 1))
    assert (world.campaign.daily_limit_kop, world.campaign.period_cap_kop) == (daily, cap)
    world.clock.advance(days=10)
    await world.adapter.get_state(world.refs)
    assert world.platform_spend() <= cap * (125 if evil else 100) // 100


@pytest.mark.parametrize("evil", [False, True])
async def test_restart_after_five_days_and_downtime_keeps_advertising(evil):
    world = await make_world(evil=evil)
    await world.advance(world.worker(), days=5)
    ledger_before = world.ledger()[0].spent_kop
    world.clock.advance(hours=0.4)
    assert await world.adapter.get_spend_today() > 0
    assert world.platform_spend() > ledger_before
    restarted = world.worker()
    await restarted.startup()
    assert world.business.status == "running"
    assert (await world.adapter.get_state(world.refs))[world.refs.campaign_id].status == "active"
    before = world.platform_spend()
    await world.advance(restarted, days=3)
    assert world.platform_spend() > before
    assert not world.changes(reason_code="SPEND_MISMATCH")


async def test_startup_stops_real_spend_above_authorized_budget(monkeypatch):
    world = await make_world()
    worker = world.worker()
    authorized = plan_budget(BUDGET_KOP, 0, today(world.clock), ("direct",)).authorized
    monkeypatch.setattr(
        "app.services.budget_guard.read_spend", AsyncMock(return_value=({"direct": authorized + 1}, {}))
    )
    await worker.startup()
    assert world.business.status == "budget_stop"
    assert (await world.adapter.get_state(world.refs))[world.refs.campaign_id].status == "paused_auto"
    assert world.changes(reason_code="SPEND_MISMATCH", status="applied")


async def test_rebalance_with_no_daily_budget_pauses_instead_of_setting_zero(monkeypatch):
    world = await make_world()
    world.business.month_budget_kop = 0
    original = world.adapter.apply
    operations = []

    async def observe(op):
        operations.append(op)
        await original(op)

    monkeypatch.setattr(world.adapter, "apply", observe)
    await rebalance_budget(world.worker(), world.business)
    assert operations and all(op.kind == "set_status" and op.after == "paused_auto" for op in operations)
    assert world.campaign.daily_limit_kop == 0


async def test_pending_outage_does_not_block_other_platform_stop_and_retries(monkeypatch):
    world = await make_world()
    unavailable = FakeAdapter(clock=world.clock)
    refs = await unavailable.create_campaign_tree(replace(unavailable.scenario.plan, id="unavailable"))
    other = Campaign(
        business_id=1,
        platform="vk",
        external_id=refs.campaign_id,
        status="active",
        owner_paused=False,
        daily_limit_kop=100_000,
        period_cap_kop=3_000_000,
    )
    world.repository.save(other)
    world.repository.save(
        ChangeLog(
            business_id=1,
            actor="autopilot",
            kind="pause",
            platform="vk",
            entity_type="campaign",
            entity_id=refs.campaign_id,
            status="pending",
            created_at=world.clock.now,
            operations=[
                asdict(Operation("set_status", "campaign", refs.campaign_id, "status", "active", "paused_auto"))
            ],
            evidence={"_service": {"refs": asdict(refs), "dry_run": False}},
        )
    )
    worker = world.worker()
    worker.resolve_adapter = lambda _, platform: world.adapter if platform == "direct" else unavailable
    world.business.month_budget_kop = 0
    with monkeypatch.context() as outage:
        for method in ("get_state", "get_stats", "get_spend_today"):
            outage.setattr(unavailable, method, AsyncMock(side_effect=PlatformError("Недоступна", status_code=503)))
        with pytest.raises(ChangeConflict):
            await worker.run("budget_guard", 1)
        assert world.adapter._states[world.refs.campaign_id].status == "paused_auto"
        assert unavailable._states[refs.campaign_id].status == "active"
        assert world.business.status == "running"
        assert worker.metadata(world.business)["pending_stop"]
    world.clock.advance(hours=0.5)
    await worker.run("budget_guard", 1)
    assert unavailable._states[refs.campaign_id].status == "paused_auto"
    assert world.business.status == "budget_stop"
    assert not worker.metadata(world.business)["pending_stop"]


async def test_worker_returns_applied_attempt_even_when_later_attempt_failed(monkeypatch):
    world = await make_world()
    worker = world.worker()
    key = f"{worker.step_key}:{world.campaign.id}:retry"
    applied = ChangeLog(business_id=1, status="applied", evidence={"worker_key": key}, idempotency_key=key)
    failed = ChangeLog(business_id=1, status="failed", evidence={"worker_key": key})
    world.repository.save(applied, failed)
    monkeypatch.setattr(worker.changes, "apply_change", AsyncMock(side_effect=AssertionError("Повторная запись")))
    result = await worker.apply(
        world.business,
        world.campaign,
        [Operation("set_status", "campaign", world.refs.campaign_id, "status", "active", "paused_auto")],
        reason_code="OWNER_STOP",
        kind="pause",
        suffix="retry",
    )
    assert result is applied


async def test_worker_retries_failed_attempt_and_moves_key_to_new_change():
    world = await make_world()
    worker = world.worker()
    worker.step_key = "optimize:2026-01-01"
    key = f"{worker.step_key}:{world.campaign.id}:retry"
    failed = ChangeLog(business_id=1, status="failed", evidence={"worker_key": key}, idempotency_key=key)
    world.repository.save(failed)
    operations = [Operation("set_status", "campaign", world.refs.campaign_id, "status", "active", "paused_auto")]
    change = await worker.apply(
        world.business,
        world.campaign,
        operations,
        reason_code="CUSTOM",
        reason_text="Тест.",
        kind="pause",
        suffix="retry",
        actor="system",
    )
    assert change is not failed and change.status == "applied"
    assert (failed.idempotency_key, change.idempotency_key, change.evidence["worker_key"]) == (None, key, key)
    again = await worker.apply(
        world.business,
        world.campaign,
        operations,
        reason_code="CUSTOM",
        reason_text="Тест.",
        kind="pause",
        suffix="retry",
        actor="system",
    )
    assert again is change


@pytest.mark.parametrize(
    "status,evidence,dry_run,outcome",
    [
        ("pending", {}, False, "conflict"),
        ("pending", {"_service": {"dry_run": True}}, True, "same"),
        ("proposed", {}, True, "same"),
        ("pending", {"_service": {"dry_run": True}}, False, "applied"),
        ("proposed", {}, False, "applied"),
        ("applied", {}, False, "same"),
        ("reverted", {}, False, "same"),
    ],
)
async def test_worker_apply_reuses_slot_change_but_never_trusts_unreconciled_pending(
    status, evidence, dry_run, outcome
):
    world = await make_world()
    world.business.settings = world.business.settings | {"dry_run": dry_run}
    worker = world.worker()
    worker.step_key = "budget_guard:2026-01-01T03:00:00+03:00"
    key = f"{worker.step_key}:{world.campaign.id}:stop"
    existing = ChangeLog(business_id=1, status=status, evidence=evidence | {"worker_key": key}, idempotency_key=key)
    world.repository.save(existing)
    if outcome != "applied":
        world.adapter.apply = AsyncMock(side_effect=AssertionError("platform write"))
    operations = [Operation("set_status", "campaign", world.refs.campaign_id, "status", "active", "paused_auto")]
    # Пробный режим касается только решений автопилота.
    actor = "autopilot" if dry_run else "system"
    options = {"reason_code": "CUSTOM", "reason_text": "Тест.", "kind": "pause", "suffix": "stop", "actor": actor}
    if outcome == "conflict":
        # Неподтверждённая остановка не должна считаться выполненной: страж повторит её после сверки.
        with pytest.raises(ChangeConflict):
            await worker.apply(world.business, world.campaign, operations, **options)
        assert world.changes(idempotency_key=key) == [existing]
    elif outcome == "same":
        assert await worker.apply(world.business, world.campaign, operations, **options) is existing
        assert world.changes(idempotency_key=key) == [existing]
    else:
        # Предложение пробного режима не остановило рекламу: после выхода из него действие применяется по-настоящему.
        change = await worker.apply(world.business, world.campaign, operations, **options)
        assert change is not existing and change.status == "applied"
        assert world.changes(idempotency_key=key) == [change]
        # Предложение заменено настоящим изменением и в истории рядом с ним не остаётся.
        assert existing not in world.changes()
        assert world.adapter._states[world.refs.campaign_id].status == "paused_auto"


async def test_stop_all_does_not_mark_budget_stop_on_unreconciled_pending_stop():
    world = await make_world()
    worker = world.worker()
    worker.step_key = "budget_guard:2026-01-01T03:00:00+03:00"
    key = f"{worker.step_key}:{world.campaign.id}:stop_all"
    world.repository.save(ChangeLog(business_id=1, status="pending", evidence={}, idempotency_key=key))
    with pytest.raises(ChangeConflict):
        await stop_all(world.business, worker=worker, reason_code="BUDGET_STOP", budget_stop=True)
    assert world.business.status == "running"
    assert world.business.settings["_worker"]["pending_stop"]["budget_stop"] is True


@pytest.mark.parametrize("key", ["", "explicit"])
async def test_notification_deduplication_keeps_different_targets(key):
    world = await make_world()
    worker = world.worker()
    for target in ("first", "second", "first"):
        worker.notice(world.business, "NOT_SHOWING", "Нет показов", key=key, target_id=target)
    assert [row.log[0]["target_id"] for row in world.repository.rows(WorkerRun)] == ["first", "second"]


@pytest.mark.parametrize("budget", [0, BUDGET_KOP])
async def test_new_month_handles_platform_pause_without_author_and_never_resumes_zero_budget(monkeypatch, budget):
    world = await make_world()
    worker = world.worker()
    await stop_all(
        world.business,
        worker=worker,
        budget_stop=True,
        reason_code="BUDGET_STOP",
        reason_params={"spent": 0, "budget": BUDGET_KOP, "margin": 0},
    )
    world.business.month_budget_kop = budget
    world.clock.advance(days=32)
    original = world.adapter.get_state

    async def platform_states(refs):
        states = await original(refs)
        return {
            key: replace(state, status="paused_owner") if state.status == "paused_auto" else state
            for key, state in states.items()
        }

    monkeypatch.setattr(world.adapter, "get_state", platform_states)
    await worker.run("month_rollover", 1)
    state = (await original(world.refs))[world.refs.campaign_id]
    if budget:
        assert state.status == "active" and state.daily_limit_kop > 0
        assert world.changes(reason_code="BUDGET_RESUME", status="applied")
    else:
        assert state.status == "paused_auto" and world.campaign.daily_limit_kop == 0
        assert not world.changes(reason_code="BUDGET_RESUME")


async def test_scale_is_logged_cools_down_and_can_be_reverted_for_one_group():
    world = await make_world()
    worker = world.worker()
    await world.advance(worker, days=20)
    changes = world.changes(reason_code="SCALE_UP", status="applied")
    assert changes and all(
        change.kind == "budget" and change.operations[0]["kind"] == "set_budget" for change in changes
    )
    for group_id in {change.entity_id for change in changes}:
        times = [change.created_at for change in changes if change.entity_id == group_id]
        assert all(later - earlier >= timedelta(hours=72) for earlier, later in zip(times, times[1:]))
    original = changes[-1]
    _, groups = await snapshot(worker, world.business)
    assert next(group for group in groups if group.id == original.entity_id).last_changed_at == original.created_at
    untouched = world.worker().metadata(world.business)["group_limits"].copy()
    campaign_budget = (world.campaign.daily_limit_kop, world.campaign.period_cap_kop)
    reverted = await RollbackService(world.repository, dry_run=False).revert(
        original.id,
        business_id=1,
        adapter=world.adapter,
        now=world.clock.now,
    )
    assert reverted.status == "applied" and original.status == "reverted"
    assert (world.campaign.daily_limit_kop, world.campaign.period_cap_kop) == campaign_budget
    limits = worker.metadata(world.business)["group_limits"]
    assert all(limits[key] == value for key, value in original.evidence["group_limits"]["before"].items())
    assert all(
        limits[key] == value
        for key, value in untouched.items()
        if key not in original.evidence["group_limits"]["before"]
    )
    assert all(ad.frozen_until is None for ad in world.repository.rows(Ad))
    _, groups = await snapshot(worker, world.business)
    assert [group.id for group in groups if group.frozen_until is not None] == [original.entity_id]
    local = next(group for group in world.repository.rows(AdGroup) if group.external_id == original.entity_id)
    assert not group_available(worker, world.business, local)


async def test_rebalance_stops_campaign_below_platform_minimum_and_resumes_when_budget_is_enough(monkeypatch):
    world = await make_world()
    world.clock.advance(days=2)
    worker = world.worker()
    below = AsyncMock(return_value=30_000)
    monkeypatch.setattr("app.services.optimize.below_platform_minimum", below)
    world.business.month_budget_kop -= 100_000
    await rebalance_budget(worker, world.business)
    (pause,) = world.changes(reason_code="BUDGET_REBALANCE", kind="pause")
    assert "меньше минимума площадки (300 ₽)" in pause.reason_text
    assert world.campaign.status == "paused_auto" and world.campaign.daily_limit_kop > 0
    assert world.business.settings["_worker"]["below_minimum"] == [world.campaign.id]
    below.return_value = None
    world.clock.advance(days=1)
    world.business.month_budget_kop += 50_000
    worker.step_key = "rebalance_budget:next"
    await rebalance_budget(worker, world.business)
    (resume,) = world.changes(reason_code="BUDGET_REBALANCE", kind="resume")
    assert [op["after"] for op in resume.operations if op["field"] == "status"] == ["active"]
    assert world.campaign.status == "active"
    assert world.business.settings["_worker"]["below_minimum"] == []


async def test_direct_minimum_check_uses_weekly_limit_with_spend_of_this_week():
    from datetime import UTC, datetime
    from types import SimpleNamespace

    from app.platforms.base import ExternalRefs, StatRow
    from app.platforms.direct import DirectAdapter
    from app.services.optimize import below_platform_minimum
    from tests.platforms.test_direct import configuration
    from tests.services.test_changes import MemoryRepository

    day, now = date(2026, 11, 30), datetime(2026, 11, 30, 12, tzinfo=UTC)
    adapter = DirectAdapter("test-only", config=configuration())
    adapter._today = lambda: day
    repository = MemoryRepository()
    campaign = Campaign(id=1, external_id="1", platform="direct", daily_limit_kop=10_000, period_cap_kop=500_000)
    repository.entities = {("campaign", "1"): campaign}
    worker = SimpleNamespace(
        repository=repository,
        resolve_adapter=lambda *_: adapter,
        refs=lambda item: ExternalRefs("1", {}, {}, {}),
        clock=SimpleNamespace(now=now),
    )
    operations = [Operation("set_budget", "campaign", "1", "daily_limit_kop", 10_000, 20_000)]
    # Понедельник и последний день месяца: уйдёт 200 ₽ при минимуме 300 ₽, хотя 7 дневных — 1400 ₽.
    adapter.get_stats = AsyncMock(return_value=[])
    assert (
        await below_platform_minimum(
            worker, repository.current_business, campaign, operations, 20_000, 500_000, {"weekly_limit_kop": 30_000}
        )
        == 30_000
    )
    adapter.get_stats = AsyncMock(
        return_value=[StatRow("1", "1", date(2026, 11, 30), "campaign", 1, 1, 10_000, {}, now, False)]
    )
    assert (
        await below_platform_minimum(
            worker, repository.current_business, campaign, operations, 20_000, 500_000, {"weekly_limit_kop": 30_000}
        )
        is None
    )
    await adapter.aclose()


async def test_vk_minimum_check_holds_daily_and_campaign_limits_at_platform_minimum():
    from types import SimpleNamespace

    from app.services.optimize import below_platform_minimum

    adapter = FakeAdapter()
    worker = SimpleNamespace(resolve_adapter=lambda *_: adapter)
    campaign = Campaign(id=1, external_id="1", platform="vk", daily_limit_kop=40_000, period_cap_kop=500_000)
    operations = [Operation("set_budget", "campaign", "1", "daily_limit_kop", 40_000, 29_999)]
    minimum = {"daily_limit_kop": 30_000, "period_cap_kop": 30_000}
    assert (
        await below_platform_minimum(worker, Business(id=1), campaign, operations, 29_999, 500_000, minimum) == 30_000
    )
    assert await below_platform_minimum(worker, Business(id=1), campaign, operations, 30_000, 500_000, minimum) is None


async def test_rebalance_pauses_vk_campaign_below_its_daily_minimum(monkeypatch):
    world = await make_world()
    world.campaign.platform = "vk"
    world.adapter.min_budgets = AsyncMock(return_value={"daily_limit_kop": 10**8, "period_cap_kop": 0})
    world.clock.advance(days=2)
    await rebalance_budget(world.worker(), world.business)
    (pause,) = world.changes(reason_code="BUDGET_REBALANCE", kind="pause")
    assert "Дневной бюджет кампании меньше минимума площадки (1 000 000 ₽ в день)" in pause.reason_text
    assert world.business.settings["_worker"]["below_minimum"] == [world.campaign.id]


def report_pauses_as_owner(adapter: FakeAdapter) -> None:
    """VK и Директ отдают остановленную кампанию как paused_owner: «blocked» и «OFF» не различают, кто остановил."""
    original = adapter.get_state

    async def get_state(refs):
        return {
            key: replace(state, status="paused_owner") if state.status == "paused_auto" else state
            for key, state in (await original(refs)).items()
        }

    adapter.get_state = get_state


async def vk_world(minimum: int):
    world = await make_world()
    world.campaign.platform = "vk"
    world.adapter.min_budgets = AsyncMock(return_value={"daily_limit_kop": minimum, "period_cap_kop": 30_000})
    return world


async def rebalance_next_day(world, minimum: int | Decimal) -> None:
    """minimum — минимум площадки в копейках или (Decimal) доля от дневного лимита, который получит кампания."""
    world.clock.advance(days=1)
    worker = world.worker()
    if isinstance(minimum, Decimal):
        day = today(world.clock)
        spent, _ = await read_spend(worker, world.business)
        started = date.fromisoformat(world.business.settings["_worker"]["started_on"])
        limit = plan_budget(world.business.month_budget_kop, sum(spent.values()), day, ("vk",), started_on=started)
        minimum = int(campaign_limits(limit.limits[0].daily_limit, 0, day)[0] * minimum)
    world.adapter.min_budgets.return_value = {"daily_limit_kop": minimum, "period_cap_kop": 30_000}
    worker.step_key = f"rebalance_budget:{world.clock.now.isoformat()}"
    await rebalance_budget(worker, world.business)


async def test_vk_paused_below_minimum_resumes_although_platform_reports_owner_pause():
    world = await vk_world(10**8)
    await rebalance_next_day(world, 10**8)
    assert world.campaign.status == "paused_auto"
    report_pauses_as_owner(world.adapter)
    assert (await world.adapter.get_state(world.refs))[world.refs.campaign_id].status == "paused_owner"
    _, groups = await snapshot(world.worker(), world.business, include_paused_campaigns=True)
    assert all(group.active for group in groups)
    await rebalance_next_day(world, 30_000)
    (resume,) = world.changes(reason_code="BUDGET_REBALANCE", kind="resume")
    assert (resume.operations[-1]["before"], resume.operations[-1]["after"]) == ("paused_owner", "active")
    assert world.adapter._states[world.refs.campaign_id].status == "active"
    assert world.campaign.status == "active" and world.campaign.daily_limit_kop >= 30_000
    assert world.business.settings["_worker"]["below_minimum"] == []


async def test_vk_near_its_minimum_pauses_once_and_resumes_only_with_margin():
    world = await vk_world(10**8)
    await rebalance_next_day(world, Decimal("1.01"))
    report_pauses_as_owner(world.adapter)
    for minimum in (Decimal("0.95"), Decimal("1.01"), Decimal("0.9")):
        await rebalance_next_day(world, minimum)
    assert len(world.changes(reason_code="BUDGET_REBALANCE", kind="pause")) == 1
    assert not world.changes(reason_code="BUDGET_REBALANCE", kind="resume")
    assert world.adapter._states[world.refs.campaign_id].status == "paused_auto"
    await rebalance_next_day(world, Decimal("0.86"))
    assert len(world.changes(reason_code="BUDGET_REBALANCE", kind="resume")) == 1
    assert world.adapter._states[world.refs.campaign_id].status == "active"


async def test_vk_held_by_minimum_waits_through_new_month_and_resumes_only_with_new_limits():
    world = await vk_world(10**8)
    await rebalance_next_day(world, 10**8)
    held_limit = world.campaign.daily_limit_kop
    worker = world.worker()
    await stop_all(world.business, worker=worker, budget_stop=True, reason_code="BUDGET_STOP")
    assert world.business.status == "budget_stop"
    report_pauses_as_owner(world.adapter)
    world.clock.advance(days=32)
    await worker.run("month_rollover", 1)
    assert not world.changes(reason_code="BUDGET_RESUME")
    assert world.adapter._states[world.refs.campaign_id].status == "paused_auto"
    assert world.business.settings["_worker"]["below_minimum"] == [world.campaign.id]
    await rebalance_next_day(world, 30_000)
    (resume,) = world.changes(reason_code="BUDGET_REBALANCE", kind="resume")
    state = world.adapter._states[world.refs.campaign_id]
    assert state.status == "active" and state.daily_limit_kop == world.campaign.daily_limit_kop
    assert world.campaign.daily_limit_kop != held_limit and world.campaign.daily_limit_kop >= 30_000 * MINIMUM_MARGIN
    assert not world.changes(reason_code="BUDGET_RESUME")


async def test_rebalance_falls_back_to_known_minimums_when_platform_dictionary_fails(caplog):
    world = await make_world()
    world.campaign.platform = "direct"
    world.adapter.min_budgets = AsyncMock(side_effect=PlatformError("Справочник недоступен", status_code=503))
    world.clock.advance(days=2)
    world.business.month_budget_kop -= 100_000
    await rebalance_budget(world.worker(), world.business)
    (change,) = world.changes(reason_code="BUDGET_REBALANCE")
    assert change.kind == "budget" and change.status == "applied"
    assert world.campaign.daily_limit_kop == world.adapter._states[world.refs.campaign_id].daily_limit_kop
    assert "Минимумы площадки direct не прочитаны" in caplog.text


async def set_campaign_status(world, before: str, after: str) -> None:
    await world.adapter.apply(Operation("set_status", "campaign", world.refs.campaign_id, "status", before, after))


async def test_pause_in_platform_cabinet_is_recorded_once_and_kept_by_rebalance_and_month():
    world = await make_world()
    worker = world.worker()
    await set_campaign_status(world, "active", "paused_owner")
    world.clock.advance(days=2)
    await rebalance_budget(worker, world.business)
    (pause,) = world.changes(reason_code="EXTERNAL_PAUSE")
    assert (pause.actor, pause.kind, pause.status) == ("external", "pause", "applied")
    assert pause.reason_text == "Вы остановили кампанию в кабинете площадки — автопилот её не включает."
    assert world.campaign.owner_paused and world.campaign.status == "paused_owner"
    world.clock.advance(days=1)
    worker.step_key = "rebalance_budget:next"
    await rebalance_budget(worker, world.business)
    world.clock.advance(days=32)
    await worker.run("month_rollover", 1)
    assert len(world.changes(reason_code="EXTERNAL_PAUSE")) == 1
    assert not world.changes(kind="resume")
    assert world.adapter._states[world.refs.campaign_id].status == "paused_owner"


async def test_owner_reverting_pause_from_platform_cabinet_lets_autopilot_manage_campaign_again():
    world = await make_world()
    worker = world.worker()
    await set_campaign_status(world, "active", "paused_owner")
    world.clock.advance(days=2)
    await rebalance_budget(worker, world.business)
    (pause,) = world.changes(reason_code="EXTERNAL_PAUSE")
    await RollbackService(world.repository, dry_run=False).revert(
        pause.id, business_id=1, adapter=world.adapter, now=world.clock.now
    )
    world.clock.advance(days=1)
    worker.step_key = "rebalance_budget:next"
    await rebalance_budget(worker, world.business)
    assert world.campaign.status == "active" and not world.campaign.owner_paused
    assert not world.changes(reason_code="EXTERNAL_RESUME")


async def test_vk_held_by_minimum_keeps_pause_after_owner_toggled_it_in_platform_cabinet():
    world = await vk_world(10**8)
    await rebalance_next_day(world, 10**8)
    report_pauses_as_owner(world.adapter)
    await set_campaign_status(world, "paused_auto", "active")
    await rebalance_next_day(world, 10**8)
    await set_campaign_status(world, "active", "paused_owner")
    await rebalance_next_day(world, 30_000)
    assert len(world.changes(reason_code="EXTERNAL_RESUME")) == 1
    assert len(world.changes(reason_code="EXTERNAL_PAUSE")) == 1
    assert not world.changes(reason_code="BUDGET_REBALANCE", kind="resume")
    assert world.adapter._states[world.refs.campaign_id].status == "paused_owner"
    assert world.campaign.owner_paused and world.campaign.status == "paused_owner"


async def test_resume_in_platform_cabinet_is_recorded_and_not_stopped_for_a_day():
    world = await vk_world(10**8)
    await rebalance_next_day(world, 10**8)
    await set_campaign_status(world, "paused_auto", "active")
    await rebalance_next_day(world, 10**8)
    (resume,) = world.changes(reason_code="EXTERNAL_RESUME")
    assert (resume.actor, resume.kind, resume.status) == ("external", "resume", "applied")
    assert world.campaign.status == "active" and not world.campaign.owner_paused
    assert world.campaign.frozen_until == world.clock.now + timedelta(hours=24)
    assert world.business.settings["_worker"]["below_minimum"] == []
    assert len(world.changes(reason_code="BUDGET_REBALANCE", kind="pause")) == 1
    assert world.adapter._states[world.refs.campaign_id].status == "active"
    await rebalance_next_day(world, 10**8)
    assert len(world.changes(reason_code="BUDGET_REBALANCE", kind="pause")) == 2
    assert len(world.changes(reason_code="EXTERNAL_RESUME")) == 1
    assert world.adapter._states[world.refs.campaign_id].status == "paused_auto"


async def test_resume_explains_when_every_campaign_stays_below_platform_minimum():
    world = await vk_world(10**8)
    await rebalance_next_day(world, 10**8)
    await ScreensService(world.worker()).pause(world.business)
    status = await ScreensService(world.worker()).resume(world.business)
    assert status["status"] == "paused"
    assert status["warning"] == (
        "Реклама не включилась: при текущем бюджете площадкам не хватает минимума. Увеличьте бюджет в настройках."
    )


async def test_vk_held_by_minimum_resumes_after_owner_stop_was_lifted():
    world = await vk_world(10**8)
    await rebalance_next_day(world, 10**8)
    report_pauses_as_owner(world.adapter)
    world.repository.save(
        ChangeLog(
            business_id=world.business.id,
            actor="owner",
            kind="pause",
            platform=world.campaign.platform,
            entity_type="campaign",
            entity_id=world.refs.campaign_id,
            operations=[
                asdict(Operation("set_status", "campaign", world.refs.campaign_id, "status", "active", "paused_owner"))
            ],
            reason_code="OWNER_STOP",
            reason_text="Остановили рекламу на всех площадках.",
            evidence={},
            status="applied",
            created_at=world.clock.now,
        )
    )
    await rebalance_next_day(world, 30_000)
    assert world.changes(reason_code="BUDGET_REBALANCE", kind="resume")
    assert world.adapter._states[world.refs.campaign_id].status == "active"


async def test_low_balance_is_notified_once_until_the_account_is_topped_up():
    from app.services.budget_guard import budget_guard

    world = await make_world()
    worker = world.worker()
    world.adapter.balance_kop = world.campaign.daily_limit_kop - 1

    def notices():
        rows = world.repository.rows(WorkerRun, world.business.id, kind="notification")
        return [row.log[0]["text"] for row in rows if row.log[0]["reason_code"] == "BALANCE_LOW"]

    for _ in range(2):
        world.clock.advance(hours=0.5)
        await budget_guard(worker, world.business)
    (text,) = notices()
    assert text.startswith("На счёте Яндекс Директа осталось") and "Пополните счёт" in text
    world.adapter.balance_kop = world.campaign.daily_limit_kop
    world.clock.advance(hours=0.5)
    await budget_guard(worker, world.business)
    assert world.business.settings["_worker"]["balance_low"] == {}
    world.adapter.balance_kop = 0
    world.clock.advance(hours=0.5)
    await budget_guard(worker, world.business)
    assert len(notices()) == 2


async def test_unknown_balance_or_platform_error_does_not_notify(monkeypatch, caplog):
    from app.services.budget_guard import budget_guard

    world = await make_world()
    await budget_guard(world.worker(), world.business)
    monkeypatch.setattr(world.adapter, "get_balance_hint", AsyncMock(side_effect=PlatformError("Нет ответа.")))
    world.clock.advance(hours=0.5)
    with caplog.at_level("WARNING", logger="app.services.budget_guard"):
        await budget_guard(world.worker(), world.business)
    assert "Остаток счёта direct" in caplog.text and "PlatformError: Нет ответа." in caplog.text
    assert not [
        row
        for row in world.repository.rows(WorkerRun, world.business.id, kind="notification")
        if row.log[0]["reason_code"] == "BALANCE_LOW"
    ]
