"""Sweep tests: winner selection, verdict phrasing, and the full walk-forward."""

import asyncio
import os
from collections.abc import AsyncIterator, Coroutine
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any, cast

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
        # estimate "wins" while the bootstrap cannot tell it from luck. The R
        # series is clustered (a run of wins then a run of losses), so the
        # moving-block bootstrap keeps the runs intact and sees the real,
        # large spread rather than an i.i.d. shuffle that would understate it.
        noisy_winner = score(
            "challenger",
            ["1.0", "1.1", "0.9", "1.0", "1.0", "-0.9", "-0.8", "-0.9", "-0.8", "-0.6"],
        )
        flat_baseline = score(
            "baseline", ["1.0", "1.0", "0.9", "1.0", "1.1", "-1.0", "-1.0", "-1.0", "-1.0", "-1.0"]
        )

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


class TestRecipeCandidates:
    def test_a_single_family_recipe_builds_that_family_bare(self) -> None:
        candidate = SweepCandidate(
            name="active_recipe",
            recipe={"entry_mode": "any", "families": {"breakout": {}}},
        )
        strategy = build_candidate_strategy(candidate)
        assert strategy.name == "breakout"  # not wrapped in a composite

    def test_a_multi_family_recipe_builds_a_composite(self) -> None:
        candidate = SweepCandidate(
            name="active_recipe",
            recipe={
                "entry_mode": "all",
                "families": {"trend_following": {}, "momentum": {}},
            },
        )
        strategy = build_candidate_strategy(candidate)
        assert strategy.name.startswith("composite[all:")

    def test_a_recipe_candidate_must_not_also_carry_family_params(self) -> None:
        with pytest.raises(ValueError, match="must not also carry"):
            SweepCandidate(
                name="bad",
                params={"fast_ema_period": 10},
                recipe={"entry_mode": "any", "families": {"trend_following": {}}},
            )

    def test_a_recipe_with_a_typoed_parameter_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="unknown breakout parameters"):
            SweepCandidate(
                name="bad",
                recipe={"entry_mode": "any", "families": {"breakout": {"channel_perod": 20}}},
            )

    def test_an_empty_recipe_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="at least one family"):
            SweepCandidate(name="bad", recipe={"entry_mode": "any", "families": {}})


class TestSweepConfig:
    def test_duplicate_candidate_names_are_rejected(self) -> None:
        candidate = SweepCandidate(name="same", params={})
        with pytest.raises(ValueError, match="unique"):
            SweepConfig(symbol="BTC/USDT", candidates=(candidate, candidate))

    def test_cost_multipliers_must_worsen_costs(self) -> None:
        candidates = (SweepCandidate(name="a", params={}), SweepCandidate(name="b", params={}))
        with pytest.raises(ValueError, match="must each exceed"):
            SweepConfig(symbol="BTC/USDT", candidates=candidates, cost_multipliers=(1.0,))
        ok = SweepConfig(symbol="BTC/USDT", candidates=candidates, cost_multipliers=(1.5, 2.0))
        assert ok.cost_multipliers == (1.5, 2.0)
        # Off by default — the auto-improver's frequent sweeps stay cheap.
        assert SweepConfig(symbol="BTC/USDT", candidates=candidates).cost_multipliers == ()

    def test_window_end_defaults_to_none_and_can_be_frozen(self) -> None:
        candidates = (SweepCandidate(name="a", params={}), SweepCandidate(name="b", params={}))
        assert SweepConfig(symbol="BTC/USDT", candidates=candidates).window_end is None
        boundary = utc_now()
        frozen = SweepConfig(symbol="BTC/USDT", candidates=candidates, window_end=boundary)
        assert frozen.window_end == boundary
        assert frozen.model_dump()["window_end"] == boundary
        # UtcDatetime enforces CLAUDE.md invariant 2: a naive end is rejected.
        with pytest.raises(ValueError, match="naive datetime is not allowed"):
            SweepConfig(symbol="BTC/USDT", candidates=candidates, window_end=datetime.now())


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
        # No cost-sensitivity block unless the sweep was asked for one.
        assert "cost_sensitivity" not in report

    async def test_cost_sensitivity_rides_along_when_requested(self, database: Database) -> None:
        await seed_candles(database)
        manager, store, tasks = make_manager(database)

        sweep_id = await manager.start(CONFIG.model_copy(update={"cost_multipliers": (1.5, 2.0)}))
        await asyncio.gather(*tasks)

        sweep = await store.fetch_sweep(sweep_id)
        assert sweep is not None and sweep["status"] == "completed"
        report = sweep["report"]
        # The block appears exactly when a challenger reached validation (the
        # configuration a promotion would adopt); baseline-best / insufficient
        # runs return before there is a winner to stress.
        if report["validation"]:
            cost = report["cost_sensitivity"]
            assert [point["multiplier"] for point in cost["points"]] == ["1", "1.5", "2"]
            assert isinstance(cost["survives_worse_costs"], bool)
        else:
            assert "cost_sensitivity" not in report

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


class _RecordingCandleStore:
    """A ``fetch_range`` that records its window and honors it like the store.

    Stands in for ``CandleStore`` so the boundary test needs no Postgres:
    ``SweepRunner._run`` reads only the candle store, so a fake that filters
    on the requested ``[start, end)`` proves the sweep asked for the right
    window and never saw a candle at or after it.
    """

    def __init__(self, candles: list[Candle]) -> None:
        self._candles = sorted(candles, key=lambda candle: candle.open_time)
        self.start: datetime | None = None
        self.end: datetime | None = None
        self.returned: list[Candle] = []

    async def fetch_range(
        self, symbol: str, interval: CandleInterval, start: datetime, end: datetime
    ) -> list[Candle]:
        self.start, self.end = start, end
        self.returned = [candle for candle in self._candles if start <= candle.open_time < end]
        return self.returned


def _minute_candles(last_open: datetime, count: int) -> list[Candle]:
    """``count`` ascending 1m candles, the last opening at ``last_open``."""
    candles: list[Candle] = []
    price = 100.0
    for index in range(count):
        open_time = last_open - timedelta(minutes=count - 1 - index)
        drift = 0.003 if (index // 40) % 2 == 0 else -0.002
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
    return candles


def _runner_with(candles: list[Candle]) -> tuple[SweepRunner, _RecordingCandleStore]:
    """A runner over an in-memory candle store; ``_run`` never touches the eval store."""
    store = _RecordingCandleStore(candles)
    runner = SweepRunner(
        cast(CandleStore, store), cast(EvaluationStore, object()), build_candidate_strategy
    )
    return runner, store


_WINDOW_CONFIG = SweepConfig(
    symbol="BTC/USDT",
    timeframe="1m",
    history_days=5,
    scenario_count=10,
    lookback_candles=60,
    horizon_candles=30,
    seed=7,
    training_fraction=0.6,
    validation_windows=2,
    candidates=(
        SweepCandidate(name="baseline", params={"fast_ema_period": 5, "slow_ema_period": 12}),
        SweepCandidate(name="slower", params={"fast_ema_period": 8, "slow_ema_period": 21}),
    ),
)


class TestWindowEnd:
    """``window_end`` reserves a holdout the walk-forward never peeks into."""

    async def test_window_end_bounds_the_fetched_history(self) -> None:
        now = utc_now().replace(second=0, microsecond=0)
        boundary = now - timedelta(days=2)
        # 600 candles strictly before the boundary, plus 200 from the holdout
        # that a leaking sweep would wrongly pull into the graded series.
        pre = _minute_candles(boundary - timedelta(minutes=1), 600)
        post = _minute_candles(now, 200)
        runner, store = _runner_with(pre + post)

        report = await runner._run(_WINDOW_CONFIG.model_copy(update={"window_end": boundary}))

        assert store.end == boundary
        assert store.start == boundary - timedelta(days=5)
        # The holdout candles existed, yet none of them reached the series.
        assert any(candle.open_time >= boundary for candle in pre + post)
        assert store.returned
        assert all(candle.open_time < boundary for candle in store.returned)
        assert report["baseline"] == "baseline"

    async def test_default_window_end_ends_at_now(self) -> None:
        now = utc_now().replace(second=0, microsecond=0)
        runner, store = _runner_with(_minute_candles(now - timedelta(minutes=1), 600))

        before = utc_now()
        await runner._run(_WINDOW_CONFIG)  # window_end defaults to None
        after = utc_now()

        assert store.end is not None and before <= store.end <= after
