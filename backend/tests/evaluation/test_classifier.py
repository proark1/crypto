"""Classifier tests: each frozen definition demonstrated on a built window."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from tradebot.core.models import Candle, CandleInterval
from tradebot.evaluation import (
    EventLabel,
    TrendLabel,
    VolatilityLabel,
    classify_window,
    window_volatility,
)

BASE_TIME = datetime(2026, 1, 2, 0, 0, tzinfo=UTC)


def make_window(closes: list[float]) -> list[Candle]:
    """Build a window whose closes follow ``closes`` (open = previous close)."""
    candles: list[Candle] = []
    previous = closes[0]
    for index, close in enumerate(closes):
        open_time = BASE_TIME + timedelta(minutes=index)
        high = max(previous, close)
        low = min(previous, close)
        candles.append(
            Candle(
                symbol="BTC/USDT",
                interval=CandleInterval.M1,
                open_time=open_time,
                close_time=open_time + timedelta(minutes=1),
                open_quote=Decimal(str(round(previous, 8))),
                high_quote=Decimal(str(round(high, 8))),
                low_quote=Decimal(str(round(low, 8))),
                close_quote=Decimal(str(round(close, 8))),
                volume_base=Decimal("1"),
            )
        )
        previous = close
    return candles


def wobble(index: int, scale: float = 0.0005) -> float:
    """Deterministic small noise so volatility is never exactly zero."""
    return scale if index % 2 == 0 else -scale


def drift_series(start: float, per_candle: float, count: int) -> list[float]:
    """Compound ``per_candle`` drift (plus wobble) forward from ``start``."""
    closes = [start]
    for index in range(count - 1):
        closes.append(closes[-1] * (1.0 + per_candle + wobble(index)))
    return closes


class TestTrend:
    def test_steady_climb_is_up(self) -> None:
        closes = drift_series(100.0, 0.005, 40)
        assert classify_window(make_window(closes)).trend == TrendLabel.UP

    def test_steady_decline_is_down(self) -> None:
        closes = drift_series(100.0, -0.005, 40)
        assert classify_window(make_window(closes)).trend == TrendLabel.DOWN

    def test_perfectly_smooth_drift_is_a_trend_not_a_range(self) -> None:
        """Zero volatility with non-flat drift: the threshold is zero, so
        any net move trends — the smoothest climb is the strongest trend."""
        closes = [100.0 * 1.005**i for i in range(40)]  # identical returns, vol == 0
        assert classify_window(make_window(closes)).trend == TrendLabel.UP

    def test_noise_without_drift_is_ranging(self) -> None:
        closes = [100.0 + (1.0 if i % 2 == 0 else -1.0) for i in range(40)]
        assert classify_window(make_window(closes)).trend == TrendLabel.RANGING

    def test_flat_prices_are_ranging_not_a_crash(self) -> None:
        closes = [100.0] * 40
        assert classify_window(make_window(closes)).trend == TrendLabel.RANGING


class TestVolatility:
    def test_no_reference_means_normal(self) -> None:
        closes = [100.0 + wobble(i) * 1000 for i in range(40)]
        assert classify_window(make_window(closes)).volatility == VolatilityLabel.NORMAL

    def test_labels_scale_against_the_reference(self) -> None:
        calm = make_window([100.0 + wobble(i) * 100 for i in range(40)])
        wild = make_window([100.0 + wobble(i) * 1000 for i in range(40)])
        reference = window_volatility(calm) * 2

        assert classify_window(calm, reference).volatility == VolatilityLabel.LOW
        assert classify_window(wild, reference).volatility == VolatilityLabel.HIGH
        assert classify_window(calm, window_volatility(calm)).volatility == (VolatilityLabel.NORMAL)


class TestEvents:
    def test_single_candle_spike_up_is_a_pump(self) -> None:
        closes = (
            [100.0 + wobble(i) for i in range(20)] + [110.0] + [110.0 + wobble(i) for i in range(5)]
        )
        events = classify_window(make_window(closes)).events
        assert EventLabel.PUMP in events
        assert EventLabel.DUMP not in events

    def test_single_candle_crash_is_a_dump(self) -> None:
        closes = (
            [100.0 + wobble(i) for i in range(20)] + [90.0] + [90.0 + wobble(i) for i in range(5)]
        )
        events = classify_window(make_window(closes)).events
        assert EventLabel.DUMP in events

    def test_break_that_holds_is_real(self) -> None:
        ranging = [100.0 + wobble(i) * 2000 for i in range(30)]  # closes 99..101
        breakout = [103.0, 104.0, 105.0, 105.5, 106.0, 106.5, 107.0, 107.5, 108.0, 108.5]
        events = classify_window(make_window(ranging + breakout)).events
        assert EventLabel.BREAKOUT_REAL in events
        assert EventLabel.BREAKOUT_FAKE not in events

    def test_break_that_collapses_back_is_fake(self) -> None:
        ranging = [100.0 + wobble(i) * 2000 for i in range(30)]
        whipsaw = [103.0, 104.0, 103.0, 102.0, 101.0, 100.5, 100.0, 99.8, 99.9, 100.0]
        events = classify_window(make_window(ranging + whipsaw)).events
        assert EventLabel.BREAKOUT_FAKE in events
        assert EventLabel.BREAKOUT_REAL not in events

    def test_crash_then_climb_is_post_crash_recovery(self) -> None:
        before = [100.0 + wobble(i) for i in range(15)]
        crash_and_recovery = drift_series(88.0, 0.004, 20)
        events = classify_window(make_window(before + crash_and_recovery)).events
        assert EventLabel.POST_CRASH_RECOVERY in events

    def test_spike_in_a_mostly_flat_window_is_still_labeled(self) -> None:
        """Median move zero must not zero the threshold; the mean is the
        fallback, and a 10% jump out of a dead-flat window clears it."""
        closes = [100.0] * 20 + [110.0] + [110.0] * 5
        events = classify_window(make_window(closes)).events
        assert EventLabel.PUMP in events

    def test_quiet_window_has_no_events(self) -> None:
        closes = [100.0 + wobble(i) for i in range(40)]
        assert classify_window(make_window(closes)).events == ()


class TestGuards:
    def test_short_windows_are_refused(self) -> None:
        with pytest.raises(ValueError, match="at least"):
            classify_window(make_window([100.0] * 5))
