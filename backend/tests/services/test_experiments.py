from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest

from app.db.models import Ad, AdGroup, ChangeLog, Experiment, StatsDaily, WorkerRun
from app.domain.experiments import evaluate_experiment
from app.domain.types import AdSnapshot, DaySpend, ExperimentDesign, Verdict
from app.services.experiments import completed_hypotheses, create_experiment
from app.services.optimize import experiments_tick
from app.services.sync import sync_moderation
from tests.integration.harness import START, make_world


@pytest.fixture
async def experiment_world():
    world = await make_world()
    world.clock.advance(days=7)
    await sync_moderation(world.worker(), world.business)
    return world


def control_snapshot(world):
    ad = world.ads["borderline-1"]
    group = next(item for item in world.repository.rows(AdGroup) if item.id == ad.ad_group_id)
    return AdSnapshot(ad.external_id, group.external_id, "fake", START)


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


async def test_factory_declines_unsupported_launch_before_any_paid_or_platform_write(experiment_world, monkeypatch):
    world = experiment_world
    before = await world.adapter.get_state(world.refs)
    monkeypatch.setattr(world.adapter, "create_campaign_tree", AsyncMock(side_effect=AssertionError("new campaign")))
    monkeypatch.setattr(world.adapter, "apply", AsyncMock(side_effect=AssertionError("write")))
    worker = world.worker()
    for _ in range(2):
        assert await create_experiment(worker, world.business, control_snapshot(world), design()) is None
    assert await world.adapter.get_state(world.refs) == before
    assert not world.repository.rows(Experiment)
    notices = world.repository.rows(WorkerRun)
    assert len(notices) == 1
    assert notices[0].log[0]["reason_code"] == "EXPERIMENT_UNSUPPORTED"


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
    assert "≈ 10 дней" in first.reason_text
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


async def test_tick_leaves_running_test_untouched_when_group_frozen(experiment_world):
    world = experiment_world
    experiment = running_experiment(world)
    world.ads["borderline-1"].frozen_until = world.clock.now + timedelta(days=30)
    world.clock.advance(days=14)
    before = await world.adapter.get_state(world.refs)
    await experiments_tick(world.worker(), world.business)
    assert experiment.status == "running"
    assert not world.changes(kind="experiment_verdict")
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
