"""Mechanical market-condition labels for evaluation scenarios.

Every label is computed from the context window's own candles by the frozen
definitions in ARCHITECTURE.md section 12 — no human judgement, no fuzzy
labels, so a report row like "bad in high-volatility chop" is reproducible
bit-for-bit from stored data. Float math is fine here (this is analysis,
like indicator internals); nothing computed in this module ever feeds an
order size.

The frozen constants below are part of the scoring spec: changing any of
them invalidates comparability with earlier runs, so they change only with
an explicit ARCHITECTURE.md amendment, never casually.
"""

from __future__ import annotations

import statistics
from collections.abc import Sequence
from math import sqrt

from tradebot.core.models import Candle
from tradebot.evaluation.models import (
    EventLabel,
    MarketConditions,
    TrendLabel,
    VolatilityLabel,
)

MINIMUM_WINDOW = 12
"""Fewer candles than this cannot be labeled meaningfully; classify raises."""

TREND_SIGNIFICANCE = 1.5
"""A window is trending when its net move exceeds this many random-walk
standard deviations (|net return| > k * vol * sqrt(n))."""

VOLATILITY_BAND = 1.5
"""High/low volatility is this factor above/below the run's reference."""

SPIKE_MULTIPLE = 6.0
"""A pump/dump is a single candle return exceeding this multiple of the
window's median absolute return (median, not stdev, so the spike cannot
inflate its own threshold)."""

RANGE_FRACTION = 2 / 3
"""The leading fraction of the window that defines the breakout range; the
trailing remainder is where the break (and its hold or failure) is judged."""

RECOVERY_SIGNIFICANCE = 1.0
"""Post-crash recovery: after a first-half dump, the second half must rise
by at least this many of its *own* random-walk standard deviations — its
own, because measuring the climb against window-wide volatility would let
the crash inflate the very threshold it is compared to."""


def classify_window(
    candles: Sequence[Candle], reference_volatility: float | None = None
) -> MarketConditions:
    """Label one context window; ``reference_volatility`` scales the vol label.

    The reference is the median per-candle volatility across the whole run's
    dataset (computed by the scenario generator); without one, volatility is
    labeled ``NORMAL`` — a window cannot know on its own what "high" means.
    """
    if len(candles) < MINIMUM_WINDOW:
        raise ValueError(f"need at least {MINIMUM_WINDOW} candles to classify, got {len(candles)}")
    closes = [float(candle.close_quote) for candle in candles]
    returns = [closes[i] / closes[i - 1] - 1 for i in range(1, len(closes))]
    volatility = statistics.pstdev(returns)
    net_return = closes[-1] / closes[0] - 1

    return MarketConditions(
        trend=_trend(net_return, volatility, len(returns)),
        volatility=_volatility_label(volatility, reference_volatility),
        events=_events(candles, returns, volatility),
    )


def window_volatility(candles: Sequence[Candle]) -> float:
    """Per-candle close-to-close return volatility (the reference's unit)."""
    closes = [float(candle.close_quote) for candle in candles]
    returns = [closes[i] / closes[i - 1] - 1 for i in range(1, len(closes))]
    return statistics.pstdev(returns) if len(returns) > 1 else 0.0


def _trend(net_return: float, volatility: float, sample_count: int) -> TrendLabel:
    threshold = TREND_SIGNIFICANCE * volatility * sqrt(sample_count)
    if volatility == 0.0 or abs(net_return) <= threshold:
        return TrendLabel.RANGING
    return TrendLabel.UP if net_return > 0 else TrendLabel.DOWN


def _volatility_label(volatility: float, reference: float | None) -> VolatilityLabel:
    if reference is None or reference <= 0.0:
        return VolatilityLabel.NORMAL
    if volatility > reference * VOLATILITY_BAND:
        return VolatilityLabel.HIGH
    if volatility < reference / VOLATILITY_BAND:
        return VolatilityLabel.LOW
    return VolatilityLabel.NORMAL


def _events(
    candles: Sequence[Candle], returns: list[float], volatility: float
) -> tuple[EventLabel, ...]:
    events: list[EventLabel] = []
    spike_threshold = _spike_threshold(returns)
    if spike_threshold > 0.0:
        if max(returns) > spike_threshold:
            events.append(EventLabel.PUMP)
        if min(returns) < -spike_threshold:
            events.append(EventLabel.DUMP)
    events.extend(_breakouts(candles))
    if _is_post_crash_recovery(returns, spike_threshold):
        events.append(EventLabel.POST_CRASH_RECOVERY)
    return tuple(events)


def _spike_threshold(returns: list[float]) -> float:
    median_move = statistics.median(abs(r) for r in returns)
    return SPIKE_MULTIPLE * median_move


def _breakouts(candles: Sequence[Candle]) -> list[EventLabel]:
    """Detect a range break in the window's tail, and whether it held.

    The leading ``RANGE_FRACTION`` of the window defines the range (by close,
    so a single wick does not set the bar); a tail close beyond it is a
    break, and the final close decides real (still beyond) vs fake (back
    inside). Both directions are checked; both labels can coexist when the
    tail whipsaws.
    """
    split = int(len(candles) * RANGE_FRACTION)
    range_part, tail = candles[:split], candles[split:]
    if len(range_part) < 4 or len(tail) < 2:
        return []
    range_high = max(float(candle.close_quote) for candle in range_part)
    range_low = min(float(candle.close_quote) for candle in range_part)
    final_close = float(tail[-1].close_quote)
    labels: list[EventLabel] = []
    broke_up = any(float(candle.close_quote) > range_high for candle in tail)
    broke_down = any(float(candle.close_quote) < range_low for candle in tail)
    if broke_up:
        labels.append(
            EventLabel.BREAKOUT_REAL if final_close > range_high else EventLabel.BREAKOUT_FAKE
        )
    if broke_down:
        labels.append(
            EventLabel.BREAKOUT_REAL if final_close < range_low else EventLabel.BREAKOUT_FAKE
        )
    return labels


def _is_post_crash_recovery(returns: list[float], spike_threshold: float) -> bool:
    """Detect a first-half dump followed by a significant second-half climb."""
    half = len(returns) // 2
    first, second = returns[:half], returns[half:]
    if not first or len(second) < 2 or spike_threshold <= 0.0:
        return False
    crashed = min(first) < -spike_threshold
    second_net = 1.0
    for sample in second:
        second_net *= 1.0 + sample
    climb = second_net - 1.0
    second_volatility = statistics.pstdev(second)
    significant = climb > RECOVERY_SIGNIFICANCE * second_volatility * sqrt(len(second))
    return crashed and significant
