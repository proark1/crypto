"""Bake-off engine: grid expansion, ranking math, and the cell orchestration.

The grid and aggregation are pure functions, tested directly. The manager
is exercised against in-memory fakes (no Postgres): a fake evaluation
manager that hands back run ids, a fake evaluation store that returns
scripted run summaries, and a fake bake-off store that records the job's
progress — enough to prove the orchestration runs every cell, skips the
infeasible ones, and ranks by average return.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import pytest

from tradebot.evaluation.bakeoff import (
    BakeOffConfig,
    BakeOffManager,
    aggregate_ranking,
    expand_cells,
)
from tradebot.evaluation.models import RunStatus
from tradebot.evaluation.runner import EvaluationRunConfig


class TestExpandCells:
    def test_grid_is_the_full_product_deepest_history_first(self) -> None:
        config = BakeOffConfig(
            symbols=("BTC/USDT",),
            grid=(("1h", (10, 100, 50)), ("4h", (10, 100, 50))),
        )
        cells = expand_cells(config)
        assert len(cells) == 6  # 2 timeframes x 3 windows
        # Within a timeframe, deepest window first.
        assert [(c.timeframe, c.history_days) for c in cells] == [
            ("1h", 100),
            ("1h", 50),
            ("1h", 10),
            ("4h", 100),
            ("4h", 50),
            ("4h", 10),
        ]

    def test_default_grid_is_three_by_three(self) -> None:
        cells = expand_cells(BakeOffConfig(symbols=("BTC/USDT",)))
        assert len(cells) == 9


class TestAggregateRanking:
    def _cell(self, status: str, results: dict[str, dict[str, Any]]) -> dict[str, Any]:
        return {"timeframe": "1h", "history_days": 100, "status": status, "results": results}

    def test_ranks_by_average_return_across_feasible_cells(self) -> None:
        cells = [
            self._cell(
                "completed",
                {
                    "alpha": {"return_fraction": "0.10", "trade_count": 5},
                    "beta": {"return_fraction": "0.02", "trade_count": 9},
                },
            ),
            self._cell(
                "completed",
                {
                    "alpha": {"return_fraction": "0.20", "trade_count": 3},
                    "beta": {"return_fraction": "0.04", "trade_count": 1},
                },
            ),
        ]
        ranking = aggregate_ranking(cells)
        assert [r["bot_id"] for r in ranking] == ["alpha", "beta"]
        # (0.10 + 0.20) / 2, quantized to the leaderboard's four places.
        assert ranking[0]["average_return_fraction"] == "0.1500"
        assert ranking[0]["cells_scored"] == 2
        assert ranking[0]["total_trades"] == 8

    def test_insufficient_cells_contribute_nothing(self) -> None:
        cells = [
            self._cell("completed", {"alpha": {"return_fraction": "0.10", "trade_count": 2}}),
            self._cell("insufficient_data", {}),
        ]
        ranking = aggregate_ranking(cells)
        assert len(ranking) == 1
        assert ranking[0]["cells_scored"] == 1  # the infeasible cell did not count

    def test_no_feasible_cells_yields_an_empty_ranking(self) -> None:
        assert aggregate_ranking([self._cell("insufficient_data", {})]) == []

    def test_ties_break_deterministically_by_trades_then_id(self) -> None:
        cells = [
            self._cell(
                "completed",
                {
                    "low_trades": {"return_fraction": "0.05", "trade_count": 1},
                    "high_trades": {"return_fraction": "0.05", "trade_count": 9},
                },
            )
        ]
        ranking = aggregate_ranking(cells)
        assert [r["bot_id"] for r in ranking] == ["high_trades", "low_trades"]

    def test_average_return_is_quantized_not_unbounded(self) -> None:
        # 0.10 and 0.05 over three cells average to 0.0833..., which must be
        # rounded to the leaderboard's four places, never persisted as a
        # repeating-decimal string.
        cells = [
            self._cell("completed", {"a": {"return_fraction": "0.10", "trade_count": 1}}),
            self._cell("completed", {"a": {"return_fraction": "0.10", "trade_count": 1}}),
            self._cell("completed", {"a": {"return_fraction": "0.05", "trade_count": 1}}),
        ]
        ranking = aggregate_ranking(cells)
        assert ranking[0]["average_return_fraction"] == "0.0833"


class FakeEvaluations:
    """Records each cell's comparison and hands back sequential run ids."""

    def __init__(self) -> None:
        self.calls: list[tuple[EvaluationRunConfig, list[str]]] = []
        self._next_id = 1

    async def start_comparison(
        self, config: EvaluationRunConfig, strategies: Sequence[str]
    ) -> list[int]:
        ids = list(range(self._next_id, self._next_id + len(strategies)))
        self._next_id += len(strategies)
        self.calls.append((config, list(strategies)))
        return ids


class FakeEvaluationStore:
    """Returns a scripted summary per run id; missing means insufficient data."""

    def __init__(self, summaries: dict[int, dict[str, Any] | None]) -> None:
        self._summaries = summaries

    async def fetch_run(self, run_id: int) -> dict[str, Any] | None:
        summary = self._summaries.get(run_id)
        status = RunStatus.COMPLETED.value if summary else RunStatus.FAILED.value
        return {"id": run_id, "status": status, "summary": summary}


class FakeBakeOffStore:
    """Captures the job lifecycle without a database."""

    def __init__(self) -> None:
        self.created: dict[str, Any] | None = None
        self.progress: list[tuple[int, dict[str, Any]]] = []
        self.final: dict[str, Any] | None = None
        self.status: str | None = None

    async def create_job(self, config, contestants, cells_total, created_at) -> int:  # type: ignore[no-untyped-def]
        self.created = {
            "config": config,
            "contestants": list(contestants),
            "cells_total": cells_total,
        }
        return 1

    async def set_status(self, job_id: int, status: RunStatus) -> None:
        self.status = status.value

    async def update_progress(self, job_id: int, cells_done: int, results: dict[str, Any]) -> None:
        self.progress.append((cells_done, results))

    async def complete_job(self, job_id: int, results: dict[str, Any]) -> None:
        self.final = results
        self.status = RunStatus.COMPLETED.value


def _spawn(coro):  # type: ignore[no-untyped-def]
    import asyncio

    return asyncio.ensure_future(coro)


class TestBakeOffManager:
    async def test_runs_every_cell_and_ranks_by_average_return(self) -> None:
        # Two contestants, a 1x2 grid (one timeframe, two windows) -> two
        # cells, four runs. The first cell's runs (1, 2) made money; the
        # second cell's runs (3, 4) are absent -> insufficient data.
        evaluations = FakeEvaluations()
        store = FakeEvaluationStore(
            {
                1: {"return_fraction": "0.05", "trade_count": 4},
                2: {"return_fraction": "0.10", "trade_count": 2},
            }
        )
        jobs = FakeBakeOffStore()
        manager = BakeOffManager(evaluations, store, jobs, spawn=_spawn)  # type: ignore[arg-type]
        manager._contestants = manager._contestants[:2]  # shrink roster for the test

        config = BakeOffConfig(symbols=("BTC/USDT",), grid=(("1h", (100, 10)),))
        job_id = await manager.start(config)
        assert job_id == 1
        assert manager._task is not None
        await manager._task

        # Both cells ran (deepest first): two comparison batches.
        assert len(evaluations.calls) == 2
        assert evaluations.calls[0][0].history_days == 100
        assert evaluations.calls[1][0].history_days == 10
        # The job completed with a ranking over the one feasible cell.
        assert jobs.status == RunStatus.COMPLETED.value
        assert jobs.final is not None
        ranking = jobs.final["ranking"]
        # Run 2's contestant outscored run 1's; run 3/4's cell was infeasible.
        assert [r["bot_id"] for r in ranking] == [
            manager._contestants[1].bot_id,
            manager._contestants[0].bot_id,
        ]
        assert all(r["cells_scored"] == 1 for r in ranking)

    async def test_a_second_bake_off_is_refused_while_one_runs(self) -> None:
        fakes = (FakeEvaluations(), FakeEvaluationStore({}), FakeBakeOffStore())
        manager = BakeOffManager(*fakes, spawn=_spawn)  # type: ignore[arg-type]

        async def _never(*args: Any, **kwargs: Any) -> None:
            import asyncio

            await asyncio.Event().wait()  # block forever

        manager._execute = _never  # type: ignore[method-assign]
        await manager.start(BakeOffConfig(symbols=("BTC/USDT",)))
        with pytest.raises(RuntimeError, match="already in progress"):
            await manager.start(BakeOffConfig(symbols=("BTC/USDT",)))
        assert manager._task is not None
        manager._task.cancel()
