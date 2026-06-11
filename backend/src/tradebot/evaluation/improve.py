"""Automated improvement: sweep the active config, promote what validates.

The loop (ARCHITECTURE.md §12.7) closes the research cycle without a human
in the middle: on a schedule it derives challenger variants from the
parameters the bot is trading *right now*, runs them through the blind
walk-forward sweep, and promotes the winner only when the verdict is
**validated** — the Bonferroni-corrected, multi-window statistical bar.
Training wins, near-misses, and findings never promote anything.

Scope is deliberate: promotions apply to the paper bot (the worker refuses
live mode outright), every promotion is journaled as a strategy-settings
version carrying its sweep as lineage, and a human can revert any version
through the API. Going live remains a human decision in every mode.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from tradebot.evaluation.models import LearningFinding, RunStatus
from tradebot.evaluation.runner import EvaluationRunConfig
from tradebot.evaluation.sweep import SweepCandidate, SweepConfig
from tradebot.strategies import MeanReversionConfig, TrendFollowingConfig

logger = logging.getLogger(__name__)

PROMOTION_VERDICT = "validated"
"""The only sweep verdict that may change the traded configuration."""

IMPROVEMENT_SCENARIO_COUNT = 400
"""Scenarios per candidate per period in automated research. The strategy
trades in only a few percent of scenarios, so a small count starves every
sweep below MIN_SWEEP_TRADES and the loop can never validate anything —
exactly the "insufficient evidence on every sweep" failure observed in
production."""

STALE_RUN_CYCLES = 2
"""A completed evaluation older than this many improvement intervals no
longer describes the configuration now trading; the cycle re-evaluates
before sweeping."""

POLL_SECONDS = 30.0
"""How often a running sweep is re-checked for a terminal status."""

SWEEP_TIMEOUT = timedelta(hours=8)
"""A sweep silent for this long is abandoned (the next cycle retries)."""

_TERMINAL = {RunStatus.COMPLETED.value, RunStatus.FAILED.value, RunStatus.INTERRUPTED.value}


class SweepStarter(Protocol):
    """The slice of ``SweepManager`` the improver depends on."""

    async def start(self, config: SweepConfig) -> int:
        """Create and launch a sweep; raises ``RuntimeError`` if one runs."""
        ...


class ResearchReader(Protocol):
    """The slice of ``EvaluationStore`` the improver depends on."""

    async def fetch_sweep(self, sweep_id: int) -> dict[str, Any] | None:
        """Return the sweep row (status + report), or ``None`` if unknown."""
        ...

    async def list_runs(self, limit: int = 50) -> list[dict[str, Any]]:
        """Return evaluation runs, newest first."""
        ...

    async def fetch_findings(self, run_id: int) -> list[tuple[int, LearningFinding]]:
        """Return one run's mined findings with their database ids."""
        ...


class EvaluationStarter(Protocol):
    """The slice of ``EvaluationManager`` the improver depends on."""

    async def start(self, config: EvaluationRunConfig) -> int:
        """Create and launch a run; raises ``RuntimeError`` if one runs."""
        ...


def build_improvement_candidates(
    active: Mapping[str, Mapping[str, Any]],
    findings: Sequence[tuple[int, str]] = (),
) -> tuple[tuple[SweepCandidate, ...], tuple[int, ...]]:
    """Derive one challenger grid from the active parameters and findings.

    Returns ``(candidates, motivating_finding_ids)``. The active trend
    configuration is the baseline (``candidates[0]``, as the sweep
    contract requires); each variant changes a single knob by a
    multiplicative step so the journal can name what earned a promotion.
    Steps are clamped to valid configurations (fast EMA strictly below
    slow, stops never collapsing to zero) and variants that clamp into a
    copy of an existing candidate are dropped — sweeping a candidate
    against itself would only spend the significance budget.

    ``findings`` — ``(id, pattern)`` pairs mined from the latest
    evaluation run — add *targeted* challengers: a losing-downtrend
    pattern toggles the mean-reversion trend filter, a chasing pattern
    toggles the trend family's extension filter. This is where the bot
    learns from its own graded record: the pattern names the knob, the
    sweep proves or refutes it, and the finding ids ride along as the
    sweep's recorded motivation (§12.5 lineage). Candidates are added
    only when their pattern actually fired — every extra candidate
    tightens the Bonferroni bar for all of them.
    """
    trend = TrendFollowingConfig(**active.get("trend_following", {}))
    reversion = MeanReversionConfig(**active.get("mean_reversion", {}))

    fast, slow = trend.fast_ema_period, trend.slow_ema_period
    faster_fast = max(3, round(fast * 0.6))
    slower_fast = round(fast * 1.5)
    raw: list[SweepCandidate] = [
        SweepCandidate(name=f"active_trend_{fast}_{slow}", params=trend.model_dump()),
        SweepCandidate(
            name="faster_cross",
            params=trend.model_copy(
                update={
                    "fast_ema_period": faster_fast,
                    "slow_ema_period": max(faster_fast + 2, round(slow * 0.6)),
                }
            ).model_dump(),
        ),
        SweepCandidate(
            name="slower_cross",
            params=trend.model_copy(
                update={
                    "fast_ema_period": slower_fast,
                    "slow_ema_period": max(slower_fast + 2, round(slow * 1.5)),
                }
            ).model_dump(),
        ),
        SweepCandidate(
            name="wider_stop",
            params=trend.model_copy(
                update={"atr_stop_multiple": round(trend.atr_stop_multiple * 1.5, 2)}
            ).model_dump(),
        ),
        SweepCandidate(
            name="tighter_stop",
            params=trend.model_copy(
                update={"atr_stop_multiple": max(0.5, round(trend.atr_stop_multiple * 0.75, 2))}
            ).model_dump(),
        ),
        SweepCandidate(
            name="active_reversion",
            family="mean_reversion",
            params=reversion.model_dump(),
        ),
    ]
    motivating: list[int] = []
    downtrend_ids = [
        finding_id
        for finding_id, pattern in findings
        if "trend is down" in pattern or "trend is ranging" in pattern
    ]
    if downtrend_ids:
        motivating += downtrend_ids
        filter_toggle = 50 if reversion.trend_filter_ema_period == 0 else 0
        raw.append(
            SweepCandidate(
                name=("trend_filtered_reversion" if filter_toggle else "unfiltered_reversion"),
                family="mean_reversion",
                params=reversion.model_copy(
                    update={"trend_filter_ema_period": filter_toggle}
                ).model_dump(),
            )
        )
    chase_ids = [finding_id for finding_id, pattern in findings if "chase" in pattern]
    if chase_ids:
        motivating += chase_ids
        chase_toggle = 2.0 if trend.max_entry_extension_atr == 0 else 0.0
        raw.append(
            SweepCandidate(
                name="anti_chase" if chase_toggle else "no_chase_filter",
                params=trend.model_copy(
                    update={"max_entry_extension_atr": chase_toggle}
                ).model_dump(),
            )
        )
    seen: set[tuple[str, tuple[tuple[str, Any], ...]]] = set()
    unique: list[SweepCandidate] = []
    for candidate in raw:
        key = (candidate.family, tuple(sorted(candidate.params.items())))
        if key in seen:
            continue
        seen.add(key)
        unique.append(candidate)
    return tuple(unique), tuple(motivating)


class AutoImprover:
    """Runs improvement cycles forever; one rotating symbol per cycle."""

    def __init__(
        self,
        *,
        sweeps: SweepStarter,
        evaluations: EvaluationStarter,
        store: ResearchReader,
        active_params: Callable[[], Mapping[str, Mapping[str, Any]]],
        symbols: Callable[[], tuple[str, ...]],
        promote: Callable[[str, Mapping[str, Any], int | None, str | None], Awaitable[int]],
        interval: timedelta,
        history_days: int,
        timeframe: str,
        notify: Callable[[str], Awaitable[None]] | None = None,
    ) -> None:
        """Bind the loop to the worker's live state.

        Everything stateful arrives as callables (``active_params``,
        ``symbols``) because coins and configurations change at runtime —
        a cycle must see the world as it is, not as it was at boot.
        ``promote`` is the worker's apply path: persist + hot-swap.
        """
        self._sweeps = sweeps
        self._evaluations = evaluations
        self._store = store
        self._active_params = active_params
        self._symbols = symbols
        self._promote = promote
        self._interval = interval
        self._history_days = history_days
        self._timeframe = timeframe
        self._notify = notify
        self._rotation = 0

    async def run(self) -> None:
        """Cycle forever; one failed cycle never stops the loop.

        The first cycle waits a full interval: boot is already busy with
        backfills, and sweeping data that is still arriving would judge
        candidates on a moving target.
        """
        while True:
            await asyncio.sleep(self._interval.total_seconds())
            try:
                await self.run_cycle()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("improvement cycle failed; retrying next interval")

    async def run_cycle(self) -> int | None:
        """Run one cycle: evaluate when stale, otherwise sweep and promote.

        Returns the sweep id when a sweep ran, ``None`` otherwise. The
        cycle alternates naturally: a stale (or absent) evaluation run is
        refreshed first — its completion mines the findings — and the next
        cycle sweeps challengers targeted at those findings.
        """
        symbols = self._symbols()
        if not symbols:
            return None
        latest_run = await self._latest_completed_run()
        if latest_run is None:
            try:
                run_id = await self._evaluations.start(
                    EvaluationRunConfig(
                        symbols=symbols,
                        timeframes=(self._timeframe,),
                        history_days=self._history_days,
                        scenario_count=IMPROVEMENT_SCENARIO_COUNT,
                    )
                )
                logger.info(
                    "improvement cycle started evaluation run %d: no fresh run to learn from",
                    run_id,
                )
            except RuntimeError:
                logger.info("improvement cycle skipped: an evaluation run is already in flight")
            return None
        findings = [
            (finding_id, finding.pattern)
            for finding_id, finding in await self._store.fetch_findings(latest_run["id"])
            if finding.status != "rejected"  # a human called those noise
        ]
        symbol = symbols[self._rotation % len(symbols)]
        self._rotation += 1
        candidates, motivating = build_improvement_candidates(self._active_params(), findings)
        config = SweepConfig(
            symbol=symbol,
            timeframe=self._timeframe,
            history_days=self._history_days,
            scenario_count=IMPROVEMENT_SCENARIO_COUNT,
            candidates=candidates,
            motivating_finding_ids=motivating,
        )
        try:
            sweep_id = await self._sweeps.start(config)
        except RuntimeError:
            logger.info("improvement cycle skipped: another sweep is already in flight")
            return None
        logger.info("improvement cycle started sweep %d on %s", sweep_id, symbol)
        report = await self._wait_for_report(sweep_id)
        if report is None:
            return sweep_id
        verdict = report.get("verdict")
        if verdict != PROMOTION_VERDICT:
            logger.info(
                "improvement sweep %d kept the active configuration (verdict: %s)",
                sweep_id,
                verdict,
            )
            return sweep_id
        winner = next(
            (candidate for candidate in candidates if candidate.name == report.get("winner")),
            None,
        )
        if winner is None or winner.name.startswith("active_"):
            # "validated" with the baseline as winner cannot happen by the
            # sweep contract; refuse rather than re-promote the incumbent.
            logger.warning("improvement sweep %d validated no challenger; skipping", sweep_id)
            return sweep_id
        explanation = str(report.get("explanation", ""))
        version = await self._promote(
            winner.family, winner.params, sweep_id, f"auto-promoted: {explanation}"
        )
        message = (
            f"auto-promoted {winner.family} settings v{version} "
            f"({winner.name}) from sweep #{sweep_id}: {explanation}"
        )
        logger.info("%s", message)
        if self._notify is not None:
            await self._notify(message)
        return sweep_id

    async def _latest_completed_run(self) -> dict[str, Any] | None:
        """Return the newest completed evaluation run, unless it is stale.

        Staleness is judged in improvement intervals: findings mined from
        a run that predates recent promotions would steer the next sweep
        with observations about a configuration no longer trading.
        """
        for run in await self._store.list_runs(limit=10):
            if run.get("status") != RunStatus.COMPLETED.value:
                continue
            age = datetime.now(UTC) - run["created_at"]
            if age > self._interval * STALE_RUN_CYCLES:
                return None  # completed, but too old to describe the bot of today
            return run
        return None

    async def _wait_for_report(self, sweep_id: int) -> dict[str, Any] | None:
        """Poll until the sweep is terminal; ``None`` unless it completed."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + SWEEP_TIMEOUT.total_seconds()
        while loop.time() < deadline:
            row = await self._store.fetch_sweep(sweep_id)
            if row is not None and row.get("status") in _TERMINAL:
                if row["status"] == RunStatus.COMPLETED.value:
                    report = row.get("report")
                    return dict(report) if report is not None else None
                logger.warning(
                    "improvement sweep %d ended %s; nothing promoted",
                    sweep_id,
                    row["status"],
                )
                return None
            await asyncio.sleep(POLL_SECONDS)
        logger.warning("improvement sweep %d timed out; nothing promoted", sweep_id)
        return None
