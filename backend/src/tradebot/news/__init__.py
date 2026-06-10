"""Defensive news pipeline: ingestion, classification, flags, calendar.

News never creates trades (ARCHITECTURE.md 5.3): negative headlines flag a
coin — block its entries, tell the human — and scheduled high-impact
events pause all entries in a window. Everything else is logged for
research.
"""

from tradebot.news.calendar import EventCalendar, ScheduledEvent
from tradebot.news.classifier import KEYWORD_RULES, classify
from tradebot.news.flags import NewsFlag, NewsFlags
from tradebot.news.gate import NewsGate
from tradebot.news.models import (
    NEGATIVE_EVENT_TYPES,
    ClassifiedNews,
    NewsEventType,
    NewsItem,
)
from tradebot.news.monitor import CryptoPanicSource, NewsFlagged, NewsMonitor

__all__ = [
    "KEYWORD_RULES",
    "NEGATIVE_EVENT_TYPES",
    "ClassifiedNews",
    "CryptoPanicSource",
    "EventCalendar",
    "NewsEventType",
    "NewsFlag",
    "NewsFlagged",
    "NewsFlags",
    "NewsGate",
    "NewsItem",
    "NewsMonitor",
    "ScheduledEvent",
    "classify",
]
