"""Trading strategies: pure signal generators.

Strategies consume closed candles and the current position, and emit
``Signal`` proposals — never orders (CLAUDE.md invariant 4). They are
deliberately ignorant of mode (backtest/paper/live), balances, and sizing;
all of that belongs to the risk manager.
"""

from tradebot.strategies.base import Strategy
from tradebot.strategies.trend_following import TrendFollowingConfig, TrendFollowingStrategy

__all__ = ["Strategy", "TrendFollowingConfig", "TrendFollowingStrategy"]
