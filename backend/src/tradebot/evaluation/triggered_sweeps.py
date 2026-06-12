"""Accept-triggered sweeps: a verdict becomes a test, visibly (§12.7).

Accepting a finding used to be a record and nothing else; the loop only
picked it up on the next scheduled cycle. This module closes that gap:
the first acceptance on a run arms a short coalescing timer, every
further acceptance before it fires rides the same sweep, and when the
timer fires the run's findings are turned into the same findings-targeted
challenger grid the §12.7 cycle uses — accepted findings outrank proposed
ones — and swept as soon as the single-flight lane is free.

Coalescing exists for statistics, not convenience: one sweep whose grid
covers every accepted finding spends one Bonferroni budget; a sweep per
click would fragment the evidence and tighten the bar for all of them.

Nothing here promotes. The sweep's verdict feeds the same paths as every
other sweep (the §12.7 cycle promotes only *validated* winners, paper
only); this module only decides when a targeted sweep starts and records
the accepted finding ids as its motivation.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Coroutine, Mapping
from datetime import timedelta
from typing import Any, Protocol

from tradebot.evaluation.improve import (
    IMPROVEMENT_SCENARIO_COUNT,
    build_improvement_candidates,
    select_targeting_findings,
)
from tradebot.evaluation.models import LearningFinding
from tradebot.evaluation.sweep import SweepConfig

logger = logging.getLogger(__name__)

BUSY_RETRY_SECONDS = 60.0
"""How long to wait before retrying when the single-flight lane is busy."""

BUSY_DEADLINE = timedelta(hours=4)
"""Give up (loudly) when the lane never frees up; the next acceptance —
or the scheduled cycle — will try again."""


class SweepStarter(Protocol):
    """The slice of ``SweepManager`` the scheduler depends on."""

    async def start(self, config: SweepConfig) -> int:
        """Create and launch a sweep; raises ``RuntimeError`` if one runs."""
        ...


class FindingsReader(Protocol):
    """The slice of ``EvaluationStore`` the scheduler depends on."""

    async def fetch_run(self, run_id: int) -> dict[str, Any] | None:
        """Return the run row, or ``None`` if unknown."""
        ...

    async def fetch_findings(self, run_id: int) -> list[tuple[int, LearningFinding]]:
        """Return one run's findings with their database ids."""
        ...


class AcceptSweepScheduler:
    """Coalesces acceptances per run and fires one targeted sweep each.

    ``spawn`` ties timers to the worker's TaskGroup so shutdown cancels
    them; a cancelled timer simply never fires (the scheduled §12.7 cycle
    remains the backstop for anything lost that way).
    """

    def __init__(
        self,
        *,
        sweeps: SweepStarter,
        store: FindingsReader,
        active_params: Callable[[], Mapping[str, Mapping[str, Any]]],
        spawn: Callable[[Coroutine[Any, Any, None]], asyncio.Task[None]],
        delay: timedelta,
        timeframe: str,
        history_days: int,
        notify: Callable[[str], Awaitable[None]] | None = None,
    ) -> None:
        """Bind the worker's research lane and the coalescing delay."""
        self._sweeps = sweeps
        self._store = store
        self._active_params = active_params
        self._spawn = spawn
        self._delay = delay
        self._timeframe = timeframe
        self._history_days = history_days
        self._notify = notify
        self._pending: dict[int, asyncio.Task[None]] = {}

    def pending_run_ids(self) -> frozenset[int]:
        """Return runs whose coalescing timer is armed (not yet fired)."""
        return frozenset(run_id for run_id, task in self._pending.items() if not task.done())

    def note_acceptance(self, run_id: int) -> None:
        """Arm the run's coalescing timer, or ride one already armed.

        The timer fires a fixed delay after the *first* acceptance —
        bounded latency — and reads finding statuses at fire time, so
        every acceptance inside the window is included without resets.
        """
        existing = self._pending.get(run_id)
        if existing is not None and not existing.done():
            return  # this acceptance rides the armed timer
        self._pending[run_id] = self._spawn(self._fire_after_delay(run_id))
        logger.info(
            "acceptance on run %d armed a targeted sweep in %.0fs",
            run_id,
            self._delay.total_seconds(),
        )

    async def _fire_after_delay(self, run_id: int) -> None:
        try:
            await asyncio.sleep(self._delay.total_seconds())
            await self._fire(run_id)
        finally:
            self._pending.pop(run_id, None)

    async def _fire(self, run_id: int) -> None:
        """Build the grid from the run's findings and start the sweep.

        The single-flight lane may be busy (a run, another sweep); retry
        on a slow cadence until a deadline, then give up loudly — the
        scheduled cycle is the backstop, never silence.
        """
        run = await self._store.fetch_run(run_id)
        if run is None:
            logger.warning("accept-sweep for run %d skipped: run vanished", run_id)
            return
        findings = select_targeting_findings(await self._store.fetch_findings(run_id))
        candidates, motivating = build_improvement_candidates(self._active_params(), findings)
        symbols = list(run.get("symbols") or [])
        if not symbols:
            logger.warning("accept-sweep for run %d skipped: run has no symbols", run_id)
            return
        config = SweepConfig(
            # The first symbol of the run whose findings motivated this
            # sweep: the patterns were mined from that data.
            symbol=symbols[0],
            timeframe=self._timeframe,
            history_days=self._history_days,
            scenario_count=IMPROVEMENT_SCENARIO_COUNT,
            candidates=candidates,
            motivating_finding_ids=motivating,
        )
        loop = asyncio.get_running_loop()
        deadline = loop.time() + BUSY_DEADLINE.total_seconds()
        while True:
            try:
                sweep_id = await self._sweeps.start(config)
            except RuntimeError:
                if loop.time() >= deadline:
                    message = (
                        f"the sweep queued by accepting findings on run #{run_id} gave up "
                        f"after {BUSY_DEADLINE} waiting for the research lane; the scheduled "
                        "improvement cycle will pick the findings up instead"
                    )
                    logger.warning("%s", message)
                    if self._notify is not None:
                        await self._notify(message)
                    return
                await asyncio.sleep(BUSY_RETRY_SECONDS)
                continue
            logger.info(
                "accepted findings on run %d started targeted sweep %d on %s",
                run_id,
                sweep_id,
                config.symbol,
            )
            return
