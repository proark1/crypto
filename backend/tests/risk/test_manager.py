from datetime import UTC, datetime, timedelta
from decimal import Decimal

from tradebot.core.models import Candle, CandleInterval, Fill, OrderType, Side, Signal
from tradebot.portfolio import Portfolio
from tradebot.risk import BreakerConfig, RiskConfig, RiskManager

SIGNAL_TIME = datetime(2026, 1, 2, 3, 4, tzinfo=UTC)


def make_signal(side: Side, stop: str) -> Signal:
    return Signal(
        strategy_name="trend_following",
        symbol="BTC/USDT",
        side=side,
        confidence=1.0,
        stop_price_quote=Decimal(stop),
        created_at=SIGNAL_TIME,
    )


def make_manager(
    balance: str = "10000",
    risk_fraction: str = "0.01",
    max_position_fraction: str = "0.25",
) -> tuple[RiskManager, Portfolio]:
    portfolio = Portfolio(Decimal(balance))
    config = RiskConfig(
        risk_per_trade_fraction=Decimal(risk_fraction),
        max_position_fraction=Decimal(max_position_fraction),
        fee_buffer_fraction=Decimal("0.005"),
    )
    return RiskManager(config, portfolio), portfolio


def open_position(portfolio: Portfolio, price: str, quantity: str) -> None:
    portfolio.apply_fill(
        Fill(
            client_order_id="seed",
            symbol="BTC/USDT",
            side=Side.BUY,
            price_quote=Decimal(price),
            quantity_base=Decimal(quantity),
            fee_quote=Decimal(0),
            filled_at=SIGNAL_TIME,
        )
    )


class TestEntrySizing:
    def test_quantity_risks_configured_fraction_between_entry_and_stop(self) -> None:
        manager, _ = make_manager(balance="10000", risk_fraction="0.01")
        order = manager.evaluate(make_signal(Side.BUY, stop="95"), Decimal("100"))

        assert order is not None
        # equity 10000 -> risk budget 100; stop distance 5 -> 20 base units
        assert order.quantity_base == Decimal("20")
        assert order.side == Side.BUY
        assert order.order_type == OrderType.MARKET
        assert order.signal_id  # lineage present

    def test_quantity_is_capped_by_max_position_fraction(self) -> None:
        # Tight stop would size huge; the exposure cap must win.
        manager, _ = make_manager(
            balance="10000", risk_fraction="0.01", max_position_fraction="0.10"
        )
        order = manager.evaluate(make_signal(Side.BUY, stop="99.9"), Decimal("100"))

        assert order is not None
        assert order.quantity_base == Decimal("10")  # 10% of 10000 / price 100

    def test_quantity_is_capped_by_spendable_balance(self) -> None:
        # Risk budget alone would size 100 units (5000 risk / 50 stop distance),
        # a 10000 notional — but only 10000 * (1 - fee buffer) = 9950 is spendable.
        manager, _ = make_manager(
            balance="10000", risk_fraction="0.50", max_position_fraction="1.0"
        )
        order = manager.evaluate(make_signal(Side.BUY, stop="50"), Decimal("100"))

        assert order is not None
        assert order.quantity_base == Decimal("99.5")  # 9950 / price 100

    def test_stop_at_or_above_price_is_vetoed(self) -> None:
        manager, _ = make_manager()
        assert manager.evaluate(make_signal(Side.BUY, stop="100"), Decimal("100")) is None
        assert manager.evaluate(make_signal(Side.BUY, stop="105"), Decimal("100")) is None

    def test_entry_with_existing_position_is_vetoed(self) -> None:
        manager, portfolio = make_manager()
        open_position(portfolio, price="100", quantity="1")
        assert manager.evaluate(make_signal(Side.BUY, stop="95"), Decimal("100")) is None

    def test_sizing_never_exceeds_limits_property(self) -> None:
        """Property over a price/stop grid: every cap holds for every order."""
        manager, portfolio = make_manager(balance="10000", risk_fraction="0.02")
        equity = portfolio.equity_quote({})
        for price_int in range(10, 200, 17):
            for stop_offset in (1, 3, 7, 15):
                price = Decimal(price_int)
                stop = price - Decimal(stop_offset)
                if stop <= 0:
                    continue
                order = manager.evaluate(make_signal(Side.BUY, stop=str(stop)), price)
                if order is None:
                    continue
                assert order.quantity_base > 0
                assert order.quantity_base * (price - stop) <= equity * Decimal("0.02")
                assert order.quantity_base * price <= equity * Decimal("0.25")


def make_candle(close: str, minute: int = 0, symbol: str = "BTC/USDT") -> Candle:
    open_time = SIGNAL_TIME + timedelta(minutes=minute)
    return Candle(
        symbol=symbol,
        interval=CandleInterval.M1,
        open_time=open_time,
        close_time=open_time + timedelta(minutes=1),
        open_quote=Decimal(close),
        high_quote=Decimal(close),
        low_quote=Decimal(close),
        close_quote=Decimal(close),
        volume_base=Decimal("1"),
    )


class TestBreakerIntegration:
    @staticmethod
    def make_braked_manager(breakers: BreakerConfig) -> tuple[RiskManager, Portfolio]:
        portfolio = Portfolio(Decimal("10000"))
        return RiskManager(RiskConfig(breakers=breakers), portfolio), portfolio

    def test_tripped_breaker_vetoes_entries_but_not_exits(self) -> None:
        manager, portfolio = self.make_braked_manager(
            BreakerConfig(max_daily_loss_fraction=Decimal("0.03"))
        )
        open_position(portfolio, price="100", quantity="50")
        manager.on_candle(make_candle("100", minute=0))
        # The position marks down hard: equity 10000 -> 7500, past -3%.
        manager.on_candle(make_candle("50", minute=1))
        assert manager.breakers.tripped_reason is not None

        exit_order = manager.evaluate(make_signal(Side.SELL, stop="50"), Decimal("50"))
        assert exit_order is not None  # capital protection is never braked
        assert exit_order.quantity_base == Decimal("50")

        # Flatten, so only the breaker can veto the next entry attempt.
        portfolio.apply_fill(
            Fill(
                client_order_id="flatten",
                symbol="BTC/USDT",
                side=Side.SELL,
                price_quote=Decimal("50"),
                quantity_base=Decimal("50"),
                fee_quote=Decimal("0"),
                filled_at=SIGNAL_TIME,
            )
        )
        assert manager.evaluate(make_signal(Side.BUY, stop="45"), Decimal("50")) is None

    def test_daily_entry_cap_counts_only_sized_entries(self) -> None:
        manager, portfolio = self.make_braked_manager(BreakerConfig(max_entries_per_day=1))
        manager.on_candle(make_candle("100"))

        first = manager.evaluate(make_signal(Side.BUY, stop="95"), Decimal("100"))
        assert first is not None
        # The cap is now reached; the next entry is vetoed even though flat.
        portfolio_is_still_flat = portfolio.position("BTC/USDT") is None
        assert portfolio_is_still_flat
        assert manager.evaluate(make_signal(Side.BUY, stop="95"), Decimal("100")) is None

    def test_loss_streak_from_fills_blocks_entries(self) -> None:
        manager, portfolio = self.make_braked_manager(
            BreakerConfig(
                loss_streak_threshold=2,
                loss_streak_cooldown=timedelta(hours=4),
                # Loosen the equity brakes so only the streak can block.
                max_daily_loss_fraction=Decimal("0.99"),
                max_drawdown_fraction=Decimal("0.99"),
            )
        )
        for round_trip in range(2):  # two losing round trips: buy 100, sell 90
            buy = Fill(
                client_order_id=f"buy-{round_trip}",
                symbol="BTC/USDT",
                side=Side.BUY,
                price_quote=Decimal("100"),
                quantity_base=Decimal("1"),
                fee_quote=Decimal("0"),
                filled_at=SIGNAL_TIME,
            )
            sell = buy.model_copy(
                update={
                    "client_order_id": f"sell-{round_trip}",
                    "side": Side.SELL,
                    "price_quote": Decimal("90"),
                }
            )
            portfolio.apply_fill(buy)
            manager.on_fill(buy)
            portfolio.apply_fill(sell)
            manager.on_fill(sell)

        assert manager.evaluate(make_signal(Side.BUY, stop="95"), Decimal("100")) is None
        # After the cooldown the same signal sizes again.
        late = make_signal(Side.BUY, stop="95").model_copy(
            update={"created_at": SIGNAL_TIME + timedelta(hours=5)}
        )
        assert manager.evaluate(late, Decimal("100")) is not None

    def test_partial_exit_fills_count_as_one_round_trip(self) -> None:
        """One losing exit filled in parts must not be a streak of losses."""
        manager, portfolio = self.make_braked_manager(
            BreakerConfig(
                loss_streak_threshold=2,
                loss_streak_cooldown=timedelta(hours=4),
                max_daily_loss_fraction=Decimal("0.99"),
                max_drawdown_fraction=Decimal("0.99"),
            )
        )
        buy = Fill(
            client_order_id="buy",
            symbol="BTC/USDT",
            side=Side.BUY,
            price_quote=Decimal("100"),
            quantity_base=Decimal("2"),
            fee_quote=Decimal("0"),
            filled_at=SIGNAL_TIME,
        )
        portfolio.apply_fill(buy)
        manager.on_fill(buy)
        # The exit fills in two losing parts; threshold 2 would trip if each
        # part were (wrongly) counted as its own losing round trip.
        for part in range(2):
            partial = buy.model_copy(
                update={
                    "client_order_id": f"sell-{part}",
                    "side": Side.SELL,
                    "price_quote": Decimal("90"),
                    "quantity_base": Decimal("1"),
                }
            )
            portfolio.apply_fill(partial)
            manager.on_fill(partial)

        # One completed round trip = streak of 1: no cooldown yet.
        assert manager.evaluate(make_signal(Side.BUY, stop="95"), Decimal("100")) is not None


class TestMultiSymbolEquity:
    def test_entry_is_vetoed_until_other_open_positions_have_marks(self) -> None:
        """Account equity cannot be guessed: no mark for ETH, no BTC entry."""
        manager, portfolio = make_manager()
        portfolio.apply_fill(
            Fill(
                client_order_id="eth",
                symbol="ETH/USDT",
                side=Side.BUY,
                price_quote=Decimal("10"),
                quantity_base=Decimal("5"),
                fee_quote=Decimal("0"),
                filled_at=SIGNAL_TIME,
            )
        )

        assert manager.evaluate(make_signal(Side.BUY, stop="95"), Decimal("100")) is None

        # Once ETH has a mark, the entry sizes against account-wide equity.
        manager.on_candle(make_candle("12", symbol="ETH/USDT"))
        order = manager.evaluate(make_signal(Side.BUY, stop="95"), Decimal("100"))
        assert order is not None
        # equity = 9950 free + 5 * 12 marked = 10010; 1% / 5 stop distance
        assert order.quantity_base == Decimal("20.02")

    def test_exits_never_need_marks(self) -> None:
        manager, portfolio = make_manager()
        portfolio.apply_fill(
            Fill(
                client_order_id="eth",
                symbol="ETH/USDT",
                side=Side.BUY,
                price_quote=Decimal("10"),
                quantity_base=Decimal("5"),
                fee_quote=Decimal("0"),
                filled_at=SIGNAL_TIME,
            )
        )
        open_position(portfolio, price="100", quantity="1")
        # No marks cached at all; the exit must still pass.
        exit_order = manager.evaluate(make_signal(Side.SELL, stop="100"), Decimal("100"))
        assert exit_order is not None


class TestExits:
    def test_exit_returns_full_position_market_sell(self) -> None:
        manager, portfolio = make_manager()
        open_position(portfolio, price="100", quantity="3")
        order = manager.evaluate(make_signal(Side.SELL, stop="100"), Decimal("100"))

        assert order is not None
        assert order.side == Side.SELL
        assert order.order_type == OrderType.MARKET
        assert order.quantity_base == Decimal("3")

    def test_exit_without_position_is_dropped(self) -> None:
        manager, _ = make_manager()
        assert manager.evaluate(make_signal(Side.SELL, stop="100"), Decimal("100")) is None
