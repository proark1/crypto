"""Account-level backtests: shared equity, caps, and brakes across symbols."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from tradebot.backtest import BacktestRunner, PortfolioBacktestRunner
from tradebot.core.models import Candle, CandleInterval, Side
from tradebot.execution import FillSimulatorConfig, SimulatedExecutionAdapter
from tradebot.portfolio import Portfolio
from tradebot.risk import RiskConfig, RiskManager
from tradebot.strategies import Strategy, TrendFollowingConfig, TrendFollowingStrategy

BASE_TIME = datetime(2026, 1, 2, 0, 0, tzinfo=UTC)
INITIAL_BALANCE = Decimal("10000")

# Warmup, rally, collapse, tail — the shape every engine test uses.
CLOSES = (
    [100.0] * 6
    + [100.0 + 4 * i for i in range(1, 11)]
    + [140.0 - 6 * i for i in range(1, 11)]
    + [80.0] * 4
)


def make_candle(index: int, close: float, symbol: str) -> Candle:
    open_time = BASE_TIME + timedelta(minutes=index)
    price = Decimal(str(close))
    return Candle(
        symbol=symbol,
        interval=CandleInterval.M1,
        open_time=open_time,
        close_time=open_time + timedelta(minutes=1),
        open_quote=price,
        high_quote=price + Decimal("0.5"),
        low_quote=price - Decimal("0.5"),
        close_quote=price,
        volume_base=Decimal("10"),
    )


def make_strategy() -> Strategy:
    return TrendFollowingStrategy(
        TrendFollowingConfig(fast_ema_period=3, slow_ema_period=6, atr_period=3)
    )


class TestPortfolioBacktestRunner:
    async def test_single_symbol_matches_the_single_symbol_runner(self) -> None:
        """On one symbol the account runner is the proven runner, exactly."""
        candles = [make_candle(i, close, "BTC/USDT") for i, close in enumerate(CLOSES)]

        single_portfolio = Portfolio(INITIAL_BALANCE)
        single = await BacktestRunner(
            make_strategy(),
            RiskManager(RiskConfig(), single_portfolio),
            single_portfolio,
            SimulatedExecutionAdapter(FillSimulatorConfig()),
        ).run(candles)

        account_portfolio = Portfolio(INITIAL_BALANCE)
        account = await PortfolioBacktestRunner(
            make_strategy,
            RiskManager(RiskConfig(), account_portfolio),
            account_portfolio,
        ).run(candles)

        assert account.fills == single.fills
        assert account.equity_curve == single.equity_curve

    async def test_two_symbols_share_one_account(self) -> None:
        candles = [make_candle(i, c, "BTC/USDT") for i, c in enumerate(CLOSES)] + [
            make_candle(i, c / 10, "ETH/USDT") for i, c in enumerate(CLOSES)
        ]
        portfolio = Portfolio(INITIAL_BALANCE)
        result = await PortfolioBacktestRunner(
            make_strategy, RiskManager(RiskConfig(), portfolio), portfolio
        ).run(candles)

        for symbol in ("BTC/USDT", "ETH/USDT"):
            sides = [f.side for f in result.fills if f.symbol == symbol]
            assert sides == [Side.BUY, Side.SELL], symbol
        # The account-level equity identity holds across both books.
        assert result.final_equity_quote == INITIAL_BALANCE + result.realized_pnl_quote

    async def test_exposure_cap_is_enforced_across_symbols(self) -> None:
        """The point of the account runner: one coin's position consumes the
        other's headroom, which no single-symbol backtest can show."""
        # ETH's rally lags three candles: its entry signal arrives after
        # BTC's position exists. (Same-candle signals would both pass risk —
        # the cap judges open positions, and neither order has filled yet;
        # committed-but-unfilled balance is a known live-trading extension.)
        eth_closes = [100.0] * 3 + [c / 10 for c in CLOSES]
        candles = [make_candle(i, c, "BTC/USDT") for i, c in enumerate(CLOSES)] + [
            make_candle(i, c, "ETH/USDT") for i, c in enumerate(eth_closes)
        ]
        portfolio = Portfolio(INITIAL_BALANCE)
        # Total exposure equals the per-position cap: whoever enters first
        # takes the whole budget.
        config = RiskConfig(
            max_position_fraction=Decimal("0.25"),
            max_total_exposure_fraction=Decimal("0.25"),
        )
        result = await PortfolioBacktestRunner(
            make_strategy, RiskManager(config, portfolio), portfolio
        ).run(candles)

        btc_sides = [f.side for f in result.fills if f.symbol == "BTC/USDT"]
        eth_buys = [f for f in result.fills if f.symbol == "ETH/USDT" and f.side == Side.BUY]
        assert btc_sides[0] == Side.BUY  # first in line got the budget
        assert eth_buys == []  # vetoed at zero headroom, loudly, not resized

    async def test_runner_is_single_use_and_rejects_empty_series(self) -> None:
        portfolio = Portfolio(INITIAL_BALANCE)
        runner = PortfolioBacktestRunner(
            make_strategy, RiskManager(RiskConfig(), portfolio), portfolio
        )
        with pytest.raises(ValueError, match="empty"):
            await runner.run([])
        # The failed run still consumed it: state may be half-built.
        with pytest.raises(RuntimeError, match="single-use"):
            await runner.run([make_candle(0, 100.0, "BTC/USDT")])
