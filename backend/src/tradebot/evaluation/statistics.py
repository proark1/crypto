"""Bootstrap uncertainty and multiple-comparison correction for sweeps.

A sweep compares several candidates and crowns the best-looking one, so
its apparent edge is inflated by selection: with enough candidates, one of
them wins by luck alone. Two defenses live here, both deliberately free of
distributional assumptions (per-trade R is fat-tailed and skewed):

- **Block bootstrap resampling** turns a candidate's R-multiples into a
  confidence interval on expectancy and a one-sided p-value for "the
  challenger's true expectancy exceeds the baseline's".
- **Bonferroni correction** divides the significance level by the number
  of comparisons the sweep made, so a grid of K variants does not get K
  chances at a 5% fluke.

The resampling is a **moving-block** bootstrap, not the textbook i.i.d.
one. The R series is *not* independent: scenarios are sampled on a short
stride with a multi-candle horizon, so consecutive scenarios overlap and a
single price move spawns clusters of correlated trades. An i.i.d. resample
(draw each trade independently) ignores that dependence, understates the
true spread of the mean, and so hands out p-values that are too small —
exactly how a fluke gets called "validated". Resampling contiguous blocks
of length ``O(n**(1/3))`` (the standard rate for the mean of a dependent
series) preserves the within-cluster correlation, so the resampled means
spread as widely as the dependent data really warrants. It is strictly
more conservative than i.i.d. and collapses to it only for tiny samples.

R-multiples are ratios of money and stay ``Decimal`` end to end. Every
resample is driven by a seeded ``random.Random``, so a sweep report is
reproducible bit for bit from its config.
"""

from __future__ import annotations

import math
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
    block = _block_size(len(r_values))
    means = sorted(_resampled_mean(r_values, rng, block) for _ in range(resamples))
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
    sets, so pairing is impossible), each with its own block length, and
    counts how often the challenger's mean fails to exceed the baseline's:
    the fraction of resamples in which the apparent edge vanishes. ``None``
    when either side is too thin to resample — a missing p-value must read
    as "unknown", never "passed".
    """
    if len(challenger_r) < MIN_BOOTSTRAP_SAMPLES or len(baseline_r) < MIN_BOOTSTRAP_SAMPLES:
        return None
    rng = random.Random(seed)
    challenger_block = _block_size(len(challenger_r))
    baseline_block = _block_size(len(baseline_r))
    not_superior = sum(
        1
        for _ in range(resamples)
        if _resampled_mean(challenger_r, rng, challenger_block)
        <= _resampled_mean(baseline_r, rng, baseline_block)
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


def _block_size(n: int) -> int:
    """Moving-block length for ``n`` observations.

    ``round(n ** (1/3))`` is the standard optimal rate for the bootstrap of
    a dependent series' mean (Politis & Romano), floored at 1. It grows
    slowly — 1 trade at n<3, ~3 at n=16, ~5 at n=125, ~7 at n=343 — so a
    real sweep (hundreds of trades) blocks enough to span a cluster of
    overlapping-scenario trades, while a tiny sample collapses to the
    ordinary i.i.d. resample (block length 1) rather than over-blocking.
    """
    return max(1, round(math.pow(n, 1 / 3)))


def _resampled_mean(values: Sequence[Decimal], rng: random.Random, block_size: int) -> Decimal:
    """Mean of one moving-block bootstrap resample of ``values``.

    For ``block_size == 1`` this is the ordinary i.i.d. resample. Otherwise
    it draws contiguous blocks of ``block_size`` from random (overlapping)
    start positions and concatenates them until at least ``len(values)``
    elements are collected, then averages the first ``len(values)`` — so the
    resample is the same size as the sample but carries the sample's local
    autocorrelation instead of destroying it.
    """
    n = len(values)
    if block_size <= 1:
        resample: list[Decimal] = rng.choices(values, k=n)
    else:
        max_start = max(0, n - block_size)
        collected: list[Decimal] = []
        while len(collected) < n:
            start = rng.randint(0, max_start)
            collected.extend(values[start : start + block_size])
        resample = collected[:n]
    return sum(resample, Decimal(0)) / Decimal(n)


def _quantize(value: Decimal) -> Decimal:
    return value.quantize(ACCOUNTING_RESOLUTION, rounding=ROUND_HALF_EVEN)
