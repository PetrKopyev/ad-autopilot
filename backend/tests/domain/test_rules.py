from dataclasses import replace
from datetime import timedelta
from decimal import Decimal

import pytest
from hypothesis import given
from hypothesis import strategies as st

from app.domain.rules import (
    can_change,
    can_reconsider,
    classify,
    client_cac,
    client_conversion,
    decide,
    first_decision_days,
    has_anomaly,
    ref_cpl,
    settled_days,
)
from app.domain.settings import DEFAULT_SETTINGS
from app.domain.stats import wilson_interval
from app.domain.types import Action, Classification, ClientAnswer, DaySpend, GroupSnapshot, Metrics


def test_only_fourteen_settled_days_are_used(now):
    days = tuple(DaySpend(now.date() - timedelta(days=offset), 100, 10, 1, 1) for offset in range(20))
    selected = settled_days(days, now.date())
    assert len(selected) == 14
    assert max(day.day for day in selected) == now.date() - timedelta(days=2)
    assert min(day.day for day in selected) == now.date() - timedelta(days=15)
    assert len(settled_days(days, now.date(), replace(DEFAULT_SETTINGS, settlement_days=3))) == 14


def test_reference_from_check_smoothed_clients_and_unknown_answers(now):
    assert ref_cpl(100_000, (), now.date()) == 9_000
    known = ClientAnswer(now.date(), 20, 10)
    unknown = ClientAnswer(now.date(), 100, None)
    future = ClientAnswer(now.date() + timedelta(days=1), 100, 100)
    assert client_conversion((known, unknown)) == Decimal(13) / 30
    assert ref_cpl(100_000, (), now.date(), (known, unknown, future)) == 13_000
    assert client_conversion((unknown,)) == Decimal("0.3")
    assert ref_cpl(0, (), now.date()) == 0


def test_relative_reference_requires_three_units_with_leads(make_ad, now):
    ads = [make_ad(id=str(index), spend=spend) for index, spend in enumerate((1_000, 2_000, 9_000))]
    assert ref_cpl(None, ads[:2], now.date()) is None
    assert ref_cpl(None, ads, now.date()) == 2_000
    assert ref_cpl(None, ads + [make_ad(id="empty", leads=0, spend=1_000_000)], now.date()) == 2_000
    recent = make_ad(id="recent", days=(DaySpend(now.date(), 1_000_000, 100, 10, 1),))
    assert ref_cpl(None, ads[:2] + [recent], now.date()) is None


def test_few_ads_without_check_use_average_lead_price_once_three_leads(make_ad, now):
    # Одна услуга: два объявления, медианы нет; опора — средняя цена обращения всей рекламы.
    ads = [make_ad(id="a", spend=10_000, leads=1), make_ad(id="b", spend=30_000, leads=2)]
    assert ref_cpl(None, ads[:1], now.date()) is None
    assert ref_cpl(None, ads, now.date()) == 40_000 // 3
    cheap, dear = (
        make_ad(id="cheap", spend=60_000, clicks=200, leads=12),
        make_ad(id="dear", spend=300_000, clicks=200, leads=1),
    )
    reference = ref_cpl(None, [cheap, dear], now.date())
    assert reference == 360_000 // 13
    groups = [GroupSnapshot("group", "direct", 10_000), GroupSnapshot("other", "direct", 10_000)]
    other = make_ad(id="other", group_id="other", spend=0, clicks=0, leads=0, impressions=0)
    decisions = decide([cheap, dear, other], groups, now, reference)
    assert {(item.target_id, item.reason_code) for item in decisions} >= {("dear", "PAUSE_TOO_EXPENSIVE")}


def test_client_cost_uses_last_four_weeks_and_unknown_is_not_zero(now):
    today = now.date()
    answers = (
        ClientAnswer(today - timedelta(weeks=4), 100, 100, 1_000_000),
        ClientAnswer(today - timedelta(days=27), 10, 2, 10_000),
        ClientAnswer(today, 10, 3, 20_000),
        ClientAnswer(today + timedelta(days=1), 100, 100, 1_000_000),
    )
    assert client_cac(answers, today) == 6_000
    assert client_cac(answers + (ClientAnswer(today, 1, None, 100),), today) is None
    assert client_cac((ClientAnswer(today, 10, 0),), today) is None
    with pytest.raises(ValueError):
        ClientAnswer(today, 1, 2)


@pytest.mark.parametrize("clicks,expected", [(39, Classification.INSUFFICIENT), (40, Classification.BORDERLINE)])
def test_minimum_price_clicks(make_ad, now, clicks, expected):
    assert classify(make_ad(clicks=clicks), now, 10_000).classification == expected


@pytest.mark.parametrize("days,expected", [(4, Classification.INSUFFICIENT), (5, Classification.GOOD)])
def test_warmup_boundary(make_ad, now, days, expected):
    ad = make_ad(spend=1_000, leads=5, approved_at=now - timedelta(days=days))
    assert classify(ad, now, 10_000).classification == expected


@pytest.mark.parametrize(
    "clicks,had_leads,expected",
    [
        (79, True, Classification.BORDERLINE),
        (80, True, Classification.BAD),
        (80, False, Classification.BORDERLINE),
    ],
)
def test_no_leads_hard_stop_boundary_even_during_warmup(make_ad, now, clicks, had_leads, expected):
    ad = make_ad(clicks=clicks, leads=0)
    assert classify(ad, now, 10_000, business_had_leads=had_leads).classification == expected
    if clicks == 80 and had_leads:
        young = replace(ad, approved_at=now - timedelta(days=1))
        assert classify(young, now, None, business_had_leads=True).reason_code == "PAUSE_NO_LEADS"


@pytest.mark.parametrize("leads,expected", [(2, Classification.INSUFFICIENT), (3, Classification.GOOD)])
def test_good_requires_three_leads(make_ad, now, leads, expected):
    assert classify(make_ad(leads=leads, spend=1_000), now, 10_000).classification == expected


def test_good_wilson_price_threshold_to_the_kopeck(make_ad, now):
    lower, _ = wilson_interval(5, 40)
    limit = int(Decimal(str(lower)) * 40 * 10_000)
    assert classify(make_ad(leads=5, spend=limit), now, 10_000).classification == Classification.GOOD
    assert classify(make_ad(leads=5, spend=limit + 1), now, 10_000).classification != Classification.GOOD


def test_bad_price_multiplier_is_strict(make_ad, now):
    ad = make_ad(leads=1, spend=100_000)
    _, upper = wilson_interval(1, 40)
    ratio = Decimal(100_000) / 40 / Decimal(str(upper)) / 10_000
    exact = replace(DEFAULT_SETTINGS, bad_cpl_multiplier=ratio)
    assert classify(ad, now, 10_000, settings=exact).classification != Classification.BAD
    below = replace(exact, bad_cpl_multiplier=ratio - Decimal("0.00001"))
    assert classify(ad, now, 10_000, settings=below).classification == Classification.BAD


@pytest.mark.parametrize("spend,expected", [(19_999, Classification.BORDERLINE), (20_000, Classification.BAD)])
def test_bad_requires_minimum_spend(make_ad, now, spend, expected):
    settings = replace(DEFAULT_SETTINGS, confidence=0.01)
    assert classify(make_ad(spend=spend), now, 10_000, settings=settings).classification == expected


@pytest.mark.parametrize("spend,expected", [(14_999, Classification.INSUFFICIENT), (15_000, Classification.BORDERLINE)])
def test_borderline_spend_boundary(make_ad, now, spend, expected):
    assert classify(make_ad(spend=spend), now, 10_000).classification == expected


@pytest.mark.parametrize(
    "impressions,clicks,median_ctr,expected",
    [
        (1_999, 1, "0.02", False),
        (2_000, 1, "0.02", True),
        (2_000, 8, "0.01", False),
        (2_000, 8, "0.01001", True),
        (2_000, 10, "0.02", False),
        (2_000, 9, "0.02", True),
    ],
)
def test_low_ctr_all_boundaries(make_ad, now, impressions, clicks, median_ctr, expected):
    ad = make_ad(impressions=impressions, clicks=clicks, leads=0)
    assessment = classify(ad, now, None, Decimal(median_ctr))
    assert (assessment.classification == Classification.LOW_CTR) == expected


@pytest.mark.parametrize("hours,impressions,expected", [(47, 0, False), (48, 0, True), (48, 1, False)])
def test_not_showing_uses_all_time_impressions(make_ad, now, hours, impressions, expected):
    ad = make_ad(approved_at=now - timedelta(hours=hours), days=(), lifetime_impressions=impressions)
    assert (classify(ad, now, None).classification == Classification.NOT_SHOWING) == expected
    historical = replace(ad, days=(DaySpend(now.date() - timedelta(days=100), impressions=1),))
    assert classify(historical, now, None).classification != Classification.NOT_SHOWING


@pytest.mark.parametrize("field,hours", [("last_changed_at", 72), ("owner_edited_at", 24)])
def test_object_and_owner_cooldown_boundaries(make_ad, now, field, hours):
    assert can_change(make_ad(**{field: now - timedelta(hours=hours)}), now)
    assert not can_change(make_ad(**{field: now - timedelta(hours=hours) + timedelta(microseconds=1)}), now)


def test_freeze_and_reconsideration_boundaries(make_ad, now):
    assert can_change(make_ad(frozen_until=now), now)
    assert not can_change(make_ad(frozen_until=now + timedelta(microseconds=1)), now)
    ad = make_ad(active=False, paused_by_autopilot_at=now - timedelta(days=21))
    assert can_reconsider(ad, now)
    assert not can_reconsider(replace(ad, paused_by_autopilot_at=now - timedelta(days=20)), now)
    assert not can_reconsider(replace(ad, paused_by_autopilot_at=None), now)


def anomaly_days(now, previous_leads=1):
    return tuple(
        DaySpend(now.date() - timedelta(days=offset), 100, 100, 10, previous_leads if offset >= 5 else 0)
        for offset in range(2, 16)
    )


def test_anomaly_three_days_previous_average_and_missing_days(now):
    days = anomaly_days(now)
    assert has_anomaly(days, now.date())
    assert not has_anomaly(days[1:], now.date())
    assert not has_anomaly(anomaly_days(now, 0), now.date())
    assert not has_anomaly((replace(days[0], leads=1),) + days[1:], now.date())
    below_one = days[:3] + (replace(days[3], leads=0),) + days[4:]
    assert not has_anomaly(below_one, now.date())
    assert not has_anomaly(days[:2], now.date())


def test_anomaly_freezes_no_leads_price_and_scaling_but_keeps_ctr(make_ad, now):
    frozen = anomaly_days(now)
    ad = make_ad(clicks=80, leads=0, spend=1_000_000)
    assert (
        classify(ad, now, 10_000, business_had_leads=True, price_frozen=True).classification
        == Classification.INSUFFICIENT
    )
    groups = [GroupSnapshot(str(i), "direct", 10_000) for i in range(4)]
    ads = [replace(ad, id=str(i), group_id=str(i)) for i in range(4)]
    result = decide(ads, groups, now, 10_000, frozen, free_daily_budget=10_000)
    assert [(item.action, item.reason_code) for item in result] == [(Action.NOTIFY, "ANOMALY")]
    low_ctr = make_ad(impressions=2_000, clicks=1, leads=0)
    assert classify(low_ctr, now, 10_000, Decimal("0.02"), price_frozen=True).reason_code == "PAUSE_LOW_CTR"


@given(group_count=st.integers(2, 20), ads_per_group=st.integers(1, 3))
def test_never_leave_fewer_than_two_active_groups_per_platform(group_count, ads_per_group):
    from datetime import UTC, datetime

    from app.domain.types import AdSnapshot

    now = datetime(2026, 10, 26, tzinfo=UTC)
    groups = [
        GroupSnapshot(f"{platform}-{i}", platform, 10_000) for platform in ("direct", "vk") for i in range(group_count)
    ]
    day = DaySpend(now.date() - timedelta(days=2), 1_000_000, 1_000, 80, 0)
    ads = [
        AdSnapshot(f"{group.id}-{j}", group.id, group.platform, now - timedelta(days=10), (day,))
        for group in groups
        for j in range(ads_per_group)
    ]
    history = (DaySpend(now.date() - timedelta(days=3), impressions=1, clicks=1, leads=1),)
    decisions = decide(ads, groups, now, 10_000, history)
    pauses = {item.target_id for item in decisions if item.action == Action.PAUSE}
    assert len(pauses) <= 5
    for platform in ("direct", "vk"):
        remaining = {ad.group_id for ad in ads if ad.platform == platform and ad.id not in pauses}
        assert len(remaining) >= 2


def test_five_changes_and_group_freeze(make_ad, now):
    groups = [GroupSnapshot(str(i), "direct", 10_000) for i in range(10)]
    ads = [make_ad(id=str(i), group_id=str(i), spend=1_000_000) for i in range(10)]
    assert len([item for item in decide(ads, groups, now, 10_000) if item.action == Action.PAUSE]) == 5
    frozen = [replace(group, frozen_until=now + timedelta(days=1)) for group in groups]
    assert not decide(ads, frozen, now, 10_000)
    config = replace(DEFAULT_SETTINGS, max_changes=1)
    assert len(decide(ads, groups, now, 10_000, settings=config)) == 1


def test_scaling_uses_only_freed_budget_and_twenty_percent_steps(make_ad, now):
    groups = [GroupSnapshot(str(i), "direct", 10_000) for i in range(3)]
    ads = [make_ad(id=str(i), group_id=str(i), spend=1_000, leads=5) for i in range(3)]
    unfunded = decide(ads, groups, now, 10_000)
    assert [(item.action, item.reason_code) for item in unfunded] == [(Action.NOTIFY, "BUDGET_OPPORTUNITY")]
    decisions = decide(ads, groups, now, 10_000, free_daily_budget=3_000)
    assert [item.new_daily_limit for item in decisions] == [12_000, 11_000]
    assert all(item.entity == "group" and item.action == Action.SCALE for item in decisions)
    assert sum(item.new_daily_limit - 10_000 for item in decisions) == 3_000


def test_scaling_thirty_percent_transfer_and_three_equal_shares(make_ad, now):
    groups = [GroupSnapshot(str(i), "direct", 10_000) for i in range(3)]
    ads = [make_ad(id=str(i), group_id=str(i), spend=1_000, leads=5) for i in range(3)]
    config = replace(DEFAULT_SETTINGS, scale_step=Decimal(100))
    decisions = decide(ads, groups, now, 10_000, free_daily_budget=30_000, settings=config)
    assert sum(item.new_daily_limit - 10_000 for item in decisions) == 18_000
    capped = replace(config, max_equal_share_multiplier=Decimal("0.6"))
    decisions = decide(ads, groups, now, 10_000, free_daily_budget=30_000, settings=capped)
    assert all(item.new_daily_limit == 12_000 for item in decisions)


@pytest.mark.parametrize("kwargs", [{"spend": 1.0}, {"clicks": 2, "impressions": 1}, {"leads": 2, "clicks": 1}])
def test_metrics_reject_invalid_money_and_counts(kwargs):
    with pytest.raises(ValueError):
        Metrics(**kwargs)


def test_unapproved_and_paused_ads_are_not_changed(make_ad, now):
    for ad in (make_ad(approved_at=None), make_ad(active=False), make_ad(approved_at=now + timedelta(days=1))):
        assert classify(ad, now, 10_000).classification == Classification.INSUFFICIENT


def test_empty_groups_cannot_defeat_minimum_active_protection(make_ad, now):
    groups = [GroupSnapshot(str(i), "direct", 10_000) for i in range(10)]
    ads = [make_ad(id=str(i), group_id=str(i), spend=1_000_000) for i in range(2)]
    result = decide(ads, groups, now, 10_000)
    assert all(item.action == Action.NOTIFY for item in result)


def test_anomaly_is_derived_from_ads_if_business_history_is_not_supplied(make_ad, now):
    groups = [GroupSnapshot(str(i), "direct", 10_000) for i in range(4)]
    ads = [make_ad(id=str(i), group_id=str(i), days=anomaly_days(now)) for i in range(4)]
    assert [item.reason_code for item in decide(ads, groups, now, 1)] == ["ANOMALY"]


def test_pauses_and_scales_share_a_single_change_limit(make_ad, now):
    groups = [GroupSnapshot(str(i), "direct", 10_000) for i in range(8)]
    ads = [make_ad(id=str(i), group_id=str(i), spend=1_000_000 if i < 4 else 1_000, leads=5) for i in range(8)]
    result = decide(ads, groups, now, 10_000, free_daily_budget=10_000)
    changes = [item for item in result if item.action != Action.NOTIFY]
    assert len(changes) == 5
    assert sum(item.action == Action.PAUSE for item in changes) == 4
    assert sum(item.action == Action.SCALE for item in changes) == 1


def test_every_emitted_decision_can_be_explained(make_ad, now):
    from app.domain.explain import explain

    groups = [GroupSnapshot(str(i), "direct", 10_000) for i in range(4)]
    ads = [make_ad(id=str(i), group_id=str(i), spend=1_000_000) for i in range(4)]
    names = {"ad": "Маникюр", "group": "Маникюр", "campaign": "Автопилот"}
    assert all(explain(item.reason_code, item.numbers, **names) for item in decide(ads, groups, now, 10_000))


def test_pause_names_the_settled_days_it_was_decided_on(make_ad, now):
    # Пояснение «Потратили 211 ₽» без периода было непонятно: за какие дни. Вчерашний день ещё не устоялся и в расчёт
    # и период не входит.
    days = tuple(DaySpend(now.date() - timedelta(days=offset), 200_000, 1_000, 30, 0) for offset in (1, 3, 5, 9))
    groups = [GroupSnapshot(str(i), "direct", 10_000) for i in range(3)]
    ads = [make_ad(id="0", group_id="0", days=days)] + [make_ad(id=str(i), group_id=str(i)) for i in (1, 2)]
    (pause,) = [item for item in decide(ads, groups, now, 10_000) if item.action == Action.PAUSE]
    assert pause.numbers["period_start"] == (now.date() - timedelta(days=9)).isoformat()
    assert pause.numbers["period_end"] == (now.date() - timedelta(days=3)).isoformat()


def test_ad_without_settled_days_is_not_paused(make_ad, now):
    # Пауза называет период устоявшихся дней; вчерашний расход без обращений ещё не повод и не ломает пояснение.
    fresh = (DaySpend(now.date() - timedelta(days=1), 1_000_000, 100_000, 500, 0),)
    groups = [GroupSnapshot(str(i), "direct", 10_000) for i in range(3)]
    ads = [make_ad(id="0", group_id="0", days=fresh)] + [make_ad(id=str(i), group_id=str(i)) for i in (1, 2)]
    history = (DaySpend(now.date() - timedelta(days=3), impressions=1, clicks=1, leads=1),)
    assert not [item for item in decide(ads, groups, now, 10_000, history) if item.target_id == "0"]


def test_first_decision_days_follow_thresholds():
    # На проверке: только разогрев.
    assert first_decision_days(None, None) == DEFAULT_SETTINGS.warmup_days
    # 10 кликов в день: 40 кликов за 4 дня и ещё 2 дня на уточнение — 6 дней с одобрения, прошло 2.
    assert first_decision_days(2, Decimal(10)) == 4
    # Кликов много: срок задаёт разогрев.
    assert first_decision_days(1, Decimal(100)) == DEFAULT_SETTINGS.warmup_days - 1
    assert first_decision_days(9, Decimal(10)) == 0
    # 2 клика в день: за 14 дней окна 40 не набрать.
    assert first_decision_days(3, Decimal(2)) is None
    assert first_decision_days(3, Decimal(0)) is None
