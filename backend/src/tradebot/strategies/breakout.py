"""Breakout: Donchian-channel entries with ATR-derived stops.

The third strategy family (the review's item 9): buy when the close clears
the highest high of the prior ``channel_period`` candles — range expansion
the trend follower's crossover only confirms many candles later — and exit
when the close falls below the lowest low of the prior
``exit_channel_period`` candles (the classic turtle-style channel exit).
Long-only, like every family in this spot bot, and the stop sits
``atr_stop_multiple`` ATRs below the close, the shared convention that
lets the risk manager size every family identically.

Research-first wiring on purpose: the family is registered for sweeps and
evaluation so the research pipeline can pit it against the incumbents on
identical scenarios, but it is **not** routed in production yet — which
regime should activate it (and at whose expense) is a human architecture
decision the sweep evidence should inform, not preempt.
"""

from __future__ import annotations

from collections import deque
from datetime import datetime
from decimal import ROUND_HALF_EVEN, Decimal

from pydantic import BaseModel, ConfigDict

from tradebot.core.models import ACCOUNTING_RESOLUTION, Candle, Side, Signal
from tradebot.indicators import Atr, Ema
from tradebot.portfolio import Position


class BreakoutConfig(BaseModel):
    """Channel lengths and stop convention; defaults are the classic 20/10."""

    model_config = ConfigDict(frozen=True)

    channel_period: int = 20
    """Entry channel: close must clear the highest high of this many prior
    candles."""

    exit_channel_period: int = 10
    """Exit channel: close below the lowest low of this many prior candles
    closes the position."""

    atr_period: int = 14
    atr_stop_multiple: float = 2.0

    min_channel_width_atr: float = 0.0
    """Skip breakouts of channels narrower than this many ATRs — a flat
    channel breaks on noise, not participation. ``0`` disables."""

    volume_ema_period: int = 20
    """Length of the volume EMA used as the participation baseline."""

    min_volume_ratio: float = 0.0
    """Volume confirmation (ARCHITECTURE.md §5.2.3): entries require the
    candle's base-currency volume to reach this multiple of the *prior*
    candles' volume EMA — a breakout nobody traded is the classic fakeout.
    ``0`` disables; while the baseline is still forming, entries wait
    (fail-safe). Exits are never volume-filtered."""

    breakeven_at_r: float = 0.0
    """Stop management: ratchet the stop to entry once the trade has earned
    this many R. ``0`` disables."""

    trail_atr_multiple: float = 0.0
    """Stop management: trail the stop this many entry-time ATRs below the
    highest high since entry. ``0`` disables."""


class BreakoutStrategy:
    """Donchian-channel breakout for one symbol.

    Indicator math runs in floats (permitted: it never feeds an order size
    directly); the stop converts to ``Decimal`` at the signal boundary.
    Channel extremes scan a bounded deque (≤ ``channel_period`` floats per
    candle) — bounded work, deliberately simple over a monotonic queue.
    """

    def __init__(self, config: BreakoutConfig) -> None:
        """Validate the config and reset all channel/indicator state."""
        if config.channel_period < 2:
            raise ValueError(f"channel period must be at least 2, got {config.channel_period}")
        if config.exit_channel_period < 1:
            raise ValueError(
                f"exit channel period must be at least 1, got {config.exit_channel_period}"
            )
        self._config = config
        self._atr = Atr(config.atr_period)
        self._volume_ema = Ema(config.volume_ema_period)
        self._highs: deque[float] = deque(maxlen=config.channel_period)
        self._lows: deque[float] = deque(maxlen=config.exit_channel_period)
        self._last_open_time: datetime | None = None

    @property
    def name(self) -> str:
        """Stable identifier for signal lineage."""
        return "breakout"

    def on_candle(self, candle: Candle, position: Position | None) -> Signal | None:
        """Update channels and propose entries on range expansion.

        Candles must arrive in strictly increasing time order, the shared
        stateful-strategy contract: disorder raises rather than silently
        poisoning the channels.
        """
        if self._last_open_time is not None and candle.open_time <= self._last_open_time:
            raise ValueError(
                f"out-of-order or duplicate candle: {candle.open_time.isoformat()} after "
                f"{self._last_open_time.isoformat()}"
            )
        self._last_open_time = candle.open_time

        close = float(candle.close_quote)
        atr = self._atr.update(float(candle.high_quote), float(candle.low_quote), close)
        # Channels are built from *prior* candles: the breakout candle must
        # clear a level that existed before it, never one it set itself.
        entry_ceiling = max(self._highs) if len(self._highs) == self._highs.maxlen else None
        entry_floor = min(self._highs) if len(self._highs) == self._highs.maxlen else None
        exit_floor = min(self._lows) if len(self._lows) == self._lows.maxlen else None
        self._highs.append(float(candle.high_quote))
        self._lows.append(float(candle.low_quote))
        # The participation baseline, like the channels, is prior candles
        # only: the breakout's own volume must clear a bar it did not set.
        volume = float(candle.volume_base)
        average_volume = self._volume_ema.value
        self._volume_ema.update(volume)
        if atr is None:
            return None

        signal_id = f"{self.name}:{candle.symbol}:{candle.close_time.isoformat()}"
        if position is not None:
            if exit_floor is not None and close < exit_floor:
                return Signal(
                    signal_id=signal_id,
                    strategy_name=self.name,
                    symbol=candle.symbol,
                    side=Side.SELL,
                    confidence=1.0,
                    stop_price_quote=candle.close_quote,  # informational: full exit
                    reasons=(
                        f"close {close:g} fell below the "
                        f"{self._config.exit_channel_period}-candle channel floor "
                        f"{exit_floor:g}",
                    ),
                    created_at=candle.close_time,
                )
            return None

        if entry_ceiling is None or close <= entry_ceiling:
            return None
        if (
            self._config.min_channel_width_atr > 0
            and entry_floor is not None
            and entry_ceiling - entry_floor < self._config.min_channel_width_atr * atr
        ):
            return None  # a flat channel breaks on noise, not participation
        if self._config.min_volume_ratio > 0 and (
            average_volume is None or volume < self._config.min_volume_ratio * average_volume
        ):
            return None  # a breakout nobody traded is a fakeout in waiting
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
                f"close {close:g} broke above the {self._config.channel_period}-candle "
                f"channel ceiling {entry_ceiling:g}",
                f"stop at {self._config.atr_stop_multiple} x ATR below close",
            ),
            created_at=candle.close_time,
        )
