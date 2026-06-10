"""Run orchestration: load candles, generate, evaluate, persist, summarize.

Runs execute inside the worker process as background tasks, yielding to the
event loop after every scenario so the live candle loop is never starved.
A run can fail or be cancelled without taking the bot down: every terminal
path writes an honest status (completed / failed / interrupted) — a run is
never left looking half-done without saying so.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Coroutine
from datetime import timedelta
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from tradebot.core.models import CandleInterval, utc_now
from tradebot.evaluation.engine import ScenarioEvaluator
from tradebot.evaluation.generator import GeneratorConfig, generate_specs
from tradebot.evaluation.learning import mine_findings
from tradebot.evaluation.models import RunStatus, Scenario, ScenarioResult
from tradebot.evaluation.reports import build_summary
from tradebot.marketdata import aggregate_candles
from tradebot.persistence import CandleStore, EvaluationStore

logger = logging.getLogger(__name__)

PROGRESS_EVERY = 5
"""Persist progress every N scenarios: visible in the UI without writing
the database on every single one."""


class EvaluationRunConfig(BaseModel):
    """One run's shape; snapshotted verbatim into the run row."""

    model_config = ConfigDict(frozen=True)

    symbols: tuple[str, ...] = Field(min_length=1)
    timeframes: tuple[str, ...] = ("1h",)
    history_days: int = Field(default=365, gt=0)
    scenario_count: int = Field(default=200, gt=0)
    """Scenarios per (symbol, timeframe) series."""

    lookback_candles: int = Field(default=200, ge=60)
    horizon_candles: int = Field(default=60, gt=0)
    seed: int = 7

    def intervals(self) -> tuple[CandleInterval, ...]:
        """Parse timeframes; raises ``ValueError`` on unknown ones."""
        return tuple(CandleInterval(timeframe) for timeframe in self.timeframes)


class EvaluationRunner:
    """Executes one run end to end against the stores."""

    def __init__(
        self,
        candle_store: CandleStore,
        evaluation_store: EvaluationStore,
        evaluator: ScenarioEvaluator,
    ) -> None:
        """Bind the data sources and the (strategy-carrying) evaluator."""
        self._candles = candle_store
        self._store = evaluation_store
        self._evaluator = evaluator

    async def execute(self, run_id: int, config: EvaluationRunConfig) -> None:
        """Drive ``run_id`` to a terminal status; never raises except on cancel."""
        try:
            await self._store.set_run_status(run_id, RunStatus.RUNNING)
            records = await self._evaluate_all(run_id, config)
            # Findings land before the run flips to completed, so a
            # "completed" run is always fully mined — never half-reported.
            findings = mine_findings(run_id, records, utc_now())
            for finding in findings:
                await self._store.insert_finding(finding)
            await self._store.complete_run(run_id, build_summary(records))
            logger.info(
                "evaluation run %d completed: %d scenarios, %d findings",
                run_id,
                len(records),
                len(findings),
            )
        except asyncio.CancelledError:
            # Shutdown or user cancel: say so, never look half-done silently.
            await self._store.set_run_status(run_id, RunStatus.INTERRUPTED)
            logger.warning("evaluation run %d interrupted", run_id)
            raise
        except Exception:
            logger.exception("evaluation run %d failed", run_id)
            await self._store.set_run_status(run_id, RunStatus.FAILED)

    async def _evaluate_all(
        self, run_id: int, config: EvaluationRunConfig
    ) -> list[tuple[Scenario, ScenarioResult]]:
        records: list[tuple[Scenario, ScenarioResult]] = []
        done = 0
        now = utc_now()
        start = now - timedelta(days=config.history_days)
        for symbol in config.symbols:
            base = await self._candles.fetch_range(symbol, CandleInterval.M1, start, now)
            for interval in config.intervals():
                series = (
                    base if interval == CandleInterval.M1 else aggregate_candles(base, interval)
                )
                try:
                    specs = generate_specs(
                        series,
                        GeneratorConfig(
                            scenario_count=config.scenario_count,
                            lookback_candles=config.lookback_candles,
                            horizon_candles=config.horizon_candles,
                            seed=config.seed,
                        ),
                    )
                except ValueError:
                    logger.warning(
                        "run %d: %s %s has too little history; series skipped",
                        run_id,
                        symbol,
                        interval.value,
                    )
                    continue
                for spec, conditions in specs:
                    outcome = self._evaluator.evaluate(series, spec)
                    scenario = Scenario(
                        run_id=run_id,
                        symbol=symbol,
                        timeframe=interval.value,
                        decision_time=series[spec.decision_index - 1].close_time,
                        lookback_candles=spec.lookback,
                        scenario_class=outcome.scenario_class,
                        conditions=conditions,
                        seed=config.seed,
                    )
                    (scenario_id,) = await self._store.insert_scenarios([scenario])
                    result = ScenarioResult(
                        scenario_id=scenario_id,
                        decision=outcome.decision,
                        confidence=outcome.confidence,
                        reasons=outcome.reasons,
                        entry_price_quote=outcome.entry_price_quote,
                        exit_price_quote=outcome.exit_price_quote,
                        r_multiple=outcome.r_multiple,
                        pnl_quote=outcome.pnl_quote,
                        mfe_r=outcome.mfe_r,
                        mae_r=outcome.mae_r,
                        duration_candles=outcome.duration_candles,
                        stop_hit=outcome.stop_hit,
                        oracle_r=outcome.oracle_r,
                        verdict=outcome.verdict,
                        timing=outcome.timing,
                        created_at=utc_now(),
                    )
                    await self._store.insert_result(result)
                    records.append((scenario, result))
                    done += 1
                    if done % PROGRESS_EVERY == 0:
                        await self._store.set_progress(run_id, done)
                    # Yield: the live candle loop must never wait on a run.
                    await asyncio.sleep(0)
        await self._store.set_progress(run_id, done)
        return records


class EvaluationManager:
    """Owns the single in-flight run and its background task."""

    def __init__(
        self,
        runner: EvaluationRunner,
        store: EvaluationStore,
        code_version: str,
        spawn: Callable[[Coroutine[Any, Any, None]], asyncio.Task[None]],
    ) -> None:
        """``spawn`` ties the run's task to the worker's TaskGroup lifetime."""
        self._runner = runner
        self._store = store
        self._code_version = code_version
        self._spawn = spawn
        self._task: asyncio.Task[None] | None = None
        self._current_run_id: int | None = None

    async def start(self, config: EvaluationRunConfig) -> int:
        """Create the run row and launch it; one run at a time, on purpose.

        Raises ``RuntimeError`` if a run is in flight (evaluation shares the
        worker's CPU with live trading) and ``ValueError`` for bad config.
        """
        config.intervals()  # validate timeframes before any row exists
        if self._task is not None and not self._task.done():
            raise RuntimeError(f"evaluation run {self._current_run_id} is already in progress")
        run_id = await self._store.create_run(
            symbols=list(config.symbols),
            timeframes=list(config.timeframes),
            config=config.model_dump(),
            code_version=self._code_version,
            progress_total=config.scenario_count * len(config.symbols) * len(config.timeframes),
            created_at=utc_now(),
        )
        self._current_run_id = run_id
        self._task = self._spawn(self._runner.execute(run_id, config))
        logger.info("evaluation run %d started", run_id)
        return run_id

    def cancel(self, run_id: int) -> bool:
        """Cancel the in-flight run; returns whether anything was cancelled."""
        if self._current_run_id != run_id or self._task is None or self._task.done():
            return False
        self._task.cancel()
        # A task cancelled before it ever ran never executes a line of
        # ``execute`` — including its own INTERRUPTED write — so the run
        # would sit "pending" forever. Reconcile from here; idempotent if
        # ``execute`` got there first.
        self._spawn(self._mark_interrupted_if_not_terminal(run_id))
        return True

    async def _mark_interrupted_if_not_terminal(self, run_id: int) -> None:
        run = await self._store.fetch_run(run_id)
        if run is not None and run["status"] in (RunStatus.PENDING.value, RunStatus.RUNNING.value):
            await self._store.set_run_status(run_id, RunStatus.INTERRUPTED)
