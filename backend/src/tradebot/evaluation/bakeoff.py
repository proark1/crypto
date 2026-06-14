"""The bake-off: grade every contestant across a grid, rank by money made.

A bake-off runs the fixed contestant roster (``presets.py``) across a grid
of *cells* — every (timeframe, history-window) pair — and crowns whoever
earned the highest average return across the cells they could trade. Each
cell is one ordinary comparison: the same bots on byte-identical scenarios
(one frozen window end, one seed), so within a cell the only variable is
the strategy, and across cells the only variables are the timeframe and
how far back the history reaches.

It is built on the existing comparison machinery rather than beside it: the
orchestrator drives ``EvaluationManager.start_comparison`` one cell at a
time and polls the run rows to completion, exactly as the auto-improver
polls its sweeps. That keeps a single research workload on the CPU at once
(the live candle loop is never starved) and means every per-cell number is
a normal, inspectable evaluation run.

Feasibility is honest: a short window on a high timeframe may hold too few
candles to host a single scenario (a 10-day window is ten daily candles).
Such a cell is recorded as ``insufficient_data`` and simply excluded from
the averages — a bot is never charged for a cell nobody could trade.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Coroutine, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import ROUND_HALF_EVEN, Decimal
from itertools import product
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from tradebot.core.models import CandleInterval, utc_now
from tradebot.evaluation.models import RunStatus
from tradebot.evaluation.presets import BAKE_OFF_CONTESTANTS
from tradebot.evaluation.runner import EvaluationManager, EvaluationRunConfig
from tradebot.persistence.bakeoff_store import BakeOffStore
from tradebot.persistence.evaluation_store import EvaluationStore

logger = logging.getLogger(__name__)

DEFAULT_TIMEFRAMES: tuple[str, ...] = ("1h", "4h", "1d")
"""The candle intervals the grid sweeps, fast to slow."""

DEFAULT_HISTORY_WINDOWS: tuple[int, ...] = (10, 50, 100)
"""History depths in days the grid sweeps, recent to deep."""

DEFAULT_SCENARIO_COUNT = 150
"""Scenarios per contestant per cell. Lower than a solo evaluation's
default: a bake-off runs this many for every contestant in every cell, so
the budget trades some statistical depth for finishing in reasonable time."""

DEFAULT_LOOKBACK_CANDLES = 120
"""Context per scenario. Covers the slowest preset indicator (an 80-period
EMA) with slack, while staying small enough that the shorter windows can
still host scenarios."""

DEFAULT_HORIZON_CANDLES = 30
"""Candles of future revealed for grading each decision."""

POLL_SECONDS = 5.0
"""How often the orchestrator re-checks a cell's runs for completion."""

CELL_TIMEOUT_SECONDS = 60.0 * 60.0
"""A single cell silent this long is abandoned and marked failed; the
bake-off moves on rather than hanging forever on one stuck comparison."""

_RETURN_FRACTION_RESOLUTION = Decimal("0.0001")
"""Display resolution for the ranking's averaged return — the same four
places ``money_result`` rounds a return fraction to, so the leaderboard
never persists an unbounded-precision quotient (e.g. ``0.11666...67``)."""

_TERMINAL = {RunStatus.COMPLETED.value, RunStatus.FAILED.value, RunStatus.INTERRUPTED.value}


class BakeOffConfig(BaseModel):
    """One bake-off's grid and scenario shape; snapshotted into the job row."""

    model_config = ConfigDict(frozen=True)

    symbols: tuple[str, ...] = Field(min_length=1)
    timeframes: tuple[str, ...] = DEFAULT_TIMEFRAMES
    history_windows: tuple[int, ...] = DEFAULT_HISTORY_WINDOWS
    scenario_count: int = Field(default=DEFAULT_SCENARIO_COUNT, gt=0)
    lookback_candles: int = Field(default=DEFAULT_LOOKBACK_CANDLES, ge=60)
    horizon_candles: int = Field(default=DEFAULT_HORIZON_CANDLES, gt=0)
    seed: int = 7

    def validated(self) -> BakeOffConfig:
        """Parse the timeframes and windows, raising ``ValueError`` on bad ones."""
        if not self.timeframes:
            raise ValueError("a bake-off needs at least one timeframe")
        if not self.history_windows:
            raise ValueError("a bake-off needs at least one history window")
        tuple(CandleInterval(timeframe) for timeframe in self.timeframes)
        if any(days <= 0 for days in self.history_windows):
            raise ValueError("history windows must be positive day counts")
        return self


@dataclass(frozen=True)
class BakeOffCell:
    """One grid cell: one timeframe at one history depth."""

    timeframe: str
    history_days: int


def expand_cells(config: BakeOffConfig) -> list[BakeOffCell]:
    """Return the grid's cells, deepest history first within each timeframe.

    Deepest-first so the most likely-feasible cell of each timeframe runs
    before the short ones that may turn out to hold too little history.
    """
    return [
        BakeOffCell(timeframe=timeframe, history_days=days)
        for timeframe, days in product(
            config.timeframes, sorted(config.history_windows, reverse=True)
        )
    ]


@dataclass(frozen=True)
class ContestantRank:
    """One contestant's standing across the whole grid."""

    bot_id: str
    average_return_fraction: Decimal
    cells_scored: int
    total_trades: int


def aggregate_ranking(cells: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """Rank contestants by average return across the cells they traded.

    ``cells`` is the per-cell record the orchestrator builds (one dict per
    grid cell, each carrying a ``results`` map of bot_id -> {return_fraction,
    trade_count}). A contestant's score is the mean of its per-cell return
    fractions over feasible cells only; cells flagged anything other than
    ``completed`` contribute nothing, so no bot is charged for a window that
    held too little history. Ties break by total trades then bot id, so the
    order is deterministic. Returned newest-money-first as JSON-able dicts
    (Decimals stringified) ready for the job row.
    """
    returns: dict[str, list[Decimal]] = {}
    trades: dict[str, int] = {}
    for cell in cells:
        if cell.get("status") != "completed":
            continue
        for bot_id, result in cell.get("results", {}).items():
            fraction = result.get("return_fraction")
            if fraction is None:
                continue
            returns.setdefault(bot_id, []).append(Decimal(str(fraction)))
            trades[bot_id] = trades.get(bot_id, 0) + int(result.get("trade_count", 0))
    ranks = [
        ContestantRank(
            bot_id=bot_id,
            average_return_fraction=sum(values, Decimal(0)) / len(values),
            cells_scored=len(values),
            total_trades=trades.get(bot_id, 0),
        )
        for bot_id, values in returns.items()
    ]
    # Rank on the full-precision average so ties are real ties, then round
    # only the persisted/displayed figure to the leaderboard's resolution.
    ranks.sort(key=lambda r: (r.average_return_fraction, r.total_trades, r.bot_id), reverse=True)
    return [
        {
            "bot_id": rank.bot_id,
            "average_return_fraction": str(
                rank.average_return_fraction.quantize(
                    _RETURN_FRACTION_RESOLUTION, rounding=ROUND_HALF_EVEN
                )
            ),
            "cells_scored": rank.cells_scored,
            "total_trades": rank.total_trades,
        }
        for rank in ranks
    ]


class BakeOffManager:
    """Owns the single in-flight bake-off and its background task.

    One bake-off at a time: it already serializes many comparisons through
    the evaluation manager, and a second concurrent bake-off would only
    fight it for the same research lane.
    """

    def __init__(
        self,
        evaluations: EvaluationManager,
        evaluation_store: EvaluationStore,
        store: BakeOffStore,
        spawn: Callable[[Coroutine[Any, Any, None]], asyncio.Task[None]],
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        """Bind the orchestrator to the evaluation manager and the stores."""
        self._evaluations = evaluations
        self._evaluation_store = evaluation_store
        self._store = store
        self._spawn = spawn
        self._clock = clock
        self._contestants = tuple(BAKE_OFF_CONTESTANTS)
        self._task: asyncio.Task[None] | None = None
        self._job_id: int | None = None

    def _require_idle(self) -> None:
        if self._task is not None and not self._task.done():
            raise RuntimeError(f"bake-off {self._job_id} already in progress")

    async def start(self, config: BakeOffConfig) -> int:
        """Create the job row and launch it; one bake-off at a time.

        Raises ``RuntimeError`` if a bake-off is already running and
        ``ValueError`` for a malformed grid.
        """
        config.validated()
        self._require_idle()
        cells = expand_cells(config)
        job_id = await self._store.create_job(
            config=config.model_dump(mode="json"),
            contestants=[c.bot_id for c in self._contestants],
            cells_total=len(cells),
            created_at=self._clock(),
        )
        self._job_id = job_id
        self._task = self._spawn(self._execute(job_id, config, cells))
        logger.info(
            "bake-off %d started: %d cells, %d contestants",
            job_id,
            len(cells),
            len(self._contestants),
        )
        return job_id

    async def _execute(self, job_id: int, config: BakeOffConfig, cells: list[BakeOffCell]) -> None:
        """Drive the bake-off to a terminal status; never raises but cancel."""
        contestant_ids = [c.bot_id for c in self._contestants]
        frozen_end = self._clock()
        cell_records: list[dict[str, Any]] = []
        try:
            await self._store.set_status(job_id, RunStatus.RUNNING)
            for index, cell in enumerate(cells):
                record = await self._run_cell(config, cell, contestant_ids, frozen_end)
                cell_records.append(record)
                await self._store.update_progress(
                    job_id,
                    cells_done=index + 1,
                    results={"cells": cell_records, "ranking": aggregate_ranking(cell_records)},
                )
            await self._store.complete_job(
                job_id, {"cells": cell_records, "ranking": aggregate_ranking(cell_records)}
            )
            logger.info("bake-off %d completed: %d cells graded", job_id, len(cell_records))
        except asyncio.CancelledError:
            # Shield the terminal write so cancellation (shutdown) cannot
            # interrupt it mid-flight and strand the job at "running"; the
            # CancelledError must still propagate, so a DB failure here is
            # logged, never allowed to replace it.
            try:
                await asyncio.shield(self._store.set_status(job_id, RunStatus.INTERRUPTED))
            except Exception:
                logger.exception("bake-off %d: could not record interrupted status", job_id)
            logger.warning("bake-off %d interrupted", job_id)
            raise
        except Exception:
            logger.exception("bake-off %d failed", job_id)
            # The failure may be the database itself; a raising fallback
            # write would escape into the worker's TaskGroup and cancel
            # every sibling task (live trading included). Guard it.
            try:
                await self._store.set_status(job_id, RunStatus.FAILED)
            except Exception:
                logger.exception("bake-off %d: could not record failed status", job_id)

    async def _run_cell(
        self,
        config: BakeOffConfig,
        cell: BakeOffCell,
        contestant_ids: Sequence[str],
        frozen_end: datetime,
    ) -> dict[str, Any]:
        """Run one cell's comparison and collect each contestant's return.

        Retries the start while the evaluation lane is busy (another run or
        the auto-improver holds it) rather than failing the cell. A cell
        whose runs all evaluated nothing is reported ``insufficient_data``.
        """
        run_config = EvaluationRunConfig(
            symbols=config.symbols,
            timeframes=(cell.timeframe,),
            history_days=cell.history_days,
            scenario_count=config.scenario_count,
            lookback_candles=config.lookback_candles,
            horizon_candles=config.horizon_candles,
            seed=config.seed,
            window_end=frozen_end,
        )
        run_ids = await self._start_when_free(run_config, contestant_ids)
        await self._await_runs(run_ids)
        results: dict[str, dict[str, Any]] = {}
        completed = 0
        for bot_id, run_id in zip(contestant_ids, run_ids, strict=True):
            run = await self._evaluation_store.fetch_run(run_id)
            summary = run.get("summary") if run is not None else None
            if run is not None and run.get("status") == RunStatus.COMPLETED.value and summary:
                completed += 1
                results[bot_id] = {
                    "return_fraction": summary.get("return_fraction", "0"),
                    "net_pnl_quote": summary.get("net_pnl_quote", "0"),
                    "trade_count": summary.get("trade_count", 0),
                }
        return {
            "timeframe": cell.timeframe,
            "history_days": cell.history_days,
            "comparison_group": run_ids[0] if run_ids else None,
            "status": "completed" if completed else "insufficient_data",
            "results": results,
        }

    async def _start_when_free(
        self, run_config: EvaluationRunConfig, contestant_ids: Sequence[str]
    ) -> list[int]:
        """Start the cell's comparison, waiting out a busy evaluation lane."""
        while True:
            try:
                return await self._evaluations.start_comparison(run_config, contestant_ids)
            except RuntimeError:
                # The lane is busy (a manual run or the improver). Wait and
                # retry — the bake-off is patient, not a queue-jumper.
                await asyncio.sleep(POLL_SECONDS)

    async def _await_runs(self, run_ids: Sequence[int]) -> None:
        """Poll until every run in the cell reaches a terminal status."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + CELL_TIMEOUT_SECONDS
        while loop.time() < deadline:
            statuses = []
            for run_id in run_ids:
                run = await self._evaluation_store.fetch_run(run_id)
                statuses.append(run["status"] if run is not None else RunStatus.PENDING.value)
            if all(status in _TERMINAL for status in statuses):
                return
            await asyncio.sleep(POLL_SECONDS)
        logger.warning("bake-off cell timed out waiting on runs %s", list(run_ids))
