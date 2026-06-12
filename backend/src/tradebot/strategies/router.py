"""Regime-driven routing between strategy families (ARCHITECTURE.md 5.2).

The regime decides *which family is preferred* — trend following in
trending markets, mean reversion in ranging ones. Both strategies consume
every candle so their indicators stay warm across regime changes; the
preferred family's entry is forwarded first, and the other family's entry
is forwarded when the preferred one is silent. Exits pass from **either**
family whenever a position is open: a regime flip must never orphan a
position with its exit logic switched off.

Preference here is the *only* family routing: the regime gate no longer
vetoes the non-preferred family in a healthy regime (that veto starved the
single-coin production account — see ARCHITECTURE.md 5.2). The gate still
blocks every family on risk-off, warm-up, and stale data, so this router
stays mode- and venue-ignorant and never has to know the market is hostile.

The router is itself a ``Strategy``, so engines, backtests, and the
evaluator can use it without changes — and it takes the regime as a plain
callable, keeping this module as mode- and venue-ignorant as every other
strategy.
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
        forwarded when the preferred one is silent, so a healthy market is
        never left untraded just because the preferred family was quiet (the
        gate allows either family in a healthy regime, and any genuine block
        there is journaled where a signal suppressed here would vanish
        without a trace).
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
