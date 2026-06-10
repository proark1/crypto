"""Engine lifecycle and schema definition.

One ``Database`` per process wraps the async engine. The schema is created
idempotently at startup — migrations (Alembic) become worthwhile once the
schema stops churning; revisit after Phase 2 settles.
"""

from __future__ import annotations

from types import TracebackType

from sqlalchemy import BigInteger, Column, DateTime, MetaData, Numeric, Table, Text
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

metadata = MetaData()

# NUMERIC with no precision cap: Postgres stores exact arbitrary-precision
# decimals, matching the Decimal-only money invariant end to end.
candles_table = Table(
    "candles",
    metadata,
    Column("symbol", Text, primary_key=True),
    Column("interval", Text, primary_key=True),
    Column("open_time", DateTime(timezone=True), primary_key=True),
    Column("close_time", DateTime(timezone=True), nullable=False),
    Column("open_quote", Numeric, nullable=False),
    Column("high_quote", Numeric, nullable=False),
    Column("low_quote", Numeric, nullable=False),
    Column("close_quote", Numeric, nullable=False),
    Column("volume_base", Numeric, nullable=False),
)
"""PK (symbol, interval, open_time) doubles as the range-scan index for the
hot fetch path (one symbol+interval ordered by time)."""

fills_table = Table(
    "fills",
    metadata,
    Column("id", BigInteger, primary_key=True, autoincrement=True),
    Column("client_order_id", Text, nullable=False, index=True),
    Column("symbol", Text, nullable=False, index=True),
    Column("side", Text, nullable=False),
    Column("price_quote", Numeric, nullable=False),
    Column("quantity_base", Numeric, nullable=False),
    Column("fee_quote", Numeric, nullable=False),
    Column("filled_at", DateTime(timezone=True), nullable=False),
)
"""Append-only: fills are facts. A surrogate id because one order can fill
in several parts with identical timestamps."""

decisions_table = Table(
    "decisions",
    metadata,
    Column("id", BigInteger, primary_key=True, autoincrement=True),
    Column("signal_id", Text, nullable=False),
    Column("strategy_name", Text, nullable=False),
    Column("symbol", Text, nullable=False, index=True),
    Column("side", Text, nullable=False),
    Column("stop_price_quote", Numeric, nullable=False),
    Column("reasons", ARRAY(Text), nullable=False),
    Column("outcome", Text, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
)
"""Every signal and its fate (submitted/vetoed/paused) — the audit trail the
decision-pipeline UI reads. Append-only, like fills."""


_SYNC_SCHEME_PREFIXES = (
    "postgres://",
    "postgresql://",
    "postgresql+psycopg2://",
    "postgresql+psycopg://",
)


def coerce_async_dsn(url: str) -> str:
    """Rewrite common Postgres URL schemes to the asyncpg driver.

    Platforms hand out plain ``postgresql://`` (Railway) or legacy
    ``postgres://`` (Heroku-style) DSNs; SQLAlchemy would resolve those to
    the synchronous psycopg2 driver, which is not installed — by design,
    this codebase is asyncpg-only. Coercing the scheme here means any
    standard Postgres URL can be pasted into ``TRADEBOT_DATABASE_URL``
    without the deploy crash-looping on a driver import.
    """
    if url.startswith("postgresql+asyncpg://"):
        return url
    for prefix in _SYNC_SCHEME_PREFIXES:
        if url.startswith(prefix):
            return "postgresql+asyncpg://" + url.removeprefix(prefix)
    return url


class Database:
    """Owns the async engine; use as an async context manager."""

    def __init__(self, url: str) -> None:
        """Create an engine for ``url`` (any standard Postgres DSN form).

        ``pool_pre_ping`` because the bot runs for weeks against a managed
        Postgres: pooled connections silently die across DB restarts and
        idle timeouts, and the first query after that must not be the one
        that fails.
        """
        self._engine: AsyncEngine = create_async_engine(coerce_async_dsn(url), pool_pre_ping=True)

    @property
    def engine(self) -> AsyncEngine:
        """The underlying engine, for stores and tests."""
        return self._engine

    async def create_schema(self) -> None:
        """Create all tables if they do not exist (idempotent)."""
        async with self._engine.begin() as connection:
            await connection.run_sync(metadata.create_all)

    async def __aenter__(self) -> Database:
        """Enter context: the engine connects lazily, nothing to do yet."""
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Dispose the engine and its connection pool."""
        await self._engine.dispose()
