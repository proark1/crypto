"""Control-plane tests over the ASGI app with a real database behind it."""

import os
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import httpx
import pytest

from tradebot.api import create_app, create_health_only_app
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
            response = await client.get("/status")
        assert response.status_code == 401

    async def test_health_is_public_for_platform_healthchecks(self, database: Database) -> None:
        app = create_app(StubBot(database), TOKEN)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://control"
        ) as client:
            response = await client.get("/health")  # no Authorization header
        assert response.status_code == 200
        # Minimal by design: no mode, no symbol, no balances — nothing for
        # an unauthenticated scanner to learn beyond "something is alive".
        assert response.json() == {"status": "ok"}

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

    async def test_health_only_app_serves_nothing_but_health(self) -> None:
        """Without a token the deploy healthcheck still works — and only that."""
        app = create_health_only_app()
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://control"
        ) as client:
            assert (await client.get("/health")).json() == {"status": "ok"}
            assert (await client.get("/status")).status_code == 404
            assert (await client.post("/kill")).status_code == 404


class TestCors:
    """The dashboard lives on a different origin than the API; without CORS
    headers the browser blocks every dashboard request before it is sent."""

    async def test_cross_origin_request_gets_allow_origin_header(self, database: Database) -> None:
        bot = StubBot(database)  # default config: api_cors_origins="*"
        async with make_client(bot) as client:
            response = await client.get(
                "/status", headers={"Origin": "https://dashboard.example.com"}
            )

        assert response.status_code == 200
        assert response.headers["access-control-allow-origin"] == "*"

    async def test_preflight_allows_authorization_header(self, database: Database) -> None:
        """The token rides in an Authorization header, which makes every
        dashboard request "non-simple" — the browser preflights it."""
        bot = StubBot(database)
        async with make_client(bot) as client:
            response = await client.options(
                "/proposals/approve",
                headers={
                    "Origin": "https://dashboard.example.com",
                    "Access-Control-Request-Method": "POST",
                    "Access-Control-Request-Headers": "authorization,content-type",
                },
            )

        assert response.status_code == 200
        assert "authorization" in response.headers["access-control-allow-headers"].lower()
        assert "POST" in response.headers["access-control-allow-methods"]

    async def test_configured_origin_list_is_enforced(self, database: Database) -> None:
        bot = StubBot(database)
        bot.config = AppConfig(
            mode=TradingMode.PAPER,
            symbol="BTC/USDT",
            # Trailing slash on purpose: pasted from a browser address bar.
            api_cors_origins="https://dash.example.com/, https://other.example.com",
        )
        async with make_client(bot) as client:
            allowed = await client.get("/status", headers={"Origin": "https://dash.example.com"})
            denied = await client.get("/status", headers={"Origin": "https://evil.example.com"})

        assert allowed.headers["access-control-allow-origin"] == "https://dash.example.com"
        assert "access-control-allow-origin" not in denied.headers


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


class TestBreakers:
    async def test_status_reports_untripped_breakers(self, database: Database) -> None:
        bot = StubBot(database)
        async with make_client(bot) as client:
            body = (await client.get("/status")).json()

        assert body["breakers"]["tripped_reason"] is None
        assert body["breakers"]["cooldown_until"] is None
        assert body["breakers"]["entries_today"] == 0

    async def test_reset_clears_a_tripped_breaker(self, database: Database) -> None:
        bot = StubBot(database)
        # Trip the daily-loss breaker directly: 10000 -> 9000 is past -3%.
        bot.engine.breakers.observe(BASE_TIME, Decimal("10000"))
        bot.engine.breakers.observe(BASE_TIME + timedelta(minutes=1), Decimal("9000"))

        async with make_client(bot) as client:
            tripped = (await client.get("/status")).json()
            response = await client.post("/breakers/reset")
            cleared = (await client.get("/status")).json()

        assert "daily loss" in tripped["breakers"]["tripped_reason"]
        assert response.status_code == 200
        assert response.json()["detail"] == "circuit breakers reset"
        assert cleared["breakers"]["tripped_reason"] is None


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

    async def test_out_of_range_limit_is_rejected(self, database: Database) -> None:
        bot = StubBot(database)
        async with make_client(bot) as client:
            assert (await client.get("/decisions?limit=0")).status_code == 422
            assert (await client.get("/decisions?limit=9999")).status_code == 422


class TestCandles:
    async def test_candles_are_chronological_with_string_amounts(self, database: Database) -> None:
        bot = StubBot(database)
        await bot.candle_store.insert_batch([make_candle(close="110")])

        async with make_client(bot) as client:
            body = (await client.get("/candles")).json()

        assert len(body) == 1
        assert body[0]["close_quote"] == "110"
        assert body[0]["open_time"] == BASE_TIME.isoformat()

    async def test_candles_limit_is_validated(self, database: Database) -> None:
        bot = StubBot(database)
        async with make_client(bot) as client:
            assert (await client.get("/candles?limit=0")).status_code == 422
            assert (await client.get("/candles?limit=99999")).status_code == 422


class TestProposals:
    @staticmethod
    def make_copilot_bot(database: Database) -> StubBot:
        from datetime import timedelta as td

        from tradebot.authorization import ProposalQueue
        from tradebot.core.models import AutonomyMode

        bot = StubBot(database)
        bot.engine = TradingEngine(
            TrendFollowingStrategy(
                TrendFollowingConfig(fast_ema_period=3, slow_ema_period=6, atr_period=3)
            ),
            RiskManager(RiskConfig(), bot.portfolio),
            bot.portfolio,
            SimulatedExecutionAdapter(FillSimulatorConfig()),
            symbol="BTC/USDT",
            autonomy_mode=AutonomyMode.COPILOT,
            proposal_queue=ProposalQueue(ttl=td(minutes=60), max_drift_fraction=Decimal("0.10")),
        )
        return bot

    async def drive_to_proposal(self, bot: StubBot) -> str:
        closes = [100.0, 98.0, 96.0, 94.0, 92.0, 90.0, 100.0, 112.0, 126.0]
        for index, close in enumerate(closes):
            candle = make_candle(close=str(close)).model_copy(
                update={
                    "open_time": BASE_TIME + timedelta(minutes=index),
                    "close_time": BASE_TIME + timedelta(minutes=index + 1),
                    "high_quote": Decimal(str(close + 1)),
                    "low_quote": Decimal(str(close - 1)),
                }
            )
            await bot.engine.process_candle(candle)
            if bot.engine.pending_proposals():
                break  # stop before further candles drift-cancel the proposal
        (proposal,) = bot.engine.pending_proposals()
        return proposal.signal.signal_id

    async def test_pending_proposals_are_listed(self, database: Database) -> None:
        bot = self.make_copilot_bot(database)
        signal_id = await self.drive_to_proposal(bot)

        async with make_client(bot) as client:
            body = (await client.get("/proposals")).json()

        assert len(body) == 1
        assert body[0]["signal_id"] == signal_id
        assert body[0]["side"] == "buy"
        assert body[0]["reasons"]

    async def test_approve_submits_and_clears(self, database: Database) -> None:
        bot = self.make_copilot_bot(database)
        signal_id = await self.drive_to_proposal(bot)

        async with make_client(bot) as client:
            response = await client.post("/proposals/approve", json={"signal_id": signal_id})
            remaining = (await client.get("/proposals")).json()

        assert response.status_code == 200
        assert "order submitted" in response.json()["detail"]
        assert remaining == []

    async def test_reject_clears_without_trading(self, database: Database) -> None:
        bot = self.make_copilot_bot(database)
        signal_id = await self.drive_to_proposal(bot)

        async with make_client(bot) as client:
            response = await client.post("/proposals/reject", json={"signal_id": signal_id})

        assert response.status_code == 200
        assert bot.engine.pending_proposals() == ()
        assert bot.engine.fills == ()

    async def test_already_answered_proposal_is_409_not_404(self, database: Database) -> None:
        bot = self.make_copilot_bot(database)
        signal_id = await self.drive_to_proposal(bot)

        async with make_client(bot) as client:
            first = await client.post("/proposals/reject", json={"signal_id": signal_id})
            second = await client.post("/proposals/reject", json={"signal_id": signal_id})

        assert first.status_code == 200
        assert second.status_code == 409
        assert "already rejected" in second.json()["detail"]

    async def test_unknown_proposal_is_404(self, database: Database) -> None:
        bot = self.make_copilot_bot(database)
        ghost = {"signal_id": "ghost"}
        async with make_client(bot) as client:
            assert (await client.post("/proposals/approve", json=ghost)).status_code == 404
            assert (await client.post("/proposals/reject", json=ghost)).status_code == 404


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
