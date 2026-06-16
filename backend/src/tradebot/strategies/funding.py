"""Funding-rate contrarian: long the crowded-short capitulation, spot-only.

A sixth strategy family, the first that trades on a non-price signal. Perpetual
funding is what longs pay shorts each funding window (negative when shorts pay
longs); a deeply negative rate means over-crowded, over-leveraged shorts, whose
squeeze historically resolves *upward*. So this enters long when funding sits at
or below a (negative) threshold — buying the capitulation, not chasing it — and
exits once funding recovers to or above a second threshold, where the
short-crowding has unwound (and positive funding is the opposite risk, crowded
longs). Long-only because the bot trades spot.

Funding prints only every few hours, far coarser than the trade timeframe, so
the rate is looked up per candle from an injected :class:`FundingProvider`
(backed in research by the stored history, in live by the same store) rather
than recomputed — the §3 one-code-path rule. With no provider, or before any
funding is known, the strategy simply has no opinion: an empty funding series is
a fail-safe, never an error. The stop sits ``atr_stop_multiple`` ATRs below the
close, the same convention as every other family, so risk sizing is identical.
"""

from __future__ import annotations

from datetime import datetime
from decimal import ROUND_HALF_EVEN, Decimal
from typing import Protocol

from pydantic import BaseModel, ConfigDict

from tradebot.core.models import ACCOUNTING_RESOLUTION, Candle, Side, Signal
from tradebot.indicators import Atr
from tradebot.portfolio import Position


class FundingProvider(Protocol):
    """Look up the funding rate in effect at a moment, for a symbol.

    ``rate_as_of`` returns the most recent funding print at or before ``at`` (a
    per-interval signed fraction), or ``None`` when no funding is known for the
    symbol at that time — so a strategy reading it degrades to "no opinion"
    rather than guessing.
    """

    def rate_as_of(self, symbol: str, at: datetime) -> Decimal | None:
        """Funding rate at or before ``at`` for ``symbol``, or ``None``."""
        ...


class FundingConfig(BaseModel):
    """Entry/exit funding thresholds and the ATR stop.

    Defaults are deliberately wide so a sweep tightens them rather than the code
    guessing an edge.
    """

    model_config = ConfigDict(frozen=True)

    enter_funding_at_or_below: float = -0.0005
    """Enter long when the latest funding rate is at or below this (per funding
    window). Deeply negative funding = shorts paying longs = over-crowded shorts
    whose squeeze tends to resolve up. Negative by design; contrarian."""

    exit_funding_at_or_above: float = 0.0
    """Exit once funding recovers to at or above this: the short-crowding that
    motivated the entry has unwound (and positive funding is crowded longs, the
    opposite risk). Must sit above the entry threshold."""

    atr_period: int = 14
    atr_stop_multiple: float = 2.0

    breakeven_at_r: float = 0.0
    """Ratchet the stop to entry once the trade has earned this many R. ``0``
    disables (same stop-management knobs as the other families)."""

    trail_atr_multiple: float = 0.0
    """Trail the stop this many entry-time ATRs below the highest high since
    entry. ``0`` disables."""


class FundingStrategy:
    """Funding-rate contrarian reverter for one symbol (long-only spot).

    Indicator math (ATR) runs in floats — permitted, it never feeds an order
    size; the stop price is converted to ``Decimal`` at the signal boundary
    because the risk manager sizes from it. The funding rate is already a
    ``Decimal`` (exact, from the store), compared against the thresholds without
    a float round-trip.
    """

    def __init__(self, config: FundingConfig, funding: FundingProvider | None = None) -> None:
        """Validate the threshold band and reset indicator state.

        ``funding`` is injected by the builders (research and live alike); when
        it is ``None`` the strategy is inert by design — a sweep that never wired
        a provider grades it as "no trades", not a crash.
        """
        if config.enter_funding_at_or_below >= config.exit_funding_at_or_above:
            raise ValueError(
                f"entry funding {config.enter_funding_at_or_below} must sit below the exit "
                f"funding {config.exit_funding_at_or_above}"
            )
        self._config = config
        self._funding = funding
        self._atr = Atr(config.atr_period)
        # Converted once: on_candle compares the Decimal funding rate against
        # these every candle, so the conversion does not belong in the hot path.
        self._enter_at = Decimal(str(config.enter_funding_at_or_below))
        self._exit_at = Decimal(str(config.exit_funding_at_or_above))
        self._last_open_time: datetime | None = None

    @property
    def name(self) -> str:
        """Stable identifier for signal lineage."""
        return "funding"

    def on_candle(self, candle: Candle, position: Position | None) -> Signal | None:
        """Update the ATR and propose entries/exits from the funding rate.

        Candles must arrive in strictly increasing time order, same contract as
        every stateful strategy.
        """
        if self._last_open_time is not None and candle.open_time <= self._last_open_time:
            raise ValueError(
                f"out-of-order or duplicate candle: {candle.open_time.isoformat()} after "
                f"{self._last_open_time.isoformat()}"
            )
        self._last_open_time = candle.open_time

        close = float(candle.close_quote)
        atr = self._atr.update(float(candle.high_quote), float(candle.low_quote), close)
        if atr is None:
            return None
        rate = (
            self._funding.rate_as_of(candle.symbol, candle.open_time)
            if self._funding is not None
            else None
        )
        if rate is None:
            return None  # no funding known here: no opinion (fail-safe)

        signal_id = f"{self.name}:{candle.symbol}:{candle.close_time.isoformat()}"
        if position is None and rate <= self._enter_at:
            stop = Decimal(str(close - self._config.atr_stop_multiple * atr)).quantize(
                ACCOUNTING_RESOLUTION, rounding=ROUND_HALF_EVEN
            )
            if stop <= 0:
                return None  # degenerate volatility; no defined invalidation point
            return Signal(
                signal_id=signal_id,
                strategy_name=self.name,
                symbol=candle.symbol,
                side=Side.BUY,
                confidence=1.0,
                stop_price_quote=stop,
                breakeven_at_r=self._config.breakeven_at_r,
                trail_distance_quote=(
                    Decimal(str(self._config.trail_atr_multiple * atr)).quantize(
                        ACCOUNTING_RESOLUTION, rounding=ROUND_HALF_EVEN
                    )
                    if self._config.trail_atr_multiple > 0
                    else None
                ),
                reasons=(
                    f"funding {rate} <= {self._config.enter_funding_at_or_below:g}: "
                    "crowded shorts, squeeze risk up",
                    f"stop at {self._config.atr_stop_multiple} x ATR below close",
                ),
                created_at=candle.close_time,
            )
        if position is not None and rate >= self._exit_at:
            return Signal(
                signal_id=signal_id,
                strategy_name=self.name,
                symbol=candle.symbol,
                side=Side.SELL,
                confidence=1.0,
                # Informational for exits: the position is being closed, not stopped.
                stop_price_quote=candle.close_quote,
                reasons=(
                    f"funding {rate} >= {self._config.exit_funding_at_or_above:g}: "
                    "short-crowding unwound",
                ),
                created_at=candle.close_time,
            )
        return None
