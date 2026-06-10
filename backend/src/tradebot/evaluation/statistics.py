"""Bootstrap uncertainty and multiple-comparison correction for sweeps.

A sweep compares several candidates and crowns the best-looking one, so
its apparent edge is inflated by selection: with enough candidates, one of
them wins by luck alone. Two defenses live here, both deliberately free of
distributional assumptions (per-trade R is fat-tailed and skewed):

- **Bootstrap resampling** turns a candidate's R-multiples into a
  confidence interval on expectancy and a one-sided p-value for "the
  challenger's true expectancy exceeds the baseline's".
- **Bonferroni correction** divides the significance level by the number
  of comparisons the sweep made, so a grid of K variants does not get K
  chances at a 5% fluke.

R-multiples are ratios of money and stay ``Decimal`` end to end. Every
resample is driven by a seeded ``random.Random``, so a sweep report is
reproducible bit for bit from its config.
"""

from __future__ import annotations

import random
from collections.abc import Sequence
from decimal import ROUND_HALF_EVEN, Decimal

from pydantic import BaseModel, ConfigDict

from tradebot.core.models import ACCOUNTING_RESOLUTION

BOOTSTRAP_RESAMPLES = 1_000
"""Resamples per bootstrap. Enough for a stable 95% interval on the
hundreds of trades a sweep produces; cheap enough to run in the worker."""

BASE_SIGNIFICANCE = Decimal("0.05")
"""The uncorrected one-sided significance level a single comparison must
clear; Bonferroni divides it by the number of comparisons made."""

MIN_BOOTSTRAP_SAMPLES = 2
"""Below this many trades a resample is the sample; no interval exists."""

_CI_LOWER_QUANTILE = 0.025
_CI_UPPER_QUANTILE = 0.975


class ExpectancyInterval(BaseModel):
    """A 95% bootstrap confidence interval on mean R per trade."""

    model_config = ConfigDict(frozen=True)

    low_r: Decimal
    high_r: Decimal


def bootstrap_expectancy_interval(
    r_values: Sequence[Decimal], seed: int, resamples: int = BOOTSTRAP_RESAMPLES
) -> ExpectancyInterval | None:
    """95% percentile-bootstrap interval on expectancy, or ``None`` if too thin.

    Percentile method on the resampled means: rank the ``resamples`` means
    and read the 2.5th and 97.5th percentiles. ``None`` (rather than a
    degenerate point interval) below :data:`MIN_BOOTSTRAP_SAMPLES`.
    """
    if len(r_values) < MIN_BOOTSTRAP_SAMPLES:
        return None
    rng = random.Random(seed)
    means = sorted(_resampled_mean(r_values, rng) for _ in range(resamples))
    return ExpectancyInterval(
        low_r=_quantize(means[int(_CI_LOWER_QUANTILE * (len(means) - 1))]),
        high_r=_quantize(means[int(_CI_UPPER_QUANTILE * (len(means) - 1))]),
    )


def superiority_p_value(
    challenger_r: Sequence[Decimal],
    baseline_r: Sequence[Decimal],
    seed: int,
    resamples: int = BOOTSTRAP_RESAMPLES,
) -> Decimal | None:
    """One-sided bootstrap p-value that the challenger beats the baseline.

    Resamples both R series independently (they come from different trade
    sets, so pairing is impossible) and counts how often the challenger's
    mean fails to exceed the baseline's: the fraction of resamples in which
    the apparent edge vanishes. ``None`` when either side is too thin to
    resample — a missing p-value must read as "unknown", never "passed".
    """
    if len(challenger_r) < MIN_BOOTSTRAP_SAMPLES or len(baseline_r) < MIN_BOOTSTRAP_SAMPLES:
        return None
    rng = random.Random(seed)
    not_superior = sum(
        1
        for _ in range(resamples)
        if _resampled_mean(challenger_r, rng) <= _resampled_mean(baseline_r, rng)
    )
    return _quantize(Decimal(not_superior) / Decimal(resamples))


def corrected_significance(comparisons: int) -> Decimal:
    """Bonferroni-corrected one-sided significance level for one comparison.

    ``comparisons`` is how many variants challenged the baseline in the
    sweep — every one of them had a shot at winning training, so every one
    of them spends part of the error budget.
    """
    if comparisons < 1:
        raise ValueError(f"comparisons must be >= 1, got {comparisons}")
    return BASE_SIGNIFICANCE / Decimal(comparisons)


def _resampled_mean(values: Sequence[Decimal], rng: random.Random) -> Decimal:
    resample = rng.choices(values, k=len(values))
    return sum(resample, Decimal(0)) / Decimal(len(resample))


def _quantize(value: Decimal) -> Decimal:
    return value.quantize(ACCOUNTING_RESOLUTION, rounding=ROUND_HALF_EVEN)
