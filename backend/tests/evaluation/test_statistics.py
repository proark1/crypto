"""Bootstrap interval, superiority test, and Bonferroni correction."""

from decimal import Decimal

import pytest

from tradebot.evaluation.statistics import (
    bootstrap_expectancy_interval,
    corrected_significance,
    superiority_p_value,
)


def r(values: list[str]) -> list[Decimal]:
    return [Decimal(value) for value in values]


class TestBootstrapExpectancyInterval:
    def test_interval_brackets_the_sample_mean(self) -> None:
        values = r(["-1", "-0.5", "0.2", "0.8", "1.5", "2.0", "-1", "0.3", "0.6", "1.1"])
        interval = bootstrap_expectancy_interval(values, seed=7)

        assert interval is not None
        mean = sum(values, Decimal(0)) / Decimal(len(values))
        assert interval.low_r <= mean <= interval.high_r
        assert interval.low_r < interval.high_r

    def test_same_seed_reproduces_the_interval_exactly(self) -> None:
        values = r(["-1", "0.5", "1.5", "2.0", "-0.3"])
        first = bootstrap_expectancy_interval(values, seed=7)
        second = bootstrap_expectancy_interval(values, seed=7)

        assert first == second

    def test_identical_values_collapse_to_a_point_interval(self) -> None:
        interval = bootstrap_expectancy_interval(r(["0.5"] * 20), seed=7)

        assert interval is not None
        assert interval.low_r == interval.high_r == Decimal("0.5")

    def test_too_few_samples_return_none_not_a_fake_interval(self) -> None:
        assert bootstrap_expectancy_interval(r(["1.0"]), seed=7) is None
        assert bootstrap_expectancy_interval([], seed=7) is None


class TestSuperiorityPValue:
    def test_clear_superiority_yields_a_small_p_value(self) -> None:
        challenger = r(["1.5", "2.0", "1.8", "1.2", "1.6"] * 4)
        baseline = r(["-0.5", "-1.0", "0.1", "-0.2", "0.0"] * 4)

        p_value = superiority_p_value(challenger, baseline, seed=7)

        assert p_value is not None
        assert p_value < Decimal("0.01")

    def test_identical_distributions_are_not_called_superior(self) -> None:
        values = r(["-1", "0.5", "1.5", "-0.3", "0.8"] * 4)

        p_value = superiority_p_value(values, list(values), seed=7)

        assert p_value is not None
        assert p_value > Decimal("0.05")

    def test_same_seed_reproduces_the_p_value_exactly(self) -> None:
        challenger = r(["1.0", "0.5", "1.5"])
        baseline = r(["0.2", "-0.1", "0.4"])

        assert superiority_p_value(challenger, baseline, seed=7) == superiority_p_value(
            challenger, baseline, seed=7
        )

    def test_thin_evidence_returns_none_never_passed(self) -> None:
        assert superiority_p_value(r(["1.0"]), r(["0.1", "0.2"]), seed=7) is None
        assert superiority_p_value(r(["1.0", "2.0"]), r(["0.1"]), seed=7) is None


class TestCorrectedSignificance:
    def test_divides_the_budget_by_the_comparisons_made(self) -> None:
        assert corrected_significance(1) == Decimal("0.05")
        assert corrected_significance(5) == Decimal("0.01")

    def test_zero_comparisons_is_a_caller_bug(self) -> None:
        with pytest.raises(ValueError, match="comparisons"):
            corrected_significance(0)
