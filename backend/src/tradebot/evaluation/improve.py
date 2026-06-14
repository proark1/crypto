"""Automated improvement: sweep the active config, promote what validates.

The loop (ARCHITECTURE.md §12.7) closes the research cycle without a human
in the middle: on a schedule it derives challenger variants from the
parameters the bot is trading *right now*, runs them through the blind
walk-forward sweep, and promotes the winner only when the verdict is
**validated** — the Bonferroni-corrected, multi-window statistical bar —
AND it survives the engine-backed confirmation gate: the evaluator's unit
trades validate, the production engine (sizing, fees, stop lifecycle,
breakers) confirms. Training wins, near-misses, and findings never promote
anything.

Scope is deliberate: promotions apply to the paper bot (the worker refuses
live mode outright), every promotion is journaled as a strategy-settings
version carrying its sweep as lineage, and a human can revert any version
through the API. Going live remains a human decision in every mode.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from tradebot.evaluation.models import LearningFinding, RunStatus
from tradebot.evaluation.runner import EvaluationRunConfig
from tradebot.evaluation.sweep import DEFAULT_SCENARIO_COUNT, SweepCandidate, SweepConfig
from tradebot.strategies import (
    BreakoutConfig,
    MeanReversionConfig,
    MomentumConfig,
    SqueezeConfig,
    TrendFollowingConfig,
)

logger = logging.getLogger(__name__)

PROMOTION_VERDICT = "validated"
"""The only sweep verdict that may change the traded configuration."""

IMPROVEMENT_TARGETS = ("production", "breakout", "momentum", "squeeze")
"""What the loop improves, in rotation. ``production`` covers the regime-
routed shape and therefore both of its families (trend following and mean
reversion); ``breakout``, ``momentum``, and ``squeeze`` are the research
families' solo competition accounts — tuning them sharpens the §13
leaderboard evidence the §13.7 routing decision will be made on. Custom
bots are absent on purpose: auto-tuning a user's recipe needs their opt-in
(a later change)."""

IMPROVEMENT_SCENARIO_COUNT = DEFAULT_SCENARIO_COUNT
"""Scenarios per candidate per period in automated research — the shared
unstarved default (see ``sweep.DEFAULT_SCENARIO_COUNT``)."""

STALE_RUN_CYCLES = 2
"""A completed evaluation older than this many of its target's turns (one
turn every ``len(IMPROVEMENT_TARGETS)`` intervals) no longer describes the
configuration now trading; the cycle re-evaluates before sweeping."""

POLL_SECONDS = 30.0
"""How often a running sweep is re-checked for a terminal status."""

SWEEP_TIMEOUT = timedelta(hours=8)
"""A sweep silent for this long is abandoned (the next cycle retries)."""

_TERMINAL = {RunStatus.COMPLETED.value, RunStatus.FAILED.value, RunStatus.INTERRUPTED.value}


@dataclass
class ImprovementStatus:
    """Live snapshot of the improvement loop for the status surface (§12.7).

    Mutable on purpose: the improver updates it in place as a cycle
    progresses and the control API reads it at request time. All times are
    UTC. ``last_outcome`` is one plain-words sentence — the same text the
    log carries — so the dashboard can show what the loop last did and why
    without the operator reading logs. A cycle is in progress when
    ``last_cycle_started_at`` is newer than ``last_cycle_finished_at``.
    """

    last_cycle_started_at: datetime | None = None
    last_cycle_finished_at: datetime | None = None
    last_outcome: str | None = None
    next_cycle_at: datetime | None = None


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


def select_targeting_findings(
    findings: Sequence[tuple[int, LearningFinding]],
) -> list[tuple[int, str]]:
    """Return the (id, pattern) pairs a sweep grid should target.

    A human acceptance is curation: once any of a run's findings is
    accepted, only accepted ones steer the targeted challengers — every
    extra candidate tightens the Bonferroni bar for all of them, so the
    curated few must not share their significance budget with patterns
    still awaiting judgement. With no verdicts yet the loop keeps its
    historical behavior (every non-rejected finding steers), and rejected
    findings never target anything — a human called those noise.
    """
    accepted = [
        (finding_id, finding.pattern)
        for finding_id, finding in findings
        if finding.status == "accepted"
    ]
    if accepted:
        return accepted
    return [
        (finding_id, finding.pattern)
        for finding_id, finding in findings
        if finding.status != "rejected"
    ]


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
    wrong_hold_ids = [
        finding_id for finding_id, pattern in findings if "ride into their stops" in pattern
    ]
    if wrong_hold_ids:
        motivating += wrong_hold_ids
        if trend.breakeven_at_r == 0:
            raw.append(
                SweepCandidate(
                    name="breakeven_lock",
                    params=trend.model_copy(update={"breakeven_at_r": 1.0}).model_dump(),
                )
            )
        else:
            raw.append(
                SweepCandidate(
                    name="no_breakeven",
                    params=trend.model_copy(update={"breakeven_at_r": 0.0}).model_dump(),
                )
            )
        trail_toggle = trend.atr_stop_multiple if trend.trail_atr_multiple == 0 else 0.0
        raw.append(
            SweepCandidate(
                name="atr_trailing" if trail_toggle else "no_trailing",
                params=trend.model_copy(update={"trail_atr_multiple": trail_toggle}).model_dump(),
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
    early_exit_ids = [finding_id for finding_id, pattern in findings if "cut winners" in pattern]
    if early_exit_ids:
        motivating += early_exit_ids
        # Two exits, two knobs: the trend family gives winners room with a
        # trailing stop; the reversion family by holding past the midline.
        trail_toggle = trend.atr_stop_multiple if trend.trail_atr_multiple == 0 else 0.0
        raw.append(
            SweepCandidate(
                name="atr_trailing" if trail_toggle else "no_trailing",
                params=trend.model_copy(update={"trail_atr_multiple": trail_toggle}).model_dump(),
            )
        )
        raw.append(
            SweepCandidate(
                name="later_reversion_exit",
                family="mean_reversion",
                params=reversion.model_copy(
                    update={"exit_rsi": min(80.0, round(reversion.exit_rsi * 1.2, 1))}
                ).model_dump(),
            )
        )
    missed_ids = [finding_id for finding_id, pattern in findings if "stays flat" in pattern]
    if missed_ids:
        motivating += missed_ids
        # Loosen exactly one entry gate: a higher oversold threshold lets
        # shallower dips qualify, clamped safely below the exit midline so
        # the entry condition can never sit above its own exit.
        looser = min(reversion.exit_rsi - 5.0, round(reversion.oversold_threshold * 1.2, 1))
        if looser > reversion.oversold_threshold:
            raw.append(
                SweepCandidate(
                    name="looser_oversold",
                    family="mean_reversion",
                    params=reversion.model_copy(update={"oversold_threshold": looser}).model_dump(),
                )
            )
    return _dedupe(raw, motivating)


def _candidate_key(candidate: SweepCandidate) -> tuple[str, str]:
    """Return a content key collapsing clamped duplicates, family or recipe."""
    if candidate.recipe is not None:
        return ("recipe", json.dumps(candidate.recipe, sort_keys=True))
    return (candidate.family, json.dumps(candidate.params, sort_keys=True))


def _dedupe(
    raw: list[SweepCandidate], motivating: list[int]
) -> tuple[tuple[SweepCandidate, ...], tuple[int, ...]]:
    """Drop candidates that clamp into copies; keep order, baseline first."""
    seen: set[tuple[str, str]] = set()
    unique: list[SweepCandidate] = []
    for candidate in raw:
        key = _candidate_key(candidate)
        if key in seen:
            continue
        seen.add(key)
        unique.append(candidate)
    return tuple(unique), tuple(motivating)


def _breakout_candidates(
    active: Mapping[str, Mapping[str, Any]],
    findings: Sequence[tuple[int, str]],
) -> tuple[tuple[SweepCandidate, ...], tuple[int, ...]]:
    """Build the breakout family's grid: channel variants plus targeted knobs.

    Single-knob steps like the production grid; clamps keep channels
    meaningful (never below 5 candles). The family-specific mapping is the
    fake-breakout one: losses labeled ``breakout_fake`` toggle the
    minimum-channel-width and volume-confirmation filters, the two knobs
    that exist precisely to skip breakouts with no width or participation
    behind them.
    """
    breakout = BreakoutConfig(**active.get("breakout", {}))
    channel, exit_channel = breakout.channel_period, breakout.exit_channel_period
    raw: list[SweepCandidate] = [
        SweepCandidate(
            name=f"active_breakout_{channel}_{exit_channel}",
            family="breakout",
            params=breakout.model_dump(),
        ),
        SweepCandidate(
            name="wider_channel",
            family="breakout",
            params=breakout.model_copy(
                update={"channel_period": round(channel * 1.5)}
            ).model_dump(),
        ),
        SweepCandidate(
            name="tighter_channel",
            family="breakout",
            params=breakout.model_copy(
                update={"channel_period": max(5, round(channel * 0.6))}
            ).model_dump(),
        ),
        SweepCandidate(
            name="wider_stop",
            family="breakout",
            params=breakout.model_copy(
                update={"atr_stop_multiple": round(breakout.atr_stop_multiple * 1.5, 2)}
            ).model_dump(),
        ),
        SweepCandidate(
            name="tighter_stop",
            family="breakout",
            params=breakout.model_copy(
                update={"atr_stop_multiple": max(0.5, round(breakout.atr_stop_multiple * 0.75, 2))}
            ).model_dump(),
        ),
    ]
    motivating: list[int] = []
    fake_ids = [finding_id for finding_id, pattern in findings if "breakout_fake" in pattern]
    if fake_ids:
        motivating += fake_ids
        width_toggle = 0.5 if breakout.min_channel_width_atr == 0 else 0.0
        raw.append(
            SweepCandidate(
                name="min_width_filter" if width_toggle else "no_min_width_filter",
                family="breakout",
                params=breakout.model_copy(
                    update={"min_channel_width_atr": width_toggle}
                ).model_dump(),
            )
        )
        volume_toggle = 1.0 if breakout.min_volume_ratio == 0 else 0.0
        raw.append(
            SweepCandidate(
                name="volume_confirm" if volume_toggle else "no_volume_confirm",
                family="breakout",
                params=breakout.model_copy(update={"min_volume_ratio": volume_toggle}).model_dump(),
            )
        )
    wrong_hold_ids = [
        finding_id for finding_id, pattern in findings if "ride into their stops" in pattern
    ]
    if wrong_hold_ids:
        motivating += wrong_hold_ids
        raw.append(
            SweepCandidate(
                name="breakeven_lock" if breakout.breakeven_at_r == 0 else "no_breakeven",
                family="breakout",
                params=breakout.model_copy(
                    update={"breakeven_at_r": 1.0 if breakout.breakeven_at_r == 0 else 0.0}
                ).model_dump(),
            )
        )
        trail_toggle = breakout.atr_stop_multiple if breakout.trail_atr_multiple == 0 else 0.0
        raw.append(
            SweepCandidate(
                name="atr_trailing" if trail_toggle else "no_trailing",
                family="breakout",
                params=breakout.model_copy(
                    update={"trail_atr_multiple": trail_toggle}
                ).model_dump(),
            )
        )
    early_exit_ids = [finding_id for finding_id, pattern in findings if "cut winners" in pattern]
    if early_exit_ids:
        motivating += early_exit_ids
        # Turtle exits leave when price crosses the exit channel; a longer
        # exit channel waits out shallow pullbacks instead of selling them.
        raw.append(
            SweepCandidate(
                name="later_channel_exit",
                family="breakout",
                params=breakout.model_copy(
                    update={"exit_channel_period": round(exit_channel * 1.5)}
                ).model_dump(),
            )
        )
    missed_ids = [finding_id for finding_id, pattern in findings if "stays flat" in pattern]
    if missed_ids:
        # The easier-entry knob is the tighter channel already in the base
        # grid; the finding rides as motivation without spending another
        # candidate's worth of significance budget.
        motivating += missed_ids
    return _dedupe(raw, motivating)


def _momentum_candidates(
    active: Mapping[str, Mapping[str, Any]],
    findings: Sequence[tuple[int, str]],
) -> tuple[tuple[SweepCandidate, ...], tuple[int, ...]]:
    """Build the momentum family's grid: MACD-speed variants plus targeted knobs.

    The zero-line filter is the family's gate: chasing or losing entries
    test turning it on (fewer, stronger signals); staying flat through
    moves tests turning it off (more entries). The same losing-entry
    patterns also toggle the volume-confirmation filter — a crossover
    nobody traded is the other false-positive shape. EMA clamps mirror
    the trend family's (fast strictly below slow).
    """
    momentum = MomentumConfig(**active.get("momentum", {}))
    fast, slow = momentum.fast_ema_period, momentum.slow_ema_period
    faster_fast = max(3, round(fast * 0.6))
    slower_fast = round(fast * 1.5)
    raw: list[SweepCandidate] = [
        SweepCandidate(
            name=f"active_momentum_{fast}_{slow}",
            family="momentum",
            params=momentum.model_dump(),
        ),
        SweepCandidate(
            name="faster_macd",
            family="momentum",
            params=momentum.model_copy(
                update={
                    "fast_ema_period": faster_fast,
                    "slow_ema_period": max(faster_fast + 2, round(slow * 0.6)),
                }
            ).model_dump(),
        ),
        SweepCandidate(
            name="slower_macd",
            family="momentum",
            params=momentum.model_copy(
                update={
                    "fast_ema_period": slower_fast,
                    "slow_ema_period": max(slower_fast + 2, round(slow * 1.5)),
                }
            ).model_dump(),
        ),
        SweepCandidate(
            name="wider_stop",
            family="momentum",
            params=momentum.model_copy(
                update={"atr_stop_multiple": round(momentum.atr_stop_multiple * 1.5, 2)}
            ).model_dump(),
        ),
        SweepCandidate(
            name="tighter_stop",
            family="momentum",
            params=momentum.model_copy(
                update={"atr_stop_multiple": max(0.5, round(momentum.atr_stop_multiple * 0.75, 2))}
            ).model_dump(),
        ),
    ]
    motivating: list[int] = []
    losing_entry_ids = [
        finding_id
        for finding_id, pattern in findings
        if "chase" in pattern or "trend is down" in pattern or "trend is ranging" in pattern
    ]
    if losing_entry_ids:
        motivating += losing_entry_ids
        if not momentum.require_positive_macd:
            raw.append(
                SweepCandidate(
                    name="zero_line_filter",
                    family="momentum",
                    params=momentum.model_copy(update={"require_positive_macd": True}).model_dump(),
                )
            )
        volume_toggle = 1.0 if momentum.min_volume_ratio == 0 else 0.0
        raw.append(
            SweepCandidate(
                name="volume_confirm" if volume_toggle else "no_volume_confirm",
                family="momentum",
                params=momentum.model_copy(update={"min_volume_ratio": volume_toggle}).model_dump(),
            )
        )
    missed_ids = [finding_id for finding_id, pattern in findings if "stays flat" in pattern]
    if missed_ids and momentum.require_positive_macd:
        motivating += missed_ids
        raw.append(
            SweepCandidate(
                name="no_zero_line_filter",
                family="momentum",
                params=momentum.model_copy(update={"require_positive_macd": False}).model_dump(),
            )
        )
    wrong_hold_ids = [
        finding_id for finding_id, pattern in findings if "ride into their stops" in pattern
    ]
    if wrong_hold_ids:
        motivating += wrong_hold_ids
        raw.append(
            SweepCandidate(
                name="breakeven_lock" if momentum.breakeven_at_r == 0 else "no_breakeven",
                family="momentum",
                params=momentum.model_copy(
                    update={"breakeven_at_r": 1.0 if momentum.breakeven_at_r == 0 else 0.0}
                ).model_dump(),
            )
        )
    early_exit_ids = [finding_id for finding_id, pattern in findings if "cut winners" in pattern]
    if early_exit_ids:
        motivating += early_exit_ids
        # A smoother signal line crosses back later, so winners get room;
        # the ATR trail is the other way to give it.
        raw.append(
            SweepCandidate(
                name="slower_signal",
                family="momentum",
                params=momentum.model_copy(
                    update={"signal_ema_period": round(momentum.signal_ema_period * 1.5)}
                ).model_dump(),
            )
        )
        trail_toggle = momentum.atr_stop_multiple if momentum.trail_atr_multiple == 0 else 0.0
        raw.append(
            SweepCandidate(
                name="atr_trailing" if trail_toggle else "no_trailing",
                family="momentum",
                params=momentum.model_copy(
                    update={"trail_atr_multiple": trail_toggle}
                ).model_dump(),
            )
        )
    return _dedupe(raw, motivating)


def _squeeze_candidates(
    active: Mapping[str, Mapping[str, Any]],
    findings: Sequence[tuple[int, str]],
) -> tuple[tuple[SweepCandidate, ...], tuple[int, ...]]:
    """Build the squeeze family's grid: band/channel variants plus targeted knobs.

    The family's defining knob is ``keltner_atr_multiple`` — the channel
    width that decides how tight a coil counts as a squeeze. A *wider*
    channel admits more (looser) squeezes and so more entries; a *tighter*
    one demands a stronger coil for fewer, higher-conviction setups. The
    finding map leans on that: losing or chasing entries tighten the
    squeeze (and add volume confirmation, the false-expansion guard), while
    staying flat through moves loosens it. Band-period steps clamp to a
    valid Bollinger window (never below 2).
    """
    squeeze = SqueezeConfig(**active.get("squeeze", {}))
    band = squeeze.bollinger_period
    raw: list[SweepCandidate] = [
        SweepCandidate(
            name=f"active_squeeze_{band}",
            family="squeeze",
            params=squeeze.model_dump(),
        ),
        SweepCandidate(
            name="looser_squeeze",
            family="squeeze",
            params=squeeze.model_copy(
                update={"keltner_atr_multiple": round(squeeze.keltner_atr_multiple * 1.5, 2)}
            ).model_dump(),
        ),
        SweepCandidate(
            name="tighter_squeeze",
            family="squeeze",
            params=squeeze.model_copy(
                update={
                    "keltner_atr_multiple": max(0.5, round(squeeze.keltner_atr_multiple * 0.75, 2))
                }
            ).model_dump(),
        ),
        SweepCandidate(
            name="wider_stop",
            family="squeeze",
            params=squeeze.model_copy(
                update={"atr_stop_multiple": round(squeeze.atr_stop_multiple * 1.5, 2)}
            ).model_dump(),
        ),
        SweepCandidate(
            name="tighter_stop",
            family="squeeze",
            params=squeeze.model_copy(
                update={"atr_stop_multiple": max(0.5, round(squeeze.atr_stop_multiple * 0.75, 2))}
            ).model_dump(),
        ),
    ]
    motivating: list[int] = []
    losing_entry_ids = [
        finding_id
        for finding_id, pattern in findings
        if "chase" in pattern
        or "trend is down" in pattern
        or "trend is ranging" in pattern
        or "breakout_fake" in pattern
    ]
    if losing_entry_ids:
        motivating += losing_entry_ids
        raw.append(
            SweepCandidate(
                name="stricter_squeeze",
                family="squeeze",
                params=squeeze.model_copy(
                    update={
                        "keltner_atr_multiple": max(
                            0.5, round(squeeze.keltner_atr_multiple * 0.6, 2)
                        )
                    }
                ).model_dump(),
            )
        )
        volume_toggle = 1.0 if squeeze.min_volume_ratio == 0 else 0.0
        raw.append(
            SweepCandidate(
                name="volume_confirm" if volume_toggle else "no_volume_confirm",
                family="squeeze",
                params=squeeze.model_copy(update={"min_volume_ratio": volume_toggle}).model_dump(),
            )
        )
    missed_ids = [finding_id for finding_id, pattern in findings if "stays flat" in pattern]
    if missed_ids:
        # The easier-entry knob is the looser channel already in the base
        # grid; the finding rides as motivation without spending another
        # candidate's worth of significance budget.
        motivating += missed_ids
    wrong_hold_ids = [
        finding_id for finding_id, pattern in findings if "ride into their stops" in pattern
    ]
    if wrong_hold_ids:
        motivating += wrong_hold_ids
        raw.append(
            SweepCandidate(
                name="breakeven_lock" if squeeze.breakeven_at_r == 0 else "no_breakeven",
                family="squeeze",
                params=squeeze.model_copy(
                    update={"breakeven_at_r": 1.0 if squeeze.breakeven_at_r == 0 else 0.0}
                ).model_dump(),
            )
        )
    early_exit_ids = [finding_id for finding_id, pattern in findings if "cut winners" in pattern]
    if early_exit_ids:
        motivating += early_exit_ids
        trail_toggle = squeeze.atr_stop_multiple if squeeze.trail_atr_multiple == 0 else 0.0
        raw.append(
            SweepCandidate(
                name="atr_trailing" if trail_toggle else "no_trailing",
                family="squeeze",
                params=squeeze.model_copy(update={"trail_atr_multiple": trail_toggle}).model_dump(),
            )
        )
    return _dedupe(raw, motivating)


def build_candidates_for(
    target: str,
    active: Mapping[str, Mapping[str, Any]],
    findings: Sequence[tuple[int, str]] = (),
) -> tuple[tuple[SweepCandidate, ...], tuple[int, ...]]:
    """Derive the challenger grid for one improvement target.

    ``production`` — and the two families it routes, when their solo
    accounts are evaluated directly — uses the mixed production grid, so
    the families are tuned as one budget exactly as they trade. The
    research families get their own grids. ``ValueError`` for anything
    else — a custom bot is a recipe, graded by ``build_recipe_candidates``
    instead (callers route by whether the target has a recipe), so reaching
    here with a custom-bot id is a wrong call, not a silent wrong sweep.
    """
    if target in ("production", "trend_following", "mean_reversion"):
        return build_improvement_candidates(active, findings)
    if target == "breakout":
        return _breakout_candidates(active, findings)
    if target == "momentum":
        return _momentum_candidates(active, findings)
    if target == "squeeze":
        return _squeeze_candidates(active, findings)
    raise ValueError(f"no improvement grid for {target!r}; known: {IMPROVEMENT_TARGETS}")


def _single_family_grid(
    family: str,
    params: Mapping[str, Any],
    findings: Sequence[tuple[int, str]],
) -> tuple[tuple[SweepCandidate, ...], tuple[int, ...]]:
    """One family's single-knob candidates (its baseline first, then variants).

    The research families already build single-family grids. The production
    builder mixes trend following and mean reversion, so for those two we
    keep only the requested family's candidates — the recipe lifts each
    into recipe space below.
    """
    active = {family: dict(params)}
    if family == "breakout":
        return _breakout_candidates(active, findings)
    if family == "momentum":
        return _momentum_candidates(active, findings)
    if family == "squeeze":
        return _squeeze_candidates(active, findings)
    candidates, motivating = build_improvement_candidates(active, findings)
    return tuple(c for c in candidates if c.family == family), motivating


def build_recipe_candidates(
    recipe: Mapping[str, Any],
    findings: Sequence[tuple[int, str]] = (),
) -> tuple[tuple[SweepCandidate, ...], tuple[int, ...]]:
    """Derive a custom bot's challenger grid: vary one knob at a time, in place.

    A custom bot trades a *recipe* — one or more families combined by its
    entry mode — so a challenger must be graded as that whole composite,
    not a family in isolation. Each variant changes one knob of one family
    and leaves the rest of the recipe untouched, reusing every family's own
    single-knob grid and its finding→knob mappings (lifted into recipe
    space). ``candidates[0]`` is the active recipe, the baseline the sweep
    contract requires.

    Motivating ids are the union across the recipe's families. For a recipe
    that omits one of the routed families, a finding mapped only to the
    absent family's knob can still appear here though it added no candidate
    — a harmless lineage superset, never a missing one.
    """
    baseline = SweepCandidate(name="active_recipe", recipe=dict(recipe))
    raw: list[SweepCandidate] = [baseline]
    motivating: list[int] = []
    for family, params in recipe["families"].items():
        family_candidates, family_motivating = _single_family_grid(family, params, findings)
        motivating += family_motivating
        for candidate in family_candidates:
            if candidate.name.startswith("active"):
                continue  # the family's own baseline == the recipe's current params
            variant = {**recipe, "families": {**recipe["families"], family: candidate.params}}
            raw.append(SweepCandidate(name=f"{family}:{candidate.name}", recipe=variant))
    return _dedupe(raw, motivating)


class AutoImprover:
    """Runs improvement cycles forever; one (target, symbol) pair per cycle.

    Targets rotate first (production, then each research family), symbols
    second, so every family is revisited before any symbol repeats.
    """

    def __init__(
        self,
        *,
        sweeps: SweepStarter,
        evaluations: EvaluationStarter,
        store: ResearchReader,
        active_params: Callable[[], Mapping[str, Mapping[str, Any]]],
        symbols: Callable[[], tuple[str, ...]],
        promote: Callable[[str, Mapping[str, Any], int | None, str | None], Awaitable[int]],
        confirm: Callable[[str, Mapping[str, Any], str], Awaitable[str | None]] | None = None,
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
        ``confirm`` is the engine-backed gate: given (family, params,
        symbol) it returns a veto reason, or ``None`` to allow — promotion
        is skipped entirely when it vetoes.
        """
        self._sweeps = sweeps
        self._evaluations = evaluations
        self._store = store
        self._active_params = active_params
        self._symbols = symbols
        self._promote = promote
        self._confirm = confirm
        self._interval = interval
        self._history_days = history_days
        self._timeframe = timeframe
        self._notify = notify
        self._rotation = 0
        self.status = ImprovementStatus()

    async def run(self) -> None:
        """Cycle forever; one failed cycle never stops the loop.

        The first cycle waits a full interval: boot is already busy with
        backfills, and sweeping data that is still arriving would judge
        candidates on a moving target.
        """
        while True:
            self.status.next_cycle_at = datetime.now(UTC) + self._interval
            await asyncio.sleep(self._interval.total_seconds())
            try:
                await self.run_cycle()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("improvement cycle failed; retrying next interval")
                self._finish_cycle("cycle failed; retrying next interval (see logs)")

    def _finish_cycle(self, outcome: str) -> None:
        """Record a cycle's plain-words outcome for the status surface."""
        self.status.last_outcome = outcome
        self.status.last_cycle_finished_at = datetime.now(UTC)

    async def run_cycle(self) -> int | None:
        """Run one cycle: evaluate the target when stale, else sweep it.

        Returns the sweep id when a sweep ran, ``None`` otherwise. Each
        cycle serves one improvement target (production, then each
        research family) on one symbol, rotating target-first so every
        family is revisited before any symbol repeats. A target with no
        fresh evaluation run is refreshed first — its completion mines the
        findings — and the target's next turn sweeps challengers aimed at
        them.
        """
        self.status.last_cycle_started_at = datetime.now(UTC)
        symbols = self._symbols()
        if not symbols:
            self._finish_cycle("skipped: no active coins to research")
            return None
        target = IMPROVEMENT_TARGETS[self._rotation % len(IMPROVEMENT_TARGETS)]
        symbol = symbols[(self._rotation // len(IMPROVEMENT_TARGETS)) % len(symbols)]
        self._rotation += 1
        latest_run = await self._latest_completed_run(target)
        if latest_run is None:
            try:
                run_id = await self._evaluations.start(
                    EvaluationRunConfig(
                        symbols=symbols,
                        timeframes=(self._timeframe,),
                        history_days=self._history_days,
                        scenario_count=IMPROVEMENT_SCENARIO_COUNT,
                        strategy=target,
                    )
                )
                logger.info(
                    "improvement cycle started evaluation run %d: no fresh %s run to learn from",
                    run_id,
                    target,
                )
                self._finish_cycle(
                    f"started evaluation run #{run_id}: no fresh {target} run to learn "
                    f"from; {target}'s next turn sweeps its findings"
                )
            except RuntimeError:
                logger.info("improvement cycle skipped: an evaluation run is already in flight")
                self._finish_cycle("skipped: an evaluation run is already in flight")
            return None
        findings = select_targeting_findings(await self._store.fetch_findings(latest_run["id"]))
        candidates, motivating = build_candidates_for(target, self._active_params(), findings)
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
            self._finish_cycle("skipped: another sweep is already in flight")
            return None
        logger.info("improvement cycle started sweep %d (%s) on %s", sweep_id, target, symbol)
        # Interim state, not a finished outcome: a sweep can run for hours,
        # and the status surface should say so rather than look idle.
        self.status.last_outcome = (
            f"sweep #{sweep_id} ({target}) running on {symbol} ({len(candidates)} candidates)"
        )
        report = await self._wait_for_report(sweep_id)
        if report is None:
            self._finish_cycle(f"sweep #{sweep_id} ended without a verdict; nothing promoted")
            return sweep_id
        verdict = report.get("verdict")
        if verdict != PROMOTION_VERDICT:
            logger.info(
                "improvement sweep %d kept the active configuration (verdict: %s)",
                sweep_id,
                verdict,
            )
            self._finish_cycle(
                f"sweep #{sweep_id} kept the active configuration (verdict: {verdict})"
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
            self._finish_cycle(f"sweep #{sweep_id} validated no challenger; nothing promoted")
            return sweep_id
        if winner.recipe is not None:
            # Recipe winners describe a whole custom-bot composite, not a
            # single family/params pair — ARCHITECTURE.md keeps them out of
            # auto-promotion until owner opt-in. Auto-promoting one here would
            # silently push ``winner.family``'s default with empty params (the
            # wrong bot). Refuse loudly rather than promote a stand-in.
            logger.warning(
                "improvement sweep %d validated a recipe winner (%s); recipes are not "
                "auto-promoted, skipping",
                sweep_id,
                winner.name,
            )
            self._finish_cycle(
                f"sweep #{sweep_id} validated recipe {winner.name}; recipes are not auto-promoted"
            )
            return sweep_id
        explanation = str(report.get("explanation", ""))
        if self._confirm is not None:
            veto_reason = await self._confirm(winner.family, winner.params, symbol)
            if veto_reason is not None:
                message = (
                    f"sweep #{sweep_id} validated {winner.name}, but the engine-backed "
                    f"confirmation vetoed the promotion: {veto_reason}"
                )
                logger.warning("%s", message)
                self._finish_cycle(message)
                if self._notify is not None:
                    await self._notify(message)
                return sweep_id
        version = await self._promote(
            winner.family, winner.params, sweep_id, f"auto-promoted: {explanation}"
        )
        message = (
            f"auto-promoted {winner.family} settings v{version} "
            f"({winner.name}) from sweep #{sweep_id}: {explanation}"
        )
        logger.info("%s", message)
        self._finish_cycle(message)
        if self._notify is not None:
            await self._notify(message)
        return sweep_id

    async def _latest_completed_run(self, target: str) -> dict[str, Any] | None:
        """Return the target's newest completed run, unless it is stale.

        Staleness is judged in the target's own turns (one turn every
        ``len(IMPROVEMENT_TARGETS)`` intervals): findings mined from a run
        that predates recent promotions would steer the next sweep with
        observations about a configuration no longer trading.
        """
        stale_after = self._interval * STALE_RUN_CYCLES * len(IMPROVEMENT_TARGETS)
        for run in await self._store.list_runs(limit=50):
            if run.get("status") != RunStatus.COMPLETED.value:
                continue
            if run.get("strategy") != target:
                continue
            age = datetime.now(UTC) - run["created_at"]
            if age > stale_after:
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
