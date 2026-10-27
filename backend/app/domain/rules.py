from collections import Counter
from collections.abc import Sequence
from datetime import date, datetime, timedelta
from decimal import Decimal
from statistics import median

from app.domain.settings import DEFAULT_SETTINGS, DomainSettings
from app.domain.stats import wilson_interval
from app.domain.types import (
    Action,
    AdSnapshot,
    Assessment,
    Classification,
    ClientAnswer,
    DaySpend,
    Decision,
    GroupSnapshot,
    nonnegative_int,
    total_metrics,
)


def settled_days(
    days: Sequence[DaySpend], today: date, settings: DomainSettings = DEFAULT_SETTINGS
) -> tuple[DaySpend, ...]:
    end = today - timedelta(days=settings.settlement_days)
    start = end - timedelta(days=settings.decision_window_days - 1)
    return tuple(day for day in days if start <= day.day <= end)


def business_history(ads: Sequence[AdSnapshot]) -> tuple[DaySpend, ...]:
    by_day = {}
    for ad in ads:
        for day in ad.days:
            by_day.setdefault(day.day, []).append(day)
    return tuple(
        DaySpend(
            day,
            **{
                name: sum(getattr(item, name) for item in by_day[day])
                for name in ("spend", "impressions", "clicks", "leads")
            },
        )
        for day in sorted(by_day)
    )


def client_conversion(answers: Sequence[ClientAnswer], settings: DomainSettings = DEFAULT_SETTINGS) -> Decimal:
    known = [answer for answer in answers if answer.clients is not None]
    if not known:
        return settings.initial_lead_to_client
    return Decimal(sum(answer.clients for answer in known) + settings.prior_clients) / (
        sum(answer.leads for answer in known) + settings.prior_leads
    )


def client_cac(answers: Sequence[ClientAnswer], today: date, settings: DomainSettings = DEFAULT_SETTINGS) -> int | None:
    start = today - timedelta(weeks=settings.client_window_weeks)
    window = [answer for answer in answers if start < answer.week_end <= today]
    if not window or any(answer.clients is None for answer in window):
        return None
    clients = sum(answer.clients for answer in window)
    return sum(answer.spend for answer in window) // clients if clients else None


def ref_cpl(
    avg_check: int | None,
    ads: Sequence[AdSnapshot],
    today: date,
    answers: Sequence[ClientAnswer] = (),
    settings: DomainSettings = DEFAULT_SETTINGS,
) -> int | None:
    if avg_check is not None:
        nonnegative_int(avg_check, "avg_check")
        known = [answer for answer in answers if answer.week_end <= today]
        return int(avg_check * client_conversion(known, settings) * settings.check_ad_share)
    totals = [total_metrics(settled_days(ad.days, today, settings)) for ad in ads]
    prices = [Decimal(item.spend) / item.leads for item in totals if item.leads]
    return int(median(prices)) if len(prices) >= settings.min_reference_units else None


def has_anomaly(days: Sequence[DaySpend], today: date, settings: DomainSettings = DEFAULT_SETTINGS) -> bool:
    stable = settled_days(days, today, settings)
    by_day = {day.day: day for day in stable}
    if len(by_day) != len(stable):
        raise ValueError("Business history must contain one aggregate per day")
    end = today - timedelta(days=settings.settlement_days)
    recent = [by_day.get(end - timedelta(days=offset)) for offset in range(settings.anomaly_days)]
    if any(day is None or day.leads > 0 for day in recent):
        return False
    previous = [day for day in stable if day.day <= end - timedelta(days=settings.anomaly_days)]
    if not previous:
        return False
    span = (end - timedelta(days=settings.anomaly_days) - min(day.day for day in previous)).days + 1
    return Decimal(sum(day.leads for day in previous)) / span >= settings.anomaly_previous_leads_per_day


def can_change(item: AdSnapshot | GroupSnapshot, now: datetime, settings: DomainSettings = DEFAULT_SETTINGS) -> bool:
    return not (
        (item.frozen_until is not None and now < item.frozen_until)
        or (item.last_changed_at is not None and now - item.last_changed_at < timedelta(hours=settings.cooldown_hours))
        or (
            item.owner_edited_at is not None
            and now - item.owner_edited_at < timedelta(hours=settings.owner_cooldown_hours)
        )
    )


def can_reconsider(ad: AdSnapshot, now: datetime, settings: DomainSettings = DEFAULT_SETTINGS) -> bool:
    return (
        not ad.active
        and ad.paused_by_autopilot_at is not None
        and now - ad.paused_by_autopilot_at >= timedelta(days=settings.reconsider_days)
        and can_change(ad, now, settings)
    )


def classify(
    ad: AdSnapshot,
    now: datetime,
    reference_cpl: int | None,
    group_median_ctr: Decimal | None = None,
    business_had_leads: bool = False,
    price_frozen: bool = False,
    settings: DomainSettings = DEFAULT_SETTINGS,
) -> Assessment:
    metrics = total_metrics(settled_days(ad.days, now.date(), settings))
    if reference_cpl is not None:
        nonnegative_int(reference_cpl, "reference_cpl")
    if ad.approved_at is None or now < ad.approved_at or not ad.active:
        return Assessment(Classification.INSUFFICIENT, metrics)
    age = now - ad.approved_at
    if (
        age >= timedelta(hours=settings.not_showing_hours)
        and not ad.lifetime_impressions
        and not any(day.impressions for day in ad.days)
    ):
        return Assessment(Classification.NOT_SHOWING, metrics, "NOT_SHOWING")
    if not price_frozen and business_had_leads and metrics.clicks >= settings.no_leads_clicks and metrics.leads == 0:
        return Assessment(Classification.BAD, metrics, "PAUSE_NO_LEADS")
    if (
        metrics.impressions >= settings.min_ctr_impressions
        and group_median_ctr is not None
        and metrics.ctr < group_median_ctr * settings.low_ctr_group_ratio
        and metrics.ctr < settings.low_ctr_absolute
    ):
        return Assessment(Classification.LOW_CTR, metrics, "PAUSE_LOW_CTR")
    if age < timedelta(days=settings.warmup_days):
        return Assessment(Classification.INSUFFICIENT, metrics)
    if price_frozen or reference_cpl is None or metrics.clicks < settings.min_price_clicks:
        return Assessment(Classification.INSUFFICIENT, metrics)
    lower, upper = wilson_interval(metrics.leads, metrics.clicks, settings.confidence)
    cpc = Decimal(metrics.spend) / metrics.clicks
    if metrics.leads >= settings.min_good_leads and lower > 0 and cpc / Decimal(str(lower)) <= reference_cpl:
        return Assessment(Classification.GOOD, metrics, "SCALE_UP")
    if (
        upper > 0
        and cpc / Decimal(str(upper)) > reference_cpl * settings.bad_cpl_multiplier
        and metrics.spend >= reference_cpl * settings.bad_spend_multiplier
    ):
        return Assessment(Classification.BAD, metrics, "PAUSE_TOO_EXPENSIVE")
    if metrics.spend >= reference_cpl * settings.borderline_spend_multiplier:
        return Assessment(Classification.BORDERLINE, metrics)
    return Assessment(Classification.INSUFFICIENT, metrics)


def decide(
    ads: Sequence[AdSnapshot],
    groups: Sequence[GroupSnapshot],
    now: datetime,
    reference_cpl: int | None,
    business_days: Sequence[DaySpend] = (),
    free_daily_budget: int = 0,
    settings: DomainSettings = DEFAULT_SETTINGS,
) -> tuple[Decision, ...]:
    """free_daily_budget is unassigned capacity already released from platform caps."""
    nonnegative_int(free_daily_budget, "free_daily_budget")
    group_map = {group.id: group for group in groups}
    if len(group_map) != len(groups) or len({ad.id for ad in ads}) != len(ads):
        raise ValueError("Snapshot IDs must be unique")
    if any(ad.group_id not in group_map or ad.platform != group_map[ad.group_id].platform for ad in ads):
        raise ValueError("Each ad must belong to a supplied group on the same platform")
    business_days = business_days or business_history(ads)
    frozen = has_anomaly(business_days, now.date(), settings)
    had_leads = any(day.leads for day in business_days) or any(day.leads for ad in ads for day in ad.days)
    active_ads = Counter(ad.group_id for ad in ads if ad.active and group_map[ad.group_id].active)
    active_groups = Counter(group.platform for group in groups if group.active and active_ads[group.id])
    totals = {ad.id: total_metrics(settled_days(ad.days, now.date(), settings)) for ad in ads}
    medians = {
        group.id: median(
            [totals[ad.id].ctr for ad in ads if ad.group_id == group.id and ad.active and totals[ad.id].impressions]
        )
        for group in groups
        if any(ad.group_id == group.id and ad.active and totals[ad.id].impressions for ad in ads)
    }
    decisions = [Decision("business", Action.NOTIFY, "ANOMALY")] if frozen else []
    changes = 0
    good_groups = set()
    for ad in sorted(ads, key=lambda item: item.id):
        group = group_map[ad.group_id]
        if not ad.active or not group.active:
            continue
        assessment = classify(ad, now, reference_cpl, medians.get(ad.group_id), had_leads, frozen, settings)
        if assessment.classification == Classification.GOOD:
            if can_change(ad, now, settings):
                good_groups.add(ad.group_id)
            continue
        if assessment.classification == Classification.NOT_SHOWING:
            decisions.append(Decision(ad.id, Action.NOTIFY, "NOT_SHOWING"))
        if assessment.classification not in (Classification.BAD, Classification.LOW_CTR):
            continue
        if not can_change(ad, now, settings) or not can_change(group, now, settings) or changes >= settings.max_changes:
            continue
        last_ad = active_ads[ad.group_id] == 1
        if last_ad and active_groups[ad.platform] <= settings.min_active_groups:
            decisions.append(Decision(ad.id, Action.NOTIFY, "MIN_ACTIVE_GROUPS"))
            continue
        days = [day.day for day in settled_days(ad.days, now.date(), settings)]
        numbers = {
            "clicks": assessment.metrics.clicks,
            "leads": assessment.metrics.leads,
            "spend": assessment.metrics.spend,
            "ref_cpl": reference_cpl or 0,
            "impressions": assessment.metrics.impressions,
            "period_start": min(days).isoformat(),
            "period_end": max(days).isoformat(),
        }
        decisions.append(Decision(ad.id, Action.PAUSE, assessment.reason_code, numbers=numbers))
        active_ads[ad.group_id] -= 1
        if last_ad:
            active_groups[ad.platform] -= 1
        changes += 1
    total_daily = sum(group.daily_limit for group in groups if group.active) + free_daily_budget
    available = min(free_daily_budget, int(total_daily * settings.max_budget_transfer))
    group_count = sum(active_groups.values())
    equal_share = total_daily // group_count if group_count else 0
    for group_id in sorted(good_groups):
        group = group_map[group_id]
        if changes >= settings.max_changes or available <= 0 or not active_ads[group_id]:
            continue
        if not can_change(group, now, settings):
            continue
        increment = min(
            available,
            int(group.daily_limit * settings.scale_step),
            max(0, int(equal_share * settings.max_equal_share_multiplier) - group.daily_limit),
        )
        if increment <= 0:
            continue
        new_limit = group.daily_limit + increment
        decisions.append(
            Decision(
                group_id,
                Action.SCALE,
                "SCALE_UP",
                "group",
                new_limit,
                {"old_limit": group.daily_limit, "new_limit": new_limit},
            )
        )
        available -= increment
        changes += 1
    eligible_good = [
        group_map[group_id]
        for group_id in good_groups
        if active_ads[group_id] and can_change(group_map[group_id], now, settings)
    ]
    budget_blocked = available == 0 or all(
        group.daily_limit >= int(equal_share * settings.max_equal_share_multiplier) for group in eligible_good
    )
    if (
        eligible_good
        and reference_cpl
        and budget_blocked
        and not any(item.action == Action.SCALE for item in decisions)
    ):
        extra = int(sum(group.daily_limit for group in eligible_good) * settings.scale_step)
        if extra > 0:
            decisions.append(
                Decision(
                    "business",
                    Action.NOTIFY,
                    "BUDGET_OPPORTUNITY",
                    numbers={"extra_budget": extra, "extra_leads": Decimal(extra) / reference_cpl},
                )
            )
    return tuple(decisions)
