"""The strategy competition: five paper bots, one strategy each.

Every competitor trades the same coins, the same candles, and the same
entry gates from its own paper account, so the only variable is the
strategy — the leaderboard answers "who is best" with money, and the
research comparison answers it with graded scenarios (ARCHITECTURE.md
section 13).
"""

from tradebot.competition.lineup import (
    LINEUP,
    PRODUCTION_BOT_ID,
    CompetitorSpec,
    ScopedSignalStrategy,
    build_challenger_strategy,
    build_scenario_strategy,
    spec_for,
)

__all__ = [
    "LINEUP",
    "PRODUCTION_BOT_ID",
    "CompetitorSpec",
    "ScopedSignalStrategy",
    "build_challenger_strategy",
    "build_scenario_strategy",
    "spec_for",
]
