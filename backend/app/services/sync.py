from __future__ import annotations

import hashlib
from collections import Counter
from collections.abc import Collection
from datetime import timedelta
from typing import TYPE_CHECKING

from app.db.models import Ad, AdGroup, Business, Campaign, StatsDaily
from app.db.query import Window
from app.platforms.fake import FakeAdapter
from app.worker.clock import today

if TYPE_CHECKING:
    from app.worker.jobs import Worker


def moderation_key(platform: str, external_id: str) -> str:
    """Ключ approvals и rewritten в settings["_worker"]; до F13 ключом был голый внешний id."""
    return f"{platform}:{external_id}"


def shared_ids(worker: Worker, business: Business) -> set[str]:
    """Внешние id объявлений, которые есть на нескольких площадках: старый голый ключ к ним не относится однозначно."""
    counts = Counter(
        external_id
        for platform in {campaign.platform for campaign in worker.campaigns(business)}
        for external_id in platform_ads(worker, business, platform)
    )
    return {external_id for external_id, count in counts.items() if count > 1}


def approved_at(meta: dict, platform: str, external_id: str, shared: Collection[str] = ()) -> str | None:
    approvals = meta.get("approvals", {})
    legacy = None if external_id in shared else approvals.get(external_id)
    return approvals.get(moderation_key(platform, external_id), legacy)


def platform_ads(worker: Worker, business: Business, platform: str) -> dict[str, Ad]:
    """Внешние id уникальны только внутри площадки: объявления ищутся по паре (площадка, внешний id)."""
    entities = worker.repository.local_entities(business.id, platform)
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
            if isinstance(adapter, FakeAdapter) and business.lead_goal_ids:
                stored.conversions = {str(business.lead_goal_ids[0]): row.conversions.get("lead", 0)}
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
    shared = shared_ids(worker, business)
    for campaign in worker.campaigns(business):
        ads = platform_ads(worker, business, campaign.platform)
        adapter = worker.resolve_adapter(business.id, campaign.platform)
        for info in await adapter.get_moderation(worker.refs(campaign)):
            ad = ads.get(info.entity_id)
            if ad is None:
                continue
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
                    key=f"moderation:{key}:{hashlib.sha256((info.reason or '').encode()).hexdigest()[:16]}",
                )
                # Генератор обязан вернуть уже проверенные текстовые операции; медицинские отказы не исправляем.
                reason = (info.reason or "").casefold()
                if (
                    worker.rewrite_rejected
                    and key not in rewritten
                    and (info.entity_id in shared or info.entity_id not in rewritten)
                    and any(word in reason for word in ("текст", "заголов", "опечат"))
                    and not any(word in reason for word in ("медицин", "документ", "лиценз"))
                ):
                    operations = await worker.rewrite_rejected(business, ad, info.reason)
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
                    rewritten = sorted(set(rewritten) | {key})
                    if not worker.proposed(business):
                        ad.status = "moderation"
            worker.repository.save(ad)
            worker.remember(business, approvals=approvals, rewritten=rewritten)
