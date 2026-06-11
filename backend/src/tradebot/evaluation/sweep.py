"""Parameter sweeps with walk-forward validation (ARCHITECTURE.md §12.5).

Candidate configurations — across strategy families — are scored on a
*training* span of history; only the training winner (and the baseline it
challenges) is then scored on the later, untouched *validation* slices,
walked forward one window at a time. A candidate that wins training but
loses validation is reported as **overfit**, in those words — and a winner
whose validation edge is not statistically distinguishable from luck,
after correcting for how many candidates had a shot at winning, is
reported as unproven rather than crowned. Like findings, a sweep only
ever recommends: changing the live configuration stays a human action,
outside this module.

Scenarios are generated, decided, and graded by the same blind pipeline as
evaluation runs (one code path), so sweep numbers and run numbers are
directly comparable.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Coroutine, Mapping, Sequence
from datetime import timedelta
from decimal import ROUND_HALF_EVEN, Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from tradebot.backtest import split_rolling_by_fraction
from tradebot.core.models import ACCOUNTING_RESOLUTION, Candle, CandleInterval, utc_now
from tradebot.evaluation.engine import ScenarioEvaluator
from tradebot.evaluation.generator import GeneratorConfig, generate_specs
from tradebot.evaluation.models import RunStatus
from tradebot.evaluation.reports import r_metrics
from tradebot.evaluation.statistics import (
    bootstrap_expectancy_interval,
    corrected_significance,
    superiority_p_value,
)
from tradebot.execution import FillSimulatorConfig
from tradebot.marketdata import aggregate_candles
from tradebot.persistence import CandleStore, EvaluationStore
from tradebot.strategies import (
    MeanReversionConfig,
    MeanReversionStrategy,
    Strategy,
    TrendFollowingConfig,
    TrendFollowingStrategy,
)

logger = logging.getLogger(__name__)

MIN_SWEEP_TRADES = 10
"""Candidates with fewer graded trades than this cannot be compared
honestly; expectancy over a handful of trades is noise."""

DEFAULT_SCENARIO_COUNT = 400
"""Default scenarios per candidate per period, manual and automated alike.
The strategies trade in only a few percent of scenarios, so the old
default of 100 yielded 3-4 trades per candidate — every sweep ended
"insufficient evidence" by construction."""

STRATEGY_FAMILIES: Mapping[str, tuple[type[BaseModel], Callable[..., Strategy]]] = {
    "trend_following": (TrendFollowingConfig, TrendFollowingStrategy),
    "mean_reversion": (MeanReversionConfig, MeanReversionStrategy),
}
"""Sweepable families: name -> (config model, strategy constructor). A
candidate names its family, so one sweep can pit families against each
other on identical scenarios."""


def validate_family_params(family: str, params: Mapping[str, Any]) -> None:
    """Raise ``ValueError`` for an unknown family or parameter, loudly.

    Pydantic ignores unknown keys by default; a typo'd parameter would
    silently sweep the baseline against itself.
    """
    if family not in STRATEGY_FAMILIES:
        raise ValueError(f"unknown strategy family {family!r}; known: {sorted(STRATEGY_FAMILIES)}")
    config_model, _ = STRATEGY_FAMILIES[family]
    unknown = set(params) - set(config_model.model_fields)
    if unknown:
        raise ValueError(f"unknown {family} parameters: {sorted(unknown)}")


class SweepCandidate(BaseModel):
    """One named parameter set (of one strategy family) competing in the sweep."""

    model_config = ConfigDict(frozen=True)

    name: str
    params: dict[str, Any]
    family: str = "trend_following"

    @model_validator(mode="after")
    def _family_and_params_must_exist(self) -> SweepCandidate:
        validate_family_params(self.family, self.params)
        return self


def build_candidate_strategy(candidate: SweepCandidate) -> Strategy:
    """Build one fresh strategy instance for ``candidate``."""
    validate_family_params(candidate.family, candidate.params)
    config_model, strategy_constructor = STRATEGY_FAMILIES[candidate.family]
    return strategy_constructor(config_model(**candidate.params))


class SweepConfig(BaseModel):
    """One sweep's shape; snapshotted verbatim into the sweep row.

    ``candidates[0]`` is the baseline — the configuration the bot trades
    today — and every verdict is phrased as a challenge to it.
    """

    model_config = ConfigDict(frozen=True)

    symbol: str
    timeframe: str = "1h"
    history_days: int = Field(default=180, gt=0)
    scenario_count: int = Field(default=DEFAULT_SCENARIO_COUNT, gt=0)
    """Scenarios per candidate per window side (training and validation)."""

    lookback_candles: int = Field(default=200, ge=60)
    horizon_candles: int = Field(default=60, gt=0)
    seed: int = 7
    training_fraction: float = Field(default=0.7, gt=0.0, lt=1.0)
    """Share of the series each rolling window trains on; the rest splits
    into ``validation_windows`` chronological validation slices."""

    validation_windows: int = Field(default=3, ge=1)
    """How many chronological out-of-sample slices the held-out data is
    split into. Candidates are selected once, on the training span that
    strictly precedes *every* slice (re-selecting per window would train
    on earlier windows' validation data); each slice then tests the same
    challenger, so the report shows whether the edge persists through
    successive periods or lived in one lucky stretch. One window
    reproduces the old single chronological split."""

    candidates: tuple[SweepCandidate, ...] = Field(min_length=2)
    motivating_finding_ids: tuple[int, ...] = ()
    """Accepted findings that motivated this sweep — the lineage §12.5
    requires: what changed, why, and whether validation confirmed it."""

    @model_validator(mode="after")
    def _names_must_be_unique(self) -> SweepConfig:
        names = [candidate.name for candidate in self.candidates]
        if len(set(names)) != len(names):
            raise ValueError(f"candidate names must be unique, got {names}")
        return self

    def interval(self) -> CandleInterval:
        """Parse the timeframe; raises ``ValueError`` on unknown ones."""
        return CandleInterval(self.timeframe)


DEFAULT_SWEEP_CANDIDATES: tuple[SweepCandidate, ...] = (
    SweepCandidate(name="baseline_20_50", params=TrendFollowingConfig().model_dump()),
    SweepCandidate(
        name="faster_cross_10_30",
        params=TrendFollowingConfig(fast_ema_period=10, slow_ema_period=30).model_dump(),
    ),
    SweepCandidate(
        name="slower_cross_30_90",
        params=TrendFollowingConfig(fast_ema_period=30, slow_ema_period=90).model_dump(),
    ),
    SweepCandidate(
        name="wider_stop_3x",
        params=TrendFollowingConfig(atr_stop_multiple=3.0).model_dump(),
    ),
    SweepCandidate(
        name="tighter_stop_1.5x",
        params=TrendFollowingConfig(atr_stop_multiple=1.5).model_dump(),
    ),
    SweepCandidate(
        name="reversion_rsi_14",
        family="mean_reversion",
        params=MeanReversionConfig().model_dump(),
    ),
)
"""The default grid: the live trend defaults as the baseline, one
deliberate change per trend candidate so a verdict names the single knob
that earned (or lost) it — plus the mean-reversion family's defaults, so
every default sweep also asks whether the other family beats the
incumbent on the same scenarios."""


class CandidateScore(BaseModel):
    """One candidate's graded outcomes, pooled across the scored windows."""

    model_config = ConfigDict(frozen=True)

    candidate: SweepCandidate
    scenario_count: int
    r_values: tuple[Decimal, ...]

    @property
    def trade_count(self) -> int:
        """How many scenarios actually produced a graded trade."""
        return len(self.r_values)

    @property
    def expectancy_r(self) -> Decimal | None:
        """Mean R per trade, or ``None`` when nothing traded."""
        if not self.r_values:
            return None
        return (sum(self.r_values, Decimal(0)) / Decimal(len(self.r_values))).quantize(
            ACCOUNTING_RESOLUTION, rounding=ROUND_HALF_EVEN
        )

    def merged_with(self, other: CandidateScore) -> CandidateScore:
        """Pool another window's outcomes for the same candidate."""
        if other.candidate != self.candidate:
            raise ValueError("cannot merge scores of different candidates")
        return CandidateScore(
            candidate=self.candidate,
            scenario_count=self.scenario_count + other.scenario_count,
            r_values=self.r_values + other.r_values,
        )


def select_winner(scores: Sequence[CandidateScore]) -> CandidateScore | None:
    """Return the best-by-expectancy candidate with enough trades, else ``None``.

    Ties keep the earlier candidate — with the baseline first, a variant
    must strictly beat it to challenge.
    """
    eligible = [score for score in scores if score.trade_count >= MIN_SWEEP_TRADES]
    winner: CandidateScore | None = None
    for score in eligible:
        expectancy = score.expectancy_r
        if expectancy is None:
            continue
        best = winner.expectancy_r if winner is not None else None
        if best is None or expectancy > best:
            winner = score
    return winner


class SweepRunner:
    """Executes one sweep end to end against the stores."""

    def __init__(
        self,
        candle_store: CandleStore,
        evaluation_store: EvaluationStore,
        strategy_builder: Callable[[SweepCandidate], Strategy],
        fills: FillSimulatorConfig | None = None,
    ) -> None:
        """Bind the data sources and the candidate -> strategy builder."""
        self._candles = candle_store
        self._store = evaluation_store
        self._build = strategy_builder
        self._fills = fills or FillSimulatorConfig()

    async def execute(self, sweep_id: int, config: SweepConfig) -> None:
        """Drive ``sweep_id`` to a terminal status; never raises except on cancel."""
        try:
            await self._store.set_sweep_status(sweep_id, RunStatus.RUNNING)
            report = await self._run(config)
            await self._store.complete_sweep(sweep_id, report)
            logger.info("sweep %d completed: %s", sweep_id, report["verdict"])
        except asyncio.CancelledError:
            await self._store.set_sweep_status(sweep_id, RunStatus.INTERRUPTED)
            logger.warning("sweep %d interrupted", sweep_id)
            raise
        except Exception:
            logger.exception("sweep %d failed", sweep_id)
            await self._store.set_sweep_status(sweep_id, RunStatus.FAILED)

    async def _run(self, config: SweepConfig) -> dict[str, Any]:
        interval = config.interval()
        now = utc_now()
        start = now - timedelta(days=config.history_days)
        base = await self._candles.fetch_range(config.symbol, CandleInterval.M1, start, now)
        series = base if interval == CandleInterval.M1 else aggregate_candles(base, interval)
        windows = split_rolling_by_fraction(
            series, config.training_fraction, config.validation_windows
        )
        # Selection happens once, on the span strictly before every
        # validation slice (the first window's training side). The later
        # windows' training spans contain earlier validation slices, so
        # re-selecting on them would leak validation data into training.
        training = list(windows[0].train)
        training_scores = [
            await self._score(training, candidate, config) for candidate in config.candidates
        ]
        baseline = training_scores[0]
        winner = select_winner(training_scores)

        report: dict[str, Any] = {
            "baseline": baseline.candidate.name,
            "split": {
                "training_candles": len(windows[0].train),
                "validation_candles": sum(len(window.validation) for window in windows),
                "training_fraction": config.training_fraction,
                "validation_windows": [len(window.validation) for window in windows],
            },
            "training": {
                score.candidate.name: _score_block(score, config.seed) for score in training_scores
            },
            "validation": {},
        }
        if winner is None:
            report["winner"] = None
            report["verdict"] = "insufficient_evidence"
            report["explanation"] = (
                f"no candidate produced at least {MIN_SWEEP_TRADES} trades on the training "
                "windows; there is nothing to compare honestly"
            )
            return report

        report["winner"] = winner.candidate.name
        if winner.candidate.name == baseline.candidate.name:
            report["verdict"] = "baseline_best"
            report["explanation"] = (
                f"no variant beat the baseline {baseline.candidate.name} on the training "
                "windows; nothing to validate, keep the current configuration"
            )
            return report

        # Only the challenger and the baseline earn a look at the untouched
        # validation slices — scoring every variant there would quietly turn
        # validation into a second training set.
        validation_slices = [list(window.validation) for window in windows]
        baseline_by_window = [
            await self._score(window_slice, baseline.candidate, config)
            for window_slice in validation_slices
        ]
        winner_by_window = [
            await self._score(window_slice, winner.candidate, config)
            for window_slice in validation_slices
        ]
        baseline_validation = _pooled(baseline_by_window)
        winner_validation = _pooled(winner_by_window)
        report["validation"] = {
            baseline_validation.candidate.name: _score_block(baseline_validation, config.seed),
            winner_validation.candidate.name: _score_block(winner_validation, config.seed),
        }
        # Per-window expectancies show whether the edge persisted through
        # successive periods or lived in one lucky stretch; the verdict
        # stands on the pooled numbers below.
        report["validation_by_window"] = [
            {"baseline": _window_block(base), "winner": _window_block(win)}
            for base, win in zip(baseline_by_window, winner_by_window, strict=True)
        ]
        # Every non-baseline candidate had a shot at winning training, so
        # every one of them spends part of the significance budget.
        comparisons = len(config.candidates) - 1
        report["verdict"], report["explanation"], report["significance"] = validation_verdict(
            baseline_validation, winner_validation, comparisons=comparisons, seed=config.seed
        )
        return report

    async def _score(
        self, series: list[Candle], candidate: SweepCandidate, config: SweepConfig
    ) -> CandidateScore:
        """Run the blind pipeline for one candidate over one period."""
        evaluator = ScenarioEvaluator(lambda: self._build(candidate), self._fills)
        specs = generate_specs(
            series,
            GeneratorConfig(
                scenario_count=config.scenario_count,
                lookback_candles=config.lookback_candles,
                horizon_candles=config.horizon_candles,
                seed=config.seed,
            ),
        )
        r_values: list[Decimal] = []
        for spec, _ in specs:
            outcome = evaluator.evaluate(series, spec)
            if outcome.r_multiple is not None:
                r_values.append(outcome.r_multiple)
            # Yield: the live candle loop must never wait on a sweep.
            await asyncio.sleep(0)
        return CandidateScore(
            candidate=candidate, scenario_count=len(specs), r_values=tuple(r_values)
        )


def _pooled(scores: Sequence[CandidateScore]) -> CandidateScore:
    """Merge one candidate's per-window scores into its pooled score."""
    pooled = scores[0]
    for score in scores[1:]:
        pooled = pooled.merged_with(score)
    return pooled


def _window_block(score: CandidateScore) -> dict[str, Any]:
    """One candidate's quality on one validation window, for the report."""
    expectancy = score.expectancy_r
    return {
        "trade_count": score.trade_count,
        "expectancy_r": str(expectancy) if expectancy is not None else None,
    }


def _score_block(score: CandidateScore, seed: int) -> dict[str, Any]:
    """Serialize one candidate's quality for the report (strings, §12.3 format)."""
    interval = bootstrap_expectancy_interval(score.r_values, seed)
    return {
        "params": score.candidate.params,
        "family": score.candidate.family,
        "scenario_count": score.scenario_count,
        **r_metrics(list(score.r_values)),
        "expectancy_ci_r": (
            {"low": str(interval.low_r), "high": str(interval.high_r)}
            if interval is not None
            else None
        ),
    }


def validation_verdict(
    baseline: CandidateScore, winner: CandidateScore, comparisons: int, seed: int
) -> tuple[str, str, dict[str, Any]]:
    """Phrase the walk-forward outcome in plain words (§12.5).

    Returns (verdict, explanation, significance block). "Validated" now
    means *statistically* validated: the challenger's edge over the
    baseline on the untouched slices must clear a one-sided bootstrap test
    at the Bonferroni-corrected level — a grid of N variants does not get
    N chances at a 5% fluke.
    """
    threshold = corrected_significance(comparisons)
    significance: dict[str, Any] = {
        "comparisons": comparisons,
        "corrected_threshold": str(threshold),
        "p_value": None,
    }
    winner_r = winner.expectancy_r
    baseline_r = baseline.expectancy_r
    if winner.trade_count < MIN_SWEEP_TRADES or winner_r is None:
        return (
            "insufficient_evidence",
            f"{winner.candidate.name} won training but traded only {winner.trade_count} "
            "times on the validation windows; not enough evidence to recommend it",
            significance,
        )
    if baseline_r is not None and winner_r <= baseline_r:
        return (
            "overfit",
            f"{winner.candidate.name} won the training windows but not the untouched "
            f"validation windows ({winner_r}R vs {baseline_r}R per trade); it wins only on "
            f"the data it was tuned on — keep {baseline.candidate.name}",
            significance,
        )
    p_value = superiority_p_value(winner.r_values, baseline.r_values, seed)
    if p_value is None:
        # The baseline barely traded on validation: there is nothing to
        # beat, but also nothing to test against — never call that proven.
        return (
            "insufficient_evidence",
            f"{winner.candidate.name} beat {baseline.candidate.name} on the validation "
            f"windows ({winner_r}R per trade), but the baseline produced only "
            f"{baseline.trade_count} trades there — too few to test the edge against",
            significance,
        )
    significance["p_value"] = str(p_value)
    if p_value > threshold:
        return (
            "insufficient_evidence",
            f"{winner.candidate.name} beat {baseline.candidate.name} on the validation "
            f"windows ({winner_r}R vs {baseline_r}R per trade), but the edge is not "
            f"distinguishable from luck after correcting for {comparisons} comparisons "
            f"(p={p_value}, needs <= {threshold})",
            significance,
        )
    return (
        "validated",
        f"{winner.candidate.name} beat {baseline.candidate.name} on the untouched "
        f"validation windows ({winner_r}R vs {baseline_r}R per trade, p={p_value} at "
        f"the {threshold} corrected level); the improvement survived walk-forward",
        significance,
    )


class SweepManager:
    """Owns the single in-flight sweep and its background task."""

    def __init__(
        self,
        runner: SweepRunner,
        store: EvaluationStore,
        spawn: Callable[[Coroutine[Any, Any, None]], asyncio.Task[None]],
    ) -> None:
        """``spawn`` ties the sweep's task to the worker's TaskGroup lifetime."""
        self._runner = runner
        self._store = store
        self._spawn = spawn
        self._task: asyncio.Task[None] | None = None
        self._current_sweep_id: int | None = None

    async def start(self, config: SweepConfig) -> int:
        """Create the sweep row and launch it; one sweep at a time, on purpose.

        Raises ``RuntimeError`` if a sweep is in flight (sweeps share the
        worker's CPU with live trading) and ``ValueError`` for bad config.
        """
        config.interval()  # validate the timeframe before any row exists
        if self._task is not None and not self._task.done():
            raise RuntimeError(f"sweep {self._current_sweep_id} is already in progress")
        sweep_id = await self._store.create_sweep(
            symbol=config.symbol,
            timeframe=config.timeframe,
            config=config.model_dump(),
            motivating_finding_ids=list(config.motivating_finding_ids),
            created_at=utc_now(),
        )
        self._current_sweep_id = sweep_id
        self._task = self._spawn(self._runner.execute(sweep_id, config))
        logger.info("sweep %d started", sweep_id)
        return sweep_id

    def cancel(self, sweep_id: int) -> bool:
        """Cancel the in-flight sweep; returns whether anything was cancelled."""
        if self._current_sweep_id != sweep_id or self._task is None or self._task.done():
            return False
        self._task.cancel()
        # Same reconciliation as evaluation runs: a task cancelled before it
        # ever ran would leave the sweep "pending" forever.
        self._spawn(self._mark_interrupted_if_not_terminal(sweep_id))
        return True

    async def _mark_interrupted_if_not_terminal(self, sweep_id: int) -> None:
        sweep = await self._store.fetch_sweep(sweep_id)
        if sweep is not None and sweep["status"] in (
            RunStatus.PENDING.value,
            RunStatus.RUNNING.value,
        ):
            await self._store.set_sweep_status(sweep_id, RunStatus.INTERRUPTED)
