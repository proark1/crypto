"""Worker composition tests: end-to-end paper trading with a scripted feed."""

import asyncio
import contextlib
import os
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import httpx
import pytest

from tradebot.competition import PRODUCTION_BOT_ID, CompetitorSpec, validate_rules
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
from tradebot.evaluation.runner import EvaluationRunConfig
from tradebot.marketdata.live_feed import OhlcvRow
from tradebot.persistence import Database, FillStore, OrderStore
from tradebot.persistence.database import metadata
from tradebot.worker import PRODUCTION_FAMILIES, Worker, _campaign_snapshot

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


def test_campaign_snapshot_serializes_a_status() -> None:
    """The control-API serializer: None passes through, a status flattens."""
    from tradebot.evaluation.campaign import CampaignConfig, CampaignRound, CampaignStatus

    assert _campaign_snapshot(None) is None

    status = CampaignStatus(
        config=CampaignConfig(target="momentum", symbol="BTC/USDT", max_rounds=1),
        status="completed",
        promotions=1,
        stop_reason="budget spent: reached the 1-round limit",
        holdout_read={"improved": True},
    )
    status.rounds.append(
        CampaignRound(0, 1.0, 7, "validated", "faster_macd", 3, "promoted momentum settings v3")
    )

    snapshot = _campaign_snapshot(status)
    assert snapshot is not None
    assert snapshot["target"] == "momentum"
    assert snapshot["status"] == "completed"
    assert snapshot["promotions"] == 1
    assert snapshot["holdout_read"] == {"improved": True}
    assert snapshot["rounds"][0]["winner"] == "faster_macd"
    assert snapshot["rounds"][0]["promoted_version"] == 3


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
    symbols: str = "BTC/USDT",
    api_port: int = 8901,
    regime_gate_enabled: bool = False,
    competition_enabled: bool = True,
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
        competition_enabled=competition_enabled,
        # These flow tests script trading by the minute, so the worker trades
        # by the minute too: the engine's 1m->trade_timeframe rollup is covered
        # in the engine suite, not re-proved through every worker scenario.
        # (Kept equal across live and research to satisfy the coherence check.)
        trade_timeframe="1m",
        auto_improve_timeframe="1m",
        campaign_timeframe="1m",
        # Never poll real sentiment APIs from tests; the gate is exercised
        # through the regime and news paths, which are fully scripted.
        sentiment_enabled=False,
        # Funding-history backfill has its own tests; the flow doubles don't
        # serve funding, so keep it off here (production defaults it on).
        funding_history_enabled=False,
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


class BackfillFailingExchange(ScriptedExchange):
    """Streams candles, but every REST backfill fails — a degraded feed."""

    async def fetch_ohlcv(
        self, symbol: str, timeframe: str, since: int | None = None, limit: int | None = None
    ) -> list[OhlcvRow]:
        raise ConnectionError("backfill unavailable")


class RecentHistoryExchange(ScriptedExchange):
    """Serves a recent 1m REST history for one symbol so a strategy can prime."""

    def __init__(self, symbol: str, closes: list[float]) -> None:
        super().__init__([])
        self._symbol = symbol
        # Anchored to the wall clock: prime_history reaches back from now, so
        # the history must be recent to be fetched at all.
        end = datetime.now(UTC).replace(second=0, microsecond=0)
        start_ms = int((end - timedelta(minutes=len(closes))).timestamp() * 1000)
        self.history: list[OhlcvRow] = [
            [start_ms + i * MINUTE_MS, close, close + 0.5, close - 0.5, close, 10.0]
            for i, close in enumerate(closes)
        ]

    async def fetch_ohlcv(
        self, symbol: str, timeframe: str, since: int | None = None, limit: int | None = None
    ) -> list[OhlcvRow]:
        if symbol != self._symbol:
            return []
        return self.history if since is None else [r for r in self.history if r[0] >= since]


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

    async def test_trading_fees_persist_and_reload_across_restart(self, database: Database) -> None:
        await database.create_schema()
        worker = Worker(make_config(), database, ScriptedExchange([]))
        # Boot defaults: 10 bps a side, shared by every engine's simulator.
        assert worker.trading_fees() == {
            "buy_fee_bps": Decimal("10"),
            "sell_fee_bps": Decimal("10"),
        }

        await worker.update_trading_fees(buy_fee_bps=Decimal("20"), sell_fee_bps=Decimal("30"))
        assert worker.fee_schedule.fee_bps_for(Side.BUY) == Decimal("20")
        assert worker.fee_schedule.fee_bps_for(Side.SELL) == Decimal("30")

        # A fresh process starts from config defaults, then initialize() loads
        # the operator's persisted fees over them.
        restarted = Worker(make_config(), database, ScriptedExchange([]))
        assert restarted.fee_schedule.buy_fee_bps == Decimal("10")
        await restarted.initialize()
        assert restarted.fee_schedule.buy_fee_bps == Decimal("20")
        assert restarted.fee_schedule.sell_fee_bps == Decimal("30")

    async def test_campaign_toggle_persists_and_reloads_across_restart(
        self, database: Database
    ) -> None:
        await database.create_schema()
        worker = Worker(make_config(), database, ScriptedExchange([]))
        assert worker.campaign_status()["enabled"] is False  # boot default: off

        await worker.update_campaign_enabled(enabled=True)
        assert worker.campaign_status()["enabled"] is True

        # A fresh process starts from the config default, then initialize()
        # loads the operator's persisted toggle over it.
        restarted = Worker(make_config(), database, ScriptedExchange([]))
        assert restarted.campaign_status()["enabled"] is False
        await restarted.initialize()
        assert restarted.campaign_status()["enabled"] is True

    async def test_reset_bot_capital_purges_journal_and_reloads_balance(
        self, database: Database
    ) -> None:
        await database.create_schema()
        worker = Worker(make_config(), database, ScriptedExchange([]))
        # A prior trade in the production journal — the reset must discard it.
        await worker.fill_store.append(
            Fill(
                client_order_id="ord-old",
                symbol="BTC/USDT",
                side=Side.BUY,
                price_quote=Decimal("100"),
                quantity_base=Decimal("1"),
                fee_quote=Decimal("0.1"),
                filled_at=BASE_TIME,
            )
        )

        await worker.reset_bot_capital(PRODUCTION_BOT_ID, Decimal("5000"))

        assert worker.portfolio.quote_balance == Decimal("5000")
        assert await worker.fill_store.fetch_all() == []  # journal purged

        # The new capital survives a restart: initialize() applies it before
        # replaying the (now empty) journal.
        restarted = Worker(make_config(), database, ScriptedExchange([]))
        assert restarted.portfolio.quote_balance == Decimal("10000")  # config default pre-load
        await restarted.initialize()
        assert restarted.portfolio.quote_balance == Decimal("5000")

    async def test_reset_bot_capital_refuses_while_holding_a_position(
        self, database: Database
    ) -> None:
        await database.create_schema()
        worker = Worker(make_config(), database, ScriptedExchange([]))
        worker.portfolio.apply_fill(
            Fill(
                client_order_id="ord-open",
                symbol="BTC/USDT",
                side=Side.BUY,
                price_quote=Decimal("100"),
                quantity_base=Decimal("1"),
                fee_quote=Decimal("0.1"),
                filled_at=BASE_TIME,
            )
        )
        with pytest.raises(RuntimeError, match="holds a position"):
            await worker.reset_bot_capital(PRODUCTION_BOT_ID, Decimal("5000"))

    async def test_reset_bot_capital_rejects_negative_or_zero(self, database: Database) -> None:
        await database.create_schema()
        worker = Worker(make_config(), database, ScriptedExchange([]))
        with pytest.raises(ValueError, match="greater than zero"):
            await worker.reset_bot_capital(PRODUCTION_BOT_ID, Decimal("-1"))
        with pytest.raises(ValueError, match="greater than zero"):
            await worker.reset_bot_capital(PRODUCTION_BOT_ID, Decimal("0"))

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

    async def test_add_coin_primes_strategies_from_recent_history(self, database: Database) -> None:
        """A coin added mid-run trades on warm indicators, not a cold start.

        Only a recent decline is served (every indicator warm, fast EMA below
        slow). A handful of rallying candles then crosses up and records a BUY
        decision — a cold strategy needs ~50 candles before any EMA is even
        defined, so the recorded decision proves the priming ran. The fetch is
        bounded: the store holds only the recent window, never a deep crawl.
        """
        new_symbol = "ETH/USDT"
        decline = [100.0 - 0.2 * i for i in range(60)]
        exchange = RecentHistoryExchange(new_symbol, decline)
        worker = Worker(make_config("BTC/USDT"), database, exchange)
        await worker.initialize()

        await worker.add_coin(new_symbol)

        stored = await worker.candle_store.fetch_recent(new_symbol, CandleInterval.M1, 1000)
        assert len(stored) == len(decline) - 1  # newest row dropped as in progress

        # Drive the rally through the now-warm strategy; the feed is still
        # unhealthy (not streaming), so the BUY is gated but still journaled.
        last_open = stored[-1].open_time
        engine = worker.engines[new_symbol]
        for index, close in enumerate(range(90, 150, 2), start=1):
            open_time = last_open + timedelta(minutes=index)
            price = Decimal(str(close))
            await engine.process_candle(
                Candle(
                    symbol=new_symbol,
                    interval=CandleInterval.M1,
                    open_time=open_time,
                    close_time=open_time + timedelta(minutes=1),
                    open_quote=price,
                    high_quote=price + Decimal("0.5"),
                    low_quote=price - Decimal("0.5"),
                    close_quote=price,
                    volume_base=Decimal("10"),
                )
            )

        decisions = await worker.decision_store.fetch_recent(new_symbol, 10)
        assert any(decision.side == Side.BUY for decision in decisions)

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

    async def test_degraded_feed_blocks_entries_end_to_end(self, database: Database) -> None:
        """A feed whose backfill never succeeds gates every entry, journaled.

        The same scripted rally that fills BUY then SELL in
        ``test_paper_trades_end_to_end`` produces no fills here: the
        data-health gate (wired ahead of the regime/news gates in the
        production chain) blocks the entry while the feed is degraded.
        """
        exchange = BackfillFailingExchange(CLOSES)
        worker = Worker(make_config(api_port=8913), database, exchange)
        exchange.worker = worker

        await worker.run()

        assert await worker.fill_store.fetch_all("BTC/USDT") == []
        decisions = await worker.decision_store.fetch_recent("BTC/USDT", 50)
        gated = [d for d in decisions if d.outcome.value == "gated"]
        assert gated
        assert any("data health" in reason for d in gated for reason in d.reasons)

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
        # The surprising "ungated" state is recorded for /status, not silent.
        assert worker.regime_disabled_reason is not None
        assert "BTC/USDT" in worker.regime_disabled_reason

    async def test_sentiment_thresholds_flow_from_env_config(self, database: Database) -> None:
        """The operator's tuned thresholds must reach the gate, not the defaults."""
        config = make_config(regime_gate_enabled=True).model_copy(
            update={
                "sentiment_enabled": True,  # construction only; polling needs run()
                "sentiment_extreme_fear_at_or_below": 10,
                "sentiment_extreme_greed_at_or_above": 85,
            }
        )
        worker = Worker(config, database, ScriptedExchange([]))

        assert worker.sentiment is not None
        assert worker.sentiment.config.extreme_fear_at_or_below == 10
        assert worker.sentiment.config.extreme_greed_at_or_above == 85

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
            await worker.apply_strategy_params("martingale", {})
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


class TestDivergenceReport:
    async def test_live_paper_run_replays_to_zero_divergence(self, database: Database) -> None:
        """The one-code-path invariant, measured: a live paper run and a
        backtest over the same stored candles must produce identical fills."""
        exchange = ScriptedExchange(CLOSES)
        worker = Worker(make_config(api_port=8908), database, exchange)
        exchange.worker = worker
        await worker.run()
        live_fills = await worker.fill_store.fetch_all("BTC/USDT")
        assert len(live_fills) == 2  # the scripted round trip happened

        # A pinned window over the scripted 2026-01-02 candles: reproducible
        # regardless of when the test runs.
        report = await worker.divergence_report(
            "BTC/USDT", window_hours=24, window_end=BASE_TIME + timedelta(days=1)
        )

        assert report.live_fill_count == 2
        assert report.replay_fill_count == 2
        assert report.divergence_fraction == 0.0
        assert report.mismatches == ()

    async def test_unknown_symbol_raises(self, database: Database) -> None:
        worker = Worker(make_config(), database, ScriptedExchange([]))
        await worker.initialize()
        with pytest.raises(KeyError, match="DOGE/USDT"):
            await worker.divergence_report("DOGE/USDT")


class TestResearchFamilyPromotions:
    async def test_breakout_promotion_tunes_its_account_and_says_so(
        self, database: Database
    ) -> None:
        """Tuning is not routing: the journal row carries the scope."""
        worker = Worker(make_config(), database, ScriptedExchange([]))
        await worker.initialize()

        version = await worker.apply_strategy_params(
            "breakout", {"channel_period": 30}, source_sweep_id=4, note="auto-promoted: it won"
        )

        assert worker.strategy_params["breakout"] == {"channel_period": 30}
        row = await worker.strategy_settings_store.fetch(version)
        assert row is not None
        assert "unrouted in production" in row["note"]
        assert "auto-promoted: it won" in row["note"]
        # The breakout competition account now trades the promoted params;
        # the production router's families are untouched.
        assert "breakout" not in PRODUCTION_FAMILIES
        assert worker.strategy_params.get("trend_following") is None

    async def test_unknown_family_still_fails_loudly(self, database: Database) -> None:
        worker = Worker(make_config(), database, ScriptedExchange([]))
        await worker.initialize()
        with pytest.raises(ValueError, match="unknown strategy family"):
            await worker.apply_strategy_params("nonsense", {})


class TestEvaluationStrategySelection:
    """Runs grade a named bot: the lineup always, custom bots while they compete."""

    @staticmethod
    def add_custom_bot(worker: Worker) -> str:
        """Register a recipe runtime directly (no engines — research only)."""
        spec = CompetitorSpec(
            bot_id="my-recipe",
            label="My recipe",
            family=None,
            risk_state_row_id=100,
            description="confluence test bot",
        )
        worker.challengers["my-recipe"] = worker._new_runtime(
            spec, rules=validate_rules({"families": {"trend_following": {}}})
        )
        return "my-recipe"

    async def test_the_lineup_and_custom_bots_are_offered(self, database: Database) -> None:
        worker = Worker(make_config(), database, ScriptedExchange([]))
        bot_id = self.add_custom_bot(worker)

        rows = worker.evaluation_strategies()

        by_id = {row["id"]: row for row in rows}
        assert rows[0]["id"] == PRODUCTION_BOT_ID  # the default leads the list
        assert by_id["breakout"]["kind"] == "builtin"
        assert by_id[bot_id]["kind"] == "custom"
        assert by_id[bot_id]["label"] == "My recipe"
        # Built-in challengers appear once (from the lineup), never twice.
        assert len(rows) == len(by_id)

    async def test_custom_bots_get_a_recipe_evaluator(self, database: Database) -> None:
        worker = Worker(make_config(), database, ScriptedExchange([]))
        bot_id = self.add_custom_bot(worker)

        worker._scenario_evaluator_for(bot_id)  # builds without raising
        with pytest.raises(ValueError, match="unknown competitor"):
            worker._scenario_evaluator_for("nope")

    async def test_start_evaluation_rejects_an_unknown_bot_before_any_row(
        self, database: Database
    ) -> None:
        worker = Worker(make_config(), database, ScriptedExchange([]))

        with pytest.raises(ValueError, match="unknown strategy"):
            await worker.start_evaluation(
                EvaluationRunConfig(symbols=("BTC/USDT",), strategy="nope")
            )
        assert await worker.evaluation_store.list_runs() == []


class TestCompetition:
    async def test_challengers_trade_isolated_accounts_on_the_same_candles(
        self, database: Database
    ) -> None:
        exchange = ScriptedExchange(CLOSES)
        worker = Worker(make_config(api_port=8920), database, exchange)
        exchange.worker = worker

        await worker.run()

        assert set(worker.challengers) == {
            "trend_following",
            "mean_reversion",
            "breakout",
            "momentum",
            "squeeze",
            "funding",
        }
        # The trend challenger saw the same crossover production (bare trend
        # with the gate off) traded — from its own account, in its own journal.
        challenger_fills = await FillStore(database, bot_id="trend_following").fetch_all("BTC/USDT")
        assert [fill.side for fill in challenger_fills] == [Side.BUY, Side.SELL]
        production_fills = await worker.fill_store.fetch_all("BTC/USDT")
        assert [fill.side for fill in production_fills] == [Side.BUY, Side.SELL]
        # Same family, same candles — but namespaced ids: nothing collides
        # in the shared journal tables.
        challenger_ids = {fill.client_order_id for fill in challenger_fills}
        assert challenger_ids.isdisjoint(fill.client_order_id for fill in production_fills)
        assert all(order_id.startswith("ord-trend_following/") for order_id in challenger_ids)
        # Each account's books close on their own equity identity.
        for runtime in worker.challengers.values():
            portfolio = runtime.portfolio
            assert portfolio.position("BTC/USDT") is None or runtime.spec.bot_id != (
                "trend_following"
            )
        trend_portfolio = worker.challengers["trend_following"].portfolio
        assert (
            trend_portfolio.equity_quote({})
            == Decimal("10000") + trend_portfolio.realized_pnl_quote()
        )

    async def test_restart_replays_every_account_from_its_own_journal(
        self, database: Database
    ) -> None:
        exchange = ScriptedExchange(CLOSES)
        first = Worker(make_config(api_port=8921), database, exchange)
        exchange.worker = first
        await first.run()
        traded = first.challengers["trend_following"].portfolio

        restarted = Worker(make_config(api_port=8922), database, ScriptedExchange([]))
        await restarted.initialize()

        replayed = restarted.challengers["trend_following"].portfolio
        assert replayed.quote_balance == traded.quote_balance
        assert replayed.realized_pnl_quote() == traded.realized_pnl_quote()
        assert restarted.portfolio.quote_balance == first.portfolio.quote_balance

    async def test_competition_snapshot_ranks_all_seven_accounts(self, database: Database) -> None:
        exchange = ScriptedExchange(CLOSES)
        worker = Worker(make_config(api_port=8923), database, exchange)
        exchange.worker = worker
        await worker.run()

        rows = await worker.competition_snapshot()

        assert len(rows) == 7
        assert sum(1 for row in rows if row["is_production"]) == 1
        equities = [row["equity_quote"] for row in rows]
        assert all(equity is not None for equity in equities)
        assert equities == sorted(equities, reverse=True)
        by_bot = {row["bot_id"]: row for row in rows}
        assert by_bot["trend_following"]["entry_fills"] == 1
        assert by_bot["trend_following"]["exit_fills"] == 1

    async def test_competition_can_be_disabled(self, database: Database) -> None:
        worker = Worker(
            make_config(api_port=8924, competition_enabled=False),
            database,
            ScriptedExchange([]),
        )
        await worker.initialize()

        assert worker.challengers == {}
        rows = await worker.competition_snapshot()
        assert [row["bot_id"] for row in rows] == ["production"]


class TestBotManagement:
    async def test_challengers_trade_ungated_by_the_regime_router(self, database: Database) -> None:
        """Solo bots must not inherit the router's family schedule.

        With the regime gate on but warming up (minutes of scripted data
        against a ~10-day warm-up), production entries are gated — and the
        trend challenger must trade anyway: gating a solo bot by regime
        would make the competition compare gate schedules, not strategies.
        """
        exchange = ScriptedExchange(CLOSES)
        worker = Worker(make_config(api_port=8925, regime_gate_enabled=True), database, exchange)
        exchange.worker = worker

        await worker.run()

        challenger_fills = await FillStore(database, bot_id="trend_following").fetch_all()
        assert [fill.side for fill in challenger_fills] == [Side.BUY, Side.SELL]
        assert await worker.fill_store.fetch_all() == []  # production: gated, honestly

    async def test_custom_bot_lifecycle(self, database: Database) -> None:
        worker = Worker(make_config(api_port=8926), database, ScriptedExchange([]))
        await worker.initialize()

        bot_id = await worker.create_custom_bot(
            "Dip Buyer", "", {"families": {"mean_reversion": {"rsi_period": 7}}}
        )
        assert bot_id == "custom-dip-buyer"
        assert "BTC/USDT" in worker.challengers[bot_id].engines
        rows = await worker.competition_snapshot()
        assert any(row["bot_id"] == bot_id and row["kind"] == "custom" for row in rows)
        detail = await worker.bot_detail(bot_id)
        assert detail["strategy"]["kind"] == "custom"
        assert detail["strategy"]["rules"]["families"]["mean_reversion"]["rsi_period"] == 7

        # A restart reloads the bot from Postgres, rules included.
        restarted = Worker(make_config(api_port=8927), database, ScriptedExchange([]))
        await restarted.initialize()
        assert bot_id in restarted.challengers
        assert restarted.challengers[bot_id].rules is not None

        await restarted.pause_bot(bot_id)
        assert all(engine.paused for engine in restarted.challengers[bot_id].engines.values())
        snapshot = await restarted.competition_snapshot()
        paused_row = next(row for row in snapshot if row["bot_id"] == bot_id)
        assert paused_row["paused"] is True
        await restarted.resume_bot(bot_id)

        await restarted.delete_custom_bot(bot_id)
        assert bot_id not in restarted.challengers
        rebooted = Worker(make_config(api_port=8928), database, ScriptedExchange([]))
        await rebooted.initialize()
        assert bot_id not in rebooted.challengers  # retired bots stay retired

    async def test_builtins_cannot_be_edited_or_deleted(self, database: Database) -> None:
        worker = Worker(make_config(api_port=8929), database, ScriptedExchange([]))
        await worker.initialize()

        with pytest.raises(ValueError, match="built-in"):
            await worker.delete_custom_bot("momentum")
        with pytest.raises(ValueError, match="built-in"):
            await worker.update_custom_bot("momentum", {"families": {"momentum": {}}})
        with pytest.raises(KeyError):
            await worker.pause_bot("custom-ghost")


class CrashOnceApiExchange(ScriptedExchange):
    """Idle feed that stops the worker the moment the safety pause lands.

    The control API is replaced with a task that crashes at startup; this feed
    simply spins until every production engine is paused, then stops the run so
    the test asserts on a settled state (with a tick ceiling so a regression
    that never pauses fails fast instead of hanging).
    """

    def __init__(self) -> None:
        super().__init__([])
        self._ticks = 0

    async def watch_ohlcv(self, symbol: str, timeframe: str) -> list[OhlcvRow]:
        assert self.worker is not None
        self._ticks += 1
        engines = self.worker.engines
        if (engines and all(engine.paused for engine in engines.values())) or self._ticks > 500:
            self.worker.stop()
            return []
        await asyncio.sleep(0.005)
        return []


class TestSafetyPause:
    """A crashed control plane mutes new entries (flatten-safe), never stops."""

    async def test_pause_for_failed_service_mutes_production_not_challengers(
        self, database: Database
    ) -> None:
        """Production entries halt; autonomous paper challengers keep running."""
        worker = Worker(make_config(api_port=8931), database, ScriptedExchange([]))
        await worker.initialize()

        await worker._pause_for_failed_service("control_api")

        assert worker.engines  # production is actually trading
        assert all(engine.paused for engine in worker.engines.values())
        assert worker.challengers  # the competition exists to be left alone
        for runtime in worker.challengers.values():
            assert all(not engine.paused for engine in runtime.engines.values())

    async def test_crashed_control_api_trips_a_flatten_safe_pause(
        self, database: Database, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A control-API crash during run() pauses the production bot, not the worker."""
        exchange = CrashOnceApiExchange()
        worker = Worker(make_config(api_port=8932), database, exchange)
        exchange.worker = worker

        async def boom() -> None:
            raise RuntimeError("control api crashed")

        # Keep the real supervision wiring; only the served coroutine crashes.
        monkeypatch.setattr(
            worker, "_start_api", lambda: worker._supervise_control_api(asyncio.create_task(boom()))
        )

        await worker.run()

        assert worker._safety_pause_reason == "control_api"
        assert worker.engines["BTC/USDT"].paused is True

    async def test_control_api_clean_exit_also_trips_the_pause(
        self, database: Database, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A control-API task that returns on its own is a dead plane too."""
        exchange = CrashOnceApiExchange()
        worker = Worker(make_config(api_port=8933), database, exchange)
        exchange.worker = worker

        async def clean_exit() -> None:
            return  # the server returned without being cancelled — unexpected

        monkeypatch.setattr(
            worker,
            "_start_api",
            lambda: worker._supervise_control_api(asyncio.create_task(clean_exit())),
        )

        await worker.run()

        assert worker._safety_pause_reason == "control_api"
        assert worker.engines["BTC/USDT"].paused is True


class WedgedFeedExchange(ScriptedExchange):
    """A feed that parks in watch_ohlcv forever — never returns between ticks.

    Models the production hazard the shutdown supervisor exists for: real
    ``watch_ohlcv`` blocks on the websocket, so ``stop()`` (which only flips a
    flag checked between ticks) cannot end the feed on its own.
    """

    def __init__(self) -> None:
        super().__init__([])
        self.parked = asyncio.Event()

    async def watch_ohlcv(self, symbol: str, timeframe: str) -> list[OhlcvRow]:
        self.parked.set()
        await asyncio.Event().wait()  # never returns; only cancellation ends it
        return []


class TestShutdown:
    """Shutdown must never hang on a feed wedged in a blocking watch."""

    async def test_shutdown_cancels_a_feed_wedged_in_watch(self, database: Database) -> None:
        """stop() ends a parked feed via the grace-then-cancel supervisor."""
        exchange = WedgedFeedExchange()
        worker = Worker(make_config(api_port=8941), database, exchange)
        worker._feed_shutdown_grace_seconds = 0.05
        run_task = asyncio.create_task(worker.run())
        try:
            await asyncio.wait_for(exchange.parked.wait(), timeout=5)
            worker.stop()
            # Without the supervisor's cancel this await would hang forever.
            await asyncio.wait_for(run_task, timeout=5)
        finally:
            run_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await run_task

    async def test_shutdown_supervisor_stops_feeds_set_directly(self, database: Database) -> None:
        """A stop latched by any path (not just Worker.stop) still ends feeds.

        Setting the event directly skips the ``feed.stop()`` that ``stop()``
        would call, so only the supervisor's own ``feed.stop()`` plus cancel
        can end the parked feed — the defense-in-depth this guards.
        """
        exchange = WedgedFeedExchange()
        worker = Worker(make_config(api_port=8942), database, exchange)
        worker._feed_shutdown_grace_seconds = 0.05
        run_task = asyncio.create_task(worker.run())
        try:
            await asyncio.wait_for(exchange.parked.wait(), timeout=5)
            worker._stop_requested.set()
            await asyncio.wait_for(run_task, timeout=5)
        finally:
            run_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await run_task

    async def test_add_coin_during_shutdown_does_not_spawn_a_hanging_feed(
        self, database: Database
    ) -> None:
        """An add_coin landing in the grace window must not start a new feed.

        The control API is alive through the grace window, so a coin added
        mid-shutdown could otherwise spawn a feed the supervisor has already
        passed over and hang the TaskGroup. Nulling the group on stop closes
        that window: the coin is still built, but no streaming task starts.
        """
        exchange = WedgedFeedExchange()
        worker = Worker(make_config(api_port=8943), database, exchange)
        worker._feed_shutdown_grace_seconds = 0.1
        run_task = asyncio.create_task(worker.run())
        try:
            await asyncio.wait_for(exchange.parked.wait(), timeout=5)
            worker._stop_requested.set()
            # Let the supervisor observe the stop and close the add-feed window.
            for _ in range(100):
                await asyncio.sleep(0)
                if worker._task_group is None:
                    break
            assert worker._task_group is None
            await worker.add_coin("ETH/USDT")
            assert "ETH/USDT" not in worker._feed_tasks
            await asyncio.wait_for(run_task, timeout=5)
        finally:
            run_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await run_task
