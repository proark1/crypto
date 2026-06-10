"""Exponential moving average, TA-Lib-compatible."""

from __future__ import annotations


class Ema:
    """Incremental EMA seeded with the SMA of the first ``period`` values.

    Matches TA-Lib: the first emitted value (after ``period`` updates) is the
    simple average of those values; thereafter the standard recursion with
    smoothing factor ``2 / (period + 1)``.
    """

    def __init__(self, period: int) -> None:
        """Create an EMA over ``period`` values (must be >= 1)."""
        if period < 1:
            raise ValueError(f"EMA period must be >= 1, got {period}")
        self._period = period
        self._alpha = 2.0 / (period + 1.0)
        self._warmup_sum = 0.0
        self._count = 0
        self._value: float | None = None

    @property
    def value(self) -> float | None:
        """Current EMA, or ``None`` until ``period`` values have been seen."""
        return self._value

    def update(self, value: float) -> float | None:
        """Consume the next value and return the updated EMA (O(1))."""
        self._count += 1
        if self._value is None:
            self._warmup_sum += value
            if self._count == self._period:
                self._value = self._warmup_sum / self._period
        else:
            self._value += self._alpha * (value - self._value)
        return self._value
