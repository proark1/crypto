"""Trading strategies: pure signal generators.

Strategies consume closed candles and the current position, and emit
``Signal`` proposals — never orders (CLAUDE.md invariant 4). They are
deliberately ignorant of mode (backtest/paper/live), balances, and sizing;
all of that belongs to the risk manager.
"""

from tradebot.strategies.adx_trend import AdxTrendConfig, AdxTrendStrategy
from tradebot.strategies.base import Strategy
from tradebot.strategies.bollinger_reversion import (
    BollingerReversionConfig,
    BollingerReversionStrategy,
)
from tradebot.strategies.breakout import BreakoutConfig, BreakoutStrategy
from tradebot.strategies.composite import CompositeStrategy
from tradebot.strategies.controls import (
    CONTROL_STRATEGIES,
    BuyHoldConfig,
    BuyHoldStrategy,
    DcaConfig,
    DcaStrategy,
    GridConfig,
    GridStrategy,
    RandomEntryConfig,
    RandomEntryStrategy,
    build_control_strategy,
    validate_control_params,
)
from tradebot.strategies.funding import FundingConfig, FundingProvider, FundingStrategy
from tradebot.strategies.keltner import KeltnerConfig, KeltnerStrategy
from tradebot.strategies.mean_reversion import MeanReversionConfig, MeanReversionStrategy
from tradebot.strategies.momentum import MomentumConfig, MomentumStrategy
from tradebot.strategies.router import RegimeStrategyRouter
from tradebot.strategies.rsi_trend import RsiTrendConfig, RsiTrendStrategy
from tradebot.strategies.squeeze import SqueezeConfig, SqueezeStrategy
from tradebot.strategies.supertrend import SupertrendConfig, SupertrendStrategy
from tradebot.strategies.trend_following import TrendFollowingConfig, TrendFollowingStrategy
from tradebot.strategies.tsmom import TsmomConfig, TsmomStrategy
from tradebot.strategies.vol_breakout import VolBreakoutConfig, VolBreakoutStrategy

__all__ = [
    "CONTROL_STRATEGIES",
    "AdxTrendConfig",
    "AdxTrendStrategy",
    "BollingerReversionConfig",
    "BollingerReversionStrategy",
    "BreakoutConfig",
    "BreakoutStrategy",
    "BuyHoldConfig",
    "BuyHoldStrategy",
    "CompositeStrategy",
    "DcaConfig",
    "DcaStrategy",
    "FundingConfig",
    "FundingProvider",
    "FundingStrategy",
    "GridConfig",
    "GridStrategy",
    "KeltnerConfig",
    "KeltnerStrategy",
    "MeanReversionConfig",
    "MeanReversionStrategy",
    "MomentumConfig",
    "MomentumStrategy",
    "RandomEntryConfig",
    "RandomEntryStrategy",
    "RegimeStrategyRouter",
    "RsiTrendConfig",
    "RsiTrendStrategy",
    "SqueezeConfig",
    "SqueezeStrategy",
    "Strategy",
    "SupertrendConfig",
    "SupertrendStrategy",
    "TrendFollowingConfig",
    "TrendFollowingStrategy",
    "TsmomConfig",
    "TsmomStrategy",
    "VolBreakoutConfig",
    "VolBreakoutStrategy",
    "build_control_strategy",
    "validate_control_params",
]
