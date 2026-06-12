"""Persistence for evaluation runs, scenarios, results, findings, and sweeps.

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
    sweeps_table,
)


def _require_aware(moment: datetime) -> None:
    """Reject naive datetimes (repo invariant: timestamps are UTC-aware)."""
    if moment.tzinfo is None:
        raise ValueError("naive datetime is not allowed; timestamps must be UTC-aware")


class EvaluationStore:
    """Typed access to the five evaluation tables (runs, scenarios, results, findings, sweeps)."""

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
        strategy: str = "production",
        comparison_group: int | None = None,
    ) -> int:
        """Create a pending run; returns its id.

        ``config`` must be the complete run + strategy configuration — a
        result that cannot name the rules that produced it is worthless.
        Decimal values (risk fractions, balances) are stringified on the way
        in: JSONB cannot encode Decimal, and silently coercing to float
        would betray the exactness the snapshot exists to preserve.
        ``strategy`` names the competition lineup entry being graded;
        ``comparison_group`` ties runs generated over identical scenarios.
        """
        _require_aware(created_at)
        encodable_config = json.loads(json.dumps(config, default=str))
        statement = (
            evaluation_runs_table.insert()
            .values(
                created_at=created_at,
                status=RunStatus.PENDING.value,
                strategy=strategy,
                comparison_group=comparison_group,
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

    async def set_comparison_group(self, run_id: int, comparison_group: int) -> None:
        """Tag ``run_id`` as part of a comparison (the lead run, retroactively).

        The group id is the lead run's own id, which exists only after its
        insert — every other member is created with the group set.
        """
        statement = (
            update(evaluation_runs_table)
            .where(evaluation_runs_table.c.id == run_id)
            .values(comparison_group=comparison_group)
        )
        async with self._database.engine.begin() as connection:
            await connection.execute(statement)

    async def list_comparisons(self, limit: int = 10) -> list[list[dict[str, Any]]]:
        """Return recent comparison batches, newest first.

        Each batch is its member runs in creation order (the lineup order
        they were started in), so callers can lay summaries side by side
        without re-deriving the grouping.
        """
        groups_statement = (
            select(evaluation_runs_table.c.comparison_group)
            .where(evaluation_runs_table.c.comparison_group.is_not(None))
            .group_by(evaluation_runs_table.c.comparison_group)
            .order_by(evaluation_runs_table.c.comparison_group.desc())
            .limit(limit)
        )
        async with self._database.engine.connect() as connection:
            group_ids = [row[0] for row in (await connection.execute(groups_statement)).all()]
            if not group_ids:
                return []
            runs_statement = (
                select(evaluation_runs_table)
                .where(evaluation_runs_table.c.comparison_group.in_(group_ids))
                .order_by(evaluation_runs_table.c.id)
            )
            rows = (await connection.execute(runs_statement)).mappings().all()
        batches: dict[int, list[dict[str, Any]]] = {group_id: [] for group_id in group_ids}
        for row in rows:
            batches[row["comparison_group"]].append(dict(row))
        return [batches[group_id] for group_id in group_ids]

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

    # Columns for the replay browser: each scenario joined with its graded
    # result. Selected explicitly (and the scenario id labeled) because both
    # tables carry ``id``/``created_at`` and a bare two-table select would
    # leave the mapping keys ambiguous.
    _SCENARIO_WITH_RESULT_COLUMNS = (
        scenarios_table.c.id.label("scenario_id"),
        scenarios_table.c.run_id,
        scenarios_table.c.symbol,
        scenarios_table.c.timeframe,
        scenarios_table.c.decision_time,
        scenarios_table.c.lookback_candles,
        scenarios_table.c.scenario_class,
        scenarios_table.c.trend,
        scenarios_table.c.volatility,
        scenarios_table.c.events,
        scenario_results_table.c.decision,
        scenario_results_table.c.confidence,
        scenario_results_table.c.reasons,
        scenario_results_table.c.entry_price_quote,
        scenario_results_table.c.exit_price_quote,
        scenario_results_table.c.r_multiple,
        scenario_results_table.c.pnl_quote,
        scenario_results_table.c.mfe_r,
        scenario_results_table.c.mae_r,
        scenario_results_table.c.duration_candles,
        scenario_results_table.c.stop_hit,
        scenario_results_table.c.oracle_r,
        scenario_results_table.c.verdict,
        scenario_results_table.c.timing,
    )

    async def list_scenarios_with_results(self, run_id: int) -> list[dict[str, Any]]:
        """Return each of the run's graded scenarios joined with its result.

        Scenarios without a result yet (the run is mid-flight) are omitted:
        the replay browser shows decided-and-graded scenarios only.
        """
        statement = (
            select(*self._SCENARIO_WITH_RESULT_COLUMNS)
            .select_from(
                scenarios_table.join(
                    scenario_results_table,
                    scenario_results_table.c.scenario_id == scenarios_table.c.id,
                )
            )
            .where(scenarios_table.c.run_id == run_id)
            .order_by(scenarios_table.c.id)
        )
        async with self._database.engine.connect() as connection:
            rows = (await connection.execute(statement)).mappings().all()
        return [dict(row) for row in rows]

    async def fetch_scenario_with_result(self, scenario_id: int) -> dict[str, Any] | None:
        """Return one graded scenario joined with its result, or ``None``."""
        statement = (
            select(*self._SCENARIO_WITH_RESULT_COLUMNS)
            .select_from(
                scenarios_table.join(
                    scenario_results_table,
                    scenario_results_table.c.scenario_id == scenarios_table.c.id,
                )
            )
            .where(scenarios_table.c.id == scenario_id)
        )
        async with self._database.engine.connect() as connection:
            row = (await connection.execute(statement)).mappings().first()
        return None if row is None else dict(row)

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

    async def create_sweep(
        self,
        symbol: str,
        timeframe: str,
        config: dict[str, Any],
        motivating_finding_ids: Sequence[int],
        created_at: datetime,
    ) -> int:
        """Create a pending sweep; returns its id.

        Same JSONB discipline as runs: Decimals in the config snapshot are
        stringified, never coerced to float.
        """
        _require_aware(created_at)
        encodable_config = json.loads(json.dumps(config, default=str))
        statement = (
            sweeps_table.insert()
            .values(
                created_at=created_at,
                status=RunStatus.PENDING.value,
                symbol=symbol,
                timeframe=timeframe,
                config=encodable_config,
                motivating_finding_ids=list(motivating_finding_ids),
            )
            .returning(sweeps_table.c.id)
        )
        async with self._database.engine.begin() as connection:
            sweep_id: int = (await connection.execute(statement)).scalar_one()
        return sweep_id

    async def set_sweep_status(self, sweep_id: int, status: RunStatus) -> None:
        """Advance the sweep's lifecycle (pending → running → terminal)."""
        statement = (
            update(sweeps_table).where(sweeps_table.c.id == sweep_id).values(status=status.value)
        )
        async with self._database.engine.begin() as connection:
            await connection.execute(statement)

    async def complete_sweep(self, sweep_id: int, report: dict[str, Any]) -> None:
        """Mark the sweep completed and attach its walk-forward report."""
        encodable_report = json.loads(json.dumps(report, default=str))
        statement = (
            update(sweeps_table)
            .where(sweeps_table.c.id == sweep_id)
            .values(status=RunStatus.COMPLETED.value, report=encodable_report)
        )
        async with self._database.engine.begin() as connection:
            await connection.execute(statement)

    async def fetch_sweep(self, sweep_id: int) -> dict[str, Any] | None:
        """Return one sweep row as a mapping, or ``None`` if unknown."""
        statement = select(sweeps_table).where(sweeps_table.c.id == sweep_id)
        async with self._database.engine.connect() as connection:
            row = (await connection.execute(statement)).mappings().first()
        return None if row is None else dict(row)

    async def list_sweeps(self, limit: int = 50) -> list[dict[str, Any]]:
        """Return the newest sweeps first (the research screen's sweep list)."""
        statement = select(sweeps_table).order_by(sweeps_table.c.id.desc()).limit(limit)
        async with self._database.engine.connect() as connection:
            rows = (await connection.execute(statement)).mappings().all()
        return [dict(row) for row in rows]

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

    async def fetch_findings(self, run_id: int) -> list[tuple[int, LearningFinding]]:
        """Return (id, finding) pairs for ``run_id`` in creation order.

        Ids ride along because accept/reject addresses a finding by id —
        the model itself stays a pure domain object.
        """
        statement = (
            select(learning_findings_table)
            .where(learning_findings_table.c.run_id == run_id)
            .order_by(learning_findings_table.c.id)
        )
        async with self._database.engine.connect() as connection:
            rows = (await connection.execute(statement)).mappings().all()
        return [(row["id"], LearningFinding.model_validate(dict(row))) for row in rows]

    async def fetch_findings_for_runs(
        self, run_ids: Sequence[int]
    ) -> dict[int, list[tuple[int, LearningFinding]]]:
        """Return (id, finding) pairs grouped by run, in creation order.

        One query for a whole window of runs: the research timeline and the
        recurrence annotations compare findings across many runs, and a
        per-run round trip would scale those pages with their history.
        Runs without findings are simply absent from the result.
        """
        if not run_ids:
            return {}
        statement = (
            select(learning_findings_table)
            .where(learning_findings_table.c.run_id.in_(list(run_ids)))
            .order_by(learning_findings_table.c.id)
        )
        async with self._database.engine.connect() as connection:
            rows = (await connection.execute(statement)).mappings().all()
        grouped: dict[int, list[tuple[int, LearningFinding]]] = {}
        for row in rows:
            grouped.setdefault(row["run_id"], []).append(
                (row["id"], LearningFinding.model_validate(dict(row)))
            )
        return grouped

    async def fetch_finding(self, finding_id: int) -> LearningFinding | None:
        """Return one finding, or ``None`` if unknown."""
        statement = select(learning_findings_table).where(
            learning_findings_table.c.id == finding_id
        )
        async with self._database.engine.connect() as connection:
            row = (await connection.execute(statement)).mappings().first()
        return None if row is None else LearningFinding.model_validate(dict(row))

    async def set_finding_status(self, finding_id: int, status: str) -> None:
        """Record the human's accept/reject verdict on a finding.

        The only mutation findings ever see: pattern, evidence, and impact
        are facts about the run and stay frozen.
        """
        statement = (
            update(learning_findings_table)
            .where(learning_findings_table.c.id == finding_id)
            .values(status=status)
        )
        async with self._database.engine.begin() as connection:
            await connection.execute(statement)
