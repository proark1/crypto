"""Domain models for the defensive news pipeline (ARCHITECTURE.md 5.3).

News is used defensively and for event awareness, never as an alpha source:
a retail bot cannot out-trade HFT on headlines, but it can decline to open
positions into a delisting, a hack, or a scheduled macro event.
"""

from __future__ import annotations

import enum

from pydantic import BaseModel, ConfigDict

from tradebot.core.models import UtcDatetime


class NewsEventType(enum.StrEnum):
    """What a headline means for the bot, mapped by keyword rules."""

    DELISTING = "delisting"
    HACK = "hack"
    REGULATORY = "regulatory"
    LISTING = "listing"
    PARTNERSHIP = "partnership"
    NOISE = "noise"


NEGATIVE_EVENT_TYPES = frozenset(
    {NewsEventType.DELISTING, NewsEventType.HACK, NewsEventType.REGULATORY}
)
"""Event types that flag a held coin for exit and block new entries
(ARCHITECTURE.md 5.3 actions). Everything else is logged for research."""


class NewsItem(BaseModel):
    """One headline as delivered by a news source."""

    model_config = ConfigDict(frozen=True)

    external_id: str
    """The source's own id, used for de-duplication across polls."""

    source: str
    title: str
    url: str | None = None
    currencies: tuple[str, ...] = ()
    """Base-coin codes the source tagged (e.g. ``("BTC", "SOL")``)."""

    published_at: UtcDatetime


class ClassifiedNews(BaseModel):
    """A headline plus the rule-based verdict on what it means."""

    model_config = ConfigDict(frozen=True)

    item: NewsItem
    event_type: NewsEventType
    matched_keyword: str | None = None
    """The keyword that decided the type — journaled so misclassifications
    can be traced straight back to the rule that caused them."""

    @property
    def is_negative(self) -> bool:
        """Whether this item warrants flagging coins (exit flag + entry block)."""
        return self.event_type in NEGATIVE_EVENT_TYPES
