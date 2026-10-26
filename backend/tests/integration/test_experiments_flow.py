from datetime import timedelta
from decimal import Decimal

from app.db.models import Experiment, StatsDaily
from app.services.optimize import experiments_tick
from app.services.rollback import RollbackService
from tests.integration.harness import make_world


async def test_existing_experiment_variant_wins_explains_and_rolls_back_with_group_freeze():
    """Вердикт существующего теста; создание нового блокируется интерфейсом адаптера."""
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
                platform="fake",
                campaign_id=world.campaign.id,
                ad_id=ad.id,
                date=world.clock.now.date(),
                impressions=10_000,
                clicks=clicks,
                cost_kop=10_000,
                conversions={"lead": leads},
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
    assert "10000" in change.reason_text and "300" in change.reason_text
    assert "3,0%" in change.reason_text and "1,0%" in change.reason_text
    after = await world.adapter.get_state(world.refs)
    assert after[control.external_id].status == "paused"
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
