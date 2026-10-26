from __future__ import annotations

import os
from contextlib import asynccontextmanager
from dataclasses import asdict, replace
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING

import httpx

from app.content.ad_text import generate_ad_text, offer_matrix, validate_ad_text
from app.content.art import ART_COST_KOP, ArtError
from app.content.yandexgpt import YandexGPTError
from app.db.models import Ad, AdGroup, Asset, Business, ChangeLog, Experiment, Plan
from app.domain.experiments import next_hypothesis
from app.domain.explain import explain
from app.domain.rules import can_change
from app.domain.settings import DEFAULT_SETTINGS
from app.domain.types import AdSnapshot, ExperimentDesign
from app.platforms.base import Operation, PlatformError
from app.services.changes import ChangeConflict
from app.services.onboarding import AiLimits, Blocked, SpendLLM, ai_budget, description_draft
from app.services.plan import PlanRepository, PlanService
from app.services.registry import build_art_factory, build_llm

if TYPE_CHECKING:
    from app.worker.jobs import Worker


def completed_hypotheses(worker: Worker, business: Business, group_id: int) -> tuple[str, ...]:
    return tuple(
        item.hypothesis
        for item in worker.repository.rows(Experiment, business.id)
        if item.ad_group_id == group_id and item.status == "concluded"
    )


def group_available(worker: Worker, business: Business, group: AdGroup, *, ignore_change_id: int | None = None) -> bool:
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
        change.id != ignore_change_id
        and change.status == "applied"
        and now - change.created_at < timedelta(hours=DEFAULT_SETTINGS.cooldown_hours)
        and (
            change.entity_id in affected
            or (change.entity_type != "group" and any(op["entity_id"] in affected for op in change.operations))
        )
        for change in worker.repository.rows(ChangeLog, business.id)
    )


@asynccontextmanager
async def content_service(worker: Worker):
    """Тот же контентный сервис и журнал трат, что при подготовке плана."""
    from app.config import settings

    async with httpx.AsyncClient() as http:
        yield PlanService(
            PlanRepository(worker.repository.db),
            build_llm(settings, http),
            build_art_factory(settings, http),
            Path(os.environ.get("MEDIA_DIR", "media")),
            AiLimits.from_settings(settings),
        )


async def create_experiment(
    worker: Worker, business: Business, ad: AdSnapshot, design: ExperimentDesign
) -> ChangeLog | None:
    with worker.repository.business_lock(business.id):
        return await _create_experiment(worker, business, ad, design)


async def _create_experiment(
    worker: Worker, business: Business, ad: AdSnapshot, design: ExperimentDesign
) -> ChangeLog | None:
    """Оба варианта используют общий бюджет группы; отдельный кэп плеча недоступен."""
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
    if group.external_id != ad.group_id:
        return None
    running = [item for item in repository.rows(Experiment, business.id) if item.status == "running"]
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
        (
            item
            for item in repository.rows(ChangeLog, business.id)
            if (item.evidence or {}).get("experiment_key") == key
        ),
        None,
    )
    if existing is not None and existing.status == "reverted":
        return None
    if existing is not None and (worker.proposed(business) or existing.status == "applied"):
        if any(item.change_id == existing.id for item in running) or worker.proposed(business):
            return existing
    other_running = [item for item in running if existing is None or item.change_id != existing.id]
    if len(other_running) >= DEFAULT_SETTINGS.max_tests_per_business or any(
        item.ad_group_id == group.id for item in other_running
    ):
        return None
    if not group_available(worker, business, group, ignore_change_id=existing.id if existing else None):
        return None
    if hypothesis == "audience" and not worker.proposed(business):
        worker.notice(
            business,
            "EXPERIMENT_UNSUPPORTED",
            "Проверка другой аудитории требует отдельной группы и пока недоступна.",
            key=key,
            target_id=ad.id,
        )
        return None
    if not worker.proposed(business) and any(item is not existing for item in repository.pending(business.id)):
        raise ChangeConflict("Сначала требуется сверить незавершённые изменения бизнеса.")
    params = {"hypothesis": hypothesis, "n_per_arm": design.n_per_arm, "days": design.estimated_days}
    proposal = existing or ChangeLog(
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
    if worker.proposed(business):
        return proposal
    return await launch_variant(worker, business, campaign, group, control, proposal, design, hypothesis)


async def prepare_variant(worker: Worker, business: Business, campaign, group, control: Ad, hypothesis: str) -> dict:
    adapter = worker.resolve_adapter(business.id, campaign.platform)
    source = (await adapter.get_state(worker.refs(campaign)))[control.external_id]
    creative = source.creative
    asset_id = control.asset_id
    async with content_service(worker) as service:
        budget = ai_budget(service.repository, business, service.limits, worker.clock.now.date())
        if hypothesis == "text":
            matrix = offer_matrix(business.niche_class or "other")
            original_angle = matrix[0][0]
            segment = matrix[0][1]
            for plan in worker.repository.rows(Plan, business.id):
                for planned_group in plan.payload.get("groups", []):
                    for planned_ad in planned_group.get("ads", []):
                        if (planned_ad["title"], planned_ad["text"]) == (control.title, control.text):
                            original_angle = planned_ad.get("angle", original_angle)
                            segment = planned_group["segment"]
            angle = next(angle for angle, _ in matrix if angle != original_angle)
            draft = description_draft(business)
            variant = await generate_ad_text(
                SpendLLM(service.llm, service.repository, business.id, budget, "experiment_text"),
                draft,
                platform=campaign.platform,
                angle=angle,
                segment=segment,
            )
            if variant.used_fallback or validate_ad_text(variant.ad, draft, platform=campaign.platform):
                raise Blocked("Не удалось проверить новый текст.")
            if (variant.ad.title, variant.ad.text) == (source.title, source.text):
                raise Blocked("Новый текст совпал с исходным.")
            creative = creative | {"title": variant.ad.title, "text": variant.ad.text}
        else:
            if budget.limit_kop - budget.spent_kop < ART_COST_KOP:
                raise Blocked("Лимит генерации картинок исчерпан.")
            assets = await service.generate_images(business, budget, 1)
            if not assets:
                raise Blocked("Не удалось получить проверенную картинку в пределах лимита ИИ.")
            asset = assets[0]
            asset_id = asset.id
            # Файл и id сохраняем до загрузки на площадку: повтор не вызывает ART заново.
            return {"creative": creative, "asset_id": asset_id, "image_path": str(service.media_root / asset.file_path)}
    return {"creative": creative, "asset_id": asset_id}


async def launch_variant(worker, business, campaign, group, control, change, design, hypothesis):
    repository = worker.repository
    stage = change.evidence.get("preparation")
    if stage in {"generating", "rejected"}:
        worker.notice(
            business,
            "EXPERIMENT_GENERATION_FAILED",
            "Не удалось подготовить проверенный вариант. Проверка не запущена.",
            key=change.evidence["experiment_key"],
            target_id=control.external_id,
        )
        return None
    if stage is None:
        change.evidence = change.evidence | {"preparation": "generating"}
        repository.save(change)
        try:
            prepared = await prepare_variant(worker, business, campaign, group, control, hypothesis)
        except (Blocked, ValueError, ArtError, YandexGPTError):
            change.status = "failed"
            change.evidence = change.evidence | {"preparation": "rejected"}
            repository.save(change)
            worker.notice(
                business,
                "EXPERIMENT_GENERATION_FAILED",
                "Не удалось подготовить проверенный вариант. Проверка не запущена.",
                key=change.evidence["experiment_key"],
                target_id=control.external_id,
            )
            return None
        change.evidence = change.evidence | prepared | {"preparation": "ready"}
        repository.save(change)
    adapter = worker.resolve_adapter(business.id, campaign.platform)
    creative = change.evidence["creative"]
    asset_id = change.evidence["asset_id"]
    if change.evidence.get("image_path"):
        asset = next(item for item in repository.rows(Asset, business.id) if item.id == asset_id)
        image = (asset.platform_refs or {}).get(campaign.platform)
        if image is None:
            image = await adapter.upload_image(Path(change.evidence["image_path"]))
            asset.platform_refs = (asset.platform_refs or {}) | {campaign.platform: image}
            repository.save(asset)
        creative = creative | {"image_ref": image}
        change.evidence = change.evidence | {"creative": creative}
    ref = change.evidence["experiment_key"]
    refs = replace(worker.refs(campaign), ads=worker.refs(campaign).ads | {ref: ref})
    operation = Operation("add_ad", "ad", ref, "creative", None, creative)
    change.operations = [asdict(operation)]
    change.evidence = change.evidence | {"_service": {"refs": asdict(refs), "dry_run": False}}
    change.status = "pending"
    repository.save(change)
    try:
        await adapter.apply(operation)
    except PlatformError:
        # Повтор завершит отправку на модерацию; поиск по ref исключает второй Ads.add.
        raise ChangeConflict("Добавление варианта требует повторной сверки.") from None
    states = await adapter.get_state(refs)
    state = states.get(ref)
    if state is None or state.creative != creative:
        raise ChangeConflict("Добавление варианта пока не подтверждено; требуется повторная сверка.")
    variant = next((item for item in repository.rows(Ad, business.id) if item.external_id == state.entity_id), None)
    if variant is None:
        variant = Ad(
            ad_group_id=group.id,
            external_id=state.entity_id,
            title=state.title,
            text=state.text,
            asset_id=asset_id,
            status="moderation",
            moderation_status="pending",
            role="variant",
        )
        repository.save(variant)
    refs = replace(refs, ads=refs.ads | {str(variant.id): state.entity_id})
    change.evidence = change.evidence | {"_service": {"refs": asdict(refs), "dry_run": False}}
    experiment = next((item for item in repository.rows(Experiment, business.id) if item.change_id == change.id), None)
    if experiment is None:
        experiment = Experiment(
            business_id=business.id,
            ad_group_id=group.id,
            control_ad_id=control.id,
            variant_ad_id=variant.id,
            hypothesis=hypothesis,
            metric="ctr",
            planned_n_per_arm=design.n_per_arm,
            min_effect=Decimal(str(DEFAULT_SETTINGS.test_design_lift)),
            started_at=worker.clock.now,
            deadline=worker.clock.now + timedelta(days=DEFAULT_SETTINGS.test_max_days),
            status="running",
            stats={"launch_change_id": change.id},
            change_id=change.id,
        )
        repository.save(experiment)
    control.role = "control"
    control.experiment_id = variant.experiment_id = experiment.id
    change.status = "applied"
    change.reason_text = explain("EXPERIMENT_START", change.reason_params)
    change.evidence = change.evidence | {"experiment_id": experiment.id}
    repository.save(control, variant, experiment, change)
    return change
