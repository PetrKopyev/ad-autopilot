from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import asdict
from datetime import date, datetime, timedelta
from decimal import Decimal
from math import ceil
from typing import TYPE_CHECKING

from app.db.models import Ad, AdGroup, BudgetLedger, Business, Campaign, ChangeLog, Experiment, StatsDaily
from app.db.query import Window
from app.domain.budget import (
    CAP_MULTIPLIER,
    MINIMUM_MARGIN,
    PLATFORM_MINIMUMS,
    allocate,
    campaign_limits,
    daily_floor,
    meets_minimum,
    plan_budget,
    platform_shares,
    rebalance_platform_shares,
    share_floors,
)
from app.domain.experiments import design_experiment, evaluate_audience, evaluate_experiment, select_borderline
from app.domain.explain import explain_audience, explain_decision, explain_experiment, rubles
from app.domain.rules import decide, ref_cpl, settled_days
from app.domain.settings import DEFAULT_SETTINGS
from app.domain.types import (
    Action,
    AdSnapshot,
    ClientAnswer,
    DaySpend,
    ExperimentDesign,
    ExperimentResult,
    GroupSnapshot,
    PlatformLimit,
    Verdict,
    total_metrics,
)
from app.platforms.base import Operation, PlatformError, lead_count
from app.services.budget_guard import closed_platforms, read_spend
from app.services.changes import ChangeConflict, budget_operations
from app.services.experiments import completed_hypotheses, experiment_settings, group_available, testing_groups
from app.services.sync import approved_at, shared_ids, sync_campaign_status
from app.worker.clock import MOSCOW, today

if TYPE_CHECKING:
    from app.worker.jobs import Worker

logger = logging.getLogger(__name__)


# Кампания без работающих объявлений: её останавливают не за нехватку бюджета, а потому что показывать нечего.
NO_ACTIVE_ADS = "Все объявления кампании остановлены, поэтому остановили и саму кампанию: показывать в ней нечего."


async def snapshot(
    worker: Worker, business: Business, *, include_paused_campaigns: bool = False
) -> tuple[list[AdSnapshot], list[GroupSnapshot]]:
    repository = worker.repository
    closed = closed_platforms(worker, business)
    campaigns = [campaign for campaign in worker.current_campaigns(business) if campaign.platform not in closed]
    states = {}
    for campaign in campaigns:
        states.update(await worker.resolve_adapter(business.id, campaign.platform).get_state(worker.refs(campaign)))
    campaign_map = {campaign.id: campaign for campaign in campaigns}
    groups = repository.rows(AdGroup, business.id)
    ads = repository.rows(Ad, business.id)
    group_map = {group.id: group for group in groups}
    meta = worker.metadata(business)
    limits = meta.get("group_limits", {})
    shared = shared_ids(worker, business)
    from app.config import get_domain_settings

    # Время изменения нужно правилам только внутри окна охлаждения: старые записи журнала не читаем.
    domain = get_domain_settings()
    recent = timedelta(hours=max(domain.cooldown_hours, domain.owner_cooldown_hours))
    history = repository.rows(ChangeLog, business.id, status="applied", created_at=Window(worker.clock.now - recent))

    def changed(entity_id: str, actor: str | None = None):
        return max(
            (
                change.created_at
                for change in history
                if (actor is None or change.actor == actor)
                and (
                    change.entity_id == entity_id
                    or (change.entity_type != "group" and any(op["entity_id"] == entity_id for op in change.operations))
                )
            ),
            default=None,
        )

    result = []
    rows = repository.rows(StatsDaily, business.id)
    for ad in ads:
        if ad.external_id not in states:
            continue
        group = group_map[ad.ad_group_id]
        platform = campaign_map[group.campaign_id].platform
        approved = approved_at(meta, platform, ad.external_id, shared)
        days = tuple(
            DaySpend(
                row.date,
                row.cost_kop,
                row.impressions,
                row.clicks,
                min(row.clicks, lead_count(row.conversions, business.lead_goal_ids)),
            )
            for row in rows
            if row.ad_id == ad.id and row.is_settled
        )
        result.append(
            AdSnapshot(
                ad.external_id,
                group.external_id,
                platform,
                datetime.fromisoformat(approved) if approved else None,
                days,
                states[ad.external_id].status == "active",
                changed(ad.external_id),
                ad.frozen_until,
                changed(ad.external_id, "owner"),
                lifetime_impressions=sum(row.impressions for row in rows if row.ad_id == ad.id),
            )
        )
    # Кампании, остановленные перед отключением кабинета, продолжат после его подключения: доля бюджета за ними.
    revoked = {
        campaign.id
        for campaign in campaigns
        if include_paused_campaigns
        and campaign.status == "paused_owner"
        and (last := repository.last_status_change(business.id, campaign.platform, "campaign", campaign.external_id))
        and last.reason_code == "ACCOUNT_REVOKE"
    }
    group_snapshots = []
    for group in groups:
        if group.external_id not in states or group.campaign_id not in campaign_map:
            continue
        campaign = campaign_map[group.campaign_id]
        siblings = [item for item in groups if item.campaign_id == campaign.id]
        equal = campaign.daily_limit_kop // len(siblings)
        group_snapshots.append(
            GroupSnapshot(
                group.external_id,
                campaign.platform,
                int(limits.get(group.external_id, equal)),
                states[group.external_id].status == "active"
                and (
                    states[campaign.external_id].status == "active"
                    or (
                        include_paused_campaigns
                        and (
                            campaign.status == "paused_auto"
                            or campaign.id in meta.get("kill_switch_campaigns", [])
                            or campaign.id in revoked
                        )
                        and states[campaign.external_id].status in {"paused_auto", "paused_owner"}
                        and not campaign.owner_paused
                    )
                )
                and any(item.active and item.group_id == group.external_id for item in result),
                changed(group.external_id),
                frozen_until=datetime.fromisoformat(meta["frozen_entities"][f"group:{group.external_id}"])
                if f"group:{group.external_id}" in meta.get("frozen_entities", {})
                else None,
                owner_edited_at=changed(campaign.external_id, "owner"),
            )
        )
    return result, group_snapshots


def reference(worker: Worker, business: Business, ads: list[AdSnapshot]) -> int | None:
    from app.db.models import LeadsSurvey

    # В опорную цену обращения идёт расход только площадок с источником конверсий: у VK без пикселя
    # обращений ноль, и её расход раздувал бы среднюю цену, на которую опираются правила Директа.
    missing = missing_conversion_sources(business)
    ads = [ad for ad in ads if ad.platform not in missing]
    answers = [
        ClientAnswer(row.week_start + timedelta(days=6), row.leads_count, row.clients_count)
        for row in worker.repository.rows(LeadsSurvey, business.id)
    ]
    return ref_cpl(business.avg_check_kop, ads, today(worker.clock), answers)


def freed_daily_budget(groups: Sequence[GroupSnapshot]) -> int:
    """Лимит, освободившийся полностью остановленными группами (RULES: деньги берутся из освободившегося).
    Частично остановленная группа не донорит: лимит на площадках общий на группу, оставшиеся объявления
    тратят его целиком — иначе после каждого A/B-теста группа с одним победителем «освобождала» бы
    половину лимита и SCALE снимал бы деньги с работающей рекламы."""
    return sum(group.daily_limit for group in groups if not group.active)


def missing_conversion_sources(business: Business) -> set[str]:
    """Площадки без источника конверсий: для них «нет обращений» не останавливает объявления (В1 круга 4).
    VK без пикселя (цель кабинета или общая VK_PRICED_GOAL_SOURCE_ID) всегда давала бы 0 обращений,
    Директ без счётчика и целей Метрики — то же."""
    from app.config import settings as config

    missing = set()
    if not (business.metrica_counter_id and business.lead_goal_ids):
        missing.add("direct")
    cabinet_goal = (business.settings or {}).get("vk_priced_goal_source_id")
    if not (cabinet_goal or getattr(config, "vk_priced_goal_source_id", None)):
        missing.add("vk")
    return missing


async def optimize(worker: Worker, business: Business) -> None:
    from app.config import get_domain_settings
    from app.services.launch import sync_removed_services

    if business.status != "running" or worker.metadata(business).get("kill_switch"):
        return
    await sync_removed_services(worker, business)
    ads, groups = await snapshot(worker, business)
    limits = {group.id: group.daily_limit for group in groups}
    # Освободившаяся доля остаётся доступной, но не доливается в пограничные объявления.
    free = freed_daily_budget(groups)
    decisions = decide(
        ads,
        groups,
        worker.clock.now.astimezone(MOSCOW),
        reference(worker, business, ads),
        free_daily_budget=free,
        platforms_without_source=missing_conversion_sources(business),
        settings=get_domain_settings(),
    )
    campaigns = worker.campaigns(business)
    local_groups = worker.repository.rows(AdGroup, business.id)
    testing = testing_groups(worker, business)
    ad_map = {ad.id: ad for ad in ads}
    lifetime = None
    floors: dict[str, int] = {}
    titles = {ad.external_id: ad.title for ad in worker.repository.rows(Ad, business.id)}
    for decision in decisions:
        if decision.action == Action.NOTIFY:
            worker.notice(business, decision.reason_code, explain_decision(decision), target_id=decision.target_id)
            continue
        group_id = ad_map[decision.target_id].group_id if decision.action == Action.PAUSE else decision.target_id
        group = next(item for item in local_groups if item.external_id == group_id)
        if group.id in testing:
            continue
        campaign = next(item for item in campaigns if item.id == group.campaign_id)
        if decision.action == Action.PAUSE:
            state = (await worker.resolve_adapter(business.id, campaign.platform).get_state(worker.refs(campaign)))[
                decision.target_id
            ]
            await worker.apply(
                business,
                campaign,
                [Operation("set_status", "ad", decision.target_id, "status", state.status, "paused_auto")],
                reason_code=decision.reason_code,
                reason_params={**decision.numbers, "ad": titles[decision.target_id]},
                kind="pause",
                suffix=decision.target_id,
            )
        elif decision.action == Action.SCALE:
            before = limits.copy()
            scaled = limits.copy()
            increment = decision.new_daily_limit - limits[group_id]
            # Сначала забираем средства у полностью остановленных групп; сумма лимитов не растёт. Частично
            # остановленная группа не донорит: лимит общий на группу, работающие объявления тратят его
            # целиком (иначе SCALE снимал бы деньги с группы победителя A/B). У группы из другой кампании
            # берём только то, что выше минимума площадки для её кампании: иначе, когда группа снова
            # заработает, её кампания встанет на паузу из-за минимума.
            for donor in groups:
                donor_group = next(item for item in local_groups if item.external_id == donor.id)
                if donor_group.id in testing or increment <= 0 or donor.id == group_id:
                    continue
                if donor.active:
                    continue
                spare = scaled[donor.id]
                if donor_group.campaign_id != campaign.id:
                    if donor.platform not in floors:
                        floors[donor.platform] = ceil(
                            daily_floor(await platform_minimums(worker, business, donor.platform), MINIMUM_MARGIN)
                        )
                    kept = sum(
                        scaled[item.external_id]
                        for item in local_groups
                        if item.campaign_id == donor_group.campaign_id and item.external_id in scaled
                    )
                    spare = min(spare, max(0, kept - floors[donor.platform]))
                take = min(increment, spare)
                scaled[donor.id] -= take
                scaled[group_id] += take
                increment -= take
            # Лимит кампании — сумма лимитов её действующих групп: та же база, что у rebalance_budget,
            # иначе перенос искажал лимит кампании (Н6). Остановленные группы-доноры в лимиты кампаний
            # не входили, их кампании не теряют.
            own = {item.external_id for item in local_groups if item.campaign_id == campaign.id}
            active = [item.id for item in groups if item.id in own and item.active]
            share_before = sum(before[key] for key in active)
            share_after = sum(scaled[key] for key in active)
            if share_after == share_before or not share_before:
                continue
            if lifetime is None:
                # Кабинет отключённой площадки спросить нечем: расход нужен только кампании растущей группы.
                closed = closed_platforms(worker, business)
                _, lifetime = await read_spend(
                    worker, business, campaigns=[item for item in campaigns if item.platform not in closed]
                )
            state = (await worker.resolve_adapter(business.id, campaign.platform).get_state(worker.refs(campaign)))[
                campaign.external_id
            ]
            spent = lifetime.get(campaign.external_id, 0)
            daily = state.daily_limit_kop * share_after // share_before
            # VK не хранит лимит периода: на площадку уходит только дневной лимит (месяц закрывает страж бюджета).
            budget_fields = [("daily_limit_kop", state.daily_limit_kop, daily)]
            if campaign.platform != "vk":
                cap = spent + max(state.period_cap_kop - spent, 0) * share_after // share_before
                budget_fields.insert(0, ("period_cap_kop", state.period_cap_kop, cap))
            await worker.apply(
                business,
                campaign,
                [
                    Operation("set_budget", "campaign", campaign.external_id, field, value, after)
                    for field, value, after in budget_fields
                    if value != after
                ],
                kind="budget",
                reason_code=decision.reason_code,
                reason_params={**decision.numbers, "group": group.theme, "campaign": campaign.name},
                suffix=group_id,
                entity_type="group",
                entity_id=group_id,
                evidence={
                    "group_limits": {
                        "before": {key: before[key] for key in scaled if scaled[key] != before[key]},
                        "after": {key: scaled[key] for key in scaled if scaled[key] != before[key]},
                    }
                },
            )
            if not worker.proposed(business):
                limits = scaled
    if not worker.proposed(business):
        worker.remember(business, group_limits=limits)


async def below_platform_minimum(
    worker: Worker,
    business: Business,
    campaign: Campaign,
    operations: list[Operation],
    daily: int,
    cap: int,
    limits: dict[str, int],
) -> int | None:
    """Минимум площадки, который новые лимиты не набирают: недельный у Директа, дневной у остальных; иначе None.

    Недельный лимит Директа считается так же, как перед Campaigns.update (budget_operations): с расходом этой недели."""
    from app.platforms.direct import DirectAdapter

    adapter = worker.resolve_adapter(business.id, campaign.platform)
    if not isinstance(adapter, DirectAdapter):
        if meets_minimum(PlatformLimit(campaign.platform, cap, daily, cap), limits):
            return None
        return limits.get("daily_limit_kop", limits.get("day", 0))
    if not any(op.kind == "set_budget" for op in operations):
        return None
    minimum = limits.get("weekly_limit_kop", 0)
    _, prepared = await budget_operations(
        worker.repository, business, adapter, worker.refs(campaign), operations, worker.clock.now
    )
    weekly = next(op for op in reversed(prepared) if op.budget_context).budget_context["weekly_limit_kop"]
    return minimum if weekly < minimum else None


async def platform_minimums(worker: Worker, business: Business, platform: str) -> dict[str, int]:
    try:
        return await worker.resolve_adapter(business.id, platform).min_budgets()
    except PlatformError:
        logger.warning("Минимумы площадки %s не прочитаны, берём запасные", platform, exc_info=True)
        return dict(PLATFORM_MINIMUMS.get(platform, {}))


async def rebalance_budget(worker: Worker, business: Business, *, force: bool = False) -> dict[str, dict[str, int]]:
    from app.config import get_domain_settings

    domain = get_domain_settings()
    if not force and (business.status != "running" or worker.metadata(business).get("kill_switch")):
        return {}
    await sync_campaign_status(worker, business)
    day = today(worker.clock)
    month = day.replace(day=1)
    meta = worker.metadata(business)
    # Площадку с отключённым кабинетом спросить нечем: её расход — из BudgetLedger, лимиты её кампаний не меняем.
    closed = closed_platforms(worker, business)
    spent, lifetime = await read_spend(
        worker, business, campaigns=[item for item in worker.campaigns(business) if item.platform not in closed]
    )
    for ledger in worker.repository.rows(BudgetLedger, business.id, month=month):
        spent[ledger.platform] = max(spent.get(ledger.platform, 0), ledger.spent_kop)
    # Расход — по всем кампаниям (read_spend), а лимиты получают только кампании действующего плана.
    campaigns = worker.current_campaigns(business)
    platforms = sorted({campaign.platform for campaign in campaigns})
    if not platforms:
        return {}
    shares = {key: Decimal(value) for key, value in meta.get("shares", {}).items()}
    shares = shares if set(shares) == set(platforms) else platform_shares(platforms, domain)
    ads, groups = await snapshot(worker, business, include_paused_campaigns=True)
    prices = {}
    for platform in platforms:
        metrics = total_metrics(
            tuple(day_row for ad in ads if ad.platform == platform for day_row in settled_days(ad.days, day))
        )
        prices[platform] = metrics.cpl
    previous = datetime.fromisoformat(meta.get("shares_at", worker.clock.now.isoformat()))
    change = meta.get("budget_change", {})
    budget = business.month_budget_kop
    if not force and change.get("effective_on", "") > day.isoformat():
        # Увеличение начнёт работать завтра; снижение ограничивает расход сразу для безопасности.
        budget = min(budget, change["before"])

    def budget_plan(shares: dict[str, Decimal]):
        return plan_budget(
            budget,
            sum(spent.values()),
            day,
            platforms,
            started_on=day.fromisoformat(meta.get("started_on", month.isoformat())),
            shares=shares,
            settings=domain,
        )

    minimums = {
        platform: dict(PLATFORM_MINIMUMS.get(platform, {}))
        if platform in closed
        else await platform_minimums(worker, business, platform)
        for platform in platforms
    }
    local_groups = worker.repository.rows(AdGroup, business.id)
    live = {group.id for group in groups if group.active}
    funded = {group.campaign_id for group in local_groups if group.external_id in live}
    # Перенос между площадками не опускает ни одну кампанию площадки ниже её минимума.
    floors = share_floors(
        sum(limit.daily_limit for limit in budget_plan(shares).limits),
        minimums,
        {
            platform: sum(1 for item in campaigns if item.platform == platform and item.id in funded)
            for platform in platforms
        },
    )
    if not worker.proposed(business):
        # Перенос бюджета между площадками по цене обращения — решение автопилота: в пробном режиме доли не меняются.
        shares = rebalance_platform_shares(shares, prices, (worker.clock.now - previous).days, domain, floors)
    plan = budget_plan(shares)
    group_limits = {group.id: group.daily_limit for group in groups}
    held = set(meta.get("below_minimum", []))
    # Неиспользуемый вес остаётся у остановленных групп: его получают только хорошие через decide().
    for limit in plan.limits:
        platform_groups = [group for group in groups if group.platform == limit.platform]
        weights = {group.id: Decimal(group_limits[group.id]) for group in platform_groups}
        if not any(weights.values()):
            weights = dict.fromkeys(weights, Decimal(1))
        daily = allocate(limit.daily_limit, weights)
        caps = allocate(limit.remaining_cap, weights)
        for campaign in campaigns:
            if campaign.platform != limit.platform or campaign.platform in closed:
                continue
            own = {group.external_id for group in local_groups if group.campaign_id == campaign.id}
            active = {group.id for group in platform_groups if group.id in own and group.active}
            desired_daily = sum(daily[group_id] for group_id in active)
            desired_daily, total_cap = campaign_limits(
                desired_daily,
                sum(caps[group_id] for group_id in active),
                day,
                lifetime.get(campaign.external_id, 0),
            )
            states = await worker.resolve_adapter(business.id, campaign.platform).get_state(worker.refs(campaign))
            state = states[campaign.external_id]
            # VK не хранит лимит периода: операцию не создаём (None из get_state дал бы операцию с пустым before),
            # внутренний total_cap продолжает считаться для минимума площадки и учёта.
            budget_fields = [("daily_limit_kop", state.daily_limit_kop, desired_daily)]
            if campaign.platform != "vk":
                budget_fields.insert(0, ("period_cap_kop", state.period_cap_kop, total_cap))
            operations = [
                Operation("set_budget", "campaign", campaign.external_id, field, before, after)
                for field, before, after in budget_fields
                if before != after
            ]
            minimum = (
                await below_platform_minimum(
                    worker, business, campaign, operations, desired_daily, total_cap, minimums[campaign.platform]
                )
                if desired_daily
                else None
            )
            if not desired_daily or minimum:
                if campaign.frozen_until is not None and campaign.frozen_until > worker.clock.now:
                    continue
                await worker.apply(
                    business,
                    campaign,
                    [Operation("set_status", "campaign", campaign.external_id, "status", state.status, "paused_auto")]
                    if state.status == "active"
                    else [],
                    reason_code="BUDGET_REBALANCE",
                    kind="pause",
                    actor="system",
                    reason_text=NO_ACTIVE_ADS
                    if not active
                    else "Доступного дневного бюджета нет: кампанию остановили."
                    if not desired_daily
                    else f"До конца недели остаток бюджета меньше минимума площадки ({rubles(minimum)} ₽): "
                    "кампанию остановили. Возобновим, когда бюджета снова хватит."
                    if campaign.platform == "direct"
                    else f"Дневной бюджет кампании меньше минимума площадки ({rubles(minimum)} ₽ в день): "
                    "кампанию остановили. Возобновим, когда бюджета снова хватит.",
                )
                if minimum:
                    held.add(campaign.id)
                else:
                    campaign.daily_limit_kop = 0
                    worker.repository.save(campaign)
                continue
            last = worker.repository.last_status_change(
                business.id, campaign.platform, "campaign", campaign.external_id
            )
            stopped_by_service = (
                last is not None
                and last.kind == "pause"
                and (last.actor == "system" or last.reason_code == "OWNER_STOP" and not meta.get("kill_switch"))
            )
            # Возобновление касается кампаний из held (пауза по минимуму площадки) и остановленных с нулевым дневным
            # лимитом: те в held не входят и прежним условием не возобновлялись никогда — новый месяц с деньгами их
            # не спасал бы. Паузу «показывать нечего» не трогаем: объявления включает владелец.
            zero_pause = not campaign.daily_limit_kop and last is not None and last.reason_text != NO_ACTIVE_ADS
            resume = (
                campaign.status == "paused_auto"
                and not campaign.owner_paused
                and state.status in {"paused_auto", "paused_owner"}
                and stopped_by_service
                and (campaign.id in held or zero_pause)
            )
            if (
                resume
                and campaign.platform != "direct"
                and not meets_minimum(
                    PlatformLimit(campaign.platform, total_cap, desired_daily, total_cap),
                    minimums[campaign.platform],
                    MINIMUM_MARGIN,
                )
            ):
                continue
            if resume:
                # Кампанию останавливал только недостаток бюджета: лимиты снова его набирают.
                operations.append(
                    Operation("set_status", "campaign", campaign.external_id, "status", state.status, "active")
                )
            await worker.apply(
                business,
                campaign,
                operations,
                reason_code="BUDGET_REBALANCE",
                kind="resume" if resume else "budget",
                actor="system",
                reason_text=(
                    "Бюджета снова хватает на минимум площадки: пересчитали лимиты и возобновили кампанию."
                    if campaign.id in held
                    else "Дневного бюджета снова хватает: пересчитали лимиты и возобновили кампанию."
                )
                if resume
                else "Пересчитали лимиты от остатка бюджета и оставшихся дней месяца.",
            )
            held.discard(campaign.id)
            # Лимиты уже применены: сохраняем их даже при ошибке продления кампании.
            campaign.daily_limit_kop, campaign.period_cap_kop = desired_daily, total_cap
            worker.repository.save(campaign)
            adapter = worker.resolve_adapter(business.id, campaign.platform)
            if campaign.platform == "direct" and hasattr(adapter, "extend_end_date"):
                try:
                    await adapter.extend_end_date(campaign.external_id)
                except PlatformError:
                    worker.notice(
                        business,
                        "CAMPAIGN_EXTEND_FAILED",
                        "Не удалось продлить кампанию в Директе. Повторим при следующем пересчёте лимитов.",
                        target_id=campaign.external_id,
                    )
        ledger = worker.repository.find(BudgetLedger, business_id=business.id, platform=limit.platform, month=month)
        ledger = ledger or BudgetLedger(business_id=business.id, platform=limit.platform, month=month, spent_kop=0)
        ledger.allocated_kop = plan.authorized * int(shares[limit.platform] * 1_000_000) // 1_000_000
        ledger.period_cap_kop = int(Decimal(limit.remaining_cap) / CAP_MULTIPLIER)
        ledger.spent_kop = max(ledger.spent_kop, spent.get(limit.platform, 0))
        ledger.spent_at = worker.clock.now
        worker.repository.save(ledger)
    worker.remember(
        business,
        started_on=meta.get("started_on", day.isoformat()),
        shares={key: str(value) for key, value in shares.items()},
        shares_at=worker.clock.now.isoformat()
        if shares != {key: Decimal(value) for key, value in meta.get("shares", {}).items()}
        else previous.isoformat(),
        group_limits=group_limits,
        below_minimum=sorted(held),
    )
    return minimums


def experiment_expire_date(experiment: Experiment) -> date:
    """Первый день после срока проверки. В сам срок вывод ещё выносится по данным — сравнение по дате,
    как в domain/evaluate_experiment, а не по времени старта (иначе тик позже часа старта в последний
    день закрывал бы проверку без вывода). У аудитории срок держится ещё settlement_days: вывод —
    когда площадка уточнит цифры последнего дня (RULES)."""
    extra = DEFAULT_SETTINGS.settlement_days if experiment.metric == "cpl" else 0
    # Сравнение по дате, как в domain/evaluate_experiment, в московском поясе тика (а не в UTC-дате дедлайна).
    return experiment.deadline.astimezone(MOSCOW).date() + timedelta(days=extra)


async def experiments_tick(worker: Worker, business: Business) -> None:
    day = today(worker.clock)
    local_groups = {group.id: group for group in worker.repository.rows(AdGroup, business.id)}
    campaigns = {campaign.id: campaign for campaign in worker.campaigns(business)}
    running = worker.repository.rows(Experiment, business.id, status="running")
    # Завершённая группа остаётся занята до следующего запуска этого шага.
    occupied_groups = {local_groups[item].external_id for item in testing_groups(worker, business)}
    for experiment in running:
        if day > experiment_expire_date(experiment):
            # Дедлайн освобождает группу даже после правок или отката владельца. Вывода по данным нет —
            # в срок его вынесли бы ниже, — поэтому закрываем проверку честным «срок истёк» и останавливаем
            # вариант: остаётся прежний, как при любом другом исходе без победителя.
            result = ExperimentResult(Verdict.NO_DIFFERENCE, "EXPERIMENT_NO_DIFF", 1.0)
            if worker.proposed(business) and any(
                change.evidence.get("experiment_id") == experiment.id
                for change in worker.repository.rows(
                    ChangeLog,
                    business.id,
                    kind="experiment_verdict",
                    reason_code="EXPERIMENT_EXPIRED",
                    status="proposed",
                )
            ):
                continue
            group_row = local_groups[experiment.ad_group_id]
            campaign = campaigns[group_row.campaign_id]
            variant = worker.repository.find(Ad, id=experiment.variant_ad_id)
            operations = (
                [Operation("set_status", "ad", variant.external_id, "status", "active", "paused_auto")]
                if variant is not None
                and variant.external_id
                and variant.status == "active"
                and (variant.frozen_until is None or variant.frozen_until <= worker.clock.now)
                else []
            )
            text = "Срок проверки истёк. Победителя не выбирали, вариант остановили: по этим данным лучший не виден."
            if operations:
                if business.status != "running":
                    # Кабинет отключён или реклама остановлена: платформу не тронуть (apply упал бы на
                    # подключении кабинета и ронял бы весь тик), проверку закрываем на следующем тике.
                    continue
                try:
                    change = await worker.apply(
                        business,
                        campaign,
                        operations,
                        kind="experiment_verdict",
                        reason_code="EXPERIMENT_EXPIRED",
                        reason_text=text,
                        suffix=f"experiment:{experiment.id}",
                        evidence={"experiment_id": experiment.id, "result": asdict(result)},
                    )
                except (PlatformError, ChangeConflict):
                    # Площадка не приняла операцию (сеть, закрытый кабинет) или слот занят конфликтующим
                    # изменением: вывод не фиксируем, повторим на следующем тике (В2 круга 3).
                    continue
                if change is None or change.status == "failed":
                    # Операция не прошла — оставляем проверку запущенной, тик повторит попытку.
                    continue
                if worker.proposed(business):
                    change.status = "proposed"
                    worker.repository.save(change)
            else:
                text = "Срок проверки истёк. Победителя не выбирали: по этим данным лучший не виден."
                change = ChangeLog(
                    business_id=business.id,
                    actor="autopilot",
                    kind="experiment_verdict",
                    platform=campaign.platform,
                    entity_type="group",
                    entity_id=group_row.external_id,
                    operations=[],
                    reason_code="EXPERIMENT_EXPIRED",
                    reason_params={},
                    reason_text=text,
                    evidence={"experiment_id": experiment.id, "result": asdict(result)},
                    status="proposed" if worker.proposed(business) else "applied",
                    created_at=worker.clock.now,
                )
                worker.repository.save(change)
            if not worker.proposed(business):
                experiment.status = "concluded"
                experiment.verdict = "no_difference"
                experiment.stats = experiment.stats | {"result": asdict(result)}
                experiment.change_id = change.id
                worker.repository.save(experiment)
    if business.status != "running" or worker.metadata(business).get("kill_switch"):
        return
    ads, groups = await snapshot(worker, business)
    by_id = {ad.id: ad for ad in ads}
    local_ads = {ad.id: ad for ad in worker.repository.rows(Ad, business.id)}
    for experiment in running:
        if day > experiment_expire_date(experiment):
            continue
        control_id, variant_id = (
            local_ads[ad_id].external_id for ad_id in (experiment.control_ad_id, experiment.variant_ad_id)
        )
        if control_id not in by_id or variant_id not in by_id:
            continue
        if not group_available(worker, business, local_groups[experiment.ad_group_id]):
            continue
        design = ExperimentDesign(experiment.planned_n_per_arm, 14, 1, experiment.hypothesis, True, "EXPERIMENT_START")
        control, variant = by_id[control_id], by_id[variant_id]
        # История до старта и последние неустоявшиеся дни не входят ни в вердикт,
        # ни в его объяснение и сохранённые доказательства.
        end = today(worker.clock) - timedelta(days=2)
        started = experiment.started_at.astimezone(MOSCOW).date()
        control_days = tuple(day for day in control.days if started <= day.day <= end)
        variant_days = tuple(day for day in variant.days if started <= day.day <= end)
        audience = experiment.metric == "cpl"
        result = (
            evaluate_audience(control_days, variant_days, started, today(worker.clock))
            if audience
            else evaluate_experiment(
                design,
                control_days,
                variant_days,
                started,
                today(worker.clock),
                extended=experiment.stats.get("extended", False),
            )
        )
        if result.verdict == Verdict.RUNNING:
            continue
        if result.verdict == Verdict.EXTEND:
            if not worker.proposed(business):
                experiment.stats = experiment.stats | {"extended": True}
                experiment.deadline = experiment.started_at + timedelta(days=21)
                worker.repository.save(experiment)
            worker.notice(
                business,
                result.reason_code,
                explain_experiment(result, total_metrics(control.days), total_metrics(variant.days)),
            )
            continue
        loser = control_id if result.verdict == Verdict.VARIANT else variant_id
        campaign = campaigns[local_groups[experiment.ad_group_id].campaign_id]
        control_metrics, variant_metrics = total_metrics(control_days), total_metrics(variant_days)
        text = (
            explain_audience(
                result,
                control_metrics,
                variant_metrics,
                experiment.stats["audiences"],
                overlap=experiment.stats.get("overlap", False),
            )
            if audience
            else explain_experiment(result, control_metrics, variant_metrics)
        )
        # Готовый текст содержит все параметры эксперимента и сохраняется единым откатываемым изменением.
        change = await worker.apply(
            business,
            campaign,
            [Operation("set_status", "ad", loser, "status", "active", "paused_auto")],
            kind="experiment_verdict",
            reason_code="EXPERIMENT_VERDICT",
            reason_text=text,
            suffix=f"experiment:{experiment.id}",
        )
        evidence = {
            "experiment_id": experiment.id,
            "control": asdict(control_metrics),
            "variant": asdict(variant_metrics),
            "result": asdict(result),
        }
        change.evidence = change.evidence | evidence
        if worker.proposed(business):
            change.status = "proposed"
        worker.repository.save(change)
        if not worker.proposed(business):
            experiment.status = "concluded"
            experiment.verdict = (
                "variant_wins"
                if result.verdict == Verdict.VARIANT
                else ("control_wins" if result.verdict == Verdict.CONTROL else "no_difference")
            )
            experiment.stats = experiment.stats | {"result": asdict(result)}
            experiment.change_id = change.id
            worker.repository.save(experiment)
    now = worker.clock.now.astimezone(MOSCOW)
    for ad in select_borderline(ads, now, reference(worker, business, ads), tuple(occupied_groups)):
        group = next(item for item in groups if item.id == ad.group_id)
        days = settled_days(ad.days, today(worker.clock))
        metrics = total_metrics(days)
        local_group = next(item for item in local_groups.values() if item.external_id == group.id)
        completed = completed_hypotheses(worker, business, local_group.id)
        design = design_experiment(
            float(metrics.ctr),
            metrics.impressions // max(1, len(days)),
            group.daily_limit,
            completed,
            experiment_settings(),
        )
        if not design.accepted:
            worker.notice(
                business,
                design.reason_code,
                "Пока недостаточно данных или бюджета для проверки.",
                target_id=ad.id,
                days=design.estimated_days,
            )
        elif worker.experiment_factory:
            # Создание нового креатива и его проверка принадлежат контентному сервису, не воркеру.
            # Фабрика получает фиксированный дизайн и обязана идемпотентно подготовить вариант.
            await worker.experiment_factory(worker, business, ad, design)
        else:
            worker.notice(
                business,
                "EXPERIMENT_NEEDS_VARIANT",
                "Для проверки нужен новый проверенный вариант.",
                target_id=ad.id,
                design=asdict(design),
            )
