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
    custom_bots_table,
    decisions_table,
    fills_table,
    orders_table,
    risk_state_table,
    strategy_settings_table,
    trading_fees_table,
)
from tradebot.risk.breakers import BreakerState

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
    """Append-only record of every execution; the trade journal's raw data.

    One instance per bot: the strategy competition runs several paper
    accounts against one ``fills`` table, and ``bot_id`` keeps each
    account's journal (and the portfolio replayed from it) separate.
    The default scope is the production bot, which also owns every row
    written before the competition existed.
    """

    def __init__(self, database: Database, bot_id: str = "production") -> None:
        """Bind the store to ``database``, scoped to ``bot_id``'s journal."""
        self._database = database
        self._bot_id = bot_id

    async def append(self, fill: Fill) -> None:
        """Persist one fill under this store's bot."""
        row = fill.model_dump()
        row["bot_id"] = self._bot_id
        async with self._database.engine.begin() as connection:
            await connection.execute(fills_table.insert(), [row])

    async def fetch_all(self, symbol: str | None = None) -> list[Fill]:
        """Return this bot's fills (optionally for one symbol) in execution order."""
        statement = (
            select(fills_table)
            .where(fills_table.c.bot_id == self._bot_id)
            .order_by(fills_table.c.id)
        )
        if symbol is not None:
            statement = statement.where(fills_table.c.symbol == symbol)
        async with self._database.engine.connect() as connection:
            rows = (await connection.execute(statement)).mappings().all()
        # The surrogate ``id`` and ``bot_id`` columns are ignored by
        # validation (not model fields).
        return [Fill.model_validate(dict(row)) for row in rows]

    async def count_by_side(self) -> dict[str, int]:
        """Return this bot's fill counts per side — leaderboard activity, cheap.

        One indexed aggregate instead of dragging the whole journal across
        the wire every poll (the leaderboard refreshes continuously).
        """
        statement = (
            select(fills_table.c.side, func.count())
            .where(fills_table.c.bot_id == self._bot_id)
            .group_by(fills_table.c.side)
        )
        async with self._database.engine.connect() as connection:
            rows = (await connection.execute(statement)).all()
        return {side: int(count) for side, count in rows}


def _order_from_row(row: dict[str, Any]) -> Order:
    """Rebuild an ``Order`` from its row, folding the flattened exit plan."""
    row.pop("bot_id", None)  # storage scoping, not part of the domain model
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

    One instance per bot, like :class:`FillStore`: client order ids are
    namespaced per bot upstream (competition strategies prefix their
    signal ids), and ``bot_id`` keeps each account's restoration scoped
    to its own orders.
    """

    def __init__(self, database: Database, bot_id: str = "production") -> None:
        """Bind the store to ``database``, scoped to ``bot_id``'s orders."""
        self._database = database
        self._bot_id = bot_id

    async def record_submitted(self, order: Order) -> None:
        """Journal ``order`` as open, before the adapter accepts it.

        Upsert by ``client_order_id``: ids are deterministic per intent, so
        resubmitting a previously cancelled intent (e.g. the kill switch run
        twice on the same candle) legitimately reopens its row. An intent
        that ever filled is kept out of restoration by :meth:`fetch_open`'s
        fill-journal guard, not by refusing the write here.
        """
        row = order.model_dump(exclude={"protective_exit"})
        row["bot_id"] = self._bot_id
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
        # must carry the values the adapter is actually working with. The
        # SET clause references the excluded (inserted) row so the statement
        # shape stays constant and plan-cacheable.
        statement = pg_insert(orders_table)
        statement = statement.on_conflict_do_update(
            index_elements=["client_order_id"],
            set_={name: statement.excluded[name] for name in row if name != "client_order_id"},
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

        Quantity-aware against partial fills and crashes alike: the fill
        journal outranks this table, so an order whose journaled fills
        already cover its quantity is excluded even if its row still says
        open (a crash between the fill write and the status update), and a
        partially filled order is returned with only the *remainder* —
        restoring the full quantity would double-fill the filled part.
        """
        filled = (
            select(
                fills_table.c.client_order_id,
                func.sum(fills_table.c.quantity_base).label("filled_base"),
            )
            .group_by(fills_table.c.client_order_id)
            .subquery()
        )
        statement = (
            select(orders_table, filled.c.filled_base)
            .join(
                filled,
                orders_table.c.client_order_id == filled.c.client_order_id,
                isouter=True,
            )
            .where(
                orders_table.c.bot_id == self._bot_id,
                orders_table.c.status == OrderStatus.OPEN.value,
                func.coalesce(filled.c.filled_base, 0) < orders_table.c.quantity_base,
            )
            .order_by(orders_table.c.created_at, orders_table.c.client_order_id)
        )
        if symbol is not None:
            statement = statement.where(orders_table.c.symbol == symbol)
        async with self._database.engine.connect() as connection:
            rows = (await connection.execute(statement)).mappings().all()
        open_orders = []
        for row in rows:
            data = dict(row)
            already_filled = data.pop("filled_base") or Decimal(0)
            order = _order_from_row(data)
            if already_filled > 0:
                order = order.model_copy(
                    update={"quantity_base": order.quantity_base - already_filled}
                )
            open_orders.append(OpenOrder(order=order, triggered=row["triggered"]))
        return open_orders

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
                orders_table.c.bot_id == self._bot_id,
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
    """Append-only record of every signal's fate; the explainability trail.

    One instance per bot (see :class:`FillStore`): each competition
    account keeps its own decision trail, and the production bot owns
    every row written before the competition existed.
    """

    def __init__(self, database: Database, bot_id: str = "production") -> None:
        """Bind the store to ``database``, scoped to ``bot_id``'s decisions."""
        self._database = database
        self._bot_id = bot_id

    async def append(self, decision: Decision) -> None:
        """Persist one decision under this store's bot."""
        row = decision.model_dump()
        row["bot_id"] = self._bot_id
        row["reasons"] = list(decision.reasons)  # ARRAY column wants a list
        async with self._database.engine.begin() as connection:
            await connection.execute(decisions_table.insert(), [row])

    async def fetch_recent(self, symbol: str, limit: int = 50) -> list[Decision]:
        """Return this bot's newest ``limit`` decisions for ``symbol``, newest first."""
        statement = (
            select(decisions_table)
            .where(
                decisions_table.c.bot_id == self._bot_id,
                decisions_table.c.symbol == symbol,
            )
            .order_by(decisions_table.c.id.desc())
            .limit(limit)
        )
        async with self._database.engine.connect() as connection:
            rows = (await connection.execute(statement)).mappings().all()
        return [Decision.model_validate(dict(row)) for row in rows]


class RiskStateStore:
    """The single-row account brake state: breakers plus paused engines.

    Saved whenever the snapshot changes (the worker checks once per closed
    candle, so a change persists within one candle of happening) and loaded
    before trading resumes — a deploy must never silently release a tripped
    breaker, reset a daily-loss anchor, or resume a killed bot.
    """

    ROW_ID = 1
    """The production bot's row. Competition accounts persist their own
    brake state under the fixed row ids their lineup entries declare."""

    def __init__(self, database: Database, row_id: int = ROW_ID) -> None:
        """Bind the store to ``database``, scoped to one bot's ``row_id``."""
        self._database = database
        self._row_id = row_id

    async def save(self, state: BreakerState, paused_symbols: Sequence[str], at: datetime) -> None:
        """Upsert this bot's one risk-state row."""
        _require_aware(at)
        row = state.model_dump()
        row["id"] = self._row_id
        row["paused_symbols"] = list(paused_symbols)
        row["updated_at"] = at
        statement = pg_insert(risk_state_table)
        statement = statement.on_conflict_do_update(
            index_elements=["id"],
            # Reference the excluded (inserted) row rather than re-binding
            # literals: the statement shape stays constant across saves, so
            # the driver can cache its plan.
            set_={name: statement.excluded[name] for name in row if name != "id"},
        )
        async with self._database.engine.begin() as connection:
            await connection.execute(statement, [row])

    async def load(self) -> tuple[BreakerState, tuple[str, ...]] | None:
        """Return the persisted state and paused symbols, or ``None`` if fresh."""
        statement = select(risk_state_table).where(risk_state_table.c.id == self._row_id)
        async with self._database.engine.connect() as connection:
            row = (await connection.execute(statement)).mappings().first()
        if row is None:
            return None
        data = dict(row)
        paused = tuple(data.pop("paused_symbols"))
        data.pop("id")
        data.pop("updated_at")
        return BreakerState.model_validate(data), paused


class TradingFeesStore:
    """The single-row operator-set trading fees (buy/sell, in basis points).

    Absent until an operator first saves fees: until then the worker uses the
    boot defaults from config. Buy and sell fees are kept exact as ``Decimal``
    bps, never floats, like every other money-touching value.
    """

    ROW_ID = 1

    def __init__(self, database: Database) -> None:
        """Bind the store to ``database``."""
        self._database = database

    async def save(self, buy_fee_bps: Decimal, sell_fee_bps: Decimal, at: datetime) -> None:
        """Upsert the one trading-fees row."""
        _require_aware(at)
        row = {
            "id": self.ROW_ID,
            "buy_fee_bps": buy_fee_bps,
            "sell_fee_bps": sell_fee_bps,
            "updated_at": at,
        }
        statement = pg_insert(trading_fees_table)
        statement = statement.on_conflict_do_update(
            index_elements=["id"],
            set_={name: statement.excluded[name] for name in row if name != "id"},
        )
        async with self._database.engine.begin() as connection:
            await connection.execute(statement, [row])

    async def load(self) -> tuple[Decimal, Decimal] | None:
        """Return ``(buy_fee_bps, sell_fee_bps)``, or ``None`` if never set."""
        statement = select(
            trading_fees_table.c.buy_fee_bps, trading_fees_table.c.sell_fee_bps
        ).where(trading_fees_table.c.id == self.ROW_ID)
        async with self._database.engine.connect() as connection:
            row = (await connection.execute(statement)).first()
        if row is None:
            return None
        return Decimal(row[0]), Decimal(row[1])


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


FIRST_CUSTOM_RISK_ROW = 100
"""Where custom bots' ``risk_state`` rows start. The built-in lineup owns
the low ids (1-5 today); the gap leaves room for future built-ins without
ever colliding with a user's bot."""


class CustomBotStore:
    """User-built competition bots: the persisted recipes the worker runs.

    Journals are NOT deleted with a bot — fills/orders/decisions stay
    queryable under its bot_id forever, and a recreated bot with the same
    name gets a fresh id so histories never merge.
    """

    def __init__(self, database: Database) -> None:
        """Bind the store to ``database``."""
        self._database = database

    async def create(
        self,
        bot_id: str,
        label: str,
        description: str,
        rules: Mapping[str, Any],
        created_at: datetime,
    ) -> int:
        """Persist a new bot; returns its allocated ``risk_state`` row id.

        Raises ``ValueError`` when the id is already taken — bot ids are
        forever (journals are keyed by them), so collisions must be loud.
        """
        _require_aware(created_at)
        async with self._database.engine.begin() as connection:
            existing = (
                await connection.execute(
                    select(custom_bots_table.c.bot_id).where(custom_bots_table.c.bot_id == bot_id)
                )
            ).first()
            if existing is not None:
                raise ValueError(f"a bot named {bot_id!r} already exists")
            current_max: int | None = (
                await connection.execute(select(func.max(custom_bots_table.c.risk_state_row_id)))
            ).scalar()
            risk_row = max(FIRST_CUSTOM_RISK_ROW - 1, current_max or 0) + 1
            await connection.execute(
                custom_bots_table.insert(),
                [
                    {
                        "bot_id": bot_id,
                        "label": label,
                        "description": description,
                        "rules": dict(rules),
                        "risk_state_row_id": risk_row,
                        "created_at": created_at,
                    }
                ],
            )
        return risk_row

    async def list_all(self) -> list[dict[str, Any]]:
        """Return every bot in creation order (the lineup display order)."""
        statement = select(custom_bots_table).order_by(custom_bots_table.c.created_at)
        async with self._database.engine.connect() as connection:
            rows = (await connection.execute(statement)).mappings().all()
        return [dict(row) for row in rows]

    async def update_rules(self, bot_id: str, rules: Mapping[str, Any]) -> None:
        """Replace ``bot_id``'s recipe; raises ``KeyError`` if unknown."""
        statement = (
            custom_bots_table.update()
            .where(custom_bots_table.c.bot_id == bot_id)
            .values(rules=dict(rules))
        )
        async with self._database.engine.begin() as connection:
            result = await connection.execute(statement)
        if result.rowcount == 0:
            raise KeyError(f"no custom bot {bot_id!r}")

    async def delete(self, bot_id: str) -> None:
        """Remove the bot's recipe (its journals stay); ``KeyError`` if unknown."""
        statement = custom_bots_table.delete().where(custom_bots_table.c.bot_id == bot_id)
        async with self._database.engine.begin() as connection:
            result = await connection.execute(statement)
        if result.rowcount == 0:
            raise KeyError(f"no custom bot {bot_id!r}")
