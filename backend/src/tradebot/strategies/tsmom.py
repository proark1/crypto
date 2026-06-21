"""Time-series momentum: long when the lookback return is positive.

A momentum family with a genuinely different mechanic from the others. The
MACD momentum family reads an *oscillator* of two EMAs; the trend follower
reads a *crossover* of two EMAs; this one reads the raw **sign of the holding
return** — is the close above where it was ``lookback`` candles ago? That is
the classic CTA / time-series-momentum (TSMOM) edge: an instrument that has
risen over the recent window tends to keep rising. Long-only, exits when the
return turns down through the exit threshold, with the shared ATR stop
convention so the risk manager sizes it identically.

Distinct from the price families above precisely because it has no moving
average in the entry rule — it is an *absolute* return signal, not a relative
one — so the research tournament gets a different momentum hypothesis to grade.

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
from tradebot.indicators import Atr
from tradebot.portfolio import Position


class TsmomConfig(BaseModel):
    """Lookback, entry/exit thresholds, and stop convention; defaults are 20/0/0."""

    model_config = ConfigDict(frozen=True)

    lookback: int = 20
    """Candles back the holding return is measured over — the defining knob. A
    *longer* lookback is a slower, steadier signal (fewer flips); a *shorter*
    one reacts sooner but whipsaws more."""

    atr_period: int = 14
    """Length of the ATR that sets the protective stop distance."""

    entry_threshold: float = 0.0
    """Minimum lookback return (a fraction, e.g. ``0.02`` = +2%) to enter. The
    default ``0`` enters on any positive return; a positive value demands a
    stronger move and skips marginal ones."""

    exit_threshold: float = 0.0
    """Lookback return (a fraction) at or below which an open position exits.
    The default ``0`` exits the moment momentum turns negative; a negative
    value (e.g. ``-0.02``) tolerates a shallow pullback before exiting."""

    atr_stop_multiple: float = 2.0
    """Protective stop distance below the entry close, in entry-time ATRs —
    the shared convention so the risk manager sizes every family alike."""

    breakeven_at_r: float = 0.0
    """Stop management: ratchet the stop to entry once the trade earns this
    many R. ``0`` disables."""

    trail_atr_multiple: float = 0.0
    """Stop management: trail the stop this many entry-time ATRs below the
    highest high since entry. ``0`` disables."""


class TsmomStrategy:
    """Lookback-return entries and exits for one symbol.

    Indicator math runs in floats (permitted: it never feeds an order size
    directly); the stop converts to ``Decimal`` at the signal boundary. State
    is O(1) per candle — a bounded ring of the last ``lookback + 1`` closes
    (so the close ``lookback`` candles ago is one indexed read) plus the ATR.
    """

    def __init__(self, config: TsmomConfig) -> None:
        """Validate the config and reset all return/indicator state."""
        if config.lookback < 1:
            raise ValueError(f"lookback must be at least 1, got {config.lookback}")
        if config.atr_stop_multiple <= 0:
            raise ValueError(f"atr_stop_multiple must be > 0, got {config.atr_stop_multiple}")
        self._config = config
        self._atr = Atr(config.atr_period)
        # Ring of the last lookback + 1 closes: index 0 is the oldest (the
        # close `lookback` candles before the newest), index -1 the newest.
        self._closes: deque[float] = deque(maxlen=config.lookback + 1)
        self._last_open_time: datetime | None = None

    @property
    def name(self) -> str:
        """Stable identifier for signal lineage."""
        return "tsmom"

    def on_candle(self, candle: Candle, position: Position | None) -> Signal | None:
        """Enter on a positive lookback return; exit when it turns down.

        Candles must arrive in strictly increasing time order, the shared
        stateful-strategy contract: disorder raises rather than silently
        poisoning the return window.
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
        # The return is measured against the close `lookback` candles ago, so
        # the window must be full before any signal — never a partial lookback.
        full = len(self._closes) == self._closes.maxlen
        past_close = self._closes[0] if full else None
        self._closes.append(close)
        if atr is None or past_close is None or past_close <= 0:
            return None  # warming up, or a degenerate reference price

        lookback_return = (close - past_close) / past_close
        signal_id = f"{self.name}:{candle.symbol}:{candle.close_time.isoformat()}"
        if position is not None:
            if lookback_return <= self._config.exit_threshold:
                return Signal(
                    signal_id=signal_id,
                    strategy_name=self.name,
                    symbol=candle.symbol,
                    side=Side.SELL,
                    confidence=1.0,
                    stop_price_quote=candle.close_quote,  # informational: full exit
                    reasons=(
                        f"{self._config.lookback}-candle return {lookback_return:+.2%} fell to "
                        f"the exit threshold {self._config.exit_threshold:+.2%}",
                    ),
                    created_at=candle.close_time,
                )
            return None

        if lookback_return <= self._config.entry_threshold:
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
                f"{self._config.lookback}-candle return {lookback_return:+.2%} cleared the "
                f"entry threshold {self._config.entry_threshold:+.2%}",
                f"stop at {self._config.atr_stop_multiple} x ATR below close",
            ),
            created_at=candle.close_time,
        )
