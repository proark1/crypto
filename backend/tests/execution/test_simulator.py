from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from tradebot.core.models import Candle, CandleInterval, Fill, Order, OrderType, Side
from tradebot.execution import FeeSchedule, FillSimulatorConfig, SimulatedExecutionAdapter

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

    async def test_spread_adds_to_market_slippage(self) -> None:
        config = FillSimulatorConfig(
            maker_fee_bps=Decimal(0),
            taker_fee_bps=Decimal(0),
            market_slippage_bps=Decimal(10),
            spread_bps=Decimal(20),
        )
        adapter, collector = make_adapter(config)
        await adapter.submit(make_order(Side.BUY, OrderType.MARKET))
        await adapter.process_candle(make_candle(open_quote="100"))

        assert collector.fills[0].price_quote == Decimal("100.3")

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


class TestRestore:
    """Restart recovery: persisted open orders re-armed into a fresh adapter."""

    async def test_restored_market_order_fills_like_a_submitted_one(self) -> None:
        adapter, collector = make_adapter()
        adapter.restore_order(make_order())
        await adapter.process_candle(make_candle())

        (fill,) = collector.fills
        assert fill.price_quote == Decimal("100")  # next open, zero-cost config

    async def test_restored_trigger_latch_fills_without_recrossing_stop(self) -> None:
        """A triggered stop must come back as a live limit, not a re-armed stop.

        The candle after the restore never crosses the 95 stop; restoring
        untriggered would wrongly leave the order resting.
        """
        adapter, collector = make_adapter()
        adapter.restore_order(
            make_order(Side.SELL, OrderType.STOP_LIMIT, stop_price="95", limit_price="94"),
            triggered=True,
        )
        await adapter.process_candle(
            make_candle(open_quote="96", high_quote="97", low_quote="95.5", close_quote="96.5")
        )

        (fill,) = collector.fills
        assert fill.price_quote == Decimal("94")

    async def test_restore_validates_shape_and_id_uniqueness(self) -> None:
        adapter, _ = make_adapter()
        await adapter.submit(make_order())
        with pytest.raises(ValueError, match="duplicate client_order_id"):
            adapter.restore_order(make_order())
        with pytest.raises(ValueError, match="trigger latch"):
            adapter.restore_order(make_order(client_order_id="order-2"), triggered=True)

    async def test_trigger_latch_is_observable_for_journaling(self) -> None:
        adapter, _ = make_adapter()
        await adapter.submit(
            make_order(Side.SELL, OrderType.STOP_LIMIT, stop_price="95", limit_price="94")
        )
        assert adapter.triggered_order_ids() == frozenset()
        # Crosses the stop but gaps through the limit: triggered, unfilled.
        await adapter.process_candle(
            make_candle(open_quote="90", high_quote="92", low_quote="88", close_quote="89")
        )
        assert adapter.triggered_order_ids() == frozenset({"order-1"})


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


class TestExecutionFidelity:
    """Opt-in realism: partial fills, volume impact, latency. Defaults off."""

    async def test_volume_cap_splits_a_market_order_across_candles(self) -> None:
        adapter, collector = make_adapter(
            FillSimulatorConfig(
                maker_fee_bps=Decimal(0),
                taker_fee_bps=Decimal(0),
                market_slippage_bps=Decimal(0),
                max_volume_fraction=Decimal("0.4"),
            )
        )
        await adapter.submit(make_order(quantity="6"))  # candle volume is 10
        await adapter.process_candle(make_candle())
        await adapter.process_candle(make_candle(minutes_after_base=1, open_quote="101"))

        assert [f.quantity_base for f in collector.fills] == [Decimal("4"), Decimal("2")]
        assert [f.price_quote for f in collector.fills] == [Decimal("100"), Decimal("101")]
        assert adapter.open_orders() == ()  # done after the remainder

    async def test_zero_volume_candle_fills_nothing(self) -> None:
        adapter, collector = make_adapter(FillSimulatorConfig(max_volume_fraction=Decimal("0.5")))
        await adapter.submit(make_order())
        dead = make_candle().model_copy(update={"volume_base": Decimal(0)})
        await adapter.process_candle(dead)

        assert collector.fills == []  # an outage candle trades nothing
        assert len(adapter.open_orders()) == 1  # the order waits, not vanishes

    async def test_volume_impact_scales_slippage_with_consumed_share(self) -> None:
        adapter, collector = make_adapter(
            FillSimulatorConfig(
                maker_fee_bps=Decimal(0),
                taker_fee_bps=Decimal(0),
                market_slippage_bps=Decimal(0),
                volume_impact_bps=Decimal(100),  # 1% per whole candle consumed
            )
        )
        await adapter.submit(make_order(quantity="5"))  # half the 10 volume
        await adapter.process_candle(make_candle())

        (fill,) = collector.fills
        assert fill.price_quote == Decimal("100.5")  # 100 x (1 + 0.01 x 0.5)

    async def test_extreme_volume_impact_cannot_drive_a_sell_fill_non_positive(self) -> None:
        """A pathological volume_impact_bps must not crash the fill handler.

        Without the slippage cap, a SELL priced at open x (1 - slip) went to
        zero or negative once modeled slippage reached 100%, which a Fill
        (price_quote > 0) rejects. The cap keeps the fill strictly positive.
        """
        adapter, collector = make_adapter(
            FillSimulatorConfig(
                maker_fee_bps=Decimal(0),
                taker_fee_bps=Decimal(0),
                market_slippage_bps=Decimal(0),
                volume_impact_bps=Decimal(2_000_000),  # 200x per whole candle: absurd
            )
        )
        await adapter.submit(make_order(Side.SELL, OrderType.MARKET, quantity="10"))  # whole candle
        await adapter.process_candle(make_candle())  # priced the sell <= 0 before the cap

        (fill,) = collector.fills
        assert fill.price_quote > 0  # strictly positive: no crash
        assert fill.price_quote == Decimal("5")  # 100 x (1 - 0.95), the clamped floor

    async def test_latency_delays_activation_by_n_candles(self) -> None:
        adapter, collector = make_adapter(FillSimulatorConfig(submit_latency_candles=2))
        await adapter.submit(make_order())
        await adapter.process_candle(make_candle())
        await adapter.process_candle(make_candle(minutes_after_base=1))
        assert collector.fills == []  # two candles of latency pass first

        await adapter.process_candle(make_candle(minutes_after_base=2, open_quote="103"))
        (fill,) = collector.fills
        assert fill.filled_at == BASE_TIME + timedelta(minutes=2)


class TestFeeSchedule:
    """The live per-side fee schedule and how the simulator uses it."""

    def test_fee_bps_for_picks_the_side(self) -> None:
        schedule = FeeSchedule(buy_fee_bps=Decimal(20), sell_fee_bps=Decimal(30))
        assert schedule.fee_bps_for(Side.BUY) == Decimal(20)
        assert schedule.fee_bps_for(Side.SELL) == Decimal(30)

    def test_standard_is_ten_bps_a_side(self) -> None:
        standard = FeeSchedule.standard()
        assert standard.buy_fee_bps == Decimal(10)
        assert standard.sell_fee_bps == Decimal(10)

    def test_update_rejects_negative_and_absurd_fees(self) -> None:
        schedule = FeeSchedule.standard()
        with pytest.raises(ValueError, match="cannot be negative"):
            schedule.update(buy_fee_bps=Decimal(-1), sell_fee_bps=Decimal(10))
        with pytest.raises(ValueError, match="sanity cap"):
            schedule.update(buy_fee_bps=Decimal(10), sell_fee_bps=Decimal(2000))
        # The rejected update left the previous fees intact.
        assert schedule.buy_fee_bps == Decimal(10)

    def test_update_rejects_non_finite_fees(self) -> None:
        # NaN slips past < and > checks (every NaN comparison is False), so it
        # must be rejected explicitly before it can poison fee math.
        schedule = FeeSchedule.standard()
        with pytest.raises(ValueError, match="finite"):
            schedule.update(buy_fee_bps=Decimal("NaN"), sell_fee_bps=Decimal(10))
        with pytest.raises(ValueError, match="finite"):
            schedule.update(buy_fee_bps=Decimal(10), sell_fee_bps=Decimal("Infinity"))
        assert schedule.buy_fee_bps == Decimal(10)

    async def test_schedule_fee_overrides_config_per_side(self) -> None:
        # Config says taker 10 bps, but a live schedule of 20/30 wins, by side.
        config = FillSimulatorConfig(taker_fee_bps=Decimal(10), market_slippage_bps=Decimal(0))
        schedule = FeeSchedule(buy_fee_bps=Decimal(20), sell_fee_bps=Decimal(30))
        adapter = SimulatedExecutionAdapter(config, fees=schedule)
        collector = FillCollector()
        adapter.set_fill_handler(collector)

        await adapter.submit(make_order(Side.BUY, OrderType.MARKET, client_order_id="b"))
        await adapter.process_candle(make_candle(open_quote="100"))
        await adapter.submit(make_order(Side.SELL, OrderType.MARKET, client_order_id="s"))
        await adapter.process_candle(make_candle(open_quote="100", minutes_after_base=1))

        buy_fill, sell_fill = collector.fills
        assert buy_fill.fee_quote == Decimal("0.2")  # 100 notional * 20 bps
        assert sell_fill.fee_quote == Decimal("0.3")  # 100 notional * 30 bps

    async def test_updating_the_schedule_changes_the_next_fill(self) -> None:
        config = FillSimulatorConfig(market_slippage_bps=Decimal(0))
        schedule = FeeSchedule(buy_fee_bps=Decimal(10), sell_fee_bps=Decimal(10))
        adapter = SimulatedExecutionAdapter(config, fees=schedule)
        collector = FillCollector()
        adapter.set_fill_handler(collector)

        await adapter.submit(make_order(Side.BUY, OrderType.MARKET, client_order_id="first"))
        await adapter.process_candle(make_candle(open_quote="100"))
        schedule.update(buy_fee_bps=Decimal(50), sell_fee_bps=Decimal(50))
        await adapter.submit(make_order(Side.BUY, OrderType.MARKET, client_order_id="second"))
        await adapter.process_candle(make_candle(open_quote="100", minutes_after_base=1))

        first, second = collector.fills
        assert first.fee_quote == Decimal("0.1")  # 100 notional * 10 bps
        assert second.fee_quote == Decimal("0.5")  # live change, no rebuild: 50 bps
