from __future__ import annotations

import hashlib
from collections import Counter
from collections.abc import Collection
from datetime import timedelta
from typing import TYPE_CHECKING

from app.db.models import Ad, AdGroup, Business, Campaign, StatsDaily
from app.db.query import Window
from app.platforms.base import Operation
from app.services.changes import CLOSED_ACCOUNT
from app.worker.clock import today

if TYPE_CHECKING:
    from app.worker.jobs import Worker


def moderation_key(platform: str, external_id: str) -> str:
    """Ключ approvals и rewritten в settings["_worker"]; до F13 ключом был голый внешний id."""
    return f"{platform}:{external_id}"


def shared_ids(worker: Worker, business: Business) -> set[str]:
    """Внешние id объявлений, которые есть на нескольких площадках: старый голый ключ к ним не относится однозначно."""
    platforms = {campaign.platform for campaign in worker.campaigns(business)}
    return shared_in({platform: platform_ads(worker, business, platform) for platform in platforms})


def shared_in(ads: dict[str, dict[str, Ad]]) -> set[str]:
    counts = Counter(external_id for items in ads.values() for external_id in items)
    return {external_id for external_id, count in counts.items() if count > 1}


def approved_at(meta: dict, platform: str, external_id: str, shared: Collection[str] = ()) -> str | None:
    approvals = meta.get("approvals", {})
    legacy = None if external_id in shared else approvals.get(external_id)
    return approvals.get(moderation_key(platform, external_id), legacy)


def platform_ads(worker: Worker, business: Business, platform: str) -> dict[str, Ad]:
    """Внешние id уникальны только внутри площадки: объявления ищутся по паре (площадка, внешний id)."""
    return ads_of(worker.repository.local_entities(business.id, platform))


def ads_of(entities: dict) -> dict[str, Ad]:
    return {external_id: item for (kind, external_id), item in entities.items() if kind == "ad"}


async def sync_stats(worker: Worker, business: Business) -> None:
    end = today(worker.clock)
    start = end - timedelta(days=6)
    campaigns = worker.campaigns(business)
    for platform in sorted({campaign.platform for campaign in campaigns}):
        entities = worker.repository.local_entities(business.id, platform)
        ads = {external_id: item for (kind, external_id), item in entities.items() if kind == "ad"}
        groups = {item.id: item for item in entities.values() if isinstance(item, AdGroup)}
        own = {item.id: item for item in entities.values() if isinstance(item, Campaign)}
        adapter = worker.resolve_adapter(business.id, platform)
        fetched = await adapter.get_stats(start, end, "ad")
        # Удалённая из отчёта строка тоже перезаписывается, а не остаётся старым расходом.
        incoming = {row.entity_id: [] for row in fetched}
        for row in fetched:
            incoming[row.entity_id].append(row)
        existing = {
            (row.ad_id, row.date): row
            for row in worker.repository.rows(StatsDaily, business.id, platform=platform, date=Window(start, end))
        }
        seen = set()
        changed = []
        for row in fetched:
            ad = ads.get(row.entity_id)
            group = groups.get(ad.ad_group_id) if ad else None
            if group is None or own[group.campaign_id].external_id != row.campaign_id:
                continue
            key = (ad.id, row.date)
            stored = existing.get(key) or StatsDaily(
                business_id=business.id,
                platform=platform,
                campaign_id=group.campaign_id,
                ad_id=ad.id,
                date=row.date,
            )
            for name in ("impressions", "clicks", "cost_kop", "conversions"):
                setattr(stored, name, getattr(row, name))
            stored.fetched_at = worker.clock.now
            stored.is_settled = row.is_settled and row.date <= end - timedelta(days=2)
            changed.append(stored)
            seen.add(key)
        for key, stored in existing.items():
            if key not in seen:
                stored.impressions = stored.clicks = stored.cost_kop = 0
                stored.conversions = {}
                stored.fetched_at = worker.clock.now
                stored.is_settled = stored.date <= end - timedelta(days=2)
                changed.append(stored)
        if changed:
            worker.repository.save(*changed)


async def sync_moderation(worker: Worker, business: Business) -> None:
    meta = worker.metadata(business)
    approvals = meta.get("approvals", {})
    rewritten = meta.get("rewritten", [])
    # Объявления прежних планов не показываются: их отказы не исправляются и не приходят уведомлениями.
    campaigns = worker.current_campaigns(business)
    # Сущности площадки читаются один раз на шаг: и для поиска объявлений, и для ссылок опроса каждой кампании.
    entities = {
        platform: worker.repository.local_entities(business.id, platform)
        for platform in {campaign.platform for campaign in campaigns}
    }
    ads_by_platform = {platform: ads_of(items) for platform, items in entities.items()}
    shared = shared_in(ads_by_platform)
    failure = None
    for campaign in campaigns:
        ads = ads_by_platform[campaign.platform]
        try:
            adapter = worker.resolve_adapter(business.id, campaign.platform)
            infos = await adapter.get_moderation(worker.refs(campaign, entities[campaign.platform]))
        except CLOSED_ACCOUNT as error:
            # Кабинет одной площадки требует входа заново или площадка не ответила: остальные всё равно опрашиваются.
            failure = failure or error
            continue
        polled = []
        try:
            for info in infos:
                ad = ads.get(info.entity_id)
                if ad is None:
                    continue
                polled.append(ad)
                key = moderation_key(campaign.platform, info.entity_id)
                ad.moderation_status, ad.moderation_reason = info.status, info.reason
                if info.status == "approved":
                    approved = approved_at(meta, campaign.platform, info.entity_id, shared)
                    approvals[key] = approved or info.updated_at.isoformat()
                    if ad.status == "moderation":
                        ad.status = "active"
                elif info.status == "rejected":
                    ad.status = "rejected"
                    worker.notice(
                        business,
                        "MODERATION_REJECTED",
                        f"Объявление «{ad.title}»: {info.reason.rstrip('. ')}."
                        if info.reason
                        else f"Объявление «{ad.title}» не прошло проверку, причину площадка не указала.",
                        # Время ответа площадки меняется при каждом опросе: одно уведомление на объявление и причину.
                        # Объявление (target_id) notice добавляет к ключу сам.
                        key=f"moderation:{hashlib.sha256((info.reason or '').encode()).hexdigest()[:16]}",
                        target_id=key,
                    )
                    # Генератор обязан вернуть уже проверенные текстовые операции; медицинские отказы не исправляем.
                    # В пробном режиме ИИ не зовём и ключ не отмечаем: владельцу остаётся уведомление об отказе.
                    reason = (info.reason or "").casefold()
                    if (
                        worker.rewrite_rejected
                        and not worker.proposed(business)
                        and key not in rewritten
                        and (info.entity_id in shared or info.entity_id not in rewritten)
                        and any(word in reason for word in ("текст", "заголов", "опечат"))
                        and not any(word in reason for word in ("медицин", "документ", "лиценз"))
                    ):
                        operations = await worker.rewrite_rejected(worker, business, campaign, ad, info.reason)
                        rewritten = sorted(set(rewritten) | {key})
                        # Исправление стоило вызова ИИ: отметка сохраняется сразу, ни отказ площадки, ни сбой дальше
                        # по шагу его не повторят.
                        worker.remember(business, ad, approvals=approvals, rewritten=rewritten)
                        if any(op.kind != "update_ad_text" or op.entity_id != info.entity_id for op in operations):
                            raise ValueError("Исправление модерации может менять только текст этого объявления.")
                        await worker.apply(
                            business,
                            campaign,
                            operations,
                            reason_code="REJECTED_REWRITE",
                            kind="text",
                            reason_params={"reason": info.reason or ""},
                            suffix=f"rewrite:{ad.id}",
                        )
                        if operations and not worker.proposed(business):
                            ad.status = "moderation"
        finally:
            # Статусы и отметки модерации кампании — одним commit, в том числе когда кампания опрошена не до конца:
            # иначе save(run) после сбоя сохранил бы статус active без даты одобрения.
            if polled:
                worker.remember(business, *polled, approvals=approvals, rewritten=rewritten)
    if failure is not None:
        raise failure


async def sync_campaign_status(worker: Worker, business: Business) -> None:
    """Пауза и включение кампании в кабинете площадки. Площадка не говорит, кто остановил кампанию: что меняли мы,
    помнит локальный статус — его переписывает каждое применённое изменение."""
    from app.config import get_domain_settings
    from app.services.budget_guard import closed_platforms

    held = set(worker.metadata(business).get("below_minimum", []))
    # Кабинет, который требует входа заново, спросить нечем: остальные площадки сверяются.
    pending = {change.platform for change in worker.repository.pending(business.id)}
    skipped = pending | closed_platforms(worker, business)
    for campaign in worker.current_campaigns(business):
        if campaign.platform in skipped:
            continue
        adapter = worker.resolve_adapter(business.id, campaign.platform)
        state = (await adapter.get_state(worker.refs(campaign)))[campaign.external_id]
        paused_outside = (
            state.status in {"paused_auto", "paused_owner"}
            and campaign.status == "active"
            and not campaign.owner_paused
            and campaign.id not in held
        )
        resumed_outside = state.status == "active" and campaign.status in {"paused_auto", "paused_owner"}
        if not (paused_outside or resumed_outside):
            if campaign.owner_paused and state.status == "active":
                campaign.owner_paused = False
                worker.repository.save(campaign)
            continue
        await worker.apply(
            business,
            campaign,
            [Operation("set_status", "campaign", campaign.external_id, "status", campaign.status, state.status)],
            actor="external",
            kind="pause" if paused_outside else "resume",
            reason_code="EXTERNAL_PAUSE" if paused_outside else "EXTERNAL_RESUME",
            suffix=f"external:{worker.clock.now.isoformat()}",
        )
        campaign.owner_paused = paused_outside
        if resumed_outside:
            campaign.frozen_until = worker.clock.now + timedelta(hours=get_domain_settings().owner_cooldown_hours)
            held.discard(campaign.id)
        worker.remember(business, campaign, below_minimum=sorted(held))
