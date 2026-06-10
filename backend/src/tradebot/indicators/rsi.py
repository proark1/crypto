"""Relative Strength Index (Wilder), TA-Lib-compatible."""

from __future__ import annotations


class Rsi:
    """Incremental RSI with Wilder smoothing.

    Matches TA-Lib: the first emitted value (after ``period + 1`` updates,
    i.e. ``period`` price changes) uses the simple average of the first
    ``period`` gains/losses; subsequent values use Wilder's recursive
    smoothing ``avg = (avg * (period - 1) + current) / period``.
    """

    def __init__(self, period: int) -> None:
        """Create an RSI over ``period`` price changes (must be >= 1)."""
        if period < 1:
            raise ValueError(f"RSI period must be >= 1, got {period}")
        self._period = period
        self._previous: float | None = None
        self._changes_seen = 0
        self._gain_sum = 0.0
        self._loss_sum = 0.0
        self._avg_gain: float | None = None
        self._avg_loss: float | None = None
        self._value: float | None = None

    @property
    def value(self) -> float | None:
        """Current RSI in [0, 100], or ``None`` during warm-up."""
        return self._value

    def update(self, value: float) -> float | None:
        """Consume the next close and return the updated RSI (O(1))."""
        if self._previous is None:
            self._previous = value
            return None

        change = value - self._previous
        self._previous = value
        gain = max(change, 0.0)
        loss = max(-change, 0.0)

        if self._avg_gain is None or self._avg_loss is None:
            self._changes_seen += 1
            self._gain_sum += gain
            self._loss_sum += loss
            if self._changes_seen < self._period:
                return None
            self._avg_gain = self._gain_sum / self._period
            self._avg_loss = self._loss_sum / self._period
        else:
            self._avg_gain = (self._avg_gain * (self._period - 1) + gain) / self._period
            self._avg_loss = (self._avg_loss * (self._period - 1) + loss) / self._period

        total = self._avg_gain + self._avg_loss
        self._value = 50.0 if total == 0.0 else 100.0 * self._avg_gain / total
        return self._value
