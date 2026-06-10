"""Keyword classification of headlines (ARCHITECTURE.md 5.3).

Keyword rules first, deliberately: they are auditable, free, and fast. An
LLM classifier is a later upgrade only if this proves too noisy in the
research log. Rules are ordered most-severe-first so a headline that
matches several categories is treated by its most dangerous meaning —
"exchange delists hacked token" is a delisting *and* a hack, and either
way the coin is flagged.

Matching is case-insensitive substring over the title. Substrings are
chosen to be unambiguous stems ("delist" covers delists/delisted/
delisting); anything not matched is NOISE, which is logged for research
and triggers nothing.
"""

from __future__ import annotations

from tradebot.news.models import ClassifiedNews, NewsEventType, NewsItem

KEYWORD_RULES: tuple[tuple[NewsEventType, tuple[str, ...]], ...] = (
    (
        NewsEventType.DELISTING,
        ("delist", "cease trading", "trading termination", "suspends trading", "removal of"),
    ),
    (
        NewsEventType.HACK,
        ("hack", "exploit", "drained", "stolen", "breach", "rug pull", "rugpull", "attacker"),
    ),
    (
        NewsEventType.REGULATORY,
        (
            "sec sues",
            "sec charges",
            "lawsuit",
            "regulator",
            "regulatory action",
            "banned",
            "sanction",
            "subpoena",
            "cease and desist",
            "crackdown",
        ),
    ),
    (NewsEventType.LISTING, ("will list", "lists ", "new listing", "launches trading")),
    (NewsEventType.PARTNERSHIP, ("partners with", "partnership", "integrates", "integration")),
)
"""Frozen rule order: delisting > hack > regulatory > listing > partnership.
Changing keywords changes what the bot reacts to; keep edits deliberate."""


def classify(item: NewsItem) -> ClassifiedNews:
    """Map one headline onto an event type via the frozen keyword rules."""
    title = item.title.lower()
    for event_type, keywords in KEYWORD_RULES:
        for keyword in keywords:
            if keyword in title:
                return ClassifiedNews(item=item, event_type=event_type, matched_keyword=keyword)
    return ClassifiedNews(item=item, event_type=NewsEventType.NOISE)
