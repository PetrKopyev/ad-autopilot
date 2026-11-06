import contextlib
from unittest.mock import AsyncMock

import pytest

from app.db.models import AdGroup, ChangeLog, Experiment, WorkerRun
from app.domain.budget import stop_margin
from app.domain.types import Action, Decision
from app.platforms.base import Operation
from app.platforms.fake import FakeAdapter
from app.services.budget_guard import budget_guard, stop_all
from app.services.changes import ChangeConflict
from app.services.experiments import completed_hypotheses, create_experiment, group_available
from app.services.month import month_rollover
from app.services.optimize import experiments_tick, optimize, rebalance_budget
from app.services.rollback import RollbackService
from app.services.screens import ScreensService
from app.services.sync import sync_moderation
from tests.integration.harness import make_world
from tests.services.test_experiments import control_snapshot, design, running_experiment, wire_content
from tests.services.test_stop_before_revoke import later


async def test_experiment_starts_after_eight_days_of_scheduler_ticks(tmp_path, monkeypatch):
    world = await make_world()
    worker = world.worker(experiment_factory=create_experiment)
    await world.advance(worker, days=8)
    await sync_moderation(worker, world.business)
    wire_content(world, tmp_path, monkeypatch)
    launch = await create_experiment(worker, world.business, control_snapshot(world), design())
    assert launch is not None and launch.status == "applied"
    assert any(item.status == "running" for item in world.repository.rows(Experiment))


@pytest.mark.parametrize("status", ["running", "paused", "budget_stop"])
async def test_expired_experiment_closes_before_platform_reads_or_cooldown(monkeypatch, status):
    world = await make_world()
    experiment = running_experiment(world)
    # За день после срока (14-й день — ещё вывод по данным, см. К4).
    world.clock.advance(days=15)
    world.business.status = status
    world.ads["borderline-2"].status = "paused_owner"
    worker = world.worker()
    worker.remember(world.business, kill_switch=status == "paused")
    monkeypatch.setattr("app.services.optimize.snapshot", AsyncMock(return_value=([], [])))
    monkeypatch.setattr(world.adapter, "apply", AsyncMock(side_effect=AssertionError("статус владельца")))
    await experiments_tick(worker, world.business)
    assert experiment.status == "concluded" and experiment.verdict == "no_difference"
    assert world.ads["borderline-2"].status == "paused_owner"


@pytest.mark.parametrize("owner_pause", [False, True])
async def test_resume_protection_stop_after_month_rollover(owner_pause):
    world = await make_world()
    worker = world.worker()
    await world.advance(worker, days=3)
    await stop_all(
        world.business,
        worker=worker,
        budget_stop=True,
        reason_code="BUDGET_STOP",
        reason_params={"spent": world.platform_spend(), "budget": world.business.month_budget_kop, "margin": 0},
    )
    if owner_pause:
        await ScreensService(world.worker()).pause(world.business)
    else:
        world.business.auto_continue = False
    world.clock.advance(days=29)
    worker.step_key = "rollover"
    await month_rollover(worker, world.business)
    status = await ScreensService(world.worker()).resume(world.business)
    assert status["status"] == "running"
    before = world.platform_spend()
    await world.advance(worker, days=2)
    assert world.campaign.status == "active" and world.platform_spend() > before
    state = (await world.adapter.get_state(worker.refs(world.campaign)))[world.campaign.external_id]
    assert state.status == "active"


async def test_increasing_budget_allows_resume_after_budget_stop():
    world = await make_world()
    worker = world.worker()
    await world.advance(worker, days=3)
    spent = world.platform_spend()
    margin = stop_margin(world.campaign.daily_limit_kop)
    world.business.month_budget_kop = spent + margin
    await budget_guard(worker, world.business)
    assert world.business.status == "budget_stop"
    service = ScreensService(world.worker())
    with pytest.raises(ChangeConflict, match="защитного запаса"):
        await service.resume(world.business)
    await service.save_budget(world.business, 20_000_000)
    status = await ScreensService(world.worker()).resume(world.business)
    assert status["status"] == "running" and world.campaign.daily_limit_kop > 0
    assert world.campaign.status == "active"
    assert world.changes(reason_code="BUDGET_REBALANCE", status="applied")


async def test_resume_without_available_daily_budget_does_not_report_running(monkeypatch):
    world = await make_world()
    service = ScreensService(world.worker())
    await service.pause(world.business)
    world.campaign.daily_limit_kop = 0
    monkeypatch.setattr("app.services.screens.rebalance_budget", AsyncMock())
    status = await service.resume(world.business)
    assert status["status"] == "paused"
    assert service.worker.metadata(world.business)["kill_switch"]


@pytest.mark.parametrize("action", [Action.PAUSE, Action.SCALE])
async def test_optimize_does_not_pause_or_scale_running_test(monkeypatch, action):
    world = await make_world()
    experiment = running_experiment(world)
    group = next(row for row in world.repository.rows(AdGroup) if row.id == experiment.ad_group_id)
    target = world.ads["borderline-1"].external_id if action == Action.PAUSE else group.external_id
    monkeypatch.setattr(
        "app.services.optimize.decide",
        lambda *args, **kwargs: [Decision(target, action, "TEST", new_daily_limit=100_000)],
    )
    before = await world.adapter.get_state(world.refs)
    await optimize(world.worker(), world.business)
    assert await world.adapter.get_state(world.refs) == before
    assert not world.changes(reason_code="TEST")


@pytest.mark.parametrize(
    "actor,kind,field,blocked",
    [
        ("system", "budget", "daily_limit_kop", False),
        ("autopilot", "budget", "daily_limit_kop", False),
        ("system", "pause", "status", True),
        ("owner", "budget", "daily_limit_kop", True),
        ("external", "targeting", "targeting", True),
    ],
)
async def test_group_cooldown_distinguishes_budget_and_owner_changes(actor, kind, field, blocked):
    world = await make_world()
    world.clock.advance(days=8)
    group = next(row for row in world.repository.rows(AdGroup) if row.id == world.ads["borderline-1"].ad_group_id)
    world.repository.save(
        ChangeLog(
            business_id=world.business.id,
            actor=actor,
            kind=kind,
            status="applied",
            platform="direct",
            entity_type="campaign",
            entity_id=world.campaign.external_id,
            created_at=world.clock.now,
            operations=[{"entity_id": world.campaign.external_id, "field": field}],
        )
    )
    assert group_available(world.worker(), world.business, group) is not blocked


async def test_pending_owner_stop_does_not_skip_budget_guard(monkeypatch):
    world = await make_world()
    worker = world.worker()
    worker.remember(world.business, pending_stop={"actor": "owner"})
    monkeypatch.setattr("app.services.budget_guard.read_spend", AsyncMock(return_value=({"direct": 10_000_000}, {})))
    await budget_guard(worker, world.business)
    # Страж отработал и после остановки владельца, но пауза владельца остаётся паузой, а не «бюджет израсходован».
    assert world.business.status == "paused"
    assert world.ledger()[0].hard_stop_at == world.clock.now
    assert not worker.metadata(world.business)["pending_stop"]


async def test_owner_stop_bypasses_unrelated_pending_change():
    world = await make_world()
    world.repository.save(
        ChangeLog(
            business_id=world.business.id,
            platform="direct",
            status="pending",
            operations=[],
            evidence={"_service": {"dry_run": False}},
        )
    )
    await ScreensService(world.worker()).pause(world.business)
    assert world.campaign.status == "paused_owner" and world.business.status == "paused"


async def test_guard_uses_local_daily_limits_without_state_requests(monkeypatch):
    world = await make_world()
    monkeypatch.setattr(world.adapter, "get_state", AsyncMock(side_effect=AssertionError("лишний запрос")))
    await budget_guard(world.worker(), world.business)
    assert world.business.status == "running"


async def test_rebalance_does_not_log_or_send_unchanged_limits(monkeypatch):
    world = await make_world()
    worker = world.worker()
    await rebalance_budget(worker, world.business)
    count = len(world.changes())
    apply = AsyncMock(wraps=world.adapter.apply)
    monkeypatch.setattr(world.adapter, "apply", apply)
    worker.step_key = "next-rebalance"
    await rebalance_budget(worker, world.business)
    assert len(world.changes()) == count
    apply.assert_not_awaited()


async def test_reverted_hypothesis_stays_completed_after_freeze_expires():
    world = await make_world()
    world.clock.advance(days=7)
    worker = world.worker()
    experiment = running_experiment(world, status="concluded")
    change = await worker.apply(
        world.business,
        world.campaign,
        [Operation("set_status", "ad", world.ads["borderline-1"].external_id, "status", "active", "paused_auto")],
        kind="experiment_verdict",
        reason_code="EXPERIMENT_VERDICT",
        reason_text="Выбрали вариант Б.",
        evidence={"experiment_id": experiment.id},
    )
    experiment.change_id = change.id
    await RollbackService(world.repository, dry_run=False).revert(
        change.id,
        business_id=world.business.id,
        adapter=world.adapter,
        now=world.clock.now,
    )
    world.clock.advance(days=15)
    assert "text" in completed_hypotheses(worker, world.business, experiment.ad_group_id)
    proposal = await create_experiment(world.worker(dry_run=True), world.business, control_snapshot(world), design())
    assert proposal.reason_params["hypothesis"] == "image"
    # Откат вывода не трогает второе плечо: раньше оно записывалось «остановленным владельцем», теперь
    # остаётся в своём статусе (это объявление ещё на модерации).
    assert world.ads["borderline-2"].status == "moderation"


async def test_fake_restart_skips_jobs_and_notifies_once():
    world = await make_world()
    worker = world.worker()
    empty = FakeAdapter(clock=world.clock)
    worker.resolve_adapter = lambda *_: empty
    await worker.startup()
    await world.advance(worker, hours=1)
    notices = [row for row in world.repository.rows(WorkerRun) if row.business_id == world.business.id]
    assert len(notices) == 1
    assert notices[0].log[0]["reason_code"] == "DEMO_STATE_LOST"


async def test_refs_reads_change_history_once_per_tick(monkeypatch):
    world = await make_world()
    worker = world.worker()
    rows = world.repository.rows
    calls = []

    def observed(model, business_id=None, **filters):
        if model is ChangeLog:
            calls.append(business_id)
        return rows(model, business_id, **filters)

    monkeypatch.setattr(world.repository, "rows", observed)
    first = worker.refs(world.campaign)
    assert worker.refs(world.campaign) == first
    assert calls == [world.business.id]
    world.clock.advance(hours=0.5)
    assert worker.refs(world.campaign) == first
    assert calls == [world.business.id, world.business.id]


async def test_rejected_ad_is_rewritten_once_even_if_platform_still_reports_rejection(monkeypatch):
    from app.platforms.base import ModerationInfo

    world = await make_world()
    world.clock.advance(days=2)
    ad = next(iter(world.ads.values()))

    async def rejected(refs):
        return [ModerationInfo(ad.external_id, "rejected", "Опечатка в тексте", world.clock.now)]

    monkeypatch.setattr(world.adapter, "get_moderation", rejected)
    rewrite = AsyncMock(return_value=[Operation("update_ad_text", "ad", ad.external_id, "text", ad.text, "Исправлено")])
    for _ in range(2):
        world.clock.advance(hours=1)
        await sync_moderation(world.worker(rewrite_rejected=rewrite), world.business)
    rewrite.assert_awaited_once()
    assert world.business.settings["_worker"]["rewritten"] == [f"direct:{ad.external_id}"]
    assert len(world.changes(reason_code="REJECTED_REWRITE")) == 1


async def test_rewritten_ad_returns_campaign_to_review(monkeypatch):
    """Мелочь 6 круга 14: отметку проверки кампании, все объявления которой отклонены, шаг модерации снимает;
    исправленное объявление снова на проверке — отметка возвращается, и после одобрения его включит settle_review."""
    from dataclasses import replace

    from app.platforms.base import ModerationInfo

    world = await make_world()
    world.clock.advance(days=2)
    ad = next(iter(world.ads.values()))
    world.campaign.review_since = None

    async def rejected(refs):
        return [ModerationInfo(ad.external_id, "rejected", "Опечатка в тексте", world.clock.now)]

    monkeypatch.setattr(world.adapter, "get_moderation", rejected)
    world.adapter._states[ad.external_id] = replace(world.adapter._states[ad.external_id], status="moderation")
    rewrite = AsyncMock(return_value=[Operation("update_ad_text", "ad", ad.external_id, "text", ad.text, "Исправлено")])
    await sync_moderation(world.worker(rewrite_rejected=rewrite), world.business)
    assert ad.status == "moderation" and world.campaign.review_since == world.clock.now


async def test_rejected_ad_waits_for_unconfirmed_change_before_rewrite(monkeypatch):
    """Пока площадка не подтвердила другое изменение, правка текста упала бы в конфликт: отклонённое объявление
    переписывается на опросе после подтверждения, а не теряет отметку навсегда."""
    from app.platforms.base import ModerationInfo

    world = await make_world()
    world.clock.advance(days=2)
    ad = next(iter(world.ads.values()))

    async def rejected(refs):
        return [ModerationInfo(ad.external_id, "rejected", "Опечатка в тексте", world.clock.now)]

    monkeypatch.setattr(world.adapter, "get_moderation", rejected)
    launch = ChangeLog(
        business_id=world.business.id,
        actor="owner",
        kind="launch",
        platform="direct",
        entity_type="campaign",
        entity_id=world.campaign.external_id,
        operations=[],
        reason_code="LAUNCH",
        reason_text="Запустили рекламу.",
        evidence={"_service": {"dry_run": False}},
        status="pending",
        created_at=world.clock.now,
    )
    world.repository.save(launch)
    rewrite = AsyncMock(return_value=[Operation("update_ad_text", "ad", ad.external_id, "text", ad.text, "Исправлено")])
    await sync_moderation(world.worker(rewrite_rejected=rewrite), world.business)
    rewrite.assert_not_awaited()
    assert world.business.settings["_worker"].get("rewritten", []) == []
    launch.status = "applied"
    world.repository.save(launch)
    world.clock.advance(hours=1)
    await sync_moderation(world.worker(rewrite_rejected=rewrite), world.business)
    rewrite.assert_awaited_once()
    assert len(world.changes(reason_code="REJECTED_REWRITE")) == 1


async def test_rejected_ad_is_not_rewritten_in_dry_run(monkeypatch):
    from app.platforms.base import ModerationInfo

    world = await make_world()
    world.clock.advance(days=2)
    ad = next(iter(world.ads.values()))

    async def rejected(refs):
        return [ModerationInfo(ad.external_id, "rejected", "Опечатка в тексте", world.clock.now)]

    monkeypatch.setattr(world.adapter, "get_moderation", rejected)
    rewrite = AsyncMock(return_value=[Operation("update_ad_text", "ad", ad.external_id, "text", ad.text, "Исправлено")])
    await sync_moderation(world.worker(dry_run=True, rewrite_rejected=rewrite), world.business)
    rewrite.assert_not_awaited()
    assert world.business.settings["_worker"].get("rewritten", []) == []
    assert not world.changes(reason_code="REJECTED_REWRITE")
    assert [
        run.log[0]["reason_code"] for run in world.repository.rows(WorkerRun, world.business.id, kind="notification")
    ] == ["MODERATION_REJECTED"]


async def test_paid_rewrite_is_not_repeated_when_platform_did_not_confirm_the_text(monkeypatch):
    from app.platforms.base import ModerationInfo

    world = await make_world()
    world.clock.advance(days=2)
    ad = next(iter(world.ads.values()))

    async def rejected(refs):
        return [ModerationInfo(ad.external_id, "rejected", "Опечатка в тексте", world.clock.now)]

    monkeypatch.setattr(world.adapter, "get_moderation", rejected)
    rewrite = AsyncMock(return_value=[Operation("update_ad_text", "ad", ad.external_id, "text", ad.text, "Исправлено")])
    refused = AsyncMock(side_effect=ChangeConflict("Площадка не подтвердила изменение."))
    for _ in range(2):
        world.clock.advance(hours=1)
        worker = world.worker(rewrite_rejected=rewrite)
        worker.apply = refused
        with contextlib.suppress(ChangeConflict):
            await sync_moderation(worker, world.business)
    rewrite.assert_awaited_once()
    refused.assert_awaited_once()
    assert world.business.settings["_worker"]["rewritten"] == [f"direct:{ad.external_id}"]


async def test_rewrite_is_saved_at_once_and_not_repeated_after_step_failed_later(monkeypatch):
    from app.platforms.base import ModerationInfo

    world = await make_world()
    world.clock.advance(days=2)
    first, second = list(world.ads.values())[:2]

    async def rejected(refs):
        return [
            ModerationInfo(ad.external_id, "rejected", "Опечатка в тексте", world.clock.now) for ad in (first, second)
        ]

    monkeypatch.setattr(world.adapter, "get_moderation", rejected)
    saved = []
    save = world.repository.save
    monkeypatch.setattr(
        world.repository,
        "save",
        lambda *items: saved.extend(
            list(world.business.settings["_worker"].get("rewritten", ())) for item in items if item is world.business
        )
        or save(*items),
    )
    fixed = [Operation("update_ad_text", "ad", first.external_id, "text", first.text, "Исправлено")]
    rewrite = AsyncMock(side_effect=[fixed, RuntimeError("ИИ не ответил")])
    world.clock.advance(hours=1)
    with pytest.raises(RuntimeError):
        await sync_moderation(world.worker(rewrite_rejected=rewrite), world.business)
    assert saved[-1] == [f"direct:{first.external_id}"]
    rewrite = AsyncMock(return_value=[Operation("update_ad_text", "ad", second.external_id, "text", second.text, "Ок")])
    world.clock.advance(hours=1)
    await sync_moderation(world.worker(rewrite_rejected=rewrite), world.business)
    assert [call.args[3] for call in rewrite.await_args_list] == [second]
    assert len(world.changes(reason_code="REJECTED_REWRITE")) == 2


@pytest.mark.parametrize("vk_first", [True, False])
async def test_direct_approvals_are_saved_when_vk_needs_reauth(monkeypatch, vk_first):
    from app.db.models import Campaign
    from app.platforms.base import ModerationInfo, PlatformError
    from app.worker.jobs import Worker

    world = await make_world()
    world.clock.advance(days=2)
    vk = Campaign(
        id=99,
        business_id=1,
        platform_account_id=2,
        platform="vk",
        external_id="vk-c",
        type="vk_plan",
        name="VK",
        status="active",
        daily_limit_kop=0,
        period_cap_kop=0,
    )
    campaigns = world.repository.tables[Campaign]
    campaigns.insert(0 if vk_first else len(campaigns), vk)

    # Директ, как настоящий, отдаёт временем одобрения время опроса.
    async def approved(refs):
        return [ModerationInfo(ad.external_id, "approved", None, world.clock.now) for ad in world.ads.values()]

    def resolve(business_id: int, platform: str):
        if platform == "vk":
            raise PlatformError("Кабинет площадки не подключён или требует входа заново.")
        return world.adapter

    monkeypatch.setattr(world.adapter, "get_moderation", approved)
    first_poll = world.clock.now
    for _ in range(2):
        with pytest.raises(PlatformError):
            await Worker(world.repository, resolve, clock=world.clock, dry_run=False).run("sync_moderation", 1)
        world.clock.advance(hours=1)
    assert world.business.settings["_worker"]["approvals"] == {
        f"direct:{ad.external_id}": first_poll.isoformat() for ad in world.ads.values()
    }
    assert {ad.status for ad in world.ads.values()} == {"active"}
    assert [run.status for run in world.repository.rows(WorkerRun, 1, kind="sync_moderation")] == ["failed"] * 2


async def test_approvals_are_saved_with_statuses_when_campaign_poll_fails_midway(monkeypatch):
    from app.platforms.base import ModerationInfo

    world = await make_world()
    world.clock.advance(days=2)
    first, second = list(world.ads.values())[:2]

    async def moderation(refs):
        return [
            ModerationInfo(first.external_id, "approved", None, world.clock.now),
            ModerationInfo(second.external_id, "rejected", "Опечатка в тексте", world.clock.now),
        ]

    monkeypatch.setattr(world.adapter, "get_moderation", moderation)
    rewrite = AsyncMock(side_effect=RuntimeError("ИИ не ответил"))
    with pytest.raises(RuntimeError):
        await sync_moderation(world.worker(rewrite_rejected=rewrite), world.business)
    assert first.status == "active"
    assert world.business.settings["_worker"]["approvals"] == {
        f"direct:{first.external_id}": world.clock.now.isoformat()
    }


async def test_stop_platform_before_revoke_pauses_campaign_and_writes_history():
    from app.services.budget_guard import stop_platform

    world = await make_world()
    worker = world.worker()
    worker.step_key = "owner:revoke"
    assert (
        await stop_platform(
            world.business, worker=worker, platform="direct", reason_text="Остановили.", deadline=later()
        )
        is True
    )
    (change,) = world.changes(reason_code="ACCOUNT_REVOKE")
    assert (change.actor, change.kind, change.status) == ("owner", "pause", "applied")
    assert change.reason_text == "Остановили."
    assert world.campaign.status == "paused_owner"
    assert (
        await stop_platform(world.business, worker=worker, platform="direct", reason_text="Повтор.", deadline=later())
        is True
    )
    assert len(world.changes(reason_code="ACCOUNT_REVOKE")) == 1
    assert (
        await stop_platform(world.business, worker=worker, platform="vk", reason_text="Другая.", deadline=later())
        is True
    )


async def test_stop_platform_reports_unconfirmed_stop(monkeypatch):
    from app.platforms.base import PlatformError
    from app.services.budget_guard import stop_platform

    world = await make_world()
    monkeypatch.setattr(world.adapter, "apply", AsyncMock(side_effect=PlatformError("Площадка недоступна.")))
    worker = world.worker()
    worker.step_key = "owner:revoke"
    assert (
        await stop_platform(
            world.business, worker=worker, platform="direct", reason_text="Остановили.", deadline=later()
        )
        is False
    )
    assert world.campaign.status != "paused_owner"
