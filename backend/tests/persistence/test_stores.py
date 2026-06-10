from datetime import UTC, datetime, timedelta
from decimal import Decimal

from tradebot.core.models import Candle, CandleInterval, Fill, Side
from tradebot.persistence import CandleStore, Database, FillStore

BASE_TIME = datetime(2026, 1, 2, 0, 0, tzinfo=UTC)


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

    async def test_latest_open_time_for_backfill_resume(self, database: Database) -> None:
        store = CandleStore(database)
        assert await store.latest_open_time("BTC/USDT", CandleInterval.M1) is None

        await store.insert_batch([make_candle(0), make_candle(5)])
        latest = await store.latest_open_time("BTC/USDT", CandleInterval.M1)
        assert latest == BASE_TIME + timedelta(minutes=5)

    async def test_empty_batch_is_a_noop(self, database: Database) -> None:
        await CandleStore(database).insert_batch([])


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
