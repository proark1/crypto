"""Persistence for bake-off jobs (ARCHITECTURE.md section 13.8).

A bake-off job is the layer above a batch of comparison runs: it records
the grid that was swept, the roster that ran, and the ranking that came
out. Like evaluation runs it is append-mostly — a job advances through
status and progress and accumulates results, and a finished job is never
rescored. The per-cell evaluation runs themselves live in
``evaluation_runs`` (linked by ``comparison_group``); this store owns only
the job envelope and its aggregated ranking.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import datetime
from typing import Any

from sqlalchemy import select, update

from tradebot.evaluation.models import RunStatus
from tradebot.persistence.database import Database, bake_off_jobs_table


def _require_aware(moment: datetime) -> None:
    """Reject naive datetimes (repo invariant: timestamps are UTC-aware)."""
    if moment.tzinfo is None:
        raise ValueError("naive datetime is not allowed; timestamps must be UTC-aware")


class BakeOffStore:
    """Typed access to the ``bake_off_jobs`` table."""

    def __init__(self, database: Database) -> None:
        """Bind the store to ``database``."""
        self._database = database

    async def create_job(
        self,
        config: dict[str, Any],
        contestants: Sequence[str],
        cells_total: int,
        created_at: datetime,
    ) -> int:
        """Create a pending job; returns its id.

        ``config`` is the full grid + scenario snapshot. Decimals (if any
        ever enter the grid config) are stringified on the way in, the same
        JSONB discipline the evaluation store keeps.
        """
        _require_aware(created_at)
        encodable_config = json.loads(json.dumps(config, default=str))
        statement = (
            bake_off_jobs_table.insert()
            .values(
                created_at=created_at,
                updated_at=created_at,
                status=RunStatus.PENDING.value,
                config=encodable_config,
                contestants=list(contestants),
                cells_done=0,
                cells_total=cells_total,
                results=None,
            )
            .returning(bake_off_jobs_table.c.id)
        )
        async with self._database.engine.begin() as connection:
            job_id: int = (await connection.execute(statement)).scalar_one()
        return job_id

    async def set_status(self, job_id: int, status: RunStatus) -> None:
        """Advance the job's lifecycle (pending → running → terminal)."""
        await self._update(job_id, {"status": status.value})

    async def update_progress(self, job_id: int, cells_done: int, results: dict[str, Any]) -> None:
        """Record cells finished and the running ranking after a cell.

        Persisting the partial ranking each cell means the UI shows a live
        leaderboard rather than nothing until the whole grid is done.
        """
        await self._update(
            job_id,
            {
                "cells_done": cells_done,
                "results": json.loads(json.dumps(results, default=str)),
                "status": RunStatus.RUNNING.value,
            },
        )

    async def complete_job(self, job_id: int, results: dict[str, Any]) -> None:
        """Mark the job completed and attach its final ranking."""
        await self._update(
            job_id,
            {
                "status": RunStatus.COMPLETED.value,
                "results": json.loads(json.dumps(results, default=str)),
            },
        )

    async def _update(self, job_id: int, values: dict[str, Any]) -> None:
        statement = (
            update(bake_off_jobs_table)
            .where(bake_off_jobs_table.c.id == job_id)
            .values(updated_at=_utc_now(), **values)
        )
        async with self._database.engine.begin() as connection:
            await connection.execute(statement)

    async def fetch_job(self, job_id: int) -> dict[str, Any] | None:
        """Return one job row as a mapping, or ``None`` if unknown."""
        statement = select(bake_off_jobs_table).where(bake_off_jobs_table.c.id == job_id)
        async with self._database.engine.connect() as connection:
            row = (await connection.execute(statement)).mappings().first()
        return None if row is None else dict(row)

    async def list_jobs(self, limit: int = 20) -> list[dict[str, Any]]:
        """Return the newest jobs first (the research screen's bake-off list)."""
        statement = (
            select(bake_off_jobs_table).order_by(bake_off_jobs_table.c.id.desc()).limit(limit)
        )
        async with self._database.engine.connect() as connection:
            rows = (await connection.execute(statement)).mappings().all()
        return [dict(row) for row in rows]


def _utc_now() -> datetime:
    """Local import shim kept tiny so the store has no heavy dependency."""
    from tradebot.core.models import utc_now

    return utc_now()
