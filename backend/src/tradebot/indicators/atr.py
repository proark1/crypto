"""Average True Range (Wilder), TA-Lib-compatible."""

from __future__ import annotations


class Atr:
    """Incremental ATR with Wilder smoothing.

    Matches TA-Lib: true range needs the previous close, so the first emitted
    value arrives after ``period + 1`` candles and equals the simple average
    of the first ``period`` true ranges; thereafter Wilder's smoothing
    ``atr = (atr * (period - 1) + tr) / period``.
    """

    def __init__(self, period: int) -> None:
        """Create an ATR over ``period`` true ranges (must be >= 1)."""
        if period < 1:
            raise ValueError(f"ATR period must be >= 1, got {period}")
        self._period = period
        self._previous_close: float | None = None
        self._ranges_seen = 0
        self._tr_sum = 0.0
        self._value: float | None = None

    @property
    def value(self) -> float | None:
        """Current ATR (same unit as the input prices), or ``None`` in warm-up."""
        return self._value

    def update(self, high: float, low: float, close: float) -> float | None:
        """Consume the next candle's high/low/close and return the ATR (O(1))."""
        if self._previous_close is None:
            self._previous_close = close
            return None

        true_range = max(
            high - low,
            abs(high - self._previous_close),
            abs(low - self._previous_close),
        )
        self._previous_close = close

        if self._value is None:
            self._ranges_seen += 1
            self._tr_sum += true_range
            if self._ranges_seen == self._period:
                self._value = self._tr_sum / self._period
        else:
            self._value = (self._value * (self._period - 1) + true_range) / self._period
        return self._value
