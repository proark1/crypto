"""FastAPI app factory for the control plane.

Amounts are serialized as strings (Decimal-safe — the frontend never does
money arithmetic on floats, CLAUDE.md frontend rules). The app depends on a
narrow ``BotState`` protocol rather than the worker class itself, so it can
be tested with any object exposing the same surface and never imports the
composition root.
"""

from __future__ import annotations

import logging
import secrets
from collections import deque
from collections.abc import Iterator, Mapping, Sequence
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any, Protocol

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Query, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import PlainTextResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field, ValidationError

from tradebot.backtest.parity import DivergenceReport
from tradebot.competition import ENTRY_MODES, FAMILY_DESCRIPTIONS, LINEUP
from tradebot.competition.candidacy import Condition, RoutingCandidacy
from tradebot.core.config import AppConfig
from tradebot.core.logging import log_event
from tradebot.core.metrics import MetricsCollector, format_metric
from tradebot.core.models import Candle, CandleInterval, utc_now
from tradebot.engine import TradingEngine
from tradebot.evaluation.advisor import ResearchAdvice, synthesize_advice
from tradebot.evaluation.bakeoff import (
    DEFAULT_GRID,
    BakeOffConfig,
)
from tradebot.evaluation.bakeoff import DEFAULT_SCENARIO_COUNT as BAKE_OFF_SCENARIO_COUNT
from tradebot.evaluation.improve import (
    build_candidates_for,
    build_recipe_candidates,
    select_targeting_findings,
)
from tradebot.evaluation.models import LearningFinding, RunStatus
from tradebot.evaluation.replay import load_replay
from tradebot.evaluation.runner import EvaluationRunConfig
from tradebot.evaluation.sensitivity import DEFAULT_COST_MULTIPLIERS
from tradebot.evaluation.suggestions import build_suggestions
from tradebot.evaluation.sweep import (
    DEFAULT_SCENARIO_COUNT,
    STRATEGY_FAMILIES,
    SweepCandidate,
    SweepConfig,
)
from tradebot.evaluation.timeline import WINDOW as TIMELINE_WINDOW
from tradebot.evaluation.timeline import build_timeline
from tradebot.news import NewsFlags
from tradebot.persistence import (
    CHART_BUCKET_UNITS,
    CandleStore,
    ChartCandle,
    DecisionStore,
    EvaluationStore,
    FillStore,
    StrategySettingsStore,
)
from tradebot.portfolio import Portfolio
from tradebot.signals import FeedHealth, MarketRegimeDetector

logger = logging.getLogger(__name__)


class BotState(Protocol):
    """What the control plane is allowed to see of the running bot."""

    @property
    def config(self) -> AppConfig:
        """Runtime configuration (mode, symbol, exchange)."""
        ...

    @property
    def portfolio(self) -> Portfolio:
        """Live positions, balances, and PnL."""
        ...

    @property
    def candle_store(self) -> CandleStore:
        """Persisted candles; the newest close is the mark price."""
        ...

    @property
    def fill_store(self) -> FillStore:
        """The persistent fill journal."""
        ...

    @property
    def engines(self) -> Mapping[str, TradingEngine]:
        """The production bot's trading loops, one per symbol."""
        ...

    def all_engines(self) -> Iterator[TradingEngine]:
        """Every engine across every competition account.

        Pause/resume/kill act through this: an operator halt must mean
        every account, never "except the challengers".
        """
        ...

    async def competition_snapshot(self) -> list[dict[str, Any]]:
        """Leaderboard rows (Decimal amounts), best equity first."""
        ...

    async def routing_candidacies(self) -> list[RoutingCandidacy]:
        """§13.7 routing-evidence gate per research family (flag, never flip)."""
        ...

    async def start_comparison(self, config: EvaluationRunConfig) -> list[int]:
        """Grade the whole lineup on identical scenarios; returns run ids."""
        ...

    async def start_bake_off(self, config: BakeOffConfig) -> int:
        """Start a bake-off across the grid; returns the job id."""
        ...

    async def bake_off(self, job_id: int) -> dict[str, Any] | None:
        """Return one bake-off job row, or ``None`` if unknown."""
        ...

    async def list_bake_offs(self, limit: int = 20) -> list[dict[str, Any]]:
        """Return recent bake-off jobs, newest first."""
        ...

    async def bot_detail(self, bot_id: str) -> dict[str, Any]:
        """One bot's leaderboard row + positions + strategy descriptor."""
        ...

    async def pause_bot(self, bot_id: str) -> None:
        """Mute one bot's entries (``KeyError`` unknown)."""
        ...

    async def resume_bot(self, bot_id: str) -> None:
        """Un-mute one bot's entries (``KeyError`` unknown)."""
        ...

    async def kill_bot(self, bot_id: str) -> tuple[int, list[str]]:
        """Halt one bot and flatten it; returns (exits, failure reasons)."""
        ...

    async def create_custom_bot(
        self, label: str, description: str, rules: Mapping[str, Any]
    ) -> str:
        """Build, persist, and start a user bot; returns its id."""
        ...

    async def update_custom_bot(self, bot_id: str, rules: Mapping[str, Any]) -> None:
        """Replace a custom bot's recipe and hot-swap its strategy."""
        ...

    async def delete_custom_bot(self, bot_id: str) -> None:
        """Retire a custom bot (journals stay)."""
        ...

    async def reset_bot_capital(self, bot_id: str, new_balance_quote: Decimal) -> None:
        """Reset a bot's account to a new starting capital (must be flat)."""
        ...

    def trading_fees(self) -> Mapping[str, Decimal]:
        """Active per-side trading fees in bps (``buy_fee_bps``/``sell_fee_bps``)."""
        ...

    async def update_trading_fees(self, *, buy_fee_bps: Decimal, sell_fee_bps: Decimal) -> None:
        """Set and persist both trading fees; effective on the next fill."""
        ...

    def fill_store_for(self, bot_id: str) -> FillStore:
        """One bot's fill journal view (``KeyError`` unknown)."""
        ...

    def decision_store_for(self, bot_id: str) -> DecisionStore:
        """One bot's decision trail view (``KeyError`` unknown)."""
        ...

    async def persist_risk_state(self) -> None:
        """Persist the brake/pause snapshot now (operator halts cannot wait)."""
        ...

    @property
    def regime_detector(self) -> MarketRegimeDetector | None:
        """The regime gate's detector, or ``None`` when the gate is off."""
        ...

    @property
    def regime_disabled_reason(self) -> str | None:
        """Why the regime gate is unexpectedly off (reference feed missing).

        ``None`` when the gate is running or was simply never enabled — it
        flags only the surprising case where a configured gate had to be
        switched off because its reference market is not traded.
        """
        ...

    def feed_health(self, symbol: str) -> FeedHealth | None:
        """Return the symbol's market-data health, or ``None`` without a feed."""
        ...

    async def divergence_report(
        self, symbol: str, window_hours: int = 24, window_end: datetime | None = None
    ) -> DivergenceReport:
        """Compute the §10 paper-gate metric (``KeyError`` for an untraded coin)."""
        ...

    async def add_coin(self, symbol: str) -> None:
        """Start trading a coin at runtime (``ValueError`` on bad input)."""
        ...

    async def remove_coin(self, symbol: str) -> None:
        """Stop trading a coin (``KeyError`` unknown, ``RuntimeError`` unsafe)."""
        ...

    @property
    def decision_store(self) -> DecisionStore:
        """The explainability trail: every signal and its fate."""
        ...

    @property
    def evaluation_store(self) -> EvaluationStore:
        """Persisted evaluation runs, scenarios, and verdicts."""
        ...

    @property
    def strategy_params(self) -> Mapping[str, Mapping[str, Any]]:
        """The active (possibly auto-promoted) parameters per strategy family."""
        ...

    async def start_evaluation(self, config: EvaluationRunConfig) -> int:
        """Start a run (``RuntimeError`` if one is in flight, ``ValueError`` bad config)."""
        ...

    def evaluation_strategies(self) -> list[dict[str, str]]:
        """Every bot a run can grade: id, label, description, kind."""
        ...

    def recipe_for(self, bot_id: str) -> dict[str, Any] | None:
        """Return a custom bot's recipe, or ``None`` for a built-in bot."""
        ...

    def improvement_status(self) -> dict[str, Any]:
        """Report the §12.7 automated-improvement loop's schedule and last outcome."""
        ...

    def campaign_status(self) -> dict[str, Any]:
        """Report the §12.7 campaign loop: budget and the current/last campaign."""
        ...

    async def campaign_history(self, limit: int = 20) -> list[dict[str, Any]]:
        """Past finished campaigns' snapshots, newest first."""
        ...

    async def update_campaign_enabled(self, *, enabled: bool) -> None:
        """Toggle the §12.7 campaign loop on/off and persist it; effective live."""
        ...

    def note_finding_acceptance(self, run_id: int) -> None:
        """Arm (or ride) the accept-triggered coalescing sweep for the run."""
        ...

    def accept_sweep_pending(self, run_id: int) -> bool:
        """Report whether the run's coalescing sweep timer is armed."""
        ...

    def cancel_evaluation(self, run_id: int) -> bool:
        """Cancel the in-flight run; False when it is not running."""
        ...

    async def start_sweep(self, config: SweepConfig) -> int:
        """Start a sweep (``RuntimeError`` if one is in flight, ``ValueError`` bad config)."""
        ...

    def cancel_sweep(self, sweep_id: int) -> bool:
        """Cancel the in-flight sweep; False when it is not running."""
        ...

    @property
    def metrics(self) -> MetricsCollector:
        """Bus-fed counters for the /metrics endpoint."""
        ...

    @property
    def strategy_settings_store(self) -> StrategySettingsStore:
        """Versioned strategy parameters (automated promotions + reverts)."""
        ...

    async def revert_strategy_version(self, version_id: int) -> int:
        """Re-apply a historical settings version (``KeyError`` unknown)."""
        ...

    @property
    def news_flags(self) -> NewsFlags:
        """Active negative-news flags (gauge + status surfaces)."""
        ...


class PositionResponse(BaseModel):
    """One open position, amounts as strings."""

    symbol: str
    quantity_base: str
    average_entry_price_quote: str
    unrealized_pnl_quote: str | None


class BreakersResponse(BaseModel):
    """Circuit-breaker state: why entries are blocked, if they are."""

    tripped_reason: str | None
    cooldown_until: str | None
    entries_today: int


class RegimeResponse(BaseModel):
    """Why entries are (or are not) flowing: the gate's view of the market."""

    enabled: bool
    symbol: str | None
    label: str | None
    """"warming_up" | "trending" | "ranging" | "risk_off" when enabled."""

    reasons: list[str]
    reason: str | None = None
    """Set only when a configured gate was switched off because its reference
    market is not traded — the surprising "entries run ungated" case."""


class DataHealthResponse(BaseModel):
    """Per-coin market-data health: is this symbol's feed safe to trade on.

    ``healthy`` is ``False`` until the feed's first backfill confirms gap-free
    history and after any backfill fails; ``reason`` explains why when it is.
    Entries are paused while degraded (the data-health gate), so this is the
    first place to look when a coin's entries are quietly not firing.
    """

    healthy: bool
    reason: str | None


class StatusResponse(BaseModel):
    """The three-second answer to "is everything okay?"."""

    mode: str
    paused: bool
    protective_stop_quote: str | None
    """The armed protective stop level, or ``None`` while flat/unarmed."""

    regime: RegimeResponse
    """The regime gate's current verdict — the first place to look when
    every entry shows up gated."""

    data_health: DataHealthResponse
    """The selected coin's market-data health; entries pause while degraded."""

    symbol: str
    symbols: list[str]
    exchange_id: str
    quote_currency: str
    quote_balance: str
    realized_pnl_quote: str
    position: PositionResponse | None
    last_candle_close_time: str | None
    mark_price_quote: str | None
    equity_quote: str | None
    breakers: BreakersResponse


class HoldingResponse(BaseModel):
    """One asset the paper account holds, valued at the latest mark."""

    asset: str
    symbol: str | None
    """The trading pair behind a coin holding; ``None`` for the quote
    currency itself, which is held free rather than through a pair."""

    quantity: str
    mark_price_quote: str | None
    value_quote: str | None
    unrealized_pnl_quote: str | None


class WalletResponse(BaseModel):
    """What the account holds right now: free quote plus every coin."""

    quote_currency: str
    equity_quote: str | None
    holdings: list[HoldingResponse]


class CommandResponse(BaseModel):
    """Outcome of a control command."""

    paused: bool
    detail: str


class DecisionResponse(BaseModel):
    """One signal and its fate, with the reasons shown verbatim."""

    signal_id: str
    strategy_name: str
    symbol: str
    side: str
    stop_price_quote: str
    reasons: list[str]
    outcome: str
    created_at: str


class CoinActionRequest(BaseModel):
    """Names the coin to add or remove (in the body: symbols contain ``/``)."""

    symbol: str


class ProposalActionRequest(BaseModel):
    """Identifies the proposal to act on.

    In the body rather than the path: signal ids contain the symbol (e.g.
    ``trend_following:BTC/USDT:...``), whose slash would break path routing.
    """

    signal_id: str


class ProposalResponse(BaseModel):
    """One pending co-pilot proposal awaiting approve/reject."""

    signal_id: str
    symbol: str
    side: str
    strategy_name: str
    proposal_price_quote: str
    stop_price_quote: str
    reasons: list[str]
    created_at: str
    expires_at: str


class CandleResponse(BaseModel):
    """One OHLCV candle for charting, amounts as strings."""

    open_time: str
    open_quote: str
    high_quote: str
    low_quote: str
    close_quote: str
    volume_base: str


class EvaluationStartRequest(BaseModel):
    """Shape of a new evaluation run; symbols default to the active coins."""

    symbols: list[str] | None = None
    timeframes: list[str] = ["1h"]
    history_days: int = 365
    scenario_count: int = DEFAULT_SCENARIO_COUNT
    lookback_candles: int = 200
    horizon_candles: int = 60
    seed: int = 7
    strategy: str = "production"
    """Which bot the run grades: a lineup entry or a custom bot id (see
    ``GET /evaluations/strategies``). The default is the incumbent."""


class EvaluationStartResponse(BaseModel):
    """Acknowledgement that a run was created and launched."""

    run_id: int
    detail: str


class SuggestedEvaluationResponse(BaseModel):
    """One ready-to-run evaluation shape, fitted to a coin's stored history.

    Mirrors ``EvaluationStartRequest``'s knobs so the frontend can submit a
    suggestion verbatim — one click, no fields to fill in.
    """

    symbol: str
    timeframe: str
    history_days: int
    expected_candles: int
    scenario_count: int
    title: str
    rationale: str


class EvaluationStrategyResponse(BaseModel):
    """One bot an evaluation run can grade — the research bot selector."""

    id: str
    label: str
    description: str
    kind: str
    """"production" | "builtin" | "custom" — mirrors the competition badges."""


class ImprovementStatusResponse(BaseModel):
    """The automated improvement loop's schedule and latest outcome (§12.7).

    ``last_outcome`` is the loop's own plain-words sentence; a cycle is in
    progress when ``last_cycle_started_at`` is newer than
    ``last_cycle_finished_at``. All times are ISO-8601 UTC.
    """

    enabled: bool
    interval_hours: int
    history_days: int
    timeframe: str
    last_cycle_started_at: str | None
    last_cycle_finished_at: str | None
    last_outcome: str | None
    next_cycle_at: str | None


class SettingChangeResponse(BaseModel):
    """One parameter a promotion changed, as display strings.

    ``before`` is null when the field is new in this version; ``after`` is
    null when the field was dropped. Shared by the campaign round trail and
    the research timeline (both report "what this promotion changed").
    """

    field: str
    before: str | None
    after: str | None


class CampaignRoundResponse(BaseModel):
    """One round of a campaign: its step, sweep, verdict, and any promotion."""

    index: int
    scale: float
    sweep_id: int | None
    verdict: str | None
    winner: str | None
    promoted_version: int | None
    note: str
    changes: list[SettingChangeResponse] = []
    """For a promoted round: the field-level settings diff (what changed)."""


class CampaignSnapshotResponse(BaseModel):
    """A running (or last-run) campaign: target, progress, and the holdout read.

    ``status`` is "running" while in flight; ``holdout_read`` is the
    non-gating start-vs-final read on the reserved slice, null until the
    campaign ends. All times are ISO-8601 UTC.
    """

    target: str
    symbol: str
    status: str
    promotions: int
    stop_reason: str | None
    holdout_start: str | None
    started_at: str | None
    finished_at: str | None
    holdout_read: dict[str, Any] | None
    rounds: list[CampaignRoundResponse]


class CampaignStatusResponse(BaseModel):
    """Whether the §12.7 campaign loop is on, its budget, and the current run.

    ``campaign`` is the current or last campaign's snapshot, or ``None`` when
    campaigns are off or none has run yet.
    """

    enabled: bool
    max_rounds: int
    max_hours: float
    timeframe: str
    campaign: CampaignSnapshotResponse | None


class EvaluationRunResponse(BaseModel):
    """One run's status, progress, and (when completed) its report."""

    id: int
    created_at: str
    status: str
    strategy: str
    comparison_group: int | None
    symbols: list[str]
    timeframes: list[str]
    progress_done: int
    progress_total: int
    config: dict[str, Any]
    summary: dict[str, Any] | None


def _run_response(run: dict[str, Any]) -> EvaluationRunResponse:
    """Serialize a run row for the API."""
    return EvaluationRunResponse(
        id=run["id"],
        created_at=run["created_at"].isoformat(),
        status=run["status"],
        strategy=run["strategy"],
        comparison_group=run["comparison_group"],
        symbols=list(run["symbols"]),
        timeframes=list(run["timeframes"]),
        progress_done=run["progress_done"],
        progress_total=run["progress_total"],
        config=run["config"],
        summary=run["summary"],
    )


def _bake_off_response(job: dict[str, Any]) -> BakeOffJobResponse:
    """Serialize a bake-off job row for the API (timestamps to ISO strings)."""
    return BakeOffJobResponse(
        id=job["id"],
        created_at=job["created_at"].isoformat(),
        updated_at=job["updated_at"].isoformat(),
        status=job["status"],
        config=job["config"],
        contestants=list(job["contestants"]),
        cells_done=job["cells_done"],
        cells_total=job["cells_total"],
        results=job["results"],
    )


class CompetitorResponse(BaseModel):
    """One strategy-competition leaderboard row, amounts as strings."""

    bot_id: str
    label: str
    description: str
    is_production: bool
    kind: str
    """"production" | "builtin" | "custom" — drives badges and which
    actions (edit/delete) the UI may offer."""

    paused: bool
    equity_quote: str | None
    initial_balance_quote: str
    return_fraction: str | None
    quote_balance: str
    realized_pnl_quote: str
    unrealized_pnl_quote: str | None
    open_positions: int
    entry_fills: int
    exit_fills: int
    breaker_tripped_reason: str | None


class CandidacyConditionResponse(BaseModel):
    """One §13.7 condition: whether it is met, and a plain-words reason."""

    met: bool
    detail: str


class RoutingCandidacyResponse(BaseModel):
    """One research family's §13.7 routing-evidence verdict (flag, never flip)."""

    family: str
    is_candidate: bool
    validated_edge: CandidacyConditionResponse
    beats_incumbent: CandidacyConditionResponse
    live_paper: CandidacyConditionResponse


def _condition_response(condition: Condition) -> CandidacyConditionResponse:
    return CandidacyConditionResponse(met=condition.met, detail=condition.detail)


def _candidacy_response(candidacy: RoutingCandidacy) -> RoutingCandidacyResponse:
    """Serialize one family's routing candidacy for the research screen."""
    return RoutingCandidacyResponse(
        family=candidacy.family,
        is_candidate=candidacy.is_candidate,
        validated_edge=_condition_response(candidacy.validated_edge),
        beats_incumbent=_condition_response(candidacy.beats_incumbent),
        live_paper=_condition_response(candidacy.live_paper),
    )


def _competitor_response(row: Mapping[str, Any]) -> CompetitorResponse:
    """Serialize one leaderboard row (Decimal amounts -> strings)."""
    return CompetitorResponse(
        bot_id=row["bot_id"],
        label=row["label"],
        description=row["description"],
        is_production=row["is_production"],
        kind=row["kind"],
        paused=row["paused"],
        equity_quote=_optional_str(row["equity_quote"]),
        initial_balance_quote=str(row["initial_balance_quote"]),
        return_fraction=_optional_str(row["return_fraction"]),
        quote_balance=str(row["quote_balance"]),
        realized_pnl_quote=str(row["realized_pnl_quote"]),
        unrealized_pnl_quote=_optional_str(row["unrealized_pnl_quote"]),
        open_positions=row["open_positions"],
        entry_fills=row["entry_fills"],
        exit_fills=row["exit_fills"],
        breaker_tripped_reason=row["breaker_tripped_reason"],
    )


class CompetitionResponse(BaseModel):
    """The live leaderboard: every competitor, best equity first."""

    quote_currency: str
    competitors: list[CompetitorResponse]


class BotPositionResponse(BaseModel):
    """One open position on a bot's detail page, amounts as strings."""

    symbol: str
    quantity_base: str
    average_entry_price_quote: str
    mark_price_quote: str | None
    unrealized_pnl_quote: str | None


class BotDetailResponse(BaseModel):
    """Everything the bot detail page needs in one fetch."""

    summary: CompetitorResponse
    positions: list[BotPositionResponse]
    strategy: dict[str, Any]
    """What the bot trades: ``{"kind": "production", "families": ...}`` |
    ``{"kind": "builtin", "family", "params"}`` | ``{"kind": "custom",
    "rules"}``. Parameters are plain JSON for display/editing only."""


class CreateBotRequest(BaseModel):
    """The bot builder's submission."""

    name: str
    description: str = ""
    rules: dict[str, Any]
    """``{"entry_mode": "any"|"all", "families": {family: {params}}}`` —
    validated server-side against the real strategy config models."""


class CreateBotResponse(BaseModel):
    """Acknowledgement that a custom bot was created and started."""

    bot_id: str
    detail: str


class UpdateBotRulesRequest(BaseModel):
    """A custom bot's replacement recipe."""

    rules: dict[str, Any]


class ResetBotCapitalRequest(BaseModel):
    """A bot's new starting capital, in the quote currency.

    Saving resets the bot's paper account: its journals are purged and it
    restarts from this balance, so it is only allowed while the bot is flat.
    """

    initial_balance_quote: Decimal


class TradingFeesResponse(BaseModel):
    """The active per-side trading fees.

    ``*_fee_percent`` is what the settings UI shows and edits (``"0.1"`` =
    0.1% of notional); ``*_fee_bps`` is the exact basis-point value it maps
    to, kept as a string so the Decimal precision survives the JSON boundary.
    """

    buy_fee_percent: str
    sell_fee_percent: str
    buy_fee_bps: str
    sell_fee_bps: str


class UpdateTradingFeesRequest(BaseModel):
    """New trading fees as percentages of notional (``0.1`` = 0.1%).

    Percent (not bps) is the unit operators think in; the route converts to
    basis points with ``Decimal`` so nothing is ever rounded through a float.
    """

    buy_fee_percent: Decimal
    sell_fee_percent: Decimal


class CampaignSettingsResponse(BaseModel):
    """The §12.7 campaign loop's on/off and budget, for the Settings tab.

    ``enabled`` is the live runtime toggle (persisted, no redeploy); the budget
    fields are read-only context for the switch.
    """

    enabled: bool
    max_rounds: int
    max_hours: float
    timeframe: str


class UpdateCampaignSettingsRequest(BaseModel):
    """Flip the §12.7 campaign loop on or off."""

    enabled: bool


class RuleOptionResponse(BaseModel):
    """One pickable rule (strategy family) for the bot builder."""

    family: str
    label: str
    description: str
    defaults: dict[str, Any]
    """The family's complete default parameters, for the advanced editor."""


class BotBuilderOptionsResponse(BaseModel):
    """Everything the builder UI needs to render its choices."""

    families: list[RuleOptionResponse]
    entry_modes: list[str]


class ComparisonStartResponse(BaseModel):
    """Acknowledgement that a comparison batch was created and launched."""

    group_id: int
    run_ids: list[int]
    detail: str


class ComparisonResponse(BaseModel):
    """One comparison batch: runs over identical scenarios, lineup order."""

    group_id: int
    created_at: str
    runs: list[EvaluationRunResponse]


class BakeOffStartRequest(BaseModel):
    """Start a bake-off. Every field defaults, so the UI can post ``{}``.

    Symbols default to the live coins; the grid and scenario shape default
    to the module's recommended values, so a one-click bake-off needs no
    configuration at all. ``grid`` maps each timeframe to the history depths
    (in days) it is swept over — the depths are timeframe-relative because
    feasibility is a candle count, not a day count (see ``DEFAULT_GRID``).
    """

    symbols: list[str] | None = None
    grid: dict[str, list[int]] = Field(
        default_factory=lambda: {timeframe: list(windows) for timeframe, windows in DEFAULT_GRID}
    )
    scenario_count: int = BAKE_OFF_SCENARIO_COUNT
    seed: int = 7


class BakeOffStartResponse(BaseModel):
    """Acknowledgement that a bake-off job was created and launched."""

    job_id: int
    cells_total: int
    detail: str


class BakeOffJobResponse(BaseModel):
    """One bake-off job: status, progress, and the (running or final) ranking.

    ``results`` is the raw aggregate the worker stores — ``{"ranking": [...],
    "cells": [...]}`` — passed through verbatim so the UI renders the
    leaderboard and the per-cell grid without the API reshaping money it
    deliberately keeps as strings. ``None`` until the first cell finishes.
    """

    id: int
    created_at: str
    updated_at: str
    status: str
    config: dict[str, Any]
    contestants: list[str]
    cells_done: int
    cells_total: int
    results: dict[str, Any] | None


class ScenarioSummaryResponse(BaseModel):
    """One graded scenario row for the replay browser, amounts as strings."""

    scenario_id: int
    run_id: int
    symbol: str
    timeframe: str
    decision_time: str
    scenario_class: str
    trend: str
    volatility: str
    events: list[str]
    decision: str
    verdict: str
    r_multiple: str | None
    timing: str | None


class ScenarioReplayResponse(BaseModel):
    """Everything the replay viewer needs for one scenario.

    ``window`` is the blind context the bot decided on (its last candle
    closes at the decision time); ``horizon`` is the future it was graded
    against, for the viewer to reveal candle by candle.
    """

    scenario: ScenarioSummaryResponse
    confidence: float | None
    reasons: list[str]
    entry_price_quote: str | None
    exit_price_quote: str | None
    pnl_quote: str | None
    mfe_r: str | None
    mae_r: str | None
    duration_candles: int | None
    stop_hit: bool | None
    oracle_r: str | None
    window: list[CandleResponse]
    horizon: list[CandleResponse]


def _optional_str(value: Any) -> str | None:
    """Stringify a nullable Decimal column without inventing a zero."""
    return None if value is None else str(value)


def _optional_iso(value: datetime | None) -> str | None:
    """ISO-format a nullable timestamp without inventing an epoch."""
    return None if value is None else value.isoformat()


def _as_iso(value: datetime | str | None) -> str | None:
    """ISO-format a timestamp that may already be a string (from JSONB).

    The live campaign snapshot carries ``datetime``; the persisted history
    snapshot carries ISO strings. Accepting both lets one response builder
    serve the current campaign and the stored ones without drifting apart.
    """
    if value is None or isinstance(value, str):
        return value
    return value.isoformat()


def _campaign_snapshot_response(snapshot: Mapping[str, Any]) -> CampaignSnapshotResponse:
    """Map a campaign snapshot (live or persisted) to its API response."""
    return CampaignSnapshotResponse(
        target=snapshot["target"],
        symbol=snapshot["symbol"],
        status=snapshot["status"],
        promotions=snapshot["promotions"],
        stop_reason=snapshot["stop_reason"],
        holdout_start=_as_iso(snapshot["holdout_start"]),
        started_at=_as_iso(snapshot["started_at"]),
        finished_at=_as_iso(snapshot["finished_at"]),
        holdout_read=snapshot["holdout_read"],
        rounds=[CampaignRoundResponse(**round_record) for round_record in snapshot["rounds"]],
    )


def _scenario_summary(row: Mapping[str, Any]) -> ScenarioSummaryResponse:
    """Serialize one joined scenario+result row for the API."""
    return ScenarioSummaryResponse(
        scenario_id=row["scenario_id"],
        run_id=row["run_id"],
        symbol=row["symbol"],
        timeframe=row["timeframe"],
        decision_time=row["decision_time"].isoformat(),
        scenario_class=row["scenario_class"],
        trend=row["trend"],
        volatility=row["volatility"],
        events=list(row["events"]),
        decision=row["decision"],
        verdict=row["verdict"],
        r_multiple=_optional_str(row["r_multiple"]),
        timing=row["timing"],
    )


class SweepCandidateRequest(BaseModel):
    """One named parameter set (of one strategy family) to compete in a sweep."""

    name: str
    params: dict[str, Any]
    family: str = "trend_following"


class SweepStartRequest(BaseModel):
    """Shape of a new sweep; the symbol defaults to the first active coin.

    Omitting ``candidates`` derives the grid the automated improver would
    sweep: single-knob variants of the parameters the bot is trading right
    now, plus challengers targeted at the latest completed run's
    non-rejected findings (their ids recorded as the sweep's motivation).
    ``candidates[0]`` is always treated as the baseline.
    """

    symbol: str | None = None
    timeframe: str = "1h"
    history_days: int = 365
    scenario_count: int = DEFAULT_SCENARIO_COUNT
    lookback_candles: int = 200
    horizon_candles: int = 60
    seed: int = 7
    training_fraction: float = 0.7
    validation_windows: int = 3
    candidates: list[SweepCandidateRequest] | None = None
    motivating_finding_ids: list[int] = []


class StrategyVersionResponse(BaseModel):
    """One versioned strategy configuration: who trades what, since when."""

    id: int
    family: str
    params: dict[str, Any]
    source_sweep_id: int | None
    note: str | None
    activated_at: str


class SweepResponse(BaseModel):
    """One sweep's status and (when completed) its walk-forward report."""

    id: int
    created_at: str
    status: str
    symbol: str
    timeframe: str
    config: dict[str, Any]
    motivating_finding_ids: list[int]
    report: dict[str, Any] | None


def _sweep_response(sweep: Mapping[str, Any]) -> SweepResponse:
    """Serialize a sweep row for the API."""
    return SweepResponse(
        id=sweep["id"],
        created_at=sweep["created_at"].isoformat(),
        status=sweep["status"],
        symbol=sweep["symbol"],
        timeframe=sweep["timeframe"],
        config=sweep["config"],
        motivating_finding_ids=list(sweep["motivating_finding_ids"]),
        report=sweep["report"],
    )


class FindingResponse(BaseModel):
    """One mined mistake pattern awaiting (or carrying) the human verdict."""

    id: int
    run_id: int
    pattern: str
    evidence_scenario_ids: list[int]
    affected_count: int
    average_r_impact: str
    suggestion: str
    confidence: str
    status: str
    created_at: str
    seen_in_prior_runs: int = 0
    """How many earlier completed runs of the same bot mined this same
    pattern (within the timeline's bounded window) — recurrence is the
    pattern text itself, deterministic per miner (§12.2)."""

    first_seen_run_id: int | None = None
    """The earliest of those runs, for "recurred since run #N" prose."""

    sweep_queued: bool = False
    """True while the run's accept-triggered coalescing timer is armed —
    the verdict has been heard and a targeted sweep is about to start."""

    latest_sweep_id: int | None = None
    """The newest sweep this finding motivated, with its status/verdict —
    the finding card's cause-to-effect chain (accepted -> swept -> verdict)."""

    latest_sweep_status: str | None = None
    latest_sweep_verdict: str | None = None


class ResearchAdviceResponse(BaseModel):
    """The AI advisor's read of a run (§12.9), or ``available=false`` when absent.

    Advisory only: ``advice`` is a recommendation a human may act on by arming
    a sweep from a hypothesis — it never changes the strategy. ``available`` is
    false (with ``advice`` null) whenever the advisor is disabled, lacks its
    optional dependency or key, or the model declined or errored, so the UI can
    distinguish "no advice to show" from a request failure.
    """

    available: bool
    advice: ResearchAdvice | None = None


def _finding_response(
    finding_id: int,
    finding: LearningFinding,
    seen_in_prior_runs: int = 0,
    first_seen_run_id: int | None = None,
    sweep_queued: bool = False,
    latest_sweep: Mapping[str, Any] | None = None,
) -> FindingResponse:
    """Serialize one finding for the API."""
    report = (latest_sweep or {}).get("report") or {}
    return FindingResponse(
        id=finding_id,
        run_id=finding.run_id,
        pattern=finding.pattern,
        evidence_scenario_ids=list(finding.evidence_scenario_ids),
        affected_count=finding.affected_count,
        average_r_impact=str(finding.average_r_impact),
        suggestion=finding.suggestion,
        confidence=finding.confidence,
        status=finding.status,
        created_at=finding.created_at.isoformat(),
        seen_in_prior_runs=seen_in_prior_runs,
        first_seen_run_id=first_seen_run_id,
        sweep_queued=sweep_queued,
        latest_sweep_id=None if latest_sweep is None else latest_sweep["id"],
        latest_sweep_status=None if latest_sweep is None else latest_sweep["status"],
        latest_sweep_verdict=report.get("verdict"),
    )


class TimelineEventResponse(BaseModel):
    """One research-timeline entry (§12.8): server-composed prose + linkage."""

    at: str
    kind: str
    """"evaluation" | "sweep" | "promotion" — drives the UI's badges."""

    headline: str
    detail: str | None
    status: str | None
    strategy: str | None
    run_id: int | None
    sweep_id: int | None
    version_id: int | None
    expectancy_r: str | None
    verdict: str | None
    new_patterns: list[str]
    resolved_patterns: list[str]
    changes: list[SettingChangeResponse]
    """For a promotion: the field-level settings diff (what changed). Empty
    for runs and sweeps."""


def _candle_response(candle: Candle | ChartCandle) -> CandleResponse:
    """Serialize one candle or aggregated bucket for charting, amounts as strings."""
    return CandleResponse(
        open_time=candle.open_time.isoformat(),
        open_quote=str(candle.open_quote),
        high_quote=str(candle.high_quote),
        low_quote=str(candle.low_quote),
        close_quote=str(candle.close_quote),
        volume_base=str(candle.volume_base),
    )


class FillResponse(BaseModel):
    """One journaled fill, amounts as strings.

    ``value_quote`` is the gross notional of the trade (price * quantity) in
    quote currency, computed here as ``Decimal`` so the frontend never does
    money arithmetic; it excludes ``fee_quote``. ``id`` is the journal row's
    surrogate key, opaque to the client except as the ``before_id`` cursor for
    fetching the next older page.
    """

    id: int
    client_order_id: str
    symbol: str
    side: str
    price_quote: str
    quantity_base: str
    value_quote: str
    fee_quote: str
    filled_at: str


class AuthLockout:
    """Sliding-window brute-force brake for the bearer-token check.

    More than ``max_failures`` bad tokens inside ``window`` lock
    authentication for ``cooldown``. State is in-memory by design: a
    restart clears it, which costs an attacker nothing meaningful (the
    window is a minute) and keeps the hot path free of database writes.
    """

    def __init__(
        self,
        max_failures: int = 10,
        window: timedelta = timedelta(minutes=1),
        cooldown: timedelta = timedelta(minutes=1),
    ) -> None:
        """Defaults allow honest typos but make brute force impractical."""
        self._max_failures = max_failures
        self._window = window
        self._cooldown = cooldown
        self._failures: deque[datetime] = deque()
        self._locked_until: datetime | None = None

    def record_failure(self, now: datetime) -> None:
        """Count one bad token; trips the lock when the window overflows."""
        self._failures.append(now)
        while self._failures and now - self._failures[0] > self._window:
            self._failures.popleft()
        if len(self._failures) > self._max_failures:
            self._locked_until = now + self._cooldown
            log_event(
                logger,
                logging.WARNING,
                "auth_locked",
                locked_until=self._locked_until.isoformat(),
                failures=len(self._failures),
                window=str(self._window),
            )

    def locked_until(self, now: datetime) -> datetime | None:
        """Return the lock expiry if authentication is paused at ``now``."""
        if self._locked_until is not None and now < self._locked_until:
            return self._locked_until
        return None


def _parse_cors_origins(raw: str) -> list[str]:
    """Split the comma-separated origins setting, ignoring blanks.

    Trailing slashes are stripped because an Origin header never carries
    one — ``https://app.example.com/`` pasted from a browser address bar
    would otherwise silently never match.
    """
    return [origin.strip().rstrip("/") for origin in raw.split(",") if origin.strip()]


def create_health_only_app() -> FastAPI:
    """Build a liveness-only app for when the control plane is disabled.

    The platform healthcheck must work in every configuration; running a
    bot whose deploy can never be marked healthy just because the API token
    is unset would be a trap.
    """
    app = FastAPI(title="tradebot health")

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    return app


def create_app(state: BotState, api_token: str) -> FastAPI:
    """Build the control-plane app; every route requires the bearer token."""
    if not api_token:
        raise ValueError("control API requires a non-empty token; refusing to build")

    bearer = HTTPBearer(auto_error=False)
    lockout = AuthLockout()

    def require_token(
        credentials: HTTPAuthorizationCredentials | None = Depends(bearer),
    ) -> None:
        if lockout.locked_until(utc_now()) is not None:
            # Brute-force brake (LIVE_TRADING_CHECKLIST §8): after a burst
            # of bad tokens, authentication pauses entirely for a cooldown.
            # Locking globally is deliberate: there is one operator and one
            # token, the bot itself keeps trading (and Telegram keeps
            # alerting) while the control plane cools down, and per-IP
            # tracking behind a PaaS proxy would punish the wrong address.
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="too many failed authentication attempts; try again shortly",
            )
        # compare_digest keeps token comparison constant-time.
        if credentials is None or not secrets.compare_digest(credentials.credentials, api_token):
            lockout.record_failure(utc_now())
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="missing or invalid bearer token",
            )

    def active_symbols() -> list[str]:
        """Return the live coin set.

        Read fresh per request, never captured: coins are added and removed
        at runtime.
        """
        return list(state.engines)

    def resolve_symbol(symbol: str | None) -> str:
        """Default to the first active symbol; 404 unknown ones."""
        symbols = active_symbols()
        if not symbols:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="no coins are active")
        if symbol is None:
            return symbols[0]
        if symbol not in state.engines:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"unknown symbol {symbol!r}; active: {symbols}",
            )
        return symbol

    async def account_equity() -> Decimal | None:
        """Mark every open position at its latest stored close; ``None`` if any lacks one.

        Marks are gathered for every *active* symbol first, and only then
        is the portfolio read — synchronously, with no awaits in between. An
        await between reading positions and valuing them would let a fill on
        the trading loop open a position the marks don't cover, turning a
        status request into a 500.
        """
        marks: dict[str, Decimal] = {}
        for active in active_symbols():
            candle = await state.candle_store.latest_candle(active, CandleInterval.M1)
            if candle is not None:
                marks[active] = candle.close_quote
        for open_symbol in state.portfolio.positions:
            if open_symbol not in marks:
                return None  # refuse to guess, never wrong
        return state.portfolio.equity_quote(marks)

    app = FastAPI(title="tradebot control plane")
    # The dashboard is served from a different origin than the API (two
    # Railway services), so without these headers the browser blocks every
    # request before it leaves the page. Credentials stay off: auth is a
    # bearer header the page attaches itself, never a cookie the browser
    # would attach for an attacker. With credentials off, wildcard methods
    # and headers are safe — and they keep preflights working when the
    # frontend grows headers (tracing, monitoring) or the API grows verbs;
    # the real gate is the bearer token, not the preflight.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=_parse_cors_origins(state.config.api_cors_origins),
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    protected = APIRouter(dependencies=[Depends(require_token)])

    @app.get("/health")
    async def health() -> dict[str, str]:
        """Public liveness probe: platform healthchecks cannot send tokens.

        Minimal by design: even mode and symbol are operational details an
        unauthenticated scanner has no business learning. Everything real
        stays behind the bearer token.
        """
        return {"status": "ok"}

    @protected.get("/wallet")
    async def get_wallet() -> WalletResponse:
        """Report holdings: how much quote sits free, how much of each coin.

        Marks are gathered first, then the portfolio is read synchronously
        (no awaits in between) — the same race discipline as
        ``account_equity``. A position whose mark is missing is listed with
        unknown value rather than dropped: hiding a holding would be worse
        than not pricing it.
        """
        marks: dict[str, Decimal] = {}
        for active in active_symbols():
            candle = await state.candle_store.latest_candle(active, CandleInterval.M1)
            if candle is not None:
                marks[active] = candle.close_quote
        portfolio = state.portfolio
        quote = state.config.quote_currency
        holdings = [
            HoldingResponse(
                asset=quote,
                symbol=None,
                quantity=str(portfolio.quote_balance),
                mark_price_quote=None,
                value_quote=str(portfolio.quote_balance),
                unrealized_pnl_quote=None,
            )
        ]
        all_marked = True
        for held_symbol, position in portfolio.positions.items():
            mark = marks.get(held_symbol)
            if mark is None:
                all_marked = False
            holdings.append(
                HoldingResponse(
                    asset=held_symbol.split("/")[0],
                    symbol=held_symbol,
                    quantity=str(position.quantity_base),
                    mark_price_quote=str(mark) if mark is not None else None,
                    value_quote=(str(position.quantity_base * mark) if mark is not None else None),
                    unrealized_pnl_quote=(
                        str(position.unrealized_pnl_quote(mark)) if mark is not None else None
                    ),
                )
            )
        return WalletResponse(
            quote_currency=quote,
            equity_quote=str(portfolio.equity_quote(marks)) if all_marked else None,
            holdings=holdings,
        )

    @protected.get("/status")
    async def get_status(symbol: str | None = Query(None)) -> StatusResponse:
        portfolio = state.portfolio
        selected = resolve_symbol(symbol)
        engine = state.engines[selected]
        latest = await state.candle_store.latest_candle(selected, CandleInterval.M1)
        position = portfolio.position(selected)

        mark_price = latest.close_quote if latest is not None else None
        # Account-wide equity: every open position (any symbol) marked at
        # its latest stored close.
        equity = await account_equity()
        position_response = None
        if position is not None:
            position_response = PositionResponse(
                symbol=position.symbol,
                quantity_base=str(position.quantity_base),
                average_entry_price_quote=str(position.average_entry_price_quote),
                unrealized_pnl_quote=(
                    str(position.unrealized_pnl_quote(mark_price))
                    if mark_price is not None
                    else None
                ),
            )
        breakers = engine.breakers  # one shared account-level instance
        detector = state.regime_detector
        regime = (
            RegimeResponse(
                enabled=False,
                symbol=None,
                label=None,
                reasons=[],
                reason=state.regime_disabled_reason,
            )
            if detector is None
            else RegimeResponse(
                enabled=True,
                symbol=detector.symbol,
                label=detector.regime.label,
                reasons=list(detector.regime.reasons),
            )
        )
        health = state.feed_health(selected)
        data_health = DataHealthResponse(
            healthy=health.healthy if health is not None else False,
            reason=(health.health_reason if health is not None else "no feed for this coin"),
        )
        return StatusResponse(
            mode=state.config.mode.value,
            paused=engine.paused,
            protective_stop_quote=engine.protective_stop_quote,
            regime=regime,
            data_health=data_health,
            breakers=BreakersResponse(
                tripped_reason=breakers.tripped_reason,
                cooldown_until=(
                    breakers.cooldown_until.isoformat()
                    if breakers.cooldown_until is not None
                    else None
                ),
                entries_today=breakers.entries_today,
            ),
            symbol=selected,
            symbols=active_symbols(),
            exchange_id=state.config.exchange_id,
            quote_currency=state.config.quote_currency,
            quote_balance=str(portfolio.quote_balance),
            realized_pnl_quote=str(portfolio.realized_pnl_quote()),
            position=position_response,
            last_candle_close_time=(latest.close_time.isoformat() if latest is not None else None),
            mark_price_quote=str(mark_price) if mark_price is not None else None,
            equity_quote=str(equity) if equity is not None else None,
        )

    @protected.get("/competition")
    async def get_competition() -> CompetitionResponse:
        """Rank every competition paper account by equity (the leaderboard).

        With the competition disabled the lineup degrades to the production
        bot alone — an honest one-row leaderboard, never a 404 the UI has
        to special-case.
        """
        rows = await state.competition_snapshot()
        return CompetitionResponse(
            quote_currency=state.config.quote_currency,
            competitors=[_competitor_response(row) for row in rows],
        )

    @protected.get("/bots/options")
    async def get_bot_builder_options() -> BotBuilderOptionsResponse:
        """Render the bot builder's choices from the real strategy registry.

        Declared before ``/bots/{bot_id}`` so the literal segment matches
        as a path. Defaults come straight from the config models — the UI
        never hardcodes a parameter that could drift.
        """
        labels = {spec.family: spec.label for spec in LINEUP if spec.family is not None}
        families = []
        for family, (config_model, _constructor) in STRATEGY_FAMILIES.items():
            families.append(
                RuleOptionResponse(
                    family=family,
                    label=labels.get(family, family.replace("_", " ").capitalize()),
                    description=FAMILY_DESCRIPTIONS.get(family, ""),
                    defaults=config_model().model_dump(mode="json"),
                )
            )
        return BotBuilderOptionsResponse(families=families, entry_modes=list(ENTRY_MODES))

    @protected.get("/bots/{bot_id}")
    async def get_bot_detail(bot_id: str) -> BotDetailResponse:
        """One bot's full picture: summary, positions, and what it trades."""
        try:
            detail = await state.bot_detail(bot_id)
        except KeyError as error:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error)) from error
        return BotDetailResponse(
            summary=_competitor_response(detail),
            positions=[
                BotPositionResponse(
                    symbol=position["symbol"],
                    quantity_base=str(position["quantity_base"]),
                    average_entry_price_quote=str(position["average_entry_price_quote"]),
                    mark_price_quote=_optional_str(position["mark_price_quote"]),
                    unrealized_pnl_quote=_optional_str(position["unrealized_pnl_quote"]),
                )
                for position in detail["positions"]
            ],
            strategy=detail["strategy"],
        )

    @protected.post("/bots")
    async def create_bot(request: CreateBotRequest) -> CreateBotResponse:
        """Create a custom bot from the builder's recipe and start it."""
        try:
            bot_id = await state.create_custom_bot(request.name, request.description, request.rules)
        except ValueError as error:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail=str(error)
            ) from error
        except RuntimeError as error:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error)) from error
        return CreateBotResponse(
            bot_id=bot_id, detail=f"{request.name.strip()} joined the competition"
        )

    @protected.put("/bots/{bot_id}/rules")
    async def update_bot_rules(bot_id: str, request: UpdateBotRulesRequest) -> CommandResponse:
        """Replace a custom bot's recipe; its position and history stay."""
        try:
            await state.update_custom_bot(bot_id, request.rules)
        except KeyError as error:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error)) from error
        except ValueError as error:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail=str(error)
            ) from error
        return CommandResponse(paused=False, detail=f"{bot_id} now trades the new rules")

    @protected.put("/bots/{bot_id}/capital")
    async def reset_bot_capital(bot_id: str, request: ResetBotCapitalRequest) -> CommandResponse:
        """Reset a bot's account to a new starting capital (must be flat first).

        Destructive: the bot's fills/orders/decisions are purged and it
        restarts clean. 400 for a non-positive amount, 404 unknown, 409 while
        the bot holds a position, has open orders, or has pending proposals.
        """
        try:
            await state.reset_bot_capital(bot_id, request.initial_balance_quote)
        except KeyError as error:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error)) from error
        except ValueError as error:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail=str(error)
            ) from error
        except RuntimeError as error:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error)) from error
        return CommandResponse(
            paused=False,
            detail=f"{bot_id} reset to {request.initial_balance_quote} starting capital",
        )

    @protected.post("/bots/{bot_id}/pause")
    async def pause_bot(bot_id: str) -> CommandResponse:
        """Mute one bot's entries; its protective stops keep running."""
        try:
            await state.pause_bot(bot_id)
        except KeyError as error:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error)) from error
        return CommandResponse(paused=True, detail=f"{bot_id} paused; resting orders stay live")

    @protected.post("/bots/{bot_id}/resume")
    async def resume_bot(bot_id: str) -> CommandResponse:
        """Un-mute one bot's entries."""
        try:
            await state.resume_bot(bot_id)
        except KeyError as error:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error)) from error
        return CommandResponse(paused=False, detail=f"{bot_id} resumed")

    @protected.post("/bots/{bot_id}/kill")
    async def kill_bot(bot_id: str) -> CommandResponse:
        """Halt one bot and flatten its positions at market."""
        try:
            exits_submitted, failures = await state.kill_bot(bot_id)
        except KeyError as error:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error)) from error
        if failures:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    f"{bot_id} halted with failures ({exits_submitted} exit order(s) "
                    "submitted): " + "; ".join(failures)
                ),
            )
        plural = "s" if exits_submitted != 1 else ""
        detail = (
            f"{bot_id} halted; {exits_submitted} exit order{plural} submitted, fills on next candle"
            if exits_submitted
            else f"{bot_id} halted; no position to flatten"
        )
        return CommandResponse(paused=True, detail=detail)

    @protected.delete("/bots/{bot_id}")
    async def delete_bot(bot_id: str) -> CommandResponse:
        """Retire a custom bot; its trade history stays queryable."""
        try:
            await state.delete_custom_bot(bot_id)
        except KeyError as error:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error)) from error
        except ValueError as error:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail=str(error)
            ) from error
        except RuntimeError as error:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error)) from error
        return CommandResponse(paused=False, detail=f"{bot_id} retired; its history stays")

    def _fees_response(buy_fee_bps: Decimal, sell_fee_bps: Decimal) -> TradingFeesResponse:
        # Percent is bps / 100, exact in Decimal (10 bps -> "0.1"). Trailing
        # zeros are trimmed without scientific notation ("20.0" -> "20") so
        # the UI shows a clean number.
        def clean(value: Decimal) -> str:
            normalized = value.normalize()
            return f"{normalized:f}"

        return TradingFeesResponse(
            buy_fee_percent=clean(buy_fee_bps / 100),
            sell_fee_percent=clean(sell_fee_bps / 100),
            buy_fee_bps=clean(buy_fee_bps),
            sell_fee_bps=clean(sell_fee_bps),
        )

    @protected.get("/settings/fees")
    async def get_trading_fees() -> TradingFeesResponse:
        """Return the buy/sell trading fees applied to every live paper fill."""
        fees = state.trading_fees()
        return _fees_response(fees["buy_fee_bps"], fees["sell_fee_bps"])

    @protected.put("/settings/fees")
    async def update_trading_fees(request: UpdateTradingFeesRequest) -> TradingFeesResponse:
        """Set the buy/sell fees; effective on the next fill across every bot."""
        buy_fee_bps = request.buy_fee_percent * 100
        sell_fee_bps = request.sell_fee_percent * 100
        try:
            await state.update_trading_fees(buy_fee_bps=buy_fee_bps, sell_fee_bps=sell_fee_bps)
        except ValueError as error:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail=str(error)
            ) from error
        return _fees_response(buy_fee_bps, sell_fee_bps)

    @protected.get("/settings/campaign")
    async def get_campaign_settings() -> CampaignSettingsResponse:
        """Return the §12.7 campaign loop's on/off and budget for the Settings tab."""
        raw = state.campaign_status()
        return CampaignSettingsResponse(
            enabled=raw["enabled"],
            max_rounds=raw["max_rounds"],
            max_hours=raw["max_hours"],
            timeframe=raw["timeframe"],
        )

    @protected.put("/settings/campaign")
    async def update_campaign_settings(
        request: UpdateCampaignSettingsRequest,
    ) -> CampaignSettingsResponse:
        """Turn the campaign loop on or off; effective within one cooldown, no restart."""
        await state.update_campaign_enabled(enabled=request.enabled)
        raw = state.campaign_status()
        return CampaignSettingsResponse(
            enabled=raw["enabled"],
            max_rounds=raw["max_rounds"],
            max_hours=raw["max_hours"],
            timeframe=raw["timeframe"],
        )

    @protected.get("/coins/{symbol:path}/divergence")
    async def get_divergence(
        symbol: str, hours: int = Query(24, ge=1, le=24 * 90)
    ) -> DivergenceReport:
        """Live-vs-replay divergence for one coin: the §10 paper-gate metric.

        Zero means the one-code-path invariant is holding; sustained
        non-zero either has a journaled explanation (gates, pauses,
        co-pilot) or is a parity bug. Recomputed per request — it replays
        the window — so callers should poll it sparingly (a dashboard
        tile, not a tick stream).
        """
        try:
            return await state.divergence_report(symbol, window_hours=hours)
        except KeyError:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail=f"{symbol} is not being traded"
            ) from None

    @protected.post("/pause")
    async def pause() -> CommandResponse:
        # Whole-bot commands on purpose: pause/resume/kill are operator
        # actions, and "I paused it" must never mean "except that symbol"
        # — or "except the competition accounts".
        for engine in state.all_engines():
            engine.pause()
        await state.persist_risk_state()
        return CommandResponse(paused=True, detail="strategies muted; resting orders stay live")

    @protected.post("/resume")
    async def resume() -> CommandResponse:
        for engine in state.all_engines():
            engine.resume()
        await state.persist_risk_state()
        return CommandResponse(paused=False, detail="strategies resumed")

    @protected.post("/kill")
    async def kill() -> CommandResponse:
        exits_submitted = 0
        failures: list[str] = []
        # Kill every engine — every symbol, every competition account —
        # before reporting any failure: one symbol's unpriceable exit must
        # not leave the others trading.
        for engine in state.all_engines():
            try:
                if await engine.kill():
                    exits_submitted += 1
            except RuntimeError as error:
                failures.append(str(error))
        # Persist the halt before reporting it — even on partial failure,
        # "halted" must survive a crash the moment the operator sees it.
        await state.persist_risk_state()
        if failures:
            # Halted but NOT flat — surface it as a clear conflict, never as
            # a 500 and never as a misleading "nothing to flatten". The
            # successes are reported too: "it failed" must not hide that
            # some exits *were* submitted.
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    f"halted with failures ({exits_submitted} exit order(s) submitted): "
                    + "; ".join(failures)
                ),
            )
        plural = "s" if exits_submitted != 1 else ""
        detail = (
            f"halted; {exits_submitted} exit order{plural} submitted, fills on next candle"
            if exits_submitted
            else "halted; no position to flatten"
        )
        return CommandResponse(paused=True, detail=detail)

    @protected.post("/coins")
    async def add_coin(request: CoinActionRequest) -> CommandResponse:
        """Start trading a coin at runtime; persists across restarts."""
        try:
            await state.add_coin(request.symbol)
        except ValueError as error:
            # Bad pair, wrong quote currency, duplicate, or unlisted on the
            # exchange — the caller's input, so 400, with the reason verbatim.
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail=str(error)
            ) from error
        first_engine = next(iter(state.engines.values()))
        return CommandResponse(paused=first_engine.paused, detail=f"{request.symbol.strip()} added")

    @protected.post("/coins/remove")
    async def remove_coin(request: CoinActionRequest) -> CommandResponse:
        """Stop trading a coin; its candles, fills, and decisions stay queryable."""
        try:
            await state.remove_coin(request.symbol)
        except KeyError as error:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error)) from error
        except RuntimeError as error:
            # Open position, pending proposal, or last coin: truthful
            # conflict — the operator must act first, not be silently obeyed.
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error)) from error
        first_engine = next(iter(state.engines.values()))
        return CommandResponse(
            paused=first_engine.paused, detail=f"{request.symbol.strip()} removed"
        )

    @protected.post("/breakers/reset")
    async def reset_breakers() -> CommandResponse:
        """Clear a tripped circuit breaker — the explicit human reset.

        Deliberately does not resume a paused engine or forget the equity
        peak: it re-permits entries, nothing more. The breakers are one
        account-level instance shared by every engine, so resetting through
        any engine resets them all.
        """
        first_engine = next(iter(state.engines.values()))
        first_engine.reset_breakers()
        return CommandResponse(paused=first_engine.paused, detail="circuit breakers reset")

    def engine_for_proposal(signal_id: str) -> TradingEngine:
        """Route a proposal action to the engine whose queue knows the id."""
        for engine in state.engines.values():
            if engine.has_proposal(signal_id):
                return engine
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"no pending proposal {signal_id!r}",
        )

    @protected.get("/proposals")
    async def get_proposals() -> list[ProposalResponse]:
        return [
            ProposalResponse(
                signal_id=proposal.signal.signal_id,
                symbol=proposal.signal.symbol,
                side=proposal.signal.side.value,
                strategy_name=proposal.signal.strategy_name,
                proposal_price_quote=str(proposal.proposal_price_quote),
                stop_price_quote=str(proposal.signal.stop_price_quote),
                reasons=list(proposal.signal.reasons),
                created_at=proposal.created_at.isoformat(),
                expires_at=proposal.expires_at.isoformat(),
            )
            for engine in state.engines.values()
            for proposal in engine.pending_proposals()
        ]

    @protected.post("/proposals/approve")
    async def approve_proposal(request: ProposalActionRequest) -> CommandResponse:
        engine = engine_for_proposal(request.signal_id)
        try:
            detail = await engine.approve_proposal(request.signal_id)
        except KeyError as error:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error)) from error
        except ValueError as error:
            # Expired or drifted: the yes was given to a market that no
            # longer exists, so the approval is refused — loudly.
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error)) from error
        return CommandResponse(paused=engine.paused, detail=detail)

    @protected.post("/proposals/reject")
    async def reject_proposal(request: ProposalActionRequest) -> CommandResponse:
        engine = engine_for_proposal(request.signal_id)
        try:
            await engine.reject_proposal(request.signal_id)
        except KeyError as error:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error)) from error
        except ValueError as error:
            # Already resolved (expired/drifted/answered): truthful conflict,
            # not a 500 and not a misleading "not found".
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error)) from error
        return CommandResponse(paused=engine.paused, detail="proposal rejected")

    @protected.get("/candles")
    async def get_candles(
        limit: int = Query(300, ge=1, le=1000),
        symbol: str | None = Query(None),
        interval: str = Query("1m"),
    ) -> list[CandleResponse]:
        """Chart candles: raw 1m, or calendar buckets aggregated in SQL."""
        if interval == CandleInterval.M1.value:
            candles: Sequence[Candle | ChartCandle] = await state.candle_store.fetch_recent(
                resolve_symbol(symbol), CandleInterval.M1, limit
            )
        elif interval in CHART_BUCKET_UNITS:
            candles = await state.candle_store.fetch_recent_buckets(
                resolve_symbol(symbol), CHART_BUCKET_UNITS[interval], limit
            )
        else:
            supported = [CandleInterval.M1.value, *CHART_BUCKET_UNITS]
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"unknown chart interval {interval!r}; supported: {supported}",
            )
        return [_candle_response(candle) for candle in candles]

    @protected.get("/decisions")
    async def get_decisions(
        limit: int = Query(50, ge=1, le=200),
        symbol: str | None = Query(None),
        bot: str | None = Query(None),
    ) -> list[DecisionResponse]:
        store = state.decision_store
        if bot is not None:
            try:
                store = state.decision_store_for(bot)
            except KeyError as error:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND, detail=str(error)
                ) from error
        decisions = await store.fetch_recent(resolve_symbol(symbol), limit)
        return [
            DecisionResponse(
                signal_id=decision.signal_id,
                strategy_name=decision.strategy_name,
                symbol=decision.symbol,
                side=decision.side.value,
                stop_price_quote=str(decision.stop_price_quote),
                reasons=list(decision.reasons),
                outcome=decision.outcome.value,
                created_at=decision.created_at.isoformat(),
            )
            for decision in decisions
        ]

    @protected.post("/evaluations")
    async def start_evaluation(request: EvaluationStartRequest) -> EvaluationStartResponse:
        """Start a blind walk-forward evaluation run (one at a time)."""
        config = EvaluationRunConfig(
            symbols=tuple(request.symbols) if request.symbols else tuple(active_symbols()),
            timeframes=tuple(request.timeframes),
            history_days=request.history_days,
            scenario_count=request.scenario_count,
            lookback_candles=request.lookback_candles,
            horizon_candles=request.horizon_candles,
            seed=request.seed,
            strategy=request.strategy,
        )
        try:
            run_id = await state.start_evaluation(config)
        except RuntimeError as error:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error)) from error
        except ValueError as error:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail=str(error)
            ) from error
        return EvaluationStartResponse(run_id=run_id, detail="evaluation started")

    @protected.get("/evaluations")
    async def list_evaluations() -> list[EvaluationRunResponse]:
        return [_run_response(run) for run in await state.evaluation_store.list_runs()]

    @protected.get("/evaluations/strategies")
    async def list_evaluation_strategies() -> list[EvaluationStrategyResponse]:
        """Every bot a run can grade — the research screen's bot selector.

        Declared before ``/evaluations/{run_id}`` so the literal segment is
        matched as a path, not parsed as a run id.
        """
        return [EvaluationStrategyResponse(**entry) for entry in state.evaluation_strategies()]

    @protected.get("/improvement")
    async def get_improvement_status() -> ImprovementStatusResponse:
        """Report the §12.7 self-improvement loop: schedule and last outcome.

        Always answers — when the loop is disabled the schedule still
        reports with the cycle fields null, so the dashboard can say "off"
        instead of guessing.
        """
        raw = state.improvement_status()
        return ImprovementStatusResponse(
            enabled=raw["enabled"],
            interval_hours=raw["interval_hours"],
            history_days=raw["history_days"],
            timeframe=raw["timeframe"],
            last_cycle_started_at=_optional_iso(raw["last_cycle_started_at"]),
            last_cycle_finished_at=_optional_iso(raw["last_cycle_finished_at"]),
            last_outcome=raw["last_outcome"],
            next_cycle_at=_optional_iso(raw["next_cycle_at"]),
        )

    @protected.get("/campaign")
    async def get_campaign_status() -> CampaignStatusResponse:
        """Report the §12.7 campaign loop: budget and the current/last campaign.

        Always answers — disabled or idle reports with ``campaign`` null, so
        the dashboard says "off" instead of guessing.
        """
        raw = state.campaign_status()
        snapshot = raw["campaign"]
        campaign = _campaign_snapshot_response(snapshot) if snapshot is not None else None
        return CampaignStatusResponse(
            enabled=raw["enabled"],
            max_rounds=raw["max_rounds"],
            max_hours=raw["max_hours"],
            timeframe=raw["timeframe"],
            campaign=campaign,
        )

    @protected.get("/campaign/history")
    async def get_campaign_history(
        limit: int = Query(default=20, ge=1, le=100),
    ) -> list[CampaignSnapshotResponse]:
        """Past finished campaigns, newest first — the durable §12.7 record.

        Each is the same snapshot shape ``GET /campaign`` reports for the
        live one (round trail with per-promotion diffs, holdout read), so the
        dashboard renders history and current alike. The in-memory driver only
        holds the current campaign; these survive restarts.
        """
        snapshots = await state.campaign_history(limit)
        return [_campaign_snapshot_response(snapshot) for snapshot in snapshots]

    @protected.get("/evaluations/suggestions")
    async def list_evaluation_suggestions() -> list[SuggestedEvaluationResponse]:
        """Three fitted run shapes per coin, each the biggest sample its rung allows.

        Declared before ``/evaluations/{run_id}`` so the literal segment is
        matched as a path, not parsed as a run id.
        """
        suggestions = await build_suggestions(state.candle_store, active_symbols())
        return [
            SuggestedEvaluationResponse(**suggestion.model_dump()) for suggestion in suggestions
        ]

    @protected.post("/evaluations/compare")
    async def start_comparison(request: EvaluationStartRequest) -> ComparisonStartResponse:
        """Grade every competition strategy on identical scenarios.

        One run per lineup entry over one frozen window and seed — the
        research counterpart of the live leaderboard.
        """
        config = EvaluationRunConfig(
            symbols=tuple(request.symbols) if request.symbols else tuple(active_symbols()),
            timeframes=tuple(request.timeframes),
            history_days=request.history_days,
            scenario_count=request.scenario_count,
            lookback_candles=request.lookback_candles,
            horizon_candles=request.horizon_candles,
            seed=request.seed,
        )
        try:
            run_ids = await state.start_comparison(config)
        except RuntimeError as error:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error)) from error
        except ValueError as error:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail=str(error)
            ) from error
        return ComparisonStartResponse(
            group_id=run_ids[0],
            run_ids=run_ids,
            detail=f"comparison started: {len(run_ids)} strategies, identical scenarios",
        )

    @protected.get("/evaluations/comparisons")
    async def list_comparisons() -> list[ComparisonResponse]:
        """Recent comparison batches, newest first, runs in lineup order.

        Declared before ``/evaluations/{run_id}`` so the literal segment is
        matched as a path, not parsed as a run id.
        """
        return [
            ComparisonResponse(
                group_id=batch[0]["comparison_group"],
                created_at=batch[0]["created_at"].isoformat(),
                runs=[_run_response(run) for run in batch],
            )
            for batch in await state.evaluation_store.list_comparisons()
        ]

    @protected.get("/research/candidacy")
    async def get_routing_candidacy() -> list[RoutingCandidacyResponse]:
        """Grade the §13.7 routing-evidence gate per research family.

        For each research family (breakout, momentum, squeeze) reports the
        three conditions — a validated edge in a named regime, beating the
        incumbent across comparison batches weeks apart, and an eight-week
        positive live-paper soak — and whether all three are met. It only
        flags candidacy; routing a family into the production router stays a
        human decision (§13.7).
        """
        candidacies = await state.routing_candidacies()
        return [_candidacy_response(candidacy) for candidacy in candidacies]

    @protected.post("/research/bakeoff")
    async def start_bake_off(request: BakeOffStartRequest) -> BakeOffStartResponse:
        """Run the contestant roster across the grid and rank by money made.

        Fully automated: one call grades every energy preset (plus the live
        bot as a baseline) across every (timeframe, history-window) cell,
        persists each cell as a comparison, and accumulates a leaderboard.
        """
        try:
            # Build inside the try: the request's loose ints (scenario_count,
            # seed) and an empty symbol set fail BakeOffConfig's own
            # constraints, and that is a 400 to report, not a 500 to leak.
            config = BakeOffConfig(
                symbols=tuple(request.symbols) if request.symbols else tuple(active_symbols()),
                grid=tuple(
                    (timeframe, tuple(windows)) for timeframe, windows in request.grid.items()
                ),
                scenario_count=request.scenario_count,
                seed=request.seed,
            )
            job_id = await state.start_bake_off(config)
        except RuntimeError as error:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error)) from error
        except (ValidationError, ValueError) as error:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail=str(error)
            ) from error
        cells_total = sum(len(windows) for _, windows in config.grid)
        return BakeOffStartResponse(
            job_id=job_id,
            cells_total=cells_total,
            detail=f"bake-off started: {cells_total} cells",
        )

    @protected.get("/research/bakeoffs")
    async def list_bake_offs() -> list[BakeOffJobResponse]:
        """Recent bake-off jobs, newest first.

        Declared before ``/research/bakeoff/{job_id}`` so the literal plural
        segment is matched as a path, not parsed as a job id.
        """
        return [_bake_off_response(job) for job in await state.list_bake_offs()]

    @protected.get("/research/bakeoff/{job_id}")
    async def get_bake_off(job_id: int) -> BakeOffJobResponse:
        job = await state.bake_off(job_id)
        if job is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail=f"no bake-off job {job_id}"
            )
        return _bake_off_response(job)

    @protected.get("/evaluations/{run_id}")
    async def get_evaluation(run_id: int) -> EvaluationRunResponse:
        run = await state.evaluation_store.fetch_run(run_id)
        if run is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail=f"no evaluation run {run_id}"
            )
        return _run_response(run)

    @protected.post("/evaluations/{run_id}/advise")
    async def advise_evaluation(run_id: int) -> ResearchAdviceResponse:
        """Ask the AI advisor (§12.9) to diagnose a run and propose experiments.

        Advisory only: the response is a recommendation a human may act on by
        arming a sweep from a hypothesis — it never changes the strategy, never
        places an order, and never feeds the deterministic backtest. Returns
        ``available=false`` (a normal 200, not an error) whenever the advisor is
        disabled or could not produce advice, so a fail-safe feature never turns
        a research click into a failed request.
        """
        run = await state.evaluation_store.fetch_run(run_id)
        if run is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail=f"no evaluation run {run_id}"
            )
        summary = run.get("summary")
        if not isinstance(summary, dict):
            # Advising an unfinished run would be advising on nothing.
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"evaluation run {run_id} has no report yet",
            )
        findings = await state.evaluation_store.fetch_findings(run_id)
        finding_inputs = [
            {
                "pattern": finding.pattern,
                "suggestion": finding.suggestion,
                "affected_count": finding.affected_count,
                # Stringified for the prompt; the advisor does no money math.
                "average_r_impact": str(finding.average_r_impact),
                "confidence": finding.confidence,
            }
            for _, finding in findings
        ]
        advice = await synthesize_advice(
            report=summary,
            findings=finding_inputs,
            enabled=state.config.ai_advisor_enabled,
            model=state.config.ai_advisor_model,
            max_tokens=state.config.ai_advisor_max_tokens,
            timeout_seconds=state.config.ai_advisor_timeout_seconds,
        )
        return ResearchAdviceResponse(available=advice is not None, advice=advice)

    @protected.get("/evaluations/{run_id}/scenarios")
    async def list_evaluation_scenarios(run_id: int) -> list[ScenarioSummaryResponse]:
        """List the run's graded scenarios for the replay browser."""
        if await state.evaluation_store.fetch_run(run_id) is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail=f"no evaluation run {run_id}"
            )
        rows = await state.evaluation_store.list_scenarios_with_results(run_id)
        return [_scenario_summary(row) for row in rows]

    @protected.get("/evaluations/scenarios/{scenario_id}")
    async def get_scenario_replay(scenario_id: int) -> ScenarioReplayResponse:
        """One scenario's blind window, revealed horizon, decision, and grade.

        Candles are rebuilt from the candle store through the same
        aggregation path the run used — scenarios reference candles, they
        never copy them (ARCHITECTURE.md section 12.4).
        """
        row = await state.evaluation_store.fetch_scenario_with_result(scenario_id)
        if row is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"no graded scenario {scenario_id}",
            )
        run = await state.evaluation_store.fetch_run(row["run_id"])
        assert run is not None  # scenarios carry a foreign key to their run
        # The horizon length lives only in the run's config snapshot — it is
        # a run-level constant, not a per-scenario column.
        horizon_candles = int(run["config"]["horizon_candles"])
        window, horizon = await load_replay(
            state.candle_store,
            symbol=row["symbol"],
            timeframe=row["timeframe"],
            decision_time=row["decision_time"],
            lookback_candles=row["lookback_candles"],
            horizon_candles=horizon_candles,
        )
        return ScenarioReplayResponse(
            scenario=_scenario_summary(row),
            confidence=float(row["confidence"]) if row["confidence"] is not None else None,
            reasons=list(row["reasons"]),
            entry_price_quote=_optional_str(row["entry_price_quote"]),
            exit_price_quote=_optional_str(row["exit_price_quote"]),
            pnl_quote=_optional_str(row["pnl_quote"]),
            mfe_r=_optional_str(row["mfe_r"]),
            mae_r=_optional_str(row["mae_r"]),
            duration_candles=row["duration_candles"],
            stop_hit=row["stop_hit"],
            oracle_r=_optional_str(row["oracle_r"]),
            window=[_candle_response(candle) for candle in window],
            horizon=[_candle_response(candle) for candle in horizon],
        )

    async def finding_recurrence(run: dict[str, Any]) -> dict[str, tuple[int, int]]:
        """Map pattern -> (prior-run count, first-seen run id) for ``run``'s bot.

        Recurrence is the pattern text itself — deterministic per miner
        (§12.2) — counted across earlier completed runs of the same
        strategy within the timeline's bounded window. Patterns are mined
        at most once per run, so occurrences equal runs.
        """
        prior_ids = [
            other["id"]
            for other in await state.evaluation_store.list_runs(TIMELINE_WINDOW)
            if other["strategy"] == run["strategy"]
            and other["status"] == RunStatus.COMPLETED.value
            and other["id"] < run["id"]
        ]
        findings_by_run = await state.evaluation_store.fetch_findings_for_runs(prior_ids)
        history: dict[str, tuple[int, int]] = {}
        for prior_run_id in sorted(findings_by_run):
            for _, prior in findings_by_run[prior_run_id]:
                count, first = history.get(prior.pattern, (0, prior_run_id))
                history[prior.pattern] = (count + 1, first)
        return history

    async def latest_sweep_by_finding() -> dict[int, dict[str, Any]]:
        """Map finding id -> the newest sweep it motivated (bounded window).

        The chain a finding card renders — accepted, swept in #N, verdict —
        is read off the sweeps' recorded motivation, never stored twice.
        """
        latest: dict[int, dict[str, Any]] = {}
        for sweep in await state.evaluation_store.list_sweeps(TIMELINE_WINDOW):
            for finding_id in sweep.get("motivating_finding_ids") or []:
                latest.setdefault(finding_id, sweep)  # list is newest first
        return latest

    @protected.get("/evaluations/{run_id}/findings")
    async def list_evaluation_findings(run_id: int) -> list[FindingResponse]:
        """List the run's mined mistake patterns, each awaiting accept/reject."""
        run = await state.evaluation_store.fetch_run(run_id)
        if run is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail=f"no evaluation run {run_id}"
            )
        findings = await state.evaluation_store.fetch_findings(run_id)
        history = await finding_recurrence(run)
        sweeps_by_finding = await latest_sweep_by_finding()
        queued = state.accept_sweep_pending(run_id)
        responses = []
        for finding_id, finding in findings:
            seen, first = history.get(finding.pattern, (0, 0))
            responses.append(
                _finding_response(
                    finding_id,
                    finding,
                    seen_in_prior_runs=seen,
                    first_seen_run_id=first if seen > 0 else None,
                    sweep_queued=queued and finding.status == "accepted",
                    latest_sweep=sweeps_by_finding.get(finding_id),
                )
            )
        return responses

    async def decide_finding(finding_id: int, verdict: str) -> FindingResponse:
        """Apply the human verdict; first answer wins, repeats are conflicts."""
        finding = await state.evaluation_store.fetch_finding(finding_id)
        if finding is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail=f"no finding {finding_id}"
            )
        if finding.status != "proposed":
            # The verdict is part of the run's lineage (§12.5); silently
            # flipping it would rewrite history, so repeats are refused.
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"finding {finding_id} is already {finding.status}",
            )
        await state.evaluation_store.set_finding_status(finding_id, verdict)
        if verdict == "accepted":
            # The verdict becomes a test: arm (or ride) the run's coalesced
            # targeted sweep. Recording always precedes scheduling, so a
            # crash between the two loses only the trigger — the scheduled
            # cycle reads the recorded status and remains the backstop.
            state.note_finding_acceptance(finding.run_id)
        decided = finding.model_copy(update={"status": verdict})
        # The response replaces the finding card in place, so it must carry
        # the same annotations the list response did — a verdict click must
        # not visually reset a recurred pattern to "new" or drop the chain.
        run = await state.evaluation_store.fetch_run(finding.run_id)
        history = await finding_recurrence(run) if run is not None else {}
        seen, first = history.get(finding.pattern, (0, 0))
        sweeps_by_finding = await latest_sweep_by_finding()
        return _finding_response(
            finding_id,
            decided,
            seen_in_prior_runs=seen,
            first_seen_run_id=first if seen > 0 else None,
            sweep_queued=(
                decided.status == "accepted" and state.accept_sweep_pending(finding.run_id)
            ),
            latest_sweep=sweeps_by_finding.get(finding_id),
        )

    @protected.get("/research/timeline")
    async def research_timeline(
        limit: int = Query(default=50, ge=1, le=200),
    ) -> list[TimelineEventResponse]:
        """Serve the §12.8 research story: runs, sweeps, promotions, one feed."""
        events = await build_timeline(
            state.evaluation_store, state.strategy_settings_store, limit=limit
        )
        return [
            TimelineEventResponse(
                at=event.at.isoformat(),
                kind=event.kind,
                headline=event.headline,
                detail=event.detail,
                status=event.status,
                strategy=event.strategy,
                run_id=event.run_id,
                sweep_id=event.sweep_id,
                version_id=event.version_id,
                expectancy_r=event.expectancy_r,
                verdict=event.verdict,
                new_patterns=list(event.new_patterns),
                resolved_patterns=list(event.resolved_patterns),
                changes=[
                    SettingChangeResponse(
                        field=change.field, before=change.before, after=change.after
                    )
                    for change in event.changes
                ],
            )
            for event in events
        ]

    @protected.post("/evaluations/findings/{finding_id}/accept")
    async def accept_finding(finding_id: int) -> FindingResponse:
        """Accept a finding — the human judgement, recorded for lineage.

        Accepting records the judgement and nothing else: strategy
        configuration is never touched by the evaluation system
        (ARCHITECTURE.md section 12).
        """
        return await decide_finding(finding_id, "accepted")

    @protected.post("/evaluations/findings/{finding_id}/reject")
    async def reject_finding(finding_id: int) -> FindingResponse:
        """Reject a finding; it stays on record with its verdict."""
        return await decide_finding(finding_id, "rejected")

    @protected.post("/evaluations/{run_id}/cancel")
    async def cancel_evaluation(run_id: int) -> CommandResponse:
        if not state.cancel_evaluation(run_id):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"evaluation run {run_id} is not in flight",
            )
        first_engine = next(iter(state.engines.values()))
        return CommandResponse(paused=first_engine.paused, detail=f"run {run_id} cancelled")

    async def derive_manual_grid() -> tuple[tuple[SweepCandidate, ...], tuple[int, ...]]:
        """Build the "run sweep" button's grid from the newest completed run.

        Same curation as the automated cycle (§12.7): accepted findings
        outrank proposed ones, rejected findings never steer anything —
        and the grid matches the bot that run graded, so the button tests
        the knobs the findings on screen point at. A custom-bot run sweeps
        variants of its whole recipe; a built-in run sweeps its family
        grid. When the newest run graded a bot whose grid cannot be built,
        the button falls back to challenging the production configuration.
        """
        for run in await state.evaluation_store.list_runs(limit=10):
            if run.get("status") != RunStatus.COMPLETED.value:
                continue
            findings = select_targeting_findings(
                await state.evaluation_store.fetch_findings(run["id"])
            )
            target = str(run.get("strategy") or "production")
            recipe = state.recipe_for(target)
            try:
                if recipe is not None:
                    return build_recipe_candidates(recipe, findings)
                return build_candidates_for(target, state.strategy_params, findings)
            except ValueError:
                break  # no grid for it — fall back to the production grid
        return build_candidates_for("production", state.strategy_params, [])

    @protected.post("/sweeps")
    async def start_sweep(request: SweepStartRequest) -> EvaluationStartResponse:
        """Start a walk-forward parameter sweep (one at a time)."""
        try:
            motivating = tuple(request.motivating_finding_ids)
            if request.candidates:
                candidates = tuple(
                    SweepCandidate(
                        name=candidate.name, params=candidate.params, family=candidate.family
                    )
                    for candidate in request.candidates
                )
            else:
                # The manual button challenges the configuration actually
                # trading, steered by the latest findings — the same grid
                # the automated improver would sweep, so "run sweep" tests
                # the knobs the findings on screen point at.
                candidates, mined = await derive_manual_grid()
                motivating = tuple(dict.fromkeys((*motivating, *mined)))
            config = SweepConfig(
                symbol=request.symbol if request.symbol else resolve_symbol(None),
                timeframe=request.timeframe,
                history_days=request.history_days,
                scenario_count=request.scenario_count,
                lookback_candles=request.lookback_candles,
                horizon_candles=request.horizon_candles,
                seed=request.seed,
                training_fraction=request.training_fraction,
                validation_windows=request.validation_windows,
                candidates=candidates,
                motivating_finding_ids=motivating,
                # Human-initiated sweeps carry the §10 robustness read; the
                # auto-improver leaves it off to keep its frequent sweeps cheap.
                cost_multipliers=DEFAULT_COST_MULTIPLIERS,
            )
            sweep_id = await state.start_sweep(config)
        except RuntimeError as error:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error)) from error
        except ValueError as error:
            # Bad timeframe, duplicate names, unknown family or parameter,
            # out-of-range split — all caller input (ValidationError is a
            # ValueError).
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail=str(error)
            ) from error
        return EvaluationStartResponse(run_id=sweep_id, detail="sweep started")

    @protected.get("/sweeps")
    async def list_sweeps() -> list[SweepResponse]:
        return [_sweep_response(sweep) for sweep in await state.evaluation_store.list_sweeps()]

    @protected.get("/sweeps/{sweep_id}")
    async def get_sweep(sweep_id: int) -> SweepResponse:
        sweep = await state.evaluation_store.fetch_sweep(sweep_id)
        if sweep is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail=f"no sweep {sweep_id}"
            )
        return _sweep_response(sweep)

    @protected.post("/sweeps/{sweep_id}/cancel")
    async def cancel_sweep(sweep_id: int) -> CommandResponse:
        if not state.cancel_sweep(sweep_id):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"sweep {sweep_id} is not in flight",
            )
        first_engine = next(iter(state.engines.values()))
        return CommandResponse(paused=first_engine.paused, detail=f"sweep {sweep_id} cancelled")

    @protected.get("/strategy/versions")
    async def list_strategy_versions() -> list[StrategyVersionResponse]:
        """List the settings journal: every configuration the bot has traded."""
        return [
            StrategyVersionResponse(
                id=row["id"],
                family=row["family"],
                params=row["params"],
                source_sweep_id=row["source_sweep_id"],
                note=row["note"],
                activated_at=row["activated_at"].isoformat(),
            )
            for row in await state.strategy_settings_store.history()
        ]

    @protected.post("/strategy/versions/{version_id}/revert")
    async def revert_strategy_version(version_id: int) -> CommandResponse:
        """Re-apply a historical version — the human override of §12.7."""
        try:
            new_version = await state.revert_strategy_version(version_id)
        except KeyError as error:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"no strategy settings version {version_id}",
            ) from error
        first_engine = next(iter(state.engines.values()))
        return CommandResponse(
            paused=first_engine.paused,
            detail=f"reverted to version #{version_id} (new version #{new_version})",
        )

    @protected.get("/metrics")
    async def get_metrics() -> PlainTextResponse:
        """Prometheus text exposition (ARCHITECTURE.md 4.9).

        Behind the bearer token on purpose: balances and positions are in
        here, and Prometheus scrapes support bearer auth natively. Floats
        are fine in this one place — metrics are display, not accounting.
        """
        now = utc_now()
        portfolio = state.portfolio
        lines: list[str] = [
            "# TYPE tradebot_up gauge",
            format_metric("tradebot_up", 1),
            "# TYPE tradebot_quote_balance gauge",
            format_metric("tradebot_quote_balance", float(portfolio.quote_balance)),
            "# TYPE tradebot_realized_pnl_quote gauge",
            format_metric("tradebot_realized_pnl_quote", float(portfolio.realized_pnl_quote())),
            "# TYPE tradebot_open_positions gauge",
            format_metric("tradebot_open_positions", len(portfolio.positions)),
        ]
        lines.append("# TYPE tradebot_engine_paused gauge")
        for symbol, engine in state.engines.items():
            lines.append(
                format_metric("tradebot_engine_paused", int(engine.paused), {"symbol": symbol})
            )
        first_engine = next(iter(state.engines.values()), None)
        if first_engine is not None:
            lines.append("# TYPE tradebot_breaker_tripped gauge")
            lines.append(
                format_metric(
                    "tradebot_breaker_tripped",
                    int(first_engine.breakers.tripped_reason is not None),
                )
            )
        # Data-feed lag per symbol: the staleness alarm §4.9 asks for.
        lines.append("# TYPE tradebot_last_candle_age_seconds gauge")
        for symbol in state.engines:
            latest = await state.candle_store.latest_candle(symbol, CandleInterval.M1)
            if latest is not None:
                age = (now - latest.close_time).total_seconds()
                lines.append(
                    format_metric("tradebot_last_candle_age_seconds", age, {"symbol": symbol})
                )
        lines.append("# TYPE tradebot_news_flags_active gauge")
        lines.append(
            format_metric("tradebot_news_flags_active", len(state.news_flags.active_flags(now)))
        )
        lines.extend(state.metrics.render_counters())
        return PlainTextResponse("\n".join(lines) + "\n")

    @protected.get("/fills")
    async def get_fills(
        symbol: str | None = Query(None),
        bot: str | None = Query(None),
        limit: int = Query(200, ge=1, le=1000),
        before_id: int | None = Query(None, ge=1),
    ) -> list[FillResponse]:
        # The journal view spans the production account by default; ``bot``
        # selects a competition account's journal instead. Any symbol may
        # narrow it — including ones no longer configured: fills are
        # history, and history must stay queryable after a coin is removed.
        # Bounded by ``limit`` and paged backward through ``before_id`` (the
        # smallest id already seen), so a years-long journal never loads whole.
        store = state.fill_store
        if bot is not None:
            try:
                store = state.fill_store_for(bot)
            except KeyError as error:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND, detail=str(error)
                ) from error
        page = await store.fetch_page(symbol, limit, before_id)
        return [
            FillResponse(
                id=fill_id,
                client_order_id=fill.client_order_id,
                symbol=fill.symbol,
                side=fill.side.value,
                price_quote=str(fill.price_quote),
                quantity_base=str(fill.quantity_base),
                value_quote=str(fill.price_quote * fill.quantity_base),
                fee_quote=str(fill.fee_quote),
                filled_at=fill.filled_at.isoformat(),
            )
            for fill_id, fill in page
        ]

    app.include_router(protected)
    return app
