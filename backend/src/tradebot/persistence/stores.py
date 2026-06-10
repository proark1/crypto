"""Typed read/write access to the persisted tables.

Writes are batched (CLAUDE.md efficiency rules) and candle inserts are
idempotent via ``ON CONFLICT DO NOTHING`` — re-ingesting an overlapping
range after a backfill or restart must be harmless, because it will happen.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from tradebot.core.models import Candle, CandleInterval, Decision, Fill
from tradebot.persistence.database import (
    Database,
    candles_table,
    coins_table,
    decisions_table,
    fills_table,
)


def _require_aware(moment: datetime) -> None:
    """Reject naive datetimes (repo invariant: timestamps are UTC-aware).

    A naive bound compared against ``timestamptz`` would be interpreted in
    the session timezone — a silent off-by-hours bug, so it fails here.
    """
    if moment.tzinfo is None:
        raise ValueError("naive datetime is not allowed; timestamps must be UTC-aware")


class CandleStore:
    """Persisted candle history: the backtest dataset and live warm-up source."""

    def __init__(self, database: Database) -> None:
        """Bind the store to ``database``."""
        self._database = database

    async def insert_batch(self, candles: Sequence[Candle]) -> None:
        """Insert ``candles`` in one statement; duplicates are ignored.

        Idempotency by primary key (symbol, interval, open_time): a candle
        that is already stored is assumed identical — exchange history for a
        closed candle never legitimately changes.
        """
        if not candles:
            return
        rows = [candle.model_dump() for candle in candles]
        statement = pg_insert(candles_table).on_conflict_do_nothing(
            index_elements=["symbol", "interval", "open_time"]
        )
        async with self._database.engine.begin() as connection:
            await connection.execute(statement, rows)

    async def fetch_range(
        self,
        symbol: str,
        interval: CandleInterval,
        start: datetime,
        end: datetime,
    ) -> list[Candle]:
        """Return candles with ``start <= open_time < end``, time-ordered."""
        _require_aware(start)
        _require_aware(end)
        statement = (
            select(candles_table)
            .where(
                candles_table.c.symbol == symbol,
                candles_table.c.interval == interval.value,
                candles_table.c.open_time >= start,
                candles_table.c.open_time < end,
            )
            .order_by(candles_table.c.open_time)
        )
        async with self._database.engine.connect() as connection:
            rows = (await connection.execute(statement)).mappings().all()
        return [Candle.model_validate(dict(row)) for row in rows]

    async def fetch_recent(
        self, symbol: str, interval: CandleInterval, limit: int = 300
    ) -> list[Candle]:
        """Return the newest ``limit`` candles in chronological order."""
        statement = (
            select(candles_table)
            .where(
                candles_table.c.symbol == symbol,
                candles_table.c.interval == interval.value,
            )
            .order_by(candles_table.c.open_time.desc())
            .limit(limit)
        )
        async with self._database.engine.connect() as connection:
            rows = (await connection.execute(statement)).mappings().all()
        return [Candle.model_validate(dict(row)) for row in reversed(rows)]

    async def latest_candle(self, symbol: str, interval: CandleInterval) -> Candle | None:
        """Return the newest stored candle — the mark price for valuations."""
        statement = (
            select(candles_table)
            .where(
                candles_table.c.symbol == symbol,
                candles_table.c.interval == interval.value,
            )
            .order_by(candles_table.c.open_time.desc())
            .limit(1)
        )
        async with self._database.engine.connect() as connection:
            row = (await connection.execute(statement)).mappings().first()
        return None if row is None else Candle.model_validate(dict(row))

    async def latest_open_time(self, symbol: str, interval: CandleInterval) -> datetime | None:
        """Return the newest stored open time — where backfill resumes from."""
        statement = select(func.max(candles_table.c.open_time)).where(
            candles_table.c.symbol == symbol,
            candles_table.c.interval == interval.value,
        )
        async with self._database.engine.connect() as connection:
            value: datetime | None = (await connection.execute(statement)).scalar()
        return value


class CoinStore:
    """The actively traded pairs, surviving restarts and config changes."""

    def __init__(self, database: Database) -> None:
        """Bind the store to ``database``."""
        self._database = database

    async def list_symbols(self) -> tuple[str, ...]:
        """Return active symbols in the order they were added."""
        statement = select(coins_table.c.symbol).order_by(coins_table.c.added_at)
        async with self._database.engine.connect() as connection:
            rows = (await connection.execute(statement)).scalars().all()
        return tuple(rows)

    async def add(self, symbol: str, added_at: datetime) -> None:
        """Add ``symbol``; re-adding an active coin is harmless."""
        _require_aware(added_at)
        statement = pg_insert(coins_table).on_conflict_do_nothing(index_elements=["symbol"])
        async with self._database.engine.begin() as connection:
            await connection.execute(statement, [{"symbol": symbol, "added_at": added_at}])

    async def remove(self, symbol: str) -> None:
        """Remove ``symbol`` from the active set (its history stays)."""
        async with self._database.engine.begin() as connection:
            await connection.execute(coins_table.delete().where(coins_table.c.symbol == symbol))

    async def seed_if_empty(self, symbols: Sequence[str], now: datetime) -> bool:
        """Populate from config on first boot only; returns whether it seeded.

        After the first boot the table is the source of truth: coins removed
        through the API must not resurrect because an env var still lists
        them.
        """
        _require_aware(now)
        if await self.list_symbols():
            return False
        async with self._database.engine.begin() as connection:
            await connection.execute(
                pg_insert(coins_table).on_conflict_do_nothing(index_elements=["symbol"]),
                [{"symbol": symbol, "added_at": now} for symbol in symbols],
            )
        return True


class FillStore:
    """Append-only record of every execution; the trade journal's raw data."""

    def __init__(self, database: Database) -> None:
        """Bind the store to ``database``."""
        self._database = database

    async def append(self, fill: Fill) -> None:
        """Persist one fill."""
        async with self._database.engine.begin() as connection:
            await connection.execute(fills_table.insert(), [fill.model_dump()])

    async def fetch_all(self, symbol: str | None = None) -> list[Fill]:
        """Return fills (optionally for one symbol) in execution order."""
        statement = select(fills_table).order_by(fills_table.c.id)
        if symbol is not None:
            statement = statement.where(fills_table.c.symbol == symbol)
        async with self._database.engine.connect() as connection:
            rows = (await connection.execute(statement)).mappings().all()
        # The surrogate ``id`` column is ignored by validation (not a model field).
        return [Fill.model_validate(dict(row)) for row in rows]


class DecisionStore:
    """Append-only record of every signal's fate; the explainability trail."""

    def __init__(self, database: Database) -> None:
        """Bind the store to ``database``."""
        self._database = database

    async def append(self, decision: Decision) -> None:
        """Persist one decision."""
        row = decision.model_dump()
        row["reasons"] = list(decision.reasons)  # ARRAY column wants a list
        async with self._database.engine.begin() as connection:
            await connection.execute(decisions_table.insert(), [row])

    async def fetch_recent(self, symbol: str, limit: int = 50) -> list[Decision]:
        """Return the newest ``limit`` decisions for ``symbol``, newest first."""
        statement = (
            select(decisions_table)
            .where(decisions_table.c.symbol == symbol)
            .order_by(decisions_table.c.id.desc())
            .limit(limit)
        )
        async with self._database.engine.connect() as connection:
            rows = (await connection.execute(statement)).mappings().all()
        return [Decision.model_validate(dict(row)) for row in rows]
