import json
from dataclasses import asdict, replace
from datetime import timedelta
from math import ceil
from random import Random

import pytest

from app.domain import experiments
from app.domain.experiments import (
    design_experiment,
    evaluate_audience,
    evaluate_experiment,
    extended_target,
    next_hypothesis,
    select_borderline,
)
from app.domain.settings import DEFAULT_SETTINGS
from app.domain.stats import sample_size
from app.domain.types import DaySpend, ExperimentDesign, Verdict


@pytest.fixture
def design():
    return ExperimentDesign(10_000, 10, 10_000, "text", True, "EXPERIMENT_START")


@pytest.fixture
def arms(now):
    def factory(
        control_clicks=100,
        variant_clicks=200,
        control_leads=20,
        variant_leads=40,
        control_impressions=10_000,
        variant_impressions=10_000,
    ):
        day = now.date() - timedelta(days=2)
        return (
            (DaySpend(day, 10_000, control_impressions, control_clicks, control_leads),),
            (DaySpend(day, 10_000, variant_impressions, variant_clicks, variant_leads),),
        )

    return factory


@pytest.mark.parametrize("budget,expected", [(69_999, None), (70_000, "audience"), (70_001, "audience")])
def test_hypothesis_order_and_audience_budget(budget, expected):
    assert next_hypothesis((), budget) == "text"
    assert next_hypothesis(("text",), budget) == "image"
    assert next_hypothesis(("text", "image"), budget) == expected
    assert next_hypothesis(("text", "image", "audience"), budget) is None


def test_borderline_selection_concurrency_and_cooldown(make_ad, now):
    ads = [make_ad(id=str(i), group_id=f"group-{i // 2}") for i in range(8)]
    result = select_borderline(ads, now, 10_000)
    assert len(result) == 2 and len({ad.group_id for ad in result}) == 2
    result = select_borderline(ads, now, 10_000, ("group-0",))
    assert len(result) == 1 and result[0].group_id != "group-0"
    assert not select_borderline(ads, now, 10_000, ("group-0", "group-1"))
    assert not select_borderline([replace(ad, frozen_until=now + timedelta(days=1)) for ad in ads], now, 10_000)
    assert not select_borderline([make_ad(spend=1_000, leads=5)], now, 10_000)


def test_borderline_selection_is_frozen_by_business_anomaly(make_ad, now):
    history = tuple(
        DaySpend(now.date() - timedelta(days=i), impressions=100, clicks=10, leads=int(i >= 5)) for i in range(2, 16)
    )
    assert not select_borderline([make_ad()], now, 10_000, business_days=history)


def test_borderline_selection_skips_platforms_without_conversion_source(make_ad, now):
    """VK без пикселя (0 обращений, расход выше порога пограничного) не становится
    «пограничным» по выдуманной цене обращения на опоре Директа — под него открывался бы CTR-тест."""
    vk = make_ad(id="vk-1", platform="vk", leads=0)
    assert select_borderline([vk], now, 10_000) == (vk,)
    assert select_borderline([vk], now, 10_000, platforms_without_source={"vk"}) == ()


@pytest.mark.parametrize("days,accepted", [(13, True), (14, True), (15, False)])
def test_design_duration_boundary_and_quarter_budget(days, accepted):
    n = sample_size(0.0125)
    daily_test_impressions = ceil(2 * n / (days - DEFAULT_SETTINGS.settlement_days))
    design = design_experiment(0.0125, daily_test_impressions * 4, 70_000)
    assert design.n_per_arm == n
    assert design.estimated_days == days
    assert design.accepted == accepted
    assert design.daily_budget == 17_500
    assert design.reason_code == ("EXPERIMENT_START" if accepted else "EXPERIMENT_TOO_LONG")


def test_design_rejects_no_traffic_and_no_eligible_hypothesis():
    assert not design_experiment(0.01, 0, 70_000).accepted
    assert not design_experiment(0.01, 1_000_000, 0).accepted
    assert not design_experiment(0.01, 1_000_000, 69_999, ("text", "image")).accepted


def test_no_peeking_before_both_arms_reach_fixed_sample(design, arms, now):
    control, variant = arms(control_impressions=9_999, variant_impressions=20_000)
    start = now.date() - timedelta(days=10)
    assert evaluate_experiment(design, control, variant, start, now.date()).verdict == Verdict.RUNNING
    control, variant = arms()
    assert evaluate_experiment(design, control, variant, start, now.date()).verdict == Verdict.VARIANT


@pytest.mark.parametrize("elapsed,expected", [(13, Verdict.RUNNING), (14, Verdict.NO_DIFFERENCE)])
def test_hard_deadline_with_incomplete_samples(design, now, elapsed, expected):
    day = now.date() - timedelta(days=2)
    data = (DaySpend(day, impressions=100, clicks=1),)
    assert evaluate_experiment(design, data, data, now.date() - timedelta(days=elapsed), now.date()).verdict == expected


def test_recent_and_pre_start_days_are_excluded(design, now):
    start = now.date() - timedelta(days=10)
    days = tuple(
        DaySpend(day, impressions=20_000, clicks=200)
        for day in (now.date(), now.date() - timedelta(days=1), start - timedelta(days=1))
    )
    assert evaluate_experiment(design, days, days, start, now.date()).verdict == Verdict.RUNNING


def test_winner_control_and_unequal_samples(design, arms, now):
    start = now.date() - timedelta(days=10)
    control, variant = arms()
    assert evaluate_experiment(design, control, variant, start, now.date()).verdict == Verdict.VARIANT
    assert evaluate_experiment(design, variant, control, start, now.date()).verdict == Verdict.CONTROL
    control, variant = arms(variant_impressions=20_000, variant_clicks=400, variant_leads=80)
    assert evaluate_experiment(design, control, variant, start, now.date()).verdict == Verdict.VARIANT



def test_ctr_verdict_without_control_clicks_is_saved_to_json(design, arms, now):
    control, variant = arms(control_clicks=0, control_leads=0)
    result = evaluate_experiment(design, control, variant, now.date() - timedelta(days=10), now.date())
    assert result.verdict == Verdict.VARIANT and result.relative_lift is None
    json.dumps(asdict(result), allow_nan=False)

@pytest.mark.parametrize(
    "p,expected",
    [
        (0.049999, Verdict.VARIANT),
        (0.05, Verdict.EXTEND),
        (0.199999, Verdict.EXTEND),
        (0.2, Verdict.NO_DIFFERENCE),
    ],
)
def test_significance_and_extension_boundaries(monkeypatch, design, arms, now, p, expected):
    monkeypatch.setattr(experiments, "proportion_test", lambda *args: p)
    control, variant = arms(control_leads=0, variant_leads=0)
    result = evaluate_experiment(design, control, variant, now.date() - timedelta(days=10), now.date())
    assert result.verdict == expected


@pytest.mark.parametrize("clicks,expected", [(114, Verdict.NO_DIFFERENCE), (115, Verdict.VARIANT)])
def test_fifteen_percent_minimum_lift_inclusive(monkeypatch, design, arms, now, clicks, expected):
    monkeypatch.setattr(experiments, "proportion_test", lambda *args: 0.001)
    control, variant = arms(variant_clicks=clicks, control_leads=0, variant_leads=0)
    assert (
        evaluate_experiment(design, control, variant, now.date() - timedelta(days=10), now.date()).verdict == expected
    )
    reverse = evaluate_experiment(design, variant, control, now.date() - timedelta(days=10), now.date())
    assert reverse.verdict == (Verdict.CONTROL if expected == Verdict.VARIANT else expected)


@pytest.mark.parametrize(
    "leads,guard_p,expected",
    [
        (9, 0.09, Verdict.VARIANT),
        (10, 0.09, Verdict.CLICKBAIT),
        (10, 0.1, Verdict.VARIANT),
    ],
)
def test_conversion_guardrail_min_leads_and_p_boundary(monkeypatch, design, arms, now, leads, guard_p, expected):
    monkeypatch.setattr(experiments, "proportion_test", lambda *args: 0.001 if args[1] == 10_000 else guard_p)
    control, variant = arms(control_leads=leads, variant_leads=0)
    assert (
        evaluate_experiment(design, control, variant, now.date() - timedelta(days=10), now.date()).verdict == expected
    )


def test_real_clickbait_is_not_promoted(design, arms, now):
    control, variant = arms(control_leads=30, variant_leads=5)
    result = evaluate_experiment(design, control, variant, now.date() - timedelta(days=10), now.date())
    assert result.verdict == Verdict.CLICKBAIT
    assert result.guardrail_p_value < 0.1


def test_extension_is_once_fifty_percent_with_separate_significance(monkeypatch, design, arms, now):
    assert extended_target(replace(design, n_per_arm=101)) == 152
    control, variant = arms()
    start = now.date() - timedelta(days=15)
    assert evaluate_experiment(design, control, variant, start, now.date(), extended=True).verdict == Verdict.RUNNING
    control, variant = arms(control_impressions=15_000, variant_impressions=15_000, control_leads=0, variant_leads=0)
    monkeypatch.setattr(experiments, "proportion_test", lambda *args: 0.0099)
    assert evaluate_experiment(design, control, variant, start, now.date(), extended=True).verdict == Verdict.VARIANT
    monkeypatch.setattr(experiments, "proportion_test", lambda *args: 0.01)
    assert (
        evaluate_experiment(design, control, variant, start, now.date(), extended=True).verdict == Verdict.NO_DIFFERENCE
    )
    monkeypatch.setattr(experiments, "proportion_test", lambda *args: 0.1)
    assert (
        evaluate_experiment(design, control, variant, start, now.date(), extended=True).verdict == Verdict.NO_DIFFERENCE
    )
    assert (
        evaluate_experiment(design, (), (), now.date() - timedelta(days=21), now.date(), extended=True).verdict
        == Verdict.NO_DIFFERENCE
    )


def test_aa_thousand_runs_including_the_extension_have_at_most_six_percent_false_wins(now):
    rng = Random(20261026)
    n = sample_size(0.01)
    design = ExperimentDesign(n, 10, 1_000, "text", True, "EXPERIMENT_START")
    day = now.date() - timedelta(days=2)
    start = now.date() - timedelta(days=10)
    false_wins = 0
    for _ in range(1_000):
        a, b = rng.binomialvariate(n, 0.01), rng.binomialvariate(n, 0.01)
        control = (DaySpend(day, impressions=n, clicks=a),)
        variant = (DaySpend(day, impressions=n, clicks=b),)
        result = evaluate_experiment(design, control, variant, start, now.date())
        if result.verdict == Verdict.EXTEND:
            extra = extended_target(design) - n
            control += (DaySpend(day - timedelta(days=1), impressions=extra, clicks=rng.binomialvariate(extra, 0.01)),)
            variant += (DaySpend(day - timedelta(days=1), impressions=extra, clicks=rng.binomialvariate(extra, 0.01)),)
            result = evaluate_experiment(design, control, variant, start, now.date(), extended=True)
        false_wins += result.verdict in (Verdict.VARIANT, Verdict.CONTROL)
    assert false_wins <= 60


def test_invalid_experiment_is_rejected(design, now):
    with pytest.raises(ValueError):
        evaluate_experiment(replace(design, accepted=False), (), (), now.date(), now.date())


def audience_days(start, spend, leads):
    return tuple(DaySpend(start + timedelta(days=offset), spend, 1000, 30, leads) for offset in range(14))


def test_audience_verdict_waits_for_the_full_settled_period(now):
    start = now.date()
    control, variant = audience_days(start, 10_000, 0), audience_days(start, 10_000, 2)
    # Последний день проверки — 14-й, его цифры уточняются ещё 2 дня.
    assert evaluate_audience(control, variant, start, start + timedelta(days=14)).verdict == Verdict.RUNNING
    result = evaluate_audience(control, variant, start, start + timedelta(days=15))
    assert (result.verdict, result.reason_code) == (Verdict.VARIANT, "EXPERIMENT_WIN")
    assert result.p_value < DEFAULT_SETTINGS.test_alpha
    # У контроля ни одного обращения: разница без бесконечности, вывод сохраняется в JSON.
    assert result.relative_lift is None
    json.dumps(asdict(result), allow_nan=False)


def test_audience_needs_enough_leads_and_a_real_price_gap(now):
    start = now.date()
    end = start + timedelta(days=16)
    # 9 обращений на две аудитории — мало для вывода.
    few = evaluate_audience(audience_days(start, 10_000, 0), audience_days(start, 10_000, 0), start, end)
    assert few.verdict == Verdict.NO_DIFFERENCE
    control = audience_days(start, 10_000, 1)
    # 14 против 28 обращений при равном расходе: вдвое дешевле и надёжно.
    assert evaluate_audience(control, audience_days(start, 10_000, 2), start, end).verdict == Verdict.VARIANT
    assert evaluate_audience(audience_days(start, 10_000, 2), control, start, end).verdict == Verdict.CONTROL
    # 14 против 15: разница в цене меньше порога — вывод «разницы нет».
    close = audience_days(start, 10_000, 1)[:13] + (DaySpend(start + timedelta(days=13), 10_000, 1000, 30, 2),)
    assert evaluate_audience(control, close, start, end).verdict == Verdict.NO_DIFFERENCE


def test_audience_design_runs_the_full_period():
    design = design_experiment(0.05, 4000, 80_000, ("text", "image"))
    assert (design.hypothesis, design.estimated_days, design.accepted) == ("audience", 14, True)
