from datetime import UTC, datetime
from decimal import Decimal

from tradebot.core.models import Fill, OrderType, Side, Signal
from tradebot.portfolio import Portfolio
from tradebot.risk import RiskConfig, RiskManager

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
        manager, portfolio = make_manager(
            balance="10000", risk_fraction="0.50", max_position_fraction="1.0"
        )
        open_position(portfolio, price="100", quantity="50")  # 5000 spent
        portfolio_free = portfolio.quote_balance
        # Risk budget would buy far more than the remaining balance affords.
        order = manager.evaluate(make_signal(Side.BUY, stop="50"), Decimal("100"))

        assert order is None or order.quantity_base * Decimal("100") <= portfolio_free

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
