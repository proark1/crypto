"""Supertrend: ATR-band trend following with a volatility-adaptive flip.

A distinct trend family from the EMA-crossover follower: the Supertrend line
is a band ``atr_multiple`` ATRs from the candle midpoint that *locks* in the
direction of the trend and flips only when the close crosses the opposite
band. Entries fire on the up-flip (a down→up trend change), exits on the
down-flip — long-only, like every family in this spot bot. Because the band
is built from ATR it widens in volatile markets and tightens in quiet ones,
so it rides trends with fewer whipsaws than a fixed-width rule, which is
exactly the orthogonal behaviour the research tournament can pit against the
incumbents.

The protective stop sits ``atr_stop_multiple`` ATRs below the close — the
shared convention every family uses, so the risk manager sizes Supertrend
identically to the rest (the Supertrend line itself is a natural trailing
level, but a configurable ATR stop keeps sizing uniform and the trail policy
explicit via the managed-stop knobs).

Research-first wiring on purpose: registered for sweeps, evaluation, the
§12.7 improvement rotation, and the strategy competition so the pipeline can
grade it against the incumbents on identical scenarios — but **not** routed
in production yet (which regime should activate it is the §13.7 human
decision the evidence should inform, not preempt).
"""

from __future__ import annotations

from datetime import datetime
from decimal import ROUND_HALF_EVEN, Decimal

from pydantic import BaseModel, ConfigDict

from tradebot.core.models import ACCOUNTING_RESOLUTION, Candle, Side, Signal
from tradebot.indicators import Atr, Ema
from tradebot.portfolio import Position


class SupertrendConfig(BaseModel):
    """Band width and stop convention; defaults are the classic 10/3."""

    model_config = ConfigDict(frozen=True)

    atr_period: int = 10
    """ATR length the Supertrend band is built on."""

    atr_multiple: float = 3.0
    """Band distance from the candle midpoint, in ATRs — the Supertrend
    factor. A *wider* band locks the trend in harder (fewer, later flips); a
    *tighter* band flips sooner (more, earlier signals)."""

    atr_stop_multiple: float = 2.0
    """Protective stop distance below the entry close, in entry-time ATRs —
    the shared convention so the risk manager sizes every family alike."""

    volume_ema_period: int = 20
    """Length of the volume EMA used as the participation baseline."""

    min_volume_ratio: float = 0.0
    """Volume confirmation (ARCHITECTURE.md §5.2.3): an up-flip entry requires
    the candle's base-currency volume to reach this multiple of the *prior*
    candles' volume EMA — a trend turn nobody traded is a fakeout in waiting.
    ``0`` disables; while the baseline is still forming, entries wait
    (fail-safe). Exits are never volume-filtered."""

    breakeven_at_r: float = 0.0
    """Stop management: ratchet the stop to entry once the trade has earned
    this many R. ``0`` disables."""

    trail_atr_multiple: float = 0.0
    """Stop management: trail the stop this many entry-time ATRs below the
    highest high since entry. ``0`` disables."""


class SupertrendStrategy:
    """Supertrend trend-flip strategy for one symbol.

    Indicator math runs in floats (permitted: it never feeds an order size
    directly); the stop converts to ``Decimal`` at the signal boundary. The
    band state is O(1) per candle — the last final bands, the last close, and
    the latched trend direction.
    """

    def __init__(self, config: SupertrendConfig) -> None:
        """Validate the config and reset all band/indicator state."""
        if config.atr_multiple <= 0:
            raise ValueError(f"atr_multiple must be > 0, got {config.atr_multiple}")
        if config.atr_stop_multiple <= 0:
            raise ValueError(f"atr_stop_multiple must be > 0, got {config.atr_stop_multiple}")
        self._config = config
        self._atr = Atr(config.atr_period)
        self._volume_ema = Ema(config.volume_ema_period)
        self._final_upper: float | None = None
        self._final_lower: float | None = None
        self._prev_close: float | None = None
        self._uptrend: bool | None = None
        self._last_open_time: datetime | None = None

    @property
    def name(self) -> str:
        """Stable identifier for signal lineage."""
        return "supertrend"

    def on_candle(self, candle: Candle, position: Position | None) -> Signal | None:
        """Update the band and propose entries on the trend flip.

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
        # The participation baseline is prior candles only: the flip's own
        # volume must clear a bar it did not set.
        volume = float(candle.volume_base)
        average_volume = self._volume_ema.value
        self._volume_ema.update(volume)
        if atr is None:
            return None  # ATR still warming; no band yet

        midpoint = (high + low) / 2.0
        upper_basic = midpoint + self._config.atr_multiple * atr
        lower_basic = midpoint - self._config.atr_multiple * atr

        if self._uptrend is None:
            # Seed the bands and an initial direction; emit nothing — there is
            # no prior trend to flip from on the very first banded candle.
            self._final_upper = upper_basic
            self._final_lower = lower_basic
            self._prev_close = close
            self._uptrend = close >= midpoint
            return None

        # The classic band lock: a final band only moves in the direction that
        # tightens around price, and resets once the close pierces it. Uses the
        # *prior* final bands and the *prior* close (both never None here).
        assert self._final_upper is not None and self._final_lower is not None
        assert self._prev_close is not None
        final_upper = (
            upper_basic
            if upper_basic < self._final_upper or self._prev_close > self._final_upper
            else self._final_upper
        )
        final_lower = (
            lower_basic
            if lower_basic > self._final_lower or self._prev_close < self._final_lower
            else self._final_lower
        )

        was_uptrend = self._uptrend
        # In an uptrend the line is the lower band; the trend flips down only
        # when the close breaks below it. In a downtrend the line is the upper
        # band; it flips up only when the close breaks above it.
        new_uptrend = close >= final_lower if was_uptrend else close > final_upper

        self._final_upper = final_upper
        self._final_lower = final_lower
        self._prev_close = close
        self._uptrend = new_uptrend

        signal_id = f"{self.name}:{candle.symbol}:{candle.close_time.isoformat()}"
        if position is not None:
            if was_uptrend and not new_uptrend:
                return Signal(
                    signal_id=signal_id,
                    strategy_name=self.name,
                    symbol=candle.symbol,
                    side=Side.SELL,
                    confidence=1.0,
                    stop_price_quote=candle.close_quote,  # informational: full exit
                    reasons=(
                        f"close {close:g} flipped the supertrend down "
                        f"(below the {self._config.atr_multiple:g}x ATR band)",
                    ),
                    created_at=candle.close_time,
                )
            return None

        if was_uptrend or not new_uptrend:
            return None  # no up-flip while flat
        if self._config.min_volume_ratio > 0 and (
            average_volume is None or volume < self._config.min_volume_ratio * average_volume
        ):
            return None  # a trend turn nobody traded is a fakeout in waiting
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
                f"close {close:g} flipped the supertrend up "
                f"(above the {self._config.atr_multiple:g}x ATR band)",
                f"stop at {self._config.atr_stop_multiple} x ATR below close",
            ),
            created_at=candle.close_time,
        )
