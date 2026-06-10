"""Engine tests: the paper-trading loop driven over the event bus."""

import os
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from tradebot.core.events import CandleClosed, EventBus
from tradebot.core.models import Candle, CandleInterval, Fill, Side
from tradebot.engine import TradingEngine
from tradebot.execution import FillSimulatorConfig, SimulatedExecutionAdapter
from tradebot.persistence import Database, FillStore
from tradebot.persistence.database import metadata
from tradebot.portfolio import Portfolio
from tradebot.risk import RiskConfig, RiskManager
from tradebot.strategies import TrendFollowingConfig, TrendFollowingStrategy

BASE_TIME = datetime(2026, 1, 2, 0, 0, tzinfo=UTC)
INITIAL_BALANCE = Decimal("10000")
DEFAULT_URL = "postgresql+asyncpg://postgres:test@localhost:5432/tradebot_test"

# Same shape as the backtest end-to-end series: warmup, rally, collapse, tail.
CLOSES = (
    [100.0] * 6
    + [100.0 + 4 * i for i in range(1, 11)]
    + [140.0 - 6 * i for i in range(1, 11)]
    + [80.0] * 4
)


@pytest.fixture
async def database() -> AsyncIterator[Database]:
    url = os.environ.get("TEST_DATABASE_URL", DEFAULT_URL)
    db = Database(url)
    try:
        async with db.engine.begin() as connection:
            await connection.run_sync(metadata.drop_all)
            await connection.run_sync(metadata.create_all)
    except Exception as error:  # pragma: no cover - environment-dependent
        await db.engine.dispose()
        pytest.skip(f"Postgres unavailable at {url}: {error}")
    async with db:
        yield db


def make_candle(index: int, close: float, symbol: str = "BTC/USDT") -> Candle:
    open_time = BASE_TIME + timedelta(minutes=index)
    close_price = Decimal(str(close))
    return Candle(
        symbol=symbol,
        interval=CandleInterval.M1,
        open_time=open_time,
        close_time=open_time + timedelta(minutes=1),
        open_quote=close_price,
        high_quote=close_price + Decimal("0.5"),
        low_quote=close_price - Decimal("0.5"),
        close_quote=close_price,
        volume_base=Decimal("10"),
    )


def make_engine(
    portfolio: Portfolio, fill_store: FillStore | None = None, symbol: str | None = "BTC/USDT"
) -> TradingEngine:
    strategy = TrendFollowingStrategy(
        TrendFollowingConfig(fast_ema_period=3, slow_ema_period=6, atr_period=3)
    )
    return TradingEngine(
        strategy,
        RiskManager(RiskConfig(), portfolio),
        portfolio,
        SimulatedExecutionAdapter(FillSimulatorConfig()),
        symbol=symbol,
        fill_store=fill_store,
    )


class TestPaperFlowOverBus:
    async def test_round_trip_with_journal(self, database: Database) -> None:
        portfolio = Portfolio(INITIAL_BALANCE)
        fill_store = FillStore(database)
        engine = make_engine(portfolio, fill_store)
        bus = EventBus()
        engine.attach_to(bus)

        for index, close in enumerate(CLOSES):
            await bus.publish(CandleClosed(candle=make_candle(index, close)))

        assert [f.side for f in engine.fills] == [Side.BUY, Side.SELL]
        assert portfolio.position("BTC/USDT") is None  # flat after the collapse
        journal = await fill_store.fetch_all()
        assert [f.client_order_id for f in journal] == [f.client_order_id for f in engine.fills]
        # The books reconcile: equity identity holds after the round trip.
        assert portfolio.equity_quote({}) == INITIAL_BALANCE + portfolio.realized_pnl_quote()

    async def test_other_symbols_and_intervals_are_ignored(self) -> None:
        portfolio = Portfolio(INITIAL_BALANCE)
        engine = make_engine(portfolio, symbol="BTC/USDT")
        bus = EventBus()
        engine.attach_to(bus)

        other_symbol = make_candle(0, 100.0, symbol="ETH/USDT")
        await bus.publish(CandleClosed(candle=other_symbol))
        five_minute = make_candle(1, 100.0).model_copy(
            update={
                "interval": CandleInterval.M5,
                "close_time": BASE_TIME + timedelta(minutes=6),
            }
        )
        await bus.publish(CandleClosed(candle=five_minute))

        assert engine.fills == ()  # nothing reached the strategy or adapter

    async def test_journal_failure_leaves_in_memory_books_untouched(
        self, database: Database
    ) -> None:
        """Persist-first ordering: memory must never run ahead of the journal."""

        class BrokenFillStore(FillStore):
            async def append(self, fill: Fill) -> None:
                raise ConnectionError("database write failed")

        portfolio = Portfolio(INITIAL_BALANCE)
        engine = make_engine(portfolio, BrokenFillStore(database))

        with pytest.raises(ConnectionError, match="database write failed"):
            for index, close in enumerate(CLOSES):
                await engine.process_candle(make_candle(index, close))

        assert portfolio.position("BTC/USDT") is None
        assert portfolio.quote_balance == INITIAL_BALANCE
        assert engine.fills == ()

    async def test_unbound_engine_binds_to_first_candle_then_rejects_others(self) -> None:
        portfolio = Portfolio(INITIAL_BALANCE)
        engine = make_engine(portfolio, symbol=None)

        await engine.process_candle(make_candle(0, 100.0))
        with pytest.raises(ValueError, match="bound to BTC/USDT"):
            await engine.process_candle(make_candle(1, 100.0, symbol="ETH/USDT"))


class TestPauseAndKill:
    async def test_paused_engine_discards_signals_but_keeps_indicators_warm(self) -> None:
        portfolio = Portfolio(INITIAL_BALANCE)
        engine = make_engine(portfolio)

        engine.pause()
        for index, close in enumerate(CLOSES):
            await engine.process_candle(make_candle(index, close))
        assert engine.fills == ()  # the rally's entry signal was discarded

        engine.resume()
        # Indicators consumed the whole series: a fresh rally triggers a new
        # cross without re-warming from zero.
        next_index = len(CLOSES)
        for offset, close in enumerate([80.0 + 6 * i for i in range(1, 11)]):
            await engine.process_candle(make_candle(next_index + offset, close))
        resumed_fills: tuple[Fill, ...] = engine.fills
        assert [fill.side for fill in resumed_fills] == [Side.BUY]

    async def test_kill_cancels_resting_orders_and_flattens(self) -> None:
        portfolio = Portfolio(INITIAL_BALANCE)
        engine = make_engine(portfolio)
        # Ride the rally into an open position (stop before the collapse).
        for index, close in enumerate(CLOSES[:16]):
            await engine.process_candle(make_candle(index, close))
        assert portfolio.position("BTC/USDT") is not None

        submitted = await engine.kill()
        assert submitted is True
        assert engine.paused is True

        await engine.process_candle(make_candle(16, CLOSES[16]))
        assert portfolio.position("BTC/USDT") is None  # flattened while paused

    async def test_kill_before_any_candle_halts_without_exit(self) -> None:
        portfolio = Portfolio(INITIAL_BALANCE)
        engine = make_engine(portfolio)
        submitted = await engine.kill()
        assert submitted is False
        assert engine.paused is True


class TestParityWithBacktest:
    async def test_bus_driven_engine_matches_backtest_runner_exactly(self) -> None:
        """Paper mode and backtest mode are the same code; prove it."""
        from tradebot.backtest import BacktestRunner

        candles = [make_candle(i, c) for i, c in enumerate(CLOSES)]

        bus_portfolio = Portfolio(INITIAL_BALANCE)
        engine = make_engine(bus_portfolio)
        bus = EventBus()
        engine.attach_to(bus)
        for candle in candles:
            await bus.publish(CandleClosed(candle=candle))

        runner_portfolio = Portfolio(INITIAL_BALANCE)
        strategy = TrendFollowingStrategy(
            TrendFollowingConfig(fast_ema_period=3, slow_ema_period=6, atr_period=3)
        )
        runner = BacktestRunner(
            strategy,
            RiskManager(RiskConfig(), runner_portfolio),
            runner_portfolio,
            SimulatedExecutionAdapter(FillSimulatorConfig()),
        )
        result = await runner.run(candles)

        assert engine.fills == result.fills
        assert bus_portfolio.realized_pnl_quote() == result.realized_pnl_quote
