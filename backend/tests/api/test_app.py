"""Control-plane tests over the ASGI app with a real database behind it."""

import os
from collections.abc import AsyncIterator, Iterator, Mapping
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import httpx
import pytest

from tradebot.api import create_app, create_health_only_app
from tradebot.backtest.parity import DivergenceReport, compare_fills
from tradebot.competition import LINEUP
from tradebot.competition.candidacy import RoutingCandidacy, assemble_candidacies
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
from tradebot.evaluation.bakeoff import BakeOffConfig
from tradebot.evaluation.runner import EvaluationRunConfig
from tradebot.evaluation.sweep import SweepConfig
from tradebot.execution import FillSimulatorConfig, SimulatedExecutionAdapter
from tradebot.news import NewsFlags
from tradebot.persistence import (
    BakeOffStore,
    CandleStore,
    Database,
    DecisionStore,
    EvaluationStore,
    FillStore,
    StrategySettingsStore,
)
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


class _StubFeedHealth:
    """A feed's health latch (FeedHealth) for the app factory's /status tests."""

    def __init__(self, healthy: bool, reason: str | None) -> None:
        self._healthy = healthy
        self._reason = reason

    @property
    def healthy(self) -> bool:
        return self._healthy

    @property
    def health_reason(self) -> str | None:
        return self._reason


class StubBot:
    """Minimal BotState for the app factory."""

    def __init__(self, database: Database, symbols: str = "BTC/USDT") -> None:
        self.config = AppConfig(mode=TradingMode.PAPER, symbols=symbols)
        self.strategy_params: dict[str, dict[str, object]] = {}
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
        self.risk_state_persists = 0
        self.regime_detector = None
        self.regime_disabled_reason: str | None = None
        self.feed_healths: dict[str, _StubFeedHealth] = {}
        self.evaluation_store = EvaluationStore(database)
        self.bake_off_store = BakeOffStore(database)
        self.strategy_settings_store = StrategySettingsStore(database)
        self.metrics = MetricsCollector()
        self.news_flags = NewsFlags()
        self._evaluation_running = False
        self._sweep_running = False
        # Runs whose acceptance triggered the (stubbed) coalescing sweep.
        self.acceptance_notes: list[int] = []
        self._campaign_enabled = True
        # Custom bots, in-memory: bot_id -> {label, description, rules};
        # paused state per bot id. Enough behavior for the endpoints'
        # contract tests — worker tests cover the real lifecycle.
        self.custom_bots: dict[str, dict[str, Any]] = {}
        self.paused_bots: set[str] = set()
        # Per-bot starting capital overrides (bot_id -> balance); empty means
        # every bot uses the config default.
        self.bot_capital: dict[str, Decimal] = {}
        # In-memory trading fees, seeded from config defaults (10 bps a side).
        self.fees: dict[str, Decimal] = {
            "buy_fee_bps": self.config.buy_fee_bps,
            "sell_fee_bps": self.config.sell_fee_bps,
        }

    def all_engines(self) -> Iterator[TradingEngine]:
        yield from self.engines.values()

    def feed_health(self, symbol: str) -> "_StubFeedHealth":
        # Healthy by default; individual tests flip it to exercise /status.
        return self.feed_healths.get(symbol, _StubFeedHealth(True, None))

    def _snapshot_row(self, bot_id: str, label: str, description: str, kind: str) -> dict[str, Any]:
        initial = self.bot_capital.get(bot_id, self.config.paper_initial_balance_quote)
        equity = self.portfolio.quote_balance
        return {
            "bot_id": bot_id,
            "label": label,
            "description": description,
            "is_production": kind == "production",
            "kind": kind,
            "paused": bot_id in self.paused_bots,
            "equity_quote": equity,
            "initial_balance_quote": initial,
            "return_fraction": (equity - initial) / initial,
            "quote_balance": self.portfolio.quote_balance,
            "realized_pnl_quote": self.portfolio.realized_pnl_quote(),
            "unrealized_pnl_quote": Decimal(0),
            "open_positions": len(self.portfolio.positions),
            "entry_fills": 0,
            "exit_fills": 0,
            "breaker_tripped_reason": None,
        }

    async def competition_snapshot(self) -> list[dict[str, Any]]:
        # The incumbent plus any stub custom bots, in the worker's row
        # shape: enough surface for serialization to be exercised.
        rows = [
            self._snapshot_row("production", "Regime router", "stub lineup entry", "production")
        ]
        for bot_id, bot in self.custom_bots.items():
            rows.append(self._snapshot_row(bot_id, bot["label"], bot["description"], "custom"))
        return rows

    async def routing_candidacies(self) -> list[RoutingCandidacy]:
        # Empty record: three research families, none a candidate yet — enough
        # surface for the endpoint's serialization to be exercised.
        return assemble_candidacies(
            sweeps=[],
            runs=[],
            comparisons=[],
            competition=[],
            started_at={},
            now=datetime.now(UTC),
        )

    async def bot_detail(self, bot_id: str) -> dict[str, Any]:
        rows = await self.competition_snapshot()
        row = next((entry for entry in rows if entry["bot_id"] == bot_id), None)
        if row is None:
            raise KeyError(f"no competition bot {bot_id!r}")
        strategy: dict[str, Any] = (
            {"kind": "custom", "rules": self.custom_bots[bot_id]["rules"]}
            if bot_id in self.custom_bots
            else {"kind": "production", "regime_routed": False, "families": {}}
        )
        return {**row, "positions": [], "strategy": strategy}

    async def pause_bot(self, bot_id: str) -> None:
        await self.bot_detail(bot_id)  # KeyError for unknown bots
        self.paused_bots.add(bot_id)

    async def resume_bot(self, bot_id: str) -> None:
        await self.bot_detail(bot_id)
        self.paused_bots.discard(bot_id)

    async def kill_bot(self, bot_id: str) -> tuple[int, list[str]]:
        await self.bot_detail(bot_id)
        self.paused_bots.add(bot_id)
        return 0, []

    async def create_custom_bot(
        self, label: str, description: str, rules: Mapping[str, Any]
    ) -> str:
        from tradebot.competition import describe_rules, slugify_bot_label, validate_rules

        normalized = validate_rules(rules)
        bot_id = slugify_bot_label(label)
        if bot_id in self.custom_bots:
            raise ValueError(f"a bot named {label.strip()!r} already exists")
        self.custom_bots[bot_id] = {
            "label": label.strip(),
            "description": description.strip() or describe_rules(normalized),
            "rules": normalized,
        }
        return bot_id

    async def update_custom_bot(self, bot_id: str, rules: Mapping[str, Any]) -> None:
        from tradebot.competition import validate_rules

        if bot_id not in self.custom_bots:
            raise KeyError(f"no custom bot {bot_id!r}")
        self.custom_bots[bot_id]["rules"] = validate_rules(rules)

    async def delete_custom_bot(self, bot_id: str) -> None:
        if bot_id == "production":
            raise ValueError("production is a built-in lineup bot and cannot be deleted")
        if bot_id not in self.custom_bots:
            raise KeyError(f"no custom bot {bot_id!r}")
        del self.custom_bots[bot_id]

    async def reset_bot_capital(self, bot_id: str, new_balance_quote: Decimal) -> None:
        if new_balance_quote <= 0:
            raise ValueError("starting capital must be greater than zero")
        if bot_id != "production" and bot_id not in self.custom_bots:
            raise KeyError(f"no competition bot {bot_id!r}")
        if self.portfolio.positions:
            raise RuntimeError(f"{bot_id} holds a position; stop or flatten it first")
        self.bot_capital[bot_id] = new_balance_quote

    def trading_fees(self) -> Mapping[str, Decimal]:
        return dict(self.fees)

    async def update_trading_fees(self, *, buy_fee_bps: Decimal, sell_fee_bps: Decimal) -> None:
        from tradebot.execution import FeeSchedule

        # Reuse the real validation so the stub rejects exactly what the
        # worker would (negative, or above the sanity cap).
        FeeSchedule(buy_fee_bps=buy_fee_bps, sell_fee_bps=sell_fee_bps)
        self.fees = {"buy_fee_bps": buy_fee_bps, "sell_fee_bps": sell_fee_bps}

    def fill_store_for(self, bot_id: str) -> FillStore:
        if bot_id != "production" and bot_id not in self.custom_bots:
            raise KeyError(f"no competition bot {bot_id!r}")
        return self.fill_store

    def decision_store_for(self, bot_id: str) -> DecisionStore:
        if bot_id != "production" and bot_id not in self.custom_bots:
            raise KeyError(f"no competition bot {bot_id!r}")
        return self.decision_store

    async def start_comparison(self, config: EvaluationRunConfig) -> list[int]:
        config.intervals()  # same validation order as the real manager
        if self._evaluation_running:
            raise RuntimeError("evaluation run 1 is already in progress")
        run_ids: list[int] = []
        group: int | None = None
        for spec in LINEUP:
            run_id = await self.evaluation_store.create_run(
                symbols=list(config.symbols),
                timeframes=list(config.timeframes),
                config=config.model_dump(mode="json"),
                code_version="test",
                progress_total=config.scenario_count,
                created_at=BASE_TIME,
                strategy=spec.bot_id,
                comparison_group=group,
            )
            if group is None:
                group = run_id
                await self.evaluation_store.set_comparison_group(run_id, run_id)
            run_ids.append(run_id)
        return run_ids

    async def start_bake_off(self, config: BakeOffConfig) -> int:
        config.validated()
        if self._evaluation_running:
            raise RuntimeError("bake-off already in progress")
        return await self.bake_off_store.create_job(
            config=config.model_dump(mode="json"),
            contestants=["production", "trend_calm"],
            cells_total=sum(len(windows) for _, windows in config.grid),
            created_at=BASE_TIME,
        )

    async def bake_off(self, job_id: int) -> dict[str, object] | None:
        return await self.bake_off_store.fetch_job(job_id)

    async def list_bake_offs(self, limit: int = 20) -> list[dict[str, object]]:
        return await self.bake_off_store.list_jobs(limit)

    async def revert_strategy_version(self, version_id: int) -> int:
        row = await self.strategy_settings_store.fetch(version_id)
        if row is None:
            raise KeyError(f"no strategy settings version {version_id}")
        return await self.strategy_settings_store.record(
            row["family"],
            row["params"],
            BASE_TIME,
            row["source_sweep_id"],
            f"manual revert to version #{version_id}",
        )

    def evaluation_strategies(self) -> list[dict[str, str]]:
        # The worker's shape: the fixed lineup plus any stub custom bots.
        rows = [
            {
                "id": spec.bot_id,
                "label": spec.label,
                "description": spec.description,
                "kind": "production" if spec.bot_id == "production" else "builtin",
            }
            for spec in LINEUP
        ]
        for bot_id, bot in self.custom_bots.items():
            rows.append(
                {
                    "id": bot_id,
                    "label": bot["label"],
                    "description": bot["description"],
                    "kind": "custom",
                }
            )
        return rows

    def recipe_for(self, bot_id: str) -> dict[str, Any] | None:
        bot = self.custom_bots.get(bot_id)
        return dict(bot["rules"]) if bot is not None else None

    def note_finding_acceptance(self, run_id: int) -> None:
        # The worker arms a coalescing sweep timer here; the stub records
        # the trigger so accept-path tests can assert it fired (and the run
        # reads as "pending" for the sweep_queued annotation).
        self.acceptance_notes.append(run_id)

    def accept_sweep_pending(self, run_id: int) -> bool:
        return run_id in self.acceptance_notes

    def improvement_status(self) -> dict[str, Any]:
        # Scripted mid-loop snapshot in the worker's shape (datetimes, not
        # strings — the route serializes them).
        return {
            "enabled": True,
            "interval_hours": 12,
            "history_days": 365,
            "timeframe": "1h",
            "last_cycle_started_at": BASE_TIME,
            "last_cycle_finished_at": BASE_TIME + timedelta(minutes=30),
            "last_outcome": "sweep #1 kept the active configuration (verdict: overfit)",
            "next_cycle_at": BASE_TIME + timedelta(hours=12),
        }

    def campaign_status(self) -> dict[str, Any]:
        # Scripted mid-campaign snapshot in the worker's shape (datetimes, not
        # strings — the route serializes them).
        return {
            "enabled": self._campaign_enabled,
            "max_rounds": 8,
            "max_hours": 6.0,
            "timeframe": "1h",
            "campaign": {
                "target": "momentum",
                "symbol": "BTC/USDT",
                "timeframe": "1h",
                "promotions_enabled": True,
                "status": "running",
                "promotions": 1,
                "stop_reason": None,
                "holdout_start": BASE_TIME,
                "started_at": BASE_TIME,
                "finished_at": None,
                "holdout_read": None,
                "rounds": [
                    {
                        "index": 0,
                        "scale": 1.0,
                        "sweep_id": 1,
                        "verdict": "validated",
                        "winner": "faster_macd",
                        "promoted_version": 3,
                        "note": "promoted momentum settings v3 (faster_macd)",
                        "changes": [{"field": "macd_fast", "before": "12", "after": "8"}],
                    }
                ],
            },
        }

    async def campaign_history(self, limit: int = 20) -> list[dict[str, Any]]:
        # One finished campaign in the persisted shape (ISO strings, like JSONB).
        return [
            {
                "target": "momentum",
                "symbol": "ETH/USDT",
                "timeframe": "1d",
                "promotions_enabled": False,
                "status": "completed",
                "promotions": 2,
                "stop_reason": "budget spent: reached the 8-round limit",
                "holdout_start": BASE_TIME.isoformat(),
                "started_at": BASE_TIME.isoformat(),
                "finished_at": BASE_TIME.isoformat(),
                "holdout_read": {
                    "judged": True,
                    "improved": True,
                    "explanation": "improved out of sample",
                    "start_expectancy_r": "0.05",
                    "final_expectancy_r": "0.20",
                },
                "rounds": [
                    {
                        "index": 0,
                        "scale": 1.0,
                        "sweep_id": 7,
                        "verdict": "validated",
                        "winner": "faster_macd",
                        "promoted_version": 5,
                        "note": "promoted momentum settings v5 (faster_macd)",
                        "changes": [{"field": "macd_fast", "before": "12", "after": "8"}],
                    }
                ],
            }
        ][:limit]

    async def update_campaign_enabled(self, *, enabled: bool) -> None:
        self._campaign_enabled = enabled

    async def start_evaluation(self, config: EvaluationRunConfig) -> int:
        # Same validation order as the worker: the graded bot first (before
        # any row exists), then the manager's timeframe check.
        known = {entry["id"] for entry in self.evaluation_strategies()}
        if config.strategy not in known:
            raise ValueError(f"unknown strategy {config.strategy!r}; known: {sorted(known)}")
        config.intervals()
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
            strategy=config.strategy,
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

    async def persist_risk_state(self) -> None:
        """Count synchronous persists (pause/resume/kill must call this)."""
        self.risk_state_persists += 1

    async def divergence_report(
        self, symbol: str, window_hours: int = 24, window_end: datetime | None = None
    ) -> DivergenceReport:
        """Scripted §10 metric: zero divergence for traded coins."""
        if symbol not in self.engines:
            raise KeyError(f"{symbol} is not being traded")
        end = window_end if window_end is not None else BASE_TIME
        return compare_fills([], [], end - timedelta(hours=window_hours), end)

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


class TestAuthLockout:
    async def test_burst_of_bad_tokens_locks_even_the_right_one_out(
        self, database: Database
    ) -> None:
        """Brute-force brake: the cooldown answers 429, not 401, to everyone."""
        app = create_app(StubBot(database), TOKEN)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://control"
        ) as client:
            for _ in range(11):
                response = await client.get("/status", headers={"Authorization": "Bearer wrong"})
                assert response.status_code == 401
            locked = await client.get("/status", headers={"Authorization": f"Bearer {TOKEN}"})

        assert locked.status_code == 429
        assert "try again shortly" in locked.json()["detail"]

    def test_lock_engages_on_burst_and_expires_after_cooldown(self) -> None:
        from tradebot.api.app import AuthLockout

        lockout = AuthLockout(max_failures=3, window=timedelta(minutes=1))
        start = BASE_TIME
        for seconds in range(3):
            lockout.record_failure(start + timedelta(seconds=seconds))
        assert lockout.locked_until(start + timedelta(seconds=3)) is None  # at the limit

        lockout.record_failure(start + timedelta(seconds=3))  # over it
        assert lockout.locked_until(start + timedelta(seconds=4)) is not None
        assert lockout.locked_until(start + timedelta(minutes=2)) is None  # cooled down

    def test_slow_typos_never_trip_the_lock(self) -> None:
        from tradebot.api.app import AuthLockout

        lockout = AuthLockout(max_failures=3, window=timedelta(minutes=1))
        for minutes in range(10):  # one bad token a minute: an operator, not an attack
            lockout.record_failure(BASE_TIME + timedelta(minutes=2 * minutes))
        assert lockout.locked_until(BASE_TIME + timedelta(minutes=20)) is None


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

    async def test_candles_aggregate_to_calendar_buckets(self, database: Database) -> None:
        bot = StubBot(database)
        late = make_candle(close="120").model_copy(
            update={
                "open_time": BASE_TIME + timedelta(minutes=30),
                "close_time": BASE_TIME + timedelta(minutes=31),
                "high_quote": Decimal("125"),
            }
        )
        await bot.candle_store.insert_batch([make_candle(close="110"), late])

        async with make_client(bot) as client:
            for interval in ("1h", "1d", "1w", "1M"):
                (bucket,) = (await client.get(f"/candles?interval={interval}")).json()
                # Two 1m candles roll into one bucket on every timeframe.
                assert bucket["open_quote"] == "100"
                assert bucket["close_quote"] == "120"
                assert bucket["high_quote"] == "125"
                assert bucket["volume_base"] == "2"

    async def test_unknown_chart_interval_is_400(self, database: Database) -> None:
        bot = StubBot(database)
        async with make_client(bot) as client:
            response = await client.get("/candles?interval=7m")
        assert response.status_code == 400
        assert "supported" in response.json()["detail"]


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

    async def test_suggestions_fit_stored_history(self, database: Database) -> None:
        """A coin with candles gets the full three-rung ladder, ready to run."""
        bot = StubBot(database)
        await bot.candle_store.insert_batch([make_candle(close="110")])
        async with make_client(bot) as client:
            response = await client.get("/evaluations/suggestions")

        assert response.status_code == 200
        suggestions = response.json()
        assert [s["timeframe"] for s in suggestions] == ["4h", "1h", "15m"]
        assert all(s["symbol"] == "BTC/USDT" for s in suggestions)
        # Exact depths depend on the wall clock; runnability does not.
        assert all(s["history_days"] >= 1 for s in suggestions)
        assert all(s["scenario_count"] > 0 for s in suggestions)

    async def test_no_stored_candles_means_no_suggestions(self, database: Database) -> None:
        bot = StubBot(database)
        async with make_client(bot) as client:
            response = await client.get("/evaluations/suggestions")

        assert response.status_code == 200
        assert response.json() == []

    async def test_suggestion_starts_verbatim(self, database: Database) -> None:
        """The whole point: a suggestion submits as-is and creates a run."""
        bot = StubBot(database)
        await bot.candle_store.insert_batch([make_candle(close="110")])
        async with make_client(bot) as client:
            suggestion = (await client.get("/evaluations/suggestions")).json()[0]
            started = await client.post(
                "/evaluations",
                json={
                    "symbols": [suggestion["symbol"]],
                    "timeframes": [suggestion["timeframe"]],
                    "history_days": suggestion["history_days"],
                    "scenario_count": suggestion["scenario_count"],
                },
            )

        assert started.status_code == 200

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

    async def test_strategies_offer_the_lineup_and_custom_bots(self, database: Database) -> None:
        bot = StubBot(database)
        bot_id = await bot.create_custom_bot("My Recipe", "", {"families": {"trend_following": {}}})
        async with make_client(bot) as client:
            response = await client.get("/evaluations/strategies")

        assert response.status_code == 200
        rows = response.json()
        ids = [row["id"] for row in rows]
        assert ids[0] == "production"  # the default leads the selector
        assert {spec.bot_id for spec in LINEUP} <= set(ids)
        custom = next(row for row in rows if row["id"] == bot_id)
        assert custom["kind"] == "custom"
        assert custom["label"] == "My Recipe"

    async def test_run_grades_the_requested_bot(self, database: Database) -> None:
        bot = StubBot(database)
        async with make_client(bot) as client:
            started = await client.post("/evaluations", json={"strategy": "breakout"})
            run_id = started.json()["run_id"]
            fetched = (await client.get(f"/evaluations/{run_id}")).json()

        assert started.status_code == 200
        assert fetched["strategy"] == "breakout"

    async def test_unknown_strategy_is_400_and_creates_no_run(self, database: Database) -> None:
        bot = StubBot(database)
        async with make_client(bot) as client:
            response = await client.post("/evaluations", json={"strategy": "nope"})
            listed = (await client.get("/evaluations")).json()

        assert response.status_code == 400
        assert "unknown strategy" in response.json()["detail"]
        assert listed == []  # the typo'd run never left a row behind

    async def test_a_custom_bot_is_evaluable_by_id(self, database: Database) -> None:
        bot = StubBot(database)
        bot_id = await bot.create_custom_bot("My Recipe", "", {"families": {"trend_following": {}}})
        async with make_client(bot) as client:
            started = await client.post("/evaluations", json={"strategy": bot_id})
            fetched = (await client.get(f"/evaluations/{started.json()['run_id']}")).json()

        assert started.status_code == 200
        assert fetched["strategy"] == bot_id


async def seed_completed_run(
    bot: StubBot,
    *,
    strategy: str = "production",
    created_at: datetime,
    patterns: Mapping[str, str] | None = None,
    summary: dict[str, Any] | None = None,
) -> tuple[int, dict[str, int]]:
    """Insert one completed run with findings; returns (run_id, ids by pattern)."""
    from tradebot.evaluation.models import LearningFinding

    run_id = await bot.evaluation_store.create_run(
        symbols=["BTC/USDT"],
        timeframes=["1h"],
        config={},
        code_version="test",
        progress_total=10,
        created_at=created_at,
        strategy=strategy,
    )
    await bot.evaluation_store.complete_run(
        run_id, summary or {"expectancy_r": "-0.1296", "trade_count": 187}
    )
    finding_ids: dict[str, int] = {}
    for pattern, finding_status in (patterns or {}).items():
        finding_id = await bot.evaluation_store.insert_finding(
            LearningFinding(
                run_id=run_id,
                pattern=pattern,
                evidence_scenario_ids=(1,),
                affected_count=1,
                average_r_impact=Decimal("-0.4"),
                suggestion="test",
                confidence="low",
                status=finding_status,
                created_at=created_at,
            )
        )
        finding_ids[pattern] = finding_id
    return run_id, finding_ids


class TestResearchAdvisor:
    async def test_advise_disabled_returns_unavailable(self, database: Database) -> None:
        # The advisor is off by default, so the route wires straight through to a
        # fail-safe "no advice" answer — no model is ever called from this path.
        bot = StubBot(database)
        assert bot.config.ai_advisor_enabled is False
        run_id, _ = await seed_completed_run(
            bot,
            created_at=BASE_TIME,
            patterns={"chasing extended moves": "proposed"},
            summary={"expectancy_r": "0.18", "trade_count": 142},
        )
        async with make_client(bot) as client:
            response = await client.post(f"/evaluations/{run_id}/advise")
        assert response.status_code == 200
        assert response.json() == {"available": False, "advice": None}

    async def test_advise_unknown_run_is_404(self, database: Database) -> None:
        bot = StubBot(database)
        async with make_client(bot) as client:
            response = await client.post("/evaluations/999999/advise")
        assert response.status_code == 404

    async def test_advise_incomplete_run_is_409(self, database: Database) -> None:
        # A run with no report yet cannot be advised on — advising nothing.
        bot = StubBot(database)
        run_id = await bot.evaluation_store.create_run(
            symbols=["BTC/USDT"],
            timeframes=["1h"],
            config={},
            code_version="test",
            progress_total=10,
            created_at=BASE_TIME,
            strategy="production",
        )
        async with make_client(bot) as client:
            response = await client.post(f"/evaluations/{run_id}/advise")
        assert response.status_code == 409


class TestFindingRecurrence:
    async def test_findings_carry_their_history_across_runs(self, database: Database) -> None:
        bot = StubBot(database)
        old_run, _ = await seed_completed_run(
            bot,
            created_at=BASE_TIME,
            patterns={"entries lose money when trend is down": "accepted"},
        )
        await seed_completed_run(
            bot,
            created_at=BASE_TIME + timedelta(hours=1),
            patterns={
                "entries lose money when trend is down": "proposed",
                "held positions ride into their stops": "proposed",
            },
        )
        async with make_client(bot) as client:
            run_id = (await client.get("/evaluations")).json()[0]["id"]
            findings = (await client.get(f"/evaluations/{run_id}/findings")).json()

        by_pattern = {finding["pattern"]: finding for finding in findings}
        recurring = by_pattern["entries lose money when trend is down"]
        assert recurring["seen_in_prior_runs"] == 1
        assert recurring["first_seen_run_id"] == old_run
        fresh = by_pattern["held positions ride into their stops"]
        assert fresh["seen_in_prior_runs"] == 0
        assert fresh["first_seen_run_id"] is None

    async def test_other_bots_runs_do_not_count_as_history(self, database: Database) -> None:
        bot = StubBot(database)
        await seed_completed_run(
            bot,
            strategy="breakout",
            created_at=BASE_TIME,
            patterns={"entries lose money when trend is down": "proposed"},
        )
        await seed_completed_run(
            bot,
            created_at=BASE_TIME + timedelta(hours=1),
            patterns={"entries lose money when trend is down": "proposed"},
        )

        async with make_client(bot) as client:
            run_id = (await client.get("/evaluations")).json()[0]["id"]
            (finding,) = (await client.get(f"/evaluations/{run_id}/findings")).json()

        assert finding["seen_in_prior_runs"] == 0

    async def test_a_verdict_keeps_the_recurrence_annotations(self, database: Database) -> None:
        """Accepting must not visually reset a recurred pattern to "new"."""
        bot = StubBot(database)
        await seed_completed_run(
            bot,
            created_at=BASE_TIME,
            patterns={"entries lose money when trend is down": "proposed"},
        )
        _, finding_ids = await seed_completed_run(
            bot,
            created_at=BASE_TIME + timedelta(hours=1),
            patterns={"entries lose money when trend is down": "proposed"},
        )
        finding_id = finding_ids["entries lose money when trend is down"]

        async with make_client(bot) as client:
            decided = (await client.post(f"/evaluations/findings/{finding_id}/accept")).json()

        assert decided["status"] == "accepted"
        assert decided["seen_in_prior_runs"] == 1


class TestAcceptTriggeredSweeps:
    async def test_accepting_arms_the_sweep_and_says_so(self, database: Database) -> None:
        bot = StubBot(database)
        run_id, finding_ids = await seed_completed_run(
            bot,
            created_at=BASE_TIME,
            patterns={"entries lose money when trend is down": "proposed"},
        )
        finding_id = finding_ids["entries lose money when trend is down"]

        async with make_client(bot) as client:
            decided = (await client.post(f"/evaluations/findings/{finding_id}/accept")).json()

        assert bot.acceptance_notes == [run_id]
        assert decided["sweep_queued"] is True

    async def test_rejecting_triggers_nothing(self, database: Database) -> None:
        bot = StubBot(database)
        _, finding_ids = await seed_completed_run(
            bot,
            created_at=BASE_TIME,
            patterns={"entries lose money when trend is down": "proposed"},
        )
        finding_id = finding_ids["entries lose money when trend is down"]

        async with make_client(bot) as client:
            decided = (await client.post(f"/evaluations/findings/{finding_id}/reject")).json()

        assert bot.acceptance_notes == []
        assert decided["sweep_queued"] is False

    async def test_findings_carry_their_sweep_chain(self, database: Database) -> None:
        """The card's cause-to-effect chain reads off the sweep's motivation."""
        bot = StubBot(database)
        run_id, finding_ids = await seed_completed_run(
            bot,
            created_at=BASE_TIME,
            patterns={
                "entries lose money when trend is down": "accepted",
                "held positions ride into their stops": "proposed",
            },
        )
        motivated_id = finding_ids["entries lose money when trend is down"]
        sweep_id = await bot.evaluation_store.create_sweep(
            symbol="BTC/USDT",
            timeframe="1h",
            config={},
            motivating_finding_ids=[motivated_id],
            created_at=BASE_TIME + timedelta(hours=1),
        )
        await bot.evaluation_store.complete_sweep(
            sweep_id, {"verdict": "overfit", "winner": "tighter_stop"}
        )

        async with make_client(bot) as client:
            findings = (await client.get(f"/evaluations/{run_id}/findings")).json()

        by_pattern = {finding["pattern"]: finding for finding in findings}
        motivated = by_pattern["entries lose money when trend is down"]
        assert motivated["latest_sweep_id"] == sweep_id
        assert motivated["latest_sweep_status"] == "completed"
        assert motivated["latest_sweep_verdict"] == "overfit"
        bystander = by_pattern["held positions ride into their stops"]
        assert bystander["latest_sweep_id"] is None


class TestResearchTimeline:
    async def test_runs_sweeps_and_promotions_merge_newest_first(self, database: Database) -> None:
        bot = StubBot(database)
        await seed_completed_run(
            bot,
            created_at=BASE_TIME,
            patterns={"entries lose money when trend is down": "accepted"},
        )
        await seed_completed_run(
            bot,
            created_at=BASE_TIME + timedelta(hours=1),
            patterns={"held positions ride into their stops": "proposed"},
        )
        sweep_id = await bot.evaluation_store.create_sweep(
            symbol="BTC/USDT",
            timeframe="1h",
            config={},
            motivating_finding_ids=[1],
            created_at=BASE_TIME + timedelta(hours=2),
        )
        await bot.evaluation_store.complete_sweep(
            sweep_id,
            {
                "verdict": "validated",
                "winner": "tighter_stop",
                "explanation": "tighter_stop beat the baseline",
            },
        )
        version_id = await bot.strategy_settings_store.record(
            "trend_following",
            {"atr_stop_multiple": 1.5},
            BASE_TIME + timedelta(hours=3),
            sweep_id,
            "auto-promoted: tighter_stop beat the baseline",
        )

        async with make_client(bot) as client:
            events = (await client.get("/research/timeline")).json()

        assert [event["kind"] for event in events] == [
            "promotion",
            "sweep",
            "evaluation",
            "evaluation",
        ]
        promotion = events[0]
        assert promotion["version_id"] == version_id
        assert promotion["sweep_id"] == sweep_id
        assert "settings v" in promotion["headline"]
        sweep = events[1]
        assert sweep["verdict"] == "validated"
        assert "motivated by 1 finding(s)" in sweep["detail"]
        newest_run = events[2]
        assert newest_run["expectancy_r"] == "-0.1296"
        # The second run dropped the first run's pattern and mined a new one.
        assert newest_run["new_patterns"] == ["held positions ride into their stops"]
        assert newest_run["resolved_patterns"] == ["entries lose money when trend is down"]

    async def test_limit_caps_the_feed(self, database: Database) -> None:
        bot = StubBot(database)
        for hour in range(3):
            await seed_completed_run(bot, created_at=BASE_TIME + timedelta(hours=hour))

        async with make_client(bot) as client:
            events = (await client.get("/research/timeline?limit=2")).json()
            bad = await client.get("/research/timeline?limit=0")

        assert len(events) == 2
        assert bad.status_code == 422  # validated query bound, not a silent clamp

    async def test_promotion_carries_the_settings_diff(self, database: Database) -> None:
        # Two versions of one family: the newest promotion reads out what
        # changed — only the moved field, before -> after, as display strings.
        bot = StubBot(database)
        await bot.strategy_settings_store.record(
            "trend_following",
            {"atr_stop_multiple": 2.5, "lookback": 20},
            BASE_TIME,
            None,
            "seeded the family",
        )
        await bot.strategy_settings_store.record(
            "trend_following",
            {"atr_stop_multiple": 1.5, "lookback": 20},
            BASE_TIME + timedelta(hours=1),
            None,
            "auto-promoted: tighter_stop beat the baseline",
        )

        async with make_client(bot) as client:
            events = (await client.get("/research/timeline")).json()

        newest = events[0]
        assert newest["kind"] == "promotion"
        # ``lookback`` held at 20, so only the stop multiple shows.
        assert newest["changes"] == [
            {"field": "atr_stop_multiple", "before": "2.5", "after": "1.5"},
        ]


class TestImprovementStatus:
    async def test_status_serializes_the_loop_snapshot(self, database: Database) -> None:
        bot = StubBot(database)
        async with make_client(bot) as client:
            response = await client.get("/improvement")

        assert response.status_code == 200
        body = response.json()
        assert body["enabled"] is True
        assert body["interval_hours"] == 12
        assert body["timeframe"] == "1h"
        assert "kept the active configuration" in body["last_outcome"]
        # Datetimes cross the boundary as ISO-8601 strings.
        assert body["last_cycle_started_at"] == BASE_TIME.isoformat()
        assert body["next_cycle_at"] == (BASE_TIME + timedelta(hours=12)).isoformat()


class TestCampaignStatus:
    async def test_status_serializes_the_campaign_snapshot(self, database: Database) -> None:
        bot = StubBot(database)
        async with make_client(bot) as client:
            response = await client.get("/campaign")

        assert response.status_code == 200
        body = response.json()
        assert body["enabled"] is True
        assert body["max_rounds"] == 8
        campaign = body["campaign"]
        assert campaign["target"] == "momentum" and campaign["status"] == "running"
        assert campaign["timeframe"] == "1h"
        assert campaign["promotions_enabled"] is True
        assert campaign["promotions"] == 1
        assert campaign["rounds"][0]["winner"] == "faster_macd"
        # The promoted round carries the field-level diff (what it changed).
        assert campaign["rounds"][0]["changes"] == [
            {"field": "macd_fast", "before": "12", "after": "8"}
        ]
        # Datetimes cross the boundary as ISO-8601 strings; nulls stay null.
        assert campaign["started_at"] == BASE_TIME.isoformat()
        assert campaign["finished_at"] is None

    async def test_history_lists_past_finished_campaigns(self, database: Database) -> None:
        bot = StubBot(database)
        async with make_client(bot) as client:
            response = await client.get("/campaign/history")

        assert response.status_code == 200
        body = response.json()
        assert len(body) == 1
        past = body[0]
        assert past["target"] == "momentum" and past["symbol"] == "ETH/USDT"
        assert past["timeframe"] == "1d"
        assert past["promotions_enabled"] is False
        assert past["status"] == "completed" and past["promotions"] == 2
        # ISO strings from the persisted snapshot pass straight through.
        assert past["finished_at"] == BASE_TIME.isoformat()
        # The per-promotion diff survives the round trip into the history feed.
        assert past["rounds"][0]["changes"] == [
            {"field": "macd_fast", "before": "12", "after": "8"}
        ]


class TestCampaignSettings:
    async def test_put_toggles_the_loop_and_get_reflects_it(self, database: Database) -> None:
        bot = StubBot(database)
        async with make_client(bot) as client:
            initial = await client.get("/settings/campaign")
            assert initial.status_code == 200
            assert initial.json()["enabled"] is True
            assert initial.json()["max_rounds"] == 8

            put = await client.put("/settings/campaign", json={"enabled": False})
            assert put.status_code == 200 and put.json()["enabled"] is False

            after = await client.get("/settings/campaign")
            assert after.json()["enabled"] is False


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
    async def test_start_without_candidates_derives_the_active_grid(
        self, database: Database
    ) -> None:
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
        # The derived grid rides in the config snapshot, the active
        # configuration first — the baseline every verdict challenges.
        names = [candidate["name"] for candidate in fetched["config"]["candidates"]]
        assert names[0].startswith("active_trend")
        assert len(names) >= 2
        assert fetched["report"] is None

    async def test_findings_of_the_latest_run_steer_the_derived_grid(
        self, database: Database
    ) -> None:
        """A manual sweep challenges the knobs the latest findings point at."""
        from tradebot.evaluation.models import LearningFinding

        bot = StubBot(database)
        config = EvaluationRunConfig(symbols=("BTC/USDT",))
        run_id = await bot.evaluation_store.create_run(
            ["BTC/USDT"], ["1h"], config.model_dump(), "test", 1, BASE_TIME
        )
        await bot.evaluation_store.complete_run(run_id, {})

        def finding(pattern: str) -> LearningFinding:
            return LearningFinding(
                run_id=run_id,
                pattern=pattern,
                evidence_scenario_ids=(1, 2, 3),
                affected_count=3,
                average_r_impact=Decimal("-0.4"),
                suggestion="test",
                confidence="low",
                created_at=BASE_TIME,
            )

        wrong_hold_id = await bot.evaluation_store.insert_finding(
            finding("held positions ride into their stops")
        )
        chase_id = await bot.evaluation_store.insert_finding(
            finding("entries chase moves that are already over")
        )
        await bot.evaluation_store.set_finding_status(chase_id, "rejected")

        async with make_client(bot) as client:
            started = await client.post("/sweeps", json={})
            fetched = (await client.get(f"/sweeps/{started.json()['run_id']}")).json()

        names = {candidate["name"] for candidate in fetched["config"]["candidates"]}
        # The wrong-hold finding adds its stop-management challengers...
        assert {"breakeven_lock", "atr_trailing"} <= names
        # ...the rejected chase finding adds nothing (a human called it noise).
        assert "anti_chase" not in names
        assert fetched["config"]["motivating_finding_ids"] == [wrong_hold_id]

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
        unknown_family = {
            "candidates": [
                {"name": "incumbent", "params": {}},
                {"name": "imaginary", "params": {}, "family": "martingale"},
            ]
        }
        async with make_client(bot) as client:
            bad_timeframe = await client.post("/sweeps", json={"timeframe": "7m"})
            duplicate_names = await client.post("/sweeps", json=duplicate)
            bad_family = await client.post("/sweeps", json=unknown_family)
        assert bad_timeframe.status_code == 400
        assert duplicate_names.status_code == 400
        assert bad_family.status_code == 400

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
        assert isinstance(body[0]["id"], int)  # the cursor for paging
        assert body[0]["price_quote"] == "100"
        assert body[0]["quantity_base"] == "2"
        assert body[0]["value_quote"] == "200"  # price * quantity, fee excluded
        assert body[0]["side"] == "buy"
        assert body[0]["filled_at"] == BASE_TIME.isoformat()

    async def test_fills_are_bounded_by_limit_and_paged_by_cursor(self, database: Database) -> None:
        bot = StubBot(database)
        for index in range(5):
            await bot.fill_store.append(
                make_fill().model_copy(update={"client_order_id": f"ord-{index}"})
            )

        async with make_client(bot) as client:
            newest = (await client.get("/fills", params={"limit": 2})).json()
            # The two newest fills, oldest-first within the page (the journal's
            # own render flips to newest-first).
            assert [fill["client_order_id"] for fill in newest] == ["ord-3", "ord-4"]
            cursor = newest[0]["id"]  # smallest id on the page
            older = (await client.get("/fills", params={"limit": 2, "before_id": cursor})).json()
            assert [fill["client_order_id"] for fill in older] == ["ord-1", "ord-2"]


class TestBotCapital:
    async def test_reset_updates_the_starting_capital(self, database: Database) -> None:
        bot = StubBot(database)
        async with make_client(bot) as client:
            response = await client.put(
                "/bots/production/capital", json={"initial_balance_quote": "5000"}
            )
            assert response.status_code == 200
            rows = (await client.get("/competition")).json()["competitors"]
        production = next(row for row in rows if row["bot_id"] == "production")
        assert production["initial_balance_quote"] == "5000"

    async def test_invalid_capital_is_rejected(self, database: Database) -> None:
        bot = StubBot(database)
        async with make_client(bot) as client:
            negative = await client.put(
                "/bots/production/capital", json={"initial_balance_quote": "-1"}
            )
            zero = await client.put("/bots/production/capital", json={"initial_balance_quote": "0"})
        assert negative.status_code == 400
        assert zero.status_code == 400

    async def test_reset_refuses_while_holding_a_position(self, database: Database) -> None:
        bot = StubBot(database)
        bot.portfolio.apply_fill(make_fill())  # opens a BTC position
        async with make_client(bot) as client:
            response = await client.put(
                "/bots/production/capital", json={"initial_balance_quote": "5000"}
            )
        assert response.status_code == 409

    async def test_unknown_bot_is_404(self, database: Database) -> None:
        bot = StubBot(database)
        async with make_client(bot) as client:
            response = await client.put(
                "/bots/ghost/capital", json={"initial_balance_quote": "5000"}
            )
        assert response.status_code == 404


class TestTradingFees:
    async def test_defaults_are_returned_as_percent_and_bps(self, database: Database) -> None:
        bot = StubBot(database)
        async with make_client(bot) as client:
            body = (await client.get("/settings/fees")).json()

        assert body["buy_fee_percent"] == "0.1"  # 10 bps
        assert body["sell_fee_percent"] == "0.1"
        assert body["buy_fee_bps"] == "10"
        assert body["sell_fee_bps"] == "10"

    async def test_update_converts_percent_to_bps_and_persists(self, database: Database) -> None:
        bot = StubBot(database)
        async with make_client(bot) as client:
            response = await client.put(
                "/settings/fees",
                json={"buy_fee_percent": "0.2", "sell_fee_percent": "0.25"},
            )
            assert response.status_code == 200
            assert response.json()["buy_fee_bps"] == "20"
            assert response.json()["sell_fee_bps"] == "25"
            # The change is reflected on the next read.
            after = (await client.get("/settings/fees")).json()
            assert after["sell_fee_percent"] == "0.25"

    async def test_absurd_fee_is_rejected(self, database: Database) -> None:
        bot = StubBot(database)
        async with make_client(bot) as client:
            response = await client.put(
                "/settings/fees",
                json={"buy_fee_percent": "50", "sell_fee_percent": "0.1"},  # 5000 bps
            )
        assert response.status_code == 400


class TestStrategyVersions:
    async def test_versions_list_newest_first_with_lineage(self, database: Database) -> None:
        bot = StubBot(database)
        first = await bot.strategy_settings_store.record(
            "trend_following", {"fast_ema_period": 10}, BASE_TIME, 7, "auto-promoted"
        )
        async with make_client(bot) as client:
            body = (await client.get("/strategy/versions")).json()

        assert [row["id"] for row in body] == [first]
        assert body[0]["family"] == "trend_following"
        assert body[0]["params"] == {"fast_ema_period": 10}
        assert body[0]["source_sweep_id"] == 7
        assert body[0]["note"] == "auto-promoted"

    async def test_revert_appends_a_new_version(self, database: Database) -> None:
        bot = StubBot(database)
        original = await bot.strategy_settings_store.record(
            "trend_following", {"fast_ema_period": 10}, BASE_TIME
        )
        await bot.strategy_settings_store.record(
            "trend_following", {"fast_ema_period": 15}, BASE_TIME
        )
        async with make_client(bot) as client:
            response = await client.post(f"/strategy/versions/{original}/revert")
            versions = (await client.get("/strategy/versions")).json()

        assert response.status_code == 200
        assert f"reverted to version #{original}" in response.json()["detail"]
        assert versions[0]["params"] == {"fast_ema_period": 10}

    async def test_unknown_version_is_404(self, database: Database) -> None:
        bot = StubBot(database)
        async with make_client(bot) as client:
            assert (await client.post("/strategy/versions/9999/revert")).status_code == 404


class TestWallet:
    async def test_flat_account_holds_only_the_quote_currency(self, database: Database) -> None:
        bot = StubBot(database)
        async with make_client(bot) as client:
            body = (await client.get("/wallet")).json()

        assert body["quote_currency"] == "USDT"
        assert body["equity_quote"] == "10000"
        (usdt,) = body["holdings"]
        assert usdt["asset"] == "USDT"
        assert usdt["quantity"] == "10000"
        assert usdt["value_quote"] == "10000"

    async def test_open_position_is_listed_with_its_marked_value(self, database: Database) -> None:
        bot = StubBot(database)
        bot.portfolio.apply_fill(make_fill(price="100", quantity="2"))  # 2 BTC @ 100
        await bot.candle_store.insert_batch([make_candle(close="110")])

        async with make_client(bot) as client:
            body = (await client.get("/wallet")).json()

        usdt, btc = body["holdings"]
        assert usdt["asset"] == "USDT"
        assert usdt["quantity"] == "9799.8"  # 10000 - 200 - 0.2 fee
        assert btc["asset"] == "BTC"
        assert btc["symbol"] == "BTC/USDT"
        assert btc["quantity"] == "2"
        assert btc["mark_price_quote"] == "110"
        assert btc["value_quote"] == "220"
        assert Decimal(btc["unrealized_pnl_quote"]) == Decimal("19.8")  # incl. entry fee
        assert Decimal(body["equity_quote"]) == Decimal("10019.8")

    async def test_position_without_a_mark_is_listed_not_hidden(self, database: Database) -> None:
        """No candles yet: the coin still shows, its value honestly unknown."""
        bot = StubBot(database)
        bot.portfolio.apply_fill(make_fill(price="100", quantity="2"))

        async with make_client(bot) as client:
            body = (await client.get("/wallet")).json()

        _, btc = body["holdings"]
        assert btc["quantity"] == "2"
        assert btc["value_quote"] is None
        assert body["equity_quote"] is None


class TestRiskStatePersistsOnCommands:
    async def test_pause_resume_kill_persist_synchronously(self, database: Database) -> None:
        """A halt must reach Postgres before the command returns."""
        bot = StubBot(database)
        async with make_client(bot) as client:
            await client.post("/pause")
            await client.post("/resume")
            await client.post("/kill")
        assert bot.risk_state_persists == 3


class TestDivergenceEndpoint:
    async def test_report_for_a_traded_coin(self, database: Database) -> None:
        async with make_client(StubBot(database)) as client:
            response = await client.get("/coins/BTC/USDT/divergence", params={"hours": 12})
        assert response.status_code == 200
        body = response.json()
        assert body["divergence_fraction"] == 0.0
        assert body["mismatches"] == []

    async def test_untraded_coin_is_404(self, database: Database) -> None:
        async with make_client(StubBot(database)) as client:
            response = await client.get("/coins/DOGE/USDT/divergence")
        assert response.status_code == 404

    async def test_status_carries_the_protective_stop_level(self, database: Database) -> None:
        async with make_client(StubBot(database)) as client:
            response = await client.get("/status")
        assert response.status_code == 200
        assert "protective_stop_quote" in response.json()


class TestRegimeVisibility:
    async def test_status_reports_the_gate_disabled_when_absent(self, database: Database) -> None:
        async with make_client(StubBot(database)) as client:
            response = await client.get("/status")
        regime = response.json()["regime"]
        assert regime == {
            "enabled": False,
            "symbol": None,
            "label": None,
            "reasons": [],
            "reason": None,
        }

    async def test_status_explains_an_ungated_gate_when_reference_is_missing(
        self, database: Database
    ) -> None:
        bot = StubBot(database)
        bot.regime_disabled_reason = "reference market BTC/USDT is not among the traded coins"
        async with make_client(bot) as client:
            regime = (await client.get("/status")).json()["regime"]
        assert regime["enabled"] is False
        assert regime["reason"] == "reference market BTC/USDT is not among the traded coins"


class TestDataHealthVisibility:
    async def test_status_reports_a_healthy_feed_by_default(self, database: Database) -> None:
        async with make_client(StubBot(database)) as client:
            health = (await client.get("/status")).json()["data_health"]
        assert health == {"healthy": True, "reason": None}

    async def test_status_surfaces_a_degraded_feed_with_its_reason(
        self, database: Database
    ) -> None:
        bot = StubBot(database)
        bot.feed_healths["BTC/USDT"] = _StubFeedHealth(False, "backfill failed: ConnectionError")
        async with make_client(bot) as client:
            health = (await client.get("/status")).json()["data_health"]
        assert health == {"healthy": False, "reason": "backfill failed: ConnectionError"}


class TestCompetitionEndpoint:
    async def test_leaderboard_serializes_amounts_as_strings(self, database: Database) -> None:
        bot = StubBot(database)
        async with make_client(bot) as client:
            response = await client.get("/competition")

        assert response.status_code == 200
        body = response.json()
        assert body["quote_currency"] == "USDT"
        (row,) = body["competitors"]
        assert row["bot_id"] == "production" and row["is_production"] is True
        assert row["equity_quote"] == "10000"
        assert row["initial_balance_quote"] == "10000"
        assert row["return_fraction"] == "0"
        assert row["entry_fills"] == 0 and row["exit_fills"] == 0


class TestRoutingCandidacyEndpoint:
    async def test_grades_each_research_family_flagging_not_flipping(
        self, database: Database
    ) -> None:
        bot = StubBot(database)
        async with make_client(bot) as client:
            response = await client.get("/research/candidacy")

        assert response.status_code == 200
        body = response.json()
        assert [row["family"] for row in body] == ["breakout", "momentum", "squeeze"]
        for row in body:
            # An empty record: no family is a candidate, and every condition
            # reports a plain-words reason why not.
            assert row["is_candidate"] is False
            for key in ("validated_edge", "beats_incumbent", "live_paper"):
                assert row[key]["met"] is False
                assert row[key]["detail"]


class TestComparisonEndpoints:
    async def test_compare_creates_one_grouped_run_per_lineup_entry(
        self, database: Database
    ) -> None:
        bot = StubBot(database)
        async with make_client(bot) as client:
            started = await client.post("/evaluations/compare", json={})
            assert started.status_code == 200
            body = started.json()
            assert len(body["run_ids"]) == len(LINEUP)
            assert body["group_id"] == body["run_ids"][0]

            listed = await client.get("/evaluations/comparisons")

        assert listed.status_code == 200
        (batch,) = listed.json()
        assert batch["group_id"] == body["group_id"]
        assert [run["id"] for run in batch["runs"]] == body["run_ids"]
        assert [run["strategy"] for run in batch["runs"]] == [spec.bot_id for spec in LINEUP]
        assert all(run["comparison_group"] == body["group_id"] for run in batch["runs"])

    async def test_no_comparisons_is_an_empty_list(self, database: Database) -> None:
        async with make_client(StubBot(database)) as client:
            response = await client.get("/evaluations/comparisons")
        assert response.status_code == 200
        assert response.json() == []


class TestBakeOffEndpoints:
    async def test_start_creates_a_job_and_it_is_fetchable(self, database: Database) -> None:
        bot = StubBot(database)
        async with make_client(bot) as client:
            started = await client.post(
                "/research/bakeoff",
                json={"symbols": ["BTC/USDT"], "grid": {"1h": [50], "4h": [50]}},
            )
            assert started.status_code == 200
            body = started.json()
            assert body["cells_total"] == 2  # 2 timeframes x 1 window each
            job_id = body["job_id"]

            fetched = await client.get(f"/research/bakeoff/{job_id}")
            listed = await client.get("/research/bakeoffs")

        assert fetched.status_code == 200
        job = fetched.json()
        assert job["id"] == job_id
        assert job["cells_total"] == 2
        assert job["status"] == "pending"
        assert listed.status_code == 200
        assert [row["id"] for row in listed.json()] == [job_id]

    async def test_a_bad_timeframe_is_rejected(self, database: Database) -> None:
        async with make_client(StubBot(database)) as client:
            response = await client.post(
                "/research/bakeoff", json={"symbols": ["BTC/USDT"], "grid": {"7s": [50]}}
            )
        assert response.status_code == 400

    async def test_an_invalid_scenario_count_is_a_clean_400(self, database: Database) -> None:
        # scenario_count parses as an int but fails BakeOffConfig's gt=0
        # constraint: a 400, never a leaked 500 from the ValidationError.
        async with make_client(StubBot(database)) as client:
            response = await client.post(
                "/research/bakeoff", json={"symbols": ["BTC/USDT"], "scenario_count": 0}
            )
        assert response.status_code == 400

    async def test_unknown_job_is_404(self, database: Database) -> None:
        async with make_client(StubBot(database)) as client:
            response = await client.get("/research/bakeoff/9999")
        assert response.status_code == 404

    async def test_no_bake_offs_is_an_empty_list(self, database: Database) -> None:
        async with make_client(StubBot(database)) as client:
            response = await client.get("/research/bakeoffs")
        assert response.status_code == 200
        assert response.json() == []


class TestBotManagementEndpoints:
    async def test_builder_options_come_from_the_real_registry(self, database: Database) -> None:
        async with make_client(StubBot(database)) as client:
            response = await client.get("/bots/options")

        assert response.status_code == 200
        body = response.json()
        families = {option["family"] for option in body["families"]}
        assert {"trend_following", "mean_reversion", "breakout", "momentum", "squeeze"} <= families
        assert body["entry_modes"] == ["any", "all"]
        assert all(option["description"] for option in body["families"])
        assert all(option["defaults"] for option in body["families"])

    async def test_custom_bot_crud_and_controls(self, database: Database) -> None:
        bot = StubBot(database)
        async with make_client(bot) as client:
            created = await client.post(
                "/bots",
                json={"name": "Dip Buyer", "rules": {"families": {"mean_reversion": {}}}},
            )
            assert created.status_code == 200
            bot_id = created.json()["bot_id"]
            assert bot_id == "custom-dip-buyer"

            duplicate = await client.post(
                "/bots",
                json={"name": "Dip Buyer", "rules": {"families": {"mean_reversion": {}}}},
            )
            assert duplicate.status_code == 400
            bad_rules = await client.post(
                "/bots", json={"name": "Bad", "rules": {"families": {"martingale": {}}}}
            )
            assert bad_rules.status_code == 400

            detail = await client.get(f"/bots/{bot_id}")
            assert detail.status_code == 200
            assert detail.json()["summary"]["kind"] == "custom"
            assert detail.json()["strategy"]["kind"] == "custom"

            paused = await client.post(f"/bots/{bot_id}/pause")
            assert paused.status_code == 200 and paused.json()["paused"] is True
            leaderboard = await client.get("/competition")
            row = next(
                entry for entry in leaderboard.json()["competitors"] if entry["bot_id"] == bot_id
            )
            assert row["paused"] is True
            assert (await client.post(f"/bots/{bot_id}/resume")).status_code == 200

            updated = await client.put(
                f"/bots/{bot_id}/rules",
                json={"rules": {"entry_mode": "all", "families": {"momentum": {}}}},
            )
            assert updated.status_code == 200

            assert (await client.post(f"/bots/{bot_id}/kill")).status_code == 200
            assert (await client.delete(f"/bots/{bot_id}")).status_code == 200
            assert (await client.get(f"/bots/{bot_id}")).status_code == 404

    async def test_unknown_bot_journal_scope_is_404(self, database: Database) -> None:
        async with make_client(StubBot(database)) as client:
            response = await client.get("/fills", params={"bot": "custom-ghost"})
        assert response.status_code == 404
