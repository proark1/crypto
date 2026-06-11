"""Runner and manager tests against a real database."""

import asyncio
import os
from collections.abc import AsyncIterator, Coroutine
from datetime import timedelta
from decimal import Decimal
from typing import Any

import pytest

from tradebot.core.models import Candle, CandleInterval, utc_now
from tradebot.evaluation import ScenarioEvaluator
from tradebot.evaluation.runner import EvaluationManager, EvaluationRunConfig, EvaluationRunner
from tradebot.persistence import CandleStore, Database, EvaluationStore
from tradebot.persistence.database import metadata
from tradebot.strategies import TrendFollowingConfig, TrendFollowingStrategy

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


async def seed_candles(database: Database, count: int = 600) -> None:
    """Recent synthetic 1m candles (the runner fetches relative to now)."""
    store = CandleStore(database)
    end = utc_now().replace(second=0, microsecond=0)
    candles = []
    price = 100.0
    for index in range(count):
        open_time = end - timedelta(minutes=count - index)
        drift = 0.003 if (index // 80) % 2 == 0 else -0.002
        previous = price
        price = max(1.0, price * (1.0 + drift + (0.0005 if index % 2 == 0 else -0.0005)))
        candles.append(
            Candle(
                symbol="BTC/USDT",
                interval=CandleInterval.M1,
                open_time=open_time,
                close_time=open_time + timedelta(minutes=1),
                open_quote=Decimal(str(round(previous, 8))),
                high_quote=Decimal(str(round(max(previous, price) + 0.1, 8))),
                low_quote=Decimal(str(round(min(previous, price) - 0.1, 8))),
                close_quote=Decimal(str(round(price, 8))),
                volume_base=Decimal("1"),
            )
        )
    await store.insert_batch(candles)


def make_runner(database: Database) -> tuple[EvaluationRunner, EvaluationStore]:
    store = EvaluationStore(database)
    runner = EvaluationRunner(
        CandleStore(database),
        store,
        # Every strategy id grades the same shape here: these tests cover
        # orchestration, not the lineup mapping (the worker owns that).
        lambda _strategy_id: ScenarioEvaluator(
            lambda: TrendFollowingStrategy(
                TrendFollowingConfig(fast_ema_period=5, slow_ema_period=12, atr_period=5)
            )
        ),
    )
    return runner, store


CONFIG = EvaluationRunConfig(
    symbols=("BTC/USDT",),
    timeframes=("1m",),
    history_days=2,
    scenario_count=12,
    lookback_candles=60,
    horizon_candles=30,
    seed=7,
)


def make_manager(
    runner: EvaluationRunner, store: EvaluationStore
) -> tuple[EvaluationManager, list[asyncio.Task[None]]]:
    tasks: list[asyncio.Task[None]] = []

    def spawn(coroutine: Coroutine[Any, Any, None]) -> asyncio.Task[None]:
        task = asyncio.create_task(coroutine)
        tasks.append(task)
        return task

    return EvaluationManager(runner, store, code_version="test", spawn=spawn), tasks


class TestRunner:
    async def test_run_completes_with_persisted_scenarios_and_summary(
        self, database: Database
    ) -> None:
        await seed_candles(database)
        runner, store = make_runner(database)
        run_id = await store.create_run(
            ["BTC/USDT"], ["1m"], CONFIG.model_dump(), "test", 12, utc_now()
        )

        await runner.execute(run_id, CONFIG)

        run = await store.fetch_run(run_id)
        assert run is not None and run["status"] == "completed"
        scenarios = await store.fetch_scenarios(run_id)
        results = await store.fetch_results(run_id)
        assert len(scenarios) == len(results) == CONFIG.scenario_count
        assert run["progress_done"] == CONFIG.scenario_count
        summary = run["summary"]
        assert summary["scenario_count"] == CONFIG.scenario_count
        assert "verdicts" in summary and "by_trend" in summary
        # Mining ran as part of completion; whatever it found is proposed,
        # never auto-accepted — the verdict belongs to a human.
        findings = await store.fetch_findings(run_id)
        assert all(finding.status == "proposed" for _, finding in findings)

    async def test_run_with_no_candles_fails_instead_of_quietly_completing(
        self, database: Database
    ) -> None:
        """All series empty is a data problem; "completed, 0 scenarios" hides it."""
        runner, store = make_runner(database)
        run_id = await store.create_run(
            ["BTC/USDT"], ["1m"], CONFIG.model_dump(), "test", 12, utc_now()
        )

        await runner.execute(run_id, CONFIG)

        run = await store.fetch_run(run_id)
        assert run is not None and run["status"] == "failed"

    async def test_partially_skipped_series_still_complete(self, database: Database) -> None:
        """One starved timeframe skips; the series with data still grade."""
        await seed_candles(database)  # 600 minutes: plenty for 1m, 10 buckets on 1h
        runner, store = make_runner(database)
        config = CONFIG.model_copy(update={"timeframes": ("1m", "1h")})
        run_id = await store.create_run(
            ["BTC/USDT"], ["1m", "1h"], config.model_dump(), "test", 24, utc_now()
        )

        await runner.execute(run_id, config)

        run = await store.fetch_run(run_id)
        assert run is not None and run["status"] == "completed"
        assert run["summary"]["scenario_count"] == CONFIG.scenario_count  # 1m only
        assert "1h" not in run["summary"]["by_timeframe"]


class TestManager:
    async def test_one_run_at_a_time(self, database: Database) -> None:
        await seed_candles(database)
        runner, store = make_runner(database)
        manager, tasks = make_manager(runner, store)

        run_id = await manager.start(CONFIG)
        with pytest.raises(RuntimeError, match="already in progress"):
            await manager.start(CONFIG)
        await asyncio.gather(*tasks)

        run = await store.fetch_run(run_id)
        assert run is not None and run["status"] == "completed"
        # A finished run no longer blocks the next one.
        second = await manager.start(CONFIG)
        await asyncio.gather(*[task for task in tasks if not task.done()])
        assert second != run_id

    async def test_cancel_marks_the_run_interrupted(self, database: Database) -> None:
        await seed_candles(database)
        runner, store = make_runner(database)
        manager, tasks = make_manager(runner, store)

        run_id = await manager.start(CONFIG)
        assert manager.cancel(run_id) is True  # before the task ever ran
        with pytest.raises(asyncio.CancelledError):
            await tasks[0]
        # The reconciliation task the canceller scheduled must finish too.
        await asyncio.gather(*(task for task in tasks if not task.done()))

        run = await store.fetch_run(run_id)
        assert run is not None and run["status"] == "interrupted"
        assert manager.cancel(run_id) is False  # nothing in flight any more

    async def test_bad_timeframe_fails_before_any_row_exists(self, database: Database) -> None:
        runner, store = make_runner(database)
        manager, _ = make_manager(runner, store)
        with pytest.raises(ValueError):
            await manager.start(CONFIG.model_copy(update={"timeframes": ("7m",)}))
        assert await store.list_runs() == []


class TestComparison:
    async def test_runs_share_a_group_a_frozen_window_and_identical_scenarios(
        self, database: Database
    ) -> None:
        await seed_candles(database)
        runner, store = make_runner(database)
        manager, tasks = make_manager(runner, store)

        run_ids = await manager.start_comparison(CONFIG, ["production", "trend_following"])
        await asyncio.gather(*tasks)

        lead = await store.fetch_run(run_ids[0])
        second = await store.fetch_run(run_ids[1])
        assert lead is not None and second is not None
        assert lead["comparison_group"] == lead["id"]
        assert second["comparison_group"] == lead["id"]
        assert lead["strategy"] == "production"
        assert second["strategy"] == "trend_following"
        assert lead["status"] == second["status"] == "completed"
        # One frozen window end: "now" moving between members would shift
        # the scenario coordinates and break the comparison's premise.
        assert lead["config"]["window_end"] == second["config"]["window_end"]
        lead_scenarios = await store.fetch_scenarios(run_ids[0])
        second_scenarios = await store.fetch_scenarios(run_ids[1])
        assert [scenario.decision_time for _, scenario in lead_scenarios] == [
            scenario.decision_time for _, scenario in second_scenarios
        ]
        # The store hands batches back grouped, members in creation order.
        (batch,) = await store.list_comparisons()
        assert [run["id"] for run in batch] == run_ids

    async def test_comparison_holds_the_single_flight_slot(self, database: Database) -> None:
        await seed_candles(database)
        runner, store = make_runner(database)
        manager, tasks = make_manager(runner, store)

        await manager.start_comparison(CONFIG, ["production"])
        with pytest.raises(RuntimeError, match="already in progress"):
            await manager.start(CONFIG)
        await asyncio.gather(*tasks)

    async def test_cancel_interrupts_every_member_of_the_batch(self, database: Database) -> None:
        await seed_candles(database)
        runner, store = make_runner(database)
        manager, tasks = make_manager(runner, store)

        run_ids = await manager.start_comparison(CONFIG, ["production", "trend_following"])
        # Cancelling any member kills the batch: half a comparison cannot
        # answer the question the batch asked.
        assert manager.cancel(run_ids[1]) is True
        with pytest.raises(asyncio.CancelledError):
            await tasks[0]
        await asyncio.gather(*(task for task in tasks if not task.done()))

        for run_id in run_ids:
            run = await store.fetch_run(run_id)
            assert run is not None and run["status"] == "interrupted"
