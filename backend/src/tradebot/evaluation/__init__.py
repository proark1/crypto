"""Blind walk-forward evaluation: scenarios, verdicts, learning findings.

The bot decides on history it can see, is graded against a future it could
not, and the graded record accumulates in Postgres (ARCHITECTURE.md
section 12). This package owns scenario generation, condition labeling,
scoring, and the mined learning findings — it never places orders and never
changes trading rules; improvements always pass through a human.
"""

from tradebot.evaluation.classifier import classify_window, window_volatility
from tradebot.evaluation.engine import EvaluatedDecision, ScenarioEvaluator, ScenarioSpec
from tradebot.evaluation.generator import GeneratorConfig, generate_specs
from tradebot.evaluation.learning import mine_findings
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

# NOTE: ``runner`` and ``replay`` import the persistence layer, which itself
# imports this package's models — they are imported by their full module
# path (``tradebot.evaluation.runner`` / ``.replay``) to keep this __init__
# import-cycle-free.

__all__ = [
    "EvaluatedDecision",
    "EventLabel",
    "GeneratorConfig",
    "LearningFinding",
    "MarketConditions",
    "RunStatus",
    "Scenario",
    "ScenarioClass",
    "ScenarioEvaluator",
    "ScenarioResult",
    "ScenarioSpec",
    "TimingLabel",
    "TrendLabel",
    "Verdict",
    "VolatilityLabel",
    "classify_window",
    "generate_specs",
    "mine_findings",
    "window_volatility",
]
