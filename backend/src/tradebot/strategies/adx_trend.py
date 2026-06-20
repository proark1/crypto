"""ADX/DMI trend: enter only when the trend is strong and turning up.

A trend family gated on *trend strength*, distinct from the EMA-crossover and
Supertrend followers: it buys when the directional indicators turn up
(+DI crosses above -DI) **and** ADX confirms the move is a real trend rather
than noise (ADX at or above a threshold), and exits when direction flips back
down (-DI crosses above +DI). The ADX gate is the whole point — it keeps the
family out of the chop that whipsaws a plain crossover, which is exactly the
orthogonal behaviour the research tournament can grade. Long-only, with the
shared ATR stop convention so the risk manager sizes it identically.

Research-first wiring on purpose: registered for sweeps, evaluation, the
§12.7 improvement rotation, and the strategy competition, but **not** routed
in production yet (the §13.7 human decision the evidence informs).
"""

from __future__ import annotations

from datetime import datetime
from decimal import ROUND_HALF_EVEN, Decimal

from pydantic import BaseModel, ConfigDict

from tradebot.core.models import ACCOUNTING_RESOLUTION, Candle, Side, Signal
from tradebot.indicators import Adx, Atr
from tradebot.portfolio import Position


class AdxTrendConfig(BaseModel):
    """DMI period, trend-strength gate, and stop convention."""

    model_config = ConfigDict(frozen=True)

    adx_period: int = 14
    """Length of the ADX / directional-movement smoothing."""

    adx_threshold: float = 25.0
    """The minimum ADX an entry requires — the trend-strength gate. Classic
    practice treats ADX below ~20-25 as 'no trend'. A *higher* threshold
    demands a stronger trend (fewer, higher-conviction entries); a *lower*
    one admits weaker trends."""

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


class AdxTrendStrategy:
    """+DI/-DI crossover entries gated on ADX strength, for one symbol.

    Indicator math runs in floats (permitted: it never feeds an order size
    directly); the stop converts to ``Decimal`` at the signal boundary. State
    is O(1) per candle — the ADX/DMI indicator, the ATR, and whether +DI sat
    above -DI on the prior candle, to detect a crossover.
    """

    def __init__(self, config: AdxTrendConfig) -> None:
        """Validate the config and reset all indicator state."""
        if config.atr_stop_multiple <= 0:
            raise ValueError(f"atr_stop_multiple must be > 0, got {config.atr_stop_multiple}")
        self._config = config
        self._adx = Adx(config.adx_period)
        self._atr = Atr(config.atr_period)
        self._plus_over_minus: bool | None = None
        self._last_open_time: datetime | None = None

    @property
    def name(self) -> str:
        """Stable identifier for signal lineage."""
        return "adx_trend"

    def on_candle(self, candle: Candle, position: Position | None) -> Signal | None:
        """Buy a +DI cross-up confirmed by ADX; exit on the -DI cross-up.

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
        adx = self._adx.update(high, low, close)
        plus_di = self._adx.plus_di
        minus_di = self._adx.minus_di
        if adx is None or atr is None or plus_di is None or minus_di is None:
            return None  # indicators still warming

        was_plus_over_minus = self._plus_over_minus
        plus_over_minus = plus_di > minus_di
        self._plus_over_minus = plus_over_minus

        signal_id = f"{self.name}:{candle.symbol}:{candle.close_time.isoformat()}"
        if position is not None:
            if was_plus_over_minus and not plus_over_minus:
                return Signal(
                    signal_id=signal_id,
                    strategy_name=self.name,
                    symbol=candle.symbol,
                    side=Side.SELL,
                    confidence=1.0,
                    stop_price_quote=candle.close_quote,  # informational: full exit
                    reasons=(f"-DI {minus_di:g} crossed back above +DI {plus_di:g}",),
                    created_at=candle.close_time,
                )
            return None

        # Entry: +DI just crossed above -DI (an up turn) and ADX confirms the
        # trend is strong enough to trade.
        crossed_up = was_plus_over_minus is False and plus_over_minus
        if not crossed_up or adx < self._config.adx_threshold:
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
                f"+DI {plus_di:g} crossed above -DI {minus_di:g} with ADX {adx:g} "
                f">= {self._config.adx_threshold:g} (strong trend)",
                f"stop at {self._config.atr_stop_multiple} x ATR below close",
            ),
            created_at=candle.close_time,
        )
