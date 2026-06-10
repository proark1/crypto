"""News ingestion: poll, de-duplicate, classify, flag (ARCHITECTURE.md 5.3).

The monitor is a background loop beside trading, never in it: a dead or
slow news API costs event awareness, not candles. Every failure is logged
and retried on the next poll; nothing here raises into the worker.
"""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from collections.abc import Callable, Iterable
from datetime import datetime, timedelta

import httpx
from pydantic import BaseModel, ConfigDict

from tradebot.core.events import EventBus
from tradebot.core.models import utc_now
from tradebot.news.classifier import classify
from tradebot.news.flags import NewsFlag, NewsFlags
from tradebot.news.models import NewsItem
from tradebot.signals.sentiment import MarketSentiment

logger = logging.getLogger(__name__)

SEEN_IDS_LIMIT = 2000
"""De-duplication memory; CryptoPanic pages are ~50 items, so this covers
days of polls without unbounded growth."""


class NewsFlagged(BaseModel):
    """Published when a negative headline flags a tracked coin.

    Observers only (Telegram, UI pushes): the flag is already raised when
    this fires, so consumers can never race the gate.
    """

    model_config = ConfigDict(frozen=True)

    flag: NewsFlag


class CryptoPanicSource:
    """Fetches recent posts for the tracked coins from CryptoPanic.

    The free tier is polled gently (the worker's poll interval, 1-2
    minutes) and only for the coins the bot actually trades.
    """

    BASE_URL = "https://cryptopanic.com/api/v1/posts/"

    def __init__(self, auth_token: str, client: httpx.AsyncClient) -> None:
        """``client`` is owned by the caller (worker shutdown closes it)."""
        if not auth_token:
            raise ValueError("CryptoPanic source requires an auth token")
        self._auth_token = auth_token
        self._client = client

    async def fetch_latest(self, currencies: tuple[str, ...]) -> list[NewsItem]:
        """Return the newest posts tagged with any of ``currencies``.

        Raises on transport/shape errors — the monitor catches, logs, and
        keeps polling; the source stays a thin honest client.
        """
        response = await self._client.get(
            self.BASE_URL,
            params={
                "auth_token": self._auth_token,
                "currencies": ",".join(currencies),
                "public": "true",
            },
        )
        response.raise_for_status()
        payload = response.json()
        items = []
        for post in payload.get("results", []):
            codes = tuple(
                currency["code"]
                for currency in post.get("currencies") or []
                if isinstance(currency, dict) and "code" in currency
            )
            items.append(
                NewsItem(
                    external_id=str(post["id"]),
                    source="cryptopanic",
                    title=post["title"],
                    url=post.get("url"),
                    currencies=codes,
                    # fromisoformat handles the API's trailing "Z" (3.11+).
                    published_at=datetime.fromisoformat(post["published_at"]),
                )
            )
        return items


class NewsMonitor:
    """Polls a source, classifies headlines, and raises coin flags."""

    def __init__(
        self,
        source: CryptoPanicSource,
        flags: NewsFlags,
        tracked_coins: Callable[[], Iterable[str]],
        bus: EventBus | None = None,
        poll_interval: timedelta = timedelta(seconds=90),
        sentiment: MarketSentiment | None = None,
    ) -> None:
        """``tracked_coins`` is read fresh each poll: coins change at runtime.

        ``sentiment`` (when wired) is told about every negative headline so
        broad negative flow can raise the regime gate to risk-off
        (ARCHITECTURE.md 5.3 actions) — a flood of bad news is a market
        condition, not just a per-coin problem.
        """
        self._source = source
        self._flags = flags
        self._tracked_coins = tracked_coins
        self._bus = bus
        self._poll_interval = poll_interval
        self._sentiment = sentiment
        self._seen_ids: set[str] = set()
        self._seen_order: deque[str] = deque()

    async def run(self) -> None:
        """Poll forever; cancellation (worker shutdown) is the only exit."""
        while True:
            try:
                await self.poll_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                # Event awareness degrades, trading continues — and the log
                # says so instead of the monitor dying silently.
                logger.warning("news poll failed; retrying next interval", exc_info=True)
            await asyncio.sleep(self._poll_interval.total_seconds())

    async def poll_once(self) -> list[NewsFlag]:
        """One poll cycle; returns the flags raised (tests and run loop)."""
        coins = tuple(coin.partition("/")[0] for coin in self._tracked_coins())
        if not coins:
            return []
        items = await self._source.fetch_latest(coins)
        raised: list[NewsFlag] = []
        for item in items:
            if item.external_id in self._seen_ids:
                continue
            self._remember(item.external_id)
            classified = classify(item)
            if not classified.is_negative:
                # Logged for research, no live action (ARCHITECTURE.md 5.3).
                logger.info("news (%s): %s", classified.event_type.value, item.title)
                continue
            now = utc_now()
            if self._sentiment is not None:
                # Counted once per headline, whatever it tags: broad flow
                # is about the market's day, not any single coin's.
                self._sentiment.record_negative_news(now)
            for coin in item.currencies:
                if coin not in coins:
                    continue
                flag = self._flags.flag(coin, classified, now)
                raised.append(flag)
                logger.warning(
                    "news flag raised: %s on %s — %r (keyword %r)",
                    flag.event_type.value,
                    coin,
                    item.title,
                    classified.matched_keyword,
                )
                if self._bus is not None:
                    await self._bus.publish(NewsFlagged(flag=flag))
        return raised

    def _remember(self, external_id: str) -> None:
        self._seen_ids.add(external_id)
        self._seen_order.append(external_id)
        while len(self._seen_order) > SEEN_IDS_LIMIT:
            self._seen_ids.discard(self._seen_order.popleft())
