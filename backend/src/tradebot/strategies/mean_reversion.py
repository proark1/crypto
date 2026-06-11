"""Mean reversion: oversold-recovery entries with ATR-derived stops.

The second strategy family (ARCHITECTURE.md 5.2): active when the regime
gate says the market is ranging, where the trend follower has no edge.
Long-only (spot): an RSI dip below the oversold line followed by a close
back above it proposes an entry — buying the *recovery*, never the falling
knife — and the position exits when RSI mean-reverts to its midline. The
stop sits ``atr_stop_multiple`` ATRs below the close, same convention as
the trend family, so risk sizing treats both families identically.
"""

from __future__ import annotations

from datetime import datetime
from decimal import ROUND_HALF_EVEN, Decimal

from pydantic import BaseModel, ConfigDict

from tradebot.core.models import ACCOUNTING_RESOLUTION, Candle, Side, Signal
from tradebot.indicators import Atr, Ema, Rsi
from tradebot.portfolio import Position


class MeanReversionConfig(BaseModel):
    """Thresholds and periods; defaults are the conventional RSI(14) levels."""

    model_config = ConfigDict(frozen=True)

    rsi_period: int = 14
    oversold_threshold: float = 30.0
    exit_rsi: float = 55.0
    """Exit when RSI recovers past its midline: the reversion happened."""

    atr_period: int = 14
    atr_stop_multiple: float = 2.0

    breakeven_at_r: float = 0.0
    """Stop management: ratchet the stop to entry once the trade has earned
    this many R. ``0`` disables (the historical behavior)."""

    trail_atr_multiple: float = 0.0
    """Stop management: trail the stop this many entry-time ATRs below the
    highest high since entry. ``0`` disables (the historical behavior)."""

    trend_filter_ema_period: int = 0
    """Only buy oversold recoveries while the close sits above this EMA —
    a dip in an uptrend mean-reverts; a dip in a downtrend is a falling
    knife, and the evaluation system's "entries lose money when trend is
    down" finding is exactly this failure. ``0`` disables the filter (the
    historical behavior)."""


class MeanReversionStrategy:
    """RSI oversold-recovery reverter for one symbol.

    Indicator math runs in floats (permitted: it never feeds an order size);
    the stop price is converted to ``Decimal`` at the signal boundary because
    the risk manager derives the position size from it.
    """

    def __init__(self, config: MeanReversionConfig) -> None:
        """Validate the config and reset all indicator state."""
        if config.oversold_threshold >= config.exit_rsi:
            raise ValueError(
                f"oversold threshold {config.oversold_threshold} must sit below "
                f"the exit RSI {config.exit_rsi}"
            )
        self._config = config
        self._rsi = Rsi(config.rsi_period)
        self._atr = Atr(config.atr_period)
        self._trend_ema = (
            Ema(config.trend_filter_ema_period) if config.trend_filter_ema_period > 0 else None
        )
        self._previous_rsi: float | None = None
        self._last_open_time: datetime | None = None

    @property
    def name(self) -> str:
        """Stable identifier for signal lineage."""
        return "mean_reversion"

    def on_candle(self, candle: Candle, position: Position | None) -> Signal | None:
        """Update indicators and propose entries on oversold recoveries.

        Candles must arrive in strictly increasing time order, same contract
        as every stateful strategy: disorder raises rather than silently
        poisoning the indicators.
        """
        if self._last_open_time is not None and candle.open_time <= self._last_open_time:
            raise ValueError(
                f"out-of-order or duplicate candle: {candle.open_time.isoformat()} after "
                f"{self._last_open_time.isoformat()}"
            )
        self._last_open_time = candle.open_time

        close = float(candle.close_quote)
        rsi = self._rsi.update(close)
        atr = self._atr.update(float(candle.high_quote), float(candle.low_quote), close)
        trend_ema = self._trend_ema.update(close) if self._trend_ema is not None else None
        if rsi is None or atr is None:
            return None
        previous_rsi = self._previous_rsi
        self._previous_rsi = rsi
        if previous_rsi is None:
            return None

        signal_id = f"{self.name}:{candle.symbol}:{candle.close_time.isoformat()}"
        recovered = (
            previous_rsi < self._config.oversold_threshold
            and rsi >= self._config.oversold_threshold
        )
        # With the filter on, an unformed trend EMA also blocks: buying a
        # dip with no trend information is the falling-knife case.
        downtrending = self._trend_ema is not None and (trend_ema is None or close < trend_ema)
        if recovered and position is None and not downtrending:
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
                    f"RSI({self._config.rsi_period}) recovered above "
                    f"{self._config.oversold_threshold:g} from oversold "
                    f"({previous_rsi:.1f} -> {rsi:.1f})",
                    f"stop at {self._config.atr_stop_multiple} x ATR below close",
                ),
                created_at=candle.close_time,
            )
        if rsi >= self._config.exit_rsi and position is not None:
            return Signal(
                signal_id=signal_id,
                strategy_name=self.name,
                symbol=candle.symbol,
                side=Side.SELL,
                confidence=1.0,
                # Informational for exits: the position is being closed, not stopped.
                stop_price_quote=candle.close_quote,
                reasons=(
                    f"RSI({self._config.rsi_period}) reached {rsi:.1f} >= "
                    f"{self._config.exit_rsi:g}: the reversion played out",
                ),
                created_at=candle.close_time,
            )
        return None
