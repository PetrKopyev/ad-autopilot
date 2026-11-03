from decimal import Decimal
from string import Formatter

import pytest

from app.domain.explain import (
    MONEY_FIELDS,
    TEMPLATES,
    explain,
    explain_audience,
    explain_decision,
    explain_experiment,
    rubles,
    undone,
)
from app.domain.types import Action, Decision, ExperimentResult, Metrics, Verdict

PERIOD = {"period_start": "2026-11-01", "period_end": "2026-11-14", "ad": "Маникюр рядом с домом"}


@pytest.mark.parametrize(
    "kopecks,expected",
    [
        (0, "0"),
        (1, "0,01"),
        (157, "1,57"),
        (100, "1"),
        (9_999, "99,99"),
        (10_000, "100"),
        (10_049, "100,49"),
        (10_050, "100,50"),
        (2_014_880, "20 148,80"),
        (123_456, "1 234,56"),
        (6_000_075, "60 000,75"),
    ],
)
def test_ruble_format_keeps_kopecks_and_never_rounds_up(kopecks, expected):
    assert rubles(kopecks) == expected


@pytest.mark.parametrize(
    "code,values,expected",
    [
        ("PAUSE_NO_LEADS", {"clicks": 1, **PERIOD}, "нажали 1 раз,"),
        ("PAUSE_NO_LEADS", {"clicks": 3, **PERIOD}, "нажали 3 раза,"),
        ("PAUSE_NO_LEADS", {"clicks": 12, **PERIOD}, "нажали 12 раз,"),
        ("PAUSE_NO_LEADS", {"clicks": 22, **PERIOD}, "нажали 22 раза,"),
        ("PAUSE_TOO_EXPENSIVE", {"spend": 100, "leads": 3, "ref_cpl": 100, **PERIOD}, "получили 3 обращения."),
        ("PAUSE_TOO_EXPENSIVE", {"spend": 100, "leads": 1, "ref_cpl": 100, **PERIOD}, "получили 1 обращение."),
        ("PAUSE_TOO_EXPENSIVE", {"spend": 100, "leads": 11, "ref_cpl": 100, **PERIOD}, "получили 11 обращений."),
        ("EXPERIMENT_START", {"hypothesis": "text", "n_per_arm": 5_371, "days": 1}, "5 371 показ каждого"),
        ("EXPERIMENT_START", {"hypothesis": "text", "n_per_arm": 5_372, "days": 2}, "примерно 2 дня."),
        ("EXPERIMENT_TOO_LONG", {"days": 21}, "примерно 21 день."),
        ("BUDGET_OPPORTUNITY", {"extra_budget": 10_000, "extra_leads": Decimal("1.5")}, "1,5 обращения в день"),
        ("BUDGET_OPPORTUNITY", {"extra_budget": 10_000, "extra_leads": Decimal("2")}, "2 обращения в день"),
        ("BUDGET_OPPORTUNITY", {"extra_budget": 10_000, "extra_leads": Decimal("62.96")}, "63 обращения в день"),
    ],
)
def test_numbers_agree_with_words(code, values, expected):
    assert expected in explain(code, values)


@pytest.mark.parametrize(
    "kind,entity_type,params,expected",
    [
        (
            "pause",
            "ad",
            {"ad": "Маникюр рядом"},
            "Вы отменили остановку объявления «Маникюр рядом». Вернули прежние настройки.",
        ),
        ("pause", "ad", {}, "Вы отменили остановку объявления. Вернули прежние настройки."),
        ("pause", "campaign", {}, "Вы отменили остановку рекламы. Вернули прежние настройки."),
        (
            "budget",
            "group",
            {"group": "Педикюр"},
            "Вы отменили повышение дневного лимита группы «Педикюр». Вернули прежние настройки.",
        ),
        ("experiment_verdict", "ad", {}, "Вы отменили вывод сравнения вариантов. Вернули прежние настройки."),
    ],
)
def test_revert_names_what_was_undone_and_does_not_read_as_a_new_stop(kind, entity_type, params, expected):
    assert explain("REVERT", change=undone(kind, entity_type, params)) == expected


def test_pause_and_scale_name_the_ad_and_the_group():
    assert "на объявление «Маникюр рядом с домом» нажали 3 раза" in explain("PAUSE_NO_LEADS", PERIOD, clicks=3)
    text = explain("SCALE_UP", group="Педикюр", campaign="Автопилот — Директ", old_limit=47_500, new_limit=50_300)
    assert text == (
        "Объявления группы «Педикюр» (кампания «Автопилот — Директ») приносят обращения по подходящей цене. "
        "Повысили дневной лимит этой группы с 475 до 503 ₽."
    )


@pytest.mark.parametrize("code", TEMPLATES)
def test_every_reason_has_a_complete_template(code):
    keys = {name for _, name, _, _ in Formatter().parse(TEMPLATES[code]) if name is not None}
    values = {key: 12_345 if key in MONEY_FIELDS else "пример" for key in keys}
    text = explain(code, values)
    assert "{" not in text and "}" not in text
    assert not any(word in text.lower() for word in ("cpa", "ctr", "семантика", "ставка"))


def test_decision_reason_is_deterministic_and_does_not_mutate_numbers():
    numbers = {"clicks": 80, **PERIOD}
    decision = Decision("ad", Action.PAUSE, "PAUSE_NO_LEADS", numbers=numbers)
    assert "80 раз" in explain_decision(decision)
    assert numbers == {"clicks": 80, **PERIOD}
    assert explain_decision(decision) == explain_decision(decision)


def test_experiment_explanation_contains_actual_counts_and_rates():
    text = explain(
        "EXPERIMENT_WIN",
        winner="Б",
        loser="А",
        impressions=11_400,
        clicks=190,
        rate=Decimal("1.7"),
        other_impressions=11_400,
        other_clicks=137,
        other_rate=Decimal("1.2"),
        confidence="Разница почти наверняка не случайна",
    )
    assert "11 400 раз" in text and "190 раз (1,7%)" in text and "137 нажатий (1,2%)" in text
    values = {"winner": "Б", "loser": "А", "rate": 1, "other_rate": 1, "confidence": "Вывод"}
    text = explain("EXPERIMENT_WIN", values, impressions=1_002, clicks=2, other_impressions=1_000, other_clicks=3)
    assert "показывали 1 002 раза, на него нажали 2 раза" in text
    assert explain("EXPERIMENT_START", hypothesis="text", n_per_arm=5_489, days=12).startswith(
        "Проверяем другой текст: сравним по 5 489 показов"
    )


def test_unknown_code_or_missing_number_fails_instead_of_inventing_values():
    with pytest.raises(KeyError):
        explain("UNKNOWN")
    with pytest.raises(KeyError):
        explain("PAUSE_NO_LEADS")


def test_experiment_copy_is_derived_from_the_winning_arm():
    result = ExperimentResult(Verdict.VARIANT, "EXPERIMENT_WIN", 0.00001)
    a, b = Metrics(10_000, 11_400, 137, 10), Metrics(10_000, 11_400, 190, 15)
    text = explain_experiment(result, a, b)
    assert "Объявление Б" in text and "190 раз (1,7%)" in text
    assert "11 400 показов и 137 нажатий (1,2%) у А" in text
    assert "Разница почти наверняка не случайна." in text
    assert "Оставили Б, А остановили" in text and "вероятност" not in text.lower()
    reverse = ExperimentResult(Verdict.CONTROL, "EXPERIMENT_WIN", 0.03)
    assert "Объявление А" in explain_experiment(reverse, b, a)
    assert "Разница, скорее всего, не случайна" in explain_experiment(reverse, b, a)


def test_no_difference_verdict_shows_impressions_clicks_and_rates_of_both_arms():
    """В3: «разницы нет» — самый частый исход, по RULES в тексте обе стороны с цифрами."""
    result = ExperimentResult(Verdict.NO_DIFFERENCE, "EXPERIMENT_NO_DIFF", 0.4, 0.1)
    a, b = Metrics(10_000, 11_400, 137, 10), Metrics(10_300, 11_900, 152, 12)
    text = explain_experiment(result, a, b)
    assert "объявление А показали 11 400, нажали 137 (1,2%)" in text
    assert "объявление Б — 11 900 показов и 152 нажатий (1,3%)" in text
    assert "Оставили прежнее, новое остановили" in text


def test_clickbait_verdict_shows_click_and_lead_rates_of_both_arms():
    result = ExperimentResult(Verdict.CLICKBAIT, "EXPERIMENT_CLICKBAIT", 0.001, 0.5, 0.01)
    a, b = Metrics(10_000, 10_000, 100, 20), Metrics(10_000, 10_000, 200, 5)
    assert explain_experiment(result, a, b) == (
        "На объявление Б нажимают чаще: 2,0% показов против 1,0% у А. Но в обращения эти нажатия переходят реже: "
        "2,5% (5 обращений из 200 нажатий) против 20,0% (20 обращений из 100 нажатий). "
        "Оставили прежнее объявление А, Б остановили."
    )


def test_hypothesis_names_and_extension_are_plain_russian():
    text = explain("EXPERIMENT_START", hypothesis="image", n_per_arm=1_000, days=10)
    assert "Проверяем другую картинку" in text
    assert "50%" in explain_experiment(ExperimentResult(Verdict.EXTEND, "EXPERIMENT_EXTEND"), Metrics(), Metrics())


@pytest.mark.parametrize(
    "start,end,expected",
    [
        ("2026-11-05", "2026-11-05", "5 ноября"),
        ("2026-11-01", "2026-11-14", "С 1 по 14 ноября"),
        ("2026-10-25", "2026-11-07", "С 25 октября по 7 ноября"),
        ("2026-12-25", "2027-01-07", "С 25 декабря 2026 по 7 января 2027"),
    ],
)
def test_pause_reason_names_the_days_it_was_decided_on(start, end, expected):
    # На экране объявления расход считается за свой период: без дат «Потратили 211 ₽» спорит с «Расход 0 ₽».
    text = explain(
        "PAUSE_TOO_EXPENSIVE", spend=21_100, leads=3, ref_cpl=5_000, period_start=start, period_end=end, ad="Маникюр"
    )
    assert text.startswith(f"{expected} на объявление «Маникюр» потратили 211 ₽, получили 3 обращения.")


def test_audience_verdict_names_both_audiences_with_lead_prices():
    labels = ("ищут с названием города", "ищут без названия города")
    control, variant = Metrics(1_400_000, 9000, 300, 7), Metrics(1_400_000, 9000, 300, 14)
    win = ExperimentResult(Verdict.VARIANT, "EXPERIMENT_WIN", 0.004, 1.0)
    assert explain_audience(win, control, variant, labels) == (
        "Сравнили аудитории: «ищут с названием города» — 7 обращений по 2 000 ₽, «ищут без названия города» — "
        "14 обращений по 1 000 ₽. Разница почти наверняка не случайна. Обращения дешевле у аудитории «ищут без "
        "названия города» — её оставили, объявление для «ищут с названием города» остановили."
    )
    same = ExperimentResult(Verdict.NO_DIFFERENCE, "EXPERIMENT_NO_DIFF", 0.4, 0.1)
    assert "Убедительной разницы в цене обращения нет" in explain_audience(same, control, variant, labels)
    few = explain_audience(same, Metrics(500_000, 900, 30, 0), Metrics(500_000, 900, 30, 3), labels)
    assert "обращений слишком мало" in few and "ни одного обращения за 5 000 ₽" in few
    # VK: «все возрасты» включают «25–54 года» — вывод не выдаёт это за две разные группы людей.
    vk = explain_audience(win, control, variant, ("все возрасты", "25–54 года"), overlap=True)
    assert "Аудитории пересекаются" in vk and "сравнение настройки показа" in vk
    assert "пересекаются" not in explain_audience(win, control, variant, labels)


def test_undone_names_experiment_and_campaign_launch():
    assert undone("launch", "ad", {"hypothesis": "text", "n_per_arm": 100, "days": 7}) == "сравнение вариантов"
    assert undone("launch", "campaign", {"budget": 100, "campaign": "Маникюр"}) == "запуск рекламы «Маникюр»"
    assert explain("REVERT", change=undone("launch", "ad", {"hypothesis": "image"})) == (
        "Вы отменили сравнение вариантов. Вернули прежние настройки."
    )
