"""Typed read/write access to the persisted tables.

Writes are batched (CLAUDE.md efficiency rules) and candle inserts are
idempotent via ``ON CONFLICT DO NOTHING`` — re-ingesting an overlapping
range after a backfill or restart must be harmless, because it will happen.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict
from sqlalchemy import Numeric, func, select
from sqlalchemy.dialects.postgresql import ARRAY, aggregate_order_by
from sqlalchemy.dialects.postgresql import insert as pg_insert

from tradebot.core.models import (
    Candle,
    CandleInterval,
    Decision,
    Fill,
    Order,
    OrderStatus,
    Side,
)
from tradebot.persistence.database import (
    Database,
    candles_table,
    coins_table,
    decisions_table,
    fills_table,
    orders_table,
    strategy_settings_table,
)

CHART_BUCKET_UNITS: dict[str, str] = {
    "1h": "hour",
    "1d": "day",
    "1w": "week",
    "1M": "month",
}
"""Chart timeframes served by SQL aggregation: API interval -> Postgres
``date_trunc`` unit. Calendar-true buckets (weeks start Monday, months
vary in length), which fixed-duration flooring cannot produce — that is
why these do not reuse ``marketdata.aggregate_candles``."""


class ChartCandle(BaseModel):
    """One aggregated OHLCV bucket for charting (not a domain ``Candle``).

    Deliberately separate from :class:`~tradebot.core.models.Candle`:
    calendar buckets (weeks, months) have no fixed duration, so forcing
    them into ``CandleInterval`` would hand the trading path an interval
    whose arithmetic lies. Amounts stay ``Decimal`` end to end.
    """

    model_config = ConfigDict(frozen=True)

    open_time: datetime
    open_quote: Decimal
    high_quote: Decimal
    low_quote: Decimal
    close_quote: Decimal
    volume_base: Decimal


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

    async def fetch_recent_buckets(
        self, symbol: str, unit: str, limit: int = 300
    ) -> list[ChartCandle]:
        """Return the newest ``limit`` calendar buckets, oldest first.

        Aggregates the stored 1m candles in SQL (``date_trunc`` in UTC,
        first/last by time for open/close, max/min/sum for the rest), so a
        month of history never crosses the wire as raw minutes. ``unit``
        must be a Postgres ``date_trunc`` unit from
        :data:`CHART_BUCKET_UNITS`; anything else raises ``ValueError``
        before touching SQL.
        """
        if unit not in CHART_BUCKET_UNITS.values():
            raise ValueError(
                f"unknown bucket unit {unit!r}; known: {sorted(set(CHART_BUCKET_UNITS.values()))}"
            )
        # Three-argument date_trunc (PG 14+): truncate in UTC explicitly,
        # never in the session timezone — a session set to anything else
        # would silently shift every daily bucket.
        bucket = func.date_trunc(unit, candles_table.c.open_time, "UTC").label("open_time")
        statement = (
            select(
                bucket,
                func.array_agg(
                    aggregate_order_by(candles_table.c.open_quote, candles_table.c.open_time.asc()),
                    type_=ARRAY(Numeric),
                )[1].label("open_quote"),
                func.max(candles_table.c.high_quote).label("high_quote"),
                func.min(candles_table.c.low_quote).label("low_quote"),
                func.array_agg(
                    aggregate_order_by(
                        candles_table.c.close_quote, candles_table.c.open_time.desc()
                    ),
                    type_=ARRAY(Numeric),
                )[1].label("close_quote"),
                func.sum(candles_table.c.volume_base).label("volume_base"),
            )
            .where(
                candles_table.c.symbol == symbol,
                candles_table.c.interval == CandleInterval.M1.value,
            )
            .group_by(bucket)
            .order_by(bucket.desc())
            .limit(limit)
        )
        async with self._database.engine.connect() as connection:
            rows = (await connection.execute(statement)).mappings().all()
        return [ChartCandle.model_validate(dict(row)) for row in reversed(rows)]

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

    async def earliest_open_time(self, symbol: str, interval: CandleInterval) -> datetime | None:
        """Return the oldest stored open time — how deep history reaches."""
        statement = select(func.min(candles_table.c.open_time)).where(
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


def _order_from_row(row: dict[str, Any]) -> Order:
    """Rebuild an ``Order`` from its row, folding the flattened exit plan."""
    stop = row.pop("protective_stop_price_quote")
    limit = row.pop("protective_limit_price_quote")
    breakeven = row.pop("protective_breakeven_at_r")
    trail = row.pop("protective_trail_distance_quote")
    if stop is not None:
        row["protective_exit"] = {
            "stop_price_quote": stop,
            "limit_price_quote": limit,
            "breakeven_at_r": 0.0 if breakeven is None else breakeven,
            "trail_distance_quote": trail,
        }
    return Order.model_validate(row)


class OpenOrder(BaseModel):
    """One restorable order: the intent plus its stop-trigger latch."""

    model_config = ConfigDict(frozen=True)

    order: Order
    triggered: bool


class OrderStore:
    """Latest known state of every order intent — restart recovery for orders.

    The fill journal alone cannot restore a paper adapter: an order that was
    submitted but had not filled when the process died exists nowhere else.
    This store records every submission and its terminal transition so
    :meth:`fetch_open` can re-arm the adapter on boot.
    """

    def __init__(self, database: Database) -> None:
        """Bind the store to ``database``."""
        self._database = database

    async def record_submitted(self, order: Order) -> None:
        """Journal ``order`` as open, before the adapter accepts it.

        Upsert by ``client_order_id``: ids are deterministic per intent, so
        resubmitting a previously cancelled intent (e.g. the kill switch run
        twice on the same candle) legitimately reopens its row. An intent
        that ever filled is kept out of restoration by :meth:`fetch_open`'s
        fill-journal guard, not by refusing the write here.
        """
        row = order.model_dump(exclude={"protective_exit"})
        plan = order.protective_exit
        row["protective_stop_price_quote"] = None if plan is None else plan.stop_price_quote
        row["protective_limit_price_quote"] = None if plan is None else plan.limit_price_quote
        row["protective_breakeven_at_r"] = None if plan is None else plan.breakeven_at_r
        row["protective_trail_distance_quote"] = None if plan is None else plan.trail_distance_quote
        row["status"] = OrderStatus.OPEN.value
        row["triggered"] = False
        row["status_at"] = order.created_at
        # Refresh every column, not just the status: a ratcheted stop is
        # resubmitted under the same id at a new level, and the reopened row
        # must carry the values the adapter is actually working with.
        statement = pg_insert(orders_table).on_conflict_do_update(
            index_elements=["client_order_id"],
            set_={name: value for name, value in row.items() if name != "client_order_id"},
        )
        async with self._database.engine.begin() as connection:
            await connection.execute(statement, [row])

    async def mark_filled(self, client_order_id: str, at: datetime) -> None:
        """Record the order's terminal fill (whole-order fills only today)."""
        await self._set_status(client_order_id, OrderStatus.FILLED, at)

    async def mark_cancelled(self, client_order_id: str, at: datetime) -> None:
        """Record the order's cancellation."""
        await self._set_status(client_order_id, OrderStatus.CANCELLED, at)

    async def mark_triggered(self, client_order_id: str) -> None:
        """Latch a stop-limit's trigger; the order stays open.

        Persisted so a restart restores the order as an active limit instead
        of re-arming the stop — un-triggering across restarts would diverge
        from how a real exchange treats a triggered stop.
        """
        statement = (
            orders_table.update()
            .where(orders_table.c.client_order_id == client_order_id)
            .values(triggered=True)
        )
        async with self._database.engine.begin() as connection:
            await connection.execute(statement)

    async def _set_status(self, client_order_id: str, status: OrderStatus, at: datetime) -> None:
        _require_aware(at)
        statement = (
            orders_table.update()
            .where(orders_table.c.client_order_id == client_order_id)
            .values(status=status.value, status_at=at)
        )
        async with self._database.engine.begin() as connection:
            await connection.execute(statement)

    async def fetch_open(self, symbol: str | None = None) -> list[OpenOrder]:
        """Return open orders (optionally for one symbol), oldest first.

        Guarded against a crash between the fill-journal write and the
        status update: any order with a journaled fill is excluded even if
        its row still says open, because restoring it would double-fill —
        the fill journal outranks this table wherever they disagree.
        """
        has_fill = (
            select(fills_table.c.id)
            .where(fills_table.c.client_order_id == orders_table.c.client_order_id)
            .exists()
        )
        statement = (
            select(orders_table)
            .where(orders_table.c.status == OrderStatus.OPEN.value, ~has_fill)
            .order_by(orders_table.c.created_at, orders_table.c.client_order_id)
        )
        if symbol is not None:
            statement = statement.where(orders_table.c.symbol == symbol)
        async with self._database.engine.connect() as connection:
            rows = (await connection.execute(statement)).mappings().all()
        return [
            OpenOrder(order=_order_from_row(dict(row)), triggered=row["triggered"]) for row in rows
        ]

    async def latest_filled_entry_with_plan(self, symbol: str) -> Order | None:
        """Return the newest filled entry that planned a protective exit, if any.

        The recovery source for an unprotected position: if a crash landed
        between the entry fill and the stop placement, the plan persisted
        with this row is what the worker re-arms the stop from.
        """
        has_fill = (
            select(fills_table.c.id)
            .where(fills_table.c.client_order_id == orders_table.c.client_order_id)
            .exists()
        )
        statement = (
            select(orders_table)
            .where(
                orders_table.c.symbol == symbol,
                orders_table.c.side == Side.BUY.value,
                # A journaled fill outranks the row's status: the crash may
                # have landed between the fill write and mark_filled, and
                # this lookup exists precisely to recover from crashes.
                (orders_table.c.status == OrderStatus.FILLED.value) | has_fill,
                orders_table.c.protective_stop_price_quote.is_not(None),
            )
            .order_by(orders_table.c.status_at.desc(), orders_table.c.created_at.desc())
            .limit(1)
        )
        async with self._database.engine.connect() as connection:
            row = (await connection.execute(statement)).mappings().first()
        return None if row is None else _order_from_row(dict(row))


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


class StrategySettingsStore:
    """Versioned strategy parameters: what each family trades right now.

    Append-only by design — promotions and reverts both append, so the
    version history is the full lineage of every configuration the bot has
    ever traded (ARCHITECTURE.md §12.5/§12.7).
    """

    def __init__(self, database: Database) -> None:
        """Bind the store to ``database``."""
        self._database = database

    async def active(self) -> dict[str, dict[str, Any]]:
        """Return the newest params per family; empty dict means defaults."""
        newest = (
            select(
                strategy_settings_table.c.family,
                func.max(strategy_settings_table.c.id).label("id"),
            )
            .group_by(strategy_settings_table.c.family)
            .subquery()
        )
        statement = select(strategy_settings_table.c.family, strategy_settings_table.c.params).join(
            newest, strategy_settings_table.c.id == newest.c.id
        )
        async with self._database.engine.connect() as connection:
            rows = (await connection.execute(statement)).all()
        return {family: dict(params) for family, params in rows}

    async def record(
        self,
        family: str,
        params: Mapping[str, Any],
        activated_at: datetime,
        source_sweep_id: int | None = None,
        note: str | None = None,
    ) -> int:
        """Append a new active version for ``family``; returns its id."""
        _require_aware(activated_at)
        statement = (
            pg_insert(strategy_settings_table)
            .values(
                family=family,
                params=dict(params),
                source_sweep_id=source_sweep_id,
                note=note,
                activated_at=activated_at,
            )
            .returning(strategy_settings_table.c.id)
        )
        async with self._database.engine.begin() as connection:
            return int((await connection.execute(statement)).scalar_one())

    async def fetch(self, version_id: int) -> dict[str, Any] | None:
        """Return one version row, or ``None`` if it does not exist."""
        statement = select(strategy_settings_table).where(
            strategy_settings_table.c.id == version_id
        )
        async with self._database.engine.connect() as connection:
            row = (await connection.execute(statement)).mappings().first()
        return dict(row) if row is not None else None

    async def history(self, limit: int = 50) -> list[dict[str, Any]]:
        """Return versions newest first, across all families."""
        statement = (
            select(strategy_settings_table)
            .order_by(strategy_settings_table.c.id.desc())
            .limit(limit)
        )
        async with self._database.engine.connect() as connection:
            rows = (await connection.execute(statement)).mappings().all()
        return [dict(row) for row in rows]
