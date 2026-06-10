"""Trend following: EMA cross entries with ATR-derived protective stops.

The starter strategy from ARCHITECTURE.md 4.2. Long-only (spot): a fast EMA
crossing above the slow EMA proposes an entry; crossing back below proposes a
full exit. The entry's stop price sits ``atr_stop_multiple`` ATRs below the
close — wider in volatile regimes, tighter in calm ones — which the risk
manager turns into position size.
"""

from __future__ import annotations

from decimal import ROUND_HALF_EVEN, Decimal

from pydantic import BaseModel, ConfigDict

from tradebot.core.models import ACCOUNTING_RESOLUTION, Candle, Side, Signal
from tradebot.indicators import Atr, Ema
from tradebot.portfolio import Position


class TrendFollowingConfig(BaseModel):
    """Periods and stop width; defaults are the conventional 20/50 cross."""

    model_config = ConfigDict(frozen=True)

    fast_ema_period: int = 20
    slow_ema_period: int = 50
    atr_period: int = 14
    atr_stop_multiple: float = 2.0


class TrendFollowingStrategy:
    """EMA-cross trend follower for one symbol.

    Indicator math runs in floats (permitted: it never feeds an order size);
    the stop price is converted to ``Decimal`` at the signal boundary because
    the risk manager derives the position size from it.
    """

    def __init__(self, config: TrendFollowingConfig) -> None:
        """Validate the config and reset all indicator state."""
        if config.fast_ema_period >= config.slow_ema_period:
            raise ValueError(
                f"fast EMA period {config.fast_ema_period} must be shorter than "
                f"slow {config.slow_ema_period}"
            )
        self._config = config
        self._fast = Ema(config.fast_ema_period)
        self._slow = Ema(config.slow_ema_period)
        self._atr = Atr(config.atr_period)
        self._previous_fast_above: bool | None = None

    @property
    def name(self) -> str:
        """Stable identifier for signal lineage."""
        return "trend_following"

    def on_candle(self, candle: Candle, position: Position | None) -> Signal | None:
        """Update indicators and propose an entry/exit on EMA crosses."""
        close = float(candle.close_quote)
        fast = self._fast.update(close)
        slow = self._slow.update(close)
        atr = self._atr.update(float(candle.high_quote), float(candle.low_quote), close)
        if fast is None or slow is None or atr is None:
            return None

        fast_above = fast > slow
        crossed_up = self._previous_fast_above is False and fast_above
        crossed_down = self._previous_fast_above is True and not fast_above
        self._previous_fast_above = fast_above

        if crossed_up and position is None:
            stop = Decimal(str(close - self._config.atr_stop_multiple * atr)).quantize(
                ACCOUNTING_RESOLUTION, rounding=ROUND_HALF_EVEN
            )
            if stop <= 0:
                return None  # degenerate volatility; no defined invalidation point
            return Signal(
                strategy_name=self.name,
                symbol=candle.symbol,
                side=Side.BUY,
                confidence=1.0,
                stop_price_quote=stop,
                reasons=(
                    f"fast EMA({self._config.fast_ema_period}) crossed above "
                    f"slow EMA({self._config.slow_ema_period})",
                    f"stop at {self._config.atr_stop_multiple} x ATR below close",
                ),
                created_at=candle.close_time,
            )
        if crossed_down and position is not None:
            return Signal(
                strategy_name=self.name,
                symbol=candle.symbol,
                side=Side.SELL,
                confidence=1.0,
                # Informational for exits: the position is being closed, not stopped.
                stop_price_quote=candle.close_quote,
                reasons=(
                    f"fast EMA({self._config.fast_ema_period}) crossed below "
                    f"slow EMA({self._config.slow_ema_period})",
                ),
                created_at=candle.close_time,
            )
        return None
