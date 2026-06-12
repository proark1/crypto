"""Round-trip tests for the evaluation tables."""

import os
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from tradebot.evaluation import (
    LearningFinding,
    MarketConditions,
    RunStatus,
    Scenario,
    ScenarioClass,
    ScenarioResult,
    TimingLabel,
    TrendLabel,
    Verdict,
    VolatilityLabel,
)
from tradebot.evaluation.models import EventLabel
from tradebot.persistence import Database, EvaluationStore
from tradebot.persistence.database import metadata

BASE_TIME = datetime(2026, 1, 2, 0, 0, tzinfo=UTC)
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


async def make_run(store: EvaluationStore) -> int:
    return await store.create_run(
        symbols=["BTC/USDT"],
        timeframes=["1h"],
        config={"strategy": {"fast_ema_period": 20}, "horizon_candles": 24},
        code_version="abc1234",
        progress_total=100,
        created_at=BASE_TIME,
    )


def make_scenario(run_id: int) -> Scenario:
    return Scenario(
        run_id=run_id,
        symbol="BTC/USDT",
        timeframe="1h",
        decision_time=BASE_TIME,
        lookback_candles=168,
        scenario_class=ScenarioClass.FLAT,
        conditions=MarketConditions(
            trend=TrendLabel.UP,
            volatility=VolatilityLabel.HIGH,
            events=(EventLabel.BREAKOUT_REAL,),
        ),
        seed=42,
    )


class TestRunLifecycle:
    async def test_run_advances_to_completed_with_summary(self, database: Database) -> None:
        store = EvaluationStore(database)
        run_id = await make_run(store)

        await store.set_run_status(run_id, RunStatus.RUNNING)
        await store.set_progress(run_id, 40)
        await store.complete_run(run_id, {"expectancy_r": "0.31", "win_rate": "0.55"})

        run = await store.fetch_run(run_id)
        assert run is not None
        assert run["status"] == "completed"
        assert run["progress_done"] == 40
        assert run["summary"]["expectancy_r"] == "0.31"
        # The config snapshot survives verbatim: results are never orphaned
        # from the rules that produced them.
        assert run["config"]["strategy"]["fast_ema_period"] == 20
        assert run["code_version"] == "abc1234"

    async def test_decimal_config_values_are_stringified_not_floated(
        self, database: Database
    ) -> None:
        """Real strategy configs carry Decimals; JSONB cannot encode them,
        and coercing to float would betray the snapshot's exactness."""
        store = EvaluationStore(database)
        run_id = await store.create_run(
            symbols=["BTC/USDT"],
            timeframes=["1h"],
            config={"risk_per_trade_fraction": Decimal("0.01")},
            code_version="abc1234",
            progress_total=1,
            created_at=BASE_TIME,
        )

        run = await store.fetch_run(run_id)
        assert run is not None
        assert run["config"]["risk_per_trade_fraction"] == "0.01"

    async def test_unknown_run_is_none_and_listing_is_newest_first(
        self, database: Database
    ) -> None:
        store = EvaluationStore(database)
        assert await store.fetch_run(999) is None

        first = await make_run(store)
        second = await make_run(store)
        runs = await store.list_runs()
        assert [run["id"] for run in runs] == [second, first]


class TestScenarios:
    async def test_round_trip_preserves_conditions(self, database: Database) -> None:
        store = EvaluationStore(database)
        run_id = await make_run(store)
        original = make_scenario(run_id)

        (scenario_id,) = await store.insert_scenarios([original])
        ((loaded_id, loaded),) = await store.fetch_scenarios(run_id)

        assert loaded_id == scenario_id
        assert loaded == original  # enums, events tuple, aware datetime

    async def test_batch_ids_come_back_in_input_order(self, database: Database) -> None:
        store = EvaluationStore(database)
        run_id = await make_run(store)
        ids = await store.insert_scenarios([make_scenario(run_id) for _ in range(3)])
        assert ids == sorted(ids)
        assert len(ids) == 3

    async def test_empty_batch_is_a_noop(self, database: Database) -> None:
        assert await EvaluationStore(database).insert_scenarios([]) == []


class TestResults:
    async def test_round_trip_preserves_decimals_and_verdict(self, database: Database) -> None:
        store = EvaluationStore(database)
        run_id = await make_run(store)
        (scenario_id,) = await store.insert_scenarios([make_scenario(run_id)])
        original = ScenarioResult(
            scenario_id=scenario_id,
            decision="buy",
            confidence=1.0,
            reasons=("fast EMA crossed above slow EMA",),
            entry_price_quote=Decimal("62000.5"),
            exit_price_quote=Decimal("63500"),
            r_multiple=Decimal("1.52"),
            pnl_quote=Decimal("150.25"),
            mfe_r=Decimal("2.1"),
            mae_r=Decimal("-0.3"),
            duration_candles=18,
            stop_hit=False,
            oracle_r=Decimal("2.4"),
            verdict=Verdict.EXCELLENT,
            timing=TimingLabel.ON_TIME,
            created_at=BASE_TIME,
        )

        await store.insert_result(original)
        (loaded,) = await store.fetch_results(run_id)
        assert loaded == original

    async def test_hold_results_carry_null_trade_fields(self, database: Database) -> None:
        store = EvaluationStore(database)
        run_id = await make_run(store)
        (scenario_id,) = await store.insert_scenarios([make_scenario(run_id)])
        hold = ScenarioResult(
            scenario_id=scenario_id,
            decision="hold",
            reasons=(),
            verdict=Verdict.CORRECT_HOLD,
            created_at=BASE_TIME,
        )

        await store.insert_result(hold)
        (loaded,) = await store.fetch_results(run_id)
        assert loaded.r_multiple is None
        assert loaded.verdict == Verdict.CORRECT_HOLD
        assert loaded.timing is None


class TestFindings:
    async def test_round_trip_with_evidence_ids(self, database: Database) -> None:
        store = EvaluationStore(database)
        run_id = await make_run(store)
        finding = LearningFinding(
            run_id=run_id,
            pattern="buys too often in ranging markets",
            evidence_scenario_ids=(1, 2, 3),
            affected_count=3,
            average_r_impact=Decimal("-0.4"),
            suggestion="add a trend-regime filter before entries",
            confidence="high",
            created_at=BASE_TIME,
        )

        finding_id = await store.insert_finding(finding)
        ((loaded_id, loaded),) = await store.fetch_findings(run_id)
        assert finding_id > 0
        assert loaded_id == finding_id
        assert loaded == finding
        assert loaded.status == "proposed"

    async def test_status_is_the_only_mutable_field(self, database: Database) -> None:
        store = EvaluationStore(database)
        run_id = await make_run(store)
        finding = LearningFinding(
            run_id=run_id,
            pattern="entries lose money when trend is ranging",
            evidence_scenario_ids=(1, 2),
            affected_count=2,
            average_r_impact=Decimal("-0.3"),
            suggestion="gate entries behind extra confirmation",
            confidence="low",
            created_at=BASE_TIME,
        )
        finding_id = await store.insert_finding(finding)

        await store.set_finding_status(finding_id, "accepted")

        loaded = await store.fetch_finding(finding_id)
        assert loaded is not None
        assert loaded.status == "accepted"
        assert loaded.pattern == finding.pattern  # facts stay frozen
        assert await store.fetch_finding(9999) is None

    async def test_fetch_for_runs_groups_one_query_by_run(self, database: Database) -> None:
        store = EvaluationStore(database)
        first_run = await make_run(store)
        second_run = await make_run(store)

        def finding(run_id: int, pattern: str) -> LearningFinding:
            return LearningFinding(
                run_id=run_id,
                pattern=pattern,
                evidence_scenario_ids=(1,),
                affected_count=1,
                average_r_impact=Decimal("-0.4"),
                suggestion="test",
                confidence="low",
                created_at=BASE_TIME,
            )

        first_id = await store.insert_finding(finding(first_run, "pattern a"))
        await store.insert_finding(finding(second_run, "pattern b"))
        await store.insert_finding(finding(second_run, "pattern c"))

        grouped = await store.fetch_findings_for_runs([first_run, second_run, 9999])

        assert set(grouped) == {first_run, second_run}  # absent runs are absent
        assert [(pair[0], pair[1].pattern) for pair in grouped[first_run]] == [
            (first_id, "pattern a")
        ]
        assert [pair[1].pattern for pair in grouped[second_run]] == ["pattern b", "pattern c"]
        assert await store.fetch_findings_for_runs([]) == {}


class TestSweeps:
    async def test_sweep_lifecycle_round_trips(self, database: Database) -> None:
        store = EvaluationStore(database)
        sweep_id = await store.create_sweep(
            symbol="BTC/USDT",
            timeframe="1h",
            config={"training_fraction": 0.7, "risk": Decimal("0.01")},
            motivating_finding_ids=[7, 9],
            created_at=BASE_TIME,
        )

        await store.set_sweep_status(sweep_id, RunStatus.RUNNING)
        await store.complete_sweep(sweep_id, {"verdict": "overfit", "winner": "faster_cross"})

        sweep = await store.fetch_sweep(sweep_id)
        assert sweep is not None
        assert sweep["status"] == "completed"
        assert sweep["motivating_finding_ids"] == [7, 9]
        assert sweep["config"]["risk"] == "0.01"  # Decimal stringified, not floated
        assert sweep["report"]["verdict"] == "overfit"
        assert await store.fetch_sweep(9999) is None
        assert [row["id"] for row in await store.list_sweeps()] == [sweep_id]
