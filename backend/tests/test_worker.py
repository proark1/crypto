"""Worker composition tests: end-to-end paper trading with a scripted feed."""

import asyncio
import contextlib
import os
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import httpx
import pytest

from tradebot.core.config import AppConfig, TradingMode
from tradebot.core.events import CandleClosed
from tradebot.core.models import (
    Candle,
    CandleInterval,
    Fill,
    Order,
    OrderType,
    ProtectiveExitPlan,
    Side,
)
from tradebot.marketdata.live_feed import OhlcvRow
from tradebot.persistence import Database, FillStore, OrderStore
from tradebot.persistence.database import metadata
from tradebot.worker import Worker

BASE_TIME = datetime(2026, 1, 2, 0, 0, tzinfo=UTC)
BASE_MS = int(BASE_TIME.timestamp() * 1000)
MINUTE_MS = 60_000
DEFAULT_URL = "postgresql+asyncpg://postgres:test@localhost:5432/tradebot_test"

# Long enough for the default 20/50 EMA config: drift down through warm-up
# (fast ends below slow), rally (cross up -> buy), collapse (cross down -> sell).
CLOSES = (
    [100.0 - 0.2 * i for i in range(60)]
    + [88.0 + 2.0 * i for i in range(1, 31)]
    + [148.0 - 3.0 * i for i in range(1, 31)]
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


def make_config(
    symbols: str = "BTC/USDT", api_port: int = 8901, regime_gate_enabled: bool = False
) -> AppConfig:
    # Distinct ports per test: each worker.run() binds an HTTP server, and
    # a just-closed listener can linger long enough to collide.
    # The regime gate defaults off here (production default is on): the flow
    # tests exercise trading mechanics with minutes of scripted data, which
    # the warming-up gate would rightly block. Gate wiring has its own tests.
    return AppConfig(
        mode=TradingMode.PAPER,
        symbols=symbols,
        exchange_id="binance",
        paper_initial_balance_quote=Decimal("10000"),
        api_port=api_port,
        regime_gate_enabled=regime_gate_enabled,
        # Never poll real sentiment APIs from tests; the gate is exercised
        # through the regime and news paths, which are fully scripted.
        sentiment_enabled=False,
    )


class ScriptedExchange:
    """Feeds one candle per watch call; stops the worker when exhausted."""

    def __init__(self, closes: list[float]) -> None:
        self._rows: list[OhlcvRow] = [
            [BASE_MS + i * MINUTE_MS, close, close + 0.5, close - 0.5, close, 10.0]
            for i, close in enumerate(closes)
        ]
        self._cursor = 0
        self.worker: Worker | None = None

    async def watch_ohlcv(self, symbol: str, timeframe: str) -> list[OhlcvRow]:
        if self._cursor >= len(self._rows):
            assert self.worker is not None
            self.worker.stop()
            return []
        # Send the previous (now closed) and current (in progress) rows.
        start = max(0, self._cursor - 1)
        snapshot = self._rows[start : self._cursor + 1]
        self._cursor += 1
        return snapshot

    async def fetch_ohlcv(
        self, symbol: str, timeframe: str, since: int | None = None, limit: int | None = None
    ) -> list[OhlcvRow]:
        return []

    async def load_markets(self) -> dict[str, object]:
        return {"BTC/USDT": {}, "ETH/USDT": {}}


class MultiSymbolScriptedExchange:
    """Per-symbol scripted candles; stops the worker once all are exhausted."""

    def __init__(self, closes_by_symbol: dict[str, list[float]]) -> None:
        self._rows: dict[str, list[OhlcvRow]] = {
            symbol: [
                [BASE_MS + i * MINUTE_MS, close, close + 0.5, close - 0.5, close, 10.0]
                for i, close in enumerate(closes)
            ]
            for symbol, closes in closes_by_symbol.items()
        }
        self._cursors: dict[str, int] = dict.fromkeys(closes_by_symbol, 0)
        self.worker: Worker | None = None

    async def watch_ohlcv(self, symbol: str, timeframe: str) -> list[OhlcvRow]:
        rows = self._rows[symbol]
        cursor = self._cursors[symbol]
        if cursor >= len(rows):
            if all(self._cursors[s] >= len(self._rows[s]) for s in self._rows):
                assert self.worker is not None
                self.worker.stop()
            # Yield so the other symbol's feed keeps making progress.
            await asyncio.sleep(0.001)
            return []
        snapshot = rows[max(0, cursor - 1) : cursor + 1]
        self._cursors[symbol] = cursor + 1
        return snapshot

    async def fetch_ohlcv(
        self, symbol: str, timeframe: str, since: int | None = None, limit: int | None = None
    ) -> list[OhlcvRow]:
        return []

    async def load_markets(self) -> dict[str, object]:
        return {symbol: {} for symbol in self._rows}


class TestWorker:
    async def test_paper_trades_end_to_end(self, database: Database) -> None:
        exchange = ScriptedExchange(CLOSES)
        worker = Worker(make_config(), database, exchange)
        exchange.worker = worker

        await worker.run()

        journal = await worker.fill_store.fetch_all("BTC/USDT")
        assert [f.side for f in journal] == [Side.BUY, Side.SELL]
        assert worker.portfolio.position("BTC/USDT") is None
        assert (
            worker.portfolio.equity_quote({})
            == Decimal("10000") + worker.portfolio.realized_pnl_quote()
        )

    async def test_two_symbols_trade_one_shared_account(self, database: Database) -> None:
        """Both feeds drive their own engine; books and breakers are shared."""
        exchange = MultiSymbolScriptedExchange(
            {"BTC/USDT": list(CLOSES), "ETH/USDT": [c / 10 for c in CLOSES]}
        )
        worker = Worker(make_config("BTC/USDT,ETH/USDT", api_port=8902), database, exchange)
        exchange.worker = worker

        await worker.run()

        for symbol in ("BTC/USDT", "ETH/USDT"):
            journal = await worker.fill_store.fetch_all(symbol)
            assert [f.side for f in journal] == [Side.BUY, Side.SELL], symbol
            assert worker.portfolio.position(symbol) is None
        # The equity identity holds across the whole account.
        assert (
            worker.portfolio.equity_quote({})
            == Decimal("10000") + worker.portfolio.realized_pnl_quote()
        )
        # One shared risk manager: both engines expose the same breakers.
        engines = list(worker.engines.values())
        assert engines[0].breakers is engines[1].breakers

    async def test_restart_replays_journal_into_portfolio(self, database: Database) -> None:
        await database.create_schema()
        store = FillStore(database)
        await store.append(
            Fill(
                client_order_id="ord-1",
                symbol="BTC/USDT",
                side=Side.BUY,
                price_quote=Decimal("100"),
                quantity_base=Decimal("2"),
                fee_quote=Decimal("0.2"),
                filled_at=BASE_TIME,
            )
        )

        worker = Worker(make_config(), database, ScriptedExchange([]))
        replayed = await worker.replay_journal()

        assert replayed == 1
        position = worker.portfolio.position("BTC/USDT")
        assert position is not None
        assert position.quantity_base == Decimal("2")
        assert worker.portfolio.quote_balance == Decimal("9799.8")

    async def test_restart_restores_open_orders_into_their_engines(
        self, database: Database
    ) -> None:
        """Submitted-but-unfilled orders must survive a deploy restart."""

        def make_open_order(order_id: str, symbol: str) -> Order:
            return Order(
                client_order_id=order_id,
                signal_id=f"sig-{order_id}",
                symbol=symbol,
                side=Side.BUY,
                order_type=OrderType.MARKET,
                quantity_base=Decimal("1"),
                created_at=BASE_TIME,
            )

        store = OrderStore(database)
        await store.record_submitted(make_open_order("ord-btc", "BTC/USDT"))
        # An orphan: its coin is not traded, so no engine can host it.
        await store.record_submitted(make_open_order("ord-doge", "DOGE/USDT"))

        worker = Worker(make_config(), database, ScriptedExchange([]))
        await worker.initialize()

        (restored,) = worker.engines["BTC/USDT"].open_orders()
        assert restored.client_order_id == "ord-btc"
        # The orphan is warned about and left journaled open, not dropped.
        journaled = {o.order.client_order_id for o in await store.fetch_open()}
        assert journaled == {"ord-btc", "ord-doge"}

    async def test_boot_rearms_a_stop_lost_in_the_crash_window(self, database: Database) -> None:
        """Entry fill journaled, process died before the stop was placed."""
        order_store = OrderStore(database)
        entry = Order(
            client_order_id="ord-entry",
            signal_id="sig-entry",
            symbol="BTC/USDT",
            side=Side.BUY,
            order_type=OrderType.MARKET,
            quantity_base=Decimal("2"),
            protective_exit=ProtectiveExitPlan(
                stop_price_quote=Decimal("95"), limit_price_quote=Decimal("94.525")
            ),
            created_at=BASE_TIME,
        )
        await order_store.record_submitted(entry)
        await order_store.mark_filled("ord-entry", BASE_TIME)
        await FillStore(database).append(
            Fill(
                client_order_id="ord-entry",
                symbol="BTC/USDT",
                side=Side.BUY,
                price_quote=Decimal("100"),
                quantity_base=Decimal("2"),
                fee_quote=Decimal("0"),
                filled_at=BASE_TIME,
            )
        )
        # Deliberately no stop row: that is the crash window.

        worker = Worker(make_config(), database, ScriptedExchange([]))
        await worker.initialize()

        (stop,) = worker.engines["BTC/USDT"].open_orders()
        assert stop.client_order_id == "stop-ord-entry"
        assert stop.quantity_base == Decimal("2")  # the whole position
        assert stop.stop_price_quote == Decimal("95")
        # Journaled too, so the next restart restores it the normal way.
        journaled = await order_store.fetch_open("BTC/USDT")
        assert [o.order.client_order_id for o in journaled] == ["stop-ord-entry"]

    async def test_removal_survives_restart_despite_env_seed(self, database: Database) -> None:
        """The env var seeds once; a removed coin must stay removed."""
        first_boot = Worker(make_config("BTC/USDT,ETH/USDT"), database, ScriptedExchange([]))
        await first_boot.initialize()
        assert first_boot.symbols == ("BTC/USDT", "ETH/USDT")

        await first_boot.remove_coin("ETH/USDT")
        # Annotated: mypy otherwise carries the two-element narrowing from
        # the assert above across the removal call.
        symbols_after_removal: tuple[str, ...] = first_boot.symbols
        assert symbols_after_removal == ("BTC/USDT",)

        # Same env config on restart: ETH must not resurrect.
        second_boot = Worker(make_config("BTC/USDT,ETH/USDT"), database, ScriptedExchange([]))
        await second_boot.initialize()
        assert second_boot.symbols == ("BTC/USDT",)

    async def test_add_coin_validates_and_persists(self, database: Database) -> None:
        worker = Worker(make_config("BTC/USDT"), database, ScriptedExchange([]))
        await worker.initialize()

        await worker.add_coin("ETH/USDT")
        assert worker.symbols == ("BTC/USDT", "ETH/USDT")

        with pytest.raises(ValueError, match="already being traded"):
            await worker.add_coin("ETH/USDT")
        with pytest.raises(ValueError, match="not quoted in"):
            await worker.add_coin("BTC/EUR")
        with pytest.raises(ValueError, match="not listed"):
            await worker.add_coin("DOGE/USDT")  # absent from the market catalog

        # Persisted: a restart picks the added coin up from the database.
        restarted = Worker(make_config("BTC/USDT"), database, ScriptedExchange([]))
        await restarted.initialize()
        assert restarted.symbols == ("BTC/USDT", "ETH/USDT")

    async def test_remove_coin_refuses_unsafe_states(self, database: Database) -> None:
        worker = Worker(make_config("BTC/USDT,ETH/USDT"), database, ScriptedExchange([]))
        await worker.initialize()
        worker.portfolio.apply_fill(
            Fill(
                client_order_id="open",
                symbol="ETH/USDT",
                side=Side.BUY,
                price_quote=Decimal("10"),
                quantity_base=Decimal("1"),
                fee_quote=Decimal("0"),
                filled_at=BASE_TIME,
            )
        )

        with pytest.raises(RuntimeError, match="open position"):
            await worker.remove_coin("ETH/USDT")
        with pytest.raises(KeyError, match="not being traded"):
            await worker.remove_coin("DOGE/USDT")

        await worker.remove_coin("BTC/USDT")  # flat: allowed
        with pytest.raises(RuntimeError, match="last coin"):
            await worker.remove_coin("ETH/USDT")

    async def test_removed_engine_is_detached_from_the_bus(self, database: Database) -> None:
        """A removed coin's engine must never see another candle."""
        worker = Worker(make_config("BTC/USDT,ETH/USDT"), database, ScriptedExchange([]))
        await worker.initialize()
        removed_engine = worker.engines["ETH/USDT"]
        await worker.remove_coin("ETH/USDT")

        candle = Candle(
            symbol="ETH/USDT",
            interval=CandleInterval.M1,
            open_time=BASE_TIME,
            close_time=BASE_TIME + timedelta(minutes=1),
            open_quote=Decimal("10"),
            high_quote=Decimal("11"),
            low_quote=Decimal("9"),
            close_quote=Decimal("10"),
            volume_base=Decimal("1"),
        )
        await worker.bus.publish(CandleClosed(candle=candle))
        # The detached engine saw nothing: a kill on it still reports no
        # market data rather than pricing an exit off the published candle.
        assert removed_engine.fills == ()

    async def test_regime_gate_blocks_entries_while_warming_up(self, database: Database) -> None:
        """Default-on gate, minutes of data: the rally's BUY is journaled as gated."""
        exchange = ScriptedExchange(CLOSES)
        worker = Worker(make_config(api_port=8903, regime_gate_enabled=True), database, exchange)
        exchange.worker = worker

        await worker.run()

        assert worker.regime_detector is not None
        assert worker.regime_detector.regime.label == "warming_up"  # 2 hourly buckets max
        journal = await worker.fill_store.fetch_all("BTC/USDT")
        assert journal == []  # the entry never reached the adapter
        decisions = await worker.decision_store.fetch_recent("BTC/USDT", 50)
        assert any(decision.outcome.value == "gated" for decision in decisions)

    async def test_regime_gate_disables_itself_without_its_reference_feed(
        self, database: Database
    ) -> None:
        """No reference data means no gate — loudly, instead of blocking forever."""
        worker = Worker(
            make_config("ETH/USDT", regime_gate_enabled=True),
            database,
            ScriptedExchange([]),
        )
        await worker.initialize()
        assert worker.regime_detector is None  # BTC/USDT is not among the coins

    async def test_reference_symbol_cannot_be_removed_while_gated(self, database: Database) -> None:
        worker = Worker(
            make_config("BTC/USDT,ETH/USDT", regime_gate_enabled=True),
            database,
            ScriptedExchange([]),
        )
        await worker.initialize()

        with pytest.raises(RuntimeError, match="reference market"):
            await worker.remove_coin("BTC/USDT")
        await worker.remove_coin("ETH/USDT")  # only the reference is protected

    async def test_scheduled_event_window_blocks_entries_end_to_end(
        self, database: Database
    ) -> None:
        """A calendar window over the scripted run journals the BUY as gated."""
        exchange = ScriptedExchange(CLOSES)
        calendar_json = '[{"name": "FOMC", "time": "2026-01-02T01:00:00Z", "window_minutes": 120}]'
        config = make_config(api_port=8904).model_copy(
            update={"event_calendar_json": calendar_json}
        )
        worker = Worker(config, database, exchange)
        exchange.worker = worker

        await worker.run()

        journal = await worker.fill_store.fetch_all("BTC/USDT")
        assert journal == []  # the rally's entry fell inside the FOMC window
        decisions = await worker.decision_store.fetch_recent("BTC/USDT", 50)
        gated = [d for d in decisions if d.outcome.value == "gated"]
        assert gated
        assert any("FOMC window" in reason for d in gated for reason in d.reasons)

    async def test_news_flag_blocks_only_the_flagged_coin(self, database: Database) -> None:
        exchange = MultiSymbolScriptedExchange(
            {"BTC/USDT": list(CLOSES), "ETH/USDT": [c / 10 for c in CLOSES]}
        )
        worker = Worker(make_config("BTC/USDT,ETH/USDT", api_port=8905), database, exchange)
        exchange.worker = worker
        from tradebot.news import NewsItem, classify

        item = NewsItem(
            external_id="1",
            source="test",
            title="Exchange will delist ETH pairs",
            currencies=("ETH",),
            published_at=BASE_TIME,
        )
        worker.news_flags.flag("ETH", classify(item), BASE_TIME)

        await worker.run()

        btc_journal = await worker.fill_store.fetch_all("BTC/USDT")
        eth_journal = await worker.fill_store.fetch_all("ETH/USDT")
        assert [f.side for f in btc_journal] == [Side.BUY, Side.SELL]  # unaffected
        assert eth_journal == []  # flagged coin never entered
        decisions = await worker.decision_store.fetch_recent("ETH/USDT", 50)
        assert any(
            "delisting flag on ETH" in reason
            for d in decisions
            if d.outcome.value == "gated"
            for reason in d.reasons
        )

    async def test_non_paper_modes_are_refused(self, database: Database) -> None:
        live_config = AppConfig(mode=TradingMode.LIVE)
        with pytest.raises(NotImplementedError, match="paper mode"):
            Worker(live_config, database, ScriptedExchange([]))

        backtest_config = AppConfig(mode=TradingMode.BACKTEST)
        with pytest.raises(NotImplementedError, match="paper mode"):
            Worker(backtest_config, database, ScriptedExchange([]))


class HistoryServingExchange(ScriptedExchange):
    """Serves enough recent 1m history to warm the regime gate on first boot."""

    def __init__(self, minutes: int) -> None:
        super().__init__([])
        # Anchored to the wall clock: the first-boot backfill reaches back
        # from now, so the history must be recent to be fetched.
        end = datetime.now(UTC).replace(second=0, microsecond=0)
        start_ms = int((end - timedelta(minutes=minutes)).timestamp() * 1000)
        self.history: list[OhlcvRow] = [
            # A steady climb: hourly ADX reads it as a clean trend.
            [start_ms + i * MINUTE_MS, close, close + 0.5, close - 0.5, close, 10.0]
            for i in range(minutes)
            for close in (100 + 0.01 * i,)
        ]
        self.fetch_calls = 0

    async def fetch_ohlcv(
        self, symbol: str, timeframe: str, since: int | None = None, limit: int | None = None
    ) -> list[OhlcvRow]:
        self.fetch_calls += 1
        if symbol != "BTC/USDT":
            return []
        if since is None:
            return self.history
        return [row for row in self.history if row[0] >= since]


class FailingBackfillExchange(ScriptedExchange):
    async def fetch_ohlcv(
        self, symbol: str, timeframe: str, since: int | None = None, limit: int | None = None
    ) -> list[OhlcvRow]:
        raise ConnectionError("venue unreachable")


class TestFirstBootRegimePriming:
    async def test_first_boot_backfills_the_reference_and_primes_the_gate(
        self, database: Database
    ) -> None:
        """A fresh deploy must not block entries for days of live warm-up."""
        worker = Worker(
            make_config(regime_gate_enabled=True),
            database,
            HistoryServingExchange(minutes=14_500),  # > required_m1_candles()
        )
        await worker.initialize()

        assert worker.regime_detector is not None
        assert worker.regime_detector.regime.label == "trending"

    async def test_failed_reference_backfill_degrades_to_live_warm_up(
        self, database: Database
    ) -> None:
        """A venue outage at boot defers warm-up; it never crashes startup."""
        worker = Worker(
            make_config(regime_gate_enabled=True), database, FailingBackfillExchange([])
        )
        await worker.initialize()

        assert worker.regime_detector is not None
        assert worker.regime_detector.regime.label == "warming_up"


class TestHealthDuringFirstBackfill:
    async def test_health_answers_while_the_deep_backfill_is_still_running(
        self, database: Database
    ) -> None:
        """The platform healthcheck must not time a first deploy out."""

        class NeverFinishingBackfill(ScriptedExchange):
            async def fetch_ohlcv(
                self,
                symbol: str,
                timeframe: str,
                since: int | None = None,
                limit: int | None = None,
            ) -> list[OhlcvRow]:
                await asyncio.sleep(3600)
                return []

        worker = Worker(
            make_config(api_port=8906, regime_gate_enabled=True),
            database,
            NeverFinishingBackfill([]),
        )
        run_task = asyncio.create_task(worker.run())
        try:
            async with httpx.AsyncClient() as client:
                response = None
                for _ in range(200):  # the server needs a moment to bind
                    try:
                        response = await client.get("http://127.0.0.1:8906/health")
                        break
                    except httpx.TransportError:
                        await asyncio.sleep(0.05)
                assert response is not None and response.status_code == 200
                assert response.json() == {"status": "ok"}
        finally:
            run_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await run_task


class TestStrategyPromotion:
    async def test_apply_strategy_params_persists_and_hot_swaps_engines(
        self, database: Database
    ) -> None:
        worker = Worker(make_config("BTC/USDT,ETH/USDT"), database, ScriptedExchange([]))
        await worker.initialize()
        before = {symbol: engine._strategy for symbol, engine in worker.engines.items()}

        version = await worker.apply_strategy_params(
            "trend_following",
            {"fast_ema_period": 10, "slow_ema_period": 30},
            source_sweep_id=7,
            note="auto-promoted: it validated",
        )

        assert version > 0
        assert worker.strategy_params["trend_following"]["fast_ema_period"] == 10
        for symbol, engine in worker.engines.items():
            assert engine._strategy is not before[symbol], symbol  # fresh instance
        active = await worker.strategy_settings_store.active()
        assert active["trend_following"]["slow_ema_period"] == 30

    async def test_promoted_params_survive_a_restart(self, database: Database) -> None:
        first_boot = Worker(make_config(), database, ScriptedExchange([]))
        await first_boot.initialize()
        await first_boot.apply_strategy_params("trend_following", {"fast_ema_period": 12})

        second_boot = Worker(make_config(), database, ScriptedExchange([]))
        await second_boot.initialize()

        assert second_boot.strategy_params["trend_following"]["fast_ema_period"] == 12

    async def test_revert_reapplies_the_old_version_as_a_new_one(self, database: Database) -> None:
        worker = Worker(make_config(), database, ScriptedExchange([]))
        await worker.initialize()
        original = await worker.apply_strategy_params("trend_following", {"fast_ema_period": 10})
        await worker.apply_strategy_params("trend_following", {"fast_ema_period": 15})

        new_version = await worker.revert_strategy_version(original)

        assert new_version > original
        assert worker.strategy_params["trend_following"]["fast_ema_period"] == 10
        history = await worker.strategy_settings_store.history()
        assert f"revert to version #{original}" in (history[0]["note"] or "")

    async def test_unknown_family_or_params_are_refused(self, database: Database) -> None:
        worker = Worker(make_config(), database, ScriptedExchange([]))
        await worker.initialize()

        with pytest.raises(ValueError, match="unknown strategy family"):
            await worker.apply_strategy_params("momentum", {})
        with pytest.raises(ValueError, match="unknown trend_following parameters"):
            await worker.apply_strategy_params("trend_following", {"fast_ema_perod": 1})
        with pytest.raises(KeyError, match="no strategy settings version"):
            await worker.revert_strategy_version(9999)


class TestRiskStatePersistence:
    async def test_trip_and_pause_survive_restart(self, database: Database) -> None:
        """A deploy must not release a tripped breaker or resume a killed bot."""
        from tradebot.persistence import RiskStateStore
        from tradebot.risk import BreakerState

        await RiskStateStore(database).save(
            BreakerState(
                tripped_reason="drawdown limit",
                entries_today=4,
                peak_equity_quote=Decimal("11000"),
                last_observed_time=BASE_TIME,
            ),
            ["BTC/USDT"],
            BASE_TIME,
        )

        worker = Worker(make_config(), database, ScriptedExchange([]))
        await worker.initialize()

        assert worker.risk_manager.breakers.tripped_reason == "drawdown limit"
        assert worker.risk_manager.breakers.entries_today == 4
        assert worker.engines["BTC/USDT"].paused is True

    async def test_breaker_trip_is_persisted_during_the_run(self, database: Database) -> None:
        """The bus-driven persister writes the trip within a candle."""
        exchange = ScriptedExchange(CLOSES[:5])
        config = make_config(api_port=8907)
        worker = Worker(config, database, exchange)
        # Trip through the public surface before candles flow: a >3% drop
        # against the default daily-loss limit latches the hard trip.
        worker.risk_manager.breakers.observe(BASE_TIME, Decimal("10000"))
        worker.risk_manager.breakers.observe(BASE_TIME, Decimal("9000"))
        assert worker.risk_manager.breakers.tripped_reason is not None
        exchange.worker = worker
        await worker.run()

        from tradebot.persistence import RiskStateStore

        loaded = await RiskStateStore(database).load()
        assert loaded is not None
        assert loaded[0].tripped_reason is not None  # the trip reached Postgres


class TestPromotionConfirmation:
    async def test_gate_vetoes_without_history_and_passes_a_matching_challenger(
        self, database: Database
    ) -> None:
        """Fail-safe both ways: no data means no promotion; a challenger
        replaying at least as well as the incumbent passes."""
        from tradebot.core.models import Candle

        worker = Worker(make_config(), database, ScriptedExchange([]))
        await worker.initialize()

        params = {"fast_ema_period": 3, "slow_ema_period": 6, "atr_period": 3}
        veto = await worker._confirm_promotion("trend_following", params, "BTC/USDT")
        assert veto is not None and "no stored" in veto

        candles = [
            Candle(
                symbol="BTC/USDT",
                interval=CandleInterval.M1,
                open_time=BASE_TIME + timedelta(minutes=i),
                close_time=BASE_TIME + timedelta(minutes=i + 1),
                open_quote=Decimal(str(close)),
                high_quote=Decimal(str(close + 0.5)),
                low_quote=Decimal(str(close - 0.5)),
                close_quote=Decimal(str(close)),
                volume_base=Decimal("10"),
            )
            for i, close in enumerate([100.0] * 6 + [100.0 + 2 * i for i in range(1, 11)])
        ]
        await worker.candle_store.insert_batch(candles)
        # Challenger == incumbent defaults for the family: identical replay,
        # equal equity, so the gate must allow.
        worker.strategy_params["trend_following"] = dict(params)
        assert await worker._confirm_promotion("trend_following", params, "BTC/USDT") is None


class TestVenueFilterMapping:
    def test_ccxt_market_translates_int_and_fraction_precision(self) -> None:
        from tradebot.worker import filters_from_market

        filters = filters_from_market(
            {
                "precision": {"amount": 3, "price": 0.01},
                "limits": {"amount": {"min": 0.001}, "cost": {"min": 5.0}},
            }
        )
        assert filters.quantity_step_base == Decimal("0.001")  # 3 decimals
        assert filters.price_tick_quote == Decimal("0.01")  # fraction = tick
        assert filters.min_quantity_base == Decimal("0.001")
        assert filters.min_notional_quote == Decimal("5.0")

    def test_sparse_or_malformed_catalogs_degrade_to_unconstrained(self) -> None:
        from tradebot.worker import filters_from_market

        for market in ({}, {"precision": None, "limits": None}, {"precision": {"price": "junk"}}):
            filters = filters_from_market(market)
            assert filters.quantity_step_base == 0
            assert filters.price_tick_quote == 0
            assert filters.entry_block_reason(Decimal("1"), Decimal("1")) is None

    async def test_initialize_fills_filters_from_the_catalog(self, database: Database) -> None:
        class CatalogExchange(ScriptedExchange):
            async def load_markets(self) -> dict[str, object]:
                return {
                    "BTC/USDT": {
                        "precision": {"amount": 0.0001, "price": 0.01},
                        "limits": {"amount": {"min": 0.0001}, "cost": {"min": 5}},
                    }
                }

        worker = Worker(make_config(), database, CatalogExchange([]))
        await worker.initialize()

        filters = worker.symbol_filters["BTC/USDT"]
        assert filters.min_notional_quote == Decimal("5")
        # The risk manager reads the same live dict the worker fills.
        assert worker.risk_manager._filters_by_symbol is worker.symbol_filters


class TestGapReplay:
    async def test_restored_order_fills_on_the_downtime_candles(self, database: Database) -> None:
        """An order pending through an outage meets the candles it missed,
        not the post-restart market."""
        order_store = OrderStore(database)
        await order_store.record_submitted(
            Order(
                client_order_id="ord-gap",
                signal_id="sig-gap",
                symbol="BTC/USDT",
                side=Side.BUY,
                order_type=OrderType.MARKET,
                quantity_base=Decimal("1"),
                protective_exit=ProtectiveExitPlan(
                    stop_price_quote=Decimal("95"), limit_price_quote=Decimal("94.5")
                ),
                created_at=BASE_TIME,
            )
        )
        # The downtime candles, already repaired into Postgres (the scripted
        # exchange's backfill is a no-op, so seeding the store stands in for
        # a successful repair).
        from tradebot.persistence import CandleStore

        gap_candles = [
            Candle(
                symbol="BTC/USDT",
                interval=CandleInterval.M1,
                open_time=BASE_TIME + timedelta(minutes=i),
                close_time=BASE_TIME + timedelta(minutes=i + 1),
                open_quote=Decimal(str(100 + i)),
                high_quote=Decimal(str(100.5 + i)),
                low_quote=Decimal(str(99.5 + i)),
                close_quote=Decimal(str(100 + i)),
                volume_base=Decimal("10"),
            )
            for i in range(1, 4)
        ]
        await CandleStore(database).insert_batch(gap_candles)

        worker = Worker(make_config(), database, ScriptedExchange([]))
        await worker.initialize()

        (fill,) = await worker.fill_store.fetch_all("BTC/USDT")
        # Filled on the first downtime candle's open (plus 5bps slippage),
        # not at some later live price.
        assert fill.filled_at == BASE_TIME + timedelta(minutes=1)
        assert fill.price_quote == Decimal("101") * (1 + Decimal("0.0005"))
        position = worker.portfolio.position("BTC/USDT")
        assert position is not None and position.quantity_base == Decimal("1")
        # The entry's protective stop was armed during the replay and is the
        # one restorable order left.
        (stop,) = worker.engines["BTC/USDT"].open_orders()
        assert stop.client_order_id == "stop-ord-gap"
        assert await order_store.fetch_open("BTC/USDT") != []
