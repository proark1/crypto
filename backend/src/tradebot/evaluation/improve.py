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
from collections.abc import Awaitable, Callable, Mapping
from datetime import timedelta
from typing import Any, Protocol

from tradebot.evaluation.models import RunStatus
from tradebot.evaluation.sweep import SweepCandidate, SweepConfig
from tradebot.strategies import MeanReversionConfig, TrendFollowingConfig

logger = logging.getLogger(__name__)

PROMOTION_VERDICT = "validated"
"""The only sweep verdict that may change the traded configuration."""

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


class SweepReader(Protocol):
    """The slice of ``EvaluationStore`` the improver depends on."""

    async def fetch_sweep(self, sweep_id: int) -> dict[str, Any] | None:
        """Return the sweep row (status + report), or ``None`` if unknown."""
        ...


def build_improvement_candidates(
    active: Mapping[str, Mapping[str, Any]],
) -> tuple[SweepCandidate, ...]:
    """Derive one challenger grid from the currently traded parameters.

    The active trend configuration is the baseline (``candidates[0]``, as
    the sweep contract requires); each variant changes a single knob by a
    multiplicative step so the journal can name what earned a promotion.
    Steps are clamped to valid configurations (fast EMA strictly below
    slow, stops never collapsing to zero) and variants that clamp into a
    copy of an existing candidate are dropped — sweeping a candidate
    against itself would only spend the significance budget.
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
    seen: set[tuple[str, tuple[tuple[str, Any], ...]]] = set()
    unique: list[SweepCandidate] = []
    for candidate in raw:
        key = (candidate.family, tuple(sorted(candidate.params.items())))
        if key in seen:
            continue
        seen.add(key)
        unique.append(candidate)
    return tuple(unique)


class AutoImprover:
    """Runs improvement cycles forever; one rotating symbol per cycle."""

    def __init__(
        self,
        *,
        sweeps: SweepStarter,
        store: SweepReader,
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
        """Run one sweep-and-maybe-promote cycle; returns the sweep id."""
        symbols = self._symbols()
        if not symbols:
            return None
        symbol = symbols[self._rotation % len(symbols)]
        self._rotation += 1
        candidates = build_improvement_candidates(self._active_params())
        config = SweepConfig(
            symbol=symbol,
            timeframe=self._timeframe,
            history_days=self._history_days,
            candidates=candidates,
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
