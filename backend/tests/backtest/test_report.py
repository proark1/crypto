from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from tradebot.backtest import BacktestResult, build_report
from tradebot.core.models import Candle, CandleInterval, Fill, Side

BASE_TIME = datetime(2026, 1, 2, 0, 0, tzinfo=UTC)
INITIAL = Decimal("1000")


def make_candle(index: int, close: str) -> Candle:
    open_time = BASE_TIME + timedelta(minutes=index)
    price = Decimal(close)
    return Candle(
        symbol="BTC/USDT",
        interval=CandleInterval.M1,
        open_time=open_time,
        close_time=open_time + timedelta(minutes=1),
        open_quote=price,
        high_quote=price + Decimal("1"),
        low_quote=price - Decimal("1") if price > 1 else price,
        close_quote=price,
        volume_base=Decimal("1"),
    )


def make_fill(side: Side, price: str, quantity: str = "1", fee: str = "0", minute: int = 0) -> Fill:
    return Fill(
        client_order_id=f"ord-{minute}",
        symbol="BTC/USDT",
        side=side,
        price_quote=Decimal(price),
        quantity_base=Decimal(quantity),
        fee_quote=Decimal(fee),
        filled_at=BASE_TIME + timedelta(minutes=minute),
    )


def make_result(
    fills: list[Fill],
    equity_points: list[str],
    realized: str = "0",
) -> BacktestResult:
    curve = tuple(
        (BASE_TIME + timedelta(minutes=i + 1), Decimal(point))
        for i, point in enumerate(equity_points)
    )
    return BacktestResult(
        fills=tuple(fills),
        equity_curve=curve,
        final_equity_quote=curve[-1][1],
        realized_pnl_quote=Decimal(realized),
    )


class TestReturns:
    def test_total_return_and_buy_and_hold(self) -> None:
        candles = [make_candle(0, "100"), make_candle(1, "110")]
        result = make_result([], ["1000", "1050"])
        report = build_report(result, candles, INITIAL)

        assert report.total_return_fraction == Decimal("0.05")
        assert report.buy_and_hold_return_fraction == Decimal("0.1")
        assert report.beats_buy_and_hold is False

    def test_beating_buy_and_hold_in_a_down_market(self) -> None:
        candles = [make_candle(0, "100"), make_candle(1, "80")]
        result = make_result([], ["1000", "1000"])
        report = build_report(result, candles, INITIAL)

        assert report.buy_and_hold_return_fraction == Decimal("-0.2")
        assert report.beats_buy_and_hold is True


class TestDrawdown:
    def test_max_drawdown_is_largest_peak_to_trough(self) -> None:
        candles = [make_candle(i, "100") for i in range(5)]
        result = make_result([], ["1000", "1200", "900", "1100", "880"])
        report = build_report(result, candles, INITIAL)

        # Peak 1200 -> trough 880 is 26.67%; peak->900 was only 25%.
        expected = (Decimal("320") / Decimal("1200")).quantize(Decimal("1e-12"))
        assert report.max_drawdown_fraction == expected

    def test_monotonic_equity_has_zero_drawdown(self) -> None:
        candles = [make_candle(i, "100") for i in range(3)]
        result = make_result([], ["1000", "1100", "1200"])
        assert build_report(result, candles, INITIAL).max_drawdown_fraction == Decimal("0")


class TestRoundTrips:
    def test_win_rate_and_profit_factor_from_fills(self) -> None:
        fills = [
            make_fill(Side.BUY, "100", fee="1", minute=0),
            make_fill(Side.SELL, "120", fee="1", minute=1),  # +18 net
            make_fill(Side.BUY, "100", fee="1", minute=2),
            make_fill(Side.SELL, "95", fee="1", minute=3),  # -7 net
        ]
        candles = [make_candle(i, "100") for i in range(4)]
        result = make_result(fills, ["1000"] * 4, realized="11")
        report = build_report(result, candles, INITIAL)

        assert report.round_trips == 2
        assert report.winning_round_trips == 1
        assert report.win_rate == 0.5
        assert report.profit_factor == pytest.approx(18 / 7)
        assert report.total_fees_quote == Decimal("4")

    def test_no_trades_yields_none_rates(self) -> None:
        candles = [make_candle(0, "100")]
        report = build_report(make_result([], ["1000"]), candles, INITIAL)

        assert report.round_trips == 0
        assert report.win_rate is None
        assert report.profit_factor is None

    def test_all_winning_trades_have_no_profit_factor(self) -> None:
        fills = [
            make_fill(Side.BUY, "100", minute=0),
            make_fill(Side.SELL, "120", minute=1),
        ]
        candles = [make_candle(i, "100") for i in range(2)]
        report = build_report(make_result(fills, ["1000", "1020"]), candles, INITIAL)

        assert report.win_rate == 1.0
        assert report.profit_factor is None  # no losses to divide by

    def test_empty_candles_are_rejected(self) -> None:
        with pytest.raises(ValueError, match="empty candle series"):
            build_report(make_result([], ["1000"]), [], INITIAL)
