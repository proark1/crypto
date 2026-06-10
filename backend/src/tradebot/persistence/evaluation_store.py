"""Persistence for evaluation runs, scenarios, results, and findings.

Append-mostly by design: results are facts about a frozen (config, data)
pair, so nothing here updates a verdict after the fact — runs advance
through status/progress fields only, and old runs are never rescored
(ARCHITECTURE.md section 12, strategy versioning).
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import datetime
from typing import Any

from sqlalchemy import select, update

from tradebot.evaluation.models import (
    LearningFinding,
    MarketConditions,
    RunStatus,
    Scenario,
    ScenarioResult,
)
from tradebot.persistence.database import (
    Database,
    evaluation_runs_table,
    learning_findings_table,
    scenario_results_table,
    scenarios_table,
)


def _require_aware(moment: datetime) -> None:
    """Reject naive datetimes (repo invariant: timestamps are UTC-aware)."""
    if moment.tzinfo is None:
        raise ValueError("naive datetime is not allowed; timestamps must be UTC-aware")


class EvaluationStore:
    """Typed access to the four evaluation tables."""

    def __init__(self, database: Database) -> None:
        """Bind the store to ``database``."""
        self._database = database

    async def create_run(
        self,
        symbols: Sequence[str],
        timeframes: Sequence[str],
        config: dict[str, Any],
        code_version: str,
        progress_total: int,
        created_at: datetime,
    ) -> int:
        """Create a pending run; returns its id.

        ``config`` must be the complete run + strategy configuration — a
        result that cannot name the rules that produced it is worthless.
        Decimal values (risk fractions, balances) are stringified on the way
        in: JSONB cannot encode Decimal, and silently coercing to float
        would betray the exactness the snapshot exists to preserve.
        """
        _require_aware(created_at)
        encodable_config = json.loads(json.dumps(config, default=str))
        statement = (
            evaluation_runs_table.insert()
            .values(
                created_at=created_at,
                status=RunStatus.PENDING.value,
                symbols=list(symbols),
                timeframes=list(timeframes),
                config=encodable_config,
                code_version=code_version,
                progress_done=0,
                progress_total=progress_total,
            )
            .returning(evaluation_runs_table.c.id)
        )
        async with self._database.engine.begin() as connection:
            run_id: int = (await connection.execute(statement)).scalar_one()
        return run_id

    async def set_run_status(self, run_id: int, status: RunStatus) -> None:
        """Advance the run's lifecycle (pending → running → terminal)."""
        statement = (
            update(evaluation_runs_table)
            .where(evaluation_runs_table.c.id == run_id)
            .values(status=status.value)
        )
        async with self._database.engine.begin() as connection:
            await connection.execute(statement)

    async def set_progress(self, run_id: int, done: int) -> None:
        """Record how many scenarios have been decided and graded so far."""
        statement = (
            update(evaluation_runs_table)
            .where(evaluation_runs_table.c.id == run_id)
            .values(progress_done=done)
        )
        async with self._database.engine.begin() as connection:
            await connection.execute(statement)

    async def complete_run(self, run_id: int, summary: dict[str, Any]) -> None:
        """Mark the run completed and attach its aggregate report."""
        statement = (
            update(evaluation_runs_table)
            .where(evaluation_runs_table.c.id == run_id)
            .values(status=RunStatus.COMPLETED.value, summary=summary)
        )
        async with self._database.engine.begin() as connection:
            await connection.execute(statement)

    async def fetch_run(self, run_id: int) -> dict[str, Any] | None:
        """Return one run row as a mapping, or ``None`` if unknown."""
        statement = select(evaluation_runs_table).where(evaluation_runs_table.c.id == run_id)
        async with self._database.engine.connect() as connection:
            row = (await connection.execute(statement)).mappings().first()
        return None if row is None else dict(row)

    async def list_runs(self, limit: int = 50) -> list[dict[str, Any]]:
        """Return the newest runs first (the research screen's run list)."""
        statement = (
            select(evaluation_runs_table).order_by(evaluation_runs_table.c.id.desc()).limit(limit)
        )
        async with self._database.engine.connect() as connection:
            rows = (await connection.execute(statement)).mappings().all()
        return [dict(row) for row in rows]

    async def insert_scenarios(self, scenarios: Sequence[Scenario]) -> list[int]:
        """Insert a batch of scenarios; returns their ids in input order."""
        if not scenarios:
            return []
        rows = [
            {
                "run_id": scenario.run_id,
                "symbol": scenario.symbol,
                "timeframe": scenario.timeframe,
                "decision_time": scenario.decision_time,
                "lookback_candles": scenario.lookback_candles,
                "scenario_class": scenario.scenario_class.value,
                "trend": scenario.conditions.trend.value,
                "volatility": scenario.conditions.volatility.value,
                "events": [event.value for event in scenario.conditions.events],
                "seed": scenario.seed,
            }
            for scenario in scenarios
        ]
        statement = scenarios_table.insert().returning(scenarios_table.c.id)
        ids: list[int] = []
        async with self._database.engine.begin() as connection:
            # executemany + RETURNING support varies; one statement per row
            # keeps ids reliably ordered, and scenario batches are small.
            for row in rows:
                ids.append((await connection.execute(statement.values(**row))).scalar_one())
        return ids

    async def fetch_scenarios(self, run_id: int) -> list[tuple[int, Scenario]]:
        """Return (id, scenario) pairs for ``run_id`` in creation order."""
        statement = (
            select(scenarios_table)
            .where(scenarios_table.c.run_id == run_id)
            .order_by(scenarios_table.c.id)
        )
        async with self._database.engine.connect() as connection:
            rows = (await connection.execute(statement)).mappings().all()
        return [
            (
                row["id"],
                Scenario(
                    run_id=row["run_id"],
                    symbol=row["symbol"],
                    timeframe=row["timeframe"],
                    decision_time=row["decision_time"],
                    lookback_candles=row["lookback_candles"],
                    scenario_class=row["scenario_class"],
                    conditions=MarketConditions(
                        trend=row["trend"],
                        volatility=row["volatility"],
                        events=tuple(row["events"]),
                    ),
                    seed=row["seed"],
                ),
            )
            for row in rows
        ]

    async def insert_result(self, result: ScenarioResult) -> None:
        """Persist one scenario's graded outcome (exactly one per scenario)."""
        row = result.model_dump()
        row["reasons"] = list(result.reasons)
        row["verdict"] = result.verdict.value
        row["timing"] = result.timing.value if result.timing is not None else None
        async with self._database.engine.begin() as connection:
            await connection.execute(scenario_results_table.insert(), [row])

    async def fetch_results(self, run_id: int) -> list[ScenarioResult]:
        """Return all graded results for ``run_id`` in scenario order."""
        statement = (
            select(scenario_results_table)
            .join(
                scenarios_table,
                scenarios_table.c.id == scenario_results_table.c.scenario_id,
            )
            .where(scenarios_table.c.run_id == run_id)
            .order_by(scenario_results_table.c.scenario_id)
        )
        async with self._database.engine.connect() as connection:
            rows = (await connection.execute(statement)).mappings().all()
        return [ScenarioResult.model_validate(dict(row)) for row in rows]

    async def insert_finding(self, finding: LearningFinding) -> int:
        """Persist one mined finding; returns its id."""
        row = finding.model_dump()
        row["evidence_scenario_ids"] = list(finding.evidence_scenario_ids)
        statement = (
            learning_findings_table.insert().values(**row).returning(learning_findings_table.c.id)
        )
        async with self._database.engine.begin() as connection:
            finding_id: int = (await connection.execute(statement)).scalar_one()
        return finding_id

    async def fetch_findings(self, run_id: int) -> list[LearningFinding]:
        """Return all findings for ``run_id`` in creation order."""
        statement = (
            select(learning_findings_table)
            .where(learning_findings_table.c.run_id == run_id)
            .order_by(learning_findings_table.c.id)
        )
        async with self._database.engine.connect() as connection:
            rows = (await connection.execute(statement)).mappings().all()
        return [LearningFinding.model_validate(dict(row)) for row in rows]
