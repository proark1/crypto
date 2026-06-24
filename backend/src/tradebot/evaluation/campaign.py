"""Iterated walk-forward research campaigns (ARCHITECTURE.md §12.7).

The §12.7 auto-improver runs **one** sweep per scheduled turn. A *campaign*
closes the missing outer loop: it runs sweeps back to back, promoting every
challenger that clears the walk-forward bar and climbing from it, until a
fixed budget — rounds or wall-clock — is spent. A round that finds no
validated improvement *refines* (a smaller step around the same incumbent,
coarse to fine) rather than re-running the identical sweep, so the budget
buys new information instead of re-rolling one comparison.

The anti-overfit guard is structural, not optional. Every round is graded
strictly *before* a reserved holdout (``SweepConfig.window_end``), so an
iterated search over backtests cannot quietly turn the validation windows
into a second training set across rounds; the untouched holdout grades the
campaign's net move once, at the end (a non-gating honesty read). And every
promotion still clears the same Bonferroni-corrected, walk-forward bar plus
the engine-backed confirmation the auto-improver uses — the loop cannot
promote a configuration that only looks good on the data it was tuned on.

This module is pure orchestration: every effect (starting a sweep, reading
its verdict, promoting, confirming, grading the holdout, telling the time)
arrives as an injected callable, so a campaign grades and promotes through
exactly the same code paths the rest of §12 uses, and is unit-tested
without a worker, a database, or a network. Promotions remain paper-only
and reversible: the injected ``promote`` is the worker's journaled apply
path, which refuses any non-paper mode, and a human can revert any version.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Collection, Coroutine, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field

from tradebot.core.models import CandleInterval, utc_now
from tradebot.evaluation.models import RunStatus
from tradebot.evaluation.promotion_policy import promotion_timeframe_allowed
from tradebot.evaluation.settings_diff import SettingChange, settings_changes
from tradebot.evaluation.sweep import DEFAULT_SCENARIO_COUNT, SweepCandidate, SweepConfig

logger = logging.getLogger(__name__)

PROMOTION_VERDICT = "validated"
"""The only sweep verdict that may change the traded configuration — the
same bar the §12.7 auto-improver promotes on."""

POLL_SECONDS = 30.0
"""How often a running sweep is re-checked for a terminal status."""

SWEEP_TIMEOUT = timedelta(hours=8)
"""A round whose sweep is silent this long is abandoned; the campaign
records the dead round and refines like any other non-promotion."""

_TERMINAL = {RunStatus.COMPLETED.value, RunStatus.FAILED.value, RunStatus.INTERRUPTED.value}


class SweepStarter(Protocol):
    """The slice of ``SweepManager`` a campaign starts rounds through."""

    async def start(self, config: SweepConfig) -> int:
        """Create and launch a sweep; raise ``RuntimeError`` if one runs."""
        ...


class SweepReader(Protocol):
    """The slice of ``EvaluationStore`` a campaign polls for verdicts."""

    async def fetch_sweep(self, sweep_id: int) -> dict[str, Any] | None:
        """Return the sweep row (status + report), or ``None`` if unknown."""
        ...


class CandidateProvider(Protocol):
    """Derives one round's challenger grid from the live incumbent.

    Bound to a single improvement target; given the parameters trading
    *now* and a step ``scale`` (1.0 = the coarsest grid, shrinking toward
    the incumbent as the search refines), it returns ``(candidates,
    motivating_finding_ids)`` with the baseline first — the same contract
    ``evaluation.improve.build_candidates_for`` already satisfies.
    """

    async def __call__(
        self, active_params: Mapping[str, Mapping[str, Any]], scale: float
    ) -> tuple[Sequence[SweepCandidate], Sequence[int]]:
        """Return ``(candidates, motivating_finding_ids)``, baseline first."""
        ...


class HoldoutGrader(Protocol):
    """Grades the campaign's net move on the untouched holdout slice.

    Given the configuration the campaign started from and the one it ended
    on, both graded on candles at or after ``holdout_start`` (the slice no
    round ever swept), it returns a plain-words read or ``None`` when it
    cannot judge. The read is non-gating — every step was already
    walk-forward validated — so it never vetoes; it informs.
    """

    async def __call__(
        self,
        start_params: Mapping[str, Mapping[str, Any]],
        final_params: Mapping[str, Mapping[str, Any]],
        holdout_start: datetime,
    ) -> dict[str, Any] | None:
        """Return the holdout honesty read, or ``None`` when unavailable."""
        ...


class CampaignConfig(BaseModel):
    """One campaign's target, market, data window, and budget.

    The data window is split in time: rounds are graded on the
    ``history_days`` ending at the holdout boundary (``window_end`` on each
    round's sweep), and the most-recent ``holdout_days`` are reserved,
    never swept, for the end-of-campaign honesty read.
    """

    model_config = ConfigDict(frozen=True)

    target: str
    """The improvement target the bound ``CandidateProvider`` tunes
    (``production`` or a research family) — metadata for the record."""

    symbol: str
    timeframe: str = "4h"
    history_days: int = Field(default=1280, gt=0)
    """Days of history each round's walk-forward sweep is graded over, ending
    at the reserved holdout boundary. Defaults in step with
    ``AppConfig.campaign_history_days`` (the worker always passes that through;
    the default only applies to a directly-constructed config)."""

    holdout_days: int = Field(default=180, gt=0)
    """The most-recent days reserved as the untouched holdout — no round is
    ever graded on them; the final honesty read is."""

    scenario_count: int = Field(default=DEFAULT_SCENARIO_COUNT, gt=0)
    lookback_candles: int = Field(default=120, ge=60)
    """Context each campaign scenario sees. Campaigns default shorter than
    manual sweeps so high-timeframe validation can host enough distinct
    decision points to clear the minimum-trades bar."""

    horizon_candles: int = Field(default=30, gt=0)
    """Future candles revealed for grading each campaign scenario."""

    validation_windows: int = Field(default=3, ge=1)
    """Chronological validation slices used by each campaign sweep."""

    promotions_enabled: bool = True
    """Whether a validated round may auto-promote. Diagnostic campaign
    timeframes set this false so they add evidence without changing the
    traded configuration."""

    max_rounds: int = Field(default=8, ge=1)
    """The hard cap on rounds. The budget is what makes an iterated search
    over backtests safe: it bounds how many chances the search gets at a
    lucky winner, on top of each round's Bonferroni-corrected bar."""

    max_hours: float = Field(default=6.0, gt=0.0)
    """Wall-clock budget. Checked before each round; a campaign shares one
    CPU with live trading, so it must not run unbounded."""

    refine_factor: float = Field(default=0.5, gt=0.0, lt=1.0)
    """How much the step shrinks after a round finds no validated gain —
    coarse to fine, so the budget probes finer neighbourhoods, not the
    same one twice."""

    min_scale: float = Field(default=0.25, gt=0.0, le=1.0)
    """Once the step shrinks below this the search has converged: the
    campaign stops rather than spend the budget on indistinguishable
    neighbours."""

    base_seed: int = Field(default=7, ge=0)
    """Seed the per-round scenario draw is derived from (round seed =
    ``base_seed * 1000 + round_index``). Each round of an iterated campaign
    samples a *different* scenario set, so a winner has to clear the bar on
    independent draws rather than fit the idiosyncrasies of one fixed draw
    re-graded every round — a subtle but real overfitting channel when the
    search climbs by repeatedly probing the same sample. Deterministic from
    this base, so the whole campaign still reproduces bit for bit; the
    end-of-campaign holdout read keeps its own frozen seed so it stays
    comparable across campaigns."""

    max_lifetime_promotions: int = Field(default=0, ge=0)
    """Per-target lifetime cap on auto-promotions; ``0`` disables it. The
    campaign loop runs forever, so without an outer bound the cumulative
    multiple-comparisons exposure grows without limit — every campaign hands
    the search fresh chances to promote a fluke, and the incumbent climbs from
    inflated baselines. Once ``prior_promotions`` plus this campaign's own
    promotions reach this cap, validated, engine-confirmed winners are still
    *researched* (the round is recorded and the search refines) but no longer
    *applied*; a human can review the evidence and promote manually or raise
    the cap. It caps promotions, never research."""

    prior_promotions: int = Field(default=0, ge=0)
    """Auto-promotions this target accrued in *earlier* campaigns (summed from
    the durable campaign history by the driver). Counts against
    ``max_lifetime_promotions`` so the cap is a true lifetime bound across
    campaigns, not a per-campaign one. ``0`` for a directly-constructed
    campaign with no history."""

    auto_revert: bool = False
    """When the reserved holdout *arms a revert* — a bootstrap-significant
    out-of-sample regression of the campaign's net change — undo it instead of
    only alerting. The promoted families are reverted to the params they had
    when the campaign started, journaled as a new (revertible) version. This is
    the reproducibility tripwire: a promotion that does not reproduce its edge
    on the untouched slice is rolled back automatically. ``False`` (the safe
    library default) keeps the historical alert-only behaviour; the worker
    turns it on via ``campaign_auto_revert_on_regression``. Gated on the same
    significant evidence as the alert, so it never reverts on noise."""

    def interval(self) -> CandleInterval:
        """Parse the timeframe; raises ``ValueError`` on unknown ones."""
        return CandleInterval(self.timeframe)

    def promotions_frozen(self, promotions_this_campaign: int) -> bool:
        """Whether the per-target lifetime promotion cap is reached.

        ``0`` disables the cap. Otherwise the bound is across campaigns:
        ``prior_promotions`` (earlier campaigns) plus this campaign's
        promotions so far.
        """
        if self.max_lifetime_promotions <= 0:
            return False
        return self.prior_promotions + promotions_this_campaign >= self.max_lifetime_promotions


@dataclass(frozen=True)
class CampaignRound:
    """One round's outcome, for the status surface and the timeline.

    ``sweep_id`` ties the round to its persisted sweep (the verdict's full
    report and lineage); ``promoted_version`` ties a promotion to its
    strategy-settings journal entry. ``note`` is the one plain-words
    sentence the dashboard shows.
    """

    index: int
    scale: float
    sweep_id: int | None
    verdict: str | None
    winner: str | None
    promoted_version: int | None
    note: str
    changes: tuple[SettingChange, ...] = ()
    """For a promoted round: the field-level diff this promotion applied to
    the family's live settings (what changed), before -> after. Empty for any
    round that kept the active configuration."""


@dataclass
class CampaignStatus:
    """Live, in-memory snapshot of a campaign for the control surface.

    Mutable on purpose: the campaign updates it in place as rounds land and
    the control API reads it at request time (the same pattern as
    ``ImprovementStatus``). A campaign is in progress while ``status`` is
    ``"running"``. All times are UTC.
    """

    config: CampaignConfig
    status: str = "running"
    holdout_start: datetime | None = None
    rounds: list[CampaignRound] = field(default_factory=list)
    promotions: int = 0
    stop_reason: str | None = None
    holdout_read: dict[str, Any] | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None


def _snapshot(params: Mapping[str, Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    """Return a plain, detached copy of the active per-family parameters."""
    return {family: dict(values) for family, values in params.items()}


class ResearchCampaign:
    """Runs one campaign: sweep, promote what validates, refine, repeat.

    Everything stateful or effectful arrives as a callable so the loop sees
    the world as it is each round (the incumbent moves under it as
    promotions land) and grades/promotes through the production code paths.
    The live status is published on ``self.status`` for the control surface.
    """

    def __init__(
        self,
        *,
        sweeps: SweepStarter,
        store: SweepReader,
        candidates: CandidateProvider,
        active_params: Callable[[], Mapping[str, Mapping[str, Any]]],
        promote: Callable[[str, Mapping[str, Any], int | None, str | None], Awaitable[int]],
        confirm: Callable[[str, Mapping[str, Any], str], Awaitable[str | None]] | None = None,
        holdout: HoldoutGrader | None = None,
        clock: Callable[[], datetime] = utc_now,
        notify: Callable[[str], Awaitable[None]] | None = None,
    ) -> None:
        """Bind the loop to the worker's live state and apply paths.

        ``promote`` is the worker's journaled, paper-only apply path;
        ``confirm`` is the engine-backed gate (a veto reason, or ``None`` to
        allow); ``holdout`` grades the net move on the reserved slice (a
        non-gating read, ``None`` to skip); ``clock`` is injectable so the
        wall-clock budget is testable.
        """
        self._sweeps = sweeps
        self._store = store
        self._candidates = candidates
        self._active_params = active_params
        self._promote = promote
        self._confirm = confirm
        self._holdout = holdout
        self._clock = clock
        self._notify = notify
        self.status: CampaignStatus | None = None

    async def run(self, config: CampaignConfig) -> None:
        """Drive one campaign to a terminal status; never raises but cancel.

        Reserves the holdout once (frozen for the whole campaign), then runs
        rounds until the budget is spent or the search converges, promoting
        every validated, engine-confirmed challenger and climbing from it.
        """
        started = self._clock()
        status = CampaignStatus(config=config, started_at=started)
        self.status = status
        start_params = _snapshot(self._active_params())
        # The families this campaign actually promotes — the exact set the
        # tripwire reverts, so a first-ever promotion (no row at start) is
        # undone and a family the campaign never touched is left alone.
        promoted_families: set[str] = set()
        holdout_start = started - timedelta(days=config.holdout_days)
        status.holdout_start = holdout_start
        deadline = started + timedelta(hours=config.max_hours)
        logger.info(
            "research campaign on %s/%s started: %d-round / %gh budget, holdout from %s",
            config.target,
            config.symbol,
            config.max_rounds,
            config.max_hours,
            holdout_start.isoformat(),
        )
        scale = 1.0
        try:
            while True:
                if len(status.rounds) >= config.max_rounds:
                    status.stop_reason = (
                        f"budget spent: reached the {config.max_rounds}-round limit"
                    )
                    break
                if self._clock() >= deadline:
                    status.stop_reason = (
                        f"budget spent: reached the {config.max_hours:g}h time limit"
                    )
                    break
                if scale < config.min_scale:
                    status.stop_reason = "converged: no validated improvement at the finest step"
                    break
                advanced = await self._run_round(
                    config, holdout_start, scale, status, promoted_families
                )
                scale = 1.0 if advanced else scale * config.refine_factor
            status.holdout_read = await self._holdout_read(config, start_params, holdout_start)
            await self._maybe_revert(config, start_params, promoted_families, status.holdout_read)
            status.status = RunStatus.COMPLETED.value
            logger.info(
                "research campaign on %s/%s completed: %d round(s), %d promotion(s) — %s",
                config.target,
                config.symbol,
                len(status.rounds),
                status.promotions,
                status.stop_reason,
            )
        except asyncio.CancelledError:
            status.status = RunStatus.INTERRUPTED.value
            logger.warning("research campaign on %s/%s interrupted", config.target, config.symbol)
            raise
        except Exception:
            logger.exception("research campaign on %s/%s failed", config.target, config.symbol)
            status.status = RunStatus.FAILED.value
        finally:
            status.finished_at = self._clock()

    async def _run_round(
        self,
        config: CampaignConfig,
        holdout_start: datetime,
        scale: float,
        status: CampaignStatus,
        promoted_families: set[str],
    ) -> bool:
        """Run one round; return whether it promoted (and so advanced the incumbent).

        Appends exactly one ``CampaignRound`` to ``status`` whatever happens,
        so the budget always advances and the record is never silent. Records
        any promoted family in ``promoted_families`` so the holdout tripwire
        knows exactly what to revert.
        """
        index = len(status.rounds)
        candidates_seq, motivating = await self._candidates(self._active_params(), scale)
        candidates = tuple(candidates_seq)
        if len(candidates) < 2:
            status.rounds.append(
                CampaignRound(index, scale, None, None, None, None, "no challengers at this step")
            )
            return False
        try:
            sweep_config = SweepConfig(
                symbol=config.symbol,
                timeframe=config.timeframe,
                history_days=config.history_days,
                scenario_count=config.scenario_count,
                lookback_candles=config.lookback_candles,
                horizon_candles=config.horizon_candles,
                validation_windows=config.validation_windows,
                window_end=holdout_start,
                candidates=candidates,
                motivating_finding_ids=tuple(motivating),
                # Rotate the scenario draw per round so the search cannot climb
                # by overfitting one fixed sample re-graded every round.
                seed=config.base_seed * 1000 + index,
            )
            sweep_id = await self._sweeps.start(sweep_config)
        except ValueError as error:
            status.rounds.append(
                CampaignRound(index, scale, None, None, None, None, f"invalid sweep: {error}")
            )
            return False
        except RuntimeError:
            status.rounds.append(
                CampaignRound(
                    index, scale, None, None, None, None, "another sweep in flight; round skipped"
                )
            )
            return False
        report = await self._await_report(sweep_id)
        if report is None:
            status.rounds.append(
                CampaignRound(
                    index, scale, sweep_id, None, None, None, "sweep ended without a verdict"
                )
            )
            return False
        verdict = str(report.get("verdict") or "")
        winner_name = report.get("winner")
        if verdict != PROMOTION_VERDICT:
            status.rounds.append(
                CampaignRound(
                    index,
                    scale,
                    sweep_id,
                    verdict,
                    winner_name,
                    None,
                    f"kept the active configuration (verdict: {verdict})",
                )
            )
            return False
        winner = next(
            (candidate for candidate in candidates if candidate.name == winner_name), None
        )
        # "validated" implies a non-baseline family winner by the sweep
        # contract; the guards make a contract violation a skipped round, not
        # a wrong promotion. Recipes are never auto-promoted (owner opt-in).
        if winner is None or winner.name == candidates[0].name or winner.recipe is not None:
            status.rounds.append(
                CampaignRound(
                    index,
                    scale,
                    sweep_id,
                    verdict,
                    winner_name,
                    None,
                    "validated winner is not auto-promotable; skipped",
                )
            )
            return False
        if not config.promotions_enabled:
            status.rounds.append(
                CampaignRound(
                    index,
                    scale,
                    sweep_id,
                    verdict,
                    winner.name,
                    None,
                    f"validated on diagnostic timeframe {config.timeframe}; recorded as "
                    "research evidence, not auto-promoted",
                )
            )
            return False
        if not promotion_timeframe_allowed(config.timeframe):
            status.rounds.append(
                CampaignRound(
                    index,
                    scale,
                    sweep_id,
                    verdict,
                    winner.name,
                    None,
                    f"validated on {config.timeframe}, which is diagnostic-only for "
                    "auto-promotion; promotions require 4h or 1d validation",
                )
            )
            return False
        if config.promotions_frozen(status.promotions):
            # The per-target lifetime promotion cap is reached: keep researching
            # (record the round so the search still refines) but do not change
            # the live config. Bounds the unbounded loop's cumulative
            # multiple-comparisons exposure; a human can promote manually or
            # raise the cap after reviewing the accumulated evidence.
            total = config.prior_promotions + status.promotions
            status.rounds.append(
                CampaignRound(
                    index,
                    scale,
                    sweep_id,
                    verdict,
                    winner.name,
                    None,
                    f"lifetime promotion cap reached ({total}/{config.max_lifetime_promotions} "
                    f"for {config.target}); validated winner researched, not applied",
                )
            )
            return False
        if self._confirm is not None:
            veto = await self._confirm(winner.family, winner.params, config.symbol)
            if veto is not None:
                status.rounds.append(
                    CampaignRound(
                        index,
                        scale,
                        sweep_id,
                        verdict,
                        winner.name,
                        None,
                        f"engine confirmation vetoed the promotion: {veto}",
                    )
                )
                return False
        explanation = str(report.get("explanation", ""))
        # Capture the family's live settings before the promote moves them, so
        # the round records exactly what this promotion changed (before -> after).
        before_params = dict(self._active_params().get(winner.family, {}))
        version = await self._promote(
            winner.family, winner.params, sweep_id, f"auto-promoted (campaign): {explanation}"
        )
        status.promotions += 1
        promoted_families.add(winner.family)
        status.rounds.append(
            CampaignRound(
                index,
                scale,
                sweep_id,
                verdict,
                winner.name,
                version,
                f"promoted {winner.family} settings v{version} ({winner.name})",
                changes=settings_changes(winner.params, before_params),
            )
        )
        if self._notify is not None:
            await self._notify(
                f"campaign promoted {winner.family} settings v{version} "
                f"({winner.name}) from sweep #{sweep_id}: {explanation}"
            )
        return True

    async def _holdout_read(
        self,
        config: CampaignConfig,
        start_params: Mapping[str, Mapping[str, Any]],
        holdout_start: datetime,
    ) -> dict[str, Any] | None:
        """Grade the campaign's net move on the reserved slice (non-gating).

        Never fails the campaign: a holdout read is informative, not a veto,
        so any error resolves to "no read" with the reason logged.
        """
        if self._holdout is None:
            return None
        final_params = _snapshot(self._active_params())
        try:
            return await self._holdout(start_params, final_params, holdout_start)
        except Exception:
            logger.exception(
                "holdout read failed for campaign on %s/%s; reporting no read",
                config.target,
                config.symbol,
            )
            return None

    async def _maybe_revert(
        self,
        config: CampaignConfig,
        start_params: Mapping[str, Mapping[str, Any]],
        promoted_families: Collection[str],
        read: dict[str, Any] | None,
    ) -> None:
        """Act on an armed revert: auto-undo when enabled, else only alert.

        The read gates the action (a bootstrap-significant out-of-sample
        regression of the campaign's net change), so it fires only on real
        evidence, not on a flat or noisy move — the reproducibility tripwire. A
        promotion that does not reproduce its edge on the untouched slice is, by
        construction, the overfit the whole pipeline guards against.

        With ``auto_revert`` on, each family this campaign promoted is restored
        to its start-of-campaign value through the journaled, revertible promote
        path (a human can re-promote or re-revert), and the operator is told
        what was undone. A family that had no settings row at the start (its
        first-ever promotion) reverts to ``{}`` — i.e. back to code defaults,
        the exact pre-campaign state — which is the common, and most important,
        tripwire case. With it off, the historical behaviour: only inform, and a
        human acts on the one-click revert the read armed.
        """
        if read is None or not read.get("revert_armed"):
            return
        explanation = str(read.get("explanation", ""))
        if not config.auto_revert:
            if self._notify is not None:
                await self._notify(
                    f"campaign on {config.target}/{config.symbol}: the untouched holdout flagged "
                    f"an out-of-sample regression — revert armed for review. {explanation}"
                )
            return
        final_params = _snapshot(self._active_params())
        # Restrict to families this campaign promoted (not a global diff of the
        # shared live state), and skip any whose net change is already zero.
        reverted = [
            family
            for family in sorted(promoted_families)
            if final_params.get(family, {}) != dict(start_params.get(family, {}))
        ]
        for family in reverted:
            await self._promote(
                family,
                dict(start_params.get(family, {})),
                None,
                f"auto-revert: out-of-sample regression on {config.target}/{config.symbol}",
            )
        message = (
            f"campaign on {config.target}/{config.symbol}: the untouched holdout flagged an "
            f"out-of-sample regression — auto-reverted {', '.join(reverted) or 'nothing'} to the "
            f"pre-campaign config. {explanation}"
        )
        logger.info("%s", message)
        if self._notify is not None:
            await self._notify(message)

    async def _await_report(self, sweep_id: int) -> dict[str, Any] | None:
        """Poll until the sweep is terminal; ``None`` unless it completed."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + SWEEP_TIMEOUT.total_seconds()
        while loop.time() < deadline:
            row = await self._store.fetch_sweep(sweep_id)
            if row is None:
                # ``start`` persisted the row before returning this id, so a
                # missing row means the sweep was cancelled or removed out from
                # under us — polling to the full timeout would only stall the
                # single research lane. ``fetch_sweep`` is a deterministic
                # lookup, so a ``None`` is "gone", never a transient miss.
                logger.warning("campaign sweep %d not found; no promotion", sweep_id)
                return None
            if row.get("status") in _TERMINAL:
                if row["status"] == RunStatus.COMPLETED.value:
                    report = row.get("report")
                    return dict(report) if report is not None else None
                logger.warning("campaign sweep %d ended %s; no promotion", sweep_id, row["status"])
                return None
            await asyncio.sleep(POLL_SECONDS)
        logger.warning("campaign sweep %d timed out; no promotion", sweep_id)
        return None


class CampaignManager:
    """Owns the single in-flight campaign and its background task.

    One campaign at a time, on purpose: campaigns share the worker's CPU
    with live trading and the single research lane with sweeps and
    evaluations. The live status is read straight off the campaign.
    """

    def __init__(
        self,
        campaign: ResearchCampaign,
        spawn: Callable[[Coroutine[Any, Any, None]], asyncio.Task[None]],
    ) -> None:
        """``spawn`` ties the campaign's task to the worker's TaskGroup lifetime."""
        self._campaign = campaign
        self._spawn = spawn
        self._task: asyncio.Task[None] | None = None

    @property
    def status(self) -> CampaignStatus | None:
        """The live campaign status, or ``None`` if none has run yet."""
        return self._campaign.status

    def running(self) -> bool:
        """Whether a campaign is in flight."""
        return self._task is not None and not self._task.done()

    def start(self, config: CampaignConfig) -> asyncio.Task[None]:
        """Launch a campaign; raise ``RuntimeError`` if one is already running."""
        if self.running():
            raise RuntimeError("a research campaign is already in progress")
        config.interval()  # validate the timeframe before launching
        task = self._spawn(self._campaign.run(config))
        self._task = task
        return task

    def cancel(self) -> bool:
        """Cancel the in-flight campaign; returns whether anything was cancelled."""
        if self._task is None or self._task.done():
            return False
        self._task.cancel()
        return True
