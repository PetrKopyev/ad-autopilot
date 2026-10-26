from math import isclose

import pytest
from hypothesis import given
from hypothesis import strategies as st

from app.domain import stats


@pytest.mark.parametrize("successes,trials", [(0, 0), (0, 40), (3, 40), (40, 40)])
def test_wilson_bounds_and_symmetry(successes, trials):
    lower, upper = stats.wilson_interval(successes, trials)
    reverse_lower, reverse_upper = stats.wilson_interval(trials - successes, trials)
    assert 0 <= lower <= upper <= 1
    assert lower == pytest.approx(1 - reverse_upper)
    assert upper == pytest.approx(1 - reverse_lower)


def test_wilson_known_interval():
    assert stats.wilson_interval(20, 100) == pytest.approx((0.153764, 0.255931), abs=0.00001)
    assert stats.wilson_interval(0, 0) == (0.0, 1.0)


@pytest.mark.parametrize("confidence", [0, 1, -0.1, 1.1])
def test_invalid_confidence(confidence):
    with pytest.raises(ValueError):
        stats.wilson_interval(1, 10, confidence)


@pytest.mark.parametrize("successes,trials", [(-1, 10), (11, 10), (1, -1), (1.0, 10), (True, 10)])
def test_invalid_counts(successes, trials):
    for function in (stats.wilson_interval, stats.two_proportion_z_test, stats.fisher_exact, stats.proportion_test):
        with pytest.raises(ValueError):
            function(successes, trials, 1, 10) if function != stats.wilson_interval else function(successes, trials)


def test_fisher_reference_table():
    assert stats.fisher_exact(1, 10, 11, 14) == pytest.approx(0.00275945618522)
    assert stats.fisher_exact(11, 14, 1, 10) == pytest.approx(0.00275945618522)


@pytest.mark.parametrize("function", [stats.fisher_exact, stats.two_proportion_z_test, stats.proportion_test])
def test_empty_identical_and_degenerate_samples(function):
    assert function(0, 0, 3, 10) == 1
    assert function(0, 10, 0, 20) == pytest.approx(1)
    assert function(10, 10, 20, 20) == pytest.approx(1)
    assert function(10, 100, 20, 200) == pytest.approx(1)


def test_small_expected_cells_use_fisher(monkeypatch):
    monkeypatch.setattr(stats, "fisher_exact", lambda *args: 0.123)
    monkeypatch.setattr(stats, "two_proportion_z_test", lambda *args: 0.456)
    assert stats.proportion_test(4, 100, 4, 100) == 0.123
    assert stats.proportion_test(5, 100, 5, 100) == 0.456


@given(st.integers(0, 100), st.integers(0, 100))
def test_tests_are_symmetric(a, b):
    for function in (stats.two_proportion_z_test, stats.fisher_exact):
        p = function(a, 100, b, 100)
        assert 0 <= p <= 1
        assert isclose(p, function(b, 100, a, 100), abs_tol=1e-10)


def test_sample_size_and_power():
    n = stats.sample_size(0.0125)
    assert 15_000 < n < 17_000
    assert stats.sample_size(0.0125, power=0.9) > n
    assert stats.sample_size(0.0125, relative_lift=0.5) < n
    assert stats.sample_size(0.0125, alpha=0.01) > n


@pytest.mark.parametrize("baseline,lift", [(0, 0.3), (1, 0.3), (0.9, 0.3), (0.01, 0)])
def test_invalid_sample_design(baseline, lift):
    with pytest.raises(ValueError):
        stats.sample_size(baseline, lift)
