"""Postgres persistence for candles and fills (ARCHITECTURE.md 4.5, 7.1).

SQLAlchemy Core (no ORM) over asyncpg: explicit SQL-shaped code, exact
``NUMERIC`` decimals, timezone-aware timestamps, and batched writes per the
efficiency rules. The stores are deliberately thin — accounting and trading
logic stay in their own modules; this layer only persists and retrieves.
"""

from tradebot.persistence.bakeoff_store import BakeOffStore
from tradebot.persistence.database import Database
from tradebot.persistence.evaluation_store import EvaluationStore
from tradebot.persistence.stores import (
    CHART_BUCKET_UNITS,
    BotCapitalStore,
    CampaignHistoryStore,
    CampaignSettingsStore,
    CandleStore,
    ChartCandle,
    CoinStore,
    CustomBotStore,
    DecisionStore,
    FillStore,
    FundingStore,
    OpenOrder,
    OrderStore,
    RiskStateStore,
    StrategySettingsStore,
    TradingFeesStore,
)

__all__ = [
    "CHART_BUCKET_UNITS",
    "BakeOffStore",
    "BotCapitalStore",
    "CampaignHistoryStore",
    "CampaignSettingsStore",
    "CandleStore",
    "ChartCandle",
    "CoinStore",
    "CustomBotStore",
    "Database",
    "DecisionStore",
    "EvaluationStore",
    "FillStore",
    "FundingStore",
    "OpenOrder",
    "OrderStore",
    "RiskStateStore",
    "StrategySettingsStore",
    "TradingFeesStore",
]
