"""Average Directional Index (Wilder), TA-Lib-compatible."""

from __future__ import annotations


class Adx:
    """Incremental ADX with Wilder smoothing, matching TA-Lib's ADX.

    Directional movement needs the previous candle, the DM/TR smoothing
    needs ``period`` more, and the first ADX is the average of the first
    ``period`` DX values — so the first emitted value arrives after
    ``2 * period`` candles (TA-Lib lookback ``2 * period - 1``); thereafter
    ``adx = (adx * (period - 1) + dx) / period``.

    ``plus_di`` / ``minus_di`` expose the smoothed directional lines for
    regime classification. They follow ADX's internal smoothing schedule,
    which TA-Lib's standalone PLUS_DI/MINUS_DI functions deliberately do
    not share exactly — only the ADX output itself is reference-tested.
    """

    def __init__(self, period: int) -> None:
        """Create an ADX over ``period`` candles (must be >= 2)."""
        if period < 2:
            raise ValueError(f"ADX period must be >= 2, got {period}")
        self._period = period
        self._previous_high: float | None = None
        self._previous_low = 0.0
        self._previous_close = 0.0
        self._initial_steps = 0
        self._plus_dm = 0.0
        self._minus_dm = 0.0
        self._true_range = 0.0
        self._dx_count = 0
        self._dx_sum = 0.0
        self._value: float | None = None
        self._plus_di: float | None = None
        self._minus_di: float | None = None

    @property
    def value(self) -> float | None:
        """Current ADX (0-100), or ``None`` during warm-up."""
        return self._value

    @property
    def plus_di(self) -> float | None:
        """Smoothed +DI (0-100), or ``None`` before smoothing starts."""
        return self._plus_di

    @property
    def minus_di(self) -> float | None:
        """Smoothed -DI (0-100), or ``None`` before smoothing starts."""
        return self._minus_di

    def update(self, high: float, low: float, close: float) -> float | None:
        """Consume the next candle's high/low/close and return the ADX (O(1))."""
        if self._previous_high is None:
            self._previous_high = high
            self._previous_low = low
            self._previous_close = close
            return None

        up_move = high - self._previous_high
        down_move = self._previous_low - low
        true_range = max(
            high - low,
            abs(high - self._previous_close),
            abs(low - self._previous_close),
        )
        self._previous_high = high
        self._previous_low = low
        self._previous_close = close

        if self._initial_steps < self._period - 1:
            # Initial accumulation: plain sums, exactly TA-Lib's first phase.
            self._accumulate_dm(up_move, down_move)
            self._true_range += true_range
            self._initial_steps += 1
            return None

        # Wilder smoothing: decay first, then add today's movement.
        self._plus_dm -= self._plus_dm / self._period
        self._minus_dm -= self._minus_dm / self._period
        self._accumulate_dm(up_move, down_move)
        self._true_range = self._true_range - self._true_range / self._period + true_range

        dx: float | None = None
        if self._true_range != 0:
            self._plus_di = 100.0 * self._plus_dm / self._true_range
            self._minus_di = 100.0 * self._minus_dm / self._true_range
            di_sum = self._plus_di + self._minus_di
            if di_sum != 0:
                dx = 100.0 * abs(self._plus_di - self._minus_di) / di_sum

        if self._value is None:
            # Building the first ADX: average of the first `period` DX values,
            # counting candles even when a zero range yields no DX (TA-Lib).
            if dx is not None:
                self._dx_sum += dx
            self._dx_count += 1
            if self._dx_count == self._period:
                self._value = self._dx_sum / self._period
        elif dx is not None:
            self._value = (self._value * (self._period - 1) + dx) / self._period
        return self._value

    def _accumulate_dm(self, up_move: float, down_move: float) -> None:
        """Add today's directional movement (only the dominant side counts)."""
        if down_move > 0 and up_move < down_move:
            self._minus_dm += down_move
        elif up_move > 0 and up_move > down_move:
            self._plus_dm += up_move
