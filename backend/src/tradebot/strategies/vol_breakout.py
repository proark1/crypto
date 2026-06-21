"""Volatility-regime breakout: take Donchian breaks only when volatility expands.

A breakout family that asks a second question the plain Donchian family does
not: *is volatility expanding?* The plain breakout (``breakout``) buys every
close that clears the prior ``channel_period`` high; this one buys that break
**only when** the current ATR has risen to ``expansion_ratio`` times its own
recent baseline — a thrust that coincides with a genuine volatility expansion,
not a drift to a new high in a quiet tape. The premise is that breakouts that
matter arrive with participation, and rising true range is its proxy. It exits
back at an EMA basis (the trend's mean), so it is not a mirror of the Donchian
turtle exit. Long-only, shared ATR stop convention.

Distinct from ``breakout`` (no volatility gate, Donchian exit) and from
``keltner`` (a static ATR *envelope*, not a Donchian high with a *dynamic*
expansion gate), so the research tournament gets a regime-aware breakout
hypothesis to grade — and it answers the deferred Donchian-width question:
gate on volatility *expansion*, not raw channel width.

Research-first wiring on purpose: registered for sweeps, evaluation, the §12.7
improvement rotation, and the strategy competition, but **not** routed in
production yet (the §13.7 human decision the evidence informs).
"""

from __future__ import annotations

from collections import deque
from datetime import datetime
from decimal import ROUND_HALF_EVEN, Decimal

from pydantic import BaseModel, ConfigDict

from tradebot.core.models import ACCOUNTING_RESOLUTION, Candle, Side, Signal
from tradebot.indicators import Atr, Ema
from tradebot.portfolio import Position


class VolBreakoutConfig(BaseModel):
    """Channel, volatility-gate, exit, and stop geometry; defaults are 20/1.3."""

    model_config = ConfigDict(frozen=True)

    channel_period: int = 20
    """Entry channel: close must clear the highest high of this many prior
    candles."""

    atr_period: int = 14
    """Length of the ATR that measures current volatility (and sets the stop)."""

    atr_baseline_period: int = 50
    """Length of the EMA of ATR that forms the volatility baseline the current
    ATR is compared against."""

    expansion_ratio: float = 1.3
    """The defining knob: the current ATR must reach this multiple of its
    baseline EMA for a breakout to count — the volatility-expansion gate. A
    *higher* ratio demands a sharper expansion (fewer, more decisive entries);
    ``1.0`` admits any non-contracting tape."""

    exit_ema_period: int = 20
    """Length of the EMA basis the position exits back to (the trend's mean)."""

    atr_stop_multiple: float = 2.0
    """Protective stop distance below the entry close, in entry-time ATRs —
    the shared convention so the risk manager sizes every family alike."""

    breakeven_at_r: float = 0.0
    """Stop management: ratchet the stop to entry once the trade earns this
    many R. ``0`` disables."""

    trail_atr_multiple: float = 0.0
    """Stop management: trail the stop this many entry-time ATRs below the
    highest high since entry. ``0`` disables."""


class VolBreakoutStrategy:
    """Donchian breakout gated on a rising-volatility regime, for one symbol.

    Indicator math runs in floats (permitted: it never feeds an order size
    directly); the stop converts to ``Decimal`` at the signal boundary. State
    is O(1) per candle — a bounded deque of prior highs for the channel, the
    ATR, an EMA of the ATR for the baseline, and the EMA basis for the exit.
    """

    def __init__(self, config: VolBreakoutConfig) -> None:
        """Validate the config and reset all channel/indicator state."""
        if config.channel_period < 2:
            raise ValueError(f"channel period must be at least 2, got {config.channel_period}")
        if config.expansion_ratio <= 0:
            raise ValueError(f"expansion_ratio must be > 0, got {config.expansion_ratio}")
        if config.atr_stop_multiple <= 0:
            raise ValueError(f"atr_stop_multiple must be > 0, got {config.atr_stop_multiple}")
        self._config = config
        self._atr = Atr(config.atr_period)
        self._atr_baseline = Ema(config.atr_baseline_period)
        self._exit_ema = Ema(config.exit_ema_period)
        self._highs: deque[float] = deque(maxlen=config.channel_period)
        self._last_open_time: datetime | None = None

    @property
    def name(self) -> str:
        """Stable identifier for signal lineage."""
        return "vol_breakout"

    def on_candle(self, candle: Candle, position: Position | None) -> Signal | None:
        """Buy a Donchian break under expanding volatility; exit at the basis.

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
        # The entry channel is built from *prior* candles only: the breakout
        # candle must clear a high that existed before it, never its own.
        entry_ceiling = max(self._highs) if len(self._highs) == self._highs.maxlen else None
        self._highs.append(high)
        # The volatility baseline reads the ATR before this candle folds in, so
        # the expansion is measured against history, not against itself.
        baseline = self._atr_baseline.value
        if atr is not None:
            self._atr_baseline.update(atr)
        basis = self._exit_ema.update(close)
        if atr is None or basis is None:
            return None  # indicators still warming

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
                    reasons=(f"close {close:g} fell back to the EMA basis {basis:g}",),
                    created_at=candle.close_time,
                )
            return None

        if entry_ceiling is None or close <= entry_ceiling:
            return None
        if baseline is None or atr < self._config.expansion_ratio * baseline:
            return None  # volatility is not expanding — a quiet drift, not a thrust
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
                f"ceiling {entry_ceiling:g} with ATR {atr:g} ≥ {self._config.expansion_ratio:g}x "
                f"baseline {baseline:g}",
                f"stop at {self._config.atr_stop_multiple} x ATR below close",
            ),
            created_at=candle.close_time,
        )
