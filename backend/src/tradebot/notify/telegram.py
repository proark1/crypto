"""Telegram alerts over the plain Bot API.

One POST per message via httpx — no bot-framework dependency for the alert
path. The cardinal rule here: **notification failures never disturb
trading.** Telegram being down is logged and dropped; the bus handler must
not raise, because engine fill handling sits upstream of it.
"""

from __future__ import annotations

import logging

import httpx

from tradebot.core.events import EventBus, FillRecorded

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

    def attach_to(self, bus: EventBus) -> None:
        """Subscribe to the events worth a push notification."""
        bus.subscribe(FillRecorded, self._on_fill)

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
        fill = event.fill
        await self.send(
            f"{fill.side.value.upper()} {fill.quantity_base} {fill.symbol} "
            f"@ {fill.price_quote} (fee {fill.fee_quote})"
        )
