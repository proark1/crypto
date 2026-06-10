"""The interface every strategy implements."""

from __future__ import annotations

from typing import Protocol

from tradebot.core.models import Candle, Signal
from tradebot.portfolio import Position


class Strategy(Protocol):
    """A pure signal generator over closed candles.

    ``on_candle`` is called once per closed candle in time order. The only
    state a strategy may consult beyond its own indicators is the current
    ``position`` — it must not know balances, mode, or anything venue-shaped.
    """

    @property
    def name(self) -> str:
        """Stable identifier recorded in every signal this strategy emits."""
        ...

    def on_candle(self, candle: Candle, position: Position | None) -> Signal | None:
        """Consume one closed candle; return a trade proposal or ``None``."""
        ...
