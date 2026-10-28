import json
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest

from app.content.ad_text import generate_ad_text
from app.content.art import ART_COST_KOP
from app.db.models import Ad, AdGroup, ArtCall, ChangeLog, Experiment, LLMCall, StatsDaily, WorkerRun
from app.domain.experiments import evaluate_experiment
from app.domain.types import AdSnapshot, DaySpend, ExperimentDesign, Verdict
from app.services.experiments import completed_hypotheses, create_experiment
from app.services.onboarding import AiLimits, Blocked
from app.services.optimize import experiments_tick
from app.services.sync import sync_moderation
from tests.integration.harness import START, Crash, CrashingAdapter, make_world
from tests.services.test_onboarding import make_world as make_content_world


@pytest.fixture
async def experiment_world():
    world = await make_world()
    world.clock.advance(days=7)
    await sync_moderation(world.worker(), world.business)
    return world


def control_snapshot(world):
    ad = world.ads["borderline-1"]
    group = next(item for item in world.repository.rows(AdGroup) if item.id == ad.ad_group_id)
    return AdSnapshot(ad.external_id, group.external_id, world.campaign.platform, START)


def design():
    return ExperimentDesign(10_000, 10, 10_000, "text", True, "EXPERIMENT_START")


def running_experiment(world, *, group="borderline", hypothesis="text", status="running"):
    control, variant = (world.ads[f"{group}-{index}"] for index in (1, 2))
    item = Experiment(
        business_id=world.business.id,
        ad_group_id=control.ad_group_id,
        control_ad_id=control.id,
        variant_ad_id=variant.id,
        hypothesis=hypothesis,
        metric="ctr",
        planned_n_per_arm=10_000,
        min_effect=Decimal("0.3"),
        started_at=world.clock.now,
        deadline=world.clock.now + timedelta(days=14),
        status=status,
        stats={},
    )
    world.repository.save(item)
    return item


def wire_content(world, tmp_path, monkeypatch):
    content = make_content_world(tmp_path)
    content.repository.tables = world.repository.tables
    world.business.description = {"fields": {"services": {"value": "Уход за волосами"}}}
    content.llm.ad = json.dumps({"title": "Уход за волосами", "text": "Выберите уход за волосами"})
    content.llm.cost_kop = 10

    @asynccontextmanager
    async def service(worker):
        yield content.plans

    monkeypatch.setattr("app.services.experiments.content_service", service)
    return content


async def test_factory_notifies_once_when_generation_refused_without_platform_write(experiment_world, monkeypatch):
    world = experiment_world
    before = await world.adapter.get_state(world.refs)
    prepare = AsyncMock(side_effect=Blocked("Генерация отклонена"))
    monkeypatch.setattr("app.services.experiments.prepare_variant", prepare)
    monkeypatch.setattr(world.adapter, "create_campaign_tree", AsyncMock(side_effect=AssertionError("new campaign")))
    monkeypatch.setattr(world.adapter, "apply", AsyncMock(side_effect=AssertionError("write")))
    worker = world.worker()
    for _ in range(2):
        assert await create_experiment(worker, world.business, control_snapshot(world), design()) is None
    assert await world.adapter.get_state(world.refs) == before
    assert not world.repository.rows(Experiment)
    prepare.assert_awaited_once()
    notices = world.repository.rows(WorkerRun)
    assert len(notices) == 1
    assert notices[0].log[0]["reason_code"] == "EXPERIMENT_GENERATION_FAILED"


@pytest.mark.parametrize("platform,blocked", [("vk", False), ("direct", True)])
async def test_pending_change_blocks_test_only_when_its_cabinet_can_be_reconciled(
    experiment_world, monkeypatch, platform, blocked
):
    from app.platforms.base import PlatformError
    from app.services.changes import ChangeConflict

    world = experiment_world
    world.repository.save(
        ChangeLog(
            business_id=world.business.id, actor="autopilot", kind="pause", platform=platform, entity_type="ad",
            entity_id="x", operations=[], reason_code="CUSTOM", reason_params={}, reason_text="Т.", evidence={},
            status="pending", created_at=world.clock.now,
        )
    )
    prepare = AsyncMock(side_effect=Blocked("Генерация отклонена"))
    monkeypatch.setattr("app.services.experiments.prepare_variant", prepare)

    def resolve(business_id, name):
        # Кабинет VK требует входа заново: его изменение сверить нечем.
        if name == "vk":
            raise PlatformError("Нужен вход заново.")
        return world.adapter

    worker = world.worker()
    worker.resolve_adapter = resolve
    if blocked:
        with pytest.raises(ChangeConflict):
            await create_experiment(worker, world.business, control_snapshot(world), design())
        prepare.assert_not_awaited()
    else:
        assert await create_experiment(worker, world.business, control_snapshot(world), design()) is None
        prepare.assert_awaited_once()


@pytest.mark.parametrize("global_dry,business_dry", [(True, False), (False, True), (True, True)])
async def test_dry_run_proposal_is_idempotent_and_does_not_touch_platform(
    experiment_world, monkeypatch, global_dry, business_dry
):
    world = experiment_world
    world.business.settings = {"dry_run": business_dry}
    for name in ("apply", "upload_image", "create_campaign_tree", "get_state"):
        monkeypatch.setattr(world.adapter, name, AsyncMock(side_effect=AssertionError("platform call")))
    worker = world.worker(dry_run=global_dry)
    first = await create_experiment(worker, world.business, control_snapshot(world), design())
    worker = world.worker(dry_run=global_dry)
    worker.step_key = "after-restart"
    second = await create_experiment(worker, world.business, control_snapshot(world), design())
    assert first is second
    assert first.status == "proposed"
    assert first.reason_text.endswith("Это займёт примерно 10 дней.") and ".." not in first.reason_text
    assert first.evidence["metric"] == "ctr"
    assert len([change for change in world.repository.rows(ChangeLog) if change.status == "proposed"]) == 1
    assert not world.repository.rows(Experiment)
    assert len(world.repository.rows(Ad)) == 6


@pytest.mark.parametrize("groups", [("borderline",), ("strong", "weak")])
async def test_factory_enforces_one_test_per_group_and_two_per_business(experiment_world, groups):
    world = experiment_world
    for group in groups:
        running_experiment(world, group=group)
    result = await create_experiment(world.worker(dry_run=True), world.business, control_snapshot(world), design())
    assert result is None
    assert not [change for change in world.repository.rows(ChangeLog) if change.kind != "launch"]


@pytest.mark.parametrize(
    "changes",
    [
        {"accepted": False},
        {"estimated_days": 15},
        {"estimated_days": 0},
        {"n_per_arm": 0},
        {"daily_budget": 25_001},
        {"daily_budget": 0},
    ],
)
async def test_factory_rejects_unfeasible_design_and_budget_over_quarter(experiment_world, changes):
    world = experiment_world
    result = await create_experiment(
        world.worker(dry_run=True), world.business, control_snapshot(world), replace(design(), **changes)
    )
    assert result is None
    assert not [change for change in world.repository.rows(ChangeLog) if change.kind != "launch"]


@pytest.mark.parametrize("protection", ["frozen", "sibling_frozen", "cooldown", "group_cooldown", "owner"])
async def test_factory_respects_freeze_and_cooldowns(experiment_world, protection):
    world = experiment_world
    ad = control_snapshot(world)
    if protection == "frozen":
        world.ads["borderline-1"].frozen_until = world.clock.now + timedelta(days=1)
    elif protection == "sibling_frozen":
        world.ads["borderline-2"].frozen_until = world.clock.now + timedelta(days=1)
    elif protection == "cooldown":
        ad = replace(ad, last_changed_at=world.clock.now - timedelta(hours=71))
    elif protection == "owner":
        ad = replace(ad, owner_edited_at=world.clock.now - timedelta(hours=23))
    else:
        world.repository.save(
            ChangeLog(
                business_id=world.business.id,
                status="applied",
                actor="autopilot",
                operations=[{"entity_id": ad.group_id}],
                created_at=world.clock.now - timedelta(hours=71),
            )
        )
    assert await create_experiment(world.worker(dry_run=True), world.business, ad, design()) is None
    assert not world.repository.rows(Experiment)


@pytest.mark.parametrize(
    "completed,budget,expected",
    [
        ((), 70_000, "text"),
        (("text",), 70_000, "image"),
        (("text", "image"), 70_000, "audience"),
        (("text", "image"), 69_999, None),
        (("text", "image", "audience"), 70_000, None),
    ],
)
async def test_proposals_follow_hypothesis_order_and_audience_budget(experiment_world, completed, budget, expected):
    world = experiment_world
    for hypothesis in completed:
        running_experiment(world, hypothesis=hypothesis, status="concluded")
    worker = world.worker(dry_run=True)
    ad = control_snapshot(world)
    worker.remember(world.business, group_limits={ad.group_id: budget})
    proposal = await create_experiment(worker, world.business, ad, design())
    assert (proposal.reason_params["hypothesis"] if proposal else None) == expected
    assert set(completed_hypotheses(worker, world.business, world.ads["borderline-1"].ad_group_id)) == set(completed)


def test_guardrail_rejects_clickbait_with_real_significance_test():
    day = START.date()
    control = (DaySpend(day, 10_000, 10_000, 100, 30),)
    variant = (DaySpend(day, 10_000, 10_000, 300, 3),)
    result = evaluate_experiment(design(), control, variant, day, day + timedelta(days=3))
    assert result.verdict == Verdict.CLICKBAIT
    assert result.p_value < 0.05
    assert result.guardrail_p_value < 0.1


@pytest.mark.parametrize(
    "variant_clicks,variant_leads,verdict",
    [
        (300, 90, "variant_wins"),
        (100, 30, "no_difference"),
        (300, 3, "no_difference"),
    ],
)
async def test_tick_applies_existing_test_and_saves_exact_evidence(
    experiment_world, variant_clicks, variant_leads, verdict
):
    world = experiment_world
    experiment = running_experiment(world)
    for ad_id, clicks, leads in (
        (experiment.control_ad_id, 100, 30),
        (experiment.variant_ad_id, variant_clicks, variant_leads),
    ):
        for offset, impressions in ((-1, 1_000_000), (1, 10_000), (3, 1_000_000)):
            world.repository.save(
                StatsDaily(
                    business_id=world.business.id,
                    platform="direct",
                    campaign_id=world.campaign.id,
                    ad_id=ad_id,
                    date=(world.clock.now + timedelta(days=offset)).date(),
                    cost_kop=10_000,
                    impressions=impressions,
                    clicks=clicks,
                    conversions={"1": leads},
                    is_settled=True,
                )
            )
    world.clock.advance(days=4)
    factory = AsyncMock()
    await experiments_tick(world.worker(experiment_factory=factory), world.business)
    assert experiment.verdict == verdict and experiment.status == "concluded"
    changes = world.changes(kind="experiment_verdict")
    assert len(changes) == 1
    assert changes[0].evidence["control"]["impressions"] == 10_000
    assert changes[0].evidence["variant"]["impressions"] == 10_000
    states = await world.adapter.get_state(world.refs)
    loser = "borderline-1" if verdict == "variant_wins" else "borderline-2"
    assert states[world.ads[loser].external_id].status == "paused_auto"
    factory.assert_not_called()


async def test_tick_closes_expired_test_without_changing_frozen_group(experiment_world):
    world = experiment_world
    experiment = running_experiment(world)
    world.ads["borderline-1"].frozen_until = world.clock.now + timedelta(days=30)
    world.clock.advance(days=14)
    before = await world.adapter.get_state(world.refs)
    await experiments_tick(world.worker(), world.business)
    assert experiment.status == "concluded" and experiment.verdict == "no_difference"
    assert world.changes(kind="experiment_verdict")[0].operations == []
    assert await world.adapter.get_state(world.refs) == before


async def test_tick_dry_run_does_not_conclude_or_pause_existing_test(experiment_world):
    world = experiment_world
    experiment = running_experiment(world)
    world.clock.advance(days=14)
    before = await world.adapter.get_state(world.refs)
    await experiments_tick(world.worker(dry_run=True), world.business)
    assert experiment.status == "running" and experiment.change_id is None
    assert world.changes(kind="experiment_verdict")[0].status == "proposed"
    assert await world.adapter.get_state(world.refs) == before


async def test_text_variant_is_added_once_in_same_group_and_uses_another_angle(experiment_world, tmp_path, monkeypatch):
    world = experiment_world
    content = wire_content(world, tmp_path, monkeypatch)
    spy = AsyncMock(wraps=generate_ad_text)
    monkeypatch.setattr("app.services.experiments.generate_ad_text", spy)
    before = await world.adapter.get_state(world.refs)
    first = await create_experiment(world.worker(), world.business, control_snapshot(world), design())
    second = await create_experiment(world.worker(), world.business, control_snapshot(world), design())
    assert first is second and first.status == "applied"
    (experiment,) = world.repository.rows(Experiment)
    variant = next(item for item in world.repository.rows(Ad) if item.id == experiment.variant_ad_id)
    assert variant.id not in {item.id for item in world.ads.values()}
    assert variant.ad_group_id == world.ads["borderline-1"].ad_group_id
    assert spy.await_args.kwargs["angle"] == "результат"
    assert content.llm.calls == ["ad"]
    assert len(world.repository.rows(LLMCall)) == 1
    assert content.repository.ai_spent(world.business.id) == 10
    assert len(world.repository.rows(Ad)) == 7
    refs = world.worker().refs(world.campaign)
    states = await world.adapter.get_state(refs)
    assert states[variant.external_id].creative == first.operations[0]["after"]
    assert states[world.campaign.external_id] == before[world.campaign.external_id]
    assert not any(op["kind"] == "set_budget" for op in first.operations)


@pytest.mark.parametrize("failure", ["fallback", "validation", "same"])
async def test_rejected_text_never_starts_test_or_pays_again(experiment_world, tmp_path, monkeypatch, failure):
    world = experiment_world
    content = wire_content(world, tmp_path, monkeypatch)
    if failure == "fallback":
        content.llm.ad = "invalid json"
    elif failure == "validation":
        monkeypatch.setattr("app.services.experiments.validate_ad_text", lambda *args, **kwargs: ["unsafe"])
    else:
        source = world.ads["borderline-1"]
        content.llm.ad = json.dumps({"title": source.title, "text": source.text})
    for _ in range(2):
        assert await create_experiment(world.worker(), world.business, control_snapshot(world), design()) is None
    assert not world.repository.rows(Experiment)
    assert len(world.repository.rows(Ad)) == 6
    assert len(content.llm.calls) == (3 if failure == "fallback" else 1)
    assert len(world.repository.rows(WorkerRun)) == 1


@pytest.mark.parametrize("weekly_limit,expected", [(2500, True), (0, False), (ART_COST_KOP - 1, False)])
async def test_image_variant_respects_shared_ai_limit_and_is_not_regenerated(
    experiment_world, tmp_path, monkeypatch, weekly_limit, expected
):
    world = experiment_world
    content = wire_content(world, tmp_path, monkeypatch)
    content.plans.limits = AiLimits(6000, weekly_limit)
    running_experiment(world, hypothesis="text", status="concluded")
    first = await create_experiment(world.worker(), world.business, control_snapshot(world), design())
    spent = content.repository.ai_spent(world.business.id)
    second = await create_experiment(world.worker(), world.business, control_snapshot(world), design())
    assert bool(first) is expected
    assert second is first
    assert content.repository.ai_spent(world.business.id) == spent <= weekly_limit
    if expected:
        assert len(world.repository.rows(ArtCall)) == 1
        (operation,) = first.operations
        assert operation["after"]["image_ref"] != "fake-image"
        assert operation["after"]["text"] == world.ads["borderline-1"].text
    else:
        assert not [item for item in world.repository.rows(Experiment) if item.status == "running"]


async def test_image_upload_retry_does_not_pay_for_second_generation(experiment_world, tmp_path, monkeypatch):
    world = experiment_world
    content = wire_content(world, tmp_path, monkeypatch)
    running_experiment(world, hypothesis="text", status="concluded")
    upload = world.adapter.upload_image
    monkeypatch.setattr(world.adapter, "upload_image", AsyncMock(side_effect=RuntimeError("upload failed")))
    with pytest.raises(RuntimeError, match="upload failed"):
        await create_experiment(world.worker(), world.business, control_snapshot(world), design())
    spent = content.repository.ai_spent(world.business.id)
    monkeypatch.setattr(world.adapter, "upload_image", upload)
    result = await create_experiment(world.worker(), world.business, control_snapshot(world), design())
    assert result.status == "applied"
    assert content.repository.ai_spent(world.business.id) == spent
    assert len(world.repository.rows(ArtCall)) == 1


async def test_restart_after_platform_add_recovers_by_ref_without_second_variant(tmp_path, monkeypatch):
    world = await make_world(adapter_class=CrashingAdapter)
    world.clock.advance(days=7)
    await sync_moderation(world.worker(), world.business)
    content = wire_content(world, tmp_path, monkeypatch)
    world.adapter.crash = "after_apply"
    with pytest.raises(Crash):
        await create_experiment(world.worker(), world.business, control_snapshot(world), design())
    worker = world.worker()
    await worker.changes.recover_pending(worker.resolve_adapter, business_id=world.business.id, now=world.clock.now)
    result = await create_experiment(worker, world.business, control_snapshot(world), design())
    assert result.status == "applied"
    assert len(world.repository.rows(Experiment)) == 1
    assert len(world.repository.rows(Ad)) == 7
    assert len(world.adapter._ads) == 7
    assert content.llm.calls == ["ad"]


async def test_restart_during_paid_generation_does_not_pay_again(experiment_world, tmp_path, monkeypatch):
    world = experiment_world
    content = wire_content(world, tmp_path, monkeypatch)
    complete = content.llm.complete_json

    async def crash(*args, **kwargs):
        await complete(*args, **kwargs)
        raise Crash

    monkeypatch.setattr(content.llm, "complete_json", crash)
    with pytest.raises(Crash):
        await create_experiment(world.worker(), world.business, control_snapshot(world), design())
    assert await create_experiment(world.worker(), world.business, control_snapshot(world), design()) is None
    assert content.llm.calls == ["ad"]
    assert not world.repository.rows(Experiment)


async def test_exhausted_weekly_ai_limit_postpones_hypothesis_to_next_week(experiment_world, tmp_path, monkeypatch):
    world = experiment_world
    content = wire_content(world, tmp_path, monkeypatch)
    content.plans.limits = AiLimits(6000, 0)
    first = await create_experiment(world.worker(), world.business, control_snapshot(world), design())
    assert first is None and content.llm.calls == []
    (proposal,) = world.repository.rows(ChangeLog, reason_code="EXPERIMENT_START")
    monday = (world.clock.now + timedelta(days=7 - world.clock.now.weekday())).date()
    assert (proposal.evidence["preparation"], proposal.evidence["retry_on"]) == ("ai_limit", monday.isoformat())
    content.plans.limits = AiLimits(6000, 2500)
    world.clock.advance(days=(monday - world.clock.now.date()).days - 1)
    assert await create_experiment(world.worker(), world.business, control_snapshot(world), design()) is None
    assert content.llm.calls == []
    world.clock.advance(days=1)
    started = await create_experiment(world.worker(), world.business, control_snapshot(world), design())
    assert started is proposal and started.status == "applied" and content.llm.calls == ["ad"]
    assert [item.status for item in world.repository.rows(Experiment)] == ["running"]
    notices = [row.log[0]["reason_code"] for row in world.repository.rows(WorkerRun)]
    assert notices == ["EXPERIMENT_AI_LIMIT"]
