"""Sweep tests: winner selection, verdict phrasing, and the full walk-forward."""

import asyncio
import os
from collections.abc import AsyncIterator, Coroutine
from datetime import timedelta
from decimal import Decimal
from typing import Any

import pytest

from tradebot.core.models import Candle, CandleInterval, utc_now
from tradebot.evaluation.sweep import (
    MIN_SWEEP_TRADES,
    CandidateScore,
    SweepCandidate,
    SweepConfig,
    SweepManager,
    SweepRunner,
    build_candidate_strategy,
    select_winner,
    validation_verdict,
)
from tradebot.persistence import CandleStore, Database, EvaluationStore
from tradebot.persistence.database import metadata

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


def score(name: str, r_values: list[str]) -> CandidateScore:
    return CandidateScore(
        candidate=SweepCandidate(name=name, params={}),
        scenario_count=len(r_values),
        r_values=tuple(Decimal(value) for value in r_values),
    )


def enough(value: str) -> list[str]:
    return [value] * MIN_SWEEP_TRADES


class TestSelectWinner:
    def test_best_expectancy_with_enough_trades_wins(self) -> None:
        baseline = score("baseline", enough("0.1"))
        better = score("better", enough("0.5"))
        thin = score("thin_but_great", ["9.9"])  # too few trades to trust

        winner = select_winner([baseline, better, thin])

        assert winner is not None and winner.candidate.name == "better"

    def test_ties_keep_the_baseline(self) -> None:
        baseline = score("baseline", enough("0.3"))
        equal = score("equal", enough("0.3"))

        winner = select_winner([baseline, equal])

        assert winner is not None and winner.candidate.name == "baseline"

    def test_no_eligible_candidate_returns_none(self) -> None:
        assert select_winner([score("thin", ["1.0", "2.0"])]) is None


class TestValidationVerdict:
    def test_winner_that_holds_up_significantly_is_validated(self) -> None:
        verdict, explanation, significance = validation_verdict(
            baseline=score("baseline", enough("0.1")),
            winner=score("challenger", enough("0.4")),
            comparisons=4,
            seed=7,
        )
        assert verdict == "validated"
        assert "survived walk-forward" in explanation
        assert significance["comparisons"] == 4
        assert Decimal(significance["p_value"]) <= Decimal(significance["corrected_threshold"])

    def test_winner_that_collapses_is_called_overfit_in_plain_words(self) -> None:
        verdict, explanation, _ = validation_verdict(
            baseline=score("baseline", enough("0.2")),
            winner=score("challenger", enough("-0.3")),
            comparisons=4,
            seed=7,
        )
        assert verdict == "overfit"
        assert "wins only on the data it was tuned on" in explanation
        assert "keep baseline" in explanation

    def test_too_few_validation_trades_is_insufficient_evidence(self) -> None:
        verdict, explanation, significance = validation_verdict(
            baseline=score("baseline", enough("0.1")),
            winner=score("challenger", ["2.0"]),
            comparisons=4,
            seed=7,
        )
        assert verdict == "insufficient_evidence"
        assert "not enough evidence" in explanation
        assert significance["p_value"] is None

    def test_a_noisy_edge_fails_the_corrected_significance_test(self) -> None:
        # Mean +0.1R vs 0.0R, but the spread dwarfs the edge — the point
        # estimate "wins" while the bootstrap cannot tell it from luck.
        noisy_winner = score("challenger", ["1.1", "-0.9"] * (MIN_SWEEP_TRADES // 2))
        flat_baseline = score("baseline", ["1.0", "-1.0"] * (MIN_SWEEP_TRADES // 2))

        verdict, explanation, significance = validation_verdict(
            baseline=flat_baseline, winner=noisy_winner, comparisons=5, seed=7
        )

        assert verdict == "insufficient_evidence"
        assert "not distinguishable from luck" in explanation
        assert Decimal(significance["p_value"]) > Decimal(significance["corrected_threshold"])

    def test_a_baseline_too_thin_to_test_against_is_never_called_proven(self) -> None:
        verdict, explanation, significance = validation_verdict(
            baseline=score("baseline", ["0.1"]),
            winner=score("challenger", enough("0.4")),
            comparisons=4,
            seed=7,
        )
        assert verdict == "insufficient_evidence"
        assert "too few to test" in explanation
        assert significance["p_value"] is None


class TestBuildCandidateStrategy:
    def test_unknown_parameter_raises_instead_of_being_ignored(self) -> None:
        with pytest.raises(ValueError, match="unknown trend_following parameters"):
            SweepCandidate(name="typo", params={"fast_ema_perod": 10})  # typo on purpose

    def test_unknown_family_raises_with_the_known_ones(self) -> None:
        with pytest.raises(ValueError, match="unknown strategy family"):
            SweepCandidate(name="bad", family="martingale", params={})

    def test_each_family_builds_its_own_strategy(self) -> None:
        trend = build_candidate_strategy(
            SweepCandidate(name="t", params={"fast_ema_period": 10, "slow_ema_period": 30})
        )
        reversion = build_candidate_strategy(
            SweepCandidate(name="r", family="mean_reversion", params={"rsi_period": 7})
        )
        assert trend.name == "trend_following"
        assert reversion.name == "mean_reversion"


class TestSweepConfig:
    def test_duplicate_candidate_names_are_rejected(self) -> None:
        candidate = SweepCandidate(name="same", params={})
        with pytest.raises(ValueError, match="unique"):
            SweepConfig(symbol="BTC/USDT", candidates=(candidate, candidate))


async def seed_candles(database: Database, count: int = 800) -> None:
    """Recent synthetic 1m candles with alternating drift regimes."""
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


CONFIG = SweepConfig(
    symbol="BTC/USDT",
    timeframe="1m",
    history_days=2,
    scenario_count=10,
    lookback_candles=60,
    horizon_candles=30,
    seed=7,
    training_fraction=0.6,
    validation_windows=2,
    candidates=(
        SweepCandidate(name="baseline", params={"fast_ema_period": 5, "slow_ema_period": 12}),
        SweepCandidate(name="slower", params={"fast_ema_period": 8, "slow_ema_period": 21}),
        SweepCandidate(
            name="reverter",
            family="mean_reversion",
            params={"rsi_period": 5, "atr_period": 5},
        ),
    ),
    motivating_finding_ids=(42,),
)


def make_manager(
    database: Database,
) -> tuple[SweepManager, EvaluationStore, list[asyncio.Task[None]]]:
    store = EvaluationStore(database)
    runner = SweepRunner(CandleStore(database), store, build_candidate_strategy)
    tasks: list[asyncio.Task[None]] = []

    def spawn(coroutine: Coroutine[Any, Any, None]) -> asyncio.Task[None]:
        task = asyncio.create_task(coroutine)
        tasks.append(task)
        return task

    return SweepManager(runner, store, spawn=spawn), store, tasks


class TestSweepEndToEnd:
    async def test_sweep_completes_with_a_walk_forward_report(self, database: Database) -> None:
        await seed_candles(database)
        manager, store, tasks = make_manager(database)

        sweep_id = await manager.start(CONFIG)
        await asyncio.gather(*tasks)

        sweep = await store.fetch_sweep(sweep_id)
        assert sweep is not None and sweep["status"] == "completed"
        assert sweep["motivating_finding_ids"] == [42]
        report = sweep["report"]
        assert report["baseline"] == "baseline"
        assert set(report["training"]) == {"baseline", "slower", "reverter"}
        assert report["verdict"] in {
            "validated",
            "overfit",
            "baseline_best",
            "insufficient_evidence",
        }
        assert report["explanation"]
        assert report["split"]["training_candles"] > report["split"]["validation_candles"]
        assert len(report["split"]["validation_windows"]) == 2
        # Every candidate block names its family and carries the bootstrap
        # interval slot (None when too few trades).
        for block in report["training"].values():
            assert block["family"] in {"trend_following", "mean_reversion"}
            assert "expectancy_ci_r" in block
        # Validation never scores the full grid: at most challenger + baseline.
        assert set(report["validation"]) <= {"baseline", "slower", "reverter"}
        if report["validation"]:
            assert len(report["validation_by_window"]) == 2
            assert set(report["significance"]) == {
                "comparisons",
                "corrected_threshold",
                "p_value",
            }

    async def test_one_sweep_at_a_time(self, database: Database) -> None:
        await seed_candles(database)
        manager, store, tasks = make_manager(database)

        sweep_id = await manager.start(CONFIG)
        with pytest.raises(RuntimeError, match="already in progress"):
            await manager.start(CONFIG)
        await asyncio.gather(*tasks)

        sweep = await store.fetch_sweep(sweep_id)
        assert sweep is not None and sweep["status"] == "completed"

    async def test_cancel_marks_the_sweep_interrupted(self, database: Database) -> None:
        await seed_candles(database)
        manager, store, tasks = make_manager(database)

        sweep_id = await manager.start(CONFIG)
        assert manager.cancel(sweep_id) is True
        with pytest.raises(asyncio.CancelledError):
            await tasks[0]
        await asyncio.gather(*(task for task in tasks if not task.done()))

        sweep = await store.fetch_sweep(sweep_id)
        assert sweep is not None and sweep["status"] == "interrupted"
        assert manager.cancel(sweep_id) is False

    async def test_too_little_history_fails_honestly(self, database: Database) -> None:
        """A sweep that cannot host its scenarios is failed, never half-done."""
        manager, store, tasks = make_manager(database)

        sweep_id = await manager.start(CONFIG)
        await asyncio.gather(*tasks)

        sweep = await store.fetch_sweep(sweep_id)
        assert sweep is not None and sweep["status"] == "failed"
        assert sweep["report"] is None
