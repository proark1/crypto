"""Live market data feed over CCXT's unified WebSocket API.

Exchange-agnostic by construction: the venue is a config string ("binance",
"kraken", "coinbase", ...) resolved to a CCXT exchange instance at startup —
the trading code never knows which venue it is on. This module consumes the
exchange's OHLCV stream, emits **closed candles only** (strategies must see
identical semantics in backtest and live), persists everything it emits, and
repairs gaps via REST backfill after every (re)connect.

Float boundary note: CCXT parses exchange prices into floats. Candles are
converted ``float -> str -> Decimal`` here at the edge. For market data this
is acceptable (indicator math is float anyway and order sizing flows through
the risk manager's own Decimals); a native exchange client that keeps the
original string prices is a Phase 3+ optimization if ever needed.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from datetime import UTC, datetime
from decimal import Decimal
from typing import Protocol

from tradebot.core.events import CandleClosed, EventBus
from tradebot.core.models import Candle, CandleInterval
from tradebot.marketdata.validation import validate_candle
from tradebot.persistence import CandleStore

logger = logging.getLogger(__name__)

OhlcvRow = Sequence[float]
"""CCXT OHLCV row: [timestamp_ms, open, high, low, close, volume]."""


class OhlcvExchange(Protocol):
    """The slice of a CCXT (pro) exchange the feed depends on."""

    async def watch_ohlcv(self, symbol: str, timeframe: str) -> list[OhlcvRow]:
        """Block until the OHLCV stream updates; returns recent rows."""
        ...

    async def fetch_ohlcv(
        self, symbol: str, timeframe: str, since: int | None = None, limit: int | None = None
    ) -> list[OhlcvRow]:
        """REST snapshot of candles starting at ``since`` (ms epoch)."""
        ...


def _row_to_candle(row: OhlcvRow, symbol: str, interval: CandleInterval) -> Candle:
    open_time = datetime.fromtimestamp(row[0] / 1000, tz=UTC)
    return Candle(
        symbol=symbol,
        interval=interval,
        open_time=open_time,
        close_time=open_time + interval.duration,
        open_quote=Decimal(str(row[1])),
        high_quote=Decimal(str(row[2])),
        low_quote=Decimal(str(row[3])),
        close_quote=Decimal(str(row[4])),
        volume_base=Decimal(str(row[5])),
    )


class OhlcvCandleTracker:
    """Turns overlapping OHLCV snapshots into a closed-candle stream.

    CCXT's stream repeats the in-progress candle with updated values and may
    resend history after reconnects. A candle is *closed* once any row with a
    later open time has been seen; each closed candle is emitted exactly once,
    in time order, with the last-seen values for its bucket.
    """

    def __init__(self, symbol: str, interval: CandleInterval) -> None:
        """Track one symbol at one interval."""
        self._symbol = symbol
        self._interval = interval
        self._pending: dict[int, OhlcvRow] = {}
        self._last_emitted_ms: int | None = None

    def update(self, rows: Sequence[OhlcvRow]) -> list[Candle]:
        """Absorb a snapshot; return newly closed candles in time order."""
        for row in rows:
            timestamp_ms = int(row[0])
            if self._last_emitted_ms is not None and timestamp_ms <= self._last_emitted_ms:
                continue  # stale repeat of an already-closed candle
            self._pending[timestamp_ms] = row
        if not self._pending:
            return []
        newest_ms = max(self._pending)
        closed: list[Candle] = []
        for timestamp_ms in sorted(self._pending):
            if timestamp_ms >= newest_ms:
                break  # newest bucket is still in progress
            closed.append(
                _row_to_candle(self._pending.pop(timestamp_ms), self._symbol, self._interval)
            )
            self._last_emitted_ms = timestamp_ms
        return closed


class LiveMarketDataFeed:
    """Streams one symbol's 1m candles: persist, validate, publish.

    Candles that fail validation are quarantined (logged, not published) per
    ARCHITECTURE.md section 11 — the bot must not trade on malformed data.
    """

    def __init__(
        self,
        exchange: OhlcvExchange,
        symbol: str,
        store: CandleStore,
        bus: EventBus,
        reconnect_delays_seconds: Sequence[float] = (1, 2, 5, 10, 30),
    ) -> None:
        """Wire the feed; ``reconnect_delays_seconds`` caps at its last value."""
        self._exchange = exchange
        self._symbol = symbol
        self._store = store
        self._bus = bus
        self._delays = tuple(reconnect_delays_seconds)
        self._interval = CandleInterval.M1
        self._tracker = OhlcvCandleTracker(symbol, self._interval)
        self._stopping = False

    def stop(self) -> None:
        """Request a clean shutdown after the current iteration."""
        self._stopping = True

    async def backfill(self) -> int:
        """Fetch candles missed since the newest stored one; returns count.

        The last row of a REST snapshot is the in-progress candle and is
        always discarded — only closed candles are ever stored.
        """
        latest = await self._store.latest_open_time(self._symbol, self._interval)
        since_ms: int | None = None
        if latest is not None:
            since_ms = int((latest + self._interval.duration).timestamp() * 1000)
        rows = await self._exchange.fetch_ohlcv(self._symbol, self._interval.value, since=since_ms)
        if len(rows) <= 1:
            return 0
        candles = [_row_to_candle(row, self._symbol, self._interval) for row in rows[:-1]]
        candles = [c for c in candles if latest is None or c.open_time > latest]
        await self._store.insert_batch(candles)
        return len(candles)

    async def run(self) -> None:
        """Stream until :meth:`stop` is called, reconnecting with backoff."""
        failures = 0
        while not self._stopping:
            try:
                rows = await self._exchange.watch_ohlcv(self._symbol, self._interval.value)
            except asyncio.CancelledError:
                raise
            except Exception:
                failures += 1
                delay = self._delays[min(failures, len(self._delays)) - 1]
                logger.warning(
                    "market data stream error for %s; reconnecting in %.1fs",
                    self._symbol,
                    delay,
                    exc_info=True,
                )
                await asyncio.sleep(delay)
                try:
                    repaired = await self.backfill()
                    if repaired:
                        logger.info("backfilled %d candles for %s", repaired, self._symbol)
                except Exception:
                    logger.warning("backfill after disconnect failed", exc_info=True)
                continue
            failures = 0
            closed = self._tracker.update(rows)
            if not closed:
                continue
            publishable = []
            for candle in closed:
                issues = validate_candle(candle)
                if issues:
                    logger.error(
                        "quarantined malformed candle %s %s: %s",
                        candle.symbol,
                        candle.open_time.isoformat(),
                        "; ".join(issues),
                    )
                    continue
                publishable.append(candle)
            if not publishable:
                continue
            await self._store.insert_batch(publishable)
            for candle in publishable:
                await self._bus.publish(CandleClosed(candle=candle))


def utc_ms(moment: datetime) -> int:
    """Convert an aware datetime to the millisecond epoch CCXT uses."""
    if moment.tzinfo is None:
        raise ValueError("naive datetime is not allowed; timestamps must be UTC-aware")
    return int(moment.astimezone(UTC).timestamp() * 1000)


def ms_to_utc(timestamp_ms: int) -> datetime:
    """Convert a CCXT millisecond epoch to an aware UTC datetime."""
    return datetime.fromtimestamp(timestamp_ms / 1000, tz=UTC)


__all__ = [
    "LiveMarketDataFeed",
    "OhlcvCandleTracker",
    "OhlcvExchange",
    "ms_to_utc",
    "utc_ms",
]
