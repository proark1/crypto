"""Perpetual funding rate as a positioning tightener for the regime gate.

Funding rate is what perpetual longs pay shorts (or vice versa) each funding
window; persistently high positive funding means crowded, over-leveraged longs
— historically a top-risk condition. Like the other inputs in ``sentiment.py``
this is an advisory *tightener*: it can only push the gate toward risk-off,
never open it, so a missing or unsupported feed simply contributes nothing. We
trade spot, but the matching perpetual's funding is a market-wide positioning
gauge for spot entries.

The reading feeds the shared ``MarketSentiment`` state and is judged against the
asking moment's clock with the same TTL, so a dead feed quietly stops
influencing decisions instead of freezing the bot in its last opinion. The
fetch is injected, so the poller is testable without a live exchange — and is
never a backtest input (funding is a live signal only, so the golden backtest is
unaffected).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import timedelta
from typing import Any

from tradebot.core.models import utc_now
from tradebot.signals.sentiment import MarketSentiment

logger = logging.getLogger(__name__)

FundingRateFetcher = Callable[[str], Awaitable[float | None]]
"""Fetch the current funding rate (per-interval fraction) for a perp symbol,
or ``None`` when the venue exposes no funding for it. Injected so the poller is
testable without a live exchange and never hard-codes a ccxt call here."""


def ccxt_funding_fetcher(exchange: Any) -> FundingRateFetcher:
    """Adapt a ccxt exchange's ``fetch_funding_rate`` into a ``FundingRateFetcher``.

    Returns ``None`` when the venue reports no ``fundingRate`` for the symbol,
    so an unsupported market is a no-op rather than an error. The poller wraps
    the call in its own try/except, so a venue that lacks the method at all
    still degrades to "no reading".
    """

    async def fetch(symbol: str) -> float | None:
        result = await exchange.fetch_funding_rate(symbol)
        rate = result.get("fundingRate") if isinstance(result, dict) else None
        return None if rate is None else float(rate)

    return fetch


class FundingMonitor:
    """Polls a perp's funding rate into ``MarketSentiment``.

    Failures cost freshness, not trading: a venue without funding for the
    symbol, a transient error, or a ``None`` reading all leave the state
    untouched, so the tightener simply has no opinion (fail-safe).
    """

    def __init__(
        self,
        sentiment: MarketSentiment,
        fetch: FundingRateFetcher,
        symbol: str,
        poll_interval: timedelta = timedelta(minutes=15),
    ) -> None:
        """``fetch`` and the worker own the exchange client; this only reads it."""
        self._sentiment = sentiment
        self._fetch = fetch
        self._symbol = symbol
        self._poll_interval = poll_interval

    async def run(self) -> None:
        """Poll forever; cancellation (worker shutdown) is the only exit."""
        while True:
            await self.poll_once()
            await asyncio.sleep(self._poll_interval.total_seconds())

    async def poll_once(self) -> None:
        """Fetch the funding rate once; any failure is logged and skipped.

        The broad catch mirrors ``SentimentMonitor``: a best-effort poll must
        never crash the worker, and a skipped reading is the fail-safe outcome
        (the tightener contributes nothing without fresh data).
        """
        try:
            rate = await self._fetch(self._symbol)
        except Exception:
            logger.warning("funding rate poll failed; reading skipped", exc_info=True)
            return
        if rate is not None:
            self._sentiment.record_funding_rate(rate, utc_now())
