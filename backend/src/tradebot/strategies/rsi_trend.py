"""RSI-midline momentum: buy strength crossing up, not weakness reverting.

An RSI family that uses the oscillator the *opposite* way to mean reversion.
The mean-reversion family buys RSI **oversold** (a stretched move it expects to
snap back); this one buys RSI **crossing up through the midline** — momentum
building, the move it expects to continue — and exits when RSI falls back below
an exit level. Same indicator, opposite hypothesis, so the research tournament
can grade momentum-RSI against reversion-RSI on identical scenarios. Long-only,
shared ATR stop convention.

Distinct from ``mean_reversion`` (RSI oversold → buy the dip) precisely because
the entry is a midline *cross-up* under building strength, not an extreme; the
two families will disagree on the same tape, which is the point.

Research-first wiring on purpose: registered for sweeps, evaluation, the §12.7
improvement rotation, and the strategy competition, but **not** routed in
production yet (the §13.7 human decision the evidence informs).
"""

from __future__ import annotations

from datetime import datetime
from decimal import ROUND_HALF_EVEN, Decimal

from pydantic import BaseModel, ConfigDict

from tradebot.core.models import ACCOUNTING_RESOLUTION, Candle, Side, Signal
from tradebot.indicators import Atr, Rsi
from tradebot.portfolio import Position


class RsiTrendConfig(BaseModel):
    """RSI period, entry/exit levels, and stop convention; defaults are 14/50/45."""

    model_config = ConfigDict(frozen=True)

    rsi_period: int = 14
    """Length of the RSI (Wilder's, TA-Lib seeding) that gauges momentum."""

    entry_level: float = 50.0
    """The defining knob: a *cross up through* this RSI level opens a position —
    momentum turning constructive. A *higher* level (e.g. 55) demands clearer
    strength (fewer, later entries); the classic midline is 50."""

    exit_level: float = 45.0
    """RSI at or below which an open position exits — momentum lost. Set a
    touch below ``entry_level`` so a single bar wavering on the midline does
    not immediately round-trip the trade."""

    atr_period: int = 14
    """Length of the ATR that sets the protective stop distance."""

    atr_stop_multiple: float = 2.0
    """Protective stop distance below the entry close, in entry-time ATRs —
    the shared convention so the risk manager sizes every family alike."""

    breakeven_at_r: float = 0.0
    """Stop management: ratchet the stop to entry once the trade earns this
    many R. ``0`` disables."""

    trail_atr_multiple: float = 0.0
    """Stop management: trail the stop this many entry-time ATRs below the
    highest high since entry. ``0`` disables."""


class RsiTrendStrategy:
    """RSI midline cross-up entries, RSI exit-level exits, for one symbol.

    Indicator math runs in floats (permitted: it never feeds an order size
    directly); the stop converts to ``Decimal`` at the signal boundary. State
    is O(1) per candle — the RSI, the ATR, and whether the prior RSI already
    sat at/above the entry level (so the entry is the *cross*, not every bar
    spent above it).
    """

    def __init__(self, config: RsiTrendConfig) -> None:
        """Validate the config and reset all indicator state."""
        if not 0.0 < config.entry_level < 100.0:
            raise ValueError(f"entry_level must be in (0, 100), got {config.entry_level}")
        if config.exit_level > config.entry_level:
            raise ValueError(
                f"exit_level {config.exit_level} must not exceed entry_level {config.entry_level}"
            )
        if config.atr_stop_multiple <= 0:
            raise ValueError(f"atr_stop_multiple must be > 0, got {config.atr_stop_multiple}")
        self._config = config
        self._rsi = Rsi(config.rsi_period)
        self._atr = Atr(config.atr_period)
        self._at_or_above_entry = False
        self._last_open_time: datetime | None = None

    @property
    def name(self) -> str:
        """Stable identifier for signal lineage."""
        return "rsi_trend"

    def on_candle(self, candle: Candle, position: Position | None) -> Signal | None:
        """Buy an RSI cross up through the entry level; exit below the exit level.

        Candles must arrive in strictly increasing time order, the shared
        stateful-strategy contract: disorder raises rather than silently
        poisoning the indicator state.
        """
        if self._last_open_time is not None and candle.open_time <= self._last_open_time:
            raise ValueError(
                f"out-of-order or duplicate candle: {candle.open_time.isoformat()} after "
                f"{self._last_open_time.isoformat()}"
            )
        self._last_open_time = candle.open_time

        high = float(candle.high_quote)
        low = float(candle.low_quote)
        close = float(candle.close_quote)
        atr = self._atr.update(high, low, close)
        rsi = self._rsi.update(close)
        if rsi is None or atr is None:
            return None  # indicators still warming

        was_at_or_above_entry = self._at_or_above_entry
        self._at_or_above_entry = rsi >= self._config.entry_level

        signal_id = f"{self.name}:{candle.symbol}:{candle.close_time.isoformat()}"
        if position is not None:
            if rsi <= self._config.exit_level:
                return Signal(
                    signal_id=signal_id,
                    strategy_name=self.name,
                    symbol=candle.symbol,
                    side=Side.SELL,
                    confidence=1.0,
                    stop_price_quote=candle.close_quote,  # informational: full exit
                    reasons=(f"RSI {rsi:.1f} fell to the exit level {self._config.exit_level:g}",),
                    created_at=candle.close_time,
                )
            return None

        # Entry is the cross itself: the prior RSI sat below the entry level and
        # this one reached it — not every bar already above.
        if was_at_or_above_entry or rsi < self._config.entry_level:
            return None
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
                f"RSI {rsi:.1f} crossed up through the entry level {self._config.entry_level:g}",
                f"stop at {self._config.atr_stop_multiple} x ATR below close",
            ),
            created_at=candle.close_time,
        )
