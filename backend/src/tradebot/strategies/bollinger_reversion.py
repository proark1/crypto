"""Bollinger mean reversion: buy the recovery off the lower band.

A band-based reversion distinct from the RSI family: it buys when the close
falls *through* the lower Bollinger band (a stretched, oversold move) and
then closes back *inside* it — the snap-back, not the fall itself, so it
never tries to catch a knife mid-drop — and exits at the basis (the middle
band, the moving-average mean the price is reverting to). Long-only like
every family, with the shared ATR stop convention so the risk manager sizes
it identically.

Distinct from the squeeze family (which trades the *breakout* out of a
Bollinger compression, not the reversion to its mean) and from the RSI mean
reversion (an oscillator threshold, not a price-band stretch), so the
research tournament gets a genuinely different reversion hypothesis to grade.

Research-first wiring on purpose: registered for sweeps, evaluation, the
§12.7 improvement rotation, and the strategy competition, but **not** routed
in production yet (the §13.7 human decision the evidence informs).
"""

from __future__ import annotations

from datetime import datetime
from decimal import ROUND_HALF_EVEN, Decimal

from pydantic import BaseModel, ConfigDict

from tradebot.core.models import ACCOUNTING_RESOLUTION, Candle, Side, Signal
from tradebot.indicators import Atr, Bollinger
from tradebot.portfolio import Position


class BollingerReversionConfig(BaseModel):
    """Band geometry and stop convention; defaults are the classic 20/2."""

    model_config = ConfigDict(frozen=True)

    bollinger_period: int = 20
    """Length of the Bollinger moving average and standard deviation."""

    num_stddev: float = 2.0
    """Band width in standard deviations — how stretched a move must be to
    count as oversold. A *wider* band demands a more extreme stretch (fewer,
    higher-conviction entries); a *tighter* one triggers on milder dips."""

    atr_period: int = 14
    atr_stop_multiple: float = 2.0
    """Protective stop distance below the entry close, in entry-time ATRs —
    the shared convention so the risk manager sizes every family alike."""

    breakeven_at_r: float = 0.0
    """Stop management: ratchet the stop to entry once the trade earns this
    many R. ``0`` disables."""

    trail_atr_multiple: float = 0.0
    """Stop management: trail the stop this many entry-time ATRs below the
    highest high since entry. ``0`` disables."""


class BollingerReversionStrategy:
    """Lower-band recovery reversion for one symbol.

    Indicator math runs in floats (permitted: it never feeds an order size
    directly); the stop converts to ``Decimal`` at the signal boundary. State
    is O(1) per candle — the band, the ATR, and whether the prior close sat
    below the lower band.
    """

    def __init__(self, config: BollingerReversionConfig) -> None:
        """Validate the config and reset all band/indicator state."""
        if config.atr_stop_multiple <= 0:
            raise ValueError(f"atr_stop_multiple must be > 0, got {config.atr_stop_multiple}")
        self._config = config
        self._bollinger = Bollinger(config.bollinger_period, config.num_stddev)
        self._atr = Atr(config.atr_period)
        self._below_lower = False
        self._last_open_time: datetime | None = None

    @property
    def name(self) -> str:
        """Stable identifier for signal lineage."""
        return "bollinger_reversion"

    def on_candle(self, candle: Candle, position: Position | None) -> Signal | None:
        """Buy the recovery off the lower band; exit at the basis.

        Candles must arrive in strictly increasing time order, the shared
        stateful-strategy contract: disorder raises rather than silently
        poisoning the band state.
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
        bands = self._bollinger.update(close)
        if bands is None or atr is None:
            return None  # indicators still warming

        was_below_lower = self._below_lower
        self._below_lower = close < bands.lower

        signal_id = f"{self.name}:{candle.symbol}:{candle.close_time.isoformat()}"
        if position is not None:
            if close >= bands.middle:
                return Signal(
                    signal_id=signal_id,
                    strategy_name=self.name,
                    symbol=candle.symbol,
                    side=Side.SELL,
                    confidence=1.0,
                    stop_price_quote=candle.close_quote,  # informational: full exit
                    reasons=(f"close {close:g} reverted to the band basis {bands.middle:g}",),
                    created_at=candle.close_time,
                )
            return None

        # The entry is the recovery: the prior close pierced below the lower
        # band, and this close has snapped back inside it — never the fall.
        if not (was_below_lower and close >= bands.lower):
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
                f"close {close:g} recovered back above the lower band {bands.lower:g}",
                f"stop at {self._config.atr_stop_multiple} x ATR below close",
            ),
            created_at=candle.close_time,
        )
