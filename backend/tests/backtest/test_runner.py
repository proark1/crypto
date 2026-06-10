"""End-to-end: candles -> strategy -> risk -> simulator -> portfolio."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from tradebot.backtest import BacktestResult, BacktestRunner
from tradebot.core.models import Candle, CandleInterval, Side
from tradebot.execution import FillSimulatorConfig, SimulatedExecutionAdapter
from tradebot.portfolio import Portfolio
from tradebot.risk import RiskConfig, RiskManager
from tradebot.strategies import TrendFollowingConfig, TrendFollowingStrategy

BASE_TIME = datetime(2026, 1, 2, 0, 0, tzinfo=UTC)
INITIAL_BALANCE = Decimal("10000")

# Flat warmup, clean uptrend (cross up), clean downtrend (cross down), flat tail.
CLOSES = (
    [100.0] * 6
    + [100.0 + 4 * i for i in range(1, 11)]  # rally to 140
    + [140.0 - 6 * i for i in range(1, 11)]  # collapse to 80
    + [80.0] * 4
)


def make_candles() -> list[Candle]:
    candles = []
    previous_close = CLOSES[0]
    for index, close in enumerate(CLOSES):
        open_time = BASE_TIME + timedelta(minutes=index)
        open_price = Decimal(str(previous_close))
        close_price = Decimal(str(close))
        candles.append(
            Candle(
                symbol="BTC/USDT",
                interval=CandleInterval.M1,
                open_time=open_time,
                close_time=open_time + timedelta(minutes=1),
                open_quote=open_price,
                high_quote=max(open_price, close_price) + Decimal("0.5"),
                low_quote=min(open_price, close_price) - Decimal("0.5"),
                close_quote=close_price,
                volume_base=Decimal("10"),
            )
        )
        previous_close = close
    return candles


async def run_backtest() -> BacktestResult:
    portfolio = Portfolio(INITIAL_BALANCE)
    strategy = TrendFollowingStrategy(
        TrendFollowingConfig(fast_ema_period=3, slow_ema_period=6, atr_period=3)
    )
    risk_manager = RiskManager(RiskConfig(), portfolio)
    adapter = SimulatedExecutionAdapter(FillSimulatorConfig())
    runner = BacktestRunner(strategy, risk_manager, portfolio, adapter)
    return await runner.run(make_candles())


class TestEndToEnd:
    async def test_full_round_trip_happens(self) -> None:
        result = await run_backtest()

        sides = [fill.side for fill in result.fills]
        assert sides == [Side.BUY, Side.SELL]

    async def test_equity_curve_is_marked_every_candle(self) -> None:
        result = await run_backtest()

        assert len(result.equity_curve) == len(CLOSES)
        assert result.equity_curve[0][1] == INITIAL_BALANCE  # flat during warmup
        assert result.equity_curve[-1][1] == result.final_equity_quote

    async def test_final_equity_reflects_realized_pnl_when_flat(self) -> None:
        result = await run_backtest()

        assert result.final_equity_quote == INITIAL_BALANCE + result.realized_pnl_quote

    async def test_fees_and_slippage_are_paid(self) -> None:
        result = await run_backtest()

        assert all(fill.fee_quote > 0 for fill in result.fills)
        buy = result.fills[0]
        # Market buy fills at next candle's open, pushed up by slippage.
        assert buy.price_quote > Decimal("100")

    async def test_runs_are_deterministic(self) -> None:
        first = await run_backtest()
        second = await run_backtest()

        assert [
            (f.side, f.price_quote, f.quantity_base, f.fee_quote, f.filled_at) for f in first.fills
        ] == [
            (f.side, f.price_quote, f.quantity_base, f.fee_quote, f.filled_at) for f in second.fills
        ]
        assert first.equity_curve == second.equity_curve

    async def test_empty_series_is_rejected(self) -> None:
        portfolio = Portfolio(INITIAL_BALANCE)
        runner = BacktestRunner(
            TrendFollowingStrategy(TrendFollowingConfig()),
            RiskManager(RiskConfig(), portfolio),
            portfolio,
            SimulatedExecutionAdapter(FillSimulatorConfig()),
        )
        with pytest.raises(ValueError, match="empty candle series"):
            await runner.run([])

    async def test_mixed_symbols_are_rejected(self) -> None:
        portfolio = Portfolio(INITIAL_BALANCE)
        runner = BacktestRunner(
            TrendFollowingStrategy(TrendFollowingConfig()),
            RiskManager(RiskConfig(), portfolio),
            portfolio,
            SimulatedExecutionAdapter(FillSimulatorConfig()),
        )
        candles = make_candles()
        other = candles[1].model_copy(update={"symbol": "ETH/USDT"})
        with pytest.raises(ValueError, match="one symbol"):
            await runner.run([candles[0], other])
