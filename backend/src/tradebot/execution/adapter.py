"""The execution adapter interface every venue implementation satisfies.

Orders arrive only from the risk manager (CLAUDE.md invariant 4); fills flow
back through a single registered async handler, which the engine points at
portfolio accounting and the event stream. Adapters are venue mechanics only —
no sizing, no strategy, no risk decisions.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Protocol

from tradebot.core.models import Fill, Order

FillHandler = Callable[[Fill], Awaitable[None]]
"""Async callback invoked once per fill, in fill order."""


class ExecutionAdapter(Protocol):
    """Venue-facing order management: submit, cancel, observe."""

    def set_fill_handler(self, handler: FillHandler) -> None:
        """Register the single consumer of this adapter's fills."""
        ...

    async def submit(self, order: Order) -> None:
        """Place ``order`` on the venue.

        ``order.client_order_id`` must be new to this adapter — resubmitting
        an id is rejected, which is what makes retries idempotent.
        """
        ...

    async def cancel(self, client_order_id: str) -> None:
        """Cancel the resting order with ``client_order_id``; unknown ids raise."""
        ...

    def open_orders(self) -> tuple[Order, ...]:
        """Return currently resting (unfilled, uncancelled) orders."""
        ...
