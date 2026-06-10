import json
from datetime import UTC, datetime
from decimal import Decimal

import httpx
import pytest

from tradebot.core.events import EventBus, FillRecorded
from tradebot.core.models import Fill, Side
from tradebot.notify import TelegramNotifier

FILL = Fill(
    client_order_id="ord-1",
    symbol="BTC/USDT",
    side=Side.BUY,
    price_quote=Decimal("67000.5"),
    quantity_base=Decimal("0.05"),
    fee_quote=Decimal("3.35"),
    filled_at=datetime(2026, 1, 2, 3, 4, tzinfo=UTC),
)


class RecordingTransport(httpx.MockTransport):
    def __init__(self, status_code: int = 200, raise_error: bool = False) -> None:
        self.requests: list[httpx.Request] = []
        self.raise_error = raise_error

        def handler(request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            if self.raise_error:
                raise httpx.ConnectError("telegram unreachable")
            return httpx.Response(status_code, json={"ok": status_code == 200})

        super().__init__(handler)


def make_notifier(
    transport: RecordingTransport,
) -> tuple[TelegramNotifier, httpx.AsyncClient]:
    client = httpx.AsyncClient(transport=transport)
    return TelegramNotifier("test-token", "12345", client), client


class TestSend:
    async def test_posts_to_bot_api_with_chat_id(self) -> None:
        transport = RecordingTransport()
        notifier, client = make_notifier(transport)
        async with client:
            assert await notifier.send("hello") is True

        (request,) = transport.requests
        assert "bottest-token/sendMessage" in str(request.url)
        body = json.loads(request.content)
        assert body == {"chat_id": "12345", "text": "hello"}

    async def test_network_failure_is_swallowed_and_logged(self) -> None:
        transport = RecordingTransport(raise_error=True)
        notifier, client = make_notifier(transport)
        async with client:
            assert await notifier.send("hello") is False  # no exception escapes

    async def test_api_rejection_is_swallowed(self) -> None:
        transport = RecordingTransport(status_code=429)
        notifier, client = make_notifier(transport)
        async with client:
            assert await notifier.send("hello") is False

    async def test_missing_credentials_are_rejected(self) -> None:
        async with httpx.AsyncClient(transport=RecordingTransport()) as client:
            with pytest.raises(ValueError, match="bot token and chat id"):
                TelegramNotifier("", "12345", client)


class TestBusIntegration:
    async def test_fill_event_produces_readable_message(self) -> None:
        transport = RecordingTransport()
        notifier, client = make_notifier(transport)
        bus = EventBus()
        notifier.attach_to(bus)
        async with client:
            await bus.publish(FillRecorded(fill=FILL))
            assert transport.requests == []  # send is backgrounded, loop not blocked
            await notifier.flush()

        body = json.loads(transport.requests[0].content)
        assert body["text"] == "BUY 0.05 BTC/USDT @ 67000.5 (fee 3.35)"

    async def test_news_flag_event_produces_an_actionable_alert(self) -> None:
        from tradebot.news import NewsEventType, NewsFlag, NewsFlagged

        flag = NewsFlag(
            coin="SOL",
            event_type=NewsEventType.DELISTING,
            headline="Exchange will delist SOL pairs",
            flagged_at=datetime(2026, 1, 2, 3, 4, tzinfo=UTC),
            expires_at=datetime(2026, 1, 3, 3, 4, tzinfo=UTC),
        )
        transport = RecordingTransport()
        notifier, client = make_notifier(transport)
        bus = EventBus()
        notifier.attach_to(bus)
        async with client:
            await bus.publish(NewsFlagged(flag=flag))
            await notifier.flush()

        text = json.loads(transport.requests[0].content)["text"]
        assert "DELISTING on SOL" in text
        assert "will not sell on news by itself" in text  # the human decides exits

    async def test_telegram_outage_does_not_break_the_bus(self) -> None:
        """The notifier sits downstream of fill handling; it must never raise."""
        transport = RecordingTransport(raise_error=True)
        notifier, client = make_notifier(transport)
        bus = EventBus()
        notifier.attach_to(bus)
        async with client:
            await bus.publish(FillRecorded(fill=FILL))  # must not raise
            await notifier.flush()  # nor may the background task leak an error
