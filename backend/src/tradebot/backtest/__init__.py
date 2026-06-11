"""Backtesting: replay candles through the exact production trading path."""

from tradebot.backtest.parity import DivergenceReport, compare_fills
from tradebot.backtest.report import BacktestReport, build_report
from tradebot.backtest.runner import BacktestResult, BacktestRunner
from tradebot.backtest.walkforward import (
    WalkForwardWindow,
    split_rolling_by_fraction,
    split_walk_forward,
)

__all__ = [
    "BacktestReport",
    "BacktestResult",
    "BacktestRunner",
    "DivergenceReport",
    "WalkForwardWindow",
    "build_report",
    "compare_fills",
    "split_rolling_by_fraction",
    "split_walk_forward",
]
