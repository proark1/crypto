"""Backfill perpetual funding history into the store — the researchable series.

The bot trades spot, but funding is a perpetual-contract metric: persistently
high positive funding is crowded, over-leveraged longs (historically a top-risk
condition), so the matching perp's funding is a market-wide positioning gauge
for the spot coin. This module pages a coin's perp funding history — via the
same unified CCXT client the spot feed already uses — into :class:`FundingStore`,
keyed by the *spot* symbol the strategy trades, so backtest and live read one
series the same way (the §3 one-code-path rule).

Funding prints every few hours, so the history is tiny next to candles and an
hourly re-run keeps it current. A venue or symbol without funding degrades to an
empty series — the funding strategy then simply has no opinion (fail-safe).
"""

from __future__ import annotations

import asyncio
import logging
from bisect import bisect_right
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any, Protocol

from tradebot.core.logging import log_event
from tradebot.core.models import FundingRate
from tradebot.persistence import FundingStore

logger = logging.getLogger(__name__)

FundingRow = Mapping[str, Any]
"""One CCXT funding-history entry: at least ``fundingRate`` and ``timestamp``."""


class FundingHistoryExchange(Protocol):
    """The slice of a CCXT exchange the funding backfill depends on."""

    async def fetch_funding_rate_history(
        self, symbol: str, since: int | None = None, limit: int | None = None
    ) -> list[FundingRow]:
        """REST snapshot of funding prints from ``since`` (ms epoch) forward."""
        ...


def perp_symbol_for(spot_symbol: str) -> str:
    """Map a USDT-quoted spot pair to its CCXT linear-perp symbol.

    ``BTC/USDT`` -> ``BTC/USDT:USDT`` (USDT-margined perpetual). A symbol that is
    already a contract (contains ``:``) is returned unchanged, and anything not
    USDT-quoted is returned unchanged too — the funding fetch then finds nothing,
    which is the intended fail-safe (no funding, no opinion) rather than an error.
    """
    if ":" in spot_symbol:
        return spot_symbol
    if spot_symbol.endswith("/USDT"):
        return f"{spot_symbol}:USDT"
    return spot_symbol


def _row_to_funding_rate(row: FundingRow, symbol: str) -> FundingRate | None:
    """Normalise one CCXT funding entry into a :class:`FundingRate`.

    Returns ``None`` for a malformed row (missing rate or timestamp, or an
    unparseable rate): funding is advisory, so a bad print is dropped, never
    fatal. ``symbol`` is the *spot* key the strategy reads, not the perp fetched.
    """
    rate = row.get("fundingRate")
    timestamp = row.get("timestamp")
    if rate is None or timestamp is None:
        return None
    try:
        # float() first: some venues hand back the ms epoch as a stringified
        # float ("1712345678123.0"), which int() alone would reject.
        funding_time = datetime.fromtimestamp(int(float(timestamp)) / 1000, tz=UTC)
        return FundingRate(symbol=symbol, funding_time=funding_time, rate=Decimal(str(rate)))
    except (InvalidOperation, ValueError, TypeError):
        return None


class FundingBackfiller:
    """Pages one spot coin's perp funding history into the store.

    Keyed by the spot ``symbol``; the perp is derived for the fetch only. Resumes
    one print past the newest stored funding, or starts ``history_days`` back when
    nothing is stored, so every call is an incremental top-up — idempotent via the
    store's conflict-ignoring insert, so calling it at boot and on a timer is safe.
    """

    def __init__(
        self,
        exchange: FundingHistoryExchange,
        store: FundingStore,
        symbol: str,
        history_days: int,
        page_limit: int = 1000,
    ) -> None:
        """``symbol`` is the spot pair; ``history_days`` bounds the cold start."""
        self._exchange = exchange
        self._store = store
        self._symbol = symbol
        self._perp = perp_symbol_for(symbol)
        self._history_days = history_days
        self._page_limit = page_limit

    async def backfill(self) -> int:
        """Fetch new funding prints into the store; return how many were inserted.

        Resumable and idempotent: from the newest stored print (one millisecond
        past it) or ``history_days`` back on a cold start, paging forward until a
        short page or no new prints. A duplicate print for a past window never
        legitimately changes, so an overlap on resume is harmless.
        """
        latest = await self._store.latest_funding_time(self._symbol)
        since_ms = _since_ms(latest, self._history_days)
        inserted = 0
        while True:
            rows = await self._exchange.fetch_funding_rate_history(
                self._perp, since=since_ms, limit=self._page_limit
            )
            fresh = [
                funding
                for row in rows
                if (funding := _row_to_funding_rate(row, self._symbol)) is not None
                and (latest is None or funding.funding_time > latest)
            ]
            if not fresh:
                break
            await self._store.insert_batch(fresh)
            inserted += len(fresh)
            # max(), not fresh[-1]: CCXT sorts ascending, but a venue that does
            # not must still advance the cursor past the newest print seen.
            latest = max(funding.funding_time for funding in fresh)
            since_ms = _since_ms(latest, self._history_days)
            if len(rows) < self._page_limit:
                break
        return inserted

    async def run(self, poll_interval: timedelta = timedelta(hours=1)) -> None:
        """Top up funding forever on a timer; cancellation (shutdown) is the exit.

        Funding prints every few hours, so an hourly re-check is ample. A failed
        fetch costs freshness, not the task: it is logged and retried next tick.
        """
        while True:
            try:
                await self.backfill()
            except Exception:
                log_event(
                    logger,
                    logging.WARNING,
                    "funding_backfill_failed",
                    symbol=self._symbol,
                    exc_info=True,
                )
            await asyncio.sleep(poll_interval.total_seconds())


def _since_ms(after: datetime | None, history_days: int) -> int:
    """Resume point in ms: just past ``after``, or ``history_days`` back if None."""
    start = (
        after + timedelta(milliseconds=1)
        if after is not None
        else datetime.now(UTC) - timedelta(days=history_days)
    )
    # round(), not int(): float seconds * 1000 can land at N-0.0001 and
    # truncate a millisecond off the resume cursor.
    return round(start.timestamp() * 1000)


class FundingSeries:
    """In-memory funding history with a most-recent-at-or-before lookup per symbol.

    The concrete ``FundingProvider`` strategies read: built from stored
    ``FundingRate`` rows (research loads the graded window; live loads and
    refreshes from the same store) and then answers ``rate_as_of`` in O(log n)
    per candle. One instance holds many symbols and resolves by the candle's own
    symbol, so a strategy never needs its symbol at construction — which the
    sweep factory cannot supply.
    """

    def __init__(self) -> None:
        """Start empty; :meth:`load` fills it from stored funding."""
        self._times: dict[str, list[datetime]] = {}
        self._rates: dict[str, list[Decimal]] = {}

    def load(self, rates: Iterable[FundingRate]) -> None:
        """Replace the held history for whichever symbols ``rates`` covers.

        Sorted and de-duplicated by ``funding_time`` per symbol so the lookup's
        bisect is valid; a symbol absent from ``rates`` keeps its prior history,
        so a per-symbol refresh never wipes the others.
        """
        by_symbol: dict[str, dict[datetime, Decimal]] = {}
        for rate in rates:
            by_symbol.setdefault(rate.symbol, {})[rate.funding_time] = rate.rate
        for symbol, points in by_symbol.items():
            ordered = sorted(points)
            self._times[symbol] = ordered
            self._rates[symbol] = [points[when] for when in ordered]

    def rate_as_of(self, symbol: str, at: datetime) -> Decimal | None:
        """Most recent funding at or before ``at`` for ``symbol``, or ``None``."""
        times = self._times.get(symbol)
        if not times:
            return None
        index = bisect_right(times, at) - 1
        return self._rates[symbol][index] if index >= 0 else None


__all__ = [
    "FundingBackfiller",
    "FundingHistoryExchange",
    "FundingRow",
    "FundingSeries",
    "perp_symbol_for",
]
