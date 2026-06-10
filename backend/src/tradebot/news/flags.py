"""Active negative-news flags per coin (ARCHITECTURE.md 5.3 actions).

A flag means: block new entries on the coin and surface "consider exiting"
to the human. It never sells by itself — discretionary exits stay with the
human (or the strategy), while the protective stop remains the autonomous
backstop. Flags expire after a TTL so a days-old headline cannot silently
keep a coin untradable forever; expiry is judged against the asking
moment's clock, never the wall clock, keeping one code path everywhere.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from pydantic import BaseModel, ConfigDict

from tradebot.core.models import UtcDatetime
from tradebot.news.models import ClassifiedNews, NewsEventType


class NewsFlag(BaseModel):
    """One coin's active negative-news state."""

    model_config = ConfigDict(frozen=True)

    coin: str
    event_type: NewsEventType
    headline: str
    flagged_at: UtcDatetime
    expires_at: UtcDatetime


class NewsFlags:
    """The mutable registry of flagged coins, owned by the worker."""

    def __init__(self, ttl: timedelta = timedelta(hours=24)) -> None:
        """Create an empty registry; flags live for ``ttl`` unless renewed."""
        self._ttl = ttl
        self._flags: dict[str, NewsFlag] = {}

    def flag(self, coin: str, classified: ClassifiedNews, now: datetime) -> NewsFlag:
        """Raise (or renew) the flag on ``coin`` from a classified headline."""
        flag = NewsFlag(
            coin=coin,
            event_type=classified.event_type,
            headline=classified.item.title,
            flagged_at=now,
            expires_at=now + self._ttl,
        )
        self._flags[coin] = flag
        return flag

    def active_flag(self, coin: str, at: datetime) -> NewsFlag | None:
        """Return the live flag on ``coin`` as of ``at``, if any."""
        flag = self._flags.get(coin)
        if flag is None or at >= flag.expires_at:
            return None
        return flag

    def active_flags(self, at: datetime) -> tuple[NewsFlag, ...]:
        """Every live flag as of ``at`` (status views, alerts)."""
        return tuple(flag for flag in self._flags.values() if at < flag.expires_at)

    def clear(self, coin: str) -> bool:
        """Operator clear; returns whether a flag existed."""
        return self._flags.pop(coin, None) is not None
