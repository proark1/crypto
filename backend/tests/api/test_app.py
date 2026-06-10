"""Control-plane tests over the ASGI app with a real database behind it."""

import os
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import httpx
import pytest

from tradebot.api import create_app
from tradebot.core.config import AppConfig, TradingMode
from tradebot.core.models import (
    Candle,
    CandleInterval,
    Decision,
    DecisionOutcome,
    Fill,
    Side,
)
from tradebot.engine import TradingEngine
from tradebot.execution import FillSimulatorConfig, SimulatedExecutionAdapter
from tradebot.persistence import CandleStore, Database, DecisionStore, FillStore
from tradebot.persistence.database import metadata
from tradebot.portfolio import Portfolio
from tradebot.risk import RiskConfig, RiskManager
from tradebot.strategies import TrendFollowingConfig, TrendFollowingStrategy

BASE_TIME = datetime(2026, 1, 2, 0, 0, tzinfo=UTC)
TOKEN = "test-token"
DEFAULT_URL = "postgresql+asyncpg://postgres:test@localhost:5432/tradebot_test"


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


class StubBot:
    """Minimal BotState for the app factory."""

    def __init__(self, database: Database) -> None:
        self.config = AppConfig(mode=TradingMode.PAPER, symbol="BTC/USDT")
        self.portfolio = Portfolio(Decimal("10000"))
        self.candle_store = CandleStore(database)
        self.fill_store = FillStore(database)
        self.decision_store = DecisionStore(database)
        self.engine = TradingEngine(
            TrendFollowingStrategy(TrendFollowingConfig()),
            RiskManager(RiskConfig(), self.portfolio),
            self.portfolio,
            SimulatedExecutionAdapter(FillSimulatorConfig()),
            symbol="BTC/USDT",
        )


def make_fill(price: str = "100", quantity: str = "2") -> Fill:
    return Fill(
        client_order_id="ord-1",
        symbol="BTC/USDT",
        side=Side.BUY,
        price_quote=Decimal(price),
        quantity_base=Decimal(quantity),
        fee_quote=Decimal("0.2"),
        filled_at=BASE_TIME,
    )


def make_candle(close: str) -> Candle:
    return Candle(
        symbol="BTC/USDT",
        interval=CandleInterval.M1,
        open_time=BASE_TIME,
        close_time=BASE_TIME + timedelta(minutes=1),
        open_quote=Decimal("100"),
        high_quote=Decimal("111"),
        low_quote=Decimal("99"),
        close_quote=Decimal(close),
        volume_base=Decimal("1"),
    )


def make_client(bot: StubBot) -> httpx.AsyncClient:
    app = create_app(bot, TOKEN)
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://control",
        headers={"Authorization": f"Bearer {TOKEN}"},
    )


class TestAuth:
    async def test_requests_without_token_are_rejected(self, database: Database) -> None:
        app = create_app(StubBot(database), TOKEN)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://control"
        ) as client:
            response = await client.get("/health")
        assert response.status_code == 401

    async def test_wrong_token_is_rejected(self, database: Database) -> None:
        app = create_app(StubBot(database), TOKEN)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://control",
            headers={"Authorization": "Bearer wrong"},
        ) as client:
            response = await client.get("/status")
        assert response.status_code == 401

    async def test_empty_token_refuses_to_build(self, database: Database) -> None:
        with pytest.raises(ValueError, match="non-empty token"):
            create_app(StubBot(database), "")


class TestStatus:
    async def test_flat_account_status(self, database: Database) -> None:
        bot = StubBot(database)
        async with make_client(bot) as client:
            response = await client.get("/status")

        assert response.status_code == 200
        body = response.json()
        assert body["mode"] == "paper"
        assert body["quote_balance"] == "10000"
        assert body["position"] is None
        assert body["equity_quote"] == "10000"
        assert body["mark_price_quote"] is None

    async def test_open_position_is_marked_to_latest_candle(self, database: Database) -> None:
        bot = StubBot(database)
        bot.portfolio.apply_fill(make_fill(price="100", quantity="2"))
        await bot.candle_store.insert_batch([make_candle(close="110")])

        async with make_client(bot) as client:
            body = (await client.get("/status")).json()

        assert body["position"]["quantity_base"] == "2"
        assert body["position"]["average_entry_price_quote"] == "100.1"  # fee capitalized
        assert body["position"]["unrealized_pnl_quote"] == "19.8"  # 220 - 200.2
        assert body["mark_price_quote"] == "110"
        assert body["equity_quote"] == "10019.8"

    async def test_open_position_without_mark_price_refuses_equity(
        self, database: Database
    ) -> None:
        bot = StubBot(database)
        bot.portfolio.apply_fill(make_fill())

        async with make_client(bot) as client:
            body = (await client.get("/status")).json()

        assert body["equity_quote"] is None  # refuses to guess, never wrong
        assert body["position"]["unrealized_pnl_quote"] is None


class TestCommands:
    async def test_pause_and_resume_toggle_status(self, database: Database) -> None:
        bot = StubBot(database)
        async with make_client(bot) as client:
            paused = (await client.post("/pause")).json()
            status_paused = (await client.get("/status")).json()
            resumed = (await client.post("/resume")).json()
            status_resumed = (await client.get("/status")).json()

        assert paused["paused"] is True
        assert status_paused["paused"] is True
        assert resumed["paused"] is False
        assert status_resumed["paused"] is False

    async def test_kill_when_flat_halts_only(self, database: Database) -> None:
        bot = StubBot(database)
        async with make_client(bot) as client:
            body = (await client.post("/kill")).json()

        assert body["paused"] is True
        assert "no position" in body["detail"]
        assert bot.engine.paused is True

    async def test_kill_with_position_but_no_candle_returns_conflict(
        self, database: Database
    ) -> None:
        bot = StubBot(database)
        bot.portfolio.apply_fill(make_fill(price="100", quantity="2"))
        # The engine has processed no candle: kill cannot price an exit.
        async with make_client(bot) as client:
            response = await client.post("/kill")

        assert response.status_code == 409
        assert "NOT flat" in response.json()["detail"]
        assert bot.engine.paused is True

    async def test_kill_with_position_submits_exit(self, database: Database) -> None:
        bot = StubBot(database)
        bot.portfolio.apply_fill(make_fill(price="100", quantity="2"))
        await bot.engine.process_candle(make_candle(close="105"))  # engine sees a price

        async with make_client(bot) as client:
            body = (await client.post("/kill")).json()

        assert "exit order submitted" in body["detail"]
        # The exit fills on the next candle even though the engine is paused.
        next_candle = make_candle(close="106").model_copy(
            update={
                "open_time": BASE_TIME + timedelta(minutes=1),
                "close_time": BASE_TIME + timedelta(minutes=2),
            }
        )
        await bot.engine.process_candle(next_candle)
        assert bot.portfolio.position("BTC/USDT") is None


class TestDecisions:
    async def test_decisions_are_returned_newest_first_with_reasons(
        self, database: Database
    ) -> None:
        bot = StubBot(database)
        await bot.decision_store.append(
            Decision(
                signal_id="sig-1",
                strategy_name="trend_following",
                symbol="BTC/USDT",
                side=Side.BUY,
                stop_price_quote=Decimal("95"),
                reasons=("fast EMA crossed above slow EMA",),
                outcome=DecisionOutcome.VETOED,
                created_at=BASE_TIME,
            )
        )

        async with make_client(bot) as client:
            body = (await client.get("/decisions")).json()

        assert len(body) == 1
        assert body[0]["outcome"] == "vetoed"
        assert body[0]["reasons"] == ["fast EMA crossed above slow EMA"]
        assert body[0]["stop_price_quote"] == "95"


class TestFills:
    async def test_journal_is_returned_with_string_amounts(self, database: Database) -> None:
        bot = StubBot(database)
        await bot.fill_store.append(make_fill())

        async with make_client(bot) as client:
            body = (await client.get("/fills")).json()

        assert len(body) == 1
        assert body[0]["price_quote"] == "100"
        assert body[0]["side"] == "buy"
        assert body[0]["filled_at"] == BASE_TIME.isoformat()
