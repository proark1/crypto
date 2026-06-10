"""Roll closed 1m candles up to higher timeframes, incrementally.

Buckets are aligned to the Unix epoch in UTC (so 4h candles open at
00:00/04:00/... and daily candles at midnight UTC, matching Binance's kline
alignment). An aggregate is emitted only when a 1m candle belonging to the
*next* bucket arrives — strategies only ever see closed candles, in backtest
and live alike. A partial bucket at the end of a stream is intentionally never
emitted.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from tradebot.core.models import Candle, CandleInterval

_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


class TimeframeAggregator:
    """Aggregates one symbol's 1m candles into one target interval.

    Missing minutes inside a bucket are tolerated — the aggregate is built
    from the candles that exist (volume sums what was seen). Out-of-order or
    duplicate input raises immediately: silently mis-ordered data would
    corrupt every indicator downstream, so it must fail loudly at the source.
    """

    def __init__(self, target_interval: CandleInterval) -> None:
        """Create an aggregator emitting ``target_interval`` candles."""
        if target_interval == CandleInterval.M1:
            raise ValueError("target interval must be coarser than the 1m base")
        self._target = target_interval
        self._symbol: str | None = None
        self._last_open_time: datetime | None = None
        self._bucket_start: datetime | None = None
        self._open: Decimal | None = None
        self._high: Decimal | None = None
        self._low: Decimal | None = None
        self._close: Decimal | None = None
        self._volume = Decimal(0)

    def add(self, candle: Candle) -> Candle | None:
        """Consume one closed 1m candle; return the completed aggregate, if any.

        Returns the finished target-interval candle when ``candle`` opens a new
        bucket, otherwise ``None``.
        """
        if candle.interval != CandleInterval.M1:
            raise ValueError(f"aggregator consumes 1m candles, got {candle.interval}")
        if self._symbol is None:
            self._symbol = candle.symbol
        elif candle.symbol != self._symbol:
            raise ValueError(f"aggregator is bound to {self._symbol}, got {candle.symbol}")
        if self._last_open_time is not None and candle.open_time <= self._last_open_time:
            raise ValueError(
                f"out-of-order or duplicate candle: {candle.open_time.isoformat()} after "
                f"{self._last_open_time.isoformat()}"
            )
        self._last_open_time = candle.open_time

        bucket_start = self._bucket_start_for(candle.open_time)
        completed: Candle | None = None
        if self._bucket_start is None:
            self._start_bucket(bucket_start, candle)
        elif bucket_start != self._bucket_start:
            completed = self._finish_bucket()
            self._start_bucket(bucket_start, candle)
        else:
            self._accumulate(candle)
        return completed

    def _bucket_start_for(self, moment: datetime) -> datetime:
        return moment - ((moment - _EPOCH) % self._target.duration)

    def _start_bucket(self, bucket_start: datetime, candle: Candle) -> None:
        self._bucket_start = bucket_start
        self._open = candle.open_quote
        self._high = candle.high_quote
        self._low = candle.low_quote
        self._close = candle.close_quote
        self._volume = candle.volume_base

    def _accumulate(self, candle: Candle) -> None:
        assert self._high is not None and self._low is not None  # bucket is open
        self._high = max(self._high, candle.high_quote)
        self._low = min(self._low, candle.low_quote)
        self._close = candle.close_quote
        self._volume += candle.volume_base

    def _finish_bucket(self) -> Candle:
        assert (  # only called with an open bucket
            self._bucket_start is not None
            and self._symbol is not None
            and self._open is not None
            and self._high is not None
            and self._low is not None
            and self._close is not None
        )
        return Candle(
            symbol=self._symbol,
            interval=self._target,
            open_time=self._bucket_start,
            close_time=self._bucket_start + self._target.duration,
            open_quote=self._open,
            high_quote=self._high,
            low_quote=self._low,
            close_quote=self._close,
            volume_base=self._volume,
        )
