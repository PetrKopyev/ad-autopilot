from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
from unittest.mock import AsyncMock

import httpx
import pytest

from app.db.models import AdGroup, BudgetLedger, Campaign, WorkerRun
from app.domain.budget import campaign_limits, plan_budget
from app.domain.types import GroupSnapshot
from app.platforms.base import PlatformError
from app.platforms.fake import FakeAdapter
from app.services.budget_guard import budget_guard, stop_all
from app.services.changes import ChangeConflict
from app.services.month import month_rollover
from app.services.optimize import experiments_tick, rebalance_budget, snapshot
from app.services.registry import AccountInfo, AdapterRegistry
from app.services.screens import ScreensService
from app.worker.clock import today
from tests.integration.harness import BUDGET_KOP, make_world
from tests.services.test_experiments import running_experiment


def reserve_settings(monkeypatch):
    from app.config import Settings, get_domain_settings

    monkeypatch.setenv("BUDGET_RESERVE_PCT", "15")
    config = Settings()
    monkeypatch.setattr("app.config.settings", config)
    assert get_domain_settings().budget_reserve == Decimal("0.15")
    return get_domain_settings()


@pytest.mark.parametrize("rollover", [False, True])
async def test_reserve_from_env_reaches_launch_and_rebalance(monkeypatch, rollover):
    domain = reserve_settings(monkeypatch)
    world = await make_world()
    worker = world.worker()
    initial = plan_budget(BUDGET_KOP, 0, today(world.clock), ("direct",), settings=domain).limits[0]
    assert (world.campaign.daily_limit_kop, world.campaign.period_cap_kop) == campaign_limits(
        initial.daily_limit, initial.remaining_cap, today(world.clock)
    )
    if rollover:
        await stop_all(
            world.business, worker=worker, reason_code="BUDGET_STOP", budget_stop=True,
            reason_params={"spent": 0, "budget": BUDGET_KOP, "margin": 0},
        )
        world.clock.advance(days=31)
        await month_rollover(worker, world.business)
        assert world.business.status == "running"
    else:
        # Force a cap change so this also verifies the platform update.
        world.business.month_budget_kop = BUDGET_KOP * 2
        await rebalance_budget(worker, world.business)
    expected = plan_budget(
        world.business.month_budget_kop, 0, today(world.clock), ("direct",), settings=domain
    ).limits[0]
    assert (world.campaign.daily_limit_kop, world.campaign.period_cap_kop) == campaign_limits(
        expected.daily_limit, expected.remaining_cap, today(world.clock)
    )
    state = (await world.adapter.get_state(worker.refs(world.campaign)))[world.campaign.external_id]
    assert (state.daily_limit_kop, state.period_cap_kop) == (
        world.campaign.daily_limit_kop, world.campaign.period_cap_kop
    )


async def test_guard_reconciliation_uses_full_allowed_without_reserve(monkeypatch):
    domain = reserve_settings(monkeypatch)
    world = await make_world()
    allowed = plan_budget(BUDGET_KOP, 0, today(world.clock), ("direct",), settings=domain).authorized
    full = int(Decimal(allowed) / (1 - domain.budget_reserve))
    # В пределах резерва (между разрешённым и полным) сверка не останавливает: резерв — на задержку отчётов.
    monkeypatch.setattr(
        "app.services.budget_guard.read_spend", AsyncMock(return_value=({"direct": allowed + 1}, {}))
    )
    await budget_guard(world.worker(), world.business, reconcile=True)
    assert world.business.status == "running"
    assert not world.changes(reason_code="SPEND_MISMATCH")
    # Выше полного разрешённого — срабатывает сверка расхода.
    monkeypatch.setattr("app.services.budget_guard.read_spend", AsyncMock(return_value=({"direct": full + 1}, {})))
    await budget_guard(world.worker(), world.business, reconcile=True)
    assert world.business.status == "budget_stop"
    assert world.changes(reason_code="SPEND_MISMATCH")


async def test_vk_package_id_from_env_is_passed_through_registry(monkeypatch):
    from app.config import Settings

    monkeypatch.setenv("VK_PACKAGE_ID", "321")
    config = Settings()
    info = AccountInfo("ok", "https://salon.example", None, True, "test-only")
    async with httpx.AsyncClient() as http:
        registry = AdapterRegistry(lambda *_: info, mode="live", http=http, config=config)
        assert registry.resolve(1, "vk")._package_id == 321


async def test_extension_failure_preserves_limits_continues_other_campaigns_and_retries(monkeypatch):
    world = await make_world()
    worker = world.worker()
    ads, groups = await snapshot(worker, world.business)
    other_adapter = FakeAdapter(clock=world.clock)
    refs = await other_adapter.create_campaign_tree(replace(other_adapter.scenario.plan, id="other"))
    other = Campaign(
        business_id=world.business.id, platform="vk", external_id=refs.campaign_id,
        daily_limit_kop=100_000, period_cap_kop=1_000_000, status="active", owner_paused=False,
    )
    world.repository.save(other)
    for index, external_id in enumerate(refs.groups.values()):
        world.repository.save(AdGroup(campaign_id=other.id, external_id=external_id, theme=f"other-{index}"))
    worker.resolve_adapter = lambda _, platform: world.adapter if platform == "direct" else other_adapter
    # Use real snapshots for Direct; active VK groups suffice to verify the subsequent update.
    groups.extend(GroupSnapshot(id_, "vk", 50_000, True) for id_ in refs.groups.values())
    monkeypatch.setattr("app.services.optimize.snapshot", AsyncMock(return_value=(ads, groups)))
    seen = []

    async def fail_extension(campaign_id):
        state = (await world.adapter.get_state(worker.refs(world.campaign)))[campaign_id]
        assert world.campaign.daily_limit_kop == state.daily_limit_kop
        assert world.campaign.period_cap_kop == state.period_cap_kop
        seen.append(campaign_id)
        raise PlatformError("Продление временно недоступно")

    monkeypatch.setattr(world.adapter, "extend_end_date", fail_extension, raising=False)
    await rebalance_budget(worker, world.business)
    assert seen == [world.campaign.external_id]
    assert any(change.platform == "vk" for change in world.changes(reason_code="BUDGET_REBALANCE"))
    assert world.repository.find(BudgetLedger, business_id=world.business.id, platform="vk") is not None
    notices = [run for run in world.repository.rows(WorkerRun) if run.kind == "notification"]
    assert any(run.log[0]["reason_code"] == "CAMPAIGN_EXTEND_FAILED" for run in notices)
    retry = AsyncMock()
    monkeypatch.setattr(world.adapter, "extend_end_date", retry)
    worker.step_key = "next-rebalance"
    await rebalance_budget(worker, world.business)
    retry.assert_awaited_once_with(world.campaign.external_id)


@pytest.mark.parametrize("status,message", [("running", "Реклама уже работает"), ("draft", "Сначала запустите рекламу"),
                                             ("plan_ready", "Сначала запустите рекламу")])
async def test_resume_rejects_invalid_business_status_without_platform_calls(monkeypatch, status, message):
    world = await make_world()
    world.business.status = status
    service = ScreensService(world.worker())
    rebalance = AsyncMock()
    monkeypatch.setattr("app.services.screens.rebalance_budget", rebalance)
    monkeypatch.setattr(world.adapter, "get_stats", AsyncMock(side_effect=AssertionError("Не нужен запрос")))
    with pytest.raises(ChangeConflict, match=message):
        await service.resume(world.business)
    assert world.business.status == status
    rebalance.assert_not_called()


@pytest.mark.parametrize("status", ["paused", "budget_stop"])
async def test_empty_resume_preserves_status_and_stop_metadata(monkeypatch, status):
    world = await make_world()
    worker = world.worker()
    await stop_all(
        world.business, worker=worker, reason_code="BUDGET_STOP", budget_stop=status == "budget_stop",
        reason_params={"spent": 0, "budget": BUDGET_KOP, "margin": 0},
    )
    world.campaign.daily_limit_kop = 0
    meta = worker.metadata(world.business)
    monkeypatch.setattr("app.services.screens.rebalance_budget", AsyncMock())
    await ScreensService(worker).resume(world.business)
    assert world.business.status == status
    assert worker.metadata(world.business) == meta


async def test_budget_raise_while_running_is_deferred_and_resume_cannot_bypass_it():
    world = await make_world()
    initial_daily = world.campaign.daily_limit_kop
    service = ScreensService(world.worker())
    await service.save_budget(world.business, BUDGET_KOP * 2)
    assert world.campaign.daily_limit_kop == initial_daily
    with pytest.raises(ChangeConflict, match="Реклама уже работает"):
        await service.resume(world.business)
    assert world.campaign.daily_limit_kop == initial_daily
    world.clock.advance(days=1)
    await rebalance_budget(world.worker(), world.business)
    assert world.campaign.daily_limit_kop > initial_daily


async def test_expired_dry_run_experiments_are_idempotent_across_ticks_and_can_later_apply(monkeypatch):
    world = await make_world()
    first = running_experiment(world)
    second = running_experiment(world)
    # За день после срока (14-й день — ещё вывод по данным, см. К4).
    world.clock.advance(days=15)
    monkeypatch.setattr("app.services.optimize.snapshot", AsyncMock(return_value=([], [])))
    for _ in range(3):
        await experiments_tick(world.worker(dry_run=True), world.business)
        world.clock.advance(hours=0.5)
    proposed = world.changes(kind="experiment_verdict", status="proposed")
    assert len(proposed) == 2
    assert {change.evidence["experiment_id"] for change in proposed} == {first.id, second.id}
    assert first.status == second.status == "running"
    await experiments_tick(world.worker(), world.business)
    assert first.status == second.status == "concluded"
    assert len(world.changes(kind="experiment_verdict", status="applied")) == 2


@pytest.mark.parametrize("seconds", [1, 60, 1800, 3600])
async def test_guard_observed_spend_uses_at_least_poll_interval(monkeypatch, seconds):
    world = await make_world()
    ledger = world.repository.rows(BudgetLedger)[0]
    ledger.spent_kop = 1_000_000
    ledger.spent_at = world.clock.now - timedelta(seconds=seconds)
    monkeypatch.setattr("app.services.budget_guard.read_spend", AsyncMock(return_value=({"direct": 2_000_000}, {})))
    await budget_guard(world.worker(), world.business)
    assert world.business.status == "running"
    assert not world.changes(reason_code="BUDGET_STOP")
    assert ledger.spent_kop == 2_000_000


async def test_guard_still_stops_near_budget_after_batched_stats(monkeypatch):
    world = await make_world()
    ledger = world.repository.rows(BudgetLedger)[0]
    ledger.spent_kop = BUDGET_KOP - 2000
    ledger.spent_at = world.clock.now - timedelta(seconds=1)
    monkeypatch.setattr(
        "app.services.budget_guard.read_spend", AsyncMock(return_value=({"direct": BUDGET_KOP - 1}, {}))
    )
    await budget_guard(world.worker(), world.business)
    assert world.business.status == "budget_stop"
    assert world.changes(reason_code="BUDGET_STOP")


def test_media_directory_has_local_default_and_uses_config(monkeypatch, tmp_path):
    from pathlib import Path

    from app.api.plan import media_root
    from app.config import Settings

    monkeypatch.delenv("MEDIA_DIR", raising=False)
    assert Settings().media_dir == Path("media")
    monkeypatch.setenv("MEDIA_DIR", str(tmp_path))
    config = Settings()
    monkeypatch.setattr("app.config.settings", config)
    # All users of the directory take the parsed setting, without rereading the environment.
    monkeypatch.setenv("MEDIA_DIR", str(tmp_path / "different"))
    assert media_root() == tmp_path


async def test_guard_with_full_reserve_does_not_crash_on_zero_division(monkeypatch):
    """В4: резерв 100% — вырожденный, но валидный конфиг; страж считает верхнюю границу сверки
    с нулевым резервом и не падает на обращении доли (1 - 1 = 0)."""
    from app.config import Settings, get_domain_settings

    world = await make_world()
    monkeypatch.setenv("BUDGET_RESERVE_PCT", "100")
    monkeypatch.setattr("app.config.settings", Settings())
    assert get_domain_settings().budget_reserve == Decimal("1")
    monkeypatch.setattr("app.services.budget_guard.read_spend", AsyncMock(return_value=({"direct": 1}, {})))
    await budget_guard(world.worker(), world.business, reconcile=True)
    # Расход в пределах полного месячного бюджета не останавливает рекламу — краха нет, сверка честная.
    assert world.business.status == "running"
    monkeypatch.setattr(
        "app.services.budget_guard.read_spend", AsyncMock(return_value=({"direct": BUDGET_KOP + 1}, {}))
    )
    await budget_guard(world.worker(), world.business, reconcile=True)
    # Сверх полного разрешённого — сверка останавливает рекламу и при резерве 100%.
    assert world.business.status == "budget_stop"
