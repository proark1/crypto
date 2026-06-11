"""The strategy competition: built-in lineup plus user-built bots.

Every competitor trades the same coins and candles from its own paper
account, so the only variable is the strategy — the leaderboard answers
"who is best" with money, and the research comparison answers it with
graded scenarios (ARCHITECTURE.md section 13). Built-in challengers and
custom bots trade their rules ungated by the regime router's family
routing (that routing IS the production bot's strategy, not a house
rule); only the news/event veto applies to everyone.
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
from tradebot.competition.rules import (
    CUSTOM_BOT_PREFIX,
    ENTRY_MODES,
    FAMILY_DESCRIPTIONS,
    build_rules_strategy,
    describe_rules,
    slugify_bot_label,
    validate_rules,
)

__all__ = [
    "CUSTOM_BOT_PREFIX",
    "ENTRY_MODES",
    "FAMILY_DESCRIPTIONS",
    "LINEUP",
    "PRODUCTION_BOT_ID",
    "CompetitorSpec",
    "ScopedSignalStrategy",
    "build_challenger_strategy",
    "build_rules_strategy",
    "build_scenario_strategy",
    "describe_rules",
    "slugify_bot_label",
    "spec_for",
    "validate_rules",
]
