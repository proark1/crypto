import random
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from tradebot.core.models import Fill, Side
from tradebot.portfolio import Portfolio

FILL_TIME = datetime(2026, 1, 2, 3, 4, tzinfo=UTC)


def make_fill(
    side: Side,
    price: str,
    quantity: str,
    fee: str = "0",
    symbol: str = "BTC/USDT",
) -> Fill:
    return Fill(
        client_order_id="order-1",
        symbol=symbol,
        side=side,
        price_quote=Decimal(price),
        quantity_base=Decimal(quantity),
        fee_quote=Decimal(fee),
        filled_at=FILL_TIME,
    )


class TestBuys:
    def test_buy_opens_position_and_reduces_balance(self) -> None:
        portfolio = Portfolio(Decimal("1000"))
        portfolio.apply_fill(make_fill(Side.BUY, price="100", quantity="2", fee="1"))

        assert portfolio.quote_balance == Decimal("799")
        position = portfolio.position("BTC/USDT")
        assert position is not None
        assert position.quantity_base == Decimal("2")
        assert position.cost_basis_quote == Decimal("201")

    def test_buy_fee_is_capitalized_into_average_entry(self) -> None:
        portfolio = Portfolio(Decimal("1000"))
        portfolio.apply_fill(make_fill(Side.BUY, price="100", quantity="1", fee="1"))

        position = portfolio.position("BTC/USDT")
        assert position is not None
        assert position.average_entry_price_quote == Decimal("101")

    def test_second_buy_averages_cost_basis(self) -> None:
        portfolio = Portfolio(Decimal("1000"))
        portfolio.apply_fill(make_fill(Side.BUY, price="100", quantity="1"))
        portfolio.apply_fill(make_fill(Side.BUY, price="200", quantity="1"))

        position = portfolio.position("BTC/USDT")
        assert position is not None
        assert position.quantity_base == Decimal("2")
        assert position.average_entry_price_quote == Decimal("150")

    def test_buy_exceeding_balance_raises(self) -> None:
        portfolio = Portfolio(Decimal("100"))
        with pytest.raises(ValueError, match="reconciliation error"):
            portfolio.apply_fill(make_fill(Side.BUY, price="100", quantity="2"))

    def test_negative_initial_balance_raises(self) -> None:
        with pytest.raises(ValueError, match="cannot be negative"):
            Portfolio(Decimal("-1"))


class TestSells:
    def test_full_sell_closes_position_and_realizes_pnl(self) -> None:
        portfolio = Portfolio(Decimal("1000"))
        portfolio.apply_fill(make_fill(Side.BUY, price="100", quantity="2"))
        portfolio.apply_fill(make_fill(Side.SELL, price="150", quantity="2", fee="3"))

        assert portfolio.position("BTC/USDT") is None
        assert portfolio.realized_pnl_quote("BTC/USDT") == Decimal("97")  # 100 profit - 3 fee
        assert portfolio.quote_balance == Decimal("1097")

    def test_partial_sell_keeps_average_entry_unchanged(self) -> None:
        portfolio = Portfolio(Decimal("1000"))
        portfolio.apply_fill(make_fill(Side.BUY, price="100", quantity="4"))
        portfolio.apply_fill(make_fill(Side.SELL, price="120", quantity="1"))

        position = portfolio.position("BTC/USDT")
        assert position is not None
        assert position.quantity_base == Decimal("3")
        assert position.average_entry_price_quote == Decimal("100")
        assert portfolio.realized_pnl_quote("BTC/USDT") == Decimal("20")

    def test_selling_more_than_held_raises(self) -> None:
        portfolio = Portfolio(Decimal("1000"))
        portfolio.apply_fill(make_fill(Side.BUY, price="100", quantity="1"))
        with pytest.raises(ValueError, match="cannot go short"):
            portfolio.apply_fill(make_fill(Side.SELL, price="100", quantity="2"))

    def test_selling_with_no_position_raises(self) -> None:
        portfolio = Portfolio(Decimal("1000"))
        with pytest.raises(ValueError, match="cannot go short"):
            portfolio.apply_fill(make_fill(Side.SELL, price="100", quantity="1"))

    def test_loss_making_sell_realizes_negative_pnl(self) -> None:
        portfolio = Portfolio(Decimal("1000"))
        portfolio.apply_fill(make_fill(Side.BUY, price="100", quantity="1"))
        portfolio.apply_fill(make_fill(Side.SELL, price="80", quantity="1"))

        assert portfolio.realized_pnl_quote() == Decimal("-20")


class TestValuation:
    def test_unrealized_pnl_marks_to_market(self) -> None:
        portfolio = Portfolio(Decimal("1000"))
        portfolio.apply_fill(make_fill(Side.BUY, price="100", quantity="2", fee="2"))

        position = portfolio.position("BTC/USDT")
        assert position is not None
        assert position.unrealized_pnl_quote(Decimal("110")) == Decimal("18")

    def test_equity_is_balance_plus_marked_positions(self) -> None:
        portfolio = Portfolio(Decimal("1000"))
        portfolio.apply_fill(make_fill(Side.BUY, price="100", quantity="2"))
        portfolio.apply_fill(make_fill(Side.BUY, price="50", quantity="4", symbol="ETH/USDT"))

        equity = portfolio.equity_quote({"BTC/USDT": Decimal("110"), "ETH/USDT": Decimal("45")})
        assert equity == Decimal("1000") - Decimal("400") + Decimal("220") + Decimal("180")

    def test_equity_with_missing_price_raises(self) -> None:
        portfolio = Portfolio(Decimal("1000"))
        portfolio.apply_fill(make_fill(Side.BUY, price="100", quantity="1"))
        with pytest.raises(ValueError, match="no current price"):
            portfolio.equity_quote({})

    def test_realized_pnl_sums_across_symbols(self) -> None:
        portfolio = Portfolio(Decimal("1000"))
        portfolio.apply_fill(make_fill(Side.BUY, price="100", quantity="1"))
        portfolio.apply_fill(make_fill(Side.SELL, price="110", quantity="1"))
        portfolio.apply_fill(make_fill(Side.BUY, price="50", quantity="1", symbol="ETH/USDT"))
        portfolio.apply_fill(make_fill(Side.SELL, price="45", quantity="1", symbol="ETH/USDT"))

        assert portfolio.realized_pnl_quote() == Decimal("5")


class TestAccountingInvariants:
    def test_random_round_trips_conserve_value_with_zero_fees(self) -> None:
        """Buy and fully sell at identical prices N times: equity returns to start.

        Exact Decimal arithmetic means exact equality — any drift would expose
        a leak in the cost-basis or realized-PnL bookkeeping.
        """
        rng = random.Random(7)
        portfolio = Portfolio(Decimal("10000"))
        for _ in range(200):
            price = Decimal(rng.randint(1, 500))
            quantity = Decimal(rng.randint(1, 10))
            if price * quantity > portfolio.quote_balance:
                continue
            portfolio.apply_fill(make_fill(Side.BUY, price=str(price), quantity=str(quantity)))
            portfolio.apply_fill(make_fill(Side.SELL, price=str(price), quantity=str(quantity)))

        assert portfolio.quote_balance == Decimal("10000")
        assert portfolio.realized_pnl_quote() == Decimal("0")
        assert portfolio.positions == {}

    def test_equity_identity_holds_after_arbitrary_fills(self) -> None:
        """equity == initial + realized + unrealized, for any fill sequence."""
        rng = random.Random(11)
        initial = Decimal("10000")
        portfolio = Portfolio(initial)
        last_price = Decimal("100")
        for _ in range(300):
            last_price = Decimal(rng.randint(50, 150))
            quantity = Decimal(rng.randint(1, 5))
            fee = Decimal(rng.randint(0, 2))
            position = portfolio.position("BTC/USDT")
            held = position.quantity_base if position is not None else Decimal(0)
            can_buy = last_price * quantity + fee <= portfolio.quote_balance
            if held >= quantity and (not can_buy or rng.random() < 0.5):
                portfolio.apply_fill(
                    make_fill(
                        Side.SELL, price=str(last_price), quantity=str(quantity), fee=str(fee)
                    )
                )
            elif can_buy:
                portfolio.apply_fill(
                    make_fill(Side.BUY, price=str(last_price), quantity=str(quantity), fee=str(fee))
                )
            position = portfolio.position("BTC/USDT")
            unrealized = (
                position.unrealized_pnl_quote(last_price) if position is not None else Decimal(0)
            )
            expected_equity = initial + portfolio.realized_pnl_quote() + unrealized
            assert portfolio.equity_quote({"BTC/USDT": last_price}) == expected_equity
