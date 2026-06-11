"""Market-wide sentiment inputs for the regime gate (ARCHITECTURE.md 5.1 P1).

Fear & Greed (alternative.me), BTC dominance (CoinGecko), and broad
negative news flow. These are *advisory tighteners*: each can only push
the gate toward risk-off, never open it — so missing or stale data simply
contributes nothing, and the bot fails toward the ADX core that is always
present. That asymmetry is what makes shipping free, best-effort sources
safe.

Readings are judged against the asking moment's clock and expire after a
TTL; a dead sentiment API quietly stops influencing decisions instead of
freezing the bot in its last opinion.
"""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from datetime import datetime, timedelta

import httpx
from pydantic import BaseModel, ConfigDict, Field

from tradebot.core.models import utc_now

logger = logging.getLogger(__name__)


class SentimentConfig(BaseModel):
    """Frozen thresholds for the sentiment tighteners."""

    model_config = ConfigDict(frozen=True)

    extreme_fear_at_or_below: int = Field(default=20, ge=0, le=100)
    """Fear & Greed at or below this is capitulation conditions: trend
    entries pause (chasing momentum into a capitulating market). Mean-
    reversion entries are exempt — buying oversold recoveries *is* that
    family's edge, each entry carries its protective stop, and the regime
    gate's drawdown risk-off state still halts everything in a real crash."""

    extreme_greed_at_or_above: int = Field(default=90, ge=0, le=100)
    """Fear & Greed at or above this is euphoria — historically where tops
    form; new entries pause rather than chase the blow-off."""

    dominance_surge_points: float = Field(default=3.0, gt=0.0)
    dominance_window: timedelta = timedelta(days=2)
    """BTC dominance rising this many percentage points inside the window
    means capital is fleeing alts for BTC: risk-off for new entries."""

    reading_ttl: timedelta = timedelta(hours=2)
    """Readings older than this contribute nothing (advisory data only)."""

    negative_news_threshold: int = Field(default=5, gt=0)
    negative_news_window: timedelta = timedelta(hours=2)
    """This many negative headlines across the tracked coins inside the
    window is broad negative flow (ARCHITECTURE.md 5.3): risk-off."""


class MarketSentiment:
    """The mutable sentiment state, owned by the worker, fed by monitors."""

    def __init__(self, config: SentimentConfig | None = None) -> None:
        """Create an empty state: no readings, no opinions."""
        self._config = config or SentimentConfig()
        self._fear_greed: tuple[int, datetime] | None = None
        self._dominance: deque[tuple[float, datetime]] = deque(maxlen=500)
        self._negative_news: deque[datetime] = deque(maxlen=500)

    @property
    def config(self) -> SentimentConfig:
        """The frozen thresholds."""
        return self._config

    def record_fear_greed(self, value: int, at: datetime) -> None:
        """Store the latest Fear & Greed reading (0-100)."""
        self._fear_greed = (value, at)

    def record_btc_dominance(self, percent: float, at: datetime) -> None:
        """Append a BTC dominance reading (percent of total market cap)."""
        self._dominance.append((percent, at))

    def record_negative_news(self, at: datetime) -> None:
        """Count one negative headline toward the broad-flow window."""
        self._negative_news.append(at)

    def risk_off_reason(self, at: datetime, *, mean_reversion_entry: bool = False) -> str | None:
        """Return why entries should pause as of ``at``, or ``None``.

        First match wins; the reason is journaled verbatim by the gate.
        ``mean_reversion_entry`` exempts the extreme-fear check only: that
        family exists to buy fear, so blocking it there would gate out its
        entire edge. Greed euphoria, dominance surges, and broad negative
        news still pause every family, and the default stays the strict
        path for any caller that does not say otherwise.
        """
        config = self._config
        if self._fear_greed is not None:
            value, seen_at = self._fear_greed
            if at - seen_at <= config.reading_ttl:
                if value <= config.extreme_fear_at_or_below and not mean_reversion_entry:
                    return f"Fear & Greed at {value} (extreme fear; trend entries pause)"
                if value >= config.extreme_greed_at_or_above:
                    return f"Fear & Greed at {value} (extreme greed / euphoria)"
        surge = self._dominance_surge(at)
        if surge is not None:
            return (
                f"BTC dominance up {surge:.1f} points in "
                f"{config.dominance_window} (capital fleeing alts)"
            )
        recent_negatives = [
            moment
            for moment in self._negative_news
            if at - moment <= config.negative_news_window and moment <= at
        ]
        if len(recent_negatives) >= config.negative_news_threshold:
            return (
                f"broad negative news flow: {len(recent_negatives)} negative headlines "
                f"in {config.negative_news_window}"
            )
        return None

    def _dominance_surge(self, at: datetime) -> float | None:
        """Dominance rise inside the window, if it crosses the threshold."""
        window = [
            (percent, seen_at)
            for percent, seen_at in self._dominance
            if at - seen_at <= self._config.dominance_window and seen_at <= at
        ]
        if len(window) < 2:
            return None
        latest_percent, latest_at = window[-1]
        if at - latest_at > self._config.reading_ttl:
            return None  # the newest reading itself is stale; no opinion
        lowest = min(percent for percent, _ in window)
        rise = latest_percent - lowest
        return rise if rise >= self._config.dominance_surge_points else None


FEAR_GREED_URL = "https://api.alternative.me/fng/"
COINGECKO_GLOBAL_URL = "https://api.coingecko.com/api/v3/global"


class SentimentMonitor:
    """Polls the free sentiment APIs; failures cost freshness, not trading."""

    def __init__(
        self,
        sentiment: MarketSentiment,
        client: httpx.AsyncClient,
        poll_interval: timedelta = timedelta(minutes=15),
    ) -> None:
        """``client`` is owned by the caller (worker shutdown closes it)."""
        self._sentiment = sentiment
        self._client = client
        self._poll_interval = poll_interval

    async def run(self) -> None:
        """Poll forever; cancellation (worker shutdown) is the only exit."""
        while True:
            await self.poll_once()
            await asyncio.sleep(self._poll_interval.total_seconds())

    async def poll_once(self) -> None:
        """Fetch both sources; each failure is logged and skipped."""
        now = utc_now()
        try:
            response = await self._client.get(FEAR_GREED_URL, params={"limit": "1"})
            response.raise_for_status()
            value = int(response.json()["data"][0]["value"])
            self._sentiment.record_fear_greed(value, now)
        except Exception:
            logger.warning("fear & greed poll failed; reading skipped", exc_info=True)
        try:
            response = await self._client.get(COINGECKO_GLOBAL_URL)
            response.raise_for_status()
            dominance = float(response.json()["data"]["market_cap_percentage"]["btc"])
            self._sentiment.record_btc_dominance(dominance, now)
        except Exception:
            logger.warning("btc dominance poll failed; reading skipped", exc_info=True)
