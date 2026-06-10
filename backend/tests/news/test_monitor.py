"""Monitor behavior: dedup, tracked-coin filtering, events, and fault tolerance."""

import json
from datetime import UTC, datetime

import httpx

from tradebot.core.events import EventBus
from tradebot.news import CryptoPanicSource, NewsFlagged, NewsFlags, NewsMonitor

NOW = datetime(2026, 6, 10, 12, 0, tzinfo=UTC)


def make_post(post_id: int, title: str, codes: list[str]) -> dict[str, object]:
    return {
        "id": post_id,
        "title": title,
        "url": f"https://example.com/{post_id}",
        "currencies": [{"code": code} for code in codes],
        "published_at": NOW.isoformat(),
    }


class ScriptedTransport(httpx.MockTransport):
    """Serves a mutable list of posts; can be told to fail."""

    def __init__(self) -> None:
        self.posts: list[dict[str, object]] = []
        self.failing = False
        self.requests: list[httpx.Request] = []
        super().__init__(self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.failing:
            return httpx.Response(500, text="upstream exploded")
        return httpx.Response(200, text=json.dumps({"results": self.posts}))


def make_monitor(
    transport: ScriptedTransport,
    coins: tuple[str, ...] = ("SOL/USDT", "BTC/USDT"),
    bus: EventBus | None = None,
) -> tuple[NewsMonitor, NewsFlags, httpx.AsyncClient]:
    client = httpx.AsyncClient(transport=transport)
    flags = NewsFlags()
    monitor = NewsMonitor(
        CryptoPanicSource("token", client),
        flags,
        tracked_coins=lambda: coins,
        bus=bus,
    )
    return monitor, flags, client


class TestNewsMonitor:
    async def test_negative_news_flags_only_tracked_coins(self) -> None:
        transport = ScriptedTransport()
        transport.posts = [
            make_post(1, "Exchange will delist SOL pairs", ["SOL", "DOGE"]),
            make_post(2, "BTC partners with megacorp", ["BTC"]),
        ]
        bus = EventBus()
        observed: list[NewsFlagged] = []

        async def on_flag(event: NewsFlagged) -> None:
            observed.append(event)

        bus.subscribe(NewsFlagged, on_flag)
        monitor, flags, client = make_monitor(transport, bus=bus)
        async with client:
            raised = await monitor.poll_once()

        assert [flag.coin for flag in raised] == ["SOL"]  # DOGE is not traded
        assert flags.active_flag("SOL", NOW) is not None
        assert flags.active_flag("BTC", NOW) is None  # partnership is not negative
        assert [event.flag.coin for event in observed] == ["SOL"]
        # The poll asked only for the coins the bot trades.
        assert "currencies=SOL%2CBTC" in str(transport.requests[0].url)

    async def test_items_are_processed_once_across_polls(self) -> None:
        transport = ScriptedTransport()
        transport.posts = [make_post(1, "Protocol exploited, funds drained", ["SOL"])]
        monitor, _, client = make_monitor(transport)
        async with client:
            first = await monitor.poll_once()
            second = await monitor.poll_once()

        assert len(first) == 1
        assert second == []  # same external id: never re-flagged, never re-alerted

    async def test_source_failure_raises_to_the_loop_not_past_it(self) -> None:
        """poll_once raises (honest), run() catches and keeps polling."""
        transport = ScriptedTransport()
        transport.failing = True
        monitor, _, client = make_monitor(transport)
        async with client:
            try:
                await monitor.poll_once()
                raised = False
            except httpx.HTTPStatusError:
                raised = True
            # The API recovers; the next poll works with the same monitor.
            transport.failing = False
            transport.posts = [make_post(3, "Exchange will delist SOL", ["SOL"])]
            recovered = await monitor.poll_once()

        assert raised is True
        assert [flag.coin for flag in recovered] == ["SOL"]

    async def test_no_tracked_coins_means_no_request(self) -> None:
        transport = ScriptedTransport()
        monitor, _, client = make_monitor(transport, coins=())
        async with client:
            assert await monitor.poll_once() == []
        assert transport.requests == []
