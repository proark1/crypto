"""Worker composition tests: end-to-end paper trading with a scripted feed."""

import asyncio
import os
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from tradebot.core.config import AppConfig, TradingMode
from tradebot.core.events import CandleClosed
from tradebot.core.models import Candle, CandleInterval, Fill, Side
from tradebot.marketdata.live_feed import OhlcvRow
from tradebot.persistence import Database, FillStore
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
