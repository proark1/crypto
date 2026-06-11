"""Composite strategy: several families voting on one symbol.

The custom-bot builder lets a user mix rules: pick several strategy
families and combine their entries either as **any** (the first family
to propose a buy wins — a wider net) or **all** (every family must
propose a buy on the same candle — a confluence filter, deliberately
strict). Exits are never combined: with a position open, the first
family that wants out gets out — a mixed bot must not trap a position
behind a vote, the same principle as the regime router's exit rule.

Every member sees every candle so all indicators stay warm, exactly
like the router keeps its inactive family warm.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

from tradebot.core.models import Candle, Side, Signal
from tradebot.portfolio import Position
from tradebot.strategies.base import Strategy


class CompositeStrategy:
    """Combines member strategies' signals under one entry mode.

    A forwarded signal keeps its member family's ``strategy_name`` —
    lineage stays honest about which rule actually fired (in *all* mode,
    the first member's signal is forwarded with the confluence noted in
    its reasons).
    """

    def __init__(self, members: Sequence[Strategy], require_all_entries: bool = False) -> None:
        """Combine ``members`` (at least one); validate up front."""
        if not members:
            raise ValueError("a composite strategy needs at least one member")
        self._members = tuple(members)
        self._require_all = require_all_entries
        self._last_open_time: datetime | None = None

    @property
    def name(self) -> str:
        """Stable identifier listing the member families."""
        mode = "all" if self._require_all else "any"
        return f"composite[{mode}:{'+'.join(member.name for member in self._members)}]"

    def on_candle(self, candle: Candle, position: Position | None) -> Signal | None:
        """Feed every member, then combine: exits first, entries by mode."""
        if self._last_open_time is not None and candle.open_time <= self._last_open_time:
            raise ValueError(
                f"out-of-order or duplicate candle: {candle.open_time.isoformat()} after "
                f"{self._last_open_time.isoformat()}"
            )
        self._last_open_time = candle.open_time

        signals = [member.on_candle(candle, position) for member in self._members]
        if position is not None:
            for signal in signals:
                if signal is not None and signal.side == Side.SELL:
                    return signal
            return None
        buys = [signal for signal in signals if signal is not None and signal.side == Side.BUY]
        if not buys:
            return None
        if not self._require_all:
            return buys[0]
        if len(buys) < len(self._members):
            return None
        first = buys[0]
        others = ", ".join(signal.strategy_name for signal in buys[1:])
        reasons = first.reasons
        if others:
            reasons = (*reasons, f"confluence: {others} agreed on this candle")
        return first.model_copy(update={"reasons": reasons})
