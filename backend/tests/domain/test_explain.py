from decimal import Decimal
from string import Formatter

import pytest

from app.domain.explain import MONEY_FIELDS, TEMPLATES, explain, explain_decision, explain_experiment, rubles
from app.domain.types import Action, Decision, ExperimentResult, Metrics, Verdict


@pytest.mark.parametrize("kopecks,expected", [(0, "0"), (1, "0,01"), (100, "1"), (123_456, "1 234,56")])
def test_ruble_format_preserves_kopecks(kopecks, expected):
    assert rubles(kopecks) == expected


@pytest.mark.parametrize("code", TEMPLATES)
def test_every_reason_has_a_complete_template(code):
    keys = {name for _, name, _, _ in Formatter().parse(TEMPLATES[code]) if name is not None}
    values = {key: 12_345 if key in MONEY_FIELDS else "пример" for key in keys}
    text = explain(code, values)
    assert "{" not in text and "}" not in text
    assert not any(word in text.lower() for word in ("cpa", "ctr", "семантика", "ставка"))


def test_decision_reason_is_deterministic_and_does_not_mutate_numbers():
    numbers = {"clicks": 80}
    decision = Decision("ad", Action.PAUSE, "PAUSE_NO_LEADS", numbers=numbers)
    assert "80 раз" in explain_decision(decision)
    assert numbers == {"clicks": 80}
    assert explain_decision(decision) == explain_decision(decision)


def test_experiment_explanation_contains_actual_counts_and_rates():
    text = explain(
        "EXPERIMENT_WIN",
        winner="Б",
        loser="А",
        impressions=11_400,
        clicks=190,
        rate=Decimal("1.7"),
        other_rate=Decimal("1.2"),
        p_percent=Decimal("0.3"),
    )
    assert "11400 раз" in text and "190 раз (1,7%)" in text and "1,2%" in text and "0,3%" in text


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
    assert "1,2% у А" in text and "<0,1%" in text
    assert "Оставили Б, А остановили" in text
    reverse = ExperimentResult(Verdict.CONTROL, "EXPERIMENT_WIN", 0.01)
    assert "Объявление А" in explain_experiment(reverse, b, a)


def test_hypothesis_names_and_extension_are_plain_russian():
    text = explain("EXPERIMENT_START", hypothesis="image", n_per_arm=1_000, days=10)
    assert "Проверяем другую картинку" in text
    assert "50%" in explain_experiment(ExperimentResult(Verdict.EXTEND, "EXPERIMENT_EXTEND"), Metrics(), Metrics())
