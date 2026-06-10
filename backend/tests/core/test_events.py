from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from pydantic import BaseModel, ConfigDict

from tradebot.core.events import CandleClosed, EventBus
from tradebot.core.models import Candle, CandleInterval


class OtherEvent(BaseModel):
    model_config = ConfigDict(frozen=True)

    payload: str


def make_candle_closed() -> CandleClosed:
    open_time = datetime(2026, 1, 2, 3, 4, tzinfo=UTC)
    return CandleClosed(
        candle=Candle(
            symbol="BTC/USDT",
            interval=CandleInterval.M1,
            open_time=open_time,
            close_time=open_time + timedelta(minutes=1),
            open_quote=Decimal("100"),
            high_quote=Decimal("110"),
            low_quote=Decimal("90"),
            close_quote=Decimal("105"),
            volume_base=Decimal("2.5"),
        )
    )


async def test_handlers_run_in_subscription_order() -> None:
    bus = EventBus()
    calls: list[str] = []

    async def first(event: CandleClosed) -> None:
        calls.append("first")

    async def second(event: CandleClosed) -> None:
        calls.append("second")

    bus.subscribe(CandleClosed, first)
    bus.subscribe(CandleClosed, second)
    await bus.publish(make_candle_closed())

    assert calls == ["first", "second"]


async def test_events_are_routed_by_exact_type() -> None:
    bus = EventBus()
    received: list[BaseModel] = []

    async def on_other(event: OtherEvent) -> None:
        received.append(event)

    bus.subscribe(OtherEvent, on_other)
    await bus.publish(make_candle_closed())

    assert received == []


async def test_publish_without_subscribers_is_a_noop() -> None:
    bus = EventBus()
    await bus.publish(make_candle_closed())


async def test_unsubscribed_handler_sees_no_further_events() -> None:
    bus = EventBus()
    received: list[CandleClosed] = []

    async def handler(event: CandleClosed) -> None:
        received.append(event)

    bus.subscribe(CandleClosed, handler)
    await bus.publish(make_candle_closed())
    bus.unsubscribe(CandleClosed, handler)
    await bus.publish(make_candle_closed())

    assert len(received) == 1


async def test_unsubscribing_an_unknown_handler_is_loud() -> None:
    bus = EventBus()

    async def never_subscribed(event: CandleClosed) -> None:  # pragma: no cover
        pass

    with pytest.raises(ValueError):
        bus.unsubscribe(CandleClosed, never_subscribed)


async def test_handler_exceptions_propagate_to_publisher() -> None:
    bus = EventBus()

    async def failing(event: CandleClosed) -> None:
        raise RuntimeError("handler failure must not be swallowed")

    bus.subscribe(CandleClosed, failing)
    with pytest.raises(RuntimeError, match="must not be swallowed"):
        await bus.publish(make_candle_closed())
