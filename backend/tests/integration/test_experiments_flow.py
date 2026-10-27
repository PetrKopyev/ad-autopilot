from datetime import timedelta
from decimal import Decimal

from app.db.models import Ad, Experiment, StatsDaily
from app.platforms.fake import AdTruth
from app.services.experiments import create_experiment
from app.services.optimize import experiments_tick
from app.services.rollback import RollbackService
from app.services.sync import sync_moderation
from tests.integration.harness import make_world
from tests.services.test_experiments import control_snapshot, design, wire_content


async def test_existing_experiment_variant_wins_explains_and_rolls_back_with_group_freeze():
    """Существующие до добавления add_ad тесты по-прежнему поддерживаются."""
    world = await make_world()
    control, variant = world.ads["borderline-1"], world.ads["borderline-2"]
    experiment = Experiment(
        business_id=world.business.id,
        ad_group_id=control.ad_group_id,
        control_ad_id=control.id,
        variant_ad_id=variant.id,
        hypothesis="text",
        metric="ctr",
        planned_n_per_arm=10_000,
        min_effect=Decimal("0.3"),
        started_at=world.clock.now,
        deadline=world.clock.now + timedelta(days=14),
        status="running",
        stats={},
    )
    world.repository.save(experiment)
    for ad, clicks, leads in ((control, 100, 30), (variant, 300, 90)):
        world.repository.save(
            StatsDaily(
                business_id=world.business.id,
                platform="direct",
                campaign_id=world.campaign.id,
                ad_id=ad.id,
                date=world.clock.now.date(),
                impressions=10_000,
                clicks=clicks,
                cost_kop=10_000,
                conversions={"1": leads},
                is_settled=True,
            )
        )
    world.clock.advance(days=4)
    before = await world.adapter.get_state(world.refs)
    worker = world.worker()
    await experiments_tick(worker, world.business)
    change = world.changes(kind="experiment_verdict")[0]
    assert change.status == "applied" and change.id == experiment.change_id
    assert experiment.verdict == "variant_wins" and experiment.status == "concluded"
    assert "Оставили Б, А остановили" in change.reason_text
    assert "10 000" in change.reason_text and "300" in change.reason_text
    assert "3,0%" in change.reason_text and "1,0%" in change.reason_text
    after = await world.adapter.get_state(world.refs)
    assert after[control.external_id].status == "paused_auto"
    assert after[variant.external_id].status == "active"
    # Кампания с общим бюджетом не получает дополнительных денег при выборе победителя.
    assert after[world.campaign.external_id] == before[world.campaign.external_id]
    assert world.platform_spend() <= world.business.month_budget_kop
    reverted = await RollbackService(world.repository, dry_run=False).revert(
        change.id, business_id=world.business.id, adapter=world.adapter, now=world.clock.now
    )
    assert reverted.status == "applied" and change.status == "reverted"
    restored = await world.adapter.get_state(world.refs)
    assert restored[control.external_id].status == "active"
    assert restored[variant.external_id].status == "paused_owner"
    assert restored[world.campaign.external_id] == before[world.campaign.external_id]
    assert experiment.status == "cancelled"
    assert control.frozen_until == variant.frozen_until == world.clock.now + timedelta(days=14)
    await experiments_tick(worker, world.business)
    assert len(world.changes(kind="experiment_verdict")) == 1


async def test_live_variant_is_added_wins_from_fake_stats_and_rolls_back(tmp_path, monkeypatch):
    world = await make_world()
    world.clock.advance(days=7)
    worker = world.worker(experiment_factory=create_experiment)
    await sync_moderation(worker, world.business)
    content = wire_content(world, tmp_path, monkeypatch)
    before = await world.adapter.get_state(worker.refs(world.campaign))
    launch = await create_experiment(worker, world.business, control_snapshot(world), design())
    assert launch.status == "applied"
    (experiment,) = world.repository.rows(Experiment)
    control = world.ads["borderline-1"]
    variant = next(item for item in world.repository.rows(Ad) if item.id == experiment.variant_ad_id)
    assert variant.external_id not in before
    assert variant.ad_group_id == control.ad_group_id
    assert content.llm.calls == ["ad"]
    # Весь поток показов, кликов, конверсий и расхода формирует Fake; строки не подставляются.
    for ad_id in world.adapter._ad_truths:
        world.adapter._ad_truths[ad_id] = AdTruth(0.01, 1, 0.3, 1200)
    world.adapter._ad_truths[variant.external_id] = AdTruth(0.04, 1, 0.3, 1200)
    await world.advance(worker, days=16)
    pre_verdict = await world.adapter.get_state(worker.refs(world.campaign))
    change = next(row for row in world.changes(kind="experiment_verdict") if row.id == experiment.change_id)
    assert experiment.verdict == "variant_wins" and experiment.status == "concluded"
    assert change.status == "applied" and change.id == experiment.change_id
    assert "Оставили Б, А остановили" in change.reason_text
    assert f"{change.evidence['variant']['impressions']:,}".replace(",", " ") in change.reason_text
    assert f"{change.evidence['variant']['clicks']:,}".replace(",", " ") in change.reason_text
    for arm in ("control", "variant"):
        metrics = change.evidence[arm]
        ctr = f"{metrics['clicks'] / metrics['impressions'] * 100:.1f}%".replace(".", ",")
        assert ctr in change.reason_text
    after = await world.adapter.get_state(worker.refs(world.campaign))
    assert after[control.external_id].status == "paused_auto"
    assert after[variant.external_id].status == "active"
    assert after[world.campaign.external_id] == pre_verdict[world.campaign.external_id]
    assert world.platform_spend() <= world.business.month_budget_kop
    reverted = await RollbackService(world.repository, dry_run=False).revert(
        change.id, business_id=world.business.id, adapter=world.adapter, now=world.clock.now
    )
    assert reverted.status == "applied" and change.status == "reverted"
    restored = await world.adapter.get_state(worker.refs(world.campaign))
    assert restored[control.external_id].status == "active"
    assert restored[variant.external_id].status == "paused_owner"
    assert all(
        restored[id_] == state for id_, state in pre_verdict.items()
        if id_ not in {control.external_id, variant.external_id}
    )
    assert experiment.status == "cancelled"
    assert control.frozen_until == variant.frozen_until == world.clock.now + timedelta(days=14)
    await experiments_tick(worker, world.business)
    assert sum(
        row.evidence.get("experiment_id") == experiment.id for row in world.changes(kind="experiment_verdict")
    ) == 1
    assert sum(
        item.ad_group_id == experiment.ad_group_id and item.hypothesis == experiment.hypothesis
        for item in world.repository.rows(Experiment)
    ) == 1
    assert world.platform_spend() <= world.business.month_budget_kop


async def test_rollback_of_add_ad_pauses_variant_and_freezes_group(tmp_path, monkeypatch):
    world = await make_world()
    world.clock.advance(days=7)
    worker = world.worker()
    await sync_moderation(worker, world.business)
    wire_content(world, tmp_path, monkeypatch)
    before = await world.adapter.get_state(worker.refs(world.campaign))
    launch = await create_experiment(worker, world.business, control_snapshot(world), design())
    (experiment,) = world.repository.rows(Experiment)
    variant = next(item for item in world.repository.rows(Ad) if item.id == experiment.variant_ad_id)
    result = await RollbackService(world.repository, dry_run=False).revert(
        launch.id, business_id=world.business.id, adapter=world.adapter, now=world.clock.now
    )
    assert result.status == "applied"
    states = await world.adapter.get_state(worker.refs(world.campaign))
    assert states[variant.external_id].status == "paused_owner"
    assert all(states[id_] == state for id_, state in before.items())
    assert experiment.status == "cancelled"
    assert all(
        ad.frozen_until == world.clock.now + timedelta(days=14)
        for ad in world.repository.rows(Ad)
        if ad.ad_group_id == variant.ad_group_id
    )
    assert await create_experiment(worker, world.business, control_snapshot(world), design()) is None
