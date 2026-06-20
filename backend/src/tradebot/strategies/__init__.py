"""Trading strategies: pure signal generators.

Strategies consume closed candles and the current position, and emit
``Signal`` proposals — never orders (CLAUDE.md invariant 4). They are
deliberately ignorant of mode (backtest/paper/live), balances, and sizing;
all of that belongs to the risk manager.
"""

from tradebot.strategies.base import Strategy
from tradebot.strategies.breakout import BreakoutConfig, BreakoutStrategy
from tradebot.strategies.composite import CompositeStrategy
from tradebot.strategies.controls import (
    CONTROL_STRATEGIES,
    RandomEntryConfig,
    RandomEntryStrategy,
    build_control_strategy,
    validate_control_params,
)
from tradebot.strategies.funding import FundingConfig, FundingProvider, FundingStrategy
from tradebot.strategies.mean_reversion import MeanReversionConfig, MeanReversionStrategy
from tradebot.strategies.momentum import MomentumConfig, MomentumStrategy
from tradebot.strategies.router import RegimeStrategyRouter
from tradebot.strategies.squeeze import SqueezeConfig, SqueezeStrategy
from tradebot.strategies.supertrend import SupertrendConfig, SupertrendStrategy
from tradebot.strategies.trend_following import TrendFollowingConfig, TrendFollowingStrategy

__all__ = [
    "CONTROL_STRATEGIES",
    "BreakoutConfig",
    "BreakoutStrategy",
    "CompositeStrategy",
    "FundingConfig",
    "FundingProvider",
    "FundingStrategy",
    "MeanReversionConfig",
    "MeanReversionStrategy",
    "MomentumConfig",
    "MomentumStrategy",
    "RandomEntryConfig",
    "RandomEntryStrategy",
    "RegimeStrategyRouter",
    "SqueezeConfig",
    "SqueezeStrategy",
    "Strategy",
    "SupertrendConfig",
    "SupertrendStrategy",
    "TrendFollowingConfig",
    "TrendFollowingStrategy",
    "build_control_strategy",
    "validate_control_params",
]
