"""Gap detection over a sorted candle series, for REST backfill."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from itertools import pairwise

from tradebot.core.models import Candle, CandleInterval


def find_gaps(
    candles: Sequence[Candle], interval: CandleInterval
) -> list[tuple[datetime, datetime]]:
    """Return ``[start, end)`` open-time ranges missing from a sorted series.

    Each returned range covers exactly the candles the backfill must fetch:
    ``start`` is the first missing open time, ``end`` the first present one.
    Raises on unsorted or duplicate input rather than guessing — gap math on
    disordered data would silently mask real holes.
    """
    gaps: list[tuple[datetime, datetime]] = []
    for previous, current in pairwise(candles):
        expected = previous.open_time + interval.duration
        if current.open_time > expected:
            gaps.append((expected, current.open_time))
        elif current.open_time < expected:
            raise ValueError(
                f"candles are unsorted or duplicated at {current.open_time.isoformat()}"
            )
    return gaps
