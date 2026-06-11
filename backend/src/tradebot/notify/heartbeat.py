"""Dead-man's switch: a heartbeat ping that stops when the bot is unwell.

The monitoring direction is deliberately inverted (LIVE_TRADING_CHECKLIST.md
item 7): the bot pings an external monitor (e.g. healthchecks.io) on an
interval, and the *monitor* alerts when pings stop arriving. A dead process,
a wedged event loop, and a stalled market data feed all look identical from
the outside — silence — which is exactly what a bot-side alerting path could
never report about itself.

The ping is gated on candle freshness, not just process liveness: a worker
whose feed has silently died is an unhealthy bot with open positions, and it
must stop pinging even though the process is up. Wall-clock time is correct
here (unlike everywhere in trading logic): this measures the real-world
arrival of data, not simulated market time.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta

import httpx

from tradebot.core.events import CandleClosed, EventBus
from tradebot.core.logging import log_event

logger = logging.getLogger(__name__)


class HeartbeatPinger:
    """Pings ``url`` on an interval while closed candles keep arriving."""

    def __init__(
        self,
        url: str,
        client: httpx.AsyncClient,
        interval: timedelta = timedelta(seconds=60),
        max_staleness: timedelta = timedelta(seconds=180),
    ) -> None:
        """Wire the pinger; ``client`` is owned by the caller (worker).

        ``max_staleness`` is how long after the last candle arrival the bot
        is still considered healthy; it must cover normal candle cadence
        plus exchange jitter, or healthy quiet minutes would raise alerts.
        """
        if not url:
            raise ValueError("heartbeat pinger requires a URL")
        if interval <= timedelta(0) or max_staleness <= timedelta(0):
            raise ValueError("heartbeat interval and staleness must be positive")
        self._url = url
        self._client = client
        self._interval = interval
        self._max_staleness = max_staleness
        self._last_candle_arrival: datetime | None = None

    def attach_to(self, bus: EventBus) -> None:
        """Record candle arrivals; the bus is the same one the engine trades on."""
        bus.subscribe(CandleClosed, self._on_candle)

    async def _on_candle(self, _: CandleClosed) -> None:
        # Wall clock on purpose: freshness means "data is arriving now",
        # regardless of the candle's own (market-time) timestamps.
        self._last_candle_arrival = datetime.now(UTC)

    def is_healthy(self, now: datetime | None = None) -> bool:
        """Return whether a candle arrived recently enough to vouch for the feed.

        Before the first candle (startup, backfill) the bot is *not* yet
        healthy: a feed that never connects must never produce a heartbeat.
        The monitor's grace period covers legitimate startup time.
        """
        if self._last_candle_arrival is None:
            return False
        return (now or datetime.now(UTC)) - self._last_candle_arrival <= self._max_staleness

    async def ping_once(self) -> bool:
        """One health-gated ping; returns whether a ping was sent.

        Never raises: like every notification path, a monitoring outage must
        not disturb trading — and a failed ping correctly looks like silence
        to the dead-man's monitor.
        """
        if not self.is_healthy():
            log_event(logger, logging.WARNING, "heartbeat_suppressed_stale_candles")
            return False
        try:
            response = await self._client.get(self._url)
        except Exception:
            log_event(logger, logging.WARNING, "heartbeat_ping_failed", exc_info=True)
            return False
        if response.status_code >= 400:
            log_event(
                logger,
                logging.WARNING,
                "heartbeat_ping_rejected",
                status_code=response.status_code,
            )
            return False
        return True

    async def run(self) -> None:
        """Ping forever on the interval; cancel the task to stop."""
        log_event(
            logger,
            logging.INFO,
            "deadman_switch_active",
            interval_seconds=int(self._interval.total_seconds()),
        )
        while True:
            await self.ping_once()
            await asyncio.sleep(self._interval.total_seconds())
