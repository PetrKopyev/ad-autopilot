from dataclasses import replace
from datetime import date, timedelta
from decimal import Decimal

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from app.domain.budget import (
    MINIMUM_MARGIN,
    WORST_DAY,
    allocate,
    authorized_budget,
    budget_guard,
    budget_minimum,
    budget_platforms,
    campaign_batches,
    launch_limits,
    meets_minimum,
    plan_budget,
    platform_shares,
    rebalance_platform_shares,
    share_floors,
    split_campaign_limits,
    split_group_limits,
    stop_margin,
)
from app.domain.settings import DEFAULT_SETTINGS
from app.domain.types import PlatformLimit


def test_reserve_prorating_and_integer_rounding():
    assert authorized_budget(100_000, 31, 31) == 92_000
    assert authorized_budget(100_000, 10, 31) == 29_677
    assert authorized_budget(1, 31, 31) == 0
    assert authorized_budget(100_000, 31, 31, replace(DEFAULT_SETTINGS, budget_reserve=Decimal("0.1"))) == 90_000


@pytest.mark.parametrize("value", [-1, 1.0, Decimal("1"), True])
def test_money_must_be_nonnegative_integer(value):
    with pytest.raises(ValueError):
        authorized_budget(value, 31, 31)
    with pytest.raises(ValueError):
        budget_guard(100_000, value, 100)


def test_initial_split_single_platform_and_rounding():
    assert platform_shares(("direct", "vk")) == {"direct": Decimal("0.7"), "vk": Decimal("0.3")}
    assert platform_shares(("vk",)) == {"vk": Decimal(1)}
    assert allocate(101, platform_shares(("direct", "vk"))) == {"direct": 71, "vk": 30}
    assert allocate(2, dict.fromkeys(("a", "b", "c"), Decimal(1))) == {"a": 1, "b": 1, "c": 0}
    assert allocate(100, {"a": Decimal(0)}) == {"a": 0}
    with pytest.raises(ValueError):
        allocate(100, {"a": 0.7})


@pytest.mark.parametrize("days,expected", [(13, "0.7"), (14, "0.8"), (15, "0.8")])
def test_platform_rebalance_age_and_max_step(days, expected):
    current = platform_shares(("direct", "vk"))
    result = rebalance_platform_shares(current, {"direct": 100, "vk": 10_000}, days)
    assert result["direct"] == Decimal(expected)
    assert sum(result.values()) == 1
    assert current["direct"] == Decimal("0.7")


def test_platform_rebalance_uses_inverse_prices_and_requires_data():
    current = platform_shares(("direct", "vk"))
    assert rebalance_platform_shares(current, {"direct": 10_000, "vk": 100}, 14)["direct"] == Decimal("0.6")
    assert rebalance_platform_shares(current, {"direct": 100, "vk": 300}, 14)["direct"] == Decimal("0.75")
    assert rebalance_platform_shares(current, {"direct": None, "vk": 100}, 14) == current


@pytest.mark.parametrize("today", [date(2026, 10, 26), date(2026, 10, 31), date(2026, 11, 1), date(2028, 2, 29)])
def test_weekly_limit_stops_at_month_boundary(today):
    plan = plan_budget(3_100_000, 0, today)
    days = min(
        7 - today.weekday(), (today.replace(day=28) + timedelta(days=4)).replace(day=1).toordinal() - today.toordinal()
    )
    assert all(limit.weekly_limit <= limit.daily_limit * days for limit in plan.limits)
    assert all(limit.weekly_limit <= limit.remaining_cap for limit in plan.limits)


def test_daily_recovery_cap_and_first_month():
    plan = plan_budget(3_100_000, 0, date(2026, 10, 31))
    assert sum(limit.daily_limit for limit in plan.limits) == 119_600
    first = plan_budget(3_100_000, 0, date(2026, 10, 22), started_on=date(2026, 10, 22))
    assert first.authorized == 920_000
    assert sum(limit.daily_limit for limit in first.limits) == 92_000
    next_month = plan_budget(3_100_000, 0, date(2026, 11, 1), started_on=date(2026, 10, 22))
    assert next_month.authorized == 2_852_000


@settings(max_examples=100, deadline=None)
@given(
    monthly=st.integers(0, 10**12),
    start=st.integers(1, 31),
    expenses=st.lists(st.integers(0, 10**10), min_size=1, max_size=40),
    share=st.integers(0, 100),
)
def test_caps_never_exceed_remaining_budget_for_any_spend_sequence(monthly, start, expenses, share):
    spent = 0
    weights = {"direct": Decimal(share) / 100, "vk": Decimal(100 - share) / 100}
    for offset, expense in enumerate(expenses):
        spent += expense
        today = date(2026, 10, min(31, start + offset))
        plan = plan_budget(monthly, spent, today, started_on=date(2026, 10, start), shares=weights)
        remaining = max(0, plan.authorized - spent)
        assert sum(item.remaining_cap for item in plan.limits) == remaining
        assert sum(item.weekly_limit for item in plan.limits) <= remaining
        assert sum(item.daily_limit for item in plan.limits) <= remaining
        assert all(
            type(value) is int
            for item in plan.limits
            for value in (item.remaining_cap, item.daily_limit, item.weekly_limit)
        )


def test_guard_threshold_is_inclusive_and_margin_rounds_up():
    assert stop_margin(16_000) == 1_000
    assert stop_margin(1) == 1
    assert not budget_guard(100_000, 98_999, 16_000).stop
    result = budget_guard(100_000, 99_000, 16_000)
    assert result.stop and result.reason_code == "BUDGET_STOP"
    assert budget_guard(0, 0, 0).stop


def test_reconciliation_stops_on_one_extra_kopeck_and_uses_highest_spend():
    assert budget_guard(100_000, 10_001, 100, recorded_spent=10_000).reason_code == "SPEND_MISMATCH"
    assert not budget_guard(100_000, 10_000, 100, recorded_spent=10_000).stop
    assert budget_guard(100_000, 1, 16_000, recorded_spent=99_000).stop


@pytest.mark.parametrize("poll_intervals", [1, 2])
@pytest.mark.parametrize("monthly", [100_000, 1_000_000, 10_000_000])
def test_guard_stops_evil_platform_with_25_percent_overspend(monthly, poll_intervals):
    daily = monthly // 20
    per_poll = (daily * 125 + 4_799) // 4_800
    spent = 0
    for _ in range(2_000):
        if budget_guard(monthly, spent, daily).stop:
            assert spent + per_poll <= monthly
            break
        spent += per_poll * poll_intervals
        assert spent <= monthly
    else:
        pytest.fail("Guard did not stop the platform")


def test_unconnected_platforms_get_no_caps():
    assert plan_budget(100, 0, date(2026, 10, 1), ()).limits == ()
    with pytest.raises(ValueError):
        plan_budget(100, 0, date(2026, 10, 1), ("direct", "direct"))
    with pytest.raises(ValueError):
        plan_budget(100, 0, date(2026, 10, 1), shares={"direct": Decimal(1)})


@given(
    cap=st.integers(0, 10**10),
    daily=st.integers(0, 10**10),
    weekly=st.integers(0, 10**10),
    weights=st.lists(st.integers(0, 10_000), min_size=1, max_size=20),
)
def test_group_split_preserves_all_platform_caps(cap, daily, weekly, weights):
    platform = PlatformLimit("direct", cap, min(cap, daily), min(cap, weekly))
    groups = split_group_limits(platform, {str(i): Decimal(weight) for i, weight in enumerate(weights)})
    assert sum(group.remaining_cap for group in groups) <= platform.remaining_cap
    assert sum(group.daily_limit for group in groups) <= platform.daily_limit
    assert sum(group.weekly_limit for group in groups) <= platform.weekly_limit
    assert all(max(group.daily_limit, group.weekly_limit) <= group.remaining_cap for group in groups)


def test_group_limits_start_equal_and_use_updated_weights():
    platform = PlatformLimit("direct", 300_000, 30_000, 210_000)
    equal = split_group_limits(platform, dict.fromkeys(("a", "b", "c"), Decimal(1)))
    assert [group.daily_limit for group in equal] == [10_000] * 3
    updated = split_group_limits(platform, {"a": Decimal("1.2"), "b": Decimal(1), "c": Decimal(0)})
    assert updated[0].daily_limit > updated[1].daily_limit
    assert updated[2].remaining_cap == updated[2].daily_limit == updated[2].weekly_limit == 0


@pytest.mark.parametrize("month_start", [date(2026, 2, 1), date(2028, 2, 1), date(2026, 4, 1), date(2026, 1, 1)])
def test_odd_kopecks_split_without_loss_in_months_of_any_length(month_start):
    days = (month_start.replace(month=month_start.month % 12 + 1) - month_start).days
    plan = plan_budget(1_000_003, 0, month_start)
    assert plan.authorized == 920_002
    assert sum(limit.remaining_cap for limit in plan.limits) == plan.authorized
    daily = sum(limit.daily_limit for limit in plan.limits)
    assert daily == plan.authorized // days and daily * days <= plan.authorized
    spent, today = 0, month_start
    while today.month == month_start.month:
        plan = plan_budget(1_000_003, spent, today)
        spent += sum(limit.daily_limit for limit in plan.limits)
        today += timedelta(days=1)
    assert plan.authorized - days < spent <= plan.authorized


MONDAY = date(2026, 4, 6)  # апрель — 30 дней
DIRECT_MINIMUM = {"weekly_limit_kop": 30_000}
VK_MINIMUM = {"daily_limit_kop": 30_000, "period_cap_kop": 30_000}


@pytest.mark.parametrize(
    "monthly,connected,platforms,off",
    [
        (1_550_000, ("direct",), ["direct"], {}),
        (1_550_000, ("direct", "vk"), ["direct"], {"vk": 4_850_000}),
        (3_000_000, ("direct", "vk"), ["direct"], {"vk": 4_850_000}),
        (4_840_000, ("direct", "vk"), ["direct"], {"vk": 4_850_000}),
        (4_850_000, ("direct", "vk"), ["direct", "vk"], {}),
        (4_850_000, ("direct",), ["direct"], {}),
    ],
)
def test_vk_joins_plan_only_with_a_margin_over_its_minimum(monthly, connected, platforms, off):
    assert budget_platforms(monthly, connected) == (platforms, off)
    limits = launch_limits(monthly, platforms, WORST_DAY)
    assert set(limits) == set(platforms)
    assert all(limits[platform].remaining_cap >= 30_000 for platform in platforms)
    if "vk" in platforms:
        assert limits["vk"].daily_limit >= 34_500


def test_vk_share_one_step_below_threshold_misses_its_margin():
    assert 30_000 < launch_limits(4_840_000, ("direct", "vk"), WORST_DAY)["vk"].daily_limit < 34_500
    # Весь бюджет уходит в Директ: его дневной лимит больше, чем у Директа при двух площадках.
    alone = launch_limits(4_840_000, ("direct",), MONDAY)["direct"].daily_limit
    assert alone > launch_limits(4_850_000, ("direct", "vk"), MONDAY)["direct"].daily_limit


@pytest.mark.parametrize(
    "today",
    [
        date(2026, 2, 2),  # февраль — 28 дней
        date(2026, 2, 28),  # последний день короткого месяца
        MONDAY,
        date(2026, 4, 5),  # воскресенье: недельный лимит Директа — один дневной
        date(2026, 3, 2),  # март — 31 день
        date(2026, 3, 29),  # воскресенье 31-дневного месяца
        WORST_DAY,  # последний день 31-дневного месяца
    ],
)
def test_budget_minimum_does_not_depend_on_month_or_weekday(today):
    assert budget_minimum(("direct",)) == 1_550_000
    assert budget_minimum(("direct", "vk")) == 4_850_000
    direct = launch_limits(1_550_000, ("direct",), today)["direct"]
    assert direct.daily_limit >= 36_600 and direct.remaining_cap >= 36_600
    both = launch_limits(4_850_000, ("direct", "vk"), today)
    assert both["direct"].remaining_cap >= 36_600 and both["vk"].daily_limit >= 34_500


def test_one_step_below_direct_minimum_misses_it_on_the_worst_day():
    assert launch_limits(1_540_000, ("direct",), WORST_DAY)["direct"].remaining_cap < 36_600


@pytest.mark.parametrize(
    "limit,minimum,margin,expected",
    [
        (PlatformLimit("vk", 30_000, 29_900, 30_000), VK_MINIMUM, Decimal(1), False),
        (PlatformLimit("vk", 30_000, 30_000, 30_000), VK_MINIMUM, Decimal(1), True),
        (PlatformLimit("vk", 34_500, 34_499, 34_500), VK_MINIMUM, MINIMUM_MARGIN, False),
        (PlatformLimit("vk", 34_500, 34_500, 34_500), VK_MINIMUM, MINIMUM_MARGIN, True),
        (PlatformLimit("direct", 4_285, 4_285, 4_285), DIRECT_MINIMUM, Decimal(1), False),
        (PlatformLimit("direct", 4_286, 4_286, 4_286), DIRECT_MINIMUM, MINIMUM_MARGIN, True),
        (PlatformLimit("direct", 0, 0, 0), {"day": 0, "period": 0}, Decimal(1), True),
    ],
)
def test_minimum_is_inclusive_checks_a_full_week_and_margin_only_for_daily(limit, minimum, margin, expected):
    assert meets_minimum(limit, minimum, margin) is expected


def test_share_floors_keep_each_campaign_at_its_live_minimum():
    minimums = {"direct": DIRECT_MINIMUM, "vk": VK_MINIMUM}
    assert share_floors(100_000, minimums) == {"direct": Decimal("0.053575"), "vk": Decimal("0.43125")}
    assert share_floors(100_000, {"direct": {"weekly_limit_kop": 70_000}})["direct"] == Decimal("0.125")
    assert share_floors(100_000, {"direct": {"day": 0, "period": 0}}) == {"direct": Decimal(0)}
    assert share_floors(100_000, {"fake": {"day": 10_000}}) == {"fake": Decimal("0.14375")}
    assert share_floors(0, minimums) == {}
    # У каждой кампании Директа свой минимум: три кампании на услугу — втрое больший пол доли.
    assert share_floors(100_000, minimums, {"direct": 3})["direct"] == Decimal("0.053575") * 3
    assert share_floors(100_000, minimums, {"direct": 0})["direct"] == Decimal("0.053575")


def test_rebalance_does_not_push_platform_below_its_floor():
    current = platform_shares(("direct", "vk"))
    cheap_direct = {"direct": 100, "vk": 10_000}
    assert rebalance_platform_shares(current, cheap_direct, 14)["direct"] == Decimal("0.8")
    vk_floor = {"vk": Decimal("0.3")}
    assert rebalance_platform_shares(current, cheap_direct, 14, floors=vk_floor)["direct"] == Decimal("0.7")
    assert rebalance_platform_shares(current, cheap_direct, 14, floors={"vk": Decimal("0.25")})["direct"] == Decimal(
        "0.75"
    )
    # Доля уже ниже минимума (бюджет уменьшили): перенос не опускает её дальше, а к минимуму ведёт обычным шагом.
    low = {"direct": Decimal("0.8"), "vk": Decimal("0.2")}
    assert rebalance_platform_shares(low, cheap_direct, 14, floors=vk_floor) == low
    expected = {"direct": Decimal("0.7"), "vk": Decimal("0.3")}
    assert rebalance_platform_shares(low, {"direct": 10_000, "vk": 100}, 14, floors=vk_floor) == expected


def test_campaign_per_group_when_budget_allows():
    limit = PlatformLimit("direct", 500_000, 100_000, 500_000)
    batches = campaign_batches(limit, ["1", "2", "3"], lambda part: part.daily_limit >= 30_000)
    assert batches == [["1"], ["2"], ["3"]]
    parts = split_campaign_limits(limit, batches)
    assert sum(part.daily_limit for part in parts) == 100_000
    assert sum(part.remaining_cap for part in parts) == 500_000


def test_fewer_campaigns_when_minimum_not_met():
    limit = PlatformLimit("direct", 500_000, 100_000, 500_000)
    batches = campaign_batches(limit, ["1", "2", "3", "4", "5"], lambda part: part.daily_limit >= 30_000)
    # Три кампании дали бы последней одну группу и 200 ₽ в день — ниже минимума.
    assert batches == [["1", "3", "5"], ["2", "4"]]
    assert [part.daily_limit for part in split_campaign_limits(limit, batches)] == [60_000, 40_000]


def test_single_campaign_when_budget_is_small():
    limit = PlatformLimit("direct", 50_000, 10_000, 50_000)
    assert campaign_batches(limit, ["1", "2"], lambda part: part.daily_limit >= 30_000) == [["1", "2"]]
