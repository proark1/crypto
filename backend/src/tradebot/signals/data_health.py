"""Data-health entry gate: pause entries when market data is degraded.

A symbol's feed is *healthy* once a REST backfill has confirmed a gap-free
history, and *degraded* after a backfill fails (an unrepaired outage gap) or
before the first one completes. Trading new risk on a feed with an unrepaired
gap means entering on stale or out-of-order candles — the strategy's
indicators were computed across a hole, and resting orders may have skipped
the candles that actually happened while the bot was offline.

This gate blocks *entries* on a degraded feed and journals the reason, so the
block is visible in the decision trail. Exits are never gated (ARCHITECTURE.md
4.8), so a degraded feed pauses new risk without ever trapping an open
position — its protective stop keeps working regardless.
"""

from __future__ import annotations

from typing import Protocol

from tradebot.core.models import Signal
from tradebot.signals.base import GateDecision


class FeedHealth(Protocol):
    """The slice of a market-data feed this gate reads.

    Kept minimal and structural so the gate never imports the marketdata
    package — the live feed satisfies it by exposing ``healthy`` and
    ``health_reason``.
    """

    @property
    def healthy(self) -> bool:
        """Whether the feed has confirmed gap-free history."""
        ...

    @property
    def health_reason(self) -> str | None:
        """Why the feed is degraded, or ``None`` when healthy."""
        ...


class DataHealthGate:
    """Block entries while a symbol's market data is degraded.

    Holds one symbol's feed (anything satisfying :class:`FeedHealth`); the
    worker builds one gate per coin so each entry is judged against its own
    feed's health.
    """

    def __init__(self, feed: FeedHealth) -> None:
        """Gate entries on ``feed``'s data-health latch."""
        self._feed = feed

    def evaluate(self, signal: Signal) -> GateDecision:
        """Allow the entry only when the feed has confirmed healthy data."""
        if self._feed.healthy:
            return GateDecision(allowed=True)
        reason = self._feed.health_reason or "market data is degraded"
        return GateDecision(allowed=False, reasons=(f"data health: {reason}",))
