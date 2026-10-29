"""Один и тот же сценарий на MemoryWorkerRepository и на WorkerRepository с PostgreSQL: память ведёт себя как SQL."""

import hashlib
from collections import defaultdict
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.db.models import (
    Ad,
    AdGroup,
    Business,
    Campaign,
    ChangeLog,
    Experiment,
    LeadsSurvey,
    OAuthState,
    PlatformAccount,
    Session,
    StatsDaily,
    User,
    WorkerRun,
)
from app.db.query import Window, matches
from app.domain.settings import DEFAULT_SETTINGS
from app.platforms.base import ModerationInfo, Operation, StatRow
from app.platforms.fake import VirtualClock
from app.services.experiments import group_available
from app.services.screens import ScreensService
from app.services.sync import approved_at, sync_moderation, sync_stats
from app.worker.jobs import (
    HEARTBEAT_KEEP,
    RUN_STALE,
    RUNS_KEEP,
    MemoryWorkerRepository,
    Worker,
    WorkerRepository,
    clients_survey,
)

NOW = datetime(2026, 3, 11, 9, tzinfo=UTC)
TODAY = date(2026, 3, 11)


@pytest.fixture(params=["memory", pytest.param("sql", marks=pytest.mark.db)])
def repository(request) -> WorkerRepository:
    if request.param == "memory":
        return MemoryWorkerRepository()
    return WorkerRepository(request.getfixturevalue("db_session"))


def tree(repository: WorkerRepository, platforms: tuple[str, ...] = ("direct",)) -> SimpleNamespace:
    """Бизнес и по кампании на площадку; внешние id на площадках совпадают."""
    user = User(yandex_uid="parity-" + uuid4().hex, login="test", consent_at=NOW)
    repository.save(user)
    business = Business(
        user_id=user.id,
        site_url="https://example.test",
        city="Самара",
        month_budget_kop=100_000,
        status="running",
        settings={},
        lead_goal_ids=[1],
    )
    repository.save(business)
    result = SimpleNamespace(business=business, campaigns={}, groups={}, ads={})
    for platform in platforms:
        account = PlatformAccount(business_id=business.id, platform=platform, auth_mode="oauth", access_token_enc="t")
        repository.save(account)
        campaign = Campaign(
            business_id=business.id,
            platform_account_id=account.id,
            platform=platform,
            external_id="c-1",
            type="unified",
            name="Тест",
            status="active",
            daily_limit_kop=1_000,
            period_cap_kop=10_000,
            owner_paused=False,
        )
        repository.save(campaign)
        group = AdGroup(campaign_id=campaign.id, external_id="g-1", theme="Тест", geo={}, status="active")
        repository.save(group)
        ads = [
            Ad(ad_group_id=group.id, external_id=f"a-{index}", title=f"Т{index}", text="Т", status="active")
            for index in (1, 2)
        ]
        repository.save(*ads)
        result.campaigns[platform], result.groups[platform], result.ads[platform] = campaign, group, ads
    return result


def change(business: Business, **values) -> ChangeLog:
    defaults = {
        "business_id": business.id,
        "actor": "autopilot",
        "kind": "pause",
        "platform": "direct",
        "entity_type": "ad",
        "entity_id": "a-1",
        "operations": [],
        "reason_code": "CUSTOM",
        "reason_params": {},
        "reason_text": "Тест.",
        "evidence": {},
        "status": "applied",
        "created_at": NOW,
    }
    return ChangeLog(**defaults | values)


def run(business_id: int | None, **values) -> WorkerRun:
    defaults = {"business_id": business_id, "kind": "optimize", "status": "ok", "started_at": NOW, "units_spent": 0}
    return WorkerRun(**defaults | {"log": []} | values)


def stats(world, platform: str, day: date, **values) -> StatsDaily:
    ad = world.ads[platform][values.pop("ad", 0)]
    defaults = {
        "business_id": world.business.id,
        "platform": platform,
        "campaign_id": world.campaigns[platform].id,
        "ad_id": ad.id,
        "date": day,
        "impressions": 0,
        "clicks": 0,
        "cost_kop": 0,
        "conversions": {},
        "fetched_at": NOW,
        "is_settled": True,
    }
    return StatsDaily(**defaults | values)


def worker(repository: WorkerRepository, adapters: dict | None = None, now: datetime = NOW) -> Worker:
    return Worker(repository, lambda business_id, platform: adapters[platform], clock=VirtualClock(now), dry_run=False)


def test_null_never_matches_window_or_set_like_sql() -> None:
    item = SimpleNamespace(value=None)
    assert not matches(item, {"value": Window(None, None)})
    assert not matches(item, {"value": {None, 1}})
    assert matches(item, {"value": None})


def test_last_status_change_skips_operations_without_field(repository) -> None:
    world = tree(repository)
    status = change(world.business, operations=[{"entity_type": "ad", "entity_id": "a-1", "field": "status"}])
    budget = change(world.business, operations=[{"kind": "add_ad", "entity_type": "ad", "entity_id": "a-1"}])
    repository.save(status)
    repository.save(budget)
    assert repository.last_status_change(world.business.id, "direct", "ad", "a-1") is status


def test_latest_run_breaks_ties_by_id_and_last_check_ignores_other_businesses(repository) -> None:
    world, other = tree(repository), tree(repository)
    first, second = run(world.business.id), run(world.business.id)
    repository.save(first)
    repository.save(second)
    assert repository.latest_run(world.business.id, "optimize") is second
    repository.save(run(other.business.id, kind="budget_guard", finished_at=NOW + timedelta(minutes=5)))
    assert repository.last_check(world.business.id) is None
    repository.save(run(None, kind="heartbeat", finished_at=NOW + timedelta(minutes=1)))
    repository.save(
        run(world.business.id, kind="budget_guard", status="failed", finished_at=NOW + timedelta(minutes=2))
    )
    assert repository.last_check(world.business.id) == NOW + timedelta(minutes=1)
    repository.save(run(world.business.id, kind="budget_guard", finished_at=NOW + timedelta(minutes=3), slot_key="g"))
    assert repository.last_check(world.business.id) == NOW + timedelta(minutes=3)
    repository.save(run(None, kind="heartbeat", finished_at=NOW + timedelta(minutes=4)))
    assert repository.last_check(world.business.id) == NOW + timedelta(minutes=4)


def test_notifications_with_null_key_parts_are_never_merged(repository) -> None:
    world = tree(repository)
    for _ in range(2):
        repository.notify(run(None, kind="notification", slot_key="x"))
        repository.notify(run(world.business.id, kind="notification", slot_key=None))
        repository.notify(run(world.business.id, kind="notification", slot_key="same"))
    keys = sorted(
        (row.business_id is None, row.slot_key or "") for row in repository.rows(WorkerRun, kind="notification")
    )
    assert keys[-2:] == [(True, "x"), (True, "x")]
    assert len(repository.rows(WorkerRun, world.business.id, kind="notification", slot_key="same")) == 1
    assert len([row for row in repository.rows(WorkerRun, world.business.id) if row.slot_key is None]) == 2


def test_pending_includes_changes_without_service_metadata(repository) -> None:
    world = tree(repository)
    plain = change(world.business, status="pending", evidence={})
    other = change(world.business, status="pending", evidence={"_service": {"refs": {}}})
    dry = change(world.business, status="pending", evidence={"_service": {"dry_run": True}})
    repository.save(plain, other, dry)
    assert {item.id for item in repository.pending(world.business.id)} == {plain.id, other.id}


def test_start_run_keeps_fresh_running_slot_and_history_of_attempts(repository) -> None:
    world = tree(repository)
    business_id = world.business.id
    first = repository.start_run(business_id, "optimize", "optimize:2026-03-11", NOW)
    assert first is not None
    # Второй воркер не перехватывает идущий прогон до RUN_STALE.
    assert repository.start_run(business_id, "optimize", "optimize:2026-03-11", NOW + RUN_STALE / 2) is None
    stale = repository.start_run(business_id, "optimize", "optimize:2026-03-11", NOW + RUN_STALE)
    assert stale is not None and stale.id == first.id
    stale.status, stale.log = "failed", stale.log + [{"reason_code": "JOB_FAILED"}]
    repository.save(stale)
    retry = repository.start_run(business_id, "optimize", "optimize:2026-03-11", NOW + RUN_STALE)
    assert retry.status == "running"
    assert retry.log == [
        {"key": "optimize:2026-03-11"},
        {"key": "optimize:2026-03-11"},
        {"reason_code": "JOB_FAILED"},
        {"key": "optimize:2026-03-11"},
    ]
    repository.interrupt_runs(business_id, NOW + RUN_STALE)
    restarted = repository.start_run(business_id, "optimize", "optimize:2026-03-11", NOW + RUN_STALE)
    assert restarted is not None and restarted.log[-2:] == [
        {"reason_code": "JOB_INTERRUPTED"},
        {"key": "optimize:2026-03-11"},
    ]
    restarted.status = "ok"
    repository.save(restarted)
    assert repository.start_run(business_id, "optimize", "optimize:2026-03-11", NOW + RUNS_KEEP) is None


async def test_rewind_asks_database_once_per_done_or_busy_slot(repository, monkeypatch) -> None:
    from app.worker import jobs

    world = tree(repository)
    rewinding = worker(repository)
    calls, starts = [], []

    async def step(worker: Worker, business: Business) -> None:
        calls.append(worker.step_key)
        if len(calls) == 1:
            raise RuntimeError("Площадка не ответила")

    async def no_recovery(*args, **kwargs):
        raise AssertionError("Несверенных изменений нет: восстанавливать нечего")

    busy = jobs.schedule_key("sync_stats", NOW)
    repository.save(run(world.business.id, kind="sync_stats", status="running", slot_key=busy))
    start_run = repository.start_run
    monkeypatch.setattr(repository, "start_run", lambda *args: starts.append(args[2]) or start_run(*args))
    monkeypatch.setattr(jobs, "handlers", lambda: {"optimize": step, "sync_stats": step})
    monkeypatch.setattr(rewinding, "demo_available", lambda business: pytest.fail("Проверено перед перемоткой"))
    monkeypatch.setattr(rewinding.changes, "recover_pending", no_recovery)
    with rewinding.rewinding():
        for _ in range(3):
            for kind in ("optimize", "sync_stats"):
                try:
                    await rewinding.run(kind, world.business.id)
                except RuntimeError:
                    pass
    optimize = jobs.schedule_key("optimize", NOW)
    # Упавший шаг повторяется, успешный и занятый другим прогоном больше не спрашиваются.
    assert calls == [optimize, optimize]
    assert starts == [optimize, busy, optimize]
    monkeypatch.undo()
    checks = []
    monkeypatch.setattr(rewinding, "demo_available", lambda business: checks.append(business) or True)
    await rewinding.run("optimize", world.business.id)
    assert len(checks) == 1


async def test_pending_change_of_cabinet_that_fails_to_open_does_not_stop_step(repository, monkeypatch) -> None:
    from app.worker import jobs

    world = tree(repository, ("direct", "vk"))
    repository.save(change(world.business, platform="vk", status="pending"))
    calls = []

    async def step(worker: Worker, business: Business) -> None:
        calls.append(business.id)

    async def no_recovery(*args, **kwargs):
        return []

    def resolve(business_id: int, platform: str):
        # Кабинет удалили: загрузчик падает не ошибкой площадки.
        raise KeyError(platform)

    stepper = Worker(repository, resolve, clock=VirtualClock(NOW), dry_run=False)
    monkeypatch.setattr(jobs, "handlers", lambda: {"optimize": step})
    monkeypatch.setattr(stepper, "demo_available", lambda business: True)
    monkeypatch.setattr(stepper.changes, "recover_pending", no_recovery)
    assert stepper.pending(world.business.id) == []
    await stepper.run("optimize", world.business.id)
    assert calls == [world.business.id]


def test_prune_keeps_notifications_and_runs_on_the_boundary(repository) -> None:
    world = tree(repository)
    business_id = world.business.id
    rows = {
        "heartbeat_edge": run(None, kind="heartbeat", started_at=NOW - HEARTBEAT_KEEP),
        "heartbeat_old": run(None, kind="heartbeat", started_at=NOW - HEARTBEAT_KEEP - timedelta(seconds=1)),
        "heartbeat_now": run(None, kind="heartbeat", started_at=NOW),
        # Пульс по часам демо от старой перемотки.
        "heartbeat_future": run(None, kind="heartbeat", started_at=NOW + timedelta(seconds=1)),
        "run_edge": run(business_id, started_at=NOW - RUNS_KEEP, slot_key="a"),
        "run_old": run(business_id, started_at=NOW - RUNS_KEEP - timedelta(seconds=1), slot_key="b"),
        "notice_old": run(business_id, kind="notification", started_at=NOW - 2 * RUNS_KEEP, slot_key="experiment:1"),
        "notice_null": run(None, kind="notification", started_at=NOW - 2 * RUNS_KEEP),
    }
    repository.save(*rows.values())
    repository.prune_runs(NOW)
    left = {row.id for row in repository.rows(WorkerRun)}
    assert HEARTBEAT_KEEP == timedelta(days=1)
    assert {name for name, row in rows.items() if row.id in left} == {
        "heartbeat_edge",
        "heartbeat_now",
        "run_edge",
        "notice_old",
        "notice_null",
    }


@pytest.mark.parametrize("age,available", [(timedelta(0), True), (timedelta(seconds=1), False)])
def test_group_cooldown_ends_exactly_at_its_boundary(repository, age, available) -> None:
    world = tree(repository)
    group = world.groups["direct"]
    cooldown = timedelta(hours=DEFAULT_SETTINGS.cooldown_hours)
    repository.save(change(world.business, entity_type="group", entity_id="g-1", created_at=NOW - cooldown + age))
    assert group_available(worker(repository), world.business, group) is available


async def test_clients_survey_counts_only_the_previous_week(repository) -> None:
    world = tree(repository)
    week_start = date(2026, 3, 2)
    for day, leads in [(week_start - timedelta(days=1), 100), (week_start, 1), (week_start + timedelta(days=6), 2)]:
        repository.save(stats(world, "direct", day, conversions={"1": leads}))
    repository.save(stats(world, "direct", week_start + timedelta(days=7), conversions={"1": 1000}))
    await clients_survey(worker(repository), world.business)
    (survey,) = repository.rows(LeadsSurvey, world.business.id)
    assert (survey.week_start, survey.leads_count) == (week_start, 3)


def test_refs_take_budget_context_only_from_applied_changes(repository) -> None:
    world = tree(repository)
    campaign = world.campaigns["direct"]
    stored = {"refs": {"campaign_id": "c-1", "budget_context": {"strategy": "applied"}}}
    repository.save(change(world.business, evidence={"_service": stored}))
    operation = {"entity_type": "campaign", "entity_id": "c-1", "budget_context": {"strategy": "operation"}}
    repository.save(change(world.business, operations=[operation]))
    for status in ("failed", "pending", "reverted"):
        late = {"refs": {"campaign_id": "c-1", "budget_context": {"strategy": status}}}
        repository.save(change(world.business, status=status, evidence={"_service": late}))
    repository.save(change(world.business, evidence={"_service": {"refs": {"budget_context": None}}}))
    assert len(repository.applied_budget_contexts(world.business.id)) == 2
    assert worker(repository).refs(campaign).budget_context["strategy"] == "operation"


async def test_step_rereads_budget_context_saved_by_another_process_in_same_slot(repository, monkeypatch) -> None:
    from app.worker import jobs

    world = tree(repository)
    campaign = world.campaigns["direct"]
    stepper = worker(repository)
    assert "strategy" not in stepper.refs(campaign).budget_context
    # Тот же получасовой слот: стратегию поменял запрос владельца в процессе API.
    operation = {"entity_type": "campaign", "entity_id": "c-1", "budget_context": {"strategy": "owner"}}
    repository.save(change(world.business, operations=[operation]))
    seen = []

    async def step(worker: Worker, business: Business) -> None:
        seen.append(worker.refs(campaign).budget_context.get("strategy"))

    monkeypatch.setattr(jobs, "handlers", lambda: defaultdict(lambda: step))
    monkeypatch.setattr(stepper, "demo_available", lambda business: True)
    await stepper.run_business_due(world.business.id)
    assert seen and set(seen) == {"owner"}


def test_sync_local_keeps_status_that_platform_reports_without_local_equivalent(repository) -> None:
    world = tree(repository)
    campaign, group = world.campaigns["direct"], world.groups["direct"]
    reverted = change(world.business, kind="revert", entity_type="campaign", entity_id="c-1", status="pending")
    repository.save(reverted)
    repository.sync_local(
        reverted,
        [
            Operation("set_status", "campaign", "c-1", "status", "active", "pending"),
            Operation("set_status", "group", "g-1", "status", "active", "mixed"),
            Operation("set_status", "ad", "a-1", "status", "active", "paused"),
            Operation("set_status", "ad", "a-2", "status", "paused_auto", "pending"),
        ],
    )
    reverted.status = "applied"
    repository.save(reverted)
    first, second = world.ads["direct"]
    assert (campaign.status, group.status, first.status, second.status) == (
        "active",
        "active",
        "paused_auto",
        "active",
    )


def test_experiments_screen_counts_only_settled_days_of_arms(repository) -> None:
    world = tree(repository)
    service = ScreensService(worker(repository))
    assert service.experiments(world.business) == {"items": []}
    control, variant = world.ads["direct"]
    repository.save(
        Experiment(
            business_id=world.business.id,
            ad_group_id=world.groups["direct"].id,
            control_ad_id=control.id,
            variant_ad_id=variant.id,
            hypothesis="text",
            metric="ctr",
            planned_n_per_arm=100,
            min_effect=Decimal("0.2"),
            started_at=NOW - timedelta(days=7),
            deadline=NOW + timedelta(days=7),
            status="running",
            stats={},
        )
    )
    repository.save(
        stats(world, "direct", TODAY - timedelta(days=3), impressions=30),
        stats(world, "direct", TODAY - timedelta(days=4), impressions=500, is_settled=False),
        stats(world, "direct", TODAY - timedelta(days=3), ad=1, impressions=40),
    )
    (item,) = service.experiments(world.business)["items"]
    assert item["remaining_impressions"] == 70 + 60


def test_budget_screen_counts_only_current_month_up_to_today(repository) -> None:
    world = tree(repository)
    repository.save(
        stats(world, "direct", TODAY.replace(day=1), cost_kop=100),
        stats(world, "direct", TODAY, ad=1, cost_kop=20),
        stats(world, "direct", TODAY.replace(day=1) - timedelta(days=1), cost_kop=5_000),
        stats(world, "direct", TODAY + timedelta(days=1), cost_kop=7_000),
    )
    budget = ScreensService(worker(repository)).budget(world.business)
    assert (budget["spent_kop"], budget["remaining_kop"]) == (120, 100_000 - 120)


class StubPlatform:
    def __init__(self, cost: int, moderation: str) -> None:
        self.cost = cost
        self.moderation = moderation

    async def get_stats(self, date_from: date, date_to: date, level: str = "ad") -> list[StatRow]:
        return [StatRow("a-1", "c-1", TODAY - timedelta(days=3), "ad", 10, 1, self.cost, {"1": 1}, NOW, True)]

    async def get_moderation(self, refs) -> list[ModerationInfo]:
        return [ModerationInfo("a-1", self.moderation, None, NOW)]


async def test_sync_keeps_same_external_ids_of_two_platforms_apart_and_is_idempotent(repository) -> None:
    world = tree(repository, ("direct", "vk"))
    adapters = {"direct": StubPlatform(100, "approved"), "vk": StubPlatform(200, "rejected")}
    for _ in range(2):
        await sync_stats(worker(repository, adapters), world.business)
    rows = repository.rows(StatsDaily, world.business.id)
    assert sorted((row.platform, row.ad_id, row.cost_kop) for row in rows) == [
        ("direct", world.ads["direct"][0].id, 100),
        ("vk", world.ads["vk"][0].id, 200),
    ]
    await sync_moderation(worker(repository, adapters), world.business)
    assert (world.ads["direct"][0].moderation_status, world.ads["vk"][0].moderation_status) == ("approved", "rejected")
    assert (world.ads["direct"][0].status, world.ads["vk"][0].status) == ("active", "rejected")


async def test_moderation_approvals_are_kept_per_platform(repository) -> None:
    world = tree(repository, ("direct", "vk"))
    adapters = {"direct": StubPlatform(100, "approved"), "vk": StubPlatform(200, "rejected")}
    await sync_moderation(worker(repository, adapters), world.business)
    meta = world.business.settings["_worker"]
    assert meta["approvals"] == {"direct:a-1": NOW.isoformat()}
    assert approved_at(meta, "direct", "a-1") == NOW.isoformat() and approved_at(meta, "vk", "a-1") is None


async def test_moderation_reads_old_bare_keys_and_moves_them_under_platform(repository) -> None:
    world = tree(repository, ("direct",))
    old = (NOW - timedelta(days=3)).isoformat()
    world.business.settings = {"_worker": {"approvals": {"a-1": old}, "rewritten": ["a-1"]}}
    repository.save(world.business)
    assert approved_at(world.business.settings["_worker"], "direct", "a-1") == old
    await sync_moderation(worker(repository, {"direct": StubPlatform(100, "approved")}), world.business)
    assert world.business.settings["_worker"]["approvals"]["direct:a-1"] == old


class ApprovingPlatform(StubPlatform):
    async def get_moderation(self, refs) -> list[ModerationInfo]:
        return [ModerationInfo(external_id, "approved", None, NOW) for external_id in refs.ads.values()]


async def test_moderation_step_saves_each_campaign_with_one_commit_and_reads_entities_once(
    repository, monkeypatch
) -> None:
    world = tree(repository, ("direct", "vk"))
    for ad in world.ads["direct"] + world.ads["vk"]:
        ad.status = "moderation"
    repository.save(*world.ads["direct"], *world.ads["vk"])
    saves, reads = [], []
    save, local_entities = repository.save, repository.local_entities
    monkeypatch.setattr(repository, "save", lambda *items: saves.append(items) or save(*items))
    monkeypatch.setattr(
        repository, "local_entities", lambda *args: reads.append(args[1]) or local_entities(*args)
    )
    adapters = {"direct": ApprovingPlatform(0, "approved"), "vk": ApprovingPlatform(0, "approved")}
    await sync_moderation(worker(repository, adapters), world.business)
    assert [set(items) for items in saves] == [
        {world.business, *world.ads["direct"]},
        {world.business, *world.ads["vk"]},
    ]
    assert sorted(reads) == ["direct", "vk"]
    assert {ad.status for ad in world.ads["direct"] + world.ads["vk"]} == {"active"}
    assert sorted(world.business.settings["_worker"]["approvals"]) == ["direct:a-1", "direct:a-2", "vk:a-1", "vk:a-2"]


def test_prune_removes_expired_sessions_and_oauth_states(repository) -> None:
    world = tree(repository)
    user_id = world.business.user_id
    sessions = {
        "expired": Session(id=uuid4().hex, user_id=user_id, expires_at=NOW, csrf_secret="x"),
        "live": Session(id=uuid4().hex, user_id=user_id, expires_at=NOW + timedelta(seconds=1), csrf_secret="x"),
    }
    states = {
        "expired": OAuthState(state="old-" + uuid4().hex, purpose="login", expires_at=NOW - timedelta(minutes=1)),
        "used_code": OAuthState(state="used-code:" + uuid4().hex, purpose="login", expires_at=NOW),
        "live": OAuthState(state="new-" + uuid4().hex, purpose="login", expires_at=NOW + timedelta(minutes=5)),
    }
    repository.save(*sessions.values(), *states.values())
    worker(repository).prune()
    left_sessions = {row.id for row in repository.rows(Session)}
    left_states = {row.state for row in repository.rows(OAuthState)}
    assert {name for name, row in sessions.items() if row.id in left_sessions} == {"live"}
    assert {name for name, row in states.items() if row.state in left_states} == {"live"}


def test_change_of_other_platform_with_same_external_ids_does_not_block_group(repository) -> None:
    world = tree(repository, ("direct", "vk"))
    repository.save(change(world.business, platform="vk", entity_type="ad", entity_id="a-1"))
    assert group_available(worker(repository), world.business, world.groups["direct"]) is True
    assert group_available(worker(repository), world.business, world.groups["vk"]) is False


async def test_old_bare_approval_is_ignored_when_id_exists_on_two_platforms(repository) -> None:
    world = tree(repository, ("direct", "vk"))
    old = (NOW - timedelta(days=3)).isoformat()
    world.business.settings = {"_worker": {"approvals": {"a-1": old}}}
    repository.save(world.business)
    adapters = {"direct": StubPlatform(100, "approved"), "vk": StubPlatform(200, "approved")}
    await sync_moderation(worker(repository, adapters), world.business)
    approvals = world.business.settings["_worker"]["approvals"]
    assert approvals["direct:a-1"] == approvals["vk:a-1"] == NOW.isoformat()


class RejectingPlatform(StubPlatform):
    def __init__(self, reason: str) -> None:
        super().__init__(0, "rejected")
        self.reason = reason
        self.clock = VirtualClock(NOW)

    async def get_moderation(self, refs) -> list[ModerationInfo]:
        # Директ и VK отдают время ответа равным моменту опроса.
        return [ModerationInfo("a-1", "rejected", self.reason, self.clock.now)]


async def test_rejection_notice_is_one_per_ad_and_reason_not_per_poll(repository) -> None:
    world = tree(repository, ("direct",))
    platform = RejectingPlatform("Нужна лицензия")
    for hours in range(3):
        platform.clock = VirtualClock(NOW + timedelta(hours=hours))
        await sync_moderation(worker(repository, {"direct": platform}, now=platform.clock.now), world.business)
    platform.reason = "Неверный адрес"
    await sync_moderation(worker(repository, {"direct": platform}), world.business)
    notices = repository.rows(WorkerRun, world.business.id, kind="notification")
    assert sorted(row.log[0]["text"] for row in notices) == [
        "Объявление «Т1»: Неверный адрес.",
        "Объявление «Т1»: Нужна лицензия.",
    ]


async def test_rejection_notices_of_same_ad_id_on_two_platforms_are_separate(repository) -> None:
    world = tree(repository, ("direct", "vk"))
    platform = RejectingPlatform("Нужна лицензия")
    await sync_moderation(worker(repository, {"direct": platform, "vk": platform}), world.business)
    assert len(repository.rows(WorkerRun, world.business.id, kind="notification")) == 2
    unread = repository.unread_notifications(world.business.id, {"MODERATION_REJECTED"}, 10, NOW)
    assert sorted(row.log[0]["target_id"] for row in unread) == ["direct:a-1", "vk:a-1"]
    # Ключ: причина и объявление, без повтора объявления в ключе.
    reason = hashlib.sha256("Нужна лицензия".encode()).hexdigest()[:16]
    assert sorted(row.slot_key for row in unread) == [f"moderation:{reason}:direct:a-1", f"moderation:{reason}:vk:a-1"]


def test_unread_notifications_and_mark_read(repository) -> None:
    world = tree(repository)
    business_id = world.business.id
    rows = {
        "old": run(
            business_id,
            kind="notification",
            slot_key="n1",
            started_at=NOW,
            log=[{"reason_code": "ANOMALY", "text": "А"}],
        ),
        "new": run(
            business_id,
            kind="notification",
            slot_key="n2",
            started_at=NOW + timedelta(hours=1),
            log=[{"reason_code": "ANOMALY", "text": "Б"}],
        ),
        "other_code": run(
            business_id, kind="notification", slot_key="n3", log=[{"reason_code": "EXPERIMENT_TOO_LONG"}]
        ),
        "proposed": run(
            business_id, kind="notification", slot_key="n4", status="proposed", log=[{"reason_code": "ANOMALY"}]
        ),
        "step": run(business_id, slot_key="n5", log=[{"reason_code": "ANOMALY"}]),
    }
    repository.save(*rows.values())
    names = {row.id: name for name, row in rows.items()}
    assert [names[row.id] for row in repository.unread_notifications(business_id, {"ANOMALY"}, 10, NOW)] == [
        "new",
        "old",
    ]
    assert [names[row.id] for row in repository.unread_notifications(business_id, {"ANOMALY"}, 1, NOW)] == ["new"]
    repository.mark_read(business_id, [rows["new"].id, rows["step"].id], NOW)
    assert [names[row.id] for row in repository.unread_notifications(business_id, {"ANOMALY"}, 10, NOW)] == ["old"]
    assert repository.find(WorkerRun, id=rows["step"].id).log == [{"reason_code": "ANOMALY"}]


def test_unread_notifications_show_one_latest_per_theme(repository) -> None:
    # Ежедневное «Можно получить больше обращений» с новыми суммами — одна тема (target_id): показываем только
    # последнее, и сотня повторов не вытесняет другие уведомления из выборки.
    business_id = tree(repository).business.id

    def notice(slot: str, hours: int, code: str = "BUDGET_OPPORTUNITY", **entry):
        return run(
            business_id,
            kind="notification",
            slot_key=slot,
            started_at=NOW + timedelta(hours=hours),
            log=[{"reason_code": code, **entry}],
        )

    rows = [notice(f"o{day}", day * 24, text=f"Ещё {day} ₽", target_id="business") for day in range(1, 31)]
    rows += [
        notice("ad-1", 1, "NOT_SHOWING", text="Нет показов", target_id=11),
        notice("ad-2", 2, "NOT_SHOWING", text="Нет показов", target_id=12),
        notice("mod-1", 3, "MODERATION_REJECTED", text="Объявление «А»: лицензия."),
        notice("mod-2", 4, "MODERATION_REJECTED", text="Объявление «А»: лицензия."),
    ]
    repository.save(*rows)
    codes = {"BUDGET_OPPORTUNITY", "NOT_SHOWING", "MODERATION_REJECTED"}
    latest = repository.unread_notifications(business_id, codes, 3, NOW)
    assert [row.slot_key for row in latest] == ["o30", "mod-2", "ad-2"]
    everything = repository.unread_notifications(business_id, codes, 10, NOW)
    assert [row.slot_key for row in everything] == ["o30", "mod-2", "ad-2", "ad-1"]
    assert len(repository.same_theme(latest[0])) == 30
    repository.mark_read(business_id, repository.same_theme(latest[0]), NOW)
    assert [row.slot_key for row in repository.unread_notifications(business_id, {"BUDGET_OPPORTUNITY"}, 10, NOW)] == []


def test_notices_are_facts_in_trial_mode(repository) -> None:
    # Остановка стражем бюджета выполняется и в пробном режиме: уведомление о ней — факт, а не предложение.
    world = tree(repository)
    world.business.settings = {"dry_run": True}
    repository.save(world.business)
    trial = Worker(repository, lambda *_: None, clock=VirtualClock(NOW), dry_run=True)
    trial.notice(world.business, "MODERATION_REJECTED", "Объявление «Т1»: Нужна лицензия.", key="fact")
    trial.notice(world.business, "BUDGET_STOP", "Реклама остановлена.", key="action")
    statuses = {row.slot_key: row.status for row in repository.rows(WorkerRun, world.business.id, kind="notification")}
    assert statuses == {"fact": "ok", "action": "ok"}


def test_notice_about_a_date_is_hidden_once_the_date_has_come(repository) -> None:
    # «Новый месяц через три дня» после перемотки демо или долгого отсутствия уже неправда.
    business_id = tree(repository).business.id
    expires_at = (NOW + timedelta(days=3)).isoformat()
    repository.save(
        run(
            business_id,
            kind="notification",
            slot_key="month",
            log=[
                {
                    "reason_code": "MONTH_CONTINUE_NOTICE",
                    "text": "1 апреля начнётся новый месяц",
                    "expires_at": expires_at,
                }
            ],
        ),
        run(
            business_id,
            kind="notification",
            slot_key="other",
            log=[{"reason_code": "ANOMALY", "text": "Нет обращений"}],
        ),
    )
    codes = {"MONTH_CONTINUE_NOTICE", "ANOMALY"}
    before = repository.unread_notifications(business_id, codes, 10, NOW + timedelta(days=3) - timedelta(seconds=1))
    assert sorted(row.slot_key for row in before) == ["month", "other"]
    after = repository.unread_notifications(business_id, codes, 10, NOW + timedelta(days=3))
    assert [row.slot_key for row in after] == ["other"]


def test_notices_without_target_are_grouped_by_text_not_merged(repository) -> None:
    business_id = tree(repository).business.id
    repository.save(
        *(
            run(
                business_id,
                kind="notification",
                slot_key=key,
                log=[{"reason_code": "ANOMALY", "text": text, "target_id": None}],
            )
            for key, text in (("a", "Сайт не открывается"), ("b", "Счётчик молчит"), ("c", "Счётчик молчит"))
        )
    )
    unread = repository.unread_notifications(business_id, {"ANOMALY"}, 10, NOW)
    assert sorted(row.log[0]["text"] for row in unread) == ["Сайт не открывается", "Счётчик молчит"]
