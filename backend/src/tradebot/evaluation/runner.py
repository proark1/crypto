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
from collections.abc import Callable, Coroutine, Sequence
from datetime import datetime, timedelta
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

    strategy: str = "production"
    """Which competition lineup entry the run grades. The default is the
    incumbent — the shape production trades right now."""

    window_end: datetime | None = None
    """Freeze the history window's end instead of using "now". Comparison
    runs share one frozen end so every strategy faces byte-identical
    scenarios; ``None`` (the default) keeps the historical behavior."""

    def intervals(self) -> tuple[CandleInterval, ...]:
        """Parse timeframes; raises ``ValueError`` on unknown ones."""
        return tuple(CandleInterval(timeframe) for timeframe in self.timeframes)


class EvaluationRunner:
    """Executes one run end to end against the stores."""

    def __init__(
        self,
        candle_store: CandleStore,
        evaluation_store: EvaluationStore,
        evaluator_for: Callable[[str], ScenarioEvaluator],
    ) -> None:
        """Bind the data sources and the per-strategy evaluator factory.

        ``evaluator_for`` maps a run config's ``strategy`` id to a fresh
        evaluator grading that lineup entry; it raises ``ValueError`` for
        an unknown id, which fails the run loudly rather than grading the
        wrong strategy.
        """
        self._candles = candle_store
        self._store = evaluation_store
        self._evaluator_for = evaluator_for

    async def execute(self, run_id: int, config: EvaluationRunConfig) -> None:
        """Drive ``run_id`` to a terminal status; never raises except on cancel."""
        try:
            await self._store.set_run_status(run_id, RunStatus.RUNNING)
            records = await self._evaluate_all(run_id, config)
            if not records:
                # A run that evaluated nothing must not look "completed":
                # the operator would read an empty report as a quiet pass
                # instead of the data problem it is.
                raise RuntimeError(
                    "no symbol/timeframe series had enough stored history to host a "
                    "single scenario; deepen the candle history "
                    "(TRADEBOT_HISTORY_BACKFILL_DAYS) or shorten lookback/horizon"
                )
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
        evaluator = self._evaluator_for(config.strategy)
        end = config.window_end if config.window_end is not None else utc_now()
        start = end - timedelta(days=config.history_days)
        for symbol in config.symbols:
            base = await self._candles.fetch_range(symbol, CandleInterval.M1, start, end)
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
                    outcome = evaluator.evaluate(series, spec)
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
        # Every run id owned by the in-flight task: one for a single run, N
        # for a comparison batch (cancel must reconcile all of them).
        self._batch_run_ids: tuple[int, ...] = ()

    async def _create_run(self, config: EvaluationRunConfig, comparison_group: int | None) -> int:
        return await self._store.create_run(
            symbols=list(config.symbols),
            timeframes=list(config.timeframes),
            config=config.model_dump(mode="json"),
            code_version=self._code_version,
            progress_total=config.scenario_count * len(config.symbols) * len(config.timeframes),
            created_at=utc_now(),
            strategy=config.strategy,
            comparison_group=comparison_group,
        )

    def _require_idle(self) -> None:
        if self._task is not None and not self._task.done():
            in_flight = ", ".join(str(run_id) for run_id in self._batch_run_ids)
            raise RuntimeError(f"evaluation run(s) {in_flight} already in progress")

    async def start(self, config: EvaluationRunConfig) -> int:
        """Create the run row and launch it; one run at a time, on purpose.

        Raises ``RuntimeError`` if a run is in flight (evaluation shares the
        worker's CPU with live trading) and ``ValueError`` for bad config.
        """
        config.intervals()  # validate timeframes before any row exists
        self._require_idle()
        run_id = await self._create_run(config, comparison_group=None)
        self._batch_run_ids = (run_id,)
        self._task = self._spawn(self._runner.execute(run_id, config))
        logger.info("evaluation run %d started (%s)", run_id, config.strategy)
        return run_id

    async def start_comparison(
        self, config: EvaluationRunConfig, strategies: Sequence[str]
    ) -> list[int]:
        """Grade every strategy in ``strategies`` on identical scenarios.

        One run row per strategy, all sharing a ``comparison_group`` (the
        lead run's id) and one frozen ``window_end`` — same candles, same
        seed, same scenario coordinates, so the summaries differ only by
        strategy. Runs execute sequentially in one background task (the
        single-flight rule protects the live candle loop, comparison or
        not). Returns the run ids in lineup order; raises ``RuntimeError``
        when a run is already in flight, ``ValueError`` for bad config or
        an empty lineup.
        """
        config.intervals()
        if not strategies:
            raise ValueError("a comparison needs at least one strategy")
        self._require_idle()
        frozen_end = config.window_end if config.window_end is not None else utc_now()
        run_configs: list[EvaluationRunConfig] = [
            config.model_copy(update={"strategy": strategy, "window_end": frozen_end})
            for strategy in strategies
        ]
        lead_id = await self._create_run(run_configs[0], comparison_group=None)
        # The group id is the lead run's id — knowable only after the first
        # insert, so the lead row is tagged retroactively.
        await self._store.set_comparison_group(lead_id, lead_id)
        run_ids = [lead_id]
        for run_config in run_configs[1:]:
            run_ids.append(await self._create_run(run_config, comparison_group=lead_id))
        self._batch_run_ids = tuple(run_ids)

        async def execute_batch() -> None:
            for run_id, run_config in zip(run_ids, run_configs, strict=True):
                await self._runner.execute(run_id, run_config)

        self._task = self._spawn(execute_batch())
        logger.info(
            "comparison %d started: runs %s grading %s",
            lead_id,
            run_ids,
            ", ".join(strategies),
        )
        return run_ids

    def cancel(self, run_id: int) -> bool:
        """Cancel the in-flight run or batch; returns whether anything was.

        Cancelling any member of a comparison cancels the whole batch —
        half a comparison cannot answer the question the batch asked.
        """
        if run_id not in self._batch_run_ids or self._task is None or self._task.done():
            return False
        self._task.cancel()
        # A task cancelled before it ever ran never executes a line of
        # ``execute`` — including its own INTERRUPTED write — so the run
        # would sit "pending" forever. Reconcile from here; idempotent if
        # ``execute`` got there first.
        for batch_run_id in self._batch_run_ids:
            self._spawn(self._mark_interrupted_if_not_terminal(batch_run_id))
        return True

    async def _mark_interrupted_if_not_terminal(self, run_id: int) -> None:
        run = await self._store.fetch_run(run_id)
        if run is not None and run["status"] in (RunStatus.PENDING.value, RunStatus.RUNNING.value):
            await self._store.set_run_status(run_id, RunStatus.INTERRUPTED)
