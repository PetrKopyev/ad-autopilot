from __future__ import annotations

import hashlib
from collections import Counter
from collections.abc import Collection
from datetime import timedelta
from typing import TYPE_CHECKING

from app.db.models import Ad, AdGroup, Business, Campaign, StatsDaily
from app.db.query import Window
from app.platforms.base import Operation
from app.services.changes import CLOSED_ACCOUNT, REVIEW, SETTLE, ChangeConflict
from app.worker.clock import today

if TYPE_CHECKING:
    from app.worker.jobs import Worker


def moderation_key(platform: str, external_id: str) -> str:
    """Ключ approvals и rewritten в settings["_worker"]; раньше ключом был голый внешний id."""
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
    failure = None
    for platform in sorted({campaign.platform for campaign in campaigns}):
        # Кабинет одной площадки не ответил: статистику другой всё равно доводим, иначе сбой Директа
        # замирал бы и на статистике VK.
        try:
            await _sync_platform_stats(worker, business, platform, start, end)
        except CLOSED_ACCOUNT as error:
            failure = failure or error
    if failure is not None:
        raise failure


async def _sync_platform_stats(worker: Worker, business: Business, platform: str, start, end) -> None:
    entities = worker.repository.local_entities(business.id, platform)
    ads = {external_id: item for (kind, external_id), item in entities.items() if kind == "ad"}
    groups = {item.id: item for item in entities.values() if isinstance(item, AdGroup)}
    own = {item.id: item for item in entities.values() if isinstance(item, Campaign)}
    adapter = worker.resolve_adapter(business.id, platform)
    fetched = await adapter.get_stats(start, end, "ad", campaign_ids={item.external_id for item in own.values()})
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
                    # Пока площадка не подтвердила другое изменение (исход ответа неизвестен), правка
                    # текста упадёт в конфликт: переписываем на следующем опросе, ключ до тех пор не отмечаем.
                    reason = (info.reason or "").casefold()
                    if (
                        worker.rewrite_rejected
                        and not worker.proposed(business)
                        and not worker.pending(business.id)
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
                            if campaign.review_since is None:
                                campaign.review_since = worker.clock.now
                                worker.repository.save(campaign)
        finally:
            # Статусы и отметки модерации кампании — одним commit, в том числе когда кампания опрошена не до конца:
            # иначе save(run) после сбоя сохранил бы статус active без даты одобрения.
            if polled:
                worker.remember(business, *polled, approvals=approvals, rewritten=rewritten)
    await settle_review(worker, business)
    if failure is not None:
        raise failure


# Через сколько дней проверки площадкой владелец узнаёт, что она затянулась: модерация в выходные реально занимает
# дольше суток, а статус вне известного набора (терминальный у VK, сбой площадки) выглядит как проверка бесконечно.
REVIEW_NOTICE_AFTER = timedelta(days=5)
PAUSED = {"paused_auto", "paused_owner"}


async def settle_review(worker: Worker, business: Business, *, pauses_only: bool = False) -> None:
    """Единственный владелец состояния «площадка ещё проверяет» (Campaign.review_since): локальный статус кампании и её
    объявлений — наша цель, и после проверки площадка приводится к ней. Включаем одобренное, но выключенное (OFF) и
    отправляем на проверку черновики объявлений — только у работающего бизнеса без остановки и только в кампании, чья
    цель — работать; паузу, которую площадка не применила к черновику, доводим всегда. SUSPENDED при цели «работает» —
    остановка в кабинете (EXTERNAL_PAUSE). Кампанию-черновик (Директ v501 не включает её, пока не одобрены объявления)
    включаем снова, когда одобрено хотя бы одно её объявление. Пока что-то в дереве на проверке, отметка остаётся;
    кампания, закончившаяся по дате окончания, ждёт продления (month_rollover). pauses_only — только кампании, чья
    цель — пауза: их страж бюджета доводит раз в 30 минут, не дожидаясь часового шага модерации."""
    pending = {change.platform for change in worker.repository.pending(business.id)}
    for campaign in worker.campaigns(business):
        if (
            campaign.review_since is None
            or campaign.platform in pending
            or pauses_only
            and campaign.status not in PAUSED
        ):
            continue
        entities = worker.repository.local_entities(business.id, campaign.platform)
        try:
            adapter = worker.resolve_adapter(business.id, campaign.platform)
            refs = worker.refs(campaign, entities)
            states = await adapter.get_state(refs)
        except CLOSED_ACCOUNT:
            continue
        # У остановленной кампании выключенные объявления — норма: их не включаем и не ждём.
        rows = [("campaign", campaign)] + [
            ("ad", entities[("ad", external_id)])
            for external_id in refs.ads.values()
            if ("ad", external_id) in entities and campaign.status == "active"
        ]
        ads = [row for kind, row in rows if kind == "ad"]
        approved = any(row.moderation_status == "approved" for row in ads)
        # Все объявления отклонены: показывать нечего, и включённая кампания была бы записью «включили рекламу»
        # впустую — владелец узнаёт об отказе из MODERATION_ALL_REJECTED.
        rejected = bool(ads) and all(row.status == "rejected" for row in ads)
        reviewing, waiting = set(), False
        outside, enables, pauses = [], [], []
        for kind, row in rows:
            state = states.get(row.external_id)
            if state is None or row.status in {"archived", "rejected"} or state.status in {"archived", "rejected"}:
                continue
            target = "active" if row.status == "moderation" else row.status
            if state.status in REVIEW:
                # Черновик, у которого отклонены все объявления, площадка не примет, пока их не исправят: ждать нечего,
                # и отметка снимается — кампанию снова сверяет sync_campaign_status. Исправление вернёт отметку.
                if not (kind == "campaign" and rejected):
                    reviewing.add(row.external_id)
                # Черновик объявления не отправлен на проверку: включение отправит (Директ — moderate).
                if target == "active" and state.status == "draft" and (kind == "ad" or approved):
                    enables.append(Operation("set_status", kind, row.external_id, "status", state.status, "active"))
            elif target == "active" and state.status == "paused_owner":
                outside.append(Operation("set_status", kind, row.external_id, "status", target, "paused_owner"))
            elif target == "active" and state.status != "active":
                if kind == "campaign" and rejected:
                    continue
                if state.ended:
                    waiting = True
                else:
                    enables.append(Operation("set_status", kind, row.external_id, "status", state.status, "active"))
            elif target in PAUSED and state.status == "active":
                pauses.append(Operation("set_status", kind, row.external_id, "status", state.status, target))
        fresh = worker.repository.fresh_business_state(business.id)
        if fresh is None:
            return
        status, settings = fresh
        meta = (settings or {}).get("_worker") or {}
        if enables and (status != "running" or meta.get("kill_switch") or meta.get("pending_stop")):
            enables, waiting = [], True
        change = None
        # Ключ записи — окно проверки и её операции: повтор шага в том же окне не пишет вторую такую же запись.
        window = campaign.review_since.isoformat()
        # Остановка кампании — пауза, даже если заодно включаются её объявления: «Продолжить» читает её как нашу.
        paused = not enables or any(op.entity_type == "campaign" for op in pauses)
        suffix = f"review:{window}:{'pause' if paused else 'resume'}:" + ",".join(
            sorted(op.entity_id for op in pauses + enables)
        )
        try:
            if outside:
                whole = any(op.entity_type == "campaign" for op in outside)
                await worker.apply(
                    business,
                    campaign,
                    outside,
                    actor="external",
                    kind="pause",
                    reason_code="EXTERNAL_PAUSE" if whole else "EXTERNAL_AD_PAUSE",
                    reason_text=None if whole else OUTSIDE_AD_PAUSE,
                    suffix=f"external:{window}:{','.join(sorted(op.entity_id for op in outside))}",
                )
                # Паузу владельца сохраняем сразу: следующая запись может упасть в конфликт.
                campaign.owner_paused = campaign.owner_paused or whole
                worker.repository.save(campaign)
            if pauses or enables:
                change = await worker.apply(
                    business,
                    campaign,
                    pauses + enables,
                    actor="system",
                    kind="pause" if paused else "resume",
                    reason_code=SETTLE,
                    reason_text=REVIEW_PAUSED if paused else REVIEW_RESUMED,
                    suffix=suffix,
                    force=True,
                )
        except ChangeConflict:
            change = worker.repository.change_by_key(business.id, f"{worker.step_key}:{campaign.id}:{suffix}")
            if paused or change is None or change.status != "failed":
                continue
            # Проверка закончена, а площадка оставила выключенным (State=OFF: например, на счёте нет денег): повтор
            # каждый час ничего не даст, а с отметкой кампанию не сверяли бы sync_campaign_status и balance_notice.
            change.reason_text = REVIEW_RESUME_FAILED
            worker.repository.save(change)
            change = None
        sent = set(change.evidence["_service"].get("review", ())) if change is not None else set()
        if change is not None and not sent:
            reviewing -= {op.entity_id for op in enables}
        if change is not None and not paused and {op.entity_id for op in enables} <= sent:
            # Включение только отправило черновик на проверку: реклама заработает после одобрения.
            change.reason_text = REVIEW_SENT
            worker.repository.save(change)
        if rejected:
            # Черновик без одобренных объявлений площадка не примет никогда: это не долгая проверка.
            worker.notice(
                business,
                "MODERATION_ALL_REJECTED",
                f"Площадка отклонила все объявления кампании «{campaign.name}», поэтому она не показывается. "
                "Причины — в уведомлениях об объявлениях: исправьте объявления в кабинете площадки.",
                key=f"rejected:{campaign.review_since.isoformat()}",
                target_id=campaign.id,
            )
        elif (
            reviewing and campaign.status == "active" and campaign.review_since + REVIEW_NOTICE_AFTER < worker.clock.now
        ):
            worker.notice(
                business,
                "MODERATION_LONG",
                f"Площадка больше {REVIEW_NOTICE_AFTER.days} дней проверяет объявления кампании «{campaign.name}». "
                "Проверьте объявления в кабинете площадки: возможно, модератор ждёт ответа или исправлений.",
                key=f"review:{campaign.review_since.isoformat()}",
                target_id=campaign.id,
            )
        if not (reviewing or waiting):
            campaign.review_since = None
        worker.repository.save(campaign)


REVIEW_RESUMED = "Площадка закончила проверку объявлений: включили рекламу."
REVIEW_RESUME_FAILED = (
    "Площадка закончила проверку объявлений, но реклама не включилась — например, на счёте нет денег. "
    "Проверьте счёт и кампанию в кабинете площадки."
)
REVIEW_SENT = "Отправили объявления на проверку площадке. Включим рекламу, как только она их одобрит."
OUTSIDE_AD_PAUSE = "Вы остановили объявление в кабинете площадки — автопилот его не включает."
REVIEW_PAUSED = "Площадка закончила проверку объявлений: реклама остаётся остановленной, как и была."


async def sync_campaign_status(worker: Worker, business: Business) -> None:
    """Пауза и включение кампании в кабинете площадки. Площадка не говорит, кто остановил кампанию: что меняли мы,
    помнит локальный статус — его переписывает каждое применённое изменение."""
    from app.config import get_domain_settings
    from app.services.budget_guard import campaign_state, closed_platforms

    held = set(worker.metadata(business).get("below_minimum", []))
    # Кабинет, который требует входа заново, спросить нечем: остальные площадки сверяются.
    pending = {change.platform for change in worker.repository.pending(business.id)}
    skipped = pending | closed_platforms(worker, business)
    for campaign in worker.current_campaigns(business):
        # Кампанию, которую площадка ещё проверяет, сверяет settle_review: её статус — цель, а не факт.
        if campaign.platform in skipped or campaign.review_since is not None:
            continue
        state = await campaign_state(worker, business, campaign)
        if state is None:
            continue
        paused_outside = (
            state.status in {"paused_auto", "paused_owner"}
            and campaign.status == "active"
            and not campaign.owner_paused
            and campaign.id not in held
            and not state.ended
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
