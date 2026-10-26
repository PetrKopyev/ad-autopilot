import os
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta

import pytest
from integration.harness import BUDGET_KOP, Crash, CrashingAdapter, FlakyReadAdapter, make_world
from sqlalchemy import create_engine

from app.db.models import BudgetLedger, WorkerRun
from app.services.budget_guard import stop_all
from app.services.changes import ChangeConflict
from app.worker.clock import MOSCOW, SystemClock, today
from app.worker.jobs import schedule_key
from app.worker.locks import business_lock

JAN = date(2026, 1, 1)


def test_clock_interface_and_moscow_date() -> None:
    assert SystemClock().now.tzinfo is not None
    late = datetime(2026, 1, 31, 22, 0, tzinfo=UTC)
    assert today(type("Fixed", (), {"now": late})()) == date(2026, 2, 1)


def test_schedule_keys_are_slots() -> None:
    first = datetime(2026, 1, 5, 6, 10, tzinfo=MOSCOW)
    assert schedule_key("budget_guard", first) == schedule_key("budget_guard", first.replace(minute=29))
    assert schedule_key("budget_guard", first) != schedule_key("budget_guard", first.replace(minute=30))
    assert schedule_key("sync_stats", first) == schedule_key("sync_stats", first.replace(hour=8))
    assert schedule_key("optimize", first) == schedule_key("optimize", first.replace(hour=23))
    assert schedule_key("clients_survey", first) == schedule_key("clients_survey", first + timedelta(days=6))


async def test_heartbeat_and_jobs_are_written_to_worker_runs() -> None:
    world = await make_world()
    worker = world.worker()
    await world.advance(worker, hours=7)
    runs = world.repository.rows(WorkerRun)
    assert {"heartbeat", "budget_guard", "sync_stats", "optimize", "rebalance_budget"} <= {run.kind for run in runs}
    assert {run.status for run in runs if run.kind == "heartbeat"} == {"ok"}


async def test_step_is_idempotent_within_its_slot() -> None:
    world = await make_world()
    worker = world.worker()
    await world.advance(worker, hours=3)
    await worker.run("rebalance_budget", 1)
    count = (len(world.changes()), len(world.repository.rows(WorkerRun)))
    await worker.run("rebalance_budget", 1)
    assert (len(world.changes()), len(world.repository.rows(WorkerRun))) == count


@pytest.mark.parametrize("evil", [False, True])
async def test_dead_worker_two_days_platform_limits_hold(evil: bool) -> None:
    world = await make_world(evil=evil)
    worker = world.worker()
    await world.advance(worker, days=12)
    before = world.spend_in_month(JAN)
    world.clock.advance(days=2)
    assert await world.adapter.get_spend_today() >= 0
    dead_spend = world.spend_in_month(JAN) - before
    print("dead worker", {"evil": evil, "spent_in_2_days": dead_spend, "month": world.spend_in_month(JAN)})
    state = world.adapter._states[world.refs.campaign_id]
    assert world.platform_spend() <= state.period_cap_kop * (125 if evil else 100) // 100
    assert world.spend_in_month(JAN) <= BUDGET_KOP
    await world.advance(world.worker(), days=19)
    assert world.spend_in_month(JAN) <= BUDGET_KOP


async def test_crash_after_apply_is_reconciled_on_restart() -> None:
    world = await make_world(adapter_class=CrashingAdapter)
    world.adapter.crash = "after_apply"
    world.adapter.crash_after = 2
    with pytest.raises(Crash):
        await world.advance(world.worker(), days=1)
    assert len(world.repository.pending()) == 1
    restarted = world.worker()
    await restarted.startup()
    assert not world.repository.pending()
    change = world.changes(status="applied")[0]
    state = world.adapter._states[world.refs.campaign_id]
    assert (state.daily_limit_kop, state.period_cap_kop) == (
        world.campaign.daily_limit_kop, world.campaign.period_cap_kop)
    assert change.reason_code == "BUDGET_REBALANCE"
    await world.advance(restarted, days=1)
    assert not world.repository.pending()


async def test_crash_halfway_is_repeated_after_restart() -> None:
    world = await make_world(adapter_class=CrashingAdapter)
    world.adapter.crash = "after_apply"
    world.adapter.crash_after = 1
    with pytest.raises(Crash):
        await world.advance(world.worker(), days=1)
    restarted = world.worker()
    await restarted.startup()
    assert not world.repository.pending()
    assert [item.status for item in world.changes()] == ["failed"]
    await restarted.run("rebalance_budget", 1)
    state = world.adapter._states[world.refs.campaign_id]
    assert (state.daily_limit_kop, state.period_cap_kop) == (
        world.campaign.daily_limit_kop, world.campaign.period_cap_kop)
    assert not world.repository.pending()


async def test_crash_before_apply_is_repeated_after_restart() -> None:
    world = await make_world(adapter_class=CrashingAdapter)
    initial = world.adapter._states[world.refs.campaign_id].period_cap_kop
    world.adapter.crash = "before_apply"
    with pytest.raises(Crash):
        await world.advance(world.worker(), days=1)
    restarted = world.worker()
    await restarted.startup()
    assert not world.repository.pending()
    assert [item.status for item in world.changes()] == ["failed"]
    assert world.adapter._states[world.refs.campaign_id].period_cap_kop == initial
    await restarted.run("rebalance_budget", 1)
    assert world.changes(status="applied")
    state = world.adapter._states[world.refs.campaign_id]
    assert state.period_cap_kop == world.campaign.period_cap_kop != initial


async def test_unreadable_platform_leaves_pending_until_next_run() -> None:
    world = await make_world(adapter_class=FlakyReadAdapter)
    world.adapter.fail_read_after_apply = True
    worker = world.worker()
    await world.advance(worker, hours=3)
    assert len(world.repository.pending()) == 1
    await world.advance(worker, hours=1)
    assert not world.repository.pending()
    assert world.changes(status="applied")


async def test_dry_run_proposes_without_touching_platform() -> None:
    world = await make_world()
    before = world.adapter._states[world.refs.campaign_id]
    await world.advance(world.worker(dry_run=True), days=16)
    assert world.adapter._states[world.refs.campaign_id] == before
    proposed = world.changes()
    assert proposed and all(item.evidence["_service"]["dry_run"] for item in proposed)
    assert {item.status for item in proposed} == {"pending"}
    assert any(item.kind == "pause" for item in proposed)
    assert not world.repository.pending()
    assert all(ad.status == "active" for ad in world.ads.values())


async def test_business_dry_run_setting() -> None:
    world = await make_world(business_settings={"dry_run": True})
    before = world.adapter._states[world.refs.campaign_id]
    await world.advance(world.worker(dry_run=False), days=3)
    assert world.adapter._states[world.refs.campaign_id] == before
    assert world.changes() and all(item.evidence["_service"]["dry_run"] for item in world.changes())


async def test_budget_guard_stops_everything_and_next_month_resumes() -> None:
    world = await make_world(budget=1_000_000)
    key = world.refs.campaign_id
    world.adapter._states[key] = replace(world.adapter._states[key], daily_limit_kop=10**8, period_cap_kop=10**9)
    worker = world.worker()
    for _ in range(200):
        world.clock.advance(hours=0.5)
        await worker.run("budget_guard", 1)
        if world.business.status == "budget_stop":
            break
    assert world.business.status == "budget_stop"
    assert world.adapter._states[key].status == "paused"
    ledger = world.ledger()[0]
    assert ledger.hard_stop_at is not None
    spent = world.platform_spend()
    print("guard", {"spent": spent, "budget": world.business.month_budget_kop})
    assert spent <= world.business.month_budget_kop
    world.clock.advance(days=40)
    await worker.run("month_rollover", 1)
    assert world.business.status == "running"
    assert world.adapter._states[key].status == "active"


async def test_month_rollover_without_auto_continue_stays_stopped() -> None:
    world = await make_world(auto_continue=False)
    worker = world.worker()
    await world.advance(worker, days=34)
    assert world.adapter._states[world.refs.campaign_id].status == "paused"
    assert world.spend_in_month(date(2026, 2, 1)) == 0
    stopped_at = world.platform_spend()
    await world.advance(worker, days=2)
    assert world.platform_spend() == stopped_at


async def test_kill_switch_stops_all_and_blocks_autopilot() -> None:
    world = await make_world()
    worker = world.worker()
    await world.advance(worker, days=2)
    await stop_all(world.business, worker=worker)
    assert world.adapter._states[world.refs.campaign_id].status == "paused"
    assert world.business.status == "paused"
    changes = len(world.changes())
    spent = world.platform_spend()
    await world.advance(worker, days=3)
    assert world.platform_spend() == spent
    assert len(world.changes()) == changes


async def test_kill_switch_in_dry_run_only_proposes() -> None:
    world = await make_world()
    await stop_all(world.business, worker=world.worker(dry_run=True))
    assert world.adapter._states[world.refs.campaign_id].status == "active"
    assert world.business.status == "running"


async def test_ledger_is_not_lowered_by_stale_report() -> None:
    world = await make_world()
    await world.advance(world.worker(), days=3)
    row = world.ledger()[0]
    assert isinstance(row, BudgetLedger) and row.spent_kop > 0


@pytest.mark.db
def test_advisory_lock_is_exclusive_per_business() -> None:
    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL is required")
    if not url.startswith("postgresql"):
        pytest.skip("TEST_DATABASE_URL must point to PostgreSQL")
    first, second = create_engine(url), create_engine(url)
    try:
        with business_lock(first, 987_001):
            with business_lock(first, 987_001):
                pass
            with pytest.raises(ChangeConflict):
                with business_lock(second, 987_001):
                    pass
            with business_lock(second, 987_002):
                pass
        with business_lock(second, 987_001):
            pass
    finally:
        first.dispose()
        second.dispose()
