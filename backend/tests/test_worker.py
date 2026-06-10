"""Worker composition tests: end-to-end paper trading with a scripted feed."""

import os
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from tradebot.core.config import AppConfig, TradingMode
from tradebot.core.models import Fill, Side
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


def make_config() -> AppConfig:
    return AppConfig(
        mode=TradingMode.PAPER,
        symbol="BTC/USDT",
        exchange_id="binance",
        paper_initial_balance_quote=Decimal("10000"),
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

    async def test_non_paper_modes_are_refused(self, database: Database) -> None:
        live_config = AppConfig(mode=TradingMode.LIVE)
        with pytest.raises(NotImplementedError, match="paper mode"):
            Worker(live_config, database, ScriptedExchange([]))

        backtest_config = AppConfig(mode=TradingMode.BACKTEST)
        with pytest.raises(NotImplementedError, match="paper mode"):
            Worker(backtest_config, database, ScriptedExchange([]))
