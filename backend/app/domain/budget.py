from calendar import monthrange
from collections.abc import Mapping, Sequence
from datetime import date
from decimal import ROUND_CEILING, Decimal

from app.domain.settings import DEFAULT_SETTINGS, DomainSettings
from app.domain.types import BudgetPlan, GroupLimit, GuardResult, PlatformLimit, nonnegative_int


def allocate(amount: int, weights: Mapping[str, Decimal]) -> dict[str, int]:
    """Split kopecks by largest remainder; ties follow the key order."""
    nonnegative_int(amount, "amount")
    if any(not isinstance(weight, (Decimal, int)) or weight < 0 for weight in weights.values()):
        raise ValueError("Weights must be nonnegative Decimal or int values")
    total = sum(weights.values(), Decimal(0))
    if not total:
        return dict.fromkeys(weights, 0)
    exact = {key: Decimal(amount) * weight / total for key, weight in weights.items()}
    result = {key: int(value) for key, value in exact.items()}
    remainder_order = sorted(weights, key=lambda key: exact[key] - result[key], reverse=True)
    for key in remainder_order[: amount - sum(result.values())]:
        result[key] += 1
    return result


def authorized_budget(
    monthly_budget: int,
    active_days: int,
    days_in_month: int,
    settings: DomainSettings = DEFAULT_SETTINGS,
) -> int:
    nonnegative_int(monthly_budget, "monthly_budget")
    if not 0 < active_days <= days_in_month:
        raise ValueError("Expected 0 < active_days <= days_in_month")
    return int(Decimal(monthly_budget) * active_days / days_in_month * (1 - settings.budget_reserve))


def platform_shares(platforms: Sequence[str], settings: DomainSettings = DEFAULT_SETTINGS) -> dict[str, Decimal]:
    if len(set(platforms)) != len(platforms):
        raise ValueError("Platforms must be unique")
    if len(platforms) == 1:
        return {platforms[0]: Decimal(1)}
    if not platforms:
        return {}
    if set(platforms) != {"direct", "vk"}:
        raise ValueError("Expected direct and vk")
    return {
        platform: settings.direct_share if platform == "direct" else 1 - settings.direct_share for platform in platforms
    }


def rebalance_platform_shares(
    current: Mapping[str, Decimal],
    cpl: Mapping[str, int | None],
    days_since_rebalance: int,
    settings: DomainSettings = DEFAULT_SETTINGS,
) -> dict[str, Decimal]:
    if any(value < 0 for value in current.values()) or sum(current.values()) != 1:
        raise ValueError("Platform shares must sum to 1")
    if len(current) != 2 or days_since_rebalance < settings.platform_rebalance_days:
        return dict(current)
    first, second = current
    a, b = cpl.get(first), cpl.get(second)
    if a is None or b is None or a <= 0 or b <= 0:
        return dict(current)
    desired = Decimal(b) / (a + b)
    step = settings.platform_share_step
    share = min(current[first] + step, max(current[first] - step, desired))
    share = min(Decimal(1), max(Decimal(0), share))
    return {first: share, second: 1 - share}


def plan_budget(
    monthly_budget: int,
    spent: int,
    today: date,
    platforms: Sequence[str] = ("direct", "vk"),
    started_on: date | None = None,
    shares: Mapping[str, Decimal] | None = None,
    settings: DomainSettings = DEFAULT_SETTINGS,
) -> BudgetPlan:
    """Caps are additional spend allowed now, not lifetime platform budget values."""
    nonnegative_int(spent, "spent")
    month_days = monthrange(today.year, today.month)[1]
    month_start = today.replace(day=1)
    start = max(month_start, started_on or month_start)
    if start > today:
        raise ValueError("Cannot plan before launch")
    active_days = month_days - start.day + 1
    allowed = authorized_budget(monthly_budget, active_days, month_days, settings)
    remaining = max(0, allowed - spent)
    days_left = month_days - today.day + 1
    daily = min(remaining // days_left, int(Decimal(allowed) / active_days * settings.daily_average_multiplier))
    weights = platform_shares(platforms, settings) if shares is None else dict(shares)
    if set(weights) != set(platforms) or len(set(platforms)) != len(platforms):
        raise ValueError("Shares must match connected platforms")
    if weights and sum(weights.values()) != 1:
        raise ValueError("Platform shares must sum to 1")
    caps = allocate(remaining, weights)
    daily_limits = allocate(daily, weights)
    week_days = min(7 - today.weekday(), days_left)
    limits = tuple(
        PlatformLimit(
            platform, caps[platform], daily_limits[platform], min(caps[platform], daily_limits[platform] * week_days)
        )
        for platform in platforms
    )
    return BudgetPlan(allowed, spent, remaining, limits)


def stop_margin(total_daily_limit: int, settings: DomainSettings = DEFAULT_SETTINGS) -> int:
    nonnegative_int(total_daily_limit, "total_daily_limit")
    amount = (
        total_daily_limit
        * settings.guard_spend_multiplier
        * settings.guard_poll_minutes
        * settings.guard_missed_polls
        / (24 * 60)
    )
    return int(amount.to_integral_value(rounding=ROUND_CEILING))


def split_group_limits(limit: PlatformLimit, weights: Mapping[str, Decimal]) -> tuple[GroupLimit, ...]:
    """Pass equal weights at launch, updated weights after decisions; zero excludes a group."""
    caps = allocate(limit.remaining_cap, weights)
    daily = allocate(limit.daily_limit, weights)
    weekly = allocate(limit.weekly_limit, weights)
    return tuple(
        GroupLimit(group_id, limit.platform, cap, min(daily[group_id], cap), min(weekly[group_id], cap))
        for group_id, cap in caps.items()
    )


def budget_guard(
    monthly_budget: int,
    reported_spent: int,
    total_daily_limit: int,
    recorded_spent: int | None = None,
    settings: DomainSettings = DEFAULT_SETTINGS,
) -> GuardResult:
    nonnegative_int(monthly_budget, "monthly_budget")
    nonnegative_int(reported_spent, "reported_spent")
    if recorded_spent is not None:
        nonnegative_int(recorded_spent, "recorded_spent")
    spent = max(reported_spent, recorded_spent or 0)
    margin = stop_margin(total_daily_limit, settings)
    if recorded_spent is not None and reported_spent > recorded_spent:
        return GuardResult(True, "SPEND_MISMATCH", margin, spent)
    if spent >= max(0, monthly_budget - margin):
        return GuardResult(True, "BUDGET_STOP", margin, spent)
    return GuardResult(False, None, margin, spent)
