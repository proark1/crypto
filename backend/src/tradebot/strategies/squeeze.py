"""Volatility squeeze: trade the expansion after a compression.

A fifth strategy family. The premise (the TTM-squeeze idea, long-only for
this spot bot): when the Bollinger Bands contract *inside* the Keltner
Channels, volatility has wound down into a coil — the market is resting,
not trending. The edge is not the coil itself but its release: the candle
where the bands push back outside the channel is volatility expanding
again, and entering with the breakout's direction rides the move it
starts. This is a far more selective cousin of the Donchian breakout,
which fires on every range expansion; here the range must first have been
unusually *tight*, the condition the plain breakout has no notion of.

Entry (long): on the candle the squeeze releases (bands were inside the
channel last candle, outside this one) while the close sits above the
basis — the expansion is upward, the only direction a spot long can take.
Exit: the close falls back below the basis (the move has rolled over),
with the shared ATR stop as the hard floor underneath. Long-only like
every family here, and the stop sits ``atr_stop_multiple`` ATRs below the
close — the shared convention that lets the risk manager size every
family identically.

Research-first wiring, exactly like breakout and momentum: the family is
registered for sweeps, evaluation, the §12.7 improvement rotation, and the
§13 competition, but it is **not** routed in production until the §13.7
evidence gate is met and a human routes it. Built from the TA-Lib-verified
incremental Bollinger, EMA, and ATR.
"""

from __future__ import annotations

from datetime import datetime
from decimal import ROUND_HALF_EVEN, Decimal

from pydantic import BaseModel, ConfigDict

from tradebot.core.models import ACCOUNTING_RESOLUTION, Candle, Side, Signal
from tradebot.indicators import Atr, Bollinger, Ema
from tradebot.portfolio import Position


class SqueezeConfig(BaseModel):
    """Band/channel lengths and stop convention; defaults are the classic TTM."""

    model_config = ConfigDict(frozen=True)

    bollinger_period: int = 20
    """Length of the Bollinger basis (an SMA) and its standard-deviation
    window; also the basis the entry/exit direction is measured against."""

    bollinger_stddev: float = 2.0
    """Bollinger band half-width, in standard deviations."""

    keltner_period: int = 20
    """Length of the Keltner channel's EMA basis and its ATR — kept as one
    knob, the TTM convention, so the squeeze compares like-for-like windows."""

    keltner_atr_multiple: float = 1.5
    """Keltner channel half-width, in ATRs. The squeeze is on while the
    Bollinger band sits inside the channel this multiple defines."""

    atr_period: int = 14
    atr_stop_multiple: float = 2.0
    """Stop convention, shared with every family: the protective stop sits
    this many ATRs below the entry close."""

    volume_ema_period: int = 20
    """Length of the volume EMA used as the participation baseline."""

    min_volume_ratio: float = 0.0
    """Volume confirmation (ARCHITECTURE.md §5.2.3): entries require the
    release candle's base-currency volume to reach this multiple of the
    *prior* candles' volume EMA — an expansion nobody traded is suspect.
    ``0`` disables; while the baseline is still forming, entries wait
    (fail-safe). Exits are never volume-filtered."""

    breakeven_at_r: float = 0.0
    """Stop management: ratchet the stop to entry once the trade has earned
    this many R. ``0`` disables."""

    trail_atr_multiple: float = 0.0
    """Stop management: trail the stop this many entry-time ATRs below the
    highest high since entry. ``0`` disables."""


class SqueezeStrategy:
    """Volatility-squeeze breakout for one symbol.

    Indicator math runs in floats (permitted: it never feeds an order size
    directly); the stop converts to ``Decimal`` at the signal boundary. The
    squeeze state of the *prior* candle is remembered so a release — the
    transition from compressed to expanding — can be detected without
    recomputing history.
    """

    def __init__(self, config: SqueezeConfig) -> None:
        """Validate the config and reset all indicator/state."""
        self._config = config
        self._bollinger = Bollinger(config.bollinger_period, config.bollinger_stddev)
        self._keltner_basis = Ema(config.keltner_period)
        self._keltner_atr = Atr(config.keltner_period)
        self._stop_atr = Atr(config.atr_period)
        self._volume_ema = Ema(config.volume_ema_period)
        self._was_in_squeeze: bool | None = None
        self._last_open_time: datetime | None = None

    @property
    def name(self) -> str:
        """Stable identifier for signal lineage."""
        return "squeeze"

    def on_candle(self, candle: Candle, position: Position | None) -> Signal | None:
        """Update bands/channels and propose entries on an upward release.

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
        high = float(candle.high_quote)
        low = float(candle.low_quote)
        bands = self._bollinger.update(close)
        keltner_basis = self._keltner_basis.update(close)
        keltner_atr = self._keltner_atr.update(high, low, close)
        stop_atr = self._stop_atr.update(high, low, close)
        # The participation baseline is prior candles only: the release
        # candle's volume must clear a bar it did not itself set.
        volume = float(candle.volume_base)
        average_volume = self._volume_ema.value
        self._volume_ema.update(volume)

        if bands is None or keltner_basis is None or keltner_atr is None or stop_atr is None:
            return None

        keltner_upper = keltner_basis + self._config.keltner_atr_multiple * keltner_atr
        keltner_lower = keltner_basis - self._config.keltner_atr_multiple * keltner_atr
        in_squeeze = bands.upper < keltner_upper and bands.lower > keltner_lower
        was_in_squeeze = self._was_in_squeeze
        self._was_in_squeeze = in_squeeze

        signal_id = f"{self.name}:{candle.symbol}:{candle.close_time.isoformat()}"
        if position is not None:
            if close < bands.middle:
                return Signal(
                    signal_id=signal_id,
                    strategy_name=self.name,
                    symbol=candle.symbol,
                    side=Side.SELL,
                    confidence=1.0,
                    stop_price_quote=candle.close_quote,  # informational: full exit
                    reasons=(
                        f"close {close:g} fell back below the Bollinger basis "
                        f"{bands.middle:g}: the expansion rolled over",
                    ),
                    created_at=candle.close_time,
                )
            return None

        # A release: compressed last candle, expanding now. The first decision
        # candle (no prior squeeze state) can never be a release.
        if was_in_squeeze is None or not was_in_squeeze or in_squeeze:
            return None
        if close <= bands.middle:
            return None  # expansion is not upward; a spot long has no edge here
        if self._config.min_volume_ratio > 0 and (
            average_volume is None or volume < self._config.min_volume_ratio * average_volume
        ):
            return None  # an expansion nobody traded is suspect
        stop = Decimal(str(close - self._config.atr_stop_multiple * stop_atr)).quantize(
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
                Decimal(str(self._config.trail_atr_multiple * stop_atr)).quantize(
                    ACCOUNTING_RESOLUTION, rounding=ROUND_HALF_EVEN
                )
                if self._config.trail_atr_multiple > 0
                else None
            ),
            reasons=(
                f"volatility squeeze released upward: close {close:g} above the "
                f"Bollinger basis {bands.middle:g} as the bands expanded past Keltner",
                f"stop at {self._config.atr_stop_multiple} x ATR below close",
            ),
            created_at=candle.close_time,
        )
