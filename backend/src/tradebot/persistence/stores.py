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

from tradebot.core.models import Candle, CandleInterval, Fill, Side
from tradebot.persistence.database import Database, candles_table, fills_table


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

    async def latest_open_time(self, symbol: str, interval: CandleInterval) -> datetime | None:
        """Return the newest stored open time — where backfill resumes from."""
        statement = select(func.max(candles_table.c.open_time)).where(
            candles_table.c.symbol == symbol,
            candles_table.c.interval == interval.value,
        )
        async with self._database.engine.connect() as connection:
            value: datetime | None = (await connection.execute(statement)).scalar()
        return value


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
        return [
            Fill(
                client_order_id=row["client_order_id"],
                symbol=row["symbol"],
                side=Side(row["side"]),
                price_quote=row["price_quote"],
                quantity_base=row["quantity_base"],
                fee_quote=row["fee_quote"],
                filled_at=row["filled_at"],
            )
            for row in rows
        ]
