"""Managed protective stops: one ratchet shared by engine and evaluator.

A stop only ever moves *up* (long-only spot): breakeven locks the entry in
once the trade has earned ``breakeven_at_r`` of its initial risk, and a
trailing distance follows the highest high since entry. The same object
runs inside the paper trading engine and the scenario evaluator — two
implementations would drift, and research would grade a bot that does not
exist (CLAUDE.md invariant 3).

Money stays ``Decimal`` end to end; the per-candle update is O(1).
"""

from __future__ import annotations

from decimal import Decimal

from tradebot.core.models import Candle, Signal


class ManagedStop:
    """The protective stop of one open long, ratcheting per closed candle."""

    def __init__(
        self,
        entry_price_quote: Decimal,
        initial_stop_quote: Decimal,
        breakeven_at_r: float = 0.0,
        trail_distance_quote: Decimal | None = None,
    ) -> None:
        """Arm the stop; the initial level is the signal's invalidation point."""
        self._entry = entry_price_quote
        self._stop = initial_stop_quote
        self._risk = entry_price_quote - initial_stop_quote
        self._breakeven_at_r = Decimal(str(breakeven_at_r))
        self._trail = trail_distance_quote

    @classmethod
    def from_signal(cls, signal: Signal, entry_price_quote: Decimal) -> ManagedStop:
        """Arm from the entry signal that opened the position."""
        return cls(
            entry_price_quote=entry_price_quote,
            initial_stop_quote=signal.stop_price_quote,
            breakeven_at_r=signal.breakeven_at_r,
            trail_distance_quote=signal.trail_distance_quote,
        )

    @property
    def stop_price_quote(self) -> Decimal:
        """The current protective level (monotone non-decreasing)."""
        return self._stop

    def is_breached_by(self, candle: Candle) -> bool:
        """Whether ``candle`` traded at or through the *current* stop.

        Checked before :meth:`ratchet` on each candle, in both the engine
        and the evaluator: a candle must not raise the stop above its own
        low and then claim to have stopped out at the raised level.
        """
        return candle.low_quote <= self._stop

    def ratchet(self, candle: Candle) -> Decimal:
        """Raise the stop per the policy; returns the (new) level.

        Breakeven uses the candle high against the initial risk unit; the
        trail follows the high at its frozen distance. Disabled policies
        leave the signal's stop exactly where the strategy put it.
        """
        if self._breakeven_at_r > 0 and self._risk > 0:
            earned_r = (candle.high_quote - self._entry) / self._risk
            if earned_r >= self._breakeven_at_r:
                self._stop = max(self._stop, self._entry)
        if self._trail is not None:
            self._stop = max(self._stop, candle.high_quote - self._trail)
        return self._stop
