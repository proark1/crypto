"""Regime gates, confirmation filters, and signal fusion (ARCHITECTURE.md 5.2).

A trade decision is a pipeline of gates, not a vote among equals: gates can
only block or shrink an entry, never create one, and every gate decision is
journaled with the signal so dead filters are found and removed. Nothing in
this package may know whether it runs in backtest, paper, or live.
"""

from tradebot.signals.base import EntryGate, GateDecision
from tradebot.signals.data_health import DataHealthGate, FeedHealth
from tradebot.signals.funding import FundingMonitor, FundingRateFetcher
from tradebot.signals.regime import (
    RANGING,
    RISK_OFF,
    TRENDING,
    WARMING_UP,
    MarketRegimeDetector,
    Regime,
    RegimeClassifier,
    RegimeConfig,
    RegimeGate,
)
from tradebot.signals.sentiment import MarketSentiment, SentimentConfig, SentimentMonitor

__all__ = [
    "RANGING",
    "RISK_OFF",
    "TRENDING",
    "WARMING_UP",
    "DataHealthGate",
    "EntryGate",
    "FeedHealth",
    "FundingMonitor",
    "FundingRateFetcher",
    "GateDecision",
    "MarketRegimeDetector",
    "MarketSentiment",
    "Regime",
    "RegimeClassifier",
    "RegimeConfig",
    "RegimeGate",
    "SentimentConfig",
    "SentimentMonitor",
]
