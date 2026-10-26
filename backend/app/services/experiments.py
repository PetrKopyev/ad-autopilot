from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from app.db.models import Ad, AdGroup, Business, ChangeLog, Experiment
from app.domain.experiments import next_hypothesis
from app.domain.explain import explain
from app.domain.rules import can_change
from app.domain.settings import DEFAULT_SETTINGS
from app.domain.types import AdSnapshot, ExperimentDesign

if TYPE_CHECKING:
    from app.worker.jobs import Worker


def completed_hypotheses(worker: Worker, business: Business, group_id: int) -> tuple[str, ...]:
    return tuple(
        item.hypothesis
        for item in worker.repository.rows(Experiment, business.id)
        if item.ad_group_id == group_id and item.status == "concluded"
    )


def group_available(worker: Worker, business: Business, group: AdGroup) -> bool:
    """Откат любого объявления защищает всю группу, включая новое плечо теста."""
    now = worker.clock.now
    frozen = worker.metadata(business).get("frozen_entities", {}).get(f"group:{group.external_id}")
    if frozen is not None and datetime.fromisoformat(frozen) > now:
        return False
    ads = [item for item in worker.repository.rows(Ad, business.id) if item.ad_group_id == group.id]
    if any(item.frozen_until is not None and item.frozen_until > now for item in ads):
        return False
    campaign = next(item for item in worker.campaigns(business) if item.id == group.campaign_id)
    affected = {group.external_id, campaign.external_id, *(item.external_id for item in ads)}
    return not any(
        change.status == "applied"
        and now - change.created_at < timedelta(hours=DEFAULT_SETTINGS.cooldown_hours)
        and (
            change.entity_id in affected
            or (change.entity_type != "group" and any(op["entity_id"] in affected for op in change.operations))
        )
        for change in worker.repository.rows(ChangeLog, business.id)
    )


async def create_experiment(
    worker: Worker, business: Business, ad: AdSnapshot, design: ExperimentDesign
) -> ChangeLog | None:
    """Проверить ограничения и сохранить предложение без платных побочных эффектов.

    AdPlatform пока умеет создавать только целое дерево кампании, а set_budget
    разрешён только для campaign. Поэтому запуск в существующей группе с долей
    не более 25% сейчас невозможен: живой запуск безопасно отклоняется, а не
    создаёт отдельную кампанию с дополнительным бюджетом. Для включения запуска
    нужны создание объявления в группе и ограничение бюджета плеч на адаптере.
    """
    if (
        business.status != "running"
        or worker.metadata(business).get("kill_switch")
        or not ad.active
        or not can_change(ad, worker.clock.now)
        or not design.accepted
        or not 0 < design.estimated_days <= DEFAULT_SETTINGS.test_max_days
        or design.n_per_arm <= 0
    ):
        return None
    repository = worker.repository
    control = next((item for item in repository.rows(Ad, business.id) if item.external_id == ad.id), None)
    if control is None or control.status != "active":
        return None
    group = next(item for item in repository.rows(AdGroup, business.id) if item.id == control.ad_group_id)
    if group.external_id != ad.group_id or not group_available(worker, business, group):
        return None
    running = [item for item in repository.rows(Experiment, business.id) if item.status == "running"]
    if len(running) >= DEFAULT_SETTINGS.max_tests_per_business or any(item.ad_group_id == group.id for item in running):
        return None
    campaign = next(item for item in worker.campaigns(business) if item.id == group.campaign_id)
    if campaign.status != "active" or campaign.owner_paused or group.status != "active":
        return None
    siblings = [item for item in repository.rows(AdGroup, business.id) if item.campaign_id == campaign.id]
    budget = int(
        worker.metadata(business)
        .get("group_limits", {})
        .get(group.external_id, campaign.daily_limit_kop // len(siblings))
    )
    hypothesis = next_hypothesis(completed_hypotheses(worker, business, group.id), budget)
    if hypothesis is None or not 0 < design.daily_budget <= int(budget * DEFAULT_SETTINGS.test_budget_share):
        return None
    # Ключ не зависит от расписания: повтор завтра не создаёт новое предложение.
    key = f"experiment:{control.id}:{hypothesis}"
    existing = next(
        (item for item in repository.rows(ChangeLog, business.id) if item.evidence.get("experiment_key") == key),
        None,
    )
    if existing is not None and existing.status == "proposed":
        return existing
    if not worker.proposed(business):
        worker.notice(
            business,
            "EXPERIMENT_UNSUPPORTED",
            "Пока не можем безопасно начать проверку: площадка не поддерживает добавление "
            "варианта в эту группу и отдельный лимит расходов проверки.",
            key=key,
            target_id=ad.id,
        )
        return None
    params = {"hypothesis": hypothesis, "n_per_arm": design.n_per_arm, "days": design.estimated_days}
    proposal = ChangeLog(
        business_id=business.id,
        actor="autopilot",
        kind="launch",
        platform=campaign.platform,
        entity_type="ad",
        entity_id=ad.id,
        operations=[],
        reason_code="EXPERIMENT_START",
        reason_params=params,
        reason_text="Предложено: " + explain("EXPERIMENT_START", params) + f" ≈ {design.estimated_days} дней.",
        evidence={
            "experiment_key": key,
            "design": asdict(design) | {"hypothesis": hypothesis},
            "min_effect": str(DEFAULT_SETTINGS.test_design_lift),
            "metric": "ctr",
            "deadline": (worker.clock.now + timedelta(days=DEFAULT_SETTINGS.test_max_days)).isoformat(),
            "_service": {"dry_run": True, "refs": asdict(worker.refs(campaign))},
        },
        status="proposed",
        created_at=worker.clock.now,
    )
    repository.save(proposal)
    return proposal
