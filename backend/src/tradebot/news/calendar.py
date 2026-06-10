"""Scheduled high-impact events: no-entry windows (ARCHITECTURE.md 5.3).

FOMC, CPI, and token unlocks break normal TA assumptions, so entries pause
inside a configurable window around them. The calendar is configured
explicitly (env JSON) for now; automated ingestion from public calendars is
a later upgrade. Windows are judged against the asking moment's clock,
keeping backtest/paper/live identical.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta

from pydantic import BaseModel, ConfigDict

from tradebot.core.models import UtcDatetime

DEFAULT_WINDOW_MINUTES = 120
"""The +/- window around an event's timestamp (ARCHITECTURE.md 5.3: ~2h)."""


class ScheduledEvent(BaseModel):
    """One known-in-advance event and its no-entry window."""

    model_config = ConfigDict(frozen=True)

    name: str
    starts_at: UtcDatetime
    ends_at: UtcDatetime


class EventCalendar:
    """An ordered set of scheduled no-entry windows."""

    def __init__(self, events: tuple[ScheduledEvent, ...] = ()) -> None:
        """Hold ``events``; overlapping windows are fine (first match wins)."""
        self._events = tuple(sorted(events, key=lambda event: event.starts_at))

    @property
    def events(self) -> tuple[ScheduledEvent, ...]:
        """All configured events, by start time."""
        return self._events

    def active_event(self, at: datetime) -> ScheduledEvent | None:
        """Return the event whose window covers ``at``, if any."""
        for event in self._events:
            if event.starts_at <= at < event.ends_at:
                return event
        return None

    @classmethod
    def from_json(cls, raw: str) -> EventCalendar:
        """Parse the env-var calendar; raises ``ValueError`` on bad input.

        Format: ``[{"name": "FOMC", "time": "2026-06-17T18:00:00Z",
        "window_minutes": 120}, ...]`` — ``window_minutes`` is the +/- half
        window and defaults to 120. Validated at config load so a typo
        stops the deploy instead of silently disabling event awareness.
        """
        if not raw.strip():
            return cls(())
        try:
            entries = json.loads(raw)
        except json.JSONDecodeError as error:
            raise ValueError(f"event calendar is not valid JSON: {error}") from error
        if not isinstance(entries, list):
            raise ValueError("event calendar must be a JSON list of events")
        events = []
        for entry in entries:
            if not isinstance(entry, dict) or "name" not in entry or "time" not in entry:
                raise ValueError(f"calendar entry needs 'name' and 'time': {entry!r}")
            data = dict(entry)
            if isinstance(data["time"], str):
                # fromisoformat handles the trailing "Z" (3.11+); a bad
                # timestamp raises ValueError here, at config load.
                data["time"] = datetime.fromisoformat(data["time"])
            moment = _CalendarEntry.model_validate(data)
            half_window = timedelta(minutes=moment.window_minutes)
            events.append(
                ScheduledEvent(
                    name=moment.name,
                    starts_at=moment.time - half_window,
                    ends_at=moment.time + half_window,
                )
            )
        return cls(tuple(events))


class _CalendarEntry(BaseModel):
    """Validation shape for one env-JSON calendar entry."""

    model_config = ConfigDict(frozen=True)

    name: str
    time: UtcDatetime
    window_minutes: int = DEFAULT_WINDOW_MINUTES
