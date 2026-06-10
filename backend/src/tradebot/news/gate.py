"""The news/event entry gate — step 4 of the §5.2 pipeline.

Blocks entries on a coin with an active negative-news flag and on every
coin inside a scheduled event window. Like all gates it can only block,
and exits never pass through it (the engine gates BUY signals only). With
no flags and an empty calendar it is a pass-through, so it is always
wired — event awareness must not depend on remembering to enable it.
"""

from __future__ import annotations

from tradebot.core.models import Signal
from tradebot.news.calendar import EventCalendar
from tradebot.news.flags import NewsFlags
from tradebot.signals import GateDecision


class NewsGate:
    """Entry gate over news flags and the scheduled-event calendar."""

    def __init__(self, flags: NewsFlags, calendar: EventCalendar) -> None:
        """Gate entries on ``flags`` (per coin) and ``calendar`` (market-wide)."""
        self._flags = flags
        self._calendar = calendar

    def evaluate(self, signal: Signal) -> GateDecision:
        """Block flagged coins and event windows; the clock is the signal's own."""
        coin = signal.symbol.partition("/")[0]
        flag = self._flags.active_flag(coin, signal.created_at)
        if flag is not None:
            return GateDecision(
                allowed=False,
                reasons=(
                    f"news gate: {flag.event_type.value} flag on {coin} — "
                    f"{flag.headline!r} (expires {flag.expires_at.isoformat()})",
                ),
            )
        event = self._calendar.active_event(signal.created_at)
        if event is not None:
            return GateDecision(
                allowed=False,
                reasons=(
                    f"news gate: no entries during the {event.name} window "
                    f"(until {event.ends_at.isoformat()})",
                ),
            )
        return GateDecision(allowed=True)
