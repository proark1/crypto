from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from tradebot.core.models import Candle, CandleInterval, Fill, Order, OrderType, Side
from tradebot.execution import FillSimulatorConfig, SimulatedExecutionAdapter

BASE_TIME = datetime(2026, 1, 2, 0, 0, tzinfo=UTC)

ZERO_COST_CONFIG = FillSimulatorConfig(
    maker_fee_bps=Decimal(0), taker_fee_bps=Decimal(0), market_slippage_bps=Decimal(0)
)


def make_candle(
    *,
    open_quote: str = "100",
    high_quote: str = "110",
    low_quote: str = "90",
    close_quote: str = "105",
    symbol: str = "BTC/USDT",
    minutes_after_base: int = 0,
) -> Candle:
    open_time = BASE_TIME + timedelta(minutes=minutes_after_base)
    return Candle(
        symbol=symbol,
        interval=CandleInterval.M1,
        open_time=open_time,
        close_time=open_time + timedelta(minutes=1),
        open_quote=Decimal(open_quote),
        high_quote=Decimal(high_quote),
        low_quote=Decimal(low_quote),
        close_quote=Decimal(close_quote),
        volume_base=Decimal("10"),
    )


def make_order(
    side: Side = Side.BUY,
    order_type: OrderType = OrderType.MARKET,
    quantity: str = "1",
    limit_price: str | None = None,
    stop_price: str | None = None,
    client_order_id: str = "order-1",
) -> Order:
    return Order(
        client_order_id=client_order_id,
        signal_id="signal-1",
        symbol="BTC/USDT",
        side=side,
        order_type=order_type,
        quantity_base=Decimal(quantity),
        limit_price_quote=Decimal(limit_price) if limit_price is not None else None,
        stop_price_quote=Decimal(stop_price) if stop_price is not None else None,
    )


class FillCollector:
    def __init__(self) -> None:
        self.fills: list[Fill] = []

    async def __call__(self, fill: Fill) -> None:
        self.fills.append(fill)


def make_adapter(
    config: FillSimulatorConfig = ZERO_COST_CONFIG,
) -> tuple[SimulatedExecutionAdapter, FillCollector]:
    adapter = SimulatedExecutionAdapter(config)
    collector = FillCollector()
    adapter.set_fill_handler(collector)
    return adapter, collector


class TestMarketOrders:
    async def test_fills_at_next_candle_open(self) -> None:
        adapter, collector = make_adapter()
        await adapter.submit(make_order(Side.BUY, OrderType.MARKET))
        await adapter.process_candle(make_candle(open_quote="123"))

        (fill,) = collector.fills
        assert fill.price_quote == Decimal("123")
        assert fill.filled_at == BASE_TIME
        assert adapter.open_orders() == ()

    async def test_buy_slippage_moves_price_against_the_trade(self) -> None:
        config = FillSimulatorConfig(
            maker_fee_bps=Decimal(0), taker_fee_bps=Decimal(0), market_slippage_bps=Decimal(50)
        )
        adapter, collector = make_adapter(config)
        await adapter.submit(make_order(Side.BUY, OrderType.MARKET))
        await adapter.process_candle(make_candle(open_quote="100"))

        assert collector.fills[0].price_quote == Decimal("100.5")

    async def test_sell_slippage_moves_price_down(self) -> None:
        config = FillSimulatorConfig(
            maker_fee_bps=Decimal(0), taker_fee_bps=Decimal(0), market_slippage_bps=Decimal(50)
        )
        adapter, collector = make_adapter(config)
        await adapter.submit(make_order(Side.SELL, OrderType.MARKET))
        await adapter.process_candle(make_candle(open_quote="100"))

        assert collector.fills[0].price_quote == Decimal("99.5")

    async def test_taker_fee_is_charged_on_notional(self) -> None:
        config = FillSimulatorConfig(
            maker_fee_bps=Decimal(10), taker_fee_bps=Decimal(10), market_slippage_bps=Decimal(0)
        )
        adapter, collector = make_adapter(config)
        await adapter.submit(make_order(Side.BUY, OrderType.MARKET, quantity="2"))
        await adapter.process_candle(make_candle(open_quote="100"))

        assert collector.fills[0].fee_quote == Decimal("0.2")  # 200 notional * 0.1%


class TestLimitOrders:
    async def test_buy_rests_until_low_touches_limit(self) -> None:
        adapter, collector = make_adapter()
        await adapter.submit(make_order(Side.BUY, OrderType.LIMIT, limit_price="95"))

        await adapter.process_candle(make_candle(low_quote="96"))
        assert collector.fills == []
        assert len(adapter.open_orders()) == 1

        await adapter.process_candle(make_candle(low_quote="94", minutes_after_base=1))
        (fill,) = collector.fills
        assert fill.price_quote == Decimal("95")  # at limit, never improved
        assert adapter.open_orders() == ()

    async def test_sell_fills_when_high_reaches_limit(self) -> None:
        adapter, collector = make_adapter()
        await adapter.submit(make_order(Side.SELL, OrderType.LIMIT, limit_price="108"))
        await adapter.process_candle(make_candle(high_quote="110"))

        (fill,) = collector.fills
        assert fill.price_quote == Decimal("108")

    async def test_maker_fee_is_charged(self) -> None:
        config = FillSimulatorConfig(
            maker_fee_bps=Decimal(8), taker_fee_bps=Decimal(10), market_slippage_bps=Decimal(0)
        )
        adapter, collector = make_adapter(config)
        await adapter.submit(make_order(Side.BUY, OrderType.LIMIT, limit_price="100"))
        await adapter.process_candle(make_candle(low_quote="99"))

        assert collector.fills[0].fee_quote == Decimal("0.08")

    async def test_limit_order_requires_limit_price(self) -> None:
        adapter, _ = make_adapter()
        with pytest.raises(ValueError, match="requires limit_price_quote"):
            await adapter.submit(make_order(Side.BUY, OrderType.LIMIT))


class TestStopLimitOrders:
    async def test_protective_sell_stop_triggers_and_fills_at_limit(self) -> None:
        adapter, collector = make_adapter()
        await adapter.submit(
            make_order(Side.SELL, OrderType.STOP_LIMIT, stop_price="95", limit_price="94")
        )
        await adapter.process_candle(make_candle(open_quote="100", low_quote="93"))

        (fill,) = collector.fills
        assert fill.price_quote == Decimal("94")

    async def test_sell_stop_does_not_trigger_above_stop(self) -> None:
        adapter, collector = make_adapter()
        await adapter.submit(
            make_order(Side.SELL, OrderType.STOP_LIMIT, stop_price="95", limit_price="94")
        )
        await adapter.process_candle(make_candle(low_quote="96"))

        assert collector.fills == []
        assert len(adapter.open_orders()) == 1

    async def test_gap_through_limit_leaves_stop_unfilled(self) -> None:
        """A candle opening below the limit means the stop was gapped through."""
        adapter, collector = make_adapter()
        await adapter.submit(
            make_order(Side.SELL, OrderType.STOP_LIMIT, stop_price="95", limit_price="94")
        )
        await adapter.process_candle(
            make_candle(open_quote="90", high_quote="92", low_quote="88", close_quote="89")
        )

        assert collector.fills == []  # gap risk is preserved, not papered over
        assert len(adapter.open_orders()) == 1

    async def test_gapped_stop_fills_on_recovery_without_recrossing_stop(self) -> None:
        """Triggering is permanent: after a gap, the order is a live limit order.

        The recovery candle here never crosses the stop again (low stays above
        95); a stateless simulator would wrongly leave the order unfilled.
        """
        adapter, collector = make_adapter()
        await adapter.submit(
            make_order(Side.SELL, OrderType.STOP_LIMIT, stop_price="95", limit_price="94")
        )
        await adapter.process_candle(
            make_candle(open_quote="90", high_quote="92", low_quote="88", close_quote="89")
        )
        assert collector.fills == []  # triggered but gapped through

        await adapter.process_candle(
            make_candle(
                open_quote="96",
                high_quote="97",
                low_quote="95.5",
                close_quote="96.5",
                minutes_after_base=1,
            )
        )
        (fill,) = collector.fills
        assert fill.price_quote == Decimal("94")

    async def test_cancel_clears_triggered_state(self) -> None:
        adapter, collector = make_adapter()
        await adapter.submit(
            make_order(Side.SELL, OrderType.STOP_LIMIT, stop_price="95", limit_price="94")
        )
        await adapter.process_candle(
            make_candle(open_quote="90", high_quote="92", low_quote="88", close_quote="89")
        )
        await adapter.cancel("order-1")

        assert adapter.open_orders() == ()
        await adapter.process_candle(make_candle(minutes_after_base=1))
        assert collector.fills == []

    async def test_buy_stop_triggers_when_high_crosses_stop(self) -> None:
        adapter, collector = make_adapter()
        await adapter.submit(
            make_order(Side.BUY, OrderType.STOP_LIMIT, stop_price="105", limit_price="106")
        )
        await adapter.process_candle(make_candle(high_quote="107"))

        (fill,) = collector.fills
        assert fill.price_quote == Decimal("106")

    async def test_stop_limit_requires_stop_price(self) -> None:
        adapter, _ = make_adapter()
        with pytest.raises(ValueError, match="requires stop_price_quote"):
            await adapter.submit(make_order(Side.SELL, OrderType.STOP_LIMIT, limit_price="94"))


class TestOrderManagement:
    async def test_duplicate_client_order_id_is_rejected(self) -> None:
        adapter, _ = make_adapter()
        await adapter.submit(make_order(Side.BUY, OrderType.LIMIT, limit_price="95"))
        with pytest.raises(ValueError, match="duplicate client_order_id"):
            await adapter.submit(make_order(Side.SELL, OrderType.MARKET))

    async def test_cancel_removes_resting_order(self) -> None:
        adapter, collector = make_adapter()
        await adapter.submit(make_order(Side.BUY, OrderType.LIMIT, limit_price="95"))
        await adapter.cancel("order-1")

        assert adapter.open_orders() == ()
        await adapter.process_candle(make_candle(low_quote="90"))
        assert collector.fills == []

    async def test_cancel_unknown_order_raises(self) -> None:
        adapter, _ = make_adapter()
        with pytest.raises(ValueError, match="unknown order"):
            await adapter.cancel("never-submitted")

    async def test_orders_for_other_symbols_are_untouched(self) -> None:
        adapter, collector = make_adapter()
        await adapter.submit(make_order(Side.BUY, OrderType.MARKET))
        await adapter.process_candle(make_candle(symbol="ETH/USDT"))

        assert collector.fills == []
        assert len(adapter.open_orders()) == 1

    async def test_fills_without_handler_raise_instead_of_vanishing(self) -> None:
        adapter = SimulatedExecutionAdapter(ZERO_COST_CONFIG)
        await adapter.submit(make_order(Side.BUY, OrderType.MARKET))
        with pytest.raises(RuntimeError, match="no fill handler"):
            await adapter.process_candle(make_candle())

    async def test_resting_fill_is_stamped_with_candle_close_time(self) -> None:
        adapter, collector = make_adapter()
        await adapter.submit(make_order(Side.BUY, OrderType.LIMIT, limit_price="95"))
        candle = make_candle(low_quote="94")
        await adapter.process_candle(candle)

        assert collector.fills[0].filled_at == candle.close_time
