from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from dataclasses import asdict, replace
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
from app.platforms.base import Operation, PlatformError, StatRow, lead_count
from app.services.budget_guard import campaign_state, closed_platforms, read_spend
from app.services.changes import REVIEW, ChangeConflict, awaiting_enable
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
    # limits — веса, а не копейки: группе без записи даём средний вес группы той же площадки, иначе запасное
    # значение в копейках смешалось бы с весами в ruble_rates.
    known_weights: dict[str, list[int]] = {}
    for item in groups:
        owner = campaign_map.get(item.campaign_id)
        if owner is not None and item.external_id in limits:
            known_weights.setdefault(owner.platform, []).append(int(limits[item.external_id]))
    group_snapshots = []
    for group in groups:
        campaign = campaign_map.get(group.campaign_id)
        # Кампании, удалённой в кабинете, площадка не отдаёт: её группы не работают.
        if group.external_id not in states or campaign is None or campaign.external_id not in states:
            continue
        siblings = [item for item in groups if item.campaign_id == campaign.id]
        weights = known_weights.get(campaign.platform)
        equal = sum(weights) // len(weights) if weights else campaign.daily_limit_kop // len(siblings)
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


def freed_daily_budget(groups: Sequence[GroupSnapshot]) -> dict[str, int]:
    """Лимит, освободившийся полностью остановленными группами, по площадкам (RULES: деньги берутся
    из освободившегося у той же площадки, между площадками не переходят). Частично остановленная
    группа не донорит: лимит на площадках общий на группу, оставшиеся объявления тратят его
    целиком — иначе после каждого A/B-теста группа с одним победителем «освобождала» бы половину
    лимита и SCALE снимал бы деньги с работающей рекламы."""
    freed: dict[str, int] = {}
    for group in groups:
        if not group.active:
            freed[group.platform] = freed.get(group.platform, 0) + group.daily_limit
    return freed


def ruble_rates(
    groups: Sequence[GroupSnapshot],
    local_groups: Sequence[AdGroup],
    campaigns: Sequence[Campaign],
) -> dict[str, Decimal]:
    """Рубли на единицу веса по площадкам. Доли групп (meta group_limits) хранятся как веса, а не
    рубли: лимит кампании живёт с бюджетом, поэтому рубли переводятся от текущего дневного лимита
    её кампании, а веса пересчитывать нельзя (откат сравнивает group_limits из evidence абсолютно)."""
    campaign_map = {campaign.id: campaign for campaign in campaigns}
    rates: dict[str, Decimal] = {}
    for platform in {group.platform for group in groups}:
        active = [group for group in groups if group.active and group.platform == platform]
        weight = sum(Decimal(group.daily_limit) for group in active)
        campaign_ids = {
            next(item.campaign_id for item in local_groups if item.external_id == group.id) for group in active
        }
        total = sum(campaign_map[campaign_id].daily_limit_kop for campaign_id in campaign_ids)
        rates[platform] = Decimal(total) / weight if weight > 0 and total > 0 else Decimal(1)
    return rates


def in_rubles(groups: Sequence[GroupSnapshot], rates: Mapping[str, Decimal]) -> list[GroupSnapshot]:
    """Сырые веса групп в рубли по курсу площадки (ruble_rates)."""
    return [replace(group, daily_limit=int(group.daily_limit * rates[group.platform])) for group in groups]


def missing_conversion_sources(business: Business) -> set[str]:
    """Площадки без источника конверсий: для них «нет обращений» не останавливает объявления.
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
    local_groups = worker.repository.rows(AdGroup, business.id)
    raw = {group.id: group.daily_limit for group in groups}
    rates = ruble_rates(groups, local_groups, worker.current_campaigns(business))
    platform_by_id = {group.id: group.platform for group in groups}
    groups = in_rubles(groups, rates)
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
            states = await worker.resolve_adapter(business.id, campaign.platform).get_state(worker.refs(campaign))
            state = states.get(decision.target_id)
            if state is None:
                # Объявление удалили в кабинете между снимком и остановкой: пропускаем только его,
                # остальные решения шага продолжаются.
                continue
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
            # Сначала забираем средства у полностью остановленных групп той же площадки; сумма
            # лимитов не растёт. Частично остановленная группа не донорит: лимит общий на группу,
            # работающие объявления тратят его целиком (иначе SCALE снимал бы деньги с группы
            # победителя A/B). У группы из другой кампании берём только то, что выше минимума
            # площадки для её кампании: иначе, когда группа снова заработает, её кампания встанет
            # на паузу из-за минимума.
            for donor in groups:
                donor_group = next(item for item in local_groups if item.external_id == donor.id)
                if donor_group.id in testing or increment <= 0 or donor.id == group_id:
                    continue
                if donor.active or donor.platform != campaign.platform:
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
            state = await campaign_state(worker, business, campaign)
            if state is None:
                continue
            spent = lifetime.get(campaign.external_id, 0)
            daily = state.daily_limit_kop * share_after // share_before
            # VK не хранит лимит периода: на площадку уходит только дневной лимит (месяц закрывает страж бюджета).
            budget_fields = [("daily_limit_kop", state.daily_limit_kop, daily)]
            if campaign.platform != "vk":
                cap = spent + max(state.period_cap_kop - spent, 0) * share_after // share_before
                budget_fields.insert(0, ("period_cap_kop", state.period_cap_kop, cap))
            # В БД и evidence уходят сырые веса: рубли переводятся обратно по курсу площадки, а веса
            # групп без изменений не трогаем — иначе округление дрейфовало бы их (откат сравнивает
            # group_limits из evidence абсолютно).
            changed = [key for key in scaled if scaled[key] != before[key]]
            raw_after = {
                key: raw[key] + round((scaled[key] - before[key]) / rates[platform_by_id[key]]) for key in changed
            }
            keep = {key for key in raw_after if raw_after[key] != raw[key]}
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
                        "before": {key: raw[key] for key in keep},
                        "after": {key: raw_after[key] for key in keep},
                    }
                },
            )
            if not worker.proposed(business):
                limits = scaled
                raw = raw | {key: raw_after[key] for key in keep}
                # Лимиты кампании уже применены: сохраняем их в базе, как rebalance_budget, иначе до
                # ближайшего пересчёта курс ruble_rates оставался бы прежним.
                campaign.daily_limit_kop = daily
                if campaign.platform != "vk":
                    campaign.period_cap_kop = cap
                worker.repository.save(campaign)
    # Курс нужен проверке гипотез (services/experiments), у которой нет снимка площадок.
    stored = {"ruble_rates": {platform: str(rate) for platform, rate in rates.items()}}
    if not worker.proposed(business):
        stored["group_limits"] = raw
    worker.remember(business, **stored)


async def below_platform_minimum(
    worker: Worker,
    business: Business,
    campaign: Campaign,
    operations: list[Operation],
    daily: int,
    cap: int,
    limits: dict[str, int],
    *,
    lifetime: int = 0,
    remaining: int = 0,
    rows: Sequence[StatRow] | None = None,
) -> tuple[int | None, int | None]:
    """Минимум площадки, который новые лимиты не набирают: (минимум, None) — кампанию остановить;
    (None, дневной) — неделю можно добрать до минимума поднятием дневного лимита на её остаток,
    пауза не нужна; (None, None) — лимиты набирают минимум.

    Недельный минимум (Директ): неделя, которую поставят с новой парой лимитов, считается по формуле
    адаптера (direct._context_weekly) — с расходом этой недели, без двойного счёта сегодняшнего дня.
    Предел поднятия — остаток месяца: кэп с запасом 1/1,25 не даст неделе вырасти выше него, поэтому темп
    добираем, а не паузим до понедельника. rows — сырые строки отчёта этого пересчёта (read_spend читает их
    одним запросом на все кампании): с тем же снимком, что и lifetime, без повторного запроса на кампанию."""
    from app.platforms.direct import budget_end

    minimum = limits.get("weekly_limit_kop", 0)
    adapter = worker.resolve_adapter(business.id, campaign.platform)
    if not minimum:
        if meets_minimum(PlatformLimit(campaign.platform, cap, daily, cap), limits):
            return (None, None)
        return (limits.get("daily_limit_kop", limits.get("day", 0)), None)
    if not any(op.kind == "set_budget" for op in operations):
        return (None, None)
    day = today(worker.clock)
    month = day.replace(day=1)
    if rows is None:
        started = date.fromisoformat(business.settings.get("_worker", {}).get("started_on", month.isoformat()))
        rows = await adapter.get_stats(min(month, started), day, "campaign", campaign_ids={campaign.external_id})
    week = day - timedelta(days=day.weekday())
    spent_before_week = sum(row.cost_kop for row in rows if row.date < week)
    spent_week = sum(row.cost_kop for row in rows if row.date >= week)
    spent_today = sum(row.cost_kop for row in rows if row.date == day)
    days_left = (budget_end(day) - day).days + 1
    weekly = min(cap - spent_before_week, spent_week + min(daily * days_left, cap) - spent_today)
    if weekly >= minimum:
        return (None, None)
    # Потолок кэпа при любом темпе: если и он ниже минимума, остаток месяца минимум не покрывает — пауза.
    cap_room = lifetime + int(Decimal(remaining) / CAP_MULTIPLIER) - spent_before_week
    if cap_room < minimum:
        return (minimum, None)
    # Темп добираем до минимума: расходом недели и кэпом. Платформенная неделя не выше дневного лимита,
    # поэтому с запасом требуем дневной и от минимума. Ветки «темп уже достаточен» и «поднятие не набирает
    # минимум» недостижимы: fuzz на 200 000 случаев не нашёл ни одной (0 ложных пауз при cap_room >= минимума).
    needed = -(-(minimum - spent_week + spent_today) // days_left)
    cap_needed = -(-(minimum + spent_before_week - lifetime) // days_left)
    raised = max(needed, cap_needed)
    return (None, raised)


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
    # Строки отчёта идут дальше в проверку минимума площадки: тот же снимок, что и в lifetime, без повторных запросов.
    closed = closed_platforms(worker, business)
    report_rows: dict[str, list[StatRow]] = {}
    spent, lifetime = await read_spend(
        worker,
        business,
        campaigns=[item for item in worker.campaigns(business) if item.platform not in closed],
        report_rows=report_rows,
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
    # Кампания, которую площадка ещё проверяет, считается работающей, как в snapshot — и остановленная нами: лимиты по
    # плану готовы к одобрению и к «Продолжить», а остаток бюджета не раздаётся другим кампаниям, которые потом
    # вместе с ней превысили бы его.
    stopped = set(meta.get("kill_switch_campaigns", []))
    reviewing = {
        item.id
        for item in campaigns
        if item.review_since is not None
        and not item.owner_paused
        and (item.status in {"active", "paused_auto"} or item.id in stopped)
    }
    live = {group.id for group in groups if group.active} | {
        group.external_id for group in local_groups if group.campaign_id in reviewing and group.status == "active"
    }
    funded = {group.campaign_id for group in local_groups if group.external_id in live}
    # Число funded-кампаний площадки: минимум считается на каждую кампанию,
    # поэтому словарь один и у share_floors, и у blocked.
    campaign_counts = {
        platform: sum(1 for item in campaigns if item.platform == platform and item.id in funded)
        for platform in platforms
    }
    # Перенос между площадками не опускает ни одну кампанию площадки ниже её минимума.
    floors = share_floors(
        sum(limit.daily_limit for limit in budget_plan(shares).limits),
        minimums,
        campaign_counts,
    )
    if not worker.proposed(business):
        # Перенос бюджета между площадками по цене обращения — решение автопилота: в пробном режиме доли не меняются.
        shares = rebalance_platform_shares(shares, prices, (worker.clock.now - previous).days, domain, floors)
    plan_full = budget_plan(shares)
    held_before = set(meta.get("below_minimum", []))
    # Платформа, чьи кампании все стоят по минимуму и чья доля при этом не набирает даже этот минимум, не тратит
    # бюджет этот пересчёт: доля уходит платформам, которые работают. Доли в памяти не меняются — отпущенная
    # платформа вернёт свою долю сама, когда бюджета хватит на её минимум (проверка по полному плану ниже).
    # Порог тот же, что у share_floors: разрешённая доля площадки (до деления на 1,25) должна покрывать минимум
    # каждой её funded-кампании с запасом кэпа — иначе площадка получит долю и тут же встанет на паузу.
    blocked = (
        {
            platform
            for platform in platforms
            if all(campaign.id in held_before for campaign in campaigns if campaign.platform == platform)
            and next((limit.daily_limit for limit in plan_full.limits if limit.platform == platform), 0)
            < ceil(daily_floor(minimums[platform], MINIMUM_MARGIN) * CAP_MULTIPLIER * max(1, campaign_counts[platform]))
        }
        if not worker.proposed(business)
        else set()
    )
    previous_released = set(meta.get("share_released", []))
    # plan_released — перенос доли в плане этого пересчёта. Перенос — решение автопилота: в пробном режиме
    # доли не двигаются, но состояние отпускания (released) не пересчитываем — иначе вход в DRY_RUN «вернул»
    # бы долю в журнале и стёр память, а выход снова «отпустил» (ложные пары записей на переключение).
    # Полный стоп всех платформ — не «возврат»: получателя нет, доля просто не переносится, и прежнее
    # состояние сохраняется, чтобы запись о возврате появилась один раз и правдиво, когда бюджета
    # реально хватит на минимум (для VK это и есть момент возобновления).
    if worker.proposed(business):
        plan_released, released = set(), previous_released
    else:
        plan_released = blocked if blocked != set(platforms) else set()
        released = plan_released if plan_released else (previous_released if blocked == set(platforms) else set())
    if plan_released:
        kept = [platform for platform in platforms if platform not in plan_released]
        total = sum(shares[platform] for platform in kept)
        plan_shares = (
            {platform: shares[platform] / total for platform in kept}
            | {platform: Decimal(0) for platform in plan_released}
        )
    else:
        plan_shares = shares
    plan = budget_plan(plan_shares)
    group_limits = {group.id: group.daily_limit for group in groups}
    held = set(meta.get("below_minimum", []))
    # Изменение, которое площадка не подтвердила, не обрывает пересчёт остальных кампаний и продление дат: шаг
    # провалится в конце и повторится.
    conflict = None

    async def extend(campaign: Campaign) -> None:
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
            state = await campaign_state(worker, business, campaign)
            if state is None:
                continue
            own = {group.external_id for group in local_groups if group.campaign_id == campaign.id}
            active = {group.id for group in platform_groups if group.id in own and group.id in live}
            desired_daily = sum(daily[group_id] for group_id in active)
            desired_daily, total_cap = campaign_limits(
                desired_daily,
                sum(caps[group_id] for group_id in active),
                day,
                lifetime.get(campaign.external_id, 0),
            )
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
            below = (
                await below_platform_minimum(
                    worker,
                    business,
                    campaign,
                    operations,
                    desired_daily,
                    total_cap,
                    minimums[campaign.platform],
                    lifetime=lifetime.get(campaign.external_id, 0),
                    remaining=sum(caps[group_id] for group_id in active),
                    rows=report_rows.get(campaign.external_id),
                )
                if desired_daily
                else (None, None)
            )
            minimum, raised_daily = below
            raise_note = None
            if raised_daily:
                # Неделя добирается до минимума площадки поднятием темпа на её остаток: кэп с запасом 1/1,25
                # покрывает минимум, значит месячный бюджет не тронут — пауза до понедельника не нужна.
                authorized = ceil(raised_daily * CAP_MULTIPLIER)
                desired_daily, total_cap = campaign_limits(
                    authorized,
                    sum(caps[group_id] for group_id in active),
                    day,
                    lifetime.get(campaign.external_id, 0),
                )
                budget_fields = [
                    ("period_cap_kop", state.period_cap_kop, total_cap),
                    ("daily_limit_kop", state.daily_limit_kop, desired_daily),
                ]
                operations = [
                    Operation("set_budget", "campaign", campaign.external_id, field, before, after)
                    for field, before, after in budget_fields
                    if before != after
                ]
                minimum = None
                weekly_minimum = rubles(minimums[campaign.platform]["weekly_limit_kop"])
                raise_note = (
                    f"Неделя вышла бы меньше минимума площадки ({weekly_minimum} ₽): "
                    "подняли темп на остаток недели до минимума. "
                    "Месячный бюджет не тронут: остаток пересчитаем завтра."
                )
            if not desired_daily or minimum:
                if campaign.frozen_until is not None and campaign.frozen_until > worker.clock.now:
                    continue
                # Черновик, который уже на паузе по нашей цели, повторно не останавливаем: запись в журнале
                # на каждом пересчёте ничего не меняла бы.
                pause_operations = (
                    [
                        Operation(
                            "set_status", "campaign", campaign.external_id, "status", state.status, "paused_auto"
                        )
                    ]
                    if state.status == "active"
                    or (state.status in REVIEW and campaign.status == "active")
                    or awaiting_enable(campaign, state)
                    else []
                )
                try:
                    await worker.apply(
                        business,
                        campaign,
                        pause_operations,
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
                except ChangeConflict as error:
                    conflict = error
                    await extend(campaign)
                    continue
                if minimum:
                    held.add(campaign.id)
                    # Уведомление — только о паузе, реально применённой в этом пересчёте: уже стоящей кампании
                    # сообщать нечего, а остановленная владельцем стоит по его решению, а не по минимуму.
                    # Пауза по минимуму в пробном режиме настоящая, поэтому уведомление не зависит от proposed.
                    if pause_operations and not campaign.owner_paused and state.status != "paused_owner":
                        limit_text = (
                            f"недельный минимум площадки ({rubles(minimum)} ₽)"
                            if campaign.platform == "direct"
                            else f"дневной минимум площадки ({rubles(minimum)} ₽)"
                        )
                        worker.notice(
                            business,
                            "PLATFORM_MINIMUM_PAUSE",
                            f"Кампания «{campaign.name}» остановлена: при таком бюджете она не набирает "
                            f"{limit_text}. Включим её при пересчёте лимитов; чтобы остановки не повторялись, "
                            "увеличьте бюджет в настройках.",
                            key=f"minimum:{day.isoformat()}:{campaign.id}",
                            target_id=campaign.id,
                        )
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
            # Возобновление касается кампаний из held (пауза по минимуму площадки) и остановленных пересчётом с нулевым
            # дневным лимитом (нет бюджета или нечего показывать): те в held не входят и прежним условием не
            # возобновлялись никогда. Паузу стража бюджета, месяца и kill_switch (другие reason_code) не трогаем.
            zero_pause = (
                not campaign.daily_limit_kop
                and last is not None
                and last.reason_code == "BUDGET_REBALANCE"
                and not meta.get("kill_switch")
            )
            resume = (
                campaign.status == "paused_auto"
                and not campaign.owner_paused
                and state.status in {"paused_auto", "paused_owner", *REVIEW}
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
            # Замороженную кампанию автопилот не включает и не останавливает: решение о статусе за
            # владельцем (откат или включение в кабинете) до конца заморозки. Лимиты пересчитываются
            # как обычно — они не спорят с решением владельца, а запись «по минимуму» остаётся:
            # возобновление придёт на пересчёт после заморозки.
            frozen = campaign.frozen_until is not None and campaign.frozen_until > worker.clock.now
            if resume and frozen:
                resume = False
            if resume:
                # Кампанию останавливал только недостаток бюджета: лимиты снова его набирают.
                operations.append(
                    Operation("set_status", "campaign", campaign.external_id, "status", state.status, "active")
                )
            try:
                await worker.apply(
                    business,
                    campaign,
                    operations,
                    reason_code="BUDGET_REBALANCE",
                    kind="resume" if resume else "budget",
                    actor="system",
                    reason_text=(
                        raise_note
                        or (
                            "Бюджета снова хватает на минимум площадки: пересчитали лимиты и возобновили кампанию."
                            if campaign.id in held
                            else "Снова есть работающие объявления: пересчитали лимиты и возобновили кампанию."
                            if last.reason_text == NO_ACTIVE_ADS
                            else "Дневного бюджета снова хватает: пересчитали лимиты и возобновили кампанию."
                        )
                    )
                    if resume
                    else raise_note or "Пересчитали лимиты от остатка бюджета и оставшихся дней месяца.",
                )
            except ChangeConflict as error:
                conflict = error
                await extend(campaign)
                continue
            if resume:
                held.discard(campaign.id)
            # Лимиты уже применены: сохраняем их даже при ошибке продления кампании.
            campaign.daily_limit_kop, campaign.period_cap_kop = desired_daily, total_cap
            worker.repository.save(campaign)
            await extend(campaign)
        ledger = worker.repository.find(BudgetLedger, business_id=business.id, platform=limit.platform, month=month)
        ledger = ledger or BudgetLedger(business_id=business.id, platform=limit.platform, month=month, spent_kop=0)
        ledger.allocated_kop = plan.authorized * int(plan_shares[limit.platform] * 1_000_000) // 1_000_000
        ledger.period_cap_kop = int(Decimal(limit.remaining_cap) / CAP_MULTIPLIER)
        ledger.spent_kop = max(ledger.spent_kop, spent.get(limit.platform, 0))
        ledger.spent_at = worker.clock.now
        worker.repository.save(ledger)
    # Отпущенная платформа остаётся «по минимуму» в памяти сама: все её кампании уже в held и в этом пересчёте
    # из него не выходят — долю с нулевым лимитом они не набирают и возобновления не получают.
    if released != previous_released:
        from app.services.launch import TITLES

        if released:
            names = " и ".join(TITLES[platform] for platform in sorted(released))
            # Получатель доли — только платформа, которая реально её тратит: kept-платформа с работающей
            # (funded и не по минимуму) кампанией. Кабинет отключён или все кампании стоят — долю в плане она
            # получила, но не тратит: в записи её не называем.
            receivers = [
                platform
                for platform in platforms
                if platform not in released
                and any(
                    campaign.id in funded and campaign.id not in held
                    for campaign in campaigns
                    if campaign.platform == platform
                )
            ]
            if receivers:
                receiver_names = " и ".join(TITLES[platform] for platform in receivers)
                reason_text = (
                    f"{names} не набирает минимум площадки при текущем бюджете: доля её бюджета временно ушла "
                    f"в {receiver_names}. Вернём долю, когда бюджета снова хватит на минимум."
                )
            else:
                reason_text = (
                    f"{names} не набирает минимум площадки при текущем бюджете: её доля в этом пересчёте "
                    "не тратится. Вернём долю, когда бюджета снова хватит на минимум."
                )
        else:
            names = " и ".join(TITLES[platform] for platform in sorted(previous_released))
            reason_text = f"Бюджета снова хватает на минимум площадки {names}: доля бюджета возвращена ей."
        worker.repository.save(
            ChangeLog(
                business_id=business.id,
                actor="system",
                kind="budget",
                platform=None,
                entity_type="business",
                entity_id=str(business.id),
                operations=[],
                reason_code="BUDGET_REBALANCE",
                reason_text=reason_text,
                evidence={},
                status="applied",
                created_at=worker.clock.now,
            )
        )
    worker.remember(
        business,
        started_on=meta.get("started_on", day.isoformat()),
        shares={key: str(value) for key, value in shares.items()},
        shares_at=worker.clock.now.isoformat()
        if shares != {key: Decimal(value) for key, value in meta.get("shares", {}).items()}
        else previous.isoformat(),
        group_limits=group_limits,
        below_minimum=sorted(held),
        share_released=sorted(released),
    )
    if conflict is not None:
        raise conflict
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
                    # изменением: вывод не фиксируем, повторим на следующем тике.
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
    rates = ruble_rates(groups, list(local_groups.values()), worker.current_campaigns(business))
    groups = in_rubles(groups, rates)
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
        if any(ad.active for ad in ads if ad.group_id == control.group_id and ad.id != loser):
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
        else:
            # Победителя с тех пор остановили: проигравший — последнее работающее объявление группы, и его
            # остановка выключила бы всю группу. Вывод записываем, объявления не трогаем.
            change = ChangeLog(
                business_id=business.id,
                actor="autopilot",
                kind="experiment_verdict",
                platform=campaign.platform,
                entity_type="group",
                entity_id=control.group_id,
                operations=[],
                reason_code="EXPERIMENT_VERDICT",
                reason_params={},
                reason_text=f"{text} Проигравший вариант не остановили: других работающих объявлений в группе нет.",
                evidence={},
                status="applied",
                created_at=worker.clock.now,
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
    missing_sources = missing_conversion_sources(business)
    for ad in select_borderline(
        ads, now, reference(worker, business, ads), tuple(occupied_groups), platforms_without_source=missing_sources
    ):
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
