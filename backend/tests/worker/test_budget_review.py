from dataclasses import asdict, replace
from datetime import date, timedelta
from unittest.mock import AsyncMock

import pytest
from integration.harness import BUDGET_KOP, make_world

from app.db.models import Ad, AdGroup, Campaign, ChangeLog, WorkerRun
from app.domain.budget import plan_budget
from app.platforms.base import Operation, PlatformError
from app.platforms.fake import FakeAdapter
from app.services.budget_guard import stop_all
from app.services.changes import ChangeConflict
from app.services.experiments import group_available
from app.services.optimize import rebalance_budget, snapshot
from app.services.plan import campaign_limits
from app.services.rollback import RollbackService
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
    options = {"reason_code": "CUSTOM", "reason_text": "Тест.", "kind": "pause", "suffix": "stop", "actor": "system"}
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
        assert (existing.idempotency_key, world.changes(idempotency_key=key)) == (None, [change])
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
