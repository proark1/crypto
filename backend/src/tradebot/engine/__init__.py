"""Orchestration: closed candle -> strategy -> risk -> execution -> books.

One engine drives every mode. The backtest runner calls it candle-by-candle
over history; paper trading subscribes it to live ``CandleClosed`` events
with the same fill simulator behind it (real prices, simulated fills); live
trading later swaps only the execution adapter. This module existing is the
one-code-path invariant made literal.
"""

from tradebot.engine.trading_engine import TradingEngine

__all__ = ["TradingEngine"]
