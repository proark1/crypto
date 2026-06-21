"""The campaign driver: run research campaigns continuously (ARCHITECTURE.md §12.7).

This is the wiring that turns the dormant campaign engine into the §12.7
loop. On each turn it picks the next ``(target, symbol)`` on the same
rotation the auto-improver uses, **assembles** a campaign from the worker's
live state — the candidate provider (`improve.make_candidate_provider`), the
holdout grader (`holdout.make_holdout_grader`), and the paper-only promote
and engine-confirm paths — runs it to its budget, then rests.

One campaign at a time *by construction*: the loop is sequential, so the
driver never contends with itself for the single research lane, and a round
whose sweep loses that lane to a human-started sweep simply refines and
retries (the campaign loop's own behaviour). Like every other §12.7 effect,
promotions flow through the injected worker apply path, which is paper-only
and reversible — this module places no orders and constructs nothing in any
other mode.

The driver is still pure wiring: every dependency is injected, so it is
unit-tested with fakes (no worker, DB, or network), and the worker that
constructs and gates it (default-off) is a separate, thin change.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Mapping
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from tradebot.core.models import utc_now
from tradebot.evaluation.allocation import TargetSelector
from tradebot.evaluation.campaign import (
    CampaignConfig,
    CampaignStatus,
    ResearchCampaign,
    SweepStarter,
)
from tradebot.evaluation.holdout import CandleSpanReader, StrategyForParams, make_holdout_grader
from tradebot.evaluation.improve import IMPROVEMENT_TARGETS, ResearchReader, make_candidate_provider
from tradebot.evaluation.strategy import build_traded_strategy
from tradebot.evaluation.sweep import (
    DEFAULT_SCENARIO_COUNT,
    SweepCandidate,
    build_candidate_strategy,
)
from tradebot.strategies import FundingProvider, Strategy

logger = logging.getLogger(__name__)


def strategy_for_target(
    target: str, regime_routed: bool, funding_provider: FundingProvider | None = None
) -> StrategyForParams:
    """Build the ``StrategyForParams`` the holdout grader needs for ``target``.

    Target-aware, so the holdout grades what the target actually trades:
    ``production`` builds the regime router from the trend and mean-reversion
    params (``build_traded_strategy``, exactly as the bot trades and as
    evaluation grades it); a research family builds that family alone (the
    shape its solo competition account trades). ``regime_routed`` mirrors the
    worker's wiring for the production case. ``funding_provider`` is handed to a
    funding target so the holdout grades it on the live series, not inert.
    """
    if target == "production":

        def build_production(params: Mapping[str, Mapping[str, Any]]) -> Strategy:
            return build_traded_strategy(regime_routed=regime_routed, params_by_family=params)

        return build_production

    def build_family(params: Mapping[str, Mapping[str, Any]]) -> Strategy:
        return build_candidate_strategy(
            SweepCandidate(
                name=f"holdout-{target}", family=target, params=dict(params.get(target, {}))
            ),
            funding_provider,
        )

    return build_family


class CampaignDriverConfig(BaseModel):
    """The campaign tunables the driver applies to every campaign it runs.

    Conservative defaults: a campaign shares one CPU with live trading, so
    the round/time budget is bounded and a cooldown sits between campaigns.
    These mirror ``CampaignConfig``'s own defaults; the worker maps its
    ``TRADEBOT_CAMPAIGN_*`` settings onto this.
    """

    model_config = ConfigDict(frozen=True)

    timeframe: str = "1h"
    history_days: int = Field(default=730, gt=0)
    holdout_days: int = Field(default=60, gt=0)
    scenario_count: int = Field(default=DEFAULT_SCENARIO_COUNT, gt=0)
    max_rounds: int = Field(default=8, ge=1)
    max_hours: float = Field(default=6.0, gt=0.0)
    refine_factor: float = Field(default=0.5, gt=0.0, lt=1.0)
    min_scale: float = Field(default=0.25, gt=0.0, le=1.0)
    cooldown_minutes: float = Field(default=30.0, gt=0.0)
    max_lifetime_promotions_per_target: int = Field(default=0, ge=0)
    """Per-target lifetime cap on auto-promotions across all campaigns; ``0``
    disables it. Bounds the cumulative multiple-comparisons exposure of the
    forever-running loop: past this many promotions for a target, its
    campaigns still research but no longer change the live config (see
    ``CampaignConfig.max_lifetime_promotions``)."""

    regime_routed: bool = True
    """How the ``production`` campaign grades its holdout — the regime router
    (the bot's real shape) when on, the trend family alone when off."""


class CampaignDriver:
    """Runs research campaigns continuously across the §12.7 target rotation."""

    def __init__(
        self,
        *,
        sweeps: SweepStarter,
        store: ResearchReader,
        candle_store: CandleSpanReader,
        active_params: Callable[[], Mapping[str, Mapping[str, Any]]],
        symbols: Callable[[], tuple[str, ...]],
        promote: Callable[[str, Mapping[str, Any], int | None, str | None], Awaitable[int]],
        confirm: Callable[[str, Mapping[str, Any], str], Awaitable[str | None]] | None,
        config: CampaignDriverConfig,
        clock: Callable[[], datetime] = utc_now,
        notify: Callable[[str], Awaitable[None]] | None = None,
        enabled: Callable[[], bool] | None = None,
        record: Callable[[CampaignStatus], Awaitable[None]] | None = None,
        promotions_for: Callable[[str], Awaitable[int]] | None = None,
        funding_provider: FundingProvider | None = None,
        select: TargetSelector | None = None,
    ) -> None:
        """Bind the driver to the worker's live state and apply paths.

        ``store`` is the same ``ResearchReader`` the auto-improver uses (it
        also satisfies the campaign's sweep-row reader); ``promote`` is the
        worker's journaled, paper-only apply path; ``confirm`` is the
        engine-backed veto. ``record`` persists each finished campaign to the
        durable history (the driver itself only holds the current one in
        memory). ``promotions_for`` reads that history back — a target's
        lifetime auto-promotion count — to enforce the per-target promotion
        cap; absent, the cap is simply never reached. ``select`` is the §12.7
        target scheduler (standing-weighted); absent, the driver falls back to
        the flat round-robin, so the seam is backward-compatible. Everything
        stateful arrives as a callable so each campaign sees the world as it is
        when it runs.
        """
        self._sweeps = sweeps
        self._store = store
        self._candle_store = candle_store
        self._active_params = active_params
        self._symbols = symbols
        self._promote = promote
        self._confirm = confirm
        self._config = config
        self._clock = clock
        self._notify = notify
        self._enabled = enabled
        self._record = record
        self._promotions_for = promotions_for
        self._funding_provider = funding_provider
        self._select = select
        self._rotation = 0
        self._campaign: ResearchCampaign | None = None

    async def _next_assignment(self, symbols: tuple[str, ...]) -> tuple[str, str]:
        """Pick the next ``(target, symbol)`` — weighted if a selector is bound.

        With a ``select`` scheduler the pick is standing-weighted (§12.7
        allocation); without one it is the historical flat round-robin —
        targets rotate first, symbols second — preserving prior behaviour.
        """
        if self._select is not None:
            return await self._select.next_assignment(symbols)
        target = IMPROVEMENT_TARGETS[self._rotation % len(IMPROVEMENT_TARGETS)]
        symbol = symbols[(self._rotation // len(IMPROVEMENT_TARGETS)) % len(symbols)]
        self._rotation += 1
        return target, symbol

    @property
    def current(self) -> CampaignStatus | None:
        """The live status of the running (or last-run) campaign, or ``None``.

        Published the moment a campaign starts — ``ResearchCampaign.run`` sets
        its status synchronously before its first ``await`` — so the control
        surface can watch an in-flight campaign, not only finished ones (a
        campaign can run for hours).
        """
        return self._campaign.status if self._campaign is not None else None

    async def run(self) -> None:
        """Run campaigns forever; one failed campaign never stops the loop.

        A cooldown opens each turn: boot is busy with backfills, and a
        campaign that swept data still arriving would judge on a moving
        target. When an ``enabled`` predicate is bound (the worker's live
        Settings toggle), a turn where it reads false is skipped, so the loop
        is switched on and off at runtime without a restart.
        """
        while True:
            await asyncio.sleep(self._config.cooldown_minutes * 60.0)
            try:
                await self.run_one()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("research campaign turn failed; resting until the next")

    async def run_one(self) -> CampaignStatus | None:
        """Assemble and run one campaign for the next ``(target, symbol)``.

        Returns the finished campaign's status, or ``None`` when there is no
        coin to research. Targets rotate first (production, then each
        research family), symbols second, so every family is revisited before
        any symbol repeats — the same rotation the auto-improver uses.
        """
        if self._enabled is not None and not self._enabled():
            return None  # toggled off in Settings — idle, no campaign
        symbols = self._symbols()
        if not symbols:
            logger.info("campaign driver idle: no active coins to research")
            return None
        target, symbol = await self._next_assignment(symbols)
        prior_promotions = await self._promotions_for(target) if self._promotions_for else 0
        campaign = self._assemble(target, symbol)
        # Publish the campaign before running it: run() populates its status
        # synchronously before its first await, so the live surface sees the
        # in-flight campaign rather than waiting hours for it to finish.
        self._campaign = campaign
        await campaign.run(self._campaign_config(target, symbol, prior_promotions))
        status = campaign.status
        # Persist the finished campaign to the durable history. Best-effort:
        # the campaign already ran, so a history-write failure is logged and
        # the loop carries on rather than losing the next turn over it. (Only
        # reached on a normal finish — a cancelled campaign re-raises above.)
        if self._record is not None and status is not None:
            try:
                await self._record(status)
            except Exception:
                logger.exception(
                    "failed to record finished campaign on %s/%s to history", target, symbol
                )
        return status

    def _assemble(self, target: str, symbol: str) -> ResearchCampaign:
        """Build the campaign for one target from the injected worker state."""
        provider = make_candidate_provider(target, self._store)
        grader = make_holdout_grader(
            symbol=symbol,
            timeframe=self._config.timeframe,
            candles=self._candle_store,
            strategy_for=strategy_for_target(
                target, self._config.regime_routed, self._funding_provider
            ),
            scenario_count=self._config.scenario_count,
            clock=self._clock,
        )
        return ResearchCampaign(
            sweeps=self._sweeps,
            store=self._store,
            candidates=provider,
            active_params=self._active_params,
            promote=self._promote,
            confirm=self._confirm,
            holdout=grader,
            clock=self._clock,
            notify=self._notify,
        )

    def _campaign_config(self, target: str, symbol: str, prior_promotions: int) -> CampaignConfig:
        """Project the driver config onto one campaign's ``CampaignConfig``.

        ``prior_promotions`` is the target's lifetime auto-promotion count from
        earlier campaigns, so the per-target promotion cap binds across the
        whole loop, not just within one campaign.
        """
        config = self._config
        return CampaignConfig(
            target=target,
            symbol=symbol,
            timeframe=config.timeframe,
            history_days=config.history_days,
            holdout_days=config.holdout_days,
            scenario_count=config.scenario_count,
            max_rounds=config.max_rounds,
            max_hours=config.max_hours,
            refine_factor=config.refine_factor,
            min_scale=config.min_scale,
            max_lifetime_promotions=config.max_lifetime_promotions_per_target,
            prior_promotions=prior_promotions,
        )
