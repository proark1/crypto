from datetime import UTC, datetime, timedelta
from decimal import Decimal

import httpx
import pytest

from tradebot.core.events import CandleClosed, EventBus
from tradebot.core.models import Candle, CandleInterval
from tradebot.notify import HeartbeatPinger

BASE_TIME = datetime(2026, 1, 2, 0, 0, tzinfo=UTC)
PING_URL = "https://hc-ping.example.com/uuid"


def make_candle() -> Candle:
    return Candle(
        symbol="BTC/USDT",
        interval=CandleInterval.M1,
        open_time=BASE_TIME,
        close_time=BASE_TIME + timedelta(minutes=1),
        open_quote=Decimal("100"),
        high_quote=Decimal("101"),
        low_quote=Decimal("99"),
        close_quote=Decimal("100"),
        volume_base=Decimal("1"),
    )


class RecordingTransport(httpx.MockTransport):
    def __init__(self, status_code: int = 200, raise_error: bool = False) -> None:
        self.requests: list[httpx.Request] = []
        self.raise_error = raise_error

        def handler(request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            if self.raise_error:
                raise httpx.ConnectError("monitor unreachable")
            return httpx.Response(status_code)

        super().__init__(handler)


def make_pinger(transport: RecordingTransport) -> tuple[HeartbeatPinger, httpx.AsyncClient]:
    client = httpx.AsyncClient(transport=transport)
    return HeartbeatPinger(PING_URL, client), client


class TestHealthGate:
    async def test_no_ping_before_the_first_candle(self) -> None:
        """A feed that never connects must never produce a heartbeat."""
        transport = RecordingTransport()
        pinger, client = make_pinger(transport)
        async with client:
            assert await pinger.ping_once() is False
        assert transport.requests == []

    async def test_pings_while_candles_are_fresh(self) -> None:
        transport = RecordingTransport()
        pinger, client = make_pinger(transport)
        bus = EventBus()
        pinger.attach_to(bus)
        async with client:
            await bus.publish(CandleClosed(candle=make_candle()))
            assert await pinger.ping_once() is True

        (request,) = transport.requests
        assert str(request.url) == PING_URL

    async def test_staleness_is_judged_by_arrival_wall_clock(self) -> None:
        transport = RecordingTransport()
        pinger, client = make_pinger(transport)
        bus = EventBus()
        pinger.attach_to(bus)
        async with client:
            await bus.publish(CandleClosed(candle=make_candle()))

        arrival = datetime.now(UTC)
        assert pinger.is_healthy(arrival + timedelta(seconds=179)) is True
        assert pinger.is_healthy(arrival + timedelta(seconds=300)) is False


class TestFailureIsolation:
    async def test_monitor_outage_is_swallowed_and_logged(self) -> None:
        """The dead-man's switch must never take the trading loop with it."""
        transport = RecordingTransport(raise_error=True)
        pinger, client = make_pinger(transport)
        bus = EventBus()
        pinger.attach_to(bus)
        async with client:
            await bus.publish(CandleClosed(candle=make_candle()))
            assert await pinger.ping_once() is False  # no exception escapes

    async def test_http_error_status_counts_as_failed_ping(self) -> None:
        transport = RecordingTransport(status_code=500)
        pinger, client = make_pinger(transport)
        bus = EventBus()
        pinger.attach_to(bus)
        async with client:
            await bus.publish(CandleClosed(candle=make_candle()))
            assert await pinger.ping_once() is False


class TestConstruction:
    async def test_empty_url_is_rejected(self) -> None:
        async with httpx.AsyncClient(transport=RecordingTransport()) as client:
            with pytest.raises(ValueError, match="requires a URL"):
                HeartbeatPinger("", client)

    async def test_non_positive_timings_are_rejected(self) -> None:
        async with httpx.AsyncClient(transport=RecordingTransport()) as client:
            with pytest.raises(ValueError, match="must be positive"):
                HeartbeatPinger(PING_URL, client, interval=timedelta(0))
            with pytest.raises(ValueError, match="must be positive"):
                HeartbeatPinger(PING_URL, client, max_staleness=timedelta(seconds=-1))
