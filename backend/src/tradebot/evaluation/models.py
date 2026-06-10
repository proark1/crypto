"""Domain models for the evaluation module (ARCHITECTURE.md section 12).

These cross the boundary between the scenario engine, the persistence layer,
and (later) the control API, so they are pydantic models per CLAUDE.md.
Monetary values (prices, PnL) are Decimal as everywhere; dimensionless
analysis numbers (R-multiples are ratios of money, kept Decimal for
exactness; confidence and volatility are floats like indicator math).
"""

from __future__ import annotations

import enum

from pydantic import BaseModel, ConfigDict

from tradebot.core.models import Amount, UtcDatetime


class RunStatus(enum.StrEnum):
    """Lifecycle of an evaluation run; interrupted runs are never half-reported."""

    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    INTERRUPTED = "interrupted"


class ScenarioClass(enum.StrEnum):
    """What the bot is being asked: enter from flat, or manage a holding."""

    FLAT = "flat"
    HOLDING = "holding"


class TrendLabel(enum.StrEnum):
    """Direction of the context window, per the frozen classifier definitions."""

    UP = "up"
    DOWN = "down"
    RANGING = "ranging"


class VolatilityLabel(enum.StrEnum):
    """Window volatility relative to the run's dataset-wide reference."""

    HIGH = "high"
    NORMAL = "normal"
    LOW = "low"


class EventLabel(enum.StrEnum):
    """Mechanically detected market events inside a context window."""

    PUMP = "pump"
    DUMP = "dump"
    BREAKOUT_REAL = "breakout_real"
    BREAKOUT_FAKE = "breakout_fake"
    POST_CRASH_RECOVERY = "post_crash_recovery"


class Verdict(enum.StrEnum):
    """The graded outcome of one scenario decision (frozen R bands)."""

    EXCELLENT = "excellent"
    GOOD = "good"
    NEUTRAL = "neutral"
    BAD = "bad"
    VERY_BAD = "very_bad"
    MISSED_OPPORTUNITY = "missed_opportunity"
    CORRECT_HOLD = "correct_hold"
    WRONG_HOLD = "wrong_hold"


class TimingLabel(enum.StrEnum):
    """Was the idea right but the clock wrong? Derived from MFE/MAE."""

    EARLY_ENTRY = "early_entry"
    LATE_ENTRY = "late_entry"
    EARLY_EXIT = "early_exit"
    LATE_EXIT = "late_exit"
    ON_TIME = "on_time"


class MarketConditions(BaseModel):
    """The classifier's labels for one context window."""

    model_config = ConfigDict(frozen=True)

    trend: TrendLabel
    volatility: VolatilityLabel
    events: tuple[EventLabel, ...] = ()


class Scenario(BaseModel):
    """One blind decision point: a moment in history plus its context shape.

    Candles are referenced by (symbol, timeframe, decision_time, lookback),
    never copied — the candle store is the single source of price truth.
    """

    model_config = ConfigDict(frozen=True)

    run_id: int
    symbol: str
    timeframe: str
    decision_time: UtcDatetime
    lookback_candles: int
    scenario_class: ScenarioClass
    conditions: MarketConditions
    seed: int


class ScenarioResult(BaseModel):
    """The bot's blind decision and its graded outcome for one scenario."""

    model_config = ConfigDict(frozen=True)

    scenario_id: int
    decision: str
    confidence: float | None = None
    reasons: tuple[str, ...] = ()
    entry_price_quote: Amount | None = None
    exit_price_quote: Amount | None = None
    r_multiple: Amount | None = None
    pnl_quote: Amount | None = None
    mfe_r: Amount | None = None
    mae_r: Amount | None = None
    duration_candles: int | None = None
    stop_hit: bool | None = None
    oracle_r: Amount | None = None
    verdict: Verdict
    timing: TimingLabel | None = None
    created_at: UtcDatetime


class LearningFinding(BaseModel):
    """One mistake pattern mined from a run, awaiting human accept/reject.

    Findings recommend; they never change trading rules themselves
    (ARCHITECTURE.md section 12 — human approval is mandatory).
    """

    model_config = ConfigDict(frozen=True)

    run_id: int
    pattern: str
    evidence_scenario_ids: tuple[int, ...]
    affected_count: int
    average_r_impact: Amount
    suggestion: str
    confidence: str
    status: str = "proposed"
    created_at: UtcDatetime
