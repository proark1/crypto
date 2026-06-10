"""Clock abstraction so the same code path works live and in backtests.

Components that need "now" take a :class:`Clock` instead of calling
``datetime.now`` directly. Live and paper trading use :class:`WallClock`;
the backtester drives a :class:`SimulatedClock` forward as it replays candles.
This is part of the one-code-path invariant: strategy and risk code must not
behave differently because of where time comes from.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Protocol


class Clock(Protocol):
    """Source of the current time as a UTC-aware datetime."""

    def now(self) -> datetime:
        """Return the current time, timezone-aware, in UTC."""
        ...


class WallClock:
    """Real system time for paper and live trading."""

    def now(self) -> datetime:
        """Return the current wall-clock time in UTC."""
        return datetime.now(tz=UTC)


class SimulatedClock:
    """Backtest time, advanced explicitly by the backtest runner.

    Time can only move forward; replaying events out of order is a runner bug
    and raises immediately rather than corrupting indicator state silently.
    """

    def __init__(self, start: datetime) -> None:
        """Start the clock at ``start``, which must be timezone-aware."""
        if start.tzinfo is None:
            raise ValueError("SimulatedClock start must be timezone-aware UTC")
        self._now = start.astimezone(UTC)

    def now(self) -> datetime:
        """Return the current simulated time in UTC."""
        return self._now

    def advance_to(self, moment: datetime) -> None:
        """Move simulated time forward to ``moment`` (UTC-aware, monotonic)."""
        if moment.tzinfo is None:
            raise ValueError("advance_to requires a timezone-aware datetime")
        moment = moment.astimezone(UTC)
        if moment < self._now:
            raise ValueError(
                f"simulated time may not go backwards: {moment.isoformat()} < "
                f"{self._now.isoformat()}"
            )
        self._now = moment
