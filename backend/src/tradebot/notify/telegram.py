"""Telegram alerts over the plain Bot API.

One POST per message via httpx — no bot-framework dependency for the alert
path. The cardinal rule here: **notification failures never disturb
trading.** Telegram being down is logged and dropped; the bus handler must
not raise, because engine fill handling sits upstream of it.
"""

from __future__ import annotations

import asyncio
import logging

import httpx

from tradebot.core.events import EventBus, FillRecorded, ProposalCreated
from tradebot.news import NewsFlagged

logger = logging.getLogger(__name__)


class TelegramNotifier:
    """Sends alerts to one chat; attaches to the bus for fill events."""

    def __init__(self, bot_token: str, chat_id: str, client: httpx.AsyncClient) -> None:
        """Wire the notifier; ``client`` is owned by the caller (worker)."""
        if not bot_token or not chat_id:
            raise ValueError("telegram notifier requires both bot token and chat id")
        self._url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
        self._chat_id = chat_id
        self._client = client
        self._pending: set[asyncio.Task[bool]] = set()

    def attach_to(self, bus: EventBus) -> None:
        """Subscribe to the events worth a push notification."""
        bus.subscribe(FillRecorded, self._on_fill)
        bus.subscribe(ProposalCreated, self._on_proposal)
        bus.subscribe(NewsFlagged, self._on_news_flag)

    async def send(self, text: str) -> bool:
        """Send ``text``; returns False (and logs) on any failure.

        Never raises: a dead Telegram API must not take the trading loop
        down with it.
        """
        try:
            response = await self._client.post(
                self._url, json={"chat_id": self._chat_id, "text": text}
            )
        except Exception:
            logger.warning("telegram send failed", exc_info=True)
            return False
        if response.status_code != 200:
            logger.warning(
                "telegram send rejected: %d %s", response.status_code, response.text[:200]
            )
            return False
        return True

    async def _on_fill(self, event: FillRecorded) -> None:
        # Bus handlers run sequentially in the trading path, so the network
        # call is fired as a background task: Telegram latency must never
        # delay candle processing. References are held until completion so
        # tasks cannot be garbage-collected mid-send.
        fill = event.fill
        task = asyncio.create_task(
            self.send(
                f"{fill.side.value.upper()} {fill.quantity_base} {fill.symbol} "
                f"@ {fill.price_quote} (fee {fill.fee_quote})"
            )
        )
        self._pending.add(task)
        task.add_done_callback(self._pending.discard)

    async def _on_proposal(self, event: ProposalCreated) -> None:
        signal = event.proposal.signal
        reasons = "\n".join(f"- {reason}" for reason in signal.reasons)
        task = asyncio.create_task(
            self.send(
                f"PROPOSAL: {signal.side.value.upper()} {signal.symbol} "
                f"@ ~{event.proposal.proposal_price_quote} "
                f"(stop {signal.stop_price_quote})\n{reasons}\n"
                f"expires {event.proposal.expires_at.strftime('%H:%M:%S')} UTC — "
                "approve or reject in the dashboard"
            )
        )
        self._pending.add(task)
        task.add_done_callback(self._pending.discard)

    async def _on_news_flag(self, event: NewsFlagged) -> None:
        flag = event.flag
        task = asyncio.create_task(
            self.send(
                f"NEWS FLAG: {flag.event_type.value.upper()} on {flag.coin}\n"
                f"{flag.headline}\n"
                f"New entries are blocked until {flag.expires_at.strftime('%H:%M:%S')} UTC. "
                "If you hold this coin, consider exiting — the bot will not sell on news "
                "by itself."
            )
        )
        self._pending.add(task)
        task.add_done_callback(self._pending.discard)

    async def flush(self) -> None:
        """Wait for in-flight notifications (shutdown and tests)."""
        if self._pending:
            await asyncio.gather(*tuple(self._pending), return_exceptions=True)
