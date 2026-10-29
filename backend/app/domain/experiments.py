from collections import Counter
from collections.abc import Sequence
from datetime import date, datetime, timedelta
from decimal import ROUND_CEILING, Decimal
from math import ceil
from statistics import median

from app.domain.rules import business_history, can_change, classify, has_anomaly, settled_days
from app.domain.settings import DEFAULT_SETTINGS, DomainSettings
from app.domain.stats import binomial_test, proportion_test, sample_size
from app.domain.types import (
    AdSnapshot,
    Classification,
    DaySpend,
    ExperimentDesign,
    ExperimentResult,
    Verdict,
    nonnegative_int,
    total_metrics,
)


def select_borderline(
    ads: Sequence[AdSnapshot],
    now: datetime,
    reference_cpl: int | None,
    active_test_groups: Sequence[str] = (),
    business_days: Sequence[DaySpend] = (),
    settings: DomainSettings = DEFAULT_SETTINGS,
) -> tuple[AdSnapshot, ...]:
    business_days = business_days or business_history(ads)
    if has_anomaly(business_days, now.date(), settings):
        return ()
    slots = settings.max_tests_per_business - len(active_test_groups)
    counts = Counter(active_test_groups)
    totals = {ad.id: total_metrics(settled_days(ad.days, now.date(), settings)) for ad in ads}
    result = []
    for ad in sorted(ads, key=lambda item: item.id):
        if len(result) >= slots:
            break
        if counts[ad.group_id] >= settings.max_tests_per_group or not can_change(ad, now, settings):
            continue
        ctrs = [
            totals[item.id].ctr
            for item in ads
            if item.group_id == ad.group_id and item.active and totals[item.id].impressions
        ]
        assessment = classify(
            ad,
            now,
            reference_cpl,
            median(ctrs) if ctrs else None,
            any(item.leads for item in totals.values()),
            settings=settings,
        )
        if assessment.classification == Classification.BORDERLINE:
            result.append(ad)
            counts[ad.group_id] += 1
    return tuple(result)


def next_hypothesis(
    completed: Sequence[str], daily_budget: int, settings: DomainSettings = DEFAULT_SETTINGS
) -> str | None:
    nonnegative_int(daily_budget, "daily_budget")
    for hypothesis in ("text", "image"):
        if hypothesis not in completed:
            return hypothesis
    if "audience" not in completed and daily_budget >= settings.audience_min_daily_budget:
        return "audience"
    return None


def design_experiment(
    baseline_ctr: float,
    group_daily_impressions: int,
    group_daily_budget: int,
    completed_hypotheses: Sequence[str] = (),
    settings: DomainSettings = DEFAULT_SETTINGS,
) -> ExperimentDesign:
    nonnegative_int(group_daily_impressions, "group_daily_impressions")
    nonnegative_int(group_daily_budget, "group_daily_budget")
    hypothesis = next_hypothesis(completed_hypotheses, group_daily_budget, settings)
    n = sample_size(baseline_ctr, settings.test_design_lift, settings.test_alpha, settings.test_power)
    test_impressions = int(group_daily_impressions * settings.test_budget_share)
    days = ceil(2 * n / test_impressions) + settings.settlement_days if test_impressions else settings.test_max_days + 1
    if hypothesis == "audience":
        # Аудитории сравниваются по цене обращения за полный срок проверки, а не по набранным показам.
        days = settings.test_max_days
    budget = int(group_daily_budget * settings.test_budget_share)
    accepted = hypothesis is not None and budget > 0 and days <= settings.test_max_days
    reason = (
        "EXPERIMENT_START" if accepted else "EXPERIMENT_TOO_LONG" if days > settings.test_max_days else "NO_HYPOTHESIS"
    )
    return ExperimentDesign(n, days, budget, hypothesis or "", accepted, reason)


def extended_target(design: ExperimentDesign, settings: DomainSettings = DEFAULT_SETTINGS) -> int:
    return int((design.n_per_arm * settings.test_extension_factor).to_integral_value(rounding=ROUND_CEILING))


def evaluate_experiment(
    design: ExperimentDesign,
    control_days: Sequence[DaySpend],
    variant_days: Sequence[DaySpend],
    started_on: date,
    today: date,
    extended: bool = False,
    settings: DomainSettings = DEFAULT_SETTINGS,
) -> ExperimentResult:
    if not design.accepted or today < started_on:
        raise ValueError("A verdict requires an accepted, started experiment")
    end = today - timedelta(days=settings.settlement_days)

    def aggregate(days: Sequence[DaySpend]):
        if len({day.day for day in days}) != len(days):
            raise ValueError("Experiment days must be unique")
        return total_metrics(tuple(day for day in days if started_on <= day.day <= end))

    control, variant = aggregate(control_days), aggregate(variant_days)
    target = extended_target(design, settings) if extended else design.n_per_arm
    deadline = ceil(settings.test_max_days * settings.test_extension_factor) if extended else settings.test_max_days
    if min(control.impressions, variant.impressions) < target and (today - started_on).days < deadline:
        return ExperimentResult(Verdict.RUNNING, "EXPERIMENT_RUNNING")
    if not control.impressions or not variant.impressions:
        return ExperimentResult(Verdict.NO_DIFFERENCE, "EXPERIMENT_NO_DIFF", 1.0)
    p = proportion_test(
        control.clicks, control.impressions, variant.clicks, variant.impressions, settings.fisher_expected_min
    )
    a, b = float(control.ctr), float(variant.ctr)
    # Ни одного нажатия у контроля: относительной разницы нет (бесконечность не сохранить в JSON).
    lift = b / a - 1 if a else None if b else 0.0
    # A second look spends at most another 1%: the two-look A/A error is bounded by 6%.
    alpha = settings.test_extension_alpha if extended else settings.test_alpha
    required_ratio = 1 + Decimal(str(settings.test_win_lift))
    variant_wins = (
        b > a and variant.clicks * control.impressions >= control.clicks * variant.impressions * required_ratio
    )
    control_wins = (
        a > b and control.clicks * variant.impressions >= variant.clicks * control.impressions * required_ratio
    )
    if p < alpha and (variant_wins or control_wins):
        winner, loser = (variant, control) if variant_wins else (control, variant)
        guard_p = None
        if control.leads + variant.leads >= settings.guardrail_min_leads:
            guard_p = proportion_test(
                control.leads, control.clicks, variant.leads, variant.clicks, settings.fisher_expected_min
            )
            worse_conversion = winner.leads * loser.clicks < loser.leads * winner.clicks
            if worse_conversion and guard_p < settings.guardrail_alpha:
                return ExperimentResult(Verdict.CLICKBAIT, "EXPERIMENT_CLICKBAIT", p, lift, guard_p)
        return ExperimentResult(
            Verdict.VARIANT if variant_wins else Verdict.CONTROL, "EXPERIMENT_WIN", p, lift, guard_p
        )
    if not extended and settings.test_alpha <= p < settings.test_extension_p:
        return ExperimentResult(Verdict.EXTEND, "EXPERIMENT_EXTEND", p, lift)
    return ExperimentResult(Verdict.NO_DIFFERENCE, "EXPERIMENT_NO_DIFF", p, lift)


def audience_end(started_on: date, settings: DomainSettings = DEFAULT_SETTINGS) -> date:
    """Последний день проверки аудитории; вывод — когда цифры этого дня уточнятся."""
    return started_on + timedelta(days=settings.test_max_days - 1)


def evaluate_audience(
    control_days: Sequence[DaySpend],
    variant_days: Sequence[DaySpend],
    started_on: date,
    today: date,
    settings: DomainSettings = DEFAULT_SETTINGS,
) -> ExperimentResult:
    """Аудитории сравниваются по цене обращения один раз, за весь срок проверки: без подглядывания ошибка вывода
    остаётся в пределах test_alpha. При равной цене доля обращений варианта равна его доле расхода; точный
    биномиальный тест проверяет отклонение, а победитель должен быть дешевле хотя бы на test_win_lift."""
    end = audience_end(started_on, settings)
    if today - timedelta(days=settings.settlement_days) < end:
        return ExperimentResult(Verdict.RUNNING, "EXPERIMENT_RUNNING")

    def aggregate(days: Sequence[DaySpend]):
        if len({day.day for day in days}) != len(days):
            raise ValueError("Experiment days must be unique")
        return total_metrics(tuple(day for day in days if started_on <= day.day <= end))

    control, variant = aggregate(control_days), aggregate(variant_days)
    leads = control.leads + variant.leads
    if leads < settings.audience_min_leads or not control.spend or not variant.spend:
        return ExperimentResult(Verdict.NO_DIFFERENCE, "EXPERIMENT_NO_DIFF", 1.0)
    p = binomial_test(variant.leads, leads, variant.spend / (control.spend + variant.spend))
    # Обращений на рубль: у варианта control.spend * variant.leads против variant.spend * control.leads у контроля.
    a, b = control.leads * variant.spend, variant.leads * control.spend
    # У контроля ни одного обращения: относительной разницы нет (бесконечность не сохранить в JSON).
    lift = b / a - 1 if a else None
    required_ratio = 1 + Decimal(str(settings.test_win_lift))
    if p < settings.test_alpha and (b >= a * required_ratio or a >= b * required_ratio):
        return ExperimentResult(Verdict.VARIANT if b > a else Verdict.CONTROL, "EXPERIMENT_WIN", p, lift)
    return ExperimentResult(Verdict.NO_DIFFERENCE, "EXPERIMENT_NO_DIFF", p, lift)
