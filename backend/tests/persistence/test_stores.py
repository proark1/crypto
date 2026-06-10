from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from tradebot.core.models import Candle, CandleInterval, Decision, DecisionOutcome, Fill, Side
from tradebot.persistence import CandleStore, CoinStore, Database, DecisionStore, FillStore
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
