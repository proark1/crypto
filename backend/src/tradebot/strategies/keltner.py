"""Keltner breakout: ride a volatility-channel breakout, exit at the basis.

A volatility-channel trend family distinct from the Donchian breakout and the
squeeze: the channel is an EMA basis with an ATR band ``channel_atr_multiple``
wide, so it is a *volatility* envelope rather than a price-range box. It buys
when the close breaks above the upper band — a thrust that clears the
recent volatility envelope — and exits when the close falls back to the EMA
basis (the trend's mean). Long-only, with the shared ATR stop convention so
the risk manager sizes it identically.

Distinct from the breakout family (a Donchian high-of-N channel, not a
volatility band) and the squeeze (which trades the release of a
Bollinger-inside-Keltner *compression*, not a plain channel break), so the
research tournament gets a genuinely different breakout hypothesis to grade.

Research-first wiring on purpose: registered for sweeps, evaluation, the
§12.7 improvement rotation, and the strategy competition, but **not** routed
in production yet (the §13.7 human decision the evidence informs).
"""

from __future__ import annotations

from datetime import datetime
from decimal import ROUND_HALF_EVEN, Decimal

from pydantic import BaseModel, ConfigDict

from tradebot.core.models import ACCOUNTING_RESOLUTION, Candle, Side, Signal
from tradebot.indicators import Atr, Ema
from tradebot.portfolio import Position


class KeltnerConfig(BaseModel):
    """Channel geometry and stop convention; defaults are the classic 20/2."""

    model_config = ConfigDict(frozen=True)

    ema_period: int = 20
    """Length of the EMA that forms the channel basis (and the exit level)."""

    atr_period: int = 10
    """Length of the ATR that sets the channel half-width."""

    channel_atr_multiple: float = 2.0
    """Channel half-width above the basis, in ATRs — how far price must thrust
    to break out. A *wider* channel demands a stronger thrust (fewer, later
    entries); a *tighter* one triggers on milder breaks."""

    atr_stop_multiple: float = 2.0
    """Protective stop distance below the entry close, in entry-time ATRs —
    the shared convention so the risk manager sizes every family alike."""

    breakeven_at_r: float = 0.0
    """Stop management: ratchet the stop to entry once the trade earns this
    many R. ``0`` disables."""

    trail_atr_multiple: float = 0.0
    """Stop management: trail the stop this many entry-time ATRs below the
    highest high since entry. ``0`` disables."""


class KeltnerStrategy:
    """Upper-band breakout entries, basis exits, for one symbol.

    Indicator math runs in floats (permitted: it never feeds an order size
    directly); the stop converts to ``Decimal`` at the signal boundary. State
    is O(1) per candle — the EMA basis, the ATR, and whether the prior close
    sat above the upper band (so the entry is the *break*, not every bar
    spent above it).
    """

    def __init__(self, config: KeltnerConfig) -> None:
        """Validate the config and reset all channel/indicator state."""
        if config.channel_atr_multiple <= 0:
            raise ValueError(f"channel_atr_multiple must be > 0, got {config.channel_atr_multiple}")
        if config.atr_stop_multiple <= 0:
            raise ValueError(f"atr_stop_multiple must be > 0, got {config.atr_stop_multiple}")
        self._config = config
        self._ema = Ema(config.ema_period)
        self._atr = Atr(config.atr_period)
        self._above_upper = False
        self._last_open_time: datetime | None = None

    @property
    def name(self) -> str:
        """Stable identifier for signal lineage."""
        return "keltner"

    def on_candle(self, candle: Candle, position: Position | None) -> Signal | None:
        """Buy the break above the upper channel; exit back at the basis.

        Candles must arrive in strictly increasing time order, the shared
        stateful-strategy contract: disorder raises rather than silently
        poisoning the channel state.
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
        basis = self._ema.update(close)
        if basis is None or atr is None:
            return None  # indicators still warming

        upper = basis + self._config.channel_atr_multiple * atr
        was_above_upper = self._above_upper
        self._above_upper = close > upper

        signal_id = f"{self.name}:{candle.symbol}:{candle.close_time.isoformat()}"
        if position is not None:
            if close < basis:
                return Signal(
                    signal_id=signal_id,
                    strategy_name=self.name,
                    symbol=candle.symbol,
                    side=Side.SELL,
                    confidence=1.0,
                    stop_price_quote=candle.close_quote,  # informational: full exit
                    reasons=(f"close {close:g} fell back to the channel basis {basis:g}",),
                    created_at=candle.close_time,
                )
            return None

        # Entry is the break itself: the prior close was inside the channel and
        # this close cleared the upper band.
        if was_above_upper or close <= upper:
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
                f"close {close:g} broke above the {self._config.channel_atr_multiple:g}x ATR "
                f"Keltner channel ceiling {upper:g}",
                f"stop at {self._config.atr_stop_multiple} x ATR below close",
            ),
            created_at=candle.close_time,
        )
