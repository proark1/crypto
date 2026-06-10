"""Materialize the candles behind one stored scenario for the replay viewer.

Scenarios reference candles by coordinates — (symbol, timeframe,
decision_time, lookback) — and never copy them (ARCHITECTURE.md section
12.4). The replay viewer therefore rebuilds the blind window and the
revealed horizon from the candle store through the exact aggregation path
the run used, so what the viewer shows is what the bot saw and was graded
against, not a lookalike.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

from tradebot.core.models import Candle, CandleInterval
from tradebot.marketdata import aggregate_candles
from tradebot.persistence import CandleStore


def slice_replay(
    series: Sequence[Candle],
    decision_time: datetime,
    lookback_candles: int,
    horizon_candles: int,
) -> tuple[list[Candle], list[Candle]]:
    """Split ``series`` into (window, horizon) around ``decision_time``.

    The boundary mirrors the evaluator's: the window is everything closed
    at or before the decision (its last candle closes exactly at
    ``decision_time``), the horizon everything opening at or after it.
    Gaps in storage simply yield shorter slices — the viewer shows what
    exists rather than inventing candles.
    """
    window = [candle for candle in series if candle.close_time <= decision_time]
    horizon = [candle for candle in series if candle.open_time >= decision_time]
    return window[-lookback_candles:], horizon[:horizon_candles]


async def load_replay(
    candle_store: CandleStore,
    symbol: str,
    timeframe: str,
    decision_time: datetime,
    lookback_candles: int,
    horizon_candles: int,
) -> tuple[list[Candle], list[Candle]]:
    """Fetch and aggregate the (window, horizon) candles for one scenario.

    Aggregation buckets are epoch-aligned and ``decision_time`` is a bucket
    boundary (it is the close of an aggregated candle), so aggregating just
    this subrange reproduces byte-identical candles to the run's
    full-history aggregation. Raises ``ValueError`` on an unknown
    timeframe.
    """
    interval = CandleInterval(timeframe)
    start = decision_time - interval.duration * lookback_candles
    end = decision_time + interval.duration * horizon_candles
    # One extra minute past the end: the aggregator emits a bucket only
    # when a candle of the *next* bucket arrives, so without it the final
    # horizon candle would never materialize.
    base = await candle_store.fetch_range(
        symbol, CandleInterval.M1, start, end + CandleInterval.M1.duration
    )
    series = base if interval == CandleInterval.M1 else aggregate_candles(base, interval)
    return slice_replay(series, decision_time, lookback_candles, horizon_candles)
