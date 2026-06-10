"""Blind walk-forward evaluation: scenarios, verdicts, learning findings.

The bot decides on history it can see, is graded against a future it could
not, and the graded record accumulates in Postgres (ARCHITECTURE.md
section 12). This package owns scenario generation, condition labeling,
scoring, and the mined learning findings — it never places orders and never
changes trading rules; improvements always pass through a human.
"""

from tradebot.evaluation.classifier import classify_window, window_volatility
from tradebot.evaluation.models import (
    EventLabel,
    LearningFinding,
    MarketConditions,
    RunStatus,
    Scenario,
    ScenarioClass,
    ScenarioResult,
    TimingLabel,
    TrendLabel,
    Verdict,
    VolatilityLabel,
)

__all__ = [
    "EventLabel",
    "LearningFinding",
    "MarketConditions",
    "RunStatus",
    "Scenario",
    "ScenarioClass",
    "ScenarioResult",
    "TimingLabel",
    "TrendLabel",
    "Verdict",
    "VolatilityLabel",
    "classify_window",
    "window_volatility",
]
