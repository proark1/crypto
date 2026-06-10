"""Engine tests: the paper-trading loop driven over the event bus."""

import os
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from tradebot.authorization import ProposalQueue
from tradebot.core.events import CandleClosed, EventBus, FillRecorded
from tradebot.core.models import (
    AutonomyMode,
    Candle,
    CandleInterval,
    DecisionOutcome,
    Fill,
    Side,
)
from tradebot.engine import TradingEngine
from tradebot.execution import FillSimulatorConfig, SimulatedExecutionAdapter
from tradebot.persistence import Database, DecisionStore, FillStore
from tradebot.persistence.database import metadata
from tradebot.portfolio import Portfolio
from tradebot.risk import BreakerConfig, RiskConfig, RiskManager
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
    portfolio: Portfolio,
    fill_store: FillStore | None = None,
    symbol: str | None = "BTC/USDT",
    decision_store: DecisionStore | None = None,
    autonomy_mode: AutonomyMode = AutonomyMode.AUTONOMOUS,
    proposal_queue: ProposalQueue | None = None,
    risk_config: RiskConfig | None = None,
) -> TradingEngine:
    strategy = TrendFollowingStrategy(
        TrendFollowingConfig(fast_ema_period=3, slow_ema_period=6, atr_period=3)
    )
    return TradingEngine(
        strategy,
        RiskManager(risk_config or RiskConfig(), portfolio),
        portfolio,
        SimulatedExecutionAdapter(FillSimulatorConfig()),
        symbol=symbol,
        fill_store=fill_store,
        decision_store=decision_store,
        autonomy_mode=autonomy_mode,
        proposal_queue=proposal_queue,
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

    async def test_fill_recorded_events_are_published_when_attached(self) -> None:
        portfolio = Portfolio(INITIAL_BALANCE)
        engine = make_engine(portfolio)
        bus = EventBus()
        engine.attach_to(bus)
        observed: list[FillRecorded] = []

        async def on_fill(event: FillRecorded) -> None:
            # The books must already reflect the fill when observers run.
            assert portfolio.realized_pnl_quote() is not None
            observed.append(event)

        bus.subscribe(FillRecorded, on_fill)
        for index, close in enumerate(CLOSES):
            await bus.publish(CandleClosed(candle=make_candle(index, close)))

        assert [e.fill.side for e in observed] == [Side.BUY, Side.SELL]

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


class TestCircuitBreakerWiring:
    """The engine feeds the breakers; the breakers gate entries in the loop."""

    async def test_blocked_entries_are_journaled_as_vetoed(self, database: Database) -> None:
        portfolio = Portfolio(INITIAL_BALANCE)
        decision_store = DecisionStore(database)
        engine = make_engine(
            portfolio,
            decision_store=decision_store,
            risk_config=RiskConfig(breakers=BreakerConfig(max_entries_per_day=0)),
        )

        for index, close in enumerate(CLOSES):
            await engine.process_candle(make_candle(index, close))

        assert engine.fills == ()  # the cap blocked the entry end to end
        outcomes = {d.outcome for d in await decision_store.fetch_recent("BTC/USDT")}
        assert DecisionOutcome.VETOED in outcomes

    async def test_collapse_trips_breaker_but_exit_still_fills(self) -> None:
        portfolio = Portfolio(INITIAL_BALANCE)
        engine = make_engine(
            portfolio,
            risk_config=RiskConfig(
                # Any visible dip trips; the exit must still go through.
                breakers=BreakerConfig(max_daily_loss_fraction=Decimal("0.0001"))
            ),
        )

        for index, close in enumerate(CLOSES):
            await engine.process_candle(make_candle(index, close))

        assert engine.breakers.tripped_reason is not None
        assert [f.side for f in engine.fills] == [Side.BUY, Side.SELL]
        assert portfolio.position("BTC/USDT") is None  # flat: exit was not braked

    async def test_submitted_and_paused_outcomes_are_recorded(self, database: Database) -> None:
        await database.create_schema()
        store = DecisionStore(database)
        portfolio = Portfolio(INITIAL_BALANCE)
        engine = make_engine(portfolio, decision_store=store)

        # Rally produces a submitted entry...
        for index, close in enumerate(CLOSES[:16]):
            await engine.process_candle(make_candle(index, close))
        # ...then pause and ride the collapse: the exit signal is discarded.
        engine.pause()
        for offset, close in enumerate(CLOSES[16:]):
            await engine.process_candle(make_candle(16 + offset, close))

        decisions = await store.fetch_recent("BTC/USDT")
        outcomes = [d.outcome for d in decisions]
        assert DecisionOutcome.SUBMITTED in outcomes
        assert DecisionOutcome.PAUSED in outcomes
        assert all(d.reasons for d in decisions)  # explainability: never empty


def make_copilot_engine(
    portfolio: Portfolio, ttl_minutes: int = 60, drift: str = "1.0"
) -> TradingEngine:
    from datetime import timedelta as td

    return make_engine(
        portfolio,
        autonomy_mode=AutonomyMode.COPILOT,
        proposal_queue=ProposalQueue(
            ttl=td(minutes=ttl_minutes), max_drift_fraction=Decimal(drift)
        ),
    )


async def drive_until_proposal(engine: TradingEngine) -> int:
    """Feed CLOSES until the entry proposal appears; returns the next index."""
    for index, close in enumerate(CLOSES):
        await engine.process_candle(make_candle(index, close))
        if engine.pending_proposals():
            return index + 1
    raise AssertionError("series never produced a proposal")


class TestCopilotMode:
    async def test_entry_becomes_proposal_not_order(self) -> None:
        portfolio = Portfolio(INITIAL_BALANCE)
        engine = make_copilot_engine(portfolio)
        await drive_until_proposal(engine)

        assert engine.fills == ()  # nothing executed without approval
        (proposal,) = engine.pending_proposals()
        assert proposal.signal.side == Side.BUY
        assert proposal.signal.reasons  # explainability carried through

    async def test_approval_executes_with_rechecked_risk(self) -> None:
        portfolio = Portfolio(INITIAL_BALANCE)
        engine = make_copilot_engine(portfolio)
        next_index = await drive_until_proposal(engine)
        (proposal,) = engine.pending_proposals()

        detail = await engine.approve_proposal(proposal.signal.signal_id)
        assert "order submitted" in detail
        await engine.process_candle(make_candle(next_index, CLOSES[next_index]))
        assert [f.side for f in engine.fills] == [Side.BUY]

    async def test_rejection_executes_nothing(self) -> None:
        portfolio = Portfolio(INITIAL_BALANCE)
        engine = make_copilot_engine(portfolio)
        next_index = await drive_until_proposal(engine)
        (proposal,) = engine.pending_proposals()

        await engine.reject_proposal(proposal.signal.signal_id)
        for offset, close in enumerate(CLOSES[next_index:]):
            await engine.process_candle(make_candle(next_index + offset, close))
        assert engine.fills == ()
        assert engine.pending_proposals() == ()

    async def test_unanswered_proposal_expires_via_sweep(self) -> None:
        portfolio = Portfolio(INITIAL_BALANCE)
        engine = make_copilot_engine(portfolio, ttl_minutes=2)
        next_index = await drive_until_proposal(engine)

        # Three flat 1m candles pass the 2-minute TTL unanswered.
        last_close = CLOSES[next_index - 1]
        for offset in range(3):
            await engine.process_candle(make_candle(next_index + offset, last_close))
        assert engine.pending_proposals() == ()
        assert engine.fills == ()

    async def test_drifted_proposal_is_swept(self) -> None:
        portfolio = Portfolio(INITIAL_BALANCE)
        engine = make_copilot_engine(portfolio, drift="0.02")
        next_index = await drive_until_proposal(engine)

        # The rally keeps running: price soon drifts >2% past the proposal.
        for offset, close in enumerate(CLOSES[next_index:]):
            await engine.process_candle(make_candle(next_index + offset, close))
            if not engine.pending_proposals():
                break
        assert engine.pending_proposals() == ()
        assert engine.fills == ()  # never executed at a price the user didn't see

    async def test_exits_bypass_the_queue(self) -> None:
        """Capital protection never waits for a human (ARCHITECTURE.md 4.8)."""
        portfolio = Portfolio(INITIAL_BALANCE)
        engine = make_copilot_engine(portfolio)
        next_index = await drive_until_proposal(engine)
        (proposal,) = engine.pending_proposals()
        await engine.approve_proposal(proposal.signal.signal_id)

        # Ride through the collapse: the cross-down exit executes directly.
        for offset, close in enumerate(CLOSES[next_index:]):
            await engine.process_candle(make_candle(next_index + offset, close))
        assert [f.side for f in engine.fills] == [Side.BUY, Side.SELL]
        assert portfolio.position("BTC/USDT") is None

    async def test_copilot_requires_a_queue(self) -> None:
        portfolio = Portfolio(INITIAL_BALANCE)
        with pytest.raises(ValueError, match="requires a proposal queue"):
            make_engine(portfolio, autonomy_mode=AutonomyMode.COPILOT)


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

    async def test_kill_when_flat_before_any_candle_halts_without_exit(self) -> None:
        portfolio = Portfolio(INITIAL_BALANCE)
        engine = make_engine(portfolio)
        submitted = await engine.kill()
        assert submitted is False
        assert engine.paused is True

    async def test_kill_with_position_but_no_candle_raises_not_flat(self) -> None:
        """Halted-but-not-flat must never look like 'nothing to flatten'."""
        portfolio = Portfolio(INITIAL_BALANCE)
        portfolio.apply_fill(
            Fill(
                client_order_id="seed",
                symbol="BTC/USDT",
                side=Side.BUY,
                price_quote=Decimal("100"),
                quantity_base=Decimal("1"),
                fee_quote=Decimal("0"),
                filled_at=BASE_TIME,
            )
        )
        engine = make_engine(portfolio)
        with pytest.raises(RuntimeError, match="NOT flat"):
            await engine.kill()
        assert engine.paused is True  # still halted despite the error


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
