"""Circuit breakers: account-level brakes that stop new entries.

These are the runaway brakes from LIVE_TRADING_CHECKLIST.md item 6, shipped
before the paper soak so the soak exercises them. They observe equity and
closed trades through time and **block entries** when limits are breached;
exits are never blocked — capital protection (stops, kill switch) must keep
working precisely when the brakes are on.

Two severities, on purpose:

- **Hard trips** (daily loss, drawdown from peak) latch until a human calls
  ``reset()``: an account bleeding past its limits needs eyes, not a timer.
- **Soft brakes** (loss-streak cooldown, daily entry cap) clear on their own
  — by time and by UTC day rollover respectively — because they guard against
  churn, not catastrophe.

All time comes from candle/fill timestamps, never the wall clock, so the
breakers behave identically in backtest, paper, and live (CLAUDE.md
invariant 3) and their effect is reproducible in the golden backtest.
"""

from __future__ import annotations

import logging
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from pydantic import BaseModel, ConfigDict

logger = logging.getLogger(__name__)


class BreakerConfig(BaseModel):
    """Limits for the account-level brakes; defaults are conservative."""

    model_config = ConfigDict(frozen=True)

    max_daily_loss_fraction: Decimal = Decimal("0.03")
    """Hard trip when equity falls this fraction below the UTC day's start."""

    max_drawdown_fraction: Decimal = Decimal("0.20")
    """Hard trip when equity falls this fraction below its all-time peak."""

    loss_streak_threshold: int = 3
    """Consecutive losing round trips that start the entry cooldown."""

    loss_streak_cooldown: timedelta = timedelta(hours=4)
    """How long entries stay blocked after a loss streak."""

    max_entries_per_day: int = 10
    """Entry orders allowed per UTC day (an overtrading guard, not a PnL one)."""


class BreakerState(BaseModel):
    """A point-in-time snapshot of every mutable breaker field.

    Persisted across restarts: a tripped breaker that forgets it tripped on
    deploy would resume trading exactly when a human was meant to look. All
    fields mirror :class:`CircuitBreakers`' internals one to one.
    """

    model_config = ConfigDict(frozen=True)

    tripped_reason: str | None = None
    day: date | None = None
    day_start_equity_quote: Decimal | None = None
    entries_today: int = 0
    peak_equity_quote: Decimal | None = None
    consecutive_losses: int = 0
    cooldown_until: datetime | None = None
    last_observed_time: datetime | None = None


def _require_aware(moment: datetime) -> datetime:
    """Reject naive datetimes (CLAUDE.md invariant 2) and normalize to UTC."""
    if moment.tzinfo is None:
        raise ValueError(f"naive datetime passed to circuit breakers: {moment!r}")
    return moment.astimezone(UTC)


class CircuitBreakers:
    """Stateful brakes, advanced by equity observations and closed trades.

    Drive it with ``observe`` (every candle close), ``record_closed_trade``
    (every position-reducing fill), and ``record_entry`` (every submitted
    entry); ask ``entry_block_reason`` before sizing an entry. The split
    keeps this class free of portfolio knowledge — the risk manager owns
    translating its state into these calls.
    """

    def __init__(self, config: BreakerConfig) -> None:
        """Start with no history: the first observation anchors day and peak."""
        self._config = config
        self._tripped_reason: str | None = None
        self._day: date | None = None
        self._day_start_equity_quote: Decimal | None = None
        self._entries_today = 0
        self._peak_equity_quote: Decimal | None = None
        self._consecutive_losses = 0
        self._cooldown_until: datetime | None = None
        self._last_observed_time: datetime | None = None

    @property
    def tripped_reason(self) -> str | None:
        """Why the hard trip latched, or ``None`` if not tripped."""
        return self._tripped_reason

    @property
    def cooldown_until(self) -> datetime | None:
        """When the loss-streak cooldown ends, or ``None`` if not cooling."""
        return self._cooldown_until

    @property
    def entries_today(self) -> int:
        """Entry orders recorded in the current UTC day."""
        return self._entries_today

    def observe(self, now: datetime, equity_quote: Decimal) -> None:
        """Advance time and equity; may latch a hard trip.

        Call on every candle close with post-fill equity. The first
        observation of a new UTC day anchors that day's loss limit and
        resets the entry count; the peak only ever rises.
        """
        now = _require_aware(now)
        self._last_observed_time = now
        today = now.date()
        if today != self._day:
            self._day = today
            self._day_start_equity_quote = equity_quote
            self._entries_today = 0
        if self._peak_equity_quote is None or equity_quote > self._peak_equity_quote:
            self._peak_equity_quote = equity_quote
        if self._tripped_reason is not None:
            return  # already latched; keep the original reason
        day_start = self._day_start_equity_quote
        if day_start is not None and day_start > 0:
            daily_limit = day_start * (1 - self._config.max_daily_loss_fraction)
            if equity_quote <= daily_limit:
                self._trip(
                    f"daily loss limit: equity {equity_quote} fell "
                    f"{self._config.max_daily_loss_fraction} below day start {day_start}"
                )
                return
        peak = self._peak_equity_quote
        if peak is not None and peak > 0:
            drawdown_limit = peak * (1 - self._config.max_drawdown_fraction)
            if equity_quote <= drawdown_limit:
                self._trip(
                    f"drawdown limit: equity {equity_quote} fell "
                    f"{self._config.max_drawdown_fraction} below peak {peak}"
                )

    def record_closed_trade(self, realized_pnl_delta_quote: Decimal, now: datetime) -> None:
        """Track the loss streak from one round trip's realized PnL.

        A loss extends the streak; a flat or winning trade clears it. At the
        threshold the cooldown starts and the streak resets — the cooldown
        itself is the consequence, and the count starts fresh after it.
        """
        now = _require_aware(now)
        if realized_pnl_delta_quote >= 0:
            self._consecutive_losses = 0
            return
        self._consecutive_losses += 1
        if self._consecutive_losses >= self._config.loss_streak_threshold:
            self._cooldown_until = now + self._config.loss_streak_cooldown
            self._consecutive_losses = 0
            logger.warning(
                "loss streak of %d: entries blocked until %s",
                self._config.loss_streak_threshold,
                self._cooldown_until.isoformat(),
            )

    def record_entry(self, now: datetime) -> None:
        """Count one submitted entry against the daily cap."""
        _require_aware(now)
        self._entries_today += 1

    def entry_block_reason(self, now: datetime) -> str | None:
        """Why a new entry must be vetoed right now, or ``None`` to allow it.

        ``now`` is the signal's timestamp, which can be stale: a co-pilot
        proposal approved hours after creation re-runs risk checks with the
        original signal time. The brakes judge the latest market time the
        breakers have observed instead, so an old signal can neither dodge a
        cooldown that started after it nor stay blocked by one that has
        since expired.
        """
        now = _require_aware(now)
        if self._last_observed_time is not None and self._last_observed_time > now:
            now = self._last_observed_time
        if self._tripped_reason is not None:
            return f"circuit breaker tripped ({self._tripped_reason}); reset required"
        if self._cooldown_until is not None:
            if now < self._cooldown_until:
                return f"loss-streak cooldown until {self._cooldown_until.isoformat()}"
            self._cooldown_until = None
        if self._entries_today >= self._config.max_entries_per_day:
            return f"daily entry cap reached ({self._config.max_entries_per_day})"
        return None

    def reset(self) -> None:
        """Human reset: clear the hard trip and any cooldown.

        Day counters and the equity peak survive on purpose — resetting the
        breaker must not also forget how much has already been lost today.
        """
        logger.warning("circuit breakers reset by operator (was: %s)", self._tripped_reason)
        self._tripped_reason = None
        self._cooldown_until = None

    def snapshot(self) -> BreakerState:
        """Return the full mutable state, for persistence."""
        return BreakerState(
            tripped_reason=self._tripped_reason,
            day=self._day,
            day_start_equity_quote=self._day_start_equity_quote,
            entries_today=self._entries_today,
            peak_equity_quote=self._peak_equity_quote,
            consecutive_losses=self._consecutive_losses,
            cooldown_until=self._cooldown_until,
            last_observed_time=self._last_observed_time,
        )

    def restore(self, state: BreakerState) -> None:
        """Adopt a persisted snapshot (restart recovery, before any observe).

        A restored hard trip stays latched until the human reset it was
        waiting for; day anchors and the peak roll forward naturally from
        the next observation.
        """
        self._tripped_reason = state.tripped_reason
        self._day = state.day
        self._day_start_equity_quote = state.day_start_equity_quote
        self._entries_today = state.entries_today
        self._peak_equity_quote = state.peak_equity_quote
        self._consecutive_losses = state.consecutive_losses
        self._cooldown_until = state.cooldown_until
        self._last_observed_time = state.last_observed_time
        if state.tripped_reason is not None:
            logger.warning("restored tripped circuit breaker: %s", state.tripped_reason)

    def _trip(self, reason: str) -> None:
        self._tripped_reason = reason
        logger.error("circuit breaker tripped: %s", reason)
