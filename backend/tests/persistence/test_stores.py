from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import text

from tradebot.core.models import (
    Candle,
    CandleInterval,
    Decision,
    DecisionOutcome,
    Fill,
    FundingRate,
    Order,
    OrderType,
    ProtectiveExitPlan,
    Side,
)
from tradebot.persistence import (
    CandleStore,
    CoinStore,
    Database,
    DecisionStore,
    FillStore,
    FundingStore,
    OrderStore,
)
from tradebot.persistence.database import coerce_async_dsn

BASE_TIME = datetime(2026, 1, 2, 0, 0, tzinfo=UTC)


class TestDsnCoercion:
    def test_platform_default_schemes_become_asyncpg(self) -> None:
        """Railway/Heroku-style DSNs must not crash the deploy on psycopg2."""
        schemes = (
            "postgres://",
            "postgresql://",
            "postgresql+psycopg2://",
            "postgresql+psycopg://",
            "POSTGRESQL://",  # schemes are case-insensitive, RFC 3986
            "  postgresql://",  # pasted env vars carry stray whitespace
        )
        for scheme in schemes:
            coerced = coerce_async_dsn(f"{scheme}user:pass@host:5432/db")
            assert coerced == "postgresql+asyncpg://user:pass@host:5432/db"

    def test_asyncpg_dsn_passes_through_unchanged(self) -> None:
        url = "postgresql+asyncpg://user:pass@host:5432/db"
        assert coerce_async_dsn(url) == url

    def test_engine_resolves_to_asyncpg_from_plain_scheme(self) -> None:
        database = Database("postgresql://user:pass@host:5432/db")
        assert database.engine.url.drivername == "postgresql+asyncpg"


def make_candle(minute: int, close: str = "100.5") -> Candle:
    open_time = BASE_TIME + timedelta(minutes=minute)
    return Candle(
        symbol="BTC/USDT",
        interval=CandleInterval.M1,
        open_time=open_time,
        close_time=open_time + timedelta(minutes=1),
        open_quote=Decimal("100"),
        high_quote=Decimal("101.25"),
        low_quote=Decimal("99.75"),
        close_quote=Decimal(close),
        volume_base=Decimal("2.5"),
    )


def make_fill(order_id: str = "ord-1", minute: int = 0) -> Fill:
    return Fill(
        client_order_id=order_id,
        symbol="BTC/USDT",
        side=Side.BUY,
        price_quote=Decimal("100.123456789012"),
        quantity_base=Decimal("0.00012345"),
        fee_quote=Decimal("0.01"),
        filled_at=BASE_TIME + timedelta(minutes=minute),
    )


class TestCandleStore:
    async def test_round_trip_preserves_exact_values(self, database: Database) -> None:
        store = CandleStore(database)
        original = make_candle(0, close="12345.678901234567890123")
        await store.insert_batch([original])

        (loaded,) = await store.fetch_range(
            "BTC/USDT", CandleInterval.M1, BASE_TIME, BASE_TIME + timedelta(minutes=1)
        )
        assert loaded == original  # Decimal-exact, timezone-aware, field-for-field

    async def test_reinserting_overlap_is_idempotent(self, database: Database) -> None:
        store = CandleStore(database)
        await store.insert_batch([make_candle(0), make_candle(1)])
        await store.insert_batch([make_candle(1), make_candle(2)])  # overlap

        candles = await store.fetch_range(
            "BTC/USDT", CandleInterval.M1, BASE_TIME, BASE_TIME + timedelta(minutes=10)
        )
        assert len(candles) == 3

    async def test_fetch_range_is_half_open_and_ordered(self, database: Database) -> None:
        store = CandleStore(database)
        await store.insert_batch([make_candle(2), make_candle(0), make_candle(1)])

        candles = await store.fetch_range(
            "BTC/USDT",
            CandleInterval.M1,
            BASE_TIME,
            BASE_TIME + timedelta(minutes=2),  # excludes minute 2
        )
        assert [c.open_time for c in candles] == [
            BASE_TIME,
            BASE_TIME + timedelta(minutes=1),
        ]

    async def test_intervals_are_isolated(self, database: Database) -> None:
        store = CandleStore(database)
        one_minute = make_candle(0)
        five_minute = Candle(
            symbol="BTC/USDT",
            interval=CandleInterval.M5,
            open_time=BASE_TIME,
            close_time=BASE_TIME + timedelta(minutes=5),
            open_quote=Decimal("100"),
            high_quote=Decimal("101"),
            low_quote=Decimal("99"),
            close_quote=Decimal("100"),
            volume_base=Decimal("10"),
        )
        await store.insert_batch([one_minute, five_minute])

        fetched = await store.fetch_range(
            "BTC/USDT", CandleInterval.M5, BASE_TIME, BASE_TIME + timedelta(hours=1)
        )
        assert [c.interval for c in fetched] == [CandleInterval.M5]

    async def test_fetch_recent_returns_newest_in_chronological_order(
        self, database: Database
    ) -> None:
        store = CandleStore(database)
        await store.insert_batch([make_candle(minute) for minute in range(5)])

        recent = await store.fetch_recent("BTC/USDT", CandleInterval.M1, limit=3)
        assert [c.open_time for c in recent] == [
            BASE_TIME + timedelta(minutes=2),
            BASE_TIME + timedelta(minutes=3),
            BASE_TIME + timedelta(minutes=4),
        ]

    async def test_latest_open_time_for_backfill_resume(self, database: Database) -> None:
        store = CandleStore(database)
        assert await store.latest_open_time("BTC/USDT", CandleInterval.M1) is None

        await store.insert_batch([make_candle(0), make_candle(5)])
        latest = await store.latest_open_time("BTC/USDT", CandleInterval.M1)
        assert latest == BASE_TIME + timedelta(minutes=5)

    async def test_empty_batch_is_a_noop(self, database: Database) -> None:
        await CandleStore(database).insert_batch([])

    async def test_naive_range_bounds_are_rejected(self, database: Database) -> None:
        store = CandleStore(database)
        naive = datetime(2026, 1, 2, 0, 0)
        with pytest.raises(ValueError, match="naive datetime"):
            await store.fetch_range("BTC/USDT", CandleInterval.M1, naive, BASE_TIME)
        with pytest.raises(ValueError, match="naive datetime"):
            await store.fetch_range("BTC/USDT", CandleInterval.M1, BASE_TIME, naive)


def _funding(hours: int, rate: str = "0.0001", symbol: str = "BTC/USDT") -> FundingRate:
    return FundingRate(
        symbol=symbol, funding_time=BASE_TIME + timedelta(hours=hours), rate=Decimal(rate)
    )


class TestFundingStore:
    async def test_round_trip_preserves_signed_exact_values(self, database: Database) -> None:
        store = FundingStore(database)
        original = _funding(0, rate="-0.00012345678901234567")  # shorts pay longs; exact
        await store.insert_batch([original])

        (loaded,) = await store.fetch_range("BTC/USDT", BASE_TIME, BASE_TIME + timedelta(hours=1))
        assert loaded == original  # Decimal-exact, signed, timezone-aware

    async def test_reinserting_overlap_is_idempotent(self, database: Database) -> None:
        store = FundingStore(database)
        await store.insert_batch([_funding(0), _funding(8)])
        await store.insert_batch([_funding(8), _funding(16)])  # overlap on resume

        rows = await store.fetch_range("BTC/USDT", BASE_TIME, BASE_TIME + timedelta(hours=24))
        assert len(rows) == 3

    async def test_fetch_range_is_half_open_and_ordered(self, database: Database) -> None:
        store = FundingStore(database)
        await store.insert_batch([_funding(16), _funding(0), _funding(8)])

        rows = await store.fetch_range(
            "BTC/USDT",
            BASE_TIME,
            BASE_TIME + timedelta(hours=16),  # excludes hour 16
        )
        assert [r.funding_time for r in rows] == [BASE_TIME, BASE_TIME + timedelta(hours=8)]

    async def test_symbols_are_isolated(self, database: Database) -> None:
        store = FundingStore(database)
        await store.insert_batch([_funding(0, symbol="BTC/USDT"), _funding(0, symbol="ETH/USDT")])

        rows = await store.fetch_range("ETH/USDT", BASE_TIME, BASE_TIME + timedelta(hours=1))
        assert [r.symbol for r in rows] == ["ETH/USDT"]

    async def test_latest_funding_time_for_resume(self, database: Database) -> None:
        store = FundingStore(database)
        assert await store.latest_funding_time("BTC/USDT") is None  # empty: cold start

        await store.insert_batch([_funding(0), _funding(8)])
        assert await store.latest_funding_time("BTC/USDT") == BASE_TIME + timedelta(hours=8)


class TestCoinStore:
    async def test_seed_runs_only_on_an_empty_table(self, database: Database) -> None:
        store = CoinStore(database)
        assert await store.seed_if_empty(["BTC/USDT", "ETH/USDT"], BASE_TIME) is True
        # A later boot with a different env var must not resurrect coins.
        assert await store.seed_if_empty(["BTC/USDT", "DOGE/USDT"], BASE_TIME) is False
        assert await store.list_symbols() == ("BTC/USDT", "ETH/USDT")

    async def test_add_remove_round_trip_in_added_order(self, database: Database) -> None:
        store = CoinStore(database)
        await store.add("BTC/USDT", BASE_TIME)
        await store.add("ETH/USDT", BASE_TIME + timedelta(minutes=1))
        await store.add("BTC/USDT", BASE_TIME + timedelta(minutes=2))  # idempotent

        assert await store.list_symbols() == ("BTC/USDT", "ETH/USDT")
        await store.remove("BTC/USDT")
        assert await store.list_symbols() == ("ETH/USDT",)

    async def test_naive_timestamps_are_rejected(self, database: Database) -> None:
        store = CoinStore(database)
        naive = datetime(2026, 1, 2, 0, 0)
        with pytest.raises(ValueError, match="naive datetime"):
            await store.add("BTC/USDT", naive)
        with pytest.raises(ValueError, match="naive datetime"):
            await store.seed_if_empty(["BTC/USDT"], naive)


class TestFillStore:
    async def test_round_trip_preserves_exact_values(self, database: Database) -> None:
        store = FillStore(database)
        original = make_fill()
        await store.append(original)

        (loaded,) = await store.fetch_all()
        assert loaded == original

    async def test_partial_fills_with_same_order_id_are_kept(self, database: Database) -> None:
        store = FillStore(database)
        await store.append(make_fill("ord-1", minute=0))
        await store.append(make_fill("ord-1", minute=0))  # second partial fill

        assert len(await store.fetch_all()) == 2

    async def test_fetch_preserves_execution_order(self, database: Database) -> None:
        store = FillStore(database)
        for minute in (3, 1, 2):
            await store.append(make_fill(f"ord-{minute}", minute=minute))

        fills = await store.fetch_all()
        assert [f.client_order_id for f in fills] == ["ord-3", "ord-1", "ord-2"]

    async def test_symbol_filter(self, database: Database) -> None:
        store = FillStore(database)
        await store.append(make_fill("ord-1"))
        other = make_fill("ord-2").model_copy(update={"symbol": "ETH/USDT"})
        await store.append(other)

        assert len(await store.fetch_all("ETH/USDT")) == 1

    async def test_fetch_page_returns_the_newest_window_in_execution_order(
        self, database: Database
    ) -> None:
        store = FillStore(database)
        for minute in range(5):
            await store.append(make_fill(f"ord-{minute}", minute=minute))

        page = await store.fetch_page(limit=2)
        # The two newest fills, but still oldest-first within the page.
        assert [fill.client_order_id for _, fill in page] == ["ord-3", "ord-4"]

    async def test_fetch_page_cursor_walks_backward(self, database: Database) -> None:
        store = FillStore(database)
        for minute in range(5):
            await store.append(make_fill(f"ord-{minute}", minute=minute))

        first = await store.fetch_page(limit=2)
        cursor = first[0][0]  # smallest id on the page
        older = await store.fetch_page(limit=2, before_id=cursor)
        assert [fill.client_order_id for _, fill in older] == ["ord-1", "ord-2"]
        # Walking off the start yields the remaining fills and then nothing.
        assert [
            fill.client_order_id for _, fill in await store.fetch_page(before_id=older[0][0])
        ] == ["ord-0"]

    async def test_fetch_page_symbol_filter(self, database: Database) -> None:
        store = FillStore(database)
        await store.append(make_fill("ord-btc"))
        await store.append(make_fill("ord-eth").model_copy(update={"symbol": "ETH/USDT"}))

        page = await store.fetch_page("ETH/USDT")
        assert [fill.client_order_id for _, fill in page] == ["ord-eth"]


def make_order(
    order_id: str = "ord-1",
    symbol: str = "BTC/USDT",
    order_type: OrderType = OrderType.MARKET,
    minute: int = 0,
) -> Order:
    is_resting = order_type != OrderType.MARKET
    return Order(
        client_order_id=order_id,
        signal_id=f"sig-{order_id}",
        symbol=symbol,
        side=Side.SELL,
        order_type=order_type,
        quantity_base=Decimal("0.00012345"),
        limit_price_quote=Decimal("94.123456789012") if is_resting else None,
        stop_price_quote=Decimal("95") if order_type == OrderType.STOP_LIMIT else None,
        created_at=BASE_TIME + timedelta(minutes=minute),
    )


class TestOrderStore:
    async def test_round_trip_preserves_exact_values(self, database: Database) -> None:
        store = OrderStore(database)
        original = make_order(order_type=OrderType.STOP_LIMIT)
        await store.record_submitted(original)

        (loaded,) = await store.fetch_open()
        assert loaded.order == original  # Decimal-exact, field-for-field
        assert loaded.triggered is False

    async def test_cross_bot_orders_with_the_same_id_do_not_collide(
        self, database: Database
    ) -> None:
        # The orders table is shared; the composite (client_order_id, bot_id)
        # key keeps two accounts' same-id orders — and their stops — separate,
        # where the old single-column key let one overwrite the other.
        bot_a = OrderStore(database, "bot_a")
        bot_b = OrderStore(database, "bot_b")
        await bot_a.record_submitted(make_order("ord-1", order_type=OrderType.STOP_LIMIT))
        await bot_b.record_submitted(make_order("ord-1"))

        # Both rows coexist; each store sees only its own.
        (a_open,) = await bot_a.fetch_open()
        (b_open,) = await bot_b.fetch_open()
        assert a_open.order.order_type == OrderType.STOP_LIMIT
        assert b_open.order.order_type == OrderType.MARKET

        # Closing one bot's order leaves the other's untouched.
        await bot_a.mark_filled("ord-1", BASE_TIME + timedelta(minutes=1))
        assert await bot_a.fetch_open() == []
        assert len(await bot_b.fetch_open()) == 1

    async def test_a_single_column_pk_database_is_widened_in_place(
        self, database: Database
    ) -> None:
        # Revert to the old single-column key to simulate a pre-competition
        # database, with a legacy production order already in it (inserted
        # raw, as the old code would have, under the single-column key).
        async with database.engine.begin() as connection:
            await connection.execute(text("ALTER TABLE orders DROP CONSTRAINT orders_pkey"))
            await connection.execute(text("ALTER TABLE orders ADD PRIMARY KEY (client_order_id)"))
            await connection.execute(
                text(
                    "INSERT INTO orders (client_order_id, bot_id, signal_id, symbol, side, "
                    "order_type, quantity_base, created_at, status, triggered, status_at) VALUES "
                    "('ord-legacy', 'production', 'sig', 'BTC/USDT', 'sell', 'market', 0.001, "
                    "'2026-01-02T00:00:00+00:00', 'open', false, '2026-01-02T00:00:00+00:00')"
                )
            )

        # The startup schema sync widens the key in place — safe, since a wider
        # key cannot be violated by rows the narrower one kept unique.
        await database.create_schema()

        # The legacy row survived, and a second bot can now hold the same id.
        await OrderStore(database, "challenger").record_submitted(make_order("ord-legacy"))
        production = {
            o.order.client_order_id for o in await OrderStore(database, "production").fetch_open()
        }
        challenger = {
            o.order.client_order_id for o in await OrderStore(database, "challenger").fetch_open()
        }
        assert production == {"ord-legacy"}
        assert challenger == {"ord-legacy"}

    async def test_terminal_orders_are_not_restorable(self, database: Database) -> None:
        store = OrderStore(database)
        await store.record_submitted(make_order("ord-filled"))
        await store.record_submitted(make_order("ord-cancelled"))
        await store.record_submitted(make_order("ord-open"))
        await store.mark_filled("ord-filled", BASE_TIME + timedelta(minutes=1))
        await store.mark_cancelled("ord-cancelled", BASE_TIME + timedelta(minutes=1))

        open_orders = await store.fetch_open()
        assert [o.order.client_order_id for o in open_orders] == ["ord-open"]

    async def test_resubmitted_intent_reopens_its_row(self, database: Database) -> None:
        """Deterministic ids: the same intent after a cancel is open again."""
        store = OrderStore(database)
        await store.record_submitted(make_order())
        await store.mark_cancelled("ord-1", BASE_TIME + timedelta(minutes=1))
        assert await store.fetch_open() == []

        await store.record_submitted(make_order())
        (reopened,) = await store.fetch_open()
        assert reopened.order.client_order_id == "ord-1"
        assert reopened.triggered is False  # the latch never survives a reopen

    async def test_trigger_latch_round_trips(self, database: Database) -> None:
        store = OrderStore(database)
        await store.record_submitted(make_order(order_type=OrderType.STOP_LIMIT))
        await store.mark_triggered("ord-1")

        (loaded,) = await store.fetch_open()
        assert loaded.triggered is True  # still open, but latched

    async def test_fill_journal_outranks_a_stale_open_row(self, database: Database) -> None:
        """Crash between the fill write and the status update: never restore."""
        store = OrderStore(database)
        await store.record_submitted(make_order())
        await FillStore(database).append(make_fill("ord-1"))  # status still "open"

        assert await store.fetch_open() == []

    async def test_protective_exit_plan_round_trips(self, database: Database) -> None:
        store = OrderStore(database)
        planned = make_order().model_copy(
            update={
                "protective_exit": ProtectiveExitPlan(
                    stop_price_quote=Decimal("95.000000000001"),
                    limit_price_quote=Decimal("94.525"),
                )
            }
        )
        await store.record_submitted(planned)

        (loaded,) = await store.fetch_open()
        assert loaded.order == planned  # plan included, Decimal-exact

    async def test_latest_filled_entry_with_plan_finds_the_recovery_source(
        self, database: Database
    ) -> None:
        store = OrderStore(database)
        plan = ProtectiveExitPlan(stop_price_quote=Decimal("95"), limit_price_quote=Decimal("94"))

        def planned_entry(order_id: str, minute: int) -> Order:
            return make_order(order_id, minute=minute).model_copy(
                update={"side": Side.BUY, "protective_exit": plan}
            )

        await store.record_submitted(planned_entry("ord-old", 0))
        await store.record_submitted(planned_entry("ord-new", 1))
        await store.record_submitted(planned_entry("ord-unfilled", 2))
        planless = make_order("ord-planless", minute=3).model_copy(update={"side": Side.BUY})
        await store.record_submitted(planless)
        await store.mark_filled("ord-old", BASE_TIME + timedelta(minutes=4))
        await store.mark_filled("ord-new", BASE_TIME + timedelta(minutes=5))
        await store.mark_filled("ord-planless", BASE_TIME + timedelta(minutes=6))

        entry = await store.latest_filled_entry_with_plan("BTC/USDT")
        assert entry is not None
        assert entry.client_order_id == "ord-new"  # newest filled, plan-bearing
        assert await store.latest_filled_entry_with_plan("ETH/USDT") is None

    async def test_symbol_filter_and_oldest_first_order(self, database: Database) -> None:
        store = OrderStore(database)
        await store.record_submitted(make_order("ord-newer", minute=2))
        await store.record_submitted(make_order("ord-older", minute=1))
        await store.record_submitted(make_order("ord-eth", symbol="ETH/USDT"))

        btc = await store.fetch_open("BTC/USDT")
        assert [o.order.client_order_id for o in btc] == ["ord-older", "ord-newer"]
        assert len(await store.fetch_open()) == 3


def make_decision(signal_id: str, outcome: DecisionOutcome) -> Decision:
    return Decision(
        signal_id=signal_id,
        strategy_name="trend_following",
        symbol="BTC/USDT",
        side=Side.BUY,
        stop_price_quote=Decimal("95.5"),
        reasons=("fast EMA crossed above slow EMA", "stop at 2 x ATR below close"),
        outcome=outcome,
        created_at=BASE_TIME,
    )


class TestDecisionStore:
    async def test_round_trip_preserves_reasons_and_outcome(self, database: Database) -> None:
        store = DecisionStore(database)
        original = make_decision("sig-1", DecisionOutcome.VETOED)
        await store.append(original)

        (loaded,) = await store.fetch_recent("BTC/USDT")
        assert loaded == original  # reasons tuple, Decimal stop, outcome enum

    async def test_fetch_recent_is_newest_first_and_limited(self, database: Database) -> None:
        store = DecisionStore(database)
        for index in range(5):
            await store.append(make_decision(f"sig-{index}", DecisionOutcome.SUBMITTED))

        recent = await store.fetch_recent("BTC/USDT", limit=2)
        assert [d.signal_id for d in recent] == ["sig-4", "sig-3"]

    async def test_symbols_are_isolated(self, database: Database) -> None:
        store = DecisionStore(database)
        await store.append(make_decision("sig-1", DecisionOutcome.SUBMITTED))
        assert await store.fetch_recent("ETH/USDT") == []


def rich_candle(
    at: datetime,
    open_quote: str,
    high: str,
    low: str,
    close: str,
    volume: str = "1",
    symbol: str = "BTC/USDT",
) -> Candle:
    return Candle(
        symbol=symbol,
        interval=CandleInterval.M1,
        open_time=at,
        close_time=at + timedelta(minutes=1),
        open_quote=Decimal(open_quote),
        high_quote=Decimal(high),
        low_quote=Decimal(low),
        close_quote=Decimal(close),
        volume_base=Decimal(volume),
    )


class TestChartBuckets:
    async def test_hour_buckets_keep_first_open_last_close_extremes_and_volume(
        self, database: Database
    ) -> None:
        store = CandleStore(database)
        first_hour = datetime(2026, 1, 2, 10, 0, tzinfo=UTC)
        await store.insert_batch(
            [
                rich_candle(first_hour, "100", "105", "98", "101", volume="2"),
                rich_candle(first_hour + timedelta(minutes=30), "101", "120", "99", "110"),
                rich_candle(first_hour + timedelta(minutes=59), "110", "112", "90", "95"),
                rich_candle(first_hour + timedelta(hours=1), "95", "96", "94", "96", volume="3"),
            ]
        )

        buckets = await store.fetch_recent_buckets("BTC/USDT", "hour")

        assert [bucket.open_time for bucket in buckets] == [
            first_hour,
            first_hour + timedelta(hours=1),
        ]
        full_hour = buckets[0]
        assert full_hour.open_quote == Decimal("100")  # first candle's open
        assert full_hour.close_quote == Decimal("95")  # last candle's close
        assert full_hour.high_quote == Decimal("120")
        assert full_hour.low_quote == Decimal("90")
        assert full_hour.volume_base == Decimal("4")

    async def test_week_and_month_buckets_split_on_calendar_boundaries(
        self, database: Database
    ) -> None:
        store = CandleStore(database)
        sunday = datetime(2026, 1, 4, 23, 59, tzinfo=UTC)
        monday = datetime(2026, 1, 5, 0, 0, tzinfo=UTC)  # next ISO week
        january_end = datetime(2026, 1, 31, 23, 59, tzinfo=UTC)
        february_start = datetime(2026, 2, 1, 0, 0, tzinfo=UTC)
        await store.insert_batch(
            [
                rich_candle(sunday, "1", "1", "1", "1"),
                rich_candle(monday, "2", "2", "2", "2"),
                rich_candle(january_end, "3", "3", "3", "3"),
                rich_candle(february_start, "4", "4", "4", "4"),
            ]
        )

        weeks = await store.fetch_recent_buckets("BTC/USDT", "week")
        months = await store.fetch_recent_buckets("BTC/USDT", "month")

        # The Sunday candle and the Monday candle land in different weeks
        # even though they are one minute apart (weeks start Monday).
        assert weeks[0].open_time == datetime(2025, 12, 29, tzinfo=UTC)
        assert weeks[1].open_time == datetime(2026, 1, 5, tzinfo=UTC)
        assert [month.open_time for month in months] == [
            datetime(2026, 1, 1, tzinfo=UTC),
            datetime(2026, 2, 1, tzinfo=UTC),
        ]

    async def test_limit_keeps_the_newest_buckets_oldest_first(self, database: Database) -> None:
        store = CandleStore(database)
        start = datetime(2026, 1, 2, 0, 0, tzinfo=UTC)
        await store.insert_batch(
            [rich_candle(start + timedelta(hours=hour), "1", "1", "1", "1") for hour in range(5)]
        )

        buckets = await store.fetch_recent_buckets("BTC/USDT", "hour", limit=2)

        assert [bucket.open_time for bucket in buckets] == [
            start + timedelta(hours=3),
            start + timedelta(hours=4),
        ]

    async def test_unknown_unit_raises_before_touching_sql(self, database: Database) -> None:
        store = CandleStore(database)
        with pytest.raises(ValueError, match="unknown bucket unit"):
            await store.fetch_recent_buckets("BTC/USDT", "fortnight")


class TestStrategySettingsStore:
    async def test_active_returns_the_newest_version_per_family(self, database: Database) -> None:
        from tradebot.persistence import StrategySettingsStore

        store = StrategySettingsStore(database)
        assert await store.active() == {}  # empty store means defaults

        await store.record("trend_following", {"fast_ema_period": 10}, BASE_TIME)
        await store.record("mean_reversion", {"rsi_period": 7}, BASE_TIME)
        newest = await store.record(
            "trend_following", {"fast_ema_period": 12}, BASE_TIME, source_sweep_id=42
        )

        active = await store.active()
        assert active == {
            "trend_following": {"fast_ema_period": 12},
            "mean_reversion": {"rsi_period": 7},
        }
        row = await store.fetch(newest)
        assert row is not None
        assert row["source_sweep_id"] == 42

    async def test_history_is_newest_first_and_fetch_misses_are_none(
        self, database: Database
    ) -> None:
        from tradebot.persistence import StrategySettingsStore

        store = StrategySettingsStore(database)
        first = await store.record("trend_following", {"fast_ema_period": 10}, BASE_TIME)
        second = await store.record(
            "trend_following", {"fast_ema_period": 12}, BASE_TIME, note="auto-promoted"
        )

        history = await store.history()
        assert [row["id"] for row in history] == [second, first]
        assert history[0]["note"] == "auto-promoted"
        assert await store.fetch(9999) is None


class TestCampaignHistoryStore:
    async def test_records_and_lists_newest_first(self, database: Database) -> None:
        from tradebot.persistence import CampaignHistoryStore

        store = CampaignHistoryStore(database)
        assert await store.list() == []  # nothing finished yet

        older = {
            "target": "production",
            "symbol": "BTC/USDT",
            "status": "completed",
            "promotions": 0,
            "stop_reason": "converged",
            "holdout_start": BASE_TIME.isoformat(),
            "started_at": BASE_TIME.isoformat(),
            "finished_at": BASE_TIME.isoformat(),
            "holdout_read": None,
            "rounds": [],
        }
        newer = {
            **older,
            "target": "momentum",
            "promotions": 1,
            "rounds": [
                {
                    "index": 0,
                    "scale": 1.0,
                    "sweep_id": 7,
                    "verdict": "validated",
                    "winner": "faster_macd",
                    "promoted_version": 5,
                    "note": "promoted momentum settings v5 (faster_macd)",
                    "changes": [{"field": "macd_fast", "before": "12", "after": "8"}],
                }
            ],
        }
        await store.record(older, BASE_TIME)
        await store.record(newer, BASE_TIME + timedelta(hours=1))

        history = await store.list()
        assert [entry["target"] for entry in history] == ["momentum", "production"]
        # The whole snapshot round-trips through JSONB unchanged, diff included.
        assert history[0]["promotions"] == 1
        assert history[0]["rounds"][0]["changes"] == [
            {"field": "macd_fast", "before": "12", "after": "8"}
        ]

    async def test_naive_finished_at_is_rejected(self, database: Database) -> None:
        from tradebot.persistence import CampaignHistoryStore

        store = CampaignHistoryStore(database)
        with pytest.raises(ValueError, match="naive datetime"):
            await store.record({"target": "x"}, datetime(2026, 1, 1))  # naive boundary


class TestRiskStateStore:
    async def test_round_trip_preserves_state_and_paused_symbols(self, database: Database) -> None:
        from tradebot.persistence import RiskStateStore
        from tradebot.risk import BreakerState

        store = RiskStateStore(database)
        state = BreakerState(
            tripped_reason="daily loss limit",
            day=BASE_TIME.date(),
            day_start_equity_quote=Decimal("10000.000000000001"),
            entries_today=3,
            peak_equity_quote=Decimal("12345.678901234567"),
            consecutive_losses=2,
            cooldown_until=BASE_TIME + timedelta(hours=4),
            last_observed_time=BASE_TIME,
        )
        await store.save(state, ["BTC/USDT", "ETH/USDT"], BASE_TIME)

        loaded = await store.load()
        assert loaded is not None
        assert loaded[0] == state  # Decimal-exact, timezone-aware
        assert loaded[1] == ("BTC/USDT", "ETH/USDT")

    async def test_save_overwrites_the_single_row(self, database: Database) -> None:
        from tradebot.persistence import RiskStateStore
        from tradebot.risk import BreakerState

        store = RiskStateStore(database)
        await store.save(BreakerState(entries_today=1), [], BASE_TIME)
        await store.save(BreakerState(entries_today=2), ["BTC/USDT"], BASE_TIME)

        loaded = await store.load()
        assert loaded is not None
        assert loaded[0].entries_today == 2
        assert loaded[1] == ("BTC/USDT",)

    async def test_fresh_database_loads_none(self, database: Database) -> None:
        from tradebot.persistence import RiskStateStore

        assert await RiskStateStore(database).load() is None


class TestSchemaSync:
    """Deploys must add new columns to shipped tables, not crash on them."""

    async def test_missing_column_is_added_on_create_schema(self, database: Database) -> None:
        from sqlalchemy import text

        # Simulate a deployed DB from before a column existed.
        async with database.engine.begin() as connection:
            await connection.execute(
                text('ALTER TABLE "orders" DROP COLUMN "protective_trail_distance_quote"')
            )
        await database.create_schema()  # the deploy-time sync

        store = OrderStore(database)
        planned = make_order().model_copy(
            update={
                "protective_exit": ProtectiveExitPlan(
                    stop_price_quote=Decimal("95"),
                    limit_price_quote=Decimal("94"),
                    trail_distance_quote=Decimal("2"),
                )
            }
        )
        await store.record_submitted(planned)  # would crash without the column
        (loaded,) = await store.fetch_open()
        assert loaded.order == planned

    async def test_not_null_without_server_default_is_refused_loudly(
        self, database: Database
    ) -> None:
        import pytest
        from sqlalchemy import Column, MetaData, Table, Text

        from tradebot.persistence.database import _add_missing_columns

        hostile = MetaData()
        Table("orders", hostile, Column("mandatory_no_default", Text, nullable=False))

        async with database.engine.begin() as connection:
            with pytest.raises(RuntimeError, match="server_default"):
                await connection.run_sync(
                    lambda sync_connection: _add_missing_columns(sync_connection, hostile)
                )


class TestBotScoping:
    """The strategy competition's journal isolation: one table, many accounts."""

    async def test_fills_are_isolated_per_bot(self, database: Database) -> None:
        production = FillStore(database)
        challenger = FillStore(database, bot_id="momentum")
        await production.append(make_fill("ord-prod"))
        await challenger.append(make_fill("ord-momentum/x"))

        assert [f.client_order_id for f in await production.fetch_all()] == ["ord-prod"]
        assert [f.client_order_id for f in await challenger.fetch_all()] == ["ord-momentum/x"]

    async def test_count_by_side_counts_only_this_bot(self, database: Database) -> None:
        production = FillStore(database)
        challenger = FillStore(database, bot_id="momentum")
        await production.append(make_fill("ord-1"))
        await production.append(make_fill("ord-2"))
        await challenger.append(make_fill("ord-momentum/1"))

        assert await production.count_by_side() == {"buy": 2}
        assert await challenger.count_by_side() == {"buy": 1}
        assert await FillStore(database, bot_id="breakout").count_by_side() == {}

    async def test_open_orders_restore_to_their_own_bot(self, database: Database) -> None:
        production = OrderStore(database)
        challenger = OrderStore(database, bot_id="momentum")
        await production.record_submitted(make_order("ord-prod", order_type=OrderType.LIMIT))
        await challenger.record_submitted(make_order("ord-momentum/x", order_type=OrderType.LIMIT))

        (restored,) = await production.fetch_open()
        assert restored.order.client_order_id == "ord-prod"
        (restored,) = await challenger.fetch_open()
        assert restored.order.client_order_id == "ord-momentum/x"

    async def test_recovery_entry_lookup_stays_inside_the_bot(self, database: Database) -> None:
        """A challenger's stop must never be rebuilt from the incumbent's plan."""
        from tradebot.core.models import ProtectiveExitPlan

        challenger = OrderStore(database, bot_id="momentum")
        entry = make_order("ord-prod-entry", order_type=OrderType.LIMIT).model_copy(
            update={
                "side": Side.BUY,
                "protective_exit": ProtectiveExitPlan(
                    stop_price_quote=Decimal("90"),
                    limit_price_quote=Decimal("89.5"),
                    breakeven_at_r=1.0,
                    trail_distance_quote=None,
                ),
            }
        )
        await OrderStore(database).record_submitted(entry)
        await OrderStore(database).mark_filled("ord-prod-entry", BASE_TIME)

        assert await challenger.latest_filled_entry_with_plan("BTC/USDT") is None

    async def test_decisions_are_isolated_per_bot(self, database: Database) -> None:
        production = DecisionStore(database)
        challenger = DecisionStore(database, bot_id="momentum")
        await production.append(make_decision("sig-prod", DecisionOutcome.SUBMITTED))
        await challenger.append(make_decision("sig-challenger", DecisionOutcome.VETOED))

        (decision,) = await production.fetch_recent("BTC/USDT")
        assert decision.signal_id == "sig-prod"
        (decision,) = await challenger.fetch_recent("BTC/USDT")
        assert decision.signal_id == "sig-challenger"

    async def test_risk_state_rows_are_isolated_per_bot(self, database: Database) -> None:
        from tradebot.persistence import RiskStateStore
        from tradebot.risk import BreakerState

        production = RiskStateStore(database)
        challenger = RiskStateStore(database, row_id=5)
        await production.save(BreakerState(entries_today=3), ["BTC/USDT"], BASE_TIME)
        await challenger.save(BreakerState(entries_today=7), [], BASE_TIME)

        loaded = await production.load()
        assert loaded is not None and loaded[0].entries_today == 3
        loaded = await challenger.load()
        assert loaded is not None and loaded[0].entries_today == 7


class TestCustomBotStore:
    async def test_create_list_round_trip_allocates_risk_rows(self, database: Database) -> None:
        from tradebot.persistence import CustomBotStore

        store = CustomBotStore(database)
        first_row = await store.create(
            "custom-dip-buyer", "Dip Buyer", "buys dips", {"entry_mode": "any"}, BASE_TIME
        )
        second_row = await store.create(
            "custom-confluence", "Confluence", "needs agreement", {"entry_mode": "all"}, BASE_TIME
        )

        assert first_row >= 100  # built-ins own the low ids
        assert second_row == first_row + 1
        bots = await store.list_all()
        assert [bot["bot_id"] for bot in bots] == ["custom-dip-buyer", "custom-confluence"]
        assert bots[0]["rules"] == {"entry_mode": "any"}

    async def test_duplicate_id_is_refused(self, database: Database) -> None:
        from tradebot.persistence import CustomBotStore

        store = CustomBotStore(database)
        await store.create("custom-x", "X", "", {}, BASE_TIME)
        with pytest.raises(ValueError, match="already exists"):
            await store.create("custom-x", "X again", "", {}, BASE_TIME)

    async def test_update_and_delete_unknown_are_loud(self, database: Database) -> None:
        from tradebot.persistence import CustomBotStore

        store = CustomBotStore(database)
        with pytest.raises(KeyError):
            await store.update_rules("custom-ghost", {})
        with pytest.raises(KeyError):
            await store.delete("custom-ghost")

    async def test_update_rules_round_trips(self, database: Database) -> None:
        from tradebot.persistence import CustomBotStore

        store = CustomBotStore(database)
        await store.create("custom-x", "X", "", {"entry_mode": "any"}, BASE_TIME)
        await store.update_rules("custom-x", {"entry_mode": "all"})
        (bot,) = await store.list_all()
        assert bot["rules"] == {"entry_mode": "all"}
