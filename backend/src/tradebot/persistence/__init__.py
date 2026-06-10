"""Postgres persistence for candles and fills (ARCHITECTURE.md 4.5, 7.1).

SQLAlchemy Core (no ORM) over asyncpg: explicit SQL-shaped code, exact
``NUMERIC`` decimals, timezone-aware timestamps, and batched writes per the
efficiency rules. The stores are deliberately thin — accounting and trading
logic stay in their own modules; this layer only persists and retrieves.
"""

from tradebot.persistence.database import Database
from tradebot.persistence.stores import CandleStore, CoinStore, DecisionStore, FillStore

__all__ = ["CandleStore", "CoinStore", "Database", "DecisionStore", "FillStore"]
