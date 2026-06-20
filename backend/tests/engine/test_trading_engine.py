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
    Order,
    OrderType,
    Side,
    Signal,
)
from tradebot.engine import TradingEngine
from tradebot.execution import FillSimulatorConfig, SimulatedExecutionAdapter
from tradebot.persistence import Database, DecisionStore, FillStore, OrderStore
from tradebot.persistence.database import metadata
from tradebot.portfolio import Portfolio, Position
from tradebot.risk import BreakerConfig, RiskConfig, RiskManager
from tradebot.signals import EntryGate, GateDecision
from tradebot.strategies import Strategy, TrendFollowingConfig, TrendFollowingStrategy

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
    entry_gates: tuple[EntryGate, ...] = (),
    order_store: OrderStore | None = None,
    strategy: Strategy | None = None,
    interval: CandleInterval = CandleInterval.M1,
) -> TradingEngine:
    strategy = strategy or TrendFollowingStrategy(
        TrendFollowingConfig(fast_ema_period=3, slow_ema_period=6, atr_period=3)
    )
    return TradingEngine(
        strategy,
        RiskManager(risk_config or RiskConfig(), portfolio),
        portfolio,
        SimulatedExecutionAdapter(FillSimulatorConfig()),
        symbol=symbol,
        interval=interval,
        fill_store=fill_store,
        decision_store=decision_store,
        autonomy_mode=autonomy_mode,
        proposal_queue=proposal_queue,
        entry_gates=entry_gates,
        order_store=order_store,
    )


class SellOnceStrategy:
    """Emits a single SELL on its first candle — drives the exit path directly.

    The trend strategy only sells when its EMAs cross, which is hard to line
    up against a specific restored order; this stub makes the strategy-exit
    moment deterministic.
    """

    def __init__(self) -> None:
        self._emitted = False

    @property
    def name(self) -> str:
        return "sell_once"

    def on_candle(self, candle: Candle, position: Position | None) -> Signal | None:
        if self._emitted:
            return None
        self._emitted = True
        return Signal(
            strategy_name="sell_once",
            symbol=candle.symbol,
            side=Side.SELL,
            confidence=1.0,
            stop_price_quote=Decimal("90"),
        )


class RecordingStrategy:
    """Records every candle it is asked to decide on; never trades."""

    def __init__(self) -> None:
        self.seen: list[Candle] = []

    @property
    def name(self) -> str:
        return "recording"

    def on_candle(self, candle: Candle, position: Position | None) -> Signal | None:
        self.seen.append(candle)
        return None


class SellEveryCandleStrategy:
    """Emits a SELL on every candle while a position is held.

    Drives the exit path repeatedly: if a partially-filled exit wrongly
    cleared the single-exit latch, the next candle's SELL would size a second
    order against the still-open position and sell it short.
    """

    @property
    def name(self) -> str:
        return "sell_every_candle"

    def on_candle(self, candle: Candle, position: Position | None) -> Signal | None:
        if position is None:
            return None
        return Signal(
            strategy_name="sell_every_candle",
            symbol=candle.symbol,
            side=Side.SELL,
            confidence=1.0,
            stop_price_quote=Decimal("90"),
            created_at=candle.close_time,
        )


class TestTradeTimeframeAggregation:
    """The engine rolls the 1m feed up to the timeframe it is told to trade.

    This is the research<->live coherence fix: research grades on hourly bars,
    so the live engine must decide on hourly bars too, assembled from the 1m
    stream it receives — never on the raw minutes.
    """

    async def test_engine_decides_on_completed_higher_timeframe_bars(self) -> None:
        portfolio = Portfolio(INITIAL_BALANCE)
        strategy = RecordingStrategy()
        engine = make_engine(portfolio, strategy=strategy, interval=CandleInterval.H1)
        bus = EventBus()
        engine.attach_to(bus)

        # Two full hours of minutes plus one minute into the third hour.
        for index in range(121):
            await bus.publish(CandleClosed(candle=make_candle(index, 100.0 + index)))

        # The strategy saw exactly the two hourly bars that closed, never a 1m.
        assert [candle.interval for candle in strategy.seen] == [
            CandleInterval.H1,
            CandleInterval.H1,
        ]
        first = strategy.seen[0]
        assert first.open_time == BASE_TIME
        # The hourly bar rolls up its minutes: high is the max of minutes 0..59.
        assert first.high_quote == make_candle(59, 159.0).high_quote

    async def test_minute_engine_is_unchanged(self) -> None:
        # interval=M1 (the default) passes every 1m candle straight through —
        # the behaviour every existing engine and backtest relies on.
        portfolio = Portfolio(INITIAL_BALANCE)
        strategy = RecordingStrategy()
        engine = make_engine(portfolio, strategy=strategy, interval=CandleInterval.M1)
        bus = EventBus()
        engine.attach_to(bus)

        for index in range(5):
            await bus.publish(CandleClosed(candle=make_candle(index, 100.0)))

        assert [candle.interval for candle in strategy.seen] == [CandleInterval.M1] * 5

    async def test_gap_replay_aggregates_and_never_runs_the_strategy(self) -> None:
        # A bar that closes during an outage reaches the adapter (resting orders
        # must meet it) but never the strategy (no signals for a market that
        # already moved on).
        portfolio = Portfolio(INITIAL_BALANCE)
        strategy = RecordingStrategy()
        engine = make_engine(portfolio, strategy=strategy, interval=CandleInterval.H1)

        for index in range(61):  # one full hour closes at minute 60
            await engine.replay_gap_candle(make_candle(index, 100.0))

        assert strategy.seen == []  # gap replay is adapter-only
        assert engine._last_candle is not None
        assert engine._last_candle.interval == CandleInterval.H1  # the hour reached the adapter

    async def test_partial_bucket_carries_from_gap_replay_into_the_live_stream(self) -> None:
        # The half-formed hour left by a gap is completed by the first live
        # minutes after reconnect — one aggregator spans both paths.
        portfolio = Portfolio(INITIAL_BALANCE)
        strategy = RecordingStrategy()
        engine = make_engine(portfolio, strategy=strategy, interval=CandleInterval.H1)
        bus = EventBus()
        engine.attach_to(bus)

        for index in range(30):  # first half hour, replayed — no bar closes yet
            await engine.replay_gap_candle(make_candle(index, 100.0))
        assert strategy.seen == []
        for index in range(30, 61):  # live resumes; minute 60 closes the 00:00 bar
            await bus.publish(CandleClosed(candle=make_candle(index, 100.0)))

        assert [candle.interval for candle in strategy.seen] == [CandleInterval.H1]
        assert strategy.seen[0].open_time == BASE_TIME

    async def test_replayed_duplicate_minutes_after_reconnect_are_skipped(self) -> None:
        # The bus re-delivers candles after a reconnect; feeding them again must
        # not corrupt the aggregator (which rejects out-of-order input).
        portfolio = Portfolio(INITIAL_BALANCE)
        strategy = RecordingStrategy()
        engine = make_engine(portfolio, strategy=strategy, interval=CandleInterval.H1)
        bus = EventBus()
        engine.attach_to(bus)

        candles = [make_candle(index, 100.0) for index in range(61)]
        for candle in candles:
            await bus.publish(CandleClosed(candle=candle))
        for candle in candles:  # reconnect replay of the same minutes
            await bus.publish(CandleClosed(candle=candle))

        # Still exactly one closed hour — the duplicates were dropped, not raised.
        assert [candle.interval for candle in strategy.seen] == [CandleInterval.H1]


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


class TestEntrySubmissionFailure:
    """A submission that raises must not leak the entry's committed notional."""

    async def test_failed_entry_submission_releases_committed_notional(self) -> None:
        """The risk manager commits an entry's notional against account
        exposure the instant it sizes the order. If submission then fails
        (venue rejection, journal write error), the commitment must be
        released — otherwise it leaks forever and every future entry across
        all symbols sizes against phantom exposure until the account starves.
        """

        class RejectingAdapter(SimulatedExecutionAdapter):
            """A venue that rejects every entry at submission time."""

            async def submit(self, order: Order) -> None:
                if order.side == Side.BUY:
                    raise RuntimeError("venue rejected the order")
                await super().submit(order)

        portfolio = Portfolio(INITIAL_BALANCE)
        risk = RiskManager(RiskConfig(), portfolio)
        engine = TradingEngine(
            TrendFollowingStrategy(
                TrendFollowingConfig(fast_ema_period=3, slow_ema_period=6, atr_period=3)
            ),
            risk,
            portfolio,
            RejectingAdapter(FillSimulatorConfig()),
            symbol="BTC/USDT",
        )

        with pytest.raises(RuntimeError, match="venue rejected"):
            for index, close in enumerate(CLOSES):
                await engine.process_candle(make_candle(index, close))

        # Nothing reached the venue and no position opened, so the account
        # must hold zero committed entry notional — not a phantom claim that
        # would throttle the next entry.
        assert risk._committed_entries_quote == {}
        assert portfolio.position("BTC/USDT") is None
        assert portfolio.quote_balance == INITIAL_BALANCE


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


async def drive_until_order_submitted(engine: TradingEngine) -> int:
    """Feed CLOSES until an order rests in the adapter; returns the next index."""
    for index, close in enumerate(CLOSES):
        await engine.process_candle(make_candle(index, close))
        if engine.open_orders():
            return index + 1
    raise AssertionError("series never produced an order")


class TestOrderJournal:
    """Every order intent is persisted open and closed out on its fate."""

    async def test_submitted_order_is_journaled_open_then_filled(self, database: Database) -> None:
        order_store = OrderStore(database)
        engine = make_engine(
            Portfolio(INITIAL_BALANCE), FillStore(database), order_store=order_store
        )

        next_index = await drive_until_order_submitted(engine)
        (open_order,) = await order_store.fetch_open("BTC/USDT")
        assert open_order.order.side == Side.BUY
        assert open_order.order.client_order_id == engine.open_orders()[0].client_order_id

        await engine.process_candle(make_candle(next_index, CLOSES[next_index]))
        assert [f.side for f in engine.fills] == [Side.BUY]
        # The entry's row closed out on its fill; the protective stop it
        # armed is now the one restorable order.
        (stop,) = await order_store.fetch_open("BTC/USDT")
        assert stop.order.client_order_id.startswith("stop-")

    async def test_restart_restores_pending_order_which_then_fills(
        self, database: Database
    ) -> None:
        """The crash window this journal exists for: submitted but unfilled."""
        order_store = OrderStore(database)
        fill_store = FillStore(database)
        first_run = make_engine(Portfolio(INITIAL_BALANCE), fill_store, order_store=order_store)
        next_index = await drive_until_order_submitted(first_run)

        # "Restart": fresh portfolio and engine, state rebuilt as the worker
        # does — replay the (empty) fill journal, then re-arm open orders.
        portfolio = Portfolio(INITIAL_BALANCE)
        for fill in await fill_store.fetch_all():
            portfolio.apply_fill(fill)
        second_run = make_engine(portfolio, fill_store, order_store=order_store)
        for open_order in await order_store.fetch_open("BTC/USDT"):
            second_run.restore_order(open_order)

        await second_run.process_candle(make_candle(next_index, CLOSES[next_index]))
        assert [f.side for f in second_run.fills] == [Side.BUY]
        assert portfolio.position("BTC/USDT") is not None
        # The restored entry armed its protective stop on filling, exactly
        # as it would have without the restart in between.
        (stop,) = await order_store.fetch_open("BTC/USDT")
        assert stop.order.client_order_id.startswith("stop-")

    async def test_restart_relatches_a_working_exit_so_strategy_sell_is_superseded(
        self, database: Database
    ) -> None:
        """A non-stop exit still working after a restart re-adopts the latch.

        Without it a fresh strategy SELL sizes against the still-open position
        and submits a second full-position sell — both fills would take the
        position short, which a spot venue cannot do.
        """
        order_store = OrderStore(database)
        decision_store = DecisionStore(database)
        # A resting limit exit priced far above the market: it stays working
        # across the candle below, standing in for an exit in flight.
        exit_order = Order(
            client_order_id="exit-1",
            signal_id="sig-exit",
            symbol="BTC/USDT",
            side=Side.SELL,
            order_type=OrderType.LIMIT,
            quantity_base=Decimal("1"),
            limit_price_quote=Decimal("200"),
            created_at=BASE_TIME,
        )
        await order_store.record_submitted(exit_order)

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
        engine = make_engine(
            portfolio,
            order_store=order_store,
            decision_store=decision_store,
            strategy=SellOnceStrategy(),
        )
        for open_order in await order_store.fetch_open("BTC/USDT"):
            engine.restore_order(open_order)

        # Candle far below the 200 limit: the restored exit cannot fill, so the
        # stub's SELL lands on top of an exit that is still in flight.
        await engine.process_candle(make_candle(0, 96.0))

        # The latch held: nothing new reached the adapter, only the restored
        # exit is still working, and the strategy SELL was superseded.
        assert engine.fills == ()
        assert [o.client_order_id for o in engine.open_orders()] == ["exit-1"]
        outcomes = {d.outcome for d in await decision_store.fetch_recent("BTC/USDT")}
        assert DecisionOutcome.SUPERSEDED in outcomes
        assert DecisionOutcome.SUBMITTED not in outcomes

    async def test_restart_does_not_latch_a_restored_protective_stop(
        self, database: Database
    ) -> None:
        """A resting protective stop must not arm the exit-in-flight latch.

        The strategy exit is meant to replace the stop (the SELL path cancels
        it first), so latching here would wrongly suppress that replacement.
        """
        order_store = OrderStore(database)
        decision_store = DecisionStore(database)
        stop = Order(
            client_order_id="stop-1",
            signal_id="sig-stop",
            symbol="BTC/USDT",
            side=Side.SELL,
            order_type=OrderType.STOP_LIMIT,
            quantity_base=Decimal("1"),
            limit_price_quote=Decimal("94"),
            stop_price_quote=Decimal("95"),
            created_at=BASE_TIME,
        )
        await order_store.record_submitted(stop)

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
        engine = make_engine(
            portfolio,
            order_store=order_store,
            decision_store=decision_store,
            strategy=SellOnceStrategy(),
        )
        for open_order in await order_store.fetch_open("BTC/USDT"):
            engine.restore_order(open_order)

        # Candle above the 95 trigger: the stop stays resting, and the stub's
        # SELL replaces it — the stop is cancelled and a market exit submitted.
        await engine.process_candle(make_candle(0, 110.0))

        outcomes = {d.outcome for d in await decision_store.fetch_recent("BTC/USDT")}
        assert DecisionOutcome.SUBMITTED in outcomes
        assert DecisionOutcome.SUPERSEDED not in outcomes
        # The stop was cancelled ahead of the exit; the new market sell is the
        # only working order.
        assert all(o.order_type != OrderType.STOP_LIMIT for o in engine.open_orders())

    async def test_kill_flattens_even_when_a_restored_exit_is_latched(
        self, database: Database
    ) -> None:
        """Kill must flatten despite a latch on an exit it cancels itself.

        A restored non-stop exit arms the exit-in-flight latch. Kill cancels
        every open order — including that exit — so the latch is stale: if kill
        still treated it as in flight it would skip its own market flatten and
        leave the position open and unprotected.
        """
        order_store = OrderStore(database)
        exit_order = Order(
            client_order_id="exit-1",
            signal_id="sig-exit",
            symbol="BTC/USDT",
            side=Side.SELL,
            order_type=OrderType.LIMIT,
            quantity_base=Decimal("1"),
            limit_price_quote=Decimal("200"),
            created_at=BASE_TIME,
        )
        await order_store.record_submitted(exit_order)

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
        engine = make_engine(portfolio, order_store=order_store)
        for open_order in await order_store.fetch_open("BTC/USDT"):
            engine.restore_order(open_order)

        # Price the exit; the 200 limit cannot fill at 96, so it is still the
        # working order (and still latched) when the kill arrives.
        await engine.process_candle(make_candle(0, 96.0))
        assert portfolio.position("BTC/USDT") is not None

        assert await engine.kill() is True
        # The kill cancelled the restored exit and submitted its own market
        # flatten; the next candle fills it and the position is closed.
        await engine.process_candle(make_candle(1, 95.0))
        assert portfolio.position("BTC/USDT") is None

    async def test_kill_journals_the_cancellation(self, database: Database) -> None:
        order_store = OrderStore(database)
        engine = make_engine(Portfolio(INITIAL_BALANCE), order_store=order_store)
        next_index = await drive_until_order_submitted(engine)
        assert await order_store.fetch_open("BTC/USDT") != []

        await engine.kill()  # flat: cancels the pending entry, halts
        assert await order_store.fetch_open("BTC/USDT") == []  # not restorable

        await engine.process_candle(make_candle(next_index, CLOSES[next_index]))
        assert engine.fills == ()  # the cancelled entry never fills

    async def test_trigger_latch_survives_restart(self, database: Database) -> None:
        """A stop that crossed must not re-arm as a stop after a restart.

        Recovery-path test: the stop-limit enters through the journal restore,
        the one production path that places resting protective orders today.
        """
        order_store = OrderStore(database)
        stop_limit = Order(
            client_order_id="ord-stop",
            signal_id="sig-stop",
            symbol="BTC/USDT",
            side=Side.SELL,
            order_type=OrderType.STOP_LIMIT,
            quantity_base=Decimal("1"),
            limit_price_quote=Decimal("94"),
            stop_price_quote=Decimal("95"),
            created_at=BASE_TIME,
        )
        await order_store.record_submitted(stop_limit)

        def position_holder() -> Portfolio:
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
            return portfolio

        first_run = make_engine(position_holder(), order_store=order_store)
        for open_order in await order_store.fetch_open("BTC/USDT"):
            first_run.restore_order(open_order)
        # Crosses the 95 stop but opens below the 94 limit: triggered, unfilled.
        await first_run.process_candle(make_candle(0, 89.0))
        assert first_run.fills == ()
        (latched,) = await order_store.fetch_open("BTC/USDT")
        assert latched.triggered is True

        second_run = make_engine(position_holder(), order_store=order_store)
        for open_order in await order_store.fetch_open("BTC/USDT"):
            second_run.restore_order(open_order)
        # Price returns through the limit without recrossing the stop
        # (low 95.5 > 95): only a restored latch can fill here.
        await second_run.process_candle(make_candle(1, 96.0))
        (fill,) = second_run.fills
        assert fill.price_quote == Decimal("94")
        assert await order_store.fetch_open("BTC/USDT") == []


class TestProtectiveStops:
    """Entry fills arm a resting stop; closing the position always disarms it."""

    async def test_entry_fill_arms_a_stop_sized_to_the_fill(self) -> None:
        portfolio = Portfolio(INITIAL_BALANCE)
        engine = make_engine(portfolio)
        next_index = await drive_until_order_submitted(engine)
        await engine.process_candle(make_candle(next_index, CLOSES[next_index]))

        (entry_fill,) = engine.fills
        (stop,) = engine.open_orders()
        assert stop.order_type == OrderType.STOP_LIMIT
        assert stop.side == Side.SELL
        assert stop.quantity_base == entry_fill.quantity_base
        assert stop.stop_price_quote is not None and stop.limit_price_quote is not None
        assert stop.limit_price_quote < stop.stop_price_quote

    async def test_stop_out_flattens_without_a_strategy_exit(self) -> None:
        portfolio = Portfolio(INITIAL_BALANCE)
        engine = make_engine(portfolio)
        next_index = await drive_until_order_submitted(engine)
        await engine.process_candle(make_candle(next_index, CLOSES[next_index]))
        (stop,) = engine.open_orders()
        assert stop.stop_price_quote is not None and stop.limit_price_quote is not None

        # One candle whose low crosses the trigger while opening above the
        # limit floor: the stop fills at its limit, no strategy involved.
        crash_close = float(stop.stop_price_quote + Decimal("0.4"))
        await engine.process_candle(make_candle(next_index + 1, crash_close))

        assert portfolio.position("BTC/USDT") is None
        stop_fill = engine.fills[-1]
        assert stop_fill.client_order_id.startswith("stop-")
        assert stop_fill.price_quote == stop.limit_price_quote
        assert engine.open_orders() == ()  # nothing left armed once flat

    async def test_position_close_never_double_sells(self) -> None:
        """Stop and strategy exit both SELL the position; only one may fill."""
        portfolio = Portfolio(INITIAL_BALANCE)
        engine = make_engine(portfolio)
        for index, close in enumerate(CLOSES):
            await engine.process_candle(make_candle(index, close))

        sells = [fill for fill in engine.fills if fill.side == Side.SELL]
        assert len(sells) == 1
        assert portfolio.position("BTC/USDT") is None
        assert engine.open_orders() == ()

    async def test_kill_disarms_the_stop_before_flattening(self) -> None:
        portfolio = Portfolio(INITIAL_BALANCE)
        engine = make_engine(portfolio)
        next_index = await drive_until_order_submitted(engine)
        await engine.process_candle(make_candle(next_index, CLOSES[next_index]))
        assert engine.open_orders() != ()  # the stop is armed

        await engine.kill()
        # Only the kill exit remains; with the stop gone it cannot double-sell.
        (exit_order,) = engine.open_orders()
        assert exit_order.order_type == OrderType.MARKET
        await engine.process_candle(make_candle(next_index + 1, CLOSES[next_index + 1]))
        assert portfolio.position("BTC/USDT") is None
        assert engine.open_orders() == ()


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


class StaticGate:
    """Test gate with a fixed verdict; records every signal it sees."""

    def __init__(self, allowed: bool) -> None:
        self.allowed = allowed
        self.seen: list[Signal] = []

    def evaluate(self, signal: Signal) -> GateDecision:
        self.seen.append(signal)
        return GateDecision(allowed=self.allowed, reasons=("regime gate: test verdict",))


class TestEntryGates:
    async def test_blocking_gate_stops_the_entry_and_journals_it(self, database: Database) -> None:
        portfolio = Portfolio(INITIAL_BALANCE)
        decision_store = DecisionStore(database)
        gate = StaticGate(allowed=False)
        engine = make_engine(portfolio, decision_store=decision_store, entry_gates=(gate,))

        for index, close in enumerate(CLOSES):
            await engine.process_candle(make_candle(index, close))

        assert engine.fills == ()  # the rally's BUY never reached the adapter
        assert gate.seen and all(signal.side == Side.BUY for signal in gate.seen)
        decisions = await decision_store.fetch_recent("BTC/USDT", 50)
        gated = [d for d in decisions if d.outcome == DecisionOutcome.GATED]
        assert gated
        # The gate's reason is journaled with the signal's own reasons, so
        # the decisions view explains the veto verbatim.
        assert any("regime gate: test verdict" in d.reasons for d in gated)

    async def test_allowing_gate_changes_nothing(self) -> None:
        gated_portfolio = Portfolio(INITIAL_BALANCE)
        engine = make_engine(gated_portfolio, entry_gates=(StaticGate(allowed=True),))
        ungated_portfolio = Portfolio(INITIAL_BALANCE)
        ungated = make_engine(ungated_portfolio)

        for index, close in enumerate(CLOSES):
            await engine.process_candle(make_candle(index, close))
            await ungated.process_candle(make_candle(index, close))

        assert engine.fills == ungated.fills  # gate that allows is invisible

    async def test_exits_are_never_gated(self) -> None:
        """A blocking gate must not stand between a position and its exit."""
        portfolio = Portfolio(INITIAL_BALANCE)
        portfolio.apply_fill(
            Fill(
                client_order_id="seed-1",
                symbol="BTC/USDT",
                side=Side.BUY,
                price_quote=Decimal("100"),
                quantity_base=Decimal("2"),
                fee_quote=Decimal("0"),
                filled_at=BASE_TIME,
            )
        )
        engine = make_engine(portfolio, entry_gates=(StaticGate(allowed=False),))

        for index, close in enumerate(CLOSES):
            await engine.process_candle(make_candle(index, close))

        assert portfolio.position("BTC/USDT") is None  # the collapse exit filled
        assert engine.fills and all(fill.side == Side.SELL for fill in engine.fills)


class TestReplaceStrategy:
    async def test_swap_changes_signals_but_not_position_state(self) -> None:
        """A promotion mid-flight replaces only the signal generator."""
        portfolio = Portfolio(INITIAL_BALANCE)
        engine = make_engine(portfolio)
        # Ride the rally into an open position with the original strategy.
        for index, close in enumerate(CLOSES[:14]):
            await engine.process_candle(make_candle(index, close))
        assert portfolio.position("BTC/USDT") is not None
        balance_before = portfolio.quote_balance

        replacement = TrendFollowingStrategy(
            TrendFollowingConfig(fast_ema_period=4, slow_ema_period=9, atr_period=4)
        )
        for index, close in enumerate(CLOSES[:14]):  # pre-warmed, as the worker does
            replacement.on_candle(make_candle(index, close), None)
        engine.replace_strategy(replacement)

        assert engine.strategy_name == "trend_following"
        assert portfolio.position("BTC/USDT") is not None  # untouched by the swap
        assert portfolio.quote_balance == balance_before
        # The collapse exits the position through the replacement strategy.
        for offset, close in enumerate(CLOSES[14:]):
            await engine.process_candle(make_candle(14 + offset, close))
        assert portfolio.position("BTC/USDT") is None


class TestProtectiveStop:
    """Paper positions now exit at their stop instead of riding through it."""

    @staticmethod
    def crash_candle(index: int) -> Candle:
        """A candle that collapses far below any plausible stop."""
        open_time = BASE_TIME + timedelta(minutes=index)
        return Candle(
            symbol="BTC/USDT",
            interval=CandleInterval.M1,
            open_time=open_time,
            close_time=open_time + timedelta(minutes=1),
            open_quote=Decimal("60"),
            high_quote=Decimal("61"),
            low_quote=Decimal("40"),
            close_quote=Decimal("41"),
            volume_base=Decimal("10"),
        )

    async def test_stop_breach_exits_without_a_strategy_signal(self) -> None:
        portfolio = Portfolio(INITIAL_BALANCE)
        engine = make_engine(portfolio)
        # Enter on the rally with the EMA-cross strategy as usual.
        for index, close in enumerate(CLOSES[:14]):
            await engine.process_candle(make_candle(index, close))
        assert portfolio.position("BTC/USDT") is not None

        # A crash through the stop: the exit must not wait for a cross-down.
        await engine.process_candle(self.crash_candle(14))  # breach -> exit order
        await engine.process_candle(self.crash_candle(15))  # exit fills here

        assert portfolio.position("BTC/USDT") is None
        assert engine.fills[-1].side == Side.SELL

    async def test_stop_fires_even_while_paused(self) -> None:
        """Pausing mutes the strategy, never capital protection."""
        portfolio = Portfolio(INITIAL_BALANCE)
        engine = make_engine(portfolio)
        for index, close in enumerate(CLOSES[:14]):
            await engine.process_candle(make_candle(index, close))
        assert portfolio.position("BTC/USDT") is not None
        engine.pause()

        await engine.process_candle(self.crash_candle(14))
        await engine.process_candle(self.crash_candle(15))

        assert portfolio.position("BTC/USDT") is None

    async def test_strategy_swap_keeps_the_armed_stop(self) -> None:
        portfolio = Portfolio(INITIAL_BALANCE)
        engine = make_engine(portfolio)
        for index, close in enumerate(CLOSES[:14]):
            await engine.process_candle(make_candle(index, close))
        assert engine.protective_stop_quote is not None

        engine.replace_strategy(
            TrendFollowingStrategy(
                TrendFollowingConfig(fast_ema_period=4, slow_ema_period=9, atr_period=4)
            )
        )

        assert engine.protective_stop_quote is not None  # promotion never disarms


class TestPartialFills:
    async def test_partial_exit_fill_does_not_double_sell_into_a_short(self) -> None:
        """A volume-capped exit fills across candles; the single-exit latch
        must stay set until it completes.

        Regression: clearing ``_pending_exit_order`` on the first *partial*
        exit fill let the next candle's strategy SELL size a second market
        order against the still-open position. Two exits against one spot
        position over-sell it short — which the portfolio rejects with a hard
        ValueError ("spot cannot go short"). The fix gates the latch clear on
        the exit order completing.
        """
        portfolio = Portfolio(INITIAL_BALANCE)
        # Seed a clean 20-unit long with no pending entry and no managed stop,
        # so only the exit path is under test.
        held = Decimal("20")
        portfolio.apply_fill(
            Fill(
                client_order_id="seed-entry",
                symbol="BTC/USDT",
                side=Side.BUY,
                quantity_base=held,
                price_quote=Decimal("100"),
                fee_quote=Decimal("0"),
                filled_at=BASE_TIME,
            )
        )
        engine = TradingEngine(
            SellEveryCandleStrategy(),
            RiskManager(RiskConfig(), portfolio),
            portfolio,
            # Candle volume 10, cap 0.6 -> at most 6 units/candle: the 20-unit
            # exit can only fill across four candles (6 + 6 + 6 + 2).
            SimulatedExecutionAdapter(FillSimulatorConfig(max_volume_fraction=Decimal("0.6"))),
            symbol="BTC/USDT",
        )

        for index in range(8):
            await engine.process_candle(make_candle(index, 100.0))

        sells = [fill for fill in engine.fills if fill.side == Side.SELL]
        total_sold = sum((fill.quantity_base for fill in sells), Decimal(0))
        assert total_sold == held  # exactly one position's worth left the book
        assert portfolio.position("BTC/USDT") is None  # flat, never short
        assert engine.open_orders() == ()  # no second exit order left working

    async def test_partial_entry_rearms_the_stop_cumulatively(self, database: Database) -> None:
        """Each partial re-protects the whole position, and the order's
        journal row stays open until the remainder fills."""
        order_store = OrderStore(database)
        portfolio = Portfolio(INITIAL_BALANCE)
        strategy = TrendFollowingStrategy(
            TrendFollowingConfig(fast_ema_period=3, slow_ema_period=6, atr_period=3)
        )
        engine = TradingEngine(
            strategy,
            RiskManager(RiskConfig(), portfolio),
            portfolio,
            SimulatedExecutionAdapter(FillSimulatorConfig(max_volume_fraction=Decimal("0.6"))),
            symbol="BTC/USDT",
            fill_store=FillStore(database),
            order_store=order_store,
        )
        next_index = await drive_until_order_submitted(engine)
        (pending,) = engine.open_orders()
        # Candle volume 10 caps each fill at 6: the sized entry (~20 units)
        # fills across candles.
        await engine.process_candle(make_candle(next_index, CLOSES[next_index]))

        first_fill = engine.fills[0]
        assert first_fill.quantity_base < pending.quantity_base  # genuinely partial
        position = portfolio.position("BTC/USDT")
        assert position is not None
        stops = [o for o in engine.open_orders() if o.client_order_id.startswith("stop-")]
        assert len(stops) == 1
        assert stops[0].quantity_base == position.quantity_base  # cumulative
        # The entry row is still restorable for exactly the remainder.
        open_rows = await order_store.fetch_open("BTC/USDT")
        entry_rows = [o for o in open_rows if o.order.client_order_id == pending.client_order_id]
        assert len(entry_rows) == 1
        assert entry_rows[0].order.quantity_base == pending.quantity_base - first_fill.quantity_base
