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
from datetime import UTC, datetime, timedelta
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


def _is_well_formed(row: OhlcvRow) -> bool:
    """Return True if the row has a timestamp and all five OHLCV fields.

    Some venues occasionally emit rows with ``None`` fields through CCXT; a
    single such row must degrade to a logged drop, never a crashed feed.
    """
    return len(row) >= 6 and all(row[i] is not None for i in range(6))


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
            if not _is_well_formed(row):
                logger.warning("dropping malformed OHLCV row for %s: %r", self._symbol, row)
                continue
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
        history_days: int = 0,
    ) -> None:
        """Wire the feed; ``reconnect_delays_seconds`` caps at its last value.

        ``history_days`` is how far the very first backfill reaches when the
        store has no candles for this symbol at all; once anything is
        stored, backfill always resumes from the newest stored candle.
        """
        self._exchange = exchange
        self._symbol = symbol
        self._store = store
        self._bus = bus
        self._delays = tuple(reconnect_delays_seconds)
        self._history_days = history_days
        self._interval = CandleInterval.M1
        self._tracker = OhlcvCandleTracker(symbol, self._interval)
        self._stopping = False
        # Data-health latch (read by the entry gate). Starts unhealthy: until
        # the first backfill confirms a repaired, gap-free history, new
        # entries must not fire on possibly-stale data. Exits are never
        # gated, so an unhealthy feed pauses new risk, never traps capital.
        self._healthy = False
        self._health_reason: str | None = "awaiting first backfill"

    @property
    def healthy(self) -> bool:
        """Whether the last backfill confirmed gap-free history for this symbol.

        ``True`` only after a backfill has succeeded; a failed backfill (an
        unrepaired gap) flips it back to ``False`` until the next one
        succeeds. The entry gate reads this to pause entries on degraded data.
        """
        return self._healthy

    @property
    def health_reason(self) -> str | None:
        """Why the feed is unhealthy, or ``None`` when it is healthy."""
        return self._health_reason

    def stop(self) -> None:
        """Request a clean shutdown after the current iteration."""
        self._stopping = True

    async def backfill(self) -> int:
        """Repair history, then mark the feed healthy; on failure mark it not.

        Every backfill attempt updates the data-health latch, so all callers
        (startup, post-disconnect, the worker's reference-market priming)
        keep it current. The exception is re-raised unchanged so existing
        callers' own logging is preserved.
        """
        try:
            inserted = await self._backfill()
        except Exception as error:
            self._healthy = False
            self._health_reason = f"backfill failed: {type(error).__name__}"
            raise
        self._healthy = True
        self._health_reason = None
        return inserted

    async def _backfill(self) -> int:
        """Repair history both ways: deepen the past, then page to now.

        Pagination matters: an outage longer than one REST page (typically
        500-1000 candles) must still repair completely. Each page's last row
        is discarded as potentially in progress — safe under pagination,
        because a *closed* last row is simply re-fetched as the first row of
        the next page (the resume point is the last *inserted* candle).
        """
        total_inserted = await self._deepen_history() if self._history_days > 0 else 0
        latest = await self._store.latest_open_time(self._symbol, self._interval)
        while True:
            since_ms: int | None = None
            if latest is not None:
                since_ms = utc_ms(latest + self._interval.duration)
            elif self._history_days > 0:
                # Nothing stored yet: reach back the configured horizon so
                # the database accumulates a real backtest dataset. The
                # exchange's paginated REST history is free; CCXT's rate
                # limiter keeps the deep crawl polite.
                since_ms = utc_ms(datetime.now(UTC) - timedelta(days=self._history_days))
            rows = await self._exchange.fetch_ohlcv(
                self._symbol, self._interval.value, since=since_ms
            )
            if len(rows) <= 1:
                break
            candles = [
                _row_to_candle(row, self._symbol, self._interval)
                for row in rows[:-1]
                if _is_well_formed(row)
            ]
            candles = [c for c in candles if latest is None or c.open_time > latest]
            if not candles:
                break
            await self._store.insert_batch(candles)
            total_inserted += len(candles)
            latest = candles[-1].open_time
        return total_inserted

    async def _deepen_history(self) -> int:
        """Extend stored history *backward* to the configured horizon.

        The forward pass alone never revisits the past, so a database that
        predates a deeper ``history_days`` setting would keep its shallow
        history forever — the research system would quietly evaluate on a
        sliver. Pages forward from the horizon up to the earliest stored
        candle; idempotent inserts make any overlap harmless.
        """
        earliest = await self._store.earliest_open_time(self._symbol, self._interval)
        if earliest is None:
            return 0  # nothing stored: the forward pass does the deep crawl
        horizon_start = datetime.now(UTC) - timedelta(days=self._history_days)
        if earliest <= horizon_start:
            return 0
        inserted = 0
        since_ms = utc_ms(horizon_start)
        while True:
            rows = await self._exchange.fetch_ohlcv(
                self._symbol, self._interval.value, since=since_ms
            )
            candles = [
                _row_to_candle(row, self._symbol, self._interval)
                for row in rows
                if _is_well_formed(row)
            ]
            candles = [c for c in candles if c.open_time < earliest]
            if not candles:
                break  # reached the already-stored range (or the venue's depth)
            await self._store.insert_batch(candles)
            inserted += len(candles)
            since_ms = utc_ms(candles[-1].open_time + self._interval.duration)
        if inserted:
            logger.info(
                "deepened %s history by %d candles back to the %d-day horizon",
                self._symbol,
                inserted,
                self._history_days,
            )
        return inserted

    async def run(self) -> None:
        """Stream until :meth:`stop` is called, reconnecting with backoff.

        Backfills once before streaming: candles missed while the bot was
        offline must be repaired even if the first connect succeeds.
        """
        try:
            repaired = await self.backfill()
            if repaired:
                logger.info("startup backfill repaired %d candles for %s", repaired, self._symbol)
        except Exception:
            logger.warning("startup backfill failed; stream will repair later", exc_info=True)
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
