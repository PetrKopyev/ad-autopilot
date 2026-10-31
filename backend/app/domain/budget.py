from calendar import monthrange
from collections.abc import Callable, Iterable, Mapping, Sequence
from datetime import date, timedelta
from decimal import ROUND_CEILING, Decimal
from typing import Any

from app.domain.settings import DEFAULT_SETTINGS, DomainSettings
from app.domain.types import BudgetPlan, GroupLimit, GuardResult, PlatformLimit, nonnegative_int

CAP_MULTIPLIER = Decimal("1.25")
# Минимумы площадок для расчёта без сети: Директ — недельный бюджет стратегии, VK — дневной и общий лимит кампании.
# Запуск сверяет лимиты с минимумами, которые площадка отдаёт живьём (min_budgets адаптера). Суммы — с НДС, как бюджет:
# у Директа 300 ₽ без НДС (DIRECT_AMOUNTS_INCLUDE_VAT=false) это 366 ₽ с НДС 22%.
PLATFORM_MINIMUMS = {
    "direct": {"weekly_limit_kop": 36_600},
    "vk": {"daily_limit_kop": 30_000, "period_cap_kop": 30_000},
}
MINIMUM_MARGIN = Decimal("1.15")
WORST_DAY = date(2026, 3, 31)
MINIMUM_STEP_KOP = 10_000


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
    floors: Mapping[str, Decimal] | None = None,
) -> dict[str, Decimal]:
    """floors — доли, ниже которых кампания площадки не наберёт её минимум (share_floors): перенос не опускает долю
    ниже неё, а долю, которая уже ниже, не опускает дальше."""
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
    floors = floors or {}
    share = max(share, min(current[first], floors.get(first, Decimal(0))))
    share = min(share, max(current[first], 1 - floors.get(second, Decimal(0))))
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


def campaign_limits(daily: int, remaining: int, today: date, lifetime: int = 0) -> tuple[int, int]:
    """Кэп накопленный; запас применяется только к новым разрешённым тратам."""
    next_month = (today.replace(day=28) + timedelta(days=4)).replace(day=1)
    days = min(7 - today.weekday(), (next_month - today).days)
    additional = min(remaining, daily * days)
    return int(Decimal(daily) / CAP_MULTIPLIER), lifetime + int(Decimal(additional) / CAP_MULTIPLIER)


def launch_limits(
    monthly_budget: int, platforms: Sequence[str], today: date, settings: DomainSettings = DEFAULT_SETTINGS
) -> dict[str, PlatformLimit]:
    """Лимиты кампаний при запуске сегодня: дневной и кэп до конца недели, сумма кэпов не превышает бюджет месяца."""
    budget = plan_budget(monthly_budget, 0, today, tuple(platforms), started_on=today, settings=settings)
    result = {}
    for limit in budget.limits:
        daily, cap = campaign_limits(limit.daily_limit, limit.remaining_cap, today)
        result[limit.platform] = PlatformLimit(limit.platform, cap, daily, cap)
    return result


def meets_minimum(limit: PlatformLimit, minimum: Mapping[str, int], margin: Decimal = Decimal(1)) -> bool:
    """Лимиты кампании на полную неделю не ниже минимумов площадки: Директ — недельный, VK — дневной (с запасом
    margin) и на кампанию, симулятор — day/period. Неполную неделю запуска проверяет launch.below_minimum."""
    period = minimum.get("period_cap_kop", minimum.get("period", 0))
    return limit.daily_limit >= daily_floor(minimum, margin) and limit.remaining_cap >= period


def daily_floor(minimum: Mapping[str, int], margin: Decimal = Decimal(1)) -> Decimal:
    """Дневной лимит, с которым кампания набирает минимум площадки на полную неделю. Запас margin — только у дневного
    минимума (VK — daily_limit_kop, симулятор — day): недельный минимум Директа его не получает."""
    daily = minimum.get("daily_limit_kop", minimum.get("day", 0)) * margin
    return max(Decimal(daily), Decimal(-(-minimum.get("weekly_limit_kop", 0) // 7)))


def budget_minimum(
    platforms: Sequence[str],
    settings: DomainSettings = DEFAULT_SETTINGS,
    minimums: Mapping[str, Mapping[str, int]] = PLATFORM_MINIMUMS,
) -> int:
    """Наименьший бюджет месяца, кратный 100 ₽, с которым лимиты каждой площадки при запуске в худший день не ниже её
    минимума. Худший день — последний день 31-дневного месяца (WORST_DAY): дневной лимит меньше всего, а недельный
    лимит Директа равен одному дневному. Порог не зависит от текущего месяца и дня недели, запуск с ним проходит в любой
    день. VK нужен запас MINIMUM_MARGIN: её дневной лимит падает в течение дня после расхода, и без запаса кампания у
    порога чередовала бы паузы и возобновления."""

    def enough(steps: int) -> bool:
        limits = launch_limits(steps * MINIMUM_STEP_KOP, platforms, WORST_DAY, settings)
        return all(
            meets_minimum(limits[platform], minimums.get(platform, {}), MINIMUM_MARGIN)
            and limits[platform].remaining_cap >= minimums.get(platform, {}).get("weekly_limit_kop", 0)
            for platform in platforms
        )

    low, high = 0, 1
    while not enough(high):
        low, high = high, high * 2
    while high - low > 1:
        middle = (low + high) // 2
        low, high = (low, middle) if enough(middle) else (middle, high)
    return high * MINIMUM_STEP_KOP


def budget_platforms(
    monthly_budget: int, connected: Sequence[str], settings: DomainSettings = DEFAULT_SETTINGS
) -> tuple[list[str], dict[str, int]]:
    """Площадки плана и бюджет месяца, с которого подключится выключенная. Доля VK, не набирающая её минимум с запасом,
    уходит в Директ: Директ обязателен, его минимум не даёт сохранить меньший бюджет."""
    if len(connected) < 2:
        return list(connected), {}
    needed = budget_minimum(connected, settings)
    if monthly_budget >= needed:
        return list(connected), {}
    return [platform for platform in connected if platform != "vk"], {"vk": needed}


def daily_minimum(minimum: Mapping[str, int]) -> int:
    """Дневной лимит, с которым кампания набирает минимум площадки на полную неделю."""
    return max(minimum.get("daily_limit_kop", minimum.get("day", 0)), -(-minimum.get("weekly_limit_kop", 0) // 7))


def share_floors(
    total_daily: int, minimums: Mapping[str, Mapping[str, int]], campaigns: Mapping[str, int] | None = None
) -> dict[str, Decimal]:
    """Доли дневного бюджета, ниже которых кампании площадки не наберут её минимум. Минимум у каждой кампании свой
    (в Директе — кампания на услугу), поэтому он умножается на число кампаний площадки (campaigns, по умолчанию одна).
    Дневной минимум получает запас MINIMUM_MARGIN по тому же правилу, что и при возобновлении: иначе доля у порога
    держала бы кампанию на паузе."""
    if not total_daily:
        return {}
    campaigns = campaigns or {}
    return {
        platform: daily_floor(minimum, MINIMUM_MARGIN)
        * CAP_MULTIPLIER
        * max(1, campaigns.get(platform, 1))
        / total_daily
        for platform, minimum in minimums.items()
    }


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


def active_daily_limit(campaigns: Iterable[Any]) -> int:
    """Сумма дневных лимитов работающих кампаний. Остановленные кампании не тратят: с ними запас стража
    завышен, и реклама останавливается раньше порога бюджета."""
    return sum(campaign.daily_limit_kop for campaign in campaigns if campaign.status == "active")


def split_group_limits(limit: PlatformLimit, weights: Mapping[str, Decimal]) -> tuple[GroupLimit, ...]:
    """Pass equal weights at launch, updated weights after decisions; zero excludes a group."""
    caps = allocate(limit.remaining_cap, weights)
    daily = allocate(limit.daily_limit, weights)
    weekly = allocate(limit.weekly_limit, weights)
    return tuple(
        GroupLimit(group_id, limit.platform, cap, min(daily[group_id], cap), min(weekly[group_id], cap))
        for group_id, cap in caps.items()
    )


def split_campaign_limits(limit: PlatformLimit, batches: Sequence[Sequence[str]]) -> list[PlatformLimit]:
    """Лимиты кампаний площадки: доля кампании — число её групп, сумма не превышает лимит площадки."""
    weights = {str(index): Decimal(len(batch)) for index, batch in enumerate(batches)}
    return [
        PlatformLimit(limit.platform, part.remaining_cap, part.daily_limit, part.weekly_limit)
        for part in split_group_limits(limit, weights)
    ]


def campaign_batches(
    limit: PlatformLimit, groups: Sequence[str], fits: Callable[[PlatformLimit], bool]
) -> list[list[str]]:
    """Группы площадки по кампаниям: кампания на группу, если лимит площадки даёт каждой не меньше её минимума
    (fits), иначе кампаний меньше и группы раскладываются по ним по кругу. Одна кампания — все группы вместе."""
    for count in range(len(groups), 1, -1):
        batches = [list(groups[index::count]) for index in range(count)]
        if all(fits(part) for part in split_campaign_limits(limit, batches)):
            return batches
    return [list(groups)]


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
