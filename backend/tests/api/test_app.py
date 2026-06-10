"""Control-plane tests over the ASGI app with a real database behind it."""

import os
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import httpx
import pytest

from tradebot.api import create_app, create_health_only_app
from tradebot.core.config import AppConfig, TradingMode
from tradebot.core.metrics import MetricsCollector
from tradebot.core.models import (
    Candle,
    CandleInterval,
    Decision,
    DecisionOutcome,
    Fill,
    Side,
)
from tradebot.engine import TradingEngine
from tradebot.evaluation.runner import EvaluationRunConfig
from tradebot.evaluation.sweep import SweepConfig
from tradebot.execution import FillSimulatorConfig, SimulatedExecutionAdapter
from tradebot.news import NewsFlags
from tradebot.persistence import CandleStore, Database, DecisionStore, EvaluationStore, FillStore
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

    def __init__(self, database: Database, symbols: str = "BTC/USDT") -> None:
        self.config = AppConfig(mode=TradingMode.PAPER, symbols=symbols)
        self.portfolio = Portfolio(Decimal("10000"))
        self.candle_store = CandleStore(database)
        self.fill_store = FillStore(database)
        self.decision_store = DecisionStore(database)
        # One risk manager shared by all engines, mirroring the worker.
        self.risk_manager = RiskManager(RiskConfig(), self.portfolio)
        self.engines: dict[str, TradingEngine] = {
            symbol: self._build_engine(symbol) for symbol in self.config.symbol_list()
        }
        self.engine = self.engines[self.config.symbol_list()[0]]
        self.evaluation_store = EvaluationStore(database)
        self.metrics = MetricsCollector()
        self.news_flags = NewsFlags()
        self._evaluation_running = False
        self._sweep_running = False

    async def start_evaluation(self, config: EvaluationRunConfig) -> int:
        config.intervals()  # same validation order as the real manager
        if self._evaluation_running:
            raise RuntimeError("evaluation run 1 is already in progress")
        self._evaluation_running = True
        return await self.evaluation_store.create_run(
            symbols=list(config.symbols),
            timeframes=list(config.timeframes),
            config=config.model_dump(),
            code_version="test",
            progress_total=config.scenario_count,
            created_at=BASE_TIME,
        )

    def cancel_evaluation(self, run_id: int) -> bool:
        if self._evaluation_running:
            self._evaluation_running = False
            return True
        return False

    async def start_sweep(self, config: SweepConfig) -> int:
        config.interval()  # same validation order as the real manager
        if self._sweep_running:
            raise RuntimeError("sweep 1 is already in progress")
        self._sweep_running = True
        return await self.evaluation_store.create_sweep(
            symbol=config.symbol,
            timeframe=config.timeframe,
            config=config.model_dump(),
            motivating_finding_ids=list(config.motivating_finding_ids),
            created_at=BASE_TIME,
        )

    def cancel_sweep(self, sweep_id: int) -> bool:
        if self._sweep_running:
            self._sweep_running = False
            return True
        return False

    def _build_engine(self, symbol: str) -> TradingEngine:
        return TradingEngine(
            TrendFollowingStrategy(TrendFollowingConfig()),
            self.risk_manager,
            self.portfolio,
            SimulatedExecutionAdapter(FillSimulatorConfig()),
            symbol=symbol,
        )

    async def add_coin(self, symbol: str) -> None:
        symbol = symbol.strip()
        if symbol in self.engines:
            raise ValueError(f"{symbol} is already being traded")
        self.engines[symbol] = self._build_engine(symbol)

    async def remove_coin(self, symbol: str) -> None:
        if symbol not in self.engines:
            raise KeyError(f"{symbol} is not being traded")
        if len(self.engines) == 1:
            raise RuntimeError("cannot remove the last coin; pause the bot instead")
        del self.engines[symbol]

    def replace_first_engine(self, engine: TradingEngine) -> None:
        """Swap the first symbol's engine (for co-pilot test setups)."""
        first = self.config.symbol_list()[0]
        self.engines[first] = engine
        self.engine = engine


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
            symbols="BTC/USDT",
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
        bot.replace_first_engine(
            TradingEngine(
                TrendFollowingStrategy(
                    TrendFollowingConfig(fast_ema_period=3, slow_ema_period=6, atr_period=3)
                ),
                RiskManager(RiskConfig(), bot.portfolio),
                bot.portfolio,
                SimulatedExecutionAdapter(FillSimulatorConfig()),
                symbol="BTC/USDT",
                autonomy_mode=AutonomyMode.COPILOT,
                proposal_queue=ProposalQueue(
                    ttl=td(minutes=60), max_drift_fraction=Decimal("0.10")
                ),
            )
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


class TestMultiSymbol:
    async def test_status_selects_symbol_and_lists_all(self, database: Database) -> None:
        bot = StubBot(database, symbols="BTC/USDT,ETH/USDT")
        async with make_client(bot) as client:
            default = (await client.get("/status")).json()
            eth = (await client.get("/status", params={"symbol": "ETH/USDT"})).json()

        assert default["symbol"] == "BTC/USDT"  # first configured is the default
        assert default["symbols"] == ["BTC/USDT", "ETH/USDT"]
        assert eth["symbol"] == "ETH/USDT"

    async def test_unknown_symbol_is_404(self, database: Database) -> None:
        bot = StubBot(database)
        async with make_client(bot) as client:
            response = await client.get("/status", params={"symbol": "DOGE/USDT"})
        assert response.status_code == 404
        assert "unknown symbol" in response.json()["detail"]

    async def test_account_equity_spans_all_open_positions(self, database: Database) -> None:
        bot = StubBot(database, symbols="BTC/USDT,ETH/USDT")
        bot.portfolio.apply_fill(make_fill(price="100", quantity="2"))  # BTC
        eth_fill = make_fill(price="10", quantity="5").model_copy(
            update={"symbol": "ETH/USDT", "client_order_id": "ord-eth"}
        )
        bot.portfolio.apply_fill(eth_fill)
        await bot.candle_store.insert_batch([make_candle(close="110")])  # BTC mark only

        async with make_client(bot) as client:
            without_eth_mark = (await client.get("/status")).json()
            eth_candle = make_candle(close="12").model_copy(update={"symbol": "ETH/USDT"})
            await bot.candle_store.insert_batch([eth_candle])
            with_both_marks = (await client.get("/status")).json()

        # An unmarkable open position means equity is unknown, never a guess.
        assert without_eth_mark["equity_quote"] is None
        # 10000 - 200.2 (BTC cost) - 50.2 (ETH cost) + 220 + 60 marked
        assert with_both_marks["equity_quote"] == "10029.6"

    async def test_pause_and_kill_act_on_every_symbol(self, database: Database) -> None:
        bot = StubBot(database, symbols="BTC/USDT,ETH/USDT")
        async with make_client(bot) as client:
            await client.post("/pause")
            assert all(engine.paused for engine in bot.engines.values())
            await client.post("/resume")
            assert not any(engine.paused for engine in bot.engines.values())
            body = (await client.post("/kill")).json()

        assert "no position" in body["detail"]
        assert all(engine.paused for engine in bot.engines.values())


class TestCoins:
    async def test_add_shows_up_in_status_symbols(self, database: Database) -> None:
        bot = StubBot(database)
        async with make_client(bot) as client:
            response = await client.post("/coins", json={"symbol": "ETH/USDT"})
            body = (await client.get("/status")).json()

        assert response.status_code == 200
        assert response.json()["detail"] == "ETH/USDT added"
        assert body["symbols"] == ["BTC/USDT", "ETH/USDT"]

    async def test_duplicate_add_is_400(self, database: Database) -> None:
        bot = StubBot(database)
        async with make_client(bot) as client:
            response = await client.post("/coins", json={"symbol": "BTC/USDT"})
        assert response.status_code == 400
        assert "already being traded" in response.json()["detail"]

    async def test_remove_and_its_guards(self, database: Database) -> None:
        bot = StubBot(database, symbols="BTC/USDT,ETH/USDT")
        async with make_client(bot) as client:
            removed = await client.post("/coins/remove", json={"symbol": "ETH/USDT"})
            unknown = await client.post("/coins/remove", json={"symbol": "DOGE/USDT"})
            last = await client.post("/coins/remove", json={"symbol": "BTC/USDT"})

        assert removed.status_code == 200
        assert unknown.status_code == 404
        assert last.status_code == 409
        assert "last coin" in last.json()["detail"]
        assert list(bot.engines) == ["BTC/USDT"]


class TestEvaluations:
    async def test_start_list_and_fetch(self, database: Database) -> None:
        bot = StubBot(database)
        async with make_client(bot) as client:
            started = await client.post("/evaluations", json={"scenario_count": 50})
            run_id = started.json()["run_id"]
            listed = (await client.get("/evaluations")).json()
            fetched = (await client.get(f"/evaluations/{run_id}")).json()

        assert started.status_code == 200
        assert [run["id"] for run in listed] == [run_id]
        assert fetched["status"] == "pending"
        assert fetched["symbols"] == ["BTC/USDT"]  # defaulted to active coins
        assert fetched["config"]["scenario_count"] == 50
        assert fetched["summary"] is None

    async def test_second_start_while_running_is_409(self, database: Database) -> None:
        bot = StubBot(database)
        async with make_client(bot) as client:
            assert (await client.post("/evaluations", json={})).status_code == 200
            second = await client.post("/evaluations", json={})
        assert second.status_code == 409
        assert "already in progress" in second.json()["detail"]

    async def test_bad_timeframe_is_400(self, database: Database) -> None:
        bot = StubBot(database)
        async with make_client(bot) as client:
            response = await client.post("/evaluations", json={"timeframes": ["7m"]})
        assert response.status_code == 400

    async def test_cancel_and_unknown_run(self, database: Database) -> None:
        bot = StubBot(database)
        async with make_client(bot) as client:
            started = await client.post("/evaluations", json={})
            run_id = started.json()["run_id"]
            cancelled = await client.post(f"/evaluations/{run_id}/cancel")
            again = await client.post(f"/evaluations/{run_id}/cancel")
            missing = await client.get("/evaluations/9999")

        assert cancelled.status_code == 200
        assert again.status_code == 409  # nothing in flight any more
        assert missing.status_code == 404


class TestScenarioReplay:
    """The replay endpoints rebuild a scenario's candles from the store."""

    @staticmethod
    async def seed_graded_scenario(bot: StubBot) -> tuple[int, int]:
        """Insert candles, a run, and one graded scenario; returns (run, scenario) ids."""
        from tradebot.evaluation.models import (
            MarketConditions,
            Scenario,
            ScenarioClass,
            ScenarioResult,
            TimingLabel,
            TrendLabel,
            Verdict,
            VolatilityLabel,
        )

        candles = [
            make_candle(close=str(100 + index)).model_copy(
                update={
                    "open_time": BASE_TIME + timedelta(minutes=index),
                    "close_time": BASE_TIME + timedelta(minutes=index + 1),
                }
            )
            for index in range(100)
        ]
        await bot.candle_store.insert_batch(candles)
        config = EvaluationRunConfig(
            symbols=("BTC/USDT",), timeframes=("1m",), lookback_candles=60, horizon_candles=30
        )
        run_id = await bot.evaluation_store.create_run(
            ["BTC/USDT"], ["1m"], config.model_dump(), "test", 1, BASE_TIME
        )
        scenario = Scenario(
            run_id=run_id,
            symbol="BTC/USDT",
            timeframe="1m",
            decision_time=BASE_TIME + timedelta(minutes=60),
            lookback_candles=60,
            scenario_class=ScenarioClass.FLAT,
            conditions=MarketConditions(
                trend=TrendLabel.UP, volatility=VolatilityLabel.NORMAL, events=()
            ),
            seed=7,
        )
        (scenario_id,) = await bot.evaluation_store.insert_scenarios([scenario])
        await bot.evaluation_store.insert_result(
            ScenarioResult(
                scenario_id=scenario_id,
                decision="buy",
                confidence=0.8,
                reasons=("fast EMA crossed above slow EMA",),
                entry_price_quote=Decimal("160.16"),
                exit_price_quote=Decimal("189.81"),
                r_multiple=Decimal("1.85"),
                pnl_quote=Decimal("29.3"),
                mfe_r=Decimal("2.1"),
                mae_r=Decimal("-0.1"),
                duration_candles=30,
                stop_hit=False,
                oracle_r=Decimal("2.2"),
                verdict=Verdict.EXCELLENT,
                timing=TimingLabel.ON_TIME,
                created_at=BASE_TIME,
            )
        )
        return run_id, scenario_id

    async def test_run_scenarios_are_listed_with_their_grades(self, database: Database) -> None:
        bot = StubBot(database)
        run_id, scenario_id = await self.seed_graded_scenario(bot)

        async with make_client(bot) as client:
            body = (await client.get(f"/evaluations/{run_id}/scenarios")).json()

        assert len(body) == 1
        assert body[0]["scenario_id"] == scenario_id
        assert body[0]["decision"] == "buy"
        assert body[0]["verdict"] == "excellent"
        assert body[0]["r_multiple"] == "1.85"
        assert body[0]["trend"] == "up"

    async def test_replay_rebuilds_window_and_horizon_candles(self, database: Database) -> None:
        bot = StubBot(database)
        _, scenario_id = await self.seed_graded_scenario(bot)

        async with make_client(bot) as client:
            body = (await client.get(f"/evaluations/scenarios/{scenario_id}")).json()

        assert len(body["window"]) == 60
        assert len(body["horizon"]) == 30  # the run config's horizon_candles
        decision_time = (BASE_TIME + timedelta(minutes=60)).isoformat()
        assert body["window"][-1]["open_time"] == (BASE_TIME + timedelta(minutes=59)).isoformat()
        assert body["horizon"][0]["open_time"] == decision_time
        assert body["scenario"]["decision_time"] == decision_time
        assert body["entry_price_quote"] == "160.16"
        assert body["reasons"] == ["fast EMA crossed above slow EMA"]
        assert body["confidence"] == 0.8

    async def test_unknown_run_and_scenario_are_404(self, database: Database) -> None:
        bot = StubBot(database)
        async with make_client(bot) as client:
            assert (await client.get("/evaluations/9999/scenarios")).status_code == 404
            assert (await client.get("/evaluations/scenarios/9999")).status_code == 404

    async def test_ungraded_scenarios_are_not_listed(self, database: Database) -> None:
        """Mid-flight scenarios without a result stay out of the browser."""
        from tradebot.evaluation.models import (
            MarketConditions,
            Scenario,
            ScenarioClass,
            TrendLabel,
            VolatilityLabel,
        )

        bot = StubBot(database)
        run_id, scenario_id = await self.seed_graded_scenario(bot)
        ungraded = Scenario(
            run_id=run_id,
            symbol="BTC/USDT",
            timeframe="1m",
            decision_time=BASE_TIME + timedelta(minutes=61),
            lookback_candles=60,
            scenario_class=ScenarioClass.FLAT,
            conditions=MarketConditions(
                trend=TrendLabel.UP, volatility=VolatilityLabel.NORMAL, events=()
            ),
            seed=7,
        )
        await bot.evaluation_store.insert_scenarios([ungraded])

        async with make_client(bot) as client:
            body = (await client.get(f"/evaluations/{run_id}/scenarios")).json()

        assert [row["scenario_id"] for row in body] == [scenario_id]


class TestFindings:
    """Findings carry the human accept/reject verdict — recorded, never repeated."""

    @staticmethod
    async def seed_finding(bot: StubBot) -> tuple[int, int]:
        """Insert a run with one proposed finding; returns (run, finding) ids."""
        from tradebot.evaluation.models import LearningFinding

        config = EvaluationRunConfig(symbols=("BTC/USDT",))
        run_id = await bot.evaluation_store.create_run(
            ["BTC/USDT"], ["1h"], config.model_dump(), "test", 1, BASE_TIME
        )
        finding_id = await bot.evaluation_store.insert_finding(
            LearningFinding(
                run_id=run_id,
                pattern="entries lose money when trend is ranging",
                evidence_scenario_ids=(1, 2, 3),
                affected_count=3,
                average_r_impact=Decimal("-0.4"),
                suggestion="gate entries behind extra confirmation when trend is ranging",
                confidence="low",
                created_at=BASE_TIME,
            )
        )
        return run_id, finding_id

    async def test_run_findings_are_listed(self, database: Database) -> None:
        bot = StubBot(database)
        run_id, finding_id = await self.seed_finding(bot)

        async with make_client(bot) as client:
            body = (await client.get(f"/evaluations/{run_id}/findings")).json()
            missing = await client.get("/evaluations/9999/findings")

        assert len(body) == 1
        assert body[0]["id"] == finding_id
        assert body[0]["status"] == "proposed"
        assert body[0]["average_r_impact"] == "-0.4"
        assert body[0]["evidence_scenario_ids"] == [1, 2, 3]
        assert missing.status_code == 404

    async def test_accept_records_the_verdict_once(self, database: Database) -> None:
        bot = StubBot(database)
        run_id, finding_id = await self.seed_finding(bot)

        async with make_client(bot) as client:
            accepted = await client.post(f"/evaluations/findings/{finding_id}/accept")
            repeat = await client.post(f"/evaluations/findings/{finding_id}/reject")
            listed = (await client.get(f"/evaluations/{run_id}/findings")).json()

        assert accepted.status_code == 200
        assert accepted.json()["status"] == "accepted"
        # The verdict is lineage (§12.5): flipping it would rewrite history.
        assert repeat.status_code == 409
        assert "already accepted" in repeat.json()["detail"]
        assert listed[0]["status"] == "accepted"

    async def test_reject_and_unknown_finding(self, database: Database) -> None:
        bot = StubBot(database)
        _, finding_id = await self.seed_finding(bot)

        async with make_client(bot) as client:
            rejected = await client.post(f"/evaluations/findings/{finding_id}/reject")
            missing = await client.post("/evaluations/findings/9999/accept")

        assert rejected.status_code == 200
        assert rejected.json()["status"] == "rejected"
        assert missing.status_code == 404


class TestSweeps:
    async def test_start_with_default_grid_lists_and_fetches(self, database: Database) -> None:
        bot = StubBot(database)
        async with make_client(bot) as client:
            started = await client.post("/sweeps", json={})
            sweep_id = started.json()["run_id"]
            listed = (await client.get("/sweeps")).json()
            fetched = (await client.get(f"/sweeps/{sweep_id}")).json()

        assert started.status_code == 200
        assert [sweep["id"] for sweep in listed] == [sweep_id]
        assert fetched["status"] == "pending"
        assert fetched["symbol"] == "BTC/USDT"  # defaulted to the first active coin
        # The default grid rides in the config snapshot, baseline first.
        names = [candidate["name"] for candidate in fetched["config"]["candidates"]]
        assert names[0].startswith("baseline")
        assert len(names) >= 2
        assert fetched["report"] is None

    async def test_second_start_while_running_is_409(self, database: Database) -> None:
        bot = StubBot(database)
        async with make_client(bot) as client:
            assert (await client.post("/sweeps", json={})).status_code == 200
            second = await client.post("/sweeps", json={})
        assert second.status_code == 409

    async def test_bad_config_is_400(self, database: Database) -> None:
        bot = StubBot(database)
        duplicate = {
            "candidates": [
                {"name": "same", "params": {}},
                {"name": "same", "params": {}},
            ]
        }
        async with make_client(bot) as client:
            bad_timeframe = await client.post("/sweeps", json={"timeframe": "7m"})
            duplicate_names = await client.post("/sweeps", json=duplicate)
        assert bad_timeframe.status_code == 400
        assert duplicate_names.status_code == 400

    async def test_cancel_and_unknown_sweep(self, database: Database) -> None:
        bot = StubBot(database)
        async with make_client(bot) as client:
            started = await client.post("/sweeps", json={})
            sweep_id = started.json()["run_id"]
            cancelled = await client.post(f"/sweeps/{sweep_id}/cancel")
            again = await client.post(f"/sweeps/{sweep_id}/cancel")
            missing = await client.get("/sweeps/9999")

        assert cancelled.status_code == 200
        assert again.status_code == 409
        assert missing.status_code == 404


class TestMetrics:
    async def test_metrics_render_gauges_and_counters(self, database: Database) -> None:
        bot = StubBot(database)
        bot.metrics.candles_total["BTC/USDT"] = 7
        await bot.candle_store.insert_batch([make_candle(close="110")])

        async with make_client(bot) as client:
            response = await client.get("/metrics")

        assert response.status_code == 200
        text = response.text
        assert "tradebot_up 1" in text
        assert "tradebot_quote_balance 10000.0" in text
        assert 'tradebot_candles_processed_total{symbol="BTC/USDT"} 7' in text
        assert 'tradebot_last_candle_age_seconds{symbol="BTC/USDT"}' in text
        assert "tradebot_breaker_tripped 0" in text

    async def test_metrics_require_the_bearer_token(self, database: Database) -> None:
        """Balances live in here; an unauthenticated scraper gets nothing."""
        app = create_app(StubBot(database), TOKEN)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://control"
        ) as client:
            assert (await client.get("/metrics")).status_code == 401


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
