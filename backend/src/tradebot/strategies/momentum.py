"""Momentum: MACD crossover entries with ATR-derived stops.

The fourth strategy family, added for the strategy competition: buy when
the MACD histogram (MACD line minus its signal line) crosses positive —
momentum turning up — and exit when it crosses back negative. The classic
zero-line filter is on by default: entries only fire while the MACD line
itself is positive (the fast EMA above the slow one), so the crossover is
a continuation signal inside an advance, not a counter-trend twitch.

Long-only, like every family in this spot bot, and the stop sits
``atr_stop_multiple`` ATRs below the close — the shared convention that
lets the risk manager size every family identically. Built entirely from
the TA-Lib-verified incremental EMA, following its seeding conventions:
the signal line is an EMA *of MACD values*, exactly TA-Lib's MACD shape.
"""

from __future__ import annotations

from datetime import datetime
from decimal import ROUND_HALF_EVEN, Decimal

from pydantic import BaseModel, ConfigDict

from tradebot.core.models import ACCOUNTING_RESOLUTION, Candle, Side, Signal
from tradebot.indicators import Atr, Ema
from tradebot.portfolio import Position


class MomentumConfig(BaseModel):
    """MACD periods and stop convention; defaults are the classic 12/26/9."""

    model_config = ConfigDict(frozen=True)

    fast_ema_period: int = 12
    slow_ema_period: int = 26
    signal_ema_period: int = 9
    """The signal line: an EMA of the MACD line itself."""

    require_positive_macd: bool = True
    """Zero-line filter: only enter while the MACD line is above zero, so
    the crossover confirms an advance instead of catching a bounce inside
    a decline. Off makes every bullish crossover eligible."""

    atr_period: int = 14
    atr_stop_multiple: float = 2.0

    volume_ema_period: int = 20
    """Length of the volume EMA used as the participation baseline."""

    min_volume_ratio: float = 0.0
    """Volume confirmation (ARCHITECTURE.md §5.2.3): entries require the
    candle's base-currency volume to reach this multiple of the *prior*
    candles' volume EMA — a momentum turn without participation behind it
    is a twitch, not a move. ``0`` disables; while the baseline is still
    forming, entries wait (fail-safe). Exits are never volume-filtered."""

    breakeven_at_r: float = 0.0
    """Stop management: ratchet the stop to entry once the trade has earned
    this many R. ``0`` disables."""

    trail_atr_multiple: float = 0.0
    """Stop management: trail the stop this many entry-time ATRs below the
    highest high since entry. ``0`` disables."""


class MomentumStrategy:
    """MACD histogram-crossover momentum for one symbol.

    Indicator math runs in floats (permitted: it never feeds an order size
    directly); the stop converts to ``Decimal`` at the signal boundary.
    """

    def __init__(self, config: MomentumConfig) -> None:
        """Validate the config and reset all indicator state."""
        if config.fast_ema_period >= config.slow_ema_period:
            raise ValueError(
                f"fast EMA period {config.fast_ema_period} must sit below "
                f"the slow EMA period {config.slow_ema_period}"
            )
        self._config = config
        self._fast = Ema(config.fast_ema_period)
        self._slow = Ema(config.slow_ema_period)
        self._signal_line = Ema(config.signal_ema_period)
        self._atr = Atr(config.atr_period)
        self._volume_ema = Ema(config.volume_ema_period)
        self._previous_histogram: float | None = None
        self._last_open_time: datetime | None = None

    @property
    def name(self) -> str:
        """Stable identifier for signal lineage."""
        return "momentum"

    def on_candle(self, candle: Candle, position: Position | None) -> Signal | None:
        """Update MACD state and propose entries on bullish crossovers.

        Candles must arrive in strictly increasing time order, the shared
        stateful-strategy contract: disorder raises rather than silently
        poisoning the indicators.
        """
        if self._last_open_time is not None and candle.open_time <= self._last_open_time:
            raise ValueError(
                f"out-of-order or duplicate candle: {candle.open_time.isoformat()} after "
                f"{self._last_open_time.isoformat()}"
            )
        self._last_open_time = candle.open_time

        close = float(candle.close_quote)
        # The participation baseline is prior candles only: the entry
        # candle's volume must clear a bar it did not set itself.
        volume = float(candle.volume_base)
        average_volume = self._volume_ema.value
        self._volume_ema.update(volume)
        fast = self._fast.update(close)
        slow = self._slow.update(close)
        atr = self._atr.update(float(candle.high_quote), float(candle.low_quote), close)
        if fast is None or slow is None:
            return None
        macd = fast - slow
        signal_value = self._signal_line.update(macd)
        if signal_value is None or atr is None:
            return None
        histogram = macd - signal_value
        previous_histogram = self._previous_histogram
        self._previous_histogram = histogram
        if previous_histogram is None:
            return None

        signal_id = f"{self.name}:{candle.symbol}:{candle.close_time.isoformat()}"
        if position is not None:
            if previous_histogram >= 0 and histogram < 0:
                return Signal(
                    signal_id=signal_id,
                    strategy_name=self.name,
                    symbol=candle.symbol,
                    side=Side.SELL,
                    confidence=1.0,
                    stop_price_quote=candle.close_quote,  # informational: full exit
                    reasons=(
                        f"MACD histogram crossed negative "
                        f"({previous_histogram:.4g} -> {histogram:.4g}): momentum turned down",
                    ),
                    created_at=candle.close_time,
                )
            return None

        crossed_up = previous_histogram <= 0 and histogram > 0
        if not crossed_up:
            return None
        if self._config.require_positive_macd and macd <= 0:
            return None  # a bounce inside a decline, not an advance
        if self._config.min_volume_ratio > 0 and (
            average_volume is None or volume < self._config.min_volume_ratio * average_volume
        ):
            return None  # momentum without participation is a twitch
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
                f"MACD({self._config.fast_ema_period},{self._config.slow_ema_period},"
                f"{self._config.signal_ema_period}) histogram crossed positive "
                f"({previous_histogram:.4g} -> {histogram:.4g})",
                f"stop at {self._config.atr_stop_multiple} x ATR below close",
            ),
            created_at=candle.close_time,
        )
