from dataclasses import asdict, replace
from datetime import date, timedelta
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest
from integration.harness import BUDGET_KOP, make_world

from app.db.models import Ad, AdGroup, Business, Campaign, ChangeLog, WorkerRun
from app.domain.budget import MINIMUM_MARGIN, campaign_limits, plan_budget, stop_margin
from app.platforms.base import Operation, PlatformError, StatRow
from app.platforms.fake import FakeAdapter
from app.services.budget_guard import budget_guard, read_spend, stop_all
from app.services.changes import ChangeConflict
from app.services.experiments import group_available
from app.services.optimize import NO_ACTIVE_ADS, optimize, rebalance_budget, ruble_rates, snapshot
from app.services.rollback import ConfirmationRequired, RollbackService
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
    plan = plan_budget(BUDGET_KOP, 0, today(world.clock), ("direct",))
    authorized = plan.authorized
    from app.config import get_domain_settings

    full = int(Decimal(authorized) / (1 - Decimal(str(get_domain_settings().budget_reserve))))
    # Расход в пределах резерва (между разрешённым с резервом и полным бюджетом) — обычное дело,
    # сверка его не ловит и рекламу не останавливает.
    monkeypatch.setattr(
        "app.services.budget_guard.read_spend", AsyncMock(return_value=({"direct": authorized + 1}, {}))
    )
    await worker.startup()
    assert world.business.status == "running"
    assert not world.changes(reason_code="SPEND_MISMATCH")
    # Выше полного разрешённого — расход невозможен без сбоя: сверка останавливает рекламу.
    monkeypatch.setattr(
        "app.services.budget_guard.read_spend", AsyncMock(return_value=({"direct": full + 1}, {}))
    )
    await worker.startup()
    assert world.business.status == "budget_stop"
    assert (await world.adapter.get_state(world.refs))[world.refs.campaign_id].status == "paused_auto"
    assert world.changes(reason_code="SPEND_MISMATCH", status="applied")


async def test_guard_margin_counts_only_active_campaigns(monkeypatch):
    """Остановленная кампания не тратит: запас стража считается по работающим кампаниям, поэтому порог остановки
    выше, чем если складывать лимиты всех кампаний, — реклама не останавливается раньше времени."""
    from app.config import get_domain_settings

    world = await make_world()
    worker = world.worker()
    margin_all = stop_margin(world.campaign.daily_limit_kop, get_domain_settings())
    assert margin_all > 0
    # Владелец остановил рекламу: кампания на паузе, а её дневной лимит в базе сохранился.
    await world.adapter.apply(
        Operation("set_status", "campaign", world.refs.campaign_id, "status", "active", "paused_owner")
    )
    world.campaign.status = "paused_owner"
    world.repository.save(world.campaign)
    # Расход выше старого порога (бюджет − запас с паузой), но ниже бюджета: новый запас по активным — ноль.
    spent = world.business.month_budget_kop - margin_all // 2
    monkeypatch.setattr("app.services.budget_guard.read_spend", AsyncMock(return_value=({"direct": spent}, {})))
    await budget_guard(worker, world.business)
    assert world.business.status == "running"
    assert not world.changes(reason_code="BUDGET_STOP")
    # Превысили бюджет: страж останавливает рекламу.
    monkeypatch.setattr(
        "app.services.budget_guard.read_spend",
        AsyncMock(return_value=({"direct": world.business.month_budget_kop + 1}, {})),
    )
    await budget_guard(worker, world.business)
    assert world.business.status == "budget_stop"


async def test_read_spend_counts_only_todays_report_rows(monkeypatch):
    """Отчёт со вчерашней датой не считается сегодняшним расходом: весь расход быстрого endpoint идёт в расход
    дня, а вчерашняя строка его не гасит — иначе протухший отчёт занижал бы сегодняшний расход до нуля."""
    world = await make_world()
    world.clock.advance(days=2)
    worker = world.worker()
    day = today(world.clock)
    yesterday = StatRow(
        world.refs.campaign_id,
        world.refs.campaign_id,
        day - timedelta(days=1),
        "campaign",
        0,
        0,
        7_000,
        {},
        worker.clock.now,
        True,
    )
    monkeypatch.setattr(world.adapter, "get_stats", AsyncMock(return_value=[yesterday]))
    monkeypatch.setattr(world.adapter, "get_spend_today", AsyncMock(return_value=3_000))
    spent, lifetime = await read_spend(worker, world.business)
    assert spent == {"direct": 10_000}
    assert lifetime == {world.refs.campaign_id: 7_000}


async def test_resume_margin_skips_owner_paused_but_keeps_system_paused() -> None:
    """Запас для возобновления: кампании, остановленные владельцем, не тратят и в запас не идут; пауза автопилота
    (budget_stop) — идёт: возобновление её снимет, и запас обязан покрывать трату, иначе «Продолжить» превращалось
    бы в мгновенный повторный останов."""
    world = await make_world()
    worker = world.worker()
    await world.advance(worker, days=3)
    spent = world.platform_spend()
    daily = world.campaign.daily_limit_kop
    margin = stop_margin(daily)
    assert margin > 0
    service = ScreensService(world.worker())
    world.business.month_budget_kop = spent + margin // 2
    # Пауза владельцем: запас по ней не считается, возобновить объявление можно, пока остаток положительный.
    world.campaign.owner_paused = True
    world.repository.save(world.campaign)
    await service.check_remaining(world.business)
    # Пауза автопилота: возобновление снимет её — запас обязан её учесть, иначе страж остановит снова сразу.
    world.campaign.owner_paused = False
    world.campaign.status = "paused_auto"
    world.repository.save(world.campaign)
    with pytest.raises(ChangeConflict, match="защитного запаса"):
        await service.check_remaining(world.business)


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



async def test_scale_up_is_spaced_and_reverted_with_confirmation():
    """SCALE_UP одной группы — не чаще раза в 72 часа; откат после дрейфа лимитов требует подтверждения, а после
    него лимиты кампании пересчитываются из возвращённых лимитов групп."""
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
    # Дрейф после SCALE (следующий пересчёт переписал лимиты групп): без подтверждения откат не проходит.
    drifted = world.worker().metadata(world.business)["group_limits"].copy()
    drifted[original.entity_id] += 1
    world.worker().remember(world.business, group_limits=drifted)
    with pytest.raises(ConfirmationRequired):
        await RollbackService(world.repository, dry_run=False).revert(
            original.id, business_id=1, adapter=world.adapter, now=world.clock.now
        )
    reverted = await RollbackService(world.repository, dry_run=False).revert(
        original.id,
        business_id=1,
        adapter=world.adapter,
        now=world.clock.now,
        confirm=True,
    )
    assert reverted.status == "applied" and original.status == "reverted"
    # Абсолютные лимиты кампании запись не восстанавливает: конечная точка после отката зовёт
    # rebalance_budget(force=True) — лимиты кампаний пересчитываются из возвращённых лимитов групп
    # при текущем бюджете, платформа и база согласованы (Н7).
    await rebalance_budget(worker, world.business, force=True)
    limits = world.worker().metadata(world.business)["group_limits"]
    assert all(limits[key] == value for key, value in original.evidence["group_limits"]["before"].items())
    state = (await world.adapter.get_state(world.refs))[world.campaign.external_id]
    assert (state.daily_limit_kop, state.period_cap_kop) == (
        world.campaign.daily_limit_kop,
        world.campaign.period_cap_kop,
    )
    assert all(ad.frozen_until is None for ad in world.repository.rows(Ad))
    _, groups = await snapshot(worker, world.business)
    assert [group.id for group in groups if group.frozen_until is not None] == [original.entity_id]
    local = next(group for group in world.repository.rows(AdGroup) if group.external_id == original.entity_id)
    assert not group_available(worker, world.business, local)


async def test_partially_stopped_group_does_not_donate_to_scale():
    """Н5: частично остановленная группа (одно из двух объявлений остановлено, второе работает — типичная
    картина после A/B-теста с победителем) не донорит лимит: лимит на площадках общий на группу, работающее
    объявление тратит его целиком. Иначе после каждого A/B-теста SCALE снимал бы деньги с группы победителя.
    Полный донор (вторая группа, остановленная целиком) при этом донорит — иначе SCALE не случился бы вовсе."""
    from tests.worker.test_guard_edges import with_vk, worker_for

    world = await make_world()
    vk, _ = await with_vk(world)
    weak = [world.ads["weak-1"], world.ads["weak-2"]]
    # Второе объявление слабой группы заморожено: группа останется работать с одним объявлением.
    weak[1].frozen_until = world.clock.now + timedelta(days=60)
    # Группа borderline остановлена владельцем целиком до перемотки — полный донор для SCALE.
    # Активность решает снимок площадки (snapshot читает статус из платформы), поэтому ставим
    # паузу и в базе, и в состоянии FakeAdapter, как сделал бы штатный stop_all.
    from dataclasses import replace

    borderline = [world.ads["borderline-1"], world.ads["borderline-2"]]
    for ad in borderline:
        world.adapter._states[ad.external_id] = replace(
            world.adapter._states[ad.external_id], status="paused_owner"
        )
        ad.status = "paused_owner"
    world.repository.save(weak[1], *borderline)
    worker = worker_for(world, vk)
    await world.advance(worker, days=20)
    assert sum(ad.status != "active" for ad in weak) == 1, "одно объявление слабой группы остановлено"
    scaled = world.changes(reason_code="SCALE_UP")
    assert scaled, "SCALE должен случиться: без этого проверка доноров пустая и тест ничего не ловит"
    weak_group = next(group for group in world.repository.rows(AdGroup) if group.theme == "Укладки")
    borderline_group = next(
        group for group in world.repository.rows(AdGroup) if group.id == borderline[0].ad_group_id
    )
    donors = {key for change in scaled for key in change.evidence["group_limits"]["before"]}
    assert weak_group.external_id not in donors, "частично остановленная группа не донор"
    assert borderline_group.external_id in donors, "полностью остановленная группа донорит"


async def test_pause_skips_ad_removed_from_platform_between_snapshot_and_apply(monkeypatch):
    """Объявление, удалённое в кабинете между снимком и остановкой, не роняет шаг: пропускается
    только оно, остановки остальных объявлений применяются."""
    world = await make_world()
    worker = world.worker()
    # До первой паузы слабых объявлений: decide уже вынесет PAUSE, но сама остановка ещё не применена.
    await world.advance(worker, hours=216)
    removed = world.ads["weak-1"].external_id
    original = world.adapter.get_state
    calls = 0

    async def get_state(refs):
        nonlocal calls
        calls += 1
        states = await original(refs)
        if calls > 1:
            states = {key: state for key, state in states.items() if key != removed}
        return states

    monkeypatch.setattr(world.adapter, "get_state", get_state)
    await optimize(worker, world.business)
    assert not [change for change in world.changes(kind="pause") if change.entity_id == removed]
    other = world.ads["weak-2"].external_id
    assert [change for change in world.changes(kind="pause") if change.entity_id == other]
    assert world.adapter._states[other].status == "paused_auto"


async def test_scale_up_reason_uses_rubles_from_current_campaign_limit():
    """Доли групп — веса, а не рубли: после утроения бюджета кампании следующая запись SCALE_UP
    называет рубли от нового лимита (примерно втрое выше первой записи), а веса в meta
    group_limits остаются прежними."""
    world = await make_world()
    worker = world.worker()
    await world.advance(worker, days=13)
    (first,) = world.changes(reason_code="SCALE_UP", status="applied")
    # Бюджет вырос втрое — в базе и на площадке; веса групп в meta не менялись.
    world.campaign.daily_limit_kop *= 3
    world.repository.save(world.campaign)
    state = world.adapter._states[world.refs.campaign_id]
    world.adapter._states[world.refs.campaign_id] = replace(state, daily_limit_kop=state.daily_limit_kop * 3)
    # Сырые веса и курс площадки до повторного optimize: записи SCALE_UP должны назвать рубли
    # именно по этому курсу (limit в reason_params из decide, в рублях).
    _, groups = await snapshot(worker, world.business)
    raw = {group.id: group.daily_limit for group in groups}
    rates = ruble_rates(groups, world.repository.rows(AdGroup), worker.current_campaigns(world.business))
    world.clock.advance(days=4)
    worker = world.worker()
    await optimize(worker, world.business)
    (fresh,) = [change for change in world.changes(reason_code="SCALE_UP", status="applied") if change.id != first.id]
    assert fresh.reason_params["old_limit"] == int(raw[fresh.entity_id] * rates["direct"])
    # Утроение бюджета видно в журнале; запас вверх — на шаг SCALE 20% и перенос веса от донора.
    assert 3 * first.reason_params["old_limit"] <= fresh.reason_params["old_limit"] < 4 * first.reason_params[
        "old_limit"
    ]


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


async def test_rebalance_says_campaign_stopped_because_all_its_ads_are_stopped():
    """Автопилот остановил все объявления кампании: запись контроля бюджета говорит это, а не «бюджета нет»."""
    world = await make_world()
    world.clock.advance(days=2)
    for ad in world.repository.rows(Ad):
        await world.adapter.apply(Operation("set_status", "ad", ad.external_id, "status", "active", "paused_auto"))
    await rebalance_budget(world.worker(), world.business)
    (pause,) = world.changes(reason_code="BUDGET_REBALANCE", kind="pause")
    assert pause.reason_text == NO_ACTIVE_ADS and "бюджета нет" not in pause.reason_text


async def test_zero_limit_pause_resumes_when_daily_budget_is_back():
    """Н-1: пауза «Доступного дневного бюджета нет» обнуляет лимит и в held не входит, но с положительным лимитом
    пересчёт возобновляет кампанию честной записью — и повторный пересчёт идемпотентен."""
    world = await make_world()
    worker = world.worker()
    await world.advance(worker, days=2)
    world.business.month_budget_kop = 0
    await rebalance_budget(worker, world.business)
    (pause,) = world.changes(reason_code="BUDGET_REBALANCE", kind="pause")
    assert "Доступного дневного бюджета нет" in pause.reason_text
    assert world.campaign.status == "paused_auto" and world.campaign.daily_limit_kop == 0
    # Денег по-прежнему нет: пауза держится, записей о возобновлении не появляется.
    world.clock.advance(days=1)
    worker.step_key = "rebalance_budget:still-no-money"
    await rebalance_budget(worker, world.business)
    assert not world.changes(reason_code="BUDGET_REBALANCE", kind="resume")
    # Бюджет снова позволяет: кампания снова работает с положительным лимитом.
    world.business.month_budget_kop = BUDGET_KOP
    world.clock.advance(days=1)
    worker.step_key = "rebalance_budget:money-is-back"
    await rebalance_budget(worker, world.business)
    (resume,) = world.changes(reason_code="BUDGET_REBALANCE", kind="resume")
    assert resume.reason_text == "Дневного бюджета снова хватает: пересчитали лимиты и возобновили кампанию."
    assert [op["after"] for op in resume.operations if op["field"] == "status"] == ["active"]
    assert world.campaign.status == "active" and world.campaign.daily_limit_kop > 0
    assert world.adapter._states[world.refs.campaign_id].status == "active"
    assert world.business.settings["_worker"]["below_minimum"] == []
    # Лимиты уже применены: повторный пересчёт без изменений новых записей не пишет.
    count = len(world.changes())
    worker.step_key = "rebalance_budget:again"
    await rebalance_budget(worker, world.business)
    assert len(world.changes()) == count


async def test_zero_limit_pause_resumes_in_new_month():
    """Н-1, перевод месяца: нулевая пауза не переживает смену месяца — её снимает пересчёт лимитов, а цикл
    возобновления дублирующей записи не пишет."""
    world = await make_world()
    worker = world.worker()
    await world.advance(worker, days=2)
    world.business.month_budget_kop = 0
    await rebalance_budget(worker, world.business)
    assert world.campaign.daily_limit_kop == 0 and world.campaign.status == "paused_auto"
    world.business.month_budget_kop = BUDGET_KOP
    world.clock.advance(days=32)
    await worker.run("month_rollover", 1)
    assert world.campaign.status == "active" and world.campaign.daily_limit_kop > 0
    assert world.adapter._states[world.refs.campaign_id].status == "active"
    (resume,) = world.changes(reason_code="BUDGET_REBALANCE", kind="resume")
    assert "Дневного бюджета снова хватает" in resume.reason_text
    assert not world.changes(reason_code="BUDGET_RESUME")


async def test_zero_limit_pause_resumes_after_budget_stop_month_rollover():
    """Н-1 при was_stopped: месяц страж остановил бизнес, а кампания уже стояла с нулевым лимитом — после перевода
    месяца оба ограничения сняты и кампания снова работает."""
    world = await make_world()
    worker = world.worker()
    await world.advance(worker, days=2)
    world.business.month_budget_kop = 0
    await rebalance_budget(worker, world.business)
    assert world.campaign.daily_limit_kop == 0
    await stop_all(world.business, worker=worker, budget_stop=True, reason_code="BUDGET_STOP")
    assert world.business.status == "budget_stop"
    world.business.month_budget_kop = BUDGET_KOP
    world.clock.advance(days=32)
    await worker.run("month_rollover", 1)
    assert world.business.status == "running"
    assert world.campaign.status == "active" and world.campaign.daily_limit_kop > 0
    assert world.adapter._states[world.refs.campaign_id].status == "active"


async def test_zero_limit_pause_does_not_resume_owner_paused_campaign():
    """Н-1: кампания владельца с нулевым лимитом не возобновляется — ни пересчётом, ни переводом месяца."""
    world = await make_world()
    worker = world.worker()
    await world.advance(worker, days=2)
    world.business.month_budget_kop = 0
    await rebalance_budget(worker, world.business)
    assert world.campaign.daily_limit_kop == 0
    world.campaign.owner_paused = True
    world.repository.save(world.campaign)
    world.business.month_budget_kop = BUDGET_KOP
    world.clock.advance(days=32)
    await worker.run("month_rollover", 1)
    assert not world.changes(reason_code="BUDGET_REBALANCE", kind="resume")
    assert not world.changes(reason_code="BUDGET_RESUME")
    # Статус владельца не трогаем: ни возобновления, ни пересчёта лимитов (её группы не участвуют в доле бюджета).
    assert world.campaign.status == "paused_auto" and world.campaign.daily_limit_kop == 0
    assert world.adapter._states[world.refs.campaign_id].status == "paused_auto"


async def test_no_active_ads_pause_is_resumed_when_ads_are_back():
    """Пауза «показывать нечего» поставлена автопилотом: когда появилось работающее объявление и бюджет есть,
    пересчёт сам включает кампанию."""
    world = await make_world()
    worker = world.worker()
    await world.advance(worker, days=2)
    ads = world.repository.rows(Ad)
    for ad in ads:
        await world.adapter.apply(Operation("set_status", "ad", ad.external_id, "status", "active", "paused_auto"))
    await rebalance_budget(worker, world.business)
    (pause,) = world.changes(reason_code="BUDGET_REBALANCE", kind="pause")
    assert pause.reason_text == NO_ACTIVE_ADS and world.campaign.daily_limit_kop == 0
    # Одно объявление снова включили: показывать есть что и бюджет есть, автопилот включает свою кампанию.
    ad = ads[0]
    await world.adapter.apply(Operation("set_status", "ad", ad.external_id, "status", "paused_auto", "active"))
    ad.status = "active"
    world.repository.save(ad)
    world.clock.advance(days=1)
    worker.step_key = "rebalance_budget:ad-is-back"
    await rebalance_budget(worker, world.business)
    (resume,) = world.changes(reason_code="BUDGET_REBALANCE", kind="resume")
    assert "работающие объявления" in resume.reason_text
    assert world.campaign.status == "active" and world.campaign.daily_limit_kop > 0


async def test_no_active_ads_pause_is_not_resumed_under_kill_switch():
    """Владелец остановил всю рекламу после паузы «показывать нечего»: пересчёт (в том числе при откате из истории)
    кампанию не возобновляет."""
    world = await make_world()
    worker = world.worker()
    await world.advance(worker, days=2)
    ads = world.repository.rows(Ad)
    for ad in ads:
        await world.adapter.apply(Operation("set_status", "ad", ad.external_id, "status", "active", "paused_auto"))
    await rebalance_budget(worker, world.business)
    ad = ads[0]
    await world.adapter.apply(Operation("set_status", "ad", ad.external_id, "status", "paused_auto", "active"))
    ad.status = "active"
    world.repository.save(ad)
    worker.remember(world.business, kill_switch=True)
    world.clock.advance(days=1)
    worker.step_key = "rebalance_budget:kill-switch"
    await rebalance_budget(worker, world.business, force=True)
    assert not world.changes(reason_code="BUDGET_REBALANCE", kind="resume")
    assert world.campaign.status == "paused_auto"


async def test_zero_limit_pause_by_owner_is_not_resumed_by_budget_rebalance():
    """Нулевой лимит у кампании, остановленной владельцем (не пересчётом), пересчёт не включает."""
    world = await make_world()
    worker = world.worker()
    await world.advance(worker, days=2)
    await world.adapter.apply(
        Operation("set_status", "campaign", world.campaign.external_id, "status", "active", "paused_owner")
    )
    world.campaign.status = "paused_owner"
    world.campaign.owner_paused = True
    world.campaign.daily_limit_kop = 0
    world.repository.save(world.campaign)
    world.clock.advance(days=1)
    worker.step_key = "rebalance_budget:owner"
    await rebalance_budget(worker, world.business)
    assert not world.changes(reason_code="BUDGET_REBALANCE", kind="resume")
    assert world.adapter._states[world.refs.campaign_id].status == "paused_owner"


async def test_snapshot_group_without_stored_weight_gets_average_weight_not_kopecks():
    """Запасное значение для группы без записи в group_limits — средний вес группы площадки, а не копейки."""
    world = await make_world()
    worker = world.worker()
    await world.advance(worker, days=2)
    _, groups = await snapshot(worker, world.business)
    limits = world.worker().metadata(world.business)["group_limits"]
    missing = groups[0].id
    kept = {key: value for key, value in limits.items() if key != missing}
    world.worker().remember(world.business, group_limits=kept)
    _, groups = await snapshot(worker, world.business)
    group = next(item for item in groups if item.id == missing)
    assert group.daily_limit == sum(kept.values()) // len(kept)
    assert group.daily_limit != world.campaign.daily_limit_kop // len(groups)


async def test_zero_limit_resume_entry_is_protected_from_rollback():
    """Запись о возобновлении нулевой паузы — системная защита бюджета: откат отклоняется честно, кампания продолжает
    работать."""
    world = await make_world()
    worker = world.worker()
    await world.advance(worker, days=2)
    world.business.month_budget_kop = 0
    await rebalance_budget(worker, world.business)
    world.business.month_budget_kop = BUDGET_KOP
    world.clock.advance(days=1)
    worker.step_key = "rebalance_budget:resume"
    await rebalance_budget(worker, world.business)
    (resume,) = world.changes(reason_code="BUDGET_REBALANCE", kind="resume")
    with pytest.raises(ChangeConflict, match="защита бюджета, отменить нельзя"):
        await RollbackService(world.repository, dry_run=False).revert(
            resume.id, business_id=1, adapter=world.adapter, now=world.clock.now
        )
    assert resume.status == "applied"
    assert world.adapter._states[world.refs.campaign_id].status == "active"


async def test_owner_budget_change_is_followed_by_rebalance_journal_entry():
    """М-Б: пересчёт дневных лимитов после смены бюджета владельцем виден в журнале — запись BUDGET_REBALANCE
    с операциями «было → стало» по кампании появляется сразу после OWNER_BUDGET."""
    world = await make_world()
    worker = world.worker()
    await world.advance(worker, days=8)
    old_daily = world.campaign.daily_limit_kop
    await ScreensService(world.worker()).save_budget(world.business, BUDGET_KOP // 2)
    (owner,) = world.changes(reason_code="OWNER_BUDGET")
    rebalance = [row for row in world.changes(reason_code="BUDGET_REBALANCE") if row.kind == "budget"]
    assert rebalance
    entry = rebalance[-1]
    assert entry.created_at >= owner.created_at
    assert {op["field"] for op in entry.operations} == {"daily_limit_kop", "period_cap_kop"}
    assert all(op["before"] != op["after"] for op in entry.operations)
    assert world.campaign.daily_limit_kop < old_daily


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
    # Расход 100 ₽ уже сегодня: потолок недели — 200 ₽ (один день по 200 ₽), сегодняшний расход не
    # прибавляем сверху дважды. 200 ₽ — ниже минимума 300 ₽.
    adapter.get_stats = AsyncMock(
        return_value=[StatRow("1", "1", date(2026, 11, 30), "campaign", 1, 1, 10_000, {}, now, False)]
    )
    assert (
        await below_platform_minimum(
            worker, repository.current_business, campaign, operations, 20_000, 500_000, {"weekly_limit_kop": 30_000}
        )
        == 30_000
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


async def test_vk_rebalance_sends_only_daily_limit_and_keeps_internal_period_cap():
    """VK не хранит лимит периода: на площадку уходит операция только с дневным лимитом, а внутренний учёт
    остатка месяца (campaign.period_cap_kop) продолжается — он нужен для минимума площадки и распределения."""
    world = await make_world()
    world.campaign.platform = "vk"
    world.clock.advance(days=2)
    world.business.month_budget_kop -= 100_000
    before_cap = world.campaign.period_cap_kop
    await rebalance_budget(world.worker(), world.business)
    (change,) = world.changes(reason_code="BUDGET_REBALANCE", kind="budget")
    assert {op["field"] for op in change.operations} == {"daily_limit_kop"}
    assert change.operations[0]["after"] == world.adapter._states[world.refs.campaign_id].daily_limit_kop
    assert world.campaign.period_cap_kop != before_cap and world.campaign.period_cap_kop > 0


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
