"""Deterministic in-process event bus.

Handlers are dispatched sequentially, in subscription order, and awaited to
completion before ``publish`` returns. This is a deliberate trade-off: the
backtester replays history through the same bus as live trading, so event
ordering must be reproducible run-to-run (the golden backtest depends on it).
Concurrency, if ever needed, belongs in individual handlers — not the bus.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import TypeVar, cast

from pydantic import BaseModel, ConfigDict

from tradebot.core.models import Candle

E = TypeVar("E", bound=BaseModel)

Handler = Callable[[E], Awaitable[None]]


class CandleClosed(BaseModel):
    """Published by the market data service when a candle's interval completes.

    Strategies consume closed candles only — never partial in-progress ones —
    so backtest and live behavior match.
    """

    model_config = ConfigDict(frozen=True)

    candle: Candle


class EventBus:
    """Typed publish/subscribe hub connecting the bot's components."""

    def __init__(self) -> None:
        """Create an empty bus with no subscriptions."""
        self._handlers: dict[type[BaseModel], list[Handler[BaseModel]]] = {}

    def subscribe(self, event_type: type[E], handler: Handler[E]) -> None:
        """Register ``handler`` for events of exactly ``event_type``.

        Dispatch matches the event's concrete type; subclass matching is
        intentionally unsupported to keep routing predictable.
        """
        self._handlers.setdefault(event_type, []).append(cast(Handler[BaseModel], handler))

    async def publish(self, event: BaseModel) -> None:
        """Deliver ``event`` to its subscribers in subscription order.

        Awaits each handler to completion before calling the next; exceptions
        propagate to the publisher so failures in order/position handling are
        never silently swallowed.
        """
        for handler in self._handlers.get(type(event), []):
            await handler(event)
