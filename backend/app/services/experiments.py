from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from dataclasses import asdict, replace
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING

import httpx

from app.content.ad_text import AdText, generate_ad_text, offer_matrix, validate_ad_text
from app.content.art import ART_COST_KOP, ArtError
from app.content.yandexgpt import YandexGPTError
from app.db.models import Ad, AdGroup, Asset, Business, ChangeLog, Experiment, Plan, Target
from app.db.query import Window
from app.demo_flags import demo_enabled
from app.domain.experiments import next_hypothesis
from app.domain.explain import explain
from app.domain.rules import can_change
from app.domain.settings import DEFAULT_SETTINGS, DomainSettings
from app.domain.types import AdSnapshot, ExperimentDesign
from app.platforms.base import AdGroupSpec, AdSpec, Operation, PlatformError, TargetSpec
from app.platforms.fake import FakeAdapter
from app.platforms.regions import phrase_city
from app.services.changes import PENDING, ChangeConflict
from app.services.onboarding import AiLimits, Blocked, SpendLLM, ai_budget, description_draft
from app.services.plan import PlanRepository, PlanService
from app.services.registry import build_art_factory, build_llm
from app.services.sync import platform_ads
from app.worker.clock import today

if TYPE_CHECKING:
    from app.worker.jobs import Worker

logger = logging.getLogger(__name__)

VARIANT_UNCONFIRMED = "Площадка ещё не подтвердила новый вариант объявления. Проверим ещё раз в ближайшие минуты."


def completed_hypotheses(worker: Worker, business: Business, group_id: int) -> tuple[str, ...]:
    return tuple(
        item.hypothesis
        for item in worker.repository.rows(Experiment, business.id, ad_group_id=group_id)
        if item.status == "concluded" or (item.stats or {}).get("reverted_by_change_id") is not None
    )


def group_available(worker: Worker, business: Business, group: AdGroup, *, ignore_change_id: int | None = None) -> bool:
    """Откат любого объявления защищает всю группу, включая новое плечо теста."""
    now = worker.clock.now
    frozen = worker.metadata(business).get("frozen_entities", {}).get(f"group:{group.external_id}")
    if frozen is not None and datetime.fromisoformat(frozen) > now:
        return False
    ads = worker.repository.rows(Ad, business.id, ad_group_id=group.id)
    if any(item.frozen_until is not None and item.frozen_until > now for item in ads):
        return False
    campaign = next(item for item in worker.campaigns(business) if item.id == group.campaign_id)
    child_ids = {item.external_id for item in ads} | {
        item.external_id for item in worker.repository.rows(Target, business.id, ad_group_id=group.id)
    }
    affected = {group.external_id, campaign.external_id, *child_ids}

    def blocks(change: ChangeLog) -> bool:
        if change.actor == "system" and change.kind == "budget":
            return False
        # Внешние id уникальны только внутри площадки: изменение другой площадки эту группу не трогает.
        if change.platform is not None and change.platform != campaign.platform:
            return False
        # SCALE другой группы хранит операцию с общим кэпом кампании.
        if change.entity_type == "group" and change.entity_id != group.external_id:
            return False
        if change.entity_id in child_ids or change.entity_id == group.external_id:
            return True
        if change.actor in {"owner", "external"} and (change.entity_id in affected or change.entity_type == "business"):
            return True
        return any(
            op["entity_id"] in child_ids
            or op["entity_id"] in affected
            and (
                op.get("field") in {"status", "targeting"} or change.actor in {"owner", "external"} or "field" not in op
            )
            for op in change.operations
        )

    cooldown = timedelta(hours=DEFAULT_SETTINGS.cooldown_hours)
    return not any(
        change.id != ignore_change_id and now - change.created_at < cooldown and blocks(change)
        for change in worker.repository.rows(
            ChangeLog, business.id, status="applied", created_at=Window(now - cooldown)
        )
    )


@asynccontextmanager
async def content_service(worker: Worker):
    """Тот же контентный сервис и журнал трат, что при подготовке плана."""
    from app.config import get_domain_settings, settings

    async with httpx.AsyncClient() as http:
        yield PlanService(
            PlanRepository(worker.repository.db),
            build_llm(settings, http),
            build_art_factory(settings, http),
            settings.media_dir,
            AiLimits.from_settings(settings),
            domain=get_domain_settings(settings),
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
    control = platform_ads(worker, business, ad.platform).get(ad.id)
    if control is None or control.status != "active":
        return None
    group = repository.find(AdGroup, business_id=business.id, id=control.ad_group_id)
    if group.external_id != ad.group_id:
        return None
    running = repository.rows(Experiment, business.id, status="running")
    campaign = next(item for item in worker.campaigns(business) if item.id == group.campaign_id)
    if campaign.status != "active" or campaign.owner_paused or group.status != "active":
        return None
    siblings = repository.rows(AdGroup, business.id, campaign_id=campaign.id)
    budget = int(
        worker.metadata(business)
        .get("group_limits", {})
        .get(group.external_id, campaign.daily_limit_kop // len(siblings))
    )
    hypothesis = next_hypothesis(completed_hypotheses(worker, business, group.id), budget, experiment_settings())
    if hypothesis is None or not 0 < design.daily_budget <= int(budget * DEFAULT_SETTINGS.test_budget_share):
        return None
    # Ключ не зависит от расписания: повтор завтра не создаёт новое предложение.
    key = f"experiment:{control.id}:{hypothesis}"
    existing = repository.change_by_key(business.id, key)
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
    audience = await audience_variant(worker, business, campaign, group) if hypothesis == "audience" else None
    if hypothesis == "audience" and audience is None:
        worker.notice(
            business,
            "EXPERIMENT_UNSUPPORTED",
            "Другую аудиторию для этой группы подобрать не из чего, поэтому аудитории не сравниваем.",
            key=key,
            target_id=ad.id,
        )
        return None
    if not worker.proposed(business) and any(item is not existing for item in worker.pending(business.id)):
        raise ChangeConflict(PENDING)
    params = {"hypothesis": hypothesis, "n_per_arm": design.n_per_arm, "days": design.estimated_days}
    if audience is not None:
        control_label, variant_label = audience["labels"]
        params |= {
            "group": group.theme,
            "control": control_label,
            "variant": variant_label,
            "days": DEFAULT_SETTINGS.test_max_days,
        }
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
        reason_text=explain("AUDIENCE_START" if audience is not None else "EXPERIMENT_START", params),
        evidence={
            "experiment_key": key,
            "design": asdict(design) | {"hypothesis": hypothesis},
            "min_effect": str(DEFAULT_SETTINGS.test_design_lift),
            "metric": "cpl" if audience is not None else "ctr",
            "deadline": (worker.clock.now + timedelta(days=DEFAULT_SETTINGS.test_max_days)).isoformat(),
            "_service": {"dry_run": True, "refs": asdict(worker.refs(campaign))},
        },
        status="proposed",
        idempotency_key=key,
        created_at=worker.clock.now,
    )
    repository.save(proposal)
    if worker.proposed(business):
        return proposal
    if audience is not None:
        return await launch_audience(worker, business, campaign, group, control, proposal, design, audience)
    return await launch_variant(worker, business, campaign, group, control, proposal, design, hypothesis)


class AiLimitReached(Blocked):
    """Недельный лимит ИИ исчерпан: вариант готовится заново на следующей московской неделе."""


async def prepare_variant(worker: Worker, business: Business, campaign, group, control: Ad, hypothesis: str) -> dict:
    adapter = worker.resolve_adapter(business.id, campaign.platform)
    source = (await adapter.get_state(worker.refs(campaign)))[control.external_id]
    async with content_service(worker) as service:
        budget = ai_budget(service.repository, business, service.limits, today(worker.clock))
        try:
            return await generate_variant(worker, business, campaign, control, source, hypothesis, service, budget)
        except (Blocked, ValueError, ArtError, YandexGPTError) as error:
            # SpendLLM не пускает вызов при spent >= limit, картинке нужен целый ART_COST_KOP.
            if budget.limit_kop - budget.spent_kop < (1 if hypothesis == "text" else ART_COST_KOP):
                raise AiLimitReached("Лимит трат на ИИ на этой неделе исчерпан.") from error
            raise


def planned_ad(worker: Worker, business: Business, ad: Ad) -> tuple[str, str, str]:
    """Заход, сегмент и услуга объявления в плане; не нашлось — первая строка матрицы ниши без услуги."""
    angle, segment = offer_matrix(business.niche_class or "other")[0]
    service_name = ""
    for plan in worker.repository.rows(Plan, business.id):
        for planned_group in plan.payload.get("groups", []):
            for item in planned_group.get("ads", []):
                if (item["title"], item["text"]) == (ad.title, ad.text):
                    angle = item.get("angle", angle)
                    segment = planned_group["segment"]
                    service_name = planned_group.get("service", "")
    return angle, segment, service_name


async def rewrite_rejected(worker: Worker, business: Business, campaign, ad: Ad, reason: str | None) -> list[Operation]:
    """Новый текст объявления, отклонённого модерацией за текст: тот же заход, повод и услуга, проверенный и не похожий
    на отклонённый. Не получилось (лимит ИИ, ответ не прошёл проверки) — пустой список: отказ остаётся в уведомлении."""
    angle, segment, service_name = planned_ad(worker, business, ad)
    draft = description_draft(business)
    async with content_service(worker) as service:
        budget = ai_budget(service.repository, business, service.limits, today(worker.clock))
        try:
            variant = await generate_ad_text(
                SpendLLM(service.llm, service.repository, business.id, budget, "moderation_rewrite"),
                draft,
                platform=campaign.platform,
                angle=angle,
                segment=segment,
                service=next((item for item in draft.services if item.name == service_name), None),
                other=AdText(ad.title, ad.text),
            )
        except (Blocked, ValueError, YandexGPTError):
            logger.warning("Отклонённое объявление %s не переписано", ad.id, exc_info=True)
            return []
    if variant.used_fallback or validate_ad_text(variant.ad, draft, platform=campaign.platform):
        return []
    return [
        Operation("update_ad_text", "ad", ad.external_id, field, getattr(ad, field), getattr(variant.ad, field))
        for field in ("title", "text")
        if getattr(variant.ad, field) != getattr(ad, field)
    ]


async def generate_variant(worker, business, campaign, control, source, hypothesis, service, budget) -> dict:
    creative = source.creative
    asset_id = control.asset_id
    matrix = offer_matrix(business.niche_class or "other")
    original_angle, segment, service_name = planned_ad(worker, business, control)
    if hypothesis == "text":
        angle = next(angle for angle, _ in matrix if angle != original_angle)
        draft = description_draft(business)
        variant = await generate_ad_text(
            SpendLLM(service.llm, service.repository, business.id, budget, "experiment_text"),
            draft,
            platform=campaign.platform,
            angle=angle,
            segment=segment,
            service=next((item for item in draft.services if item.name == service_name), None),
        )
        if variant.used_fallback or validate_ad_text(variant.ad, draft, platform=campaign.platform):
            raise Blocked("Не удалось проверить новый текст.")
        if (variant.ad.title, variant.ad.text) == (source.title, source.text):
            raise Blocked("Новый текст совпал с исходным.")
        creative = creative | {"title": variant.ad.title, "title2": variant.ad.title2 or None, "text": variant.ad.text}
    else:
        if budget.limit_kop - budget.spent_kop < ART_COST_KOP:
            raise Blocked("Лимит генерации картинок исчерпан.")
        # Картинка варианта — про ту же услугу и сегмент, что и группа исходного объявления.
        assets = await service.generate_images(business, budget, [(service_name, segment)])
        if not assets:
            raise Blocked("Не удалось получить проверенную картинку в пределах лимита ИИ.")
        _, asset = assets[0]
        asset_id = asset.id
        # Файл и id сохраняем до загрузки на площадку: повтор не вызывает ART заново.
        return {"creative": creative, "asset_id": asset_id, "image_path": str(service.media_root / asset.file_path)}
    return {"creative": creative, "asset_id": asset_id}


async def launch_variant(worker, business, campaign, group, control, change, design, hypothesis):
    repository = worker.repository
    stage = change.evidence.get("preparation")
    if stage == "ai_limit":
        if today(worker.clock) < date.fromisoformat(change.evidence["retry_on"]):
            return None
        stage = None
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
        except AiLimitReached:
            # Гипотеза не отвергнута: в понедельник лимит обновится, и вариант готовится заново.
            day = today(worker.clock)
            retry_on = (day + timedelta(days=7 - day.weekday())).isoformat()
            change.status = "failed"
            change.evidence = change.evidence | {"preparation": "ai_limit", "retry_on": retry_on}
            repository.save(change)
            worker.notice(
                business,
                "EXPERIMENT_AI_LIMIT",
                "Лимит трат на ИИ на этой неделе исчерпан. Вариант для проверки подготовим на следующей неделе.",
                key=f"{change.evidence['experiment_key']}:ai_limit:{retry_on}",
                target_id=control.external_id,
            )
            return None
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
        asset = repository.find(Asset, business_id=business.id, id=asset_id)
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
        raise ChangeConflict(VARIANT_UNCONFIRMED) from None
    states = await adapter.get_state(refs)
    state = states.get(ref)
    if state is None or state.creative != creative:
        raise ChangeConflict(VARIANT_UNCONFIRMED)
    variant = platform_ads(worker, business, campaign.platform).get(state.entity_id)
    if variant is None:
        if demo_enabled() and isinstance(adapter, FakeAdapter):
            truth = adapter._ad_truths[control.external_id]
            adapter._ad_truths[state.entity_id] = replace(truth, ctr=min(1, truth.ctr * 2), conversion_rate=0.15)
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
    experiment = repository.find(Experiment, business_id=business.id, change_id=change.id)
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


# Вторая аудитория VK: взрослые, которые чаще сами платят за услуги, вместо всех возрастов.
AUDIENCE_AGES = (25, 54)


# В демо бюджет 30–50 тыс. ₽ в месяц: группе не набрать 700 ₽ в день, и приёмщик не увидел бы проверку аудиторий.
DEMO_AUDIENCE_MIN_DAILY_BUDGET = 10_000


def experiment_settings() -> DomainSettings:
    """Пороги гипотез: в демо проверка аудиторий начинается со 100 ₽ в день у группы, на живых площадках — с 700 ₽."""
    if demo_enabled():
        return replace(DEFAULT_SETTINGS, audience_min_daily_budget=DEMO_AUDIENCE_MIN_DAILY_BUDGET)
    return DEFAULT_SETTINGS


async def audience_variant(worker: Worker, business: Business, campaign, group: AdGroup) -> dict | None:
    """Другая аудитория для группы. Директ: те же фразы без названия города и город в минус-фразах — люди того же
    региона, которые не пишут город в запросе; запросы с городом достаются только исходной группе, аудитории не
    пересекаются. VK: 25–54 года вместо всех возрастов; исключить этот возраст из исходной группы значило бы сменить
    её аудиторию, поэтому люди 25–54 лет видят обе группы (overlap) и вывод говорит об этом прямо. Отличить нечем —
    None."""
    adapter = worker.resolve_adapter(business.id, campaign.platform)
    targeting = (await adapter.get_state(worker.refs(campaign)))[group.external_id].targeting
    if campaign.platform == "direct":
        city = f" {phrase_city(business.city).lower()}"
        phrases = [
            target.value
            for target in worker.repository.rows(Target, business.id, ad_group_id=group.id)
            if target.kind == "phrase" and target.status == "active"
        ]
        wide = list(dict.fromkeys(phrase.removesuffix(city) for phrase in phrases))
        if not phrases or wide == phrases:
            return None
        negatives = list(targeting.get("negative_keywords", []))
        return {
            "targeting": targeting | {"negative_keywords": [*negatives, city.strip()]},
            "phrases": wide,
            "name": "без города в запросе",
            "labels": ["ищут с названием города", "ищут без названия города"],
        }
    if targeting.get("age_list"):
        return None
    low, high = AUDIENCE_AGES
    return {
        "targeting": {
            key: value for key, value in targeting.items() if key in {"regions", "sex", "interests"} and value
        }
        | {"age_from": low, "age_to": high},
        "phrases": [],
        "name": f"{low}–{high} года",
        "labels": ["все возрасты", f"{low}–{high} года"],
        "overlap": True,
    }


async def launch_audience(worker, business, campaign, group, control, change, design, audience):
    """Новая группа с копией пограничного объявления и другой аудиторией; объявление то же, отличается только
    аудитория. Строки группы создаются один раз: повтор после сбоя площадки находит их в evidence."""
    repository = worker.repository
    adapter = worker.resolve_adapter(business.id, campaign.platform)
    if "audience_group_id" not in change.evidence:
        variant_group = AdGroup(
            campaign_id=campaign.id, theme=f"{group.theme} — {audience['name']}", geo=group.geo, status="active"
        )
        repository.save(variant_group)
        targets = [
            Target(ad_group_id=variant_group.id, kind="phrase", value=value, status="active")
            for value in audience["phrases"]
        ]
        variant = Ad(
            ad_group_id=variant_group.id,
            title=control.title,
            text=control.text,
            asset_id=control.asset_id,
            status="moderation",
            moderation_status="pending",
            role="variant",
        )
        repository.save(variant, *targets)
        change.evidence = change.evidence | {
            "audience_group_id": variant_group.id,
            "audience_ad_id": variant.id,
            "audience": audience,
        }
        change.status = "pending"
        repository.save(change)
    variant_group = repository.find(AdGroup, business_id=business.id, id=change.evidence["audience_group_id"])
    variant = repository.find(Ad, business_id=business.id, id=change.evidence["audience_ad_id"])
    targets = repository.rows(Target, business.id, ad_group_id=variant_group.id)
    source = (await adapter.get_state(worker.refs(campaign)))[control.external_id]
    spec = AdGroupSpec(
        str(variant_group.id),
        variant_group.theme,
        (AdSpec(str(variant.id), variant.title, variant.text, source.image_ref, title2=variant.title2),),
        tuple(TargetSpec(str(target.id), "phrase", target.value) for target in targets),
        audience["targeting"],
    )
    try:
        refs = await adapter.add_group(campaign.external_id, spec)
    except PlatformError:
        # Повтор найдёт группу и объявление по ключам ap-<id> и не создаст дублей.
        raise ChangeConflict(VARIANT_UNCONFIRMED) from None
    variant_group.external_id = refs.groups[spec.id]
    variant.external_id = refs.ads[str(variant.id)]
    for target in targets:
        target.external_id = refs.targets[str(target.id)]
    repository.save(variant_group, variant, *targets)
    if demo_enabled() and isinstance(adapter, FakeAdapter):
        # В демо новая аудитория обращается чаще: проверка доходит до вывода «новая аудитория дешевле».
        truth = adapter._ad_truths[control.external_id]
        adapter._ad_truths[variant.external_id] = replace(truth, conversion_rate=min(1, truth.conversion_rate * 2))
    started = worker.clock.now
    experiment = repository.find(Experiment, business_id=business.id, change_id=change.id)
    if experiment is None:
        experiment = Experiment(
            business_id=business.id,
            ad_group_id=group.id,
            control_ad_id=control.id,
            variant_ad_id=variant.id,
            hypothesis="audience",
            metric="cpl",
            planned_n_per_arm=design.n_per_arm,
            min_effect=Decimal(str(DEFAULT_SETTINGS.test_win_lift)),
            started_at=started,
            # Вывод — когда уточнятся цифры последнего дня проверки; день запаса на шаг воркера.
            # settlement_days добавляет experiment_expire_date: здесь он не нужен, иначе группа занята
            # на два дня дольше.
            deadline=started + timedelta(days=DEFAULT_SETTINGS.test_max_days + 1),
            status="running",
            stats={
                "launch_change_id": change.id,
                "audiences": audience["labels"],
                "overlap": audience.get("overlap", False),
            },
            change_id=change.id,
        )
        repository.save(experiment)
    control.role = "control"
    control.experiment_id = variant.experiment_id = experiment.id
    change.status = "applied"
    change.evidence = change.evidence | {
        "experiment_id": experiment.id,
        "_service": {"refs": asdict(worker.refs(campaign)), "dry_run": False},
    }
    repository.save(control, variant, experiment, change)
    return change


def testing_groups(worker: Worker, business: Business) -> set[int]:
    """Группы идущих проверок; у проверки аудитории это и новая группа: автопилот не трогает их до вывода."""
    running = worker.repository.rows(Experiment, business.id, status="running")
    ads = {ad.id: ad for ad in worker.repository.rows(Ad, business.id)}
    return {item.ad_group_id for item in running} | {
        ads[item.variant_ad_id].ad_group_id for item in running if item.variant_ad_id in ads
    }
