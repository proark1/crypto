"""Bollinger Bands, TA-Lib-compatible."""

from __future__ import annotations

from collections import deque
from math import sqrt
from typing import NamedTuple


class BollingerBands(NamedTuple):
    """One closed candle's bands; all in the same unit as the input price."""

    lower: float
    middle: float
    upper: float


class Bollinger:
    """Incremental Bollinger Bands seeded like TA-Lib's ``BBANDS`` (SMA mode).

    Matches TA-Lib: the middle band is the simple moving average of the last
    ``period`` closes, and the band half-width is ``num_stddev`` *population*
    standard deviations (dividing by ``period``, not ``period - 1``) of that
    same window. The first value arrives after ``period`` updates; before
    that ``update`` returns ``None``.

    The window sum and sum of squares slide by adding the new close and
    subtracting the evicted one — O(1) per candle, the same rolling form
    TA-Lib uses internally, so the outputs track it bit for bit.
    """

    def __init__(self, period: int, num_stddev: float = 2.0) -> None:
        """Create bands over ``period`` closes (>= 2) at ``num_stddev`` width."""
        if period < 2:
            raise ValueError(f"Bollinger period must be >= 2, got {period}")
        if num_stddev <= 0:
            raise ValueError(f"Bollinger num_stddev must be > 0, got {num_stddev}")
        self._period = period
        self._num_stddev = num_stddev
        self._window: deque[float] = deque(maxlen=period)
        self._sum = 0.0
        self._sum_squares = 0.0
        self._value: BollingerBands | None = None

    @property
    def value(self) -> BollingerBands | None:
        """Current bands, or ``None`` until ``period`` closes have been seen."""
        return self._value

    def update(self, close: float) -> BollingerBands | None:
        """Consume the next close and return the updated bands (O(1))."""
        if len(self._window) == self._period:
            evicted = self._window[0]
            self._sum -= evicted
            self._sum_squares -= evicted * evicted
        self._window.append(close)
        self._sum += close
        self._sum_squares += close * close
        if len(self._window) < self._period:
            return None

        mean = self._sum / self._period
        # Population variance via the rolling sums; clamp to zero so float
        # noise on a flat window can never feed sqrt a negative.
        variance = max(0.0, self._sum_squares / self._period - mean * mean)
        deviation = self._num_stddev * sqrt(variance)
        self._value = BollingerBands(lower=mean - deviation, middle=mean, upper=mean + deviation)
        return self._value
