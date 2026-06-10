"""Regime-driven routing between strategy families (ARCHITECTURE.md 5.2).

The regime decides *which family is active* — trend following in trending
markets, mean reversion in ranging ones. Both strategies consume every
candle so their indicators stay warm across regime changes; only the
active family's entries pass through. Exits pass from **either** family
whenever a position is open: a regime flip must never orphan a position
with its exit logic switched off.

The router is itself a ``Strategy``, so engines, backtests, and the
evaluator can use it without changes — and it takes the regime as a plain
callable, keeping this module as mode- and venue-ignorant as every other
strategy (the gate, not the router, remains the §5.2 enforcer; routing
here just avoids generating signals the gate would have to discard).
"""

from __future__ import annotations

from collections.abc import Callable

from tradebot.core.models import Candle, Side, Signal
from tradebot.portfolio import Position
from tradebot.strategies.base import Strategy

RANGING = "ranging"
"""The one regime label the router needs (mirrors ``signals.regime``;
duplicated as a plain string so strategies stay import-independent of the
signals layer). Ranging prefers the reversion family; every other label
prefers trend."""


class RegimeStrategyRouter:
    """Routes entries by regime; passes exits from whichever family asks."""

    def __init__(
        self,
        trend: Strategy,
        reversion: Strategy,
        regime_label: Callable[[], str],
    ) -> None:
        """``regime_label`` is read per candle (regimes change at runtime)."""
        self._trend = trend
        self._reversion = reversion
        self._regime_label = regime_label

    @property
    def name(self) -> str:
        """Stable identifier; the routed signal keeps its own family's name."""
        return "regime_router"

    def on_candle(self, candle: Candle, position: Position | None) -> Signal | None:
        """Feed both families, return at most one signal per candle.

        Exit preference is deliberate: with a position open, the first SELL
        (trend family checked first) wins, whatever the regime says —
        getting out is never regime-gated. When flat, the regime picks
        which family's BUY is *preferred*; the other family's BUY is still
        forwarded when the preferred one is silent, because the regime
        gate — not the router — is the §5.2 enforcer, and a veto there is
        journaled where a signal suppressed here would vanish without a
        trace.
        """
        trend_signal = self._trend.on_candle(candle, position)
        reversion_signal = self._reversion.on_candle(candle, position)
        if position is not None:
            for signal in (trend_signal, reversion_signal):
                if signal is not None and signal.side == Side.SELL:
                    return signal
            return None
        if self._regime_label() == RANGING:
            candidates = (reversion_signal, trend_signal)
        else:
            candidates = (trend_signal, reversion_signal)
        for signal in candidates:
            if _is_buy(signal):
                return signal
        return None


def _is_buy(signal: Signal | None) -> bool:
    return signal is not None and signal.side == Side.BUY
