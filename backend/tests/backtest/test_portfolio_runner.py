"""Account-level backtests: shared equity, caps, and brakes across symbols."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from tradebot.backtest import BacktestRunner, PortfolioBacktestRunner
from tradebot.core.models import Candle, CandleInterval, Side, SymbolFilters
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
        # One equity point per close time, marked once every book updated —
        # never one stale-marked point per symbol.
        timestamps = [at for at, _ in result.equity_curve]
        assert len(timestamps) == len(set(timestamps)) == len(CLOSES)

    async def test_exposure_cap_is_enforced_across_symbols(self) -> None:
        """The point of the account runner: one coin's position consumes the
        other's headroom, which no single-symbol backtest can show."""
        # Same-candle rallies on purpose: both entry signals fire at the
        # same close, neither order has filled yet — only the committed
        # (submitted-but-unfilled) notional can enforce the cap here.
        eth_closes = [c / 10 for c in CLOSES]
        candles = [make_candle(i, c, "BTC/USDT") for i, c in enumerate(CLOSES)] + [
            make_candle(i, c, "ETH/USDT") for i, c in enumerate(eth_closes)
        ]
        portfolio = Portfolio(INITIAL_BALANCE)
        # Total exposure equals the per-position cap: whoever enters first
        # takes the whole budget.
        config = RiskConfig(
            # Risk sizing large enough that the position cap binds: the
            # first entry consumes the entire account exposure budget.
            risk_per_trade_fraction=Decimal("0.05"),
            max_position_fraction=Decimal("0.25"),
            max_total_exposure_fraction=Decimal("0.25"),
        )
        # Venue minimums, as production always has them: quantize rounding
        # can leave nano-headroom under the cap, and the min-notional filter
        # is what keeps that from becoming a dust position.
        filters = {
            symbol: SymbolFilters(min_notional_quote=Decimal("5"))
            for symbol in ("BTC/USDT", "ETH/USDT")
        }
        result = await PortfolioBacktestRunner(
            make_strategy, RiskManager(config, portfolio, filters), portfolio
        ).run(candles)

        btc_sides = [f.side for f in result.fills if f.symbol == "BTC/USDT"]
        eth_buys = [f for f in result.fills if f.symbol == "ETH/USDT" and f.side == Side.BUY]
        assert btc_sides[0] == Side.BUY  # first in line got the budget
        assert eth_buys == []  # the committed claim left ETH below the venue minimum

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


class TestAccountReport:
    async def test_two_symbol_run_reports_account_level_figures(self) -> None:
        from tradebot.backtest import build_account_report

        eth_closes = [100.0] * 3 + [c / 10 for c in CLOSES]
        candles = [make_candle(i, c, "BTC/USDT") for i, c in enumerate(CLOSES)] + [
            make_candle(i, c, "ETH/USDT") for i, c in enumerate(eth_closes)
        ]
        portfolio = Portfolio(INITIAL_BALANCE)
        result = await PortfolioBacktestRunner(
            make_strategy, RiskManager(RiskConfig(), portfolio), portfolio
        ).run(candles)

        report = build_account_report(result, INITIAL_BALANCE)

        # Turnover and fees are exact sums over the journaled fills.
        assert report.turnover_quote == sum(
            (f.price_quote * f.quantity_base for f in result.fills), Decimal(0)
        )
        assert report.total_fees_quote == sum((f.fee_quote for f in result.fills), Decimal(0))
        assert report.turnover_ratio == report.turnover_quote / INITIAL_BALANCE
        # Exposure stayed within the configured account ceiling and was
        # actually used (both coins held positions at some point).
        assert Decimal(0) < report.peak_exposure_fraction <= Decimal("0.5")
        assert Decimal(0) < report.average_exposure_fraction < report.peak_exposure_fraction
        # Per-symbol attribution reconciles with the account total.
        assert set(report.fills_by_symbol) == {"BTC/USDT", "ETH/USDT"}
        assert sum(report.realized_pnl_by_symbol.values()) == result.realized_pnl_quote
        assert (
            report.total_return_fraction
            == (report.final_equity_quote - INITIAL_BALANCE) / INITIAL_BALANCE
        )

    async def test_flat_window_reports_zeroes_not_nonsense(self) -> None:
        from tradebot.backtest import build_account_report

        portfolio = Portfolio(INITIAL_BALANCE)
        result = await PortfolioBacktestRunner(
            make_strategy, RiskManager(RiskConfig(), portfolio), portfolio
        ).run([make_candle(i, 100.0, "BTC/USDT") for i in range(8)])  # no signals

        report = build_account_report(result, INITIAL_BALANCE)
        assert report.turnover_quote == 0
        assert report.max_drawdown_fraction == 0
        assert report.average_exposure_fraction == 0
        assert report.fills_by_symbol == {}

    async def test_composes_per_walk_forward_window(self) -> None:
        """Account-level walk-forward: one report per validation window."""
        from tradebot.backtest import build_account_report

        candles = [make_candle(i, c, "BTC/USDT") for i, c in enumerate(CLOSES)]
        halves = (candles[: len(candles) // 2], candles[len(candles) // 2 :])
        reports = []
        for window in halves:
            portfolio = Portfolio(INITIAL_BALANCE)
            result = await PortfolioBacktestRunner(
                make_strategy, RiskManager(RiskConfig(), portfolio), portfolio
            ).run(window)
            reports.append(build_account_report(result, INITIAL_BALANCE))

        assert len(reports) == 2
        assert all(r.initial_balance_quote == INITIAL_BALANCE for r in reports)
