from math import ceil, erfc, exp, fsum, lgamma, log, sqrt
from statistics import NormalDist

from app.domain.settings import DEFAULT_SETTINGS


def _counts(successes: int, trials: int) -> None:
    if type(successes) is not int or type(trials) is not int or not 0 <= successes <= trials:
        raise ValueError("Expected integer counts: 0 <= successes <= trials")


def wilson_interval(
    successes: int, trials: int, confidence: float = DEFAULT_SETTINGS.confidence
) -> tuple[float, float]:
    _counts(successes, trials)
    if not 0 < confidence < 1:
        raise ValueError("Confidence must be between 0 and 1")
    if not trials:
        return 0.0, 1.0
    z = NormalDist().inv_cdf((1 + confidence) / 2)
    p = successes / trials
    denominator = 1 + z * z / trials
    center = (p + z * z / (2 * trials)) / denominator
    radius = z * sqrt(p * (1 - p) / trials + z * z / (4 * trials * trials)) / denominator
    return max(0.0, center - radius), min(1.0, center + radius)


def two_proportion_z_test(successes_a: int, trials_a: int, successes_b: int, trials_b: int) -> float:
    _counts(successes_a, trials_a)
    _counts(successes_b, trials_b)
    if not trials_a or not trials_b:
        return 1.0
    pooled = (successes_a + successes_b) / (trials_a + trials_b)
    variance = pooled * (1 - pooled) * (1 / trials_a + 1 / trials_b)
    if variance == 0:
        return 1.0
    z = abs(successes_a / trials_a - successes_b / trials_b) / sqrt(variance)
    return erfc(z / sqrt(2))


def fisher_exact(successes_a: int, trials_a: int, successes_b: int, trials_b: int) -> float:
    """Two-sided conditional test: sum tables at least as unlikely as the observed one."""
    _counts(successes_a, trials_a)
    _counts(successes_b, trials_b)
    if not trials_a or not trials_b:
        return 1.0
    successes = successes_a + successes_b
    total = trials_a + trials_b

    def log_choose(n: int, k: int) -> float:
        return lgamma(n + 1) - lgamma(k + 1) - lgamma(n - k + 1)

    denominator = log_choose(total, successes)

    def log_probability(a: int) -> float:
        return log_choose(trials_a, a) + log_choose(trials_b, successes - a) - denominator

    observed = log_probability(successes_a)
    probabilities = (
        exp(log_p)
        for a in range(max(0, successes - trials_b), min(trials_a, successes) + 1)
        if (log_p := log_probability(a)) <= observed + 1e-9
    )
    return min(1.0, fsum(probabilities))


def binomial_test(successes: int, trials: int, probability: float) -> float:
    """Two-sided exact test: sum outcomes at least as unlikely as the observed one."""
    _counts(successes, trials)
    if not trials or probability in (0, 1):
        return 1.0

    def log_probability(k: int) -> float:
        return (
            lgamma(trials + 1)
            - lgamma(k + 1)
            - lgamma(trials - k + 1)
            + k * log(probability)
            + (trials - k) * log(1 - probability)
        )

    observed = log_probability(successes)
    return min(1.0, fsum(exp(log_p) for k in range(trials + 1) if (log_p := log_probability(k)) <= observed + 1e-9))


def proportion_test(
    successes_a: int,
    trials_a: int,
    successes_b: int,
    trials_b: int,
    expected_min: int = DEFAULT_SETTINGS.fisher_expected_min,
) -> float:
    _counts(successes_a, trials_a)
    _counts(successes_b, trials_b)
    if not trials_a or not trials_b:
        return 1.0
    pooled = (successes_a + successes_b) / (trials_a + trials_b)
    expected = (trials_a * pooled, trials_b * pooled, trials_a * (1 - pooled), trials_b * (1 - pooled))
    test = fisher_exact if min(expected) < expected_min else two_proportion_z_test
    return test(successes_a, trials_a, successes_b, trials_b)


def sample_size(
    baseline: float,
    relative_lift: float = DEFAULT_SETTINGS.test_design_lift,
    alpha: float = DEFAULT_SETTINGS.test_alpha,
    power: float = DEFAULT_SETTINGS.test_power,
) -> int:
    alternative = baseline * (1 + relative_lift)
    if not 0 < baseline < alternative < 1 or not 0 < alpha < 1 or not 0.5 < power < 1:
        raise ValueError("Invalid baseline, lift, significance or power")
    normal = NormalDist()
    mean = (baseline + alternative) / 2
    numerator = normal.inv_cdf(1 - alpha / 2) * sqrt(2 * mean * (1 - mean)) + normal.inv_cdf(power) * sqrt(
        baseline * (1 - baseline) + alternative * (1 - alternative)
    )
    return ceil(numerator**2 / (alternative - baseline) ** 2)
