from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING

from app.db.models import Ad, AdGroup, BudgetLedger, Business, ChangeLog, Experiment, StatsDaily
from app.domain.budget import allocate, plan_budget, platform_shares, rebalance_platform_shares
from app.domain.experiments import design_experiment, evaluate_experiment, select_borderline
from app.domain.explain import explain_decision, explain_experiment
from app.domain.rules import decide, ref_cpl, settled_days
from app.domain.types import (
    Action,
    AdSnapshot,
    ClientAnswer,
    DaySpend,
    ExperimentDesign,
    ExperimentResult,
    GroupSnapshot,
    Verdict,
    total_metrics,
)
from app.platforms.base import Operation, PlatformError
from app.services.budget_guard import read_spend
from app.services.experiments import completed_hypotheses, group_available
from app.services.plan import CAP_MULTIPLIER, campaign_limits
from app.worker.clock import today

if TYPE_CHECKING:
    from app.worker.jobs import Worker


async def snapshot(
    worker: Worker, business: Business, *, include_paused_campaigns: bool = False
) -> tuple[list[AdSnapshot], list[GroupSnapshot]]:
    repository = worker.repository
    campaigns = worker.campaigns(business)
    states = {}
    for campaign in campaigns:
        states.update(await worker.resolve_adapter(business.id, campaign.platform).get_state(worker.refs(campaign)))
    campaign_map = {campaign.id: campaign for campaign in campaigns}
    groups = repository.rows(AdGroup, business.id)
    ads = repository.rows(Ad, business.id)
    group_map = {group.id: group for group in groups}
    meta = worker.metadata(business)
    limits = meta.get("group_limits", {})
    history = [change for change in repository.rows(ChangeLog, business.id) if change.status == "applied"]

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
    goals = {str(goal) for goal in business.lead_goal_ids} or {"lead"}
    for ad in ads:
        if ad.external_id not in states:
            continue
        group = group_map[ad.ad_group_id]
        platform = campaign_map[group.campaign_id].platform
        approved = meta.get("approvals", {}).get(ad.external_id)
        days = tuple(
            DaySpend(
                row.date,
                row.cost_kop,
                row.impressions,
                row.clicks,
                min(row.clicks, sum(value for goal, value in row.conversions.items() if goal in goals)),
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
                        and (campaign.status == "paused_auto" or campaign.id in meta.get("kill_switch_campaigns", []))
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

    answers = [
        ClientAnswer(row.week_start + timedelta(days=6), row.leads_count, row.clients_count)
        for row in worker.repository.rows(LeadsSurvey, business.id)
    ]
    return ref_cpl(business.avg_check_kop, ads, today(worker.clock), answers)


async def optimize(worker: Worker, business: Business) -> None:
    from app.config import get_domain_settings

    if business.status != "running" or worker.metadata(business).get("kill_switch"):
        return
    ads, groups = await snapshot(worker, business)
    limits = {group.id: group.daily_limit for group in groups}
    # Освободившаяся доля остаётся доступной, но не доливается в пограничные объявления.
    free = sum(group.daily_limit for group in groups if not group.active)
    decisions = decide(
        ads,
        groups,
        worker.clock.now,
        reference(worker, business, ads),
        free_daily_budget=free,
        settings=get_domain_settings(),
    )
    campaigns = worker.campaigns(business)
    local_groups = worker.repository.rows(AdGroup, business.id)
    testing = {
        item.ad_group_id for item in worker.repository.rows(Experiment, business.id) if item.status == "running"
    }
    ad_map = {ad.id: ad for ad in ads}
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
                reason_params=decision.numbers,
                kind="pause",
                suffix=decision.target_id,
            )
        elif decision.action == Action.SCALE:
            before = limits.copy()
            scaled = limits.copy()
            increment = decision.new_daily_limit - limits[group_id]
            # Сначала забираем средства у остановленных групп; сумма лимитов не растёт.
            for donor in groups:
                donor_group = next(item for item in local_groups if item.external_id == donor.id)
                if donor.active or donor_group.id in testing or increment <= 0:
                    continue
                take = min(increment, scaled[donor.id])
                scaled[donor.id] -= take
                scaled[group_id] += take
                increment -= take
            state = (await worker.resolve_adapter(business.id, campaign.platform).get_state(worker.refs(campaign)))[
                campaign.external_id
            ]
            await worker.apply(
                business,
                campaign,
                [
                    Operation(
                        "set_budget",
                        "campaign",
                        campaign.external_id,
                        "daily_limit_kop",
                        state.daily_limit_kop,
                        campaign.daily_limit_kop,
                    )
                ],
                kind="budget",
                reason_code=decision.reason_code,
                reason_params=decision.numbers,
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


async def rebalance_budget(worker: Worker, business: Business, *, force: bool = False) -> None:
    from app.config import get_domain_settings

    domain = get_domain_settings()
    if not force and (business.status != "running" or worker.metadata(business).get("kill_switch")):
        return
    day = today(worker.clock)
    month = day.replace(day=1)
    meta = worker.metadata(business)
    spent, lifetime = await read_spend(worker, business)
    for ledger in worker.repository.rows(BudgetLedger, business.id):
        if ledger.month == month:
            spent[ledger.platform] = max(spent.get(ledger.platform, 0), ledger.spent_kop)
    campaigns = worker.campaigns(business)
    platforms = sorted({campaign.platform for campaign in campaigns})
    if not platforms:
        return
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
    shares = rebalance_platform_shares(shares, prices, (worker.clock.now - previous).days, domain)
    change = meta.get("budget_change", {})
    budget = business.month_budget_kop
    if not force and change.get("effective_on", "") > day.isoformat():
        # Увеличение начнёт работать завтра; снижение ограничивает расход сразу для безопасности.
        budget = min(budget, change["before"])
    plan = plan_budget(
        budget,
        sum(spent.values()),
        day,
        platforms,
        started_on=day.fromisoformat(meta.get("started_on", month.isoformat())),
        shares=shares,
        settings=domain,
    )
    group_limits = {group.id: group.daily_limit for group in groups}
    local_groups = worker.repository.rows(AdGroup, business.id)
    # Неиспользуемый вес остаётся у остановленных групп: его получают только хорошие через decide().
    for limit in plan.limits:
        platform_groups = [group for group in groups if group.platform == limit.platform]
        weights = {group.id: Decimal(group_limits[group.id]) for group in platform_groups}
        if not any(weights.values()):
            weights = dict.fromkeys(weights, Decimal(1))
        daily = allocate(limit.daily_limit, weights)
        caps = allocate(limit.remaining_cap, weights)
        for campaign in campaigns:
            if campaign.platform != limit.platform:
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
            if not desired_daily:
                await worker.apply(
                    business,
                    campaign,
                    [Operation("set_status", "campaign", campaign.external_id, "status", state.status, "paused_auto")]
                    if state.status == "active"
                    else [],
                    reason_code="BUDGET_REBALANCE",
                    kind="pause",
                    actor="system",
                    reason_text="Доступного дневного бюджета нет: кампанию остановили.",
                )
                if not worker.proposed(business):
                    campaign.daily_limit_kop = 0
                    worker.repository.save(campaign)
                continue
            operations = [
                Operation("set_budget", "campaign", campaign.external_id, field, before, after)
                for field, before, after in (
                    ("period_cap_kop", state.period_cap_kop, total_cap),
                    ("daily_limit_kop", state.daily_limit_kop, desired_daily),
                )
                if before != after
            ]
            await worker.apply(
                business,
                campaign,
                operations,
                reason_code="BUDGET_REBALANCE",
                kind="budget",
                actor="system",
                reason_text="Пересчитали лимиты от остатка бюджета и оставшихся дней месяца.",
            )
            if not worker.proposed(business):
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
    if not worker.proposed(business):
        worker.remember(
            business,
            started_on=meta.get("started_on", day.isoformat()),
            shares={key: str(value) for key, value in shares.items()},
            shares_at=worker.clock.now.isoformat()
            if shares != {key: Decimal(value) for key, value in meta.get("shares", {}).items()}
            else previous.isoformat(),
            group_limits=group_limits,
        )


async def experiments_tick(worker: Worker, business: Business) -> None:
    local_groups = {group.id: group for group in worker.repository.rows(AdGroup, business.id)}
    campaigns = {campaign.id: campaign for campaign in worker.campaigns(business)}
    running = [item for item in worker.repository.rows(Experiment, business.id) if item.status == "running"]
    # Завершённая группа остаётся занята до следующего запуска этого шага.
    occupied_groups = {local_groups[item.ad_group_id].external_id for item in running}
    for experiment in running:
        if worker.clock.now >= experiment.deadline:
            # Дедлайн освобождает группу даже после правок или отката владельца.
            # Просроченные данные не дают права выбирать победителя и менять статусы плеч.
            result = ExperimentResult(Verdict.NO_DIFFERENCE, "EXPERIMENT_NO_DIFF", 1.0)
            if worker.proposed(business) and any(
                change.kind == "experiment_verdict"
                and change.reason_code == "EXPERIMENT_NO_DIFF"
                and change.status == "proposed"
                and change.evidence.get("experiment_id") == experiment.id
                for change in worker.repository.rows(ChangeLog, business.id)
            ):
                continue
            change = ChangeLog(
                business_id=business.id,
                actor="autopilot",
                kind="experiment_verdict",
                platform=campaigns[local_groups[experiment.ad_group_id].campaign_id].platform,
                entity_type="group",
                entity_id=local_groups[experiment.ad_group_id].external_id,
                operations=[],
                reason_code="EXPERIMENT_NO_DIFF",
                reason_params={},
                reason_text="Срок проверки истёк. Победителя не выбирали.",
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
        if worker.clock.now >= experiment.deadline:
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
        control_days = tuple(day for day in control.days if experiment.started_at.date() <= day.day <= end)
        variant_days = tuple(day for day in variant.days if experiment.started_at.date() <= day.day <= end)
        result = evaluate_experiment(
            design,
            control_days,
            variant_days,
            experiment.started_at.date(),
            today(worker.clock),
            extended=experiment.stats.get("extended", False),
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
        text = explain_experiment(result, control_metrics, variant_metrics)
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
    for ad in select_borderline(ads, worker.clock.now, reference(worker, business, ads), tuple(occupied_groups)):
        group = next(item for item in groups if item.id == ad.group_id)
        days = settled_days(ad.days, today(worker.clock))
        metrics = total_metrics(days)
        local_group = next(item for item in local_groups.values() if item.external_id == group.id)
        completed = completed_hypotheses(worker, business, local_group.id)
        design = design_experiment(
            float(metrics.ctr), metrics.impressions // max(1, len(days)), group.daily_limit, completed
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
