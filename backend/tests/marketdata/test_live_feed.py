"""Live feed tests: scripted fake exchange, real candle store, real bus."""

import os
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from tradebot.core.events import CandleClosed, EventBus
from tradebot.core.models import CandleInterval
from tradebot.marketdata.live_feed import LiveMarketDataFeed, OhlcvCandleTracker, OhlcvRow
from tradebot.persistence import CandleStore, Database
from tradebot.persistence.database import metadata

BASE_TIME = datetime(2026, 1, 2, 0, 0, tzinfo=UTC)
BASE_MS = int(BASE_TIME.timestamp() * 1000)
MINUTE_MS = 60_000
DEFAULT_URL = "postgresql+asyncpg://postgres:test@localhost:5432/tradebot_test"


def row(minute: int, close: float = 100.0, volume: float = 1.0) -> list[float]:
    return [BASE_MS + minute * MINUTE_MS, 100.0, 101.0, 99.0, close, volume]


@pytest.fixture
async def database() -> AsyncIterator[Database]:
    url = os.environ.get("TEST_DATABASE_URL", DEFAULT_URL)
    db = Database(url)
    try:
        async with db.engine.begin() as connection:
            await connection.run_sync(metadata.drop_all)
            await connection.run_sync(metadata.create_all)
    except Exception as error:  # pragma: no cover - environment-dependent
        await db.engine.dispose()
        pytest.skip(f"Postgres unavailable at {url}: {error}")
    async with db:
        yield db


class FakeExchange:
    """Plays back scripted watch_ohlcv results; raising entries simulate drops."""

    def __init__(
        self,
        watch_script: list[list[OhlcvRow] | Exception],
        rest_rows: list[OhlcvRow] | None = None,
        page_limit: int | None = None,
    ) -> None:
        self.watch_script = list(watch_script)
        self.rest_rows = rest_rows or []
        self.page_limit = page_limit
        self.rest_calls: list[int | None] = []

    async def watch_ohlcv(self, symbol: str, timeframe: str) -> list[OhlcvRow]:
        if not self.watch_script:
            raise asyncio_stop_signal()
        item = self.watch_script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    async def fetch_ohlcv(
        self, symbol: str, timeframe: str, since: int | None = None, limit: int | None = None
    ) -> list[OhlcvRow]:
        self.rest_calls.append(since)
        rows = self.rest_rows if since is None else [r for r in self.rest_rows if r[0] >= since]
        if self.page_limit is not None:
            rows = rows[: self.page_limit]
        return rows


class StopFeed(Exception):
    pass


def asyncio_stop_signal() -> StopFeed:
    return StopFeed("script exhausted")


async def run_feed_until_script_ends(feed: LiveMarketDataFeed, exchange: FakeExchange) -> None:
    """Run the feed loop; a script-exhaustion error stops it cleanly."""
    original = exchange.watch_ohlcv

    async def watch_or_stop(symbol: str, timeframe: str) -> list[OhlcvRow]:
        if not exchange.watch_script:
            feed.stop()
            return []
        return await original(symbol, timeframe)

    exchange.watch_ohlcv = watch_or_stop  # type: ignore[method-assign]
    await feed.run()


class TestTracker:
    def test_emits_all_but_newest_bucket(self) -> None:
        tracker = OhlcvCandleTracker("BTC/USDT", CandleInterval.M1)
        closed = tracker.update([row(0), row(1), row(2)])
        assert [c.open_time for c in closed] == [BASE_TIME, BASE_TIME + timedelta(minutes=1)]

    def test_in_progress_candle_uses_final_values(self) -> None:
        tracker = OhlcvCandleTracker("BTC/USDT", CandleInterval.M1)
        tracker.update([row(0, close=100.0)])
        tracker.update([row(0, close=105.5)])  # same bucket, updated close
        (closed,) = tracker.update([row(1)])
        assert closed.close_quote == Decimal("105.5")

    def test_closed_candles_are_emitted_exactly_once(self) -> None:
        tracker = OhlcvCandleTracker("BTC/USDT", CandleInterval.M1)
        tracker.update([row(0), row(1)])
        again = tracker.update([row(0), row(1)])  # reconnect resends history
        assert again == []

    def test_reconnect_history_replay_is_ignored(self) -> None:
        tracker = OhlcvCandleTracker("BTC/USDT", CandleInterval.M1)
        tracker.update([row(0), row(1), row(2)])
        closed = tracker.update([row(0), row(1), row(2), row(3)])
        assert [c.open_time for c in closed] == [BASE_TIME + timedelta(minutes=2)]

    def test_rows_with_none_fields_are_dropped_not_crashed(self) -> None:
        tracker = OhlcvCandleTracker("BTC/USDT", CandleInterval.M1)
        broken: list[float | None] = [BASE_MS, 100.0, 101.0, 99.0, 100.0, None]
        closed = tracker.update([broken, row(1), row(2)])  # type: ignore[list-item]
        # The broken bucket is dropped entirely; the stream keeps flowing.
        assert [c.open_time for c in closed] == [BASE_TIME + timedelta(minutes=1)]


class TestFeed:
    async def test_closed_candles_are_persisted_and_published(self, database: Database) -> None:
        store = CandleStore(database)
        bus = EventBus()
        received: list[CandleClosed] = []

        async def on_candle(event: CandleClosed) -> None:
            received.append(event)

        bus.subscribe(CandleClosed, on_candle)
        exchange = FakeExchange([[row(0), row(1)], [row(1), row(2)]])
        feed = LiveMarketDataFeed(exchange, "BTC/USDT", store, bus, reconnect_delays_seconds=(0,))
        await run_feed_until_script_ends(feed, exchange)

        stored = await store.fetch_range(
            "BTC/USDT", CandleInterval.M1, BASE_TIME, BASE_TIME + timedelta(minutes=10)
        )
        assert [c.open_time for c in stored] == [BASE_TIME, BASE_TIME + timedelta(minutes=1)]
        assert [e.candle.open_time for e in received] == [c.open_time for c in stored]

    async def test_stream_error_triggers_backfill_and_resume(self, database: Database) -> None:
        store = CandleStore(database)
        bus = EventBus()
        # Stream delivers minute 0+1, drops, REST repairs 1-2, stream resumes at 3.
        exchange = FakeExchange(
            watch_script=[[row(0), row(1)], ConnectionError("ws drop"), [row(3), row(4)]],
            rest_rows=[row(1), row(2), row(3)],  # last row in progress, dropped
        )
        feed = LiveMarketDataFeed(exchange, "BTC/USDT", store, bus, reconnect_delays_seconds=(0,))
        await run_feed_until_script_ends(feed, exchange)

        stored = await store.fetch_range(
            "BTC/USDT", CandleInterval.M1, BASE_TIME, BASE_TIME + timedelta(minutes=10)
        )
        assert [c.open_time for c in stored] == [
            BASE_TIME,
            BASE_TIME + timedelta(minutes=1),
            BASE_TIME + timedelta(minutes=2),
            BASE_TIME + timedelta(minutes=3),
        ]
        # Startup backfill (None, then resume page) plus the post-disconnect repair.
        assert exchange.rest_calls == [None, BASE_MS + 3 * MINUTE_MS, BASE_MS + 3 * MINUTE_MS]

    async def test_malformed_candles_are_quarantined(self, database: Database) -> None:
        store = CandleStore(database)
        bus = EventBus()
        received: list[CandleClosed] = []

        async def on_candle(event: CandleClosed) -> None:
            received.append(event)

        bus.subscribe(CandleClosed, on_candle)
        bad = [BASE_MS, 100.0, 90.0, 99.0, 100.0, 1.0]  # high < low: impossible
        exchange = FakeExchange([[bad, row(1)], [row(1), row(2)]])
        feed = LiveMarketDataFeed(exchange, "BTC/USDT", store, bus, reconnect_delays_seconds=(0,))
        await run_feed_until_script_ends(feed, exchange)

        stored = await store.fetch_range(
            "BTC/USDT", CandleInterval.M1, BASE_TIME, BASE_TIME + timedelta(minutes=10)
        )
        assert [c.open_time for c in stored] == [BASE_TIME + timedelta(minutes=1)]
        assert [e.candle.open_time for e in received] == [BASE_TIME + timedelta(minutes=1)]

    async def test_backfill_from_empty_store_drops_in_progress_row(
        self, database: Database
    ) -> None:
        store = CandleStore(database)
        exchange = FakeExchange([], rest_rows=[row(0), row(1), row(2)])
        feed = LiveMarketDataFeed(exchange, "BTC/USDT", store, EventBus())

        inserted = await feed.backfill()
        assert inserted == 2  # row(2) is in progress
        assert exchange.rest_calls == [None, BASE_MS + 2 * MINUTE_MS]  # final caught-up page

    async def test_backfill_resumes_after_latest_stored(self, database: Database) -> None:
        store = CandleStore(database)
        exchange = FakeExchange([], rest_rows=[row(0), row(1), row(2), row(3)])
        feed = LiveMarketDataFeed(exchange, "BTC/USDT", store, EventBus())
        await feed.backfill()  # stores 0..2

        exchange.rest_rows = [row(3), row(4), row(5)]
        inserted = await feed.backfill()
        assert inserted == 2  # 3 and 4; 5 in progress
        assert BASE_MS + 3 * MINUTE_MS in exchange.rest_calls  # resumed after stored minute 2

    async def test_backfill_paginates_through_long_outages(self, database: Database) -> None:
        store = CandleStore(database)
        exchange = FakeExchange([], rest_rows=[row(minute) for minute in range(10)], page_limit=4)
        feed = LiveMarketDataFeed(exchange, "BTC/USDT", store, EventBus())

        inserted = await feed.backfill()
        assert inserted == 9  # minutes 0-8; minute 9 is in progress
        stored = await store.fetch_range(
            "BTC/USDT", CandleInterval.M1, BASE_TIME, BASE_TIME + timedelta(minutes=60)
        )
        assert len(stored) == 9
        assert len(exchange.rest_calls) > 2  # genuinely paged, not one big fetch

    async def test_run_backfills_on_startup_before_streaming(self, database: Database) -> None:
        store = CandleStore(database)
        exchange = FakeExchange([], rest_rows=[row(0), row(1), row(2)])
        feed = LiveMarketDataFeed(exchange, "BTC/USDT", store, EventBus())
        await run_feed_until_script_ends(feed, exchange)  # empty script: stops at once

        stored = await store.fetch_range(
            "BTC/USDT", CandleInterval.M1, BASE_TIME, BASE_TIME + timedelta(minutes=60)
        )
        assert [c.open_time for c in stored] == [BASE_TIME, BASE_TIME + timedelta(minutes=1)]

    async def test_first_backfill_reaches_back_history_days(self, database: Database) -> None:
        """An empty store with a history horizon starts the crawl in the past."""
        store = CandleStore(database)
        exchange = FakeExchange([], rest_rows=[row(i) for i in range(4)])
        feed = LiveMarketDataFeed(exchange, "BTC/USDT", store, EventBus(), history_days=365)

        inserted = await feed.backfill()

        assert inserted == 3  # the newest row is dropped as possibly in progress
        first_since = exchange.rest_calls[0]
        assert first_since is not None
        expected_floor = (datetime.now(UTC) - timedelta(days=365)).timestamp() * 1000
        assert abs(first_since - expected_floor) < 60_000  # within a minute

    async def test_shallow_stored_history_deepens_before_resuming_forward(
        self, database: Database
    ) -> None:
        """Stored-but-shallow history reaches for the horizon first, then
        resumes forward from the newest stored candle — a database that
        predates a deeper setting must not keep its sliver forever."""
        store = CandleStore(database)
        seed = FakeExchange([], rest_rows=[row(0), row(1)])
        await LiveMarketDataFeed(seed, "BTC/USDT", store, EventBus()).backfill()  # stores row 0

        exchange = FakeExchange([], rest_rows=[row(i) for i in range(4)])
        feed = LiveMarketDataFeed(exchange, "BTC/USDT", store, EventBus(), history_days=365)
        await feed.backfill()

        # First call reaches back to the 365-day horizon (deepening)...
        first_since = exchange.rest_calls[0]
        assert first_since is not None
        expected_floor = (datetime.now(UTC) - timedelta(days=365)).timestamp() * 1000
        assert abs(first_since - expected_floor) < 60_000  # within a minute
        # ...then the forward crawl resumes at the stored candle's successor.
        assert exchange.rest_calls[1] == BASE_MS + MINUTE_MS

    async def test_backfill_when_caught_up_inserts_nothing(self, database: Database) -> None:
        store = CandleStore(database)
        exchange = FakeExchange([], rest_rows=[row(0)])
        feed = LiveMarketDataFeed(exchange, "BTC/USDT", store, EventBus())
        assert await feed.backfill() == 0


class TestDeepenHistory:
    """Backward deepening: shallow existing history grows to the horizon."""

    @staticmethod
    def recent_row(minutes_ago: int, now_ms: int) -> list[float]:
        return [now_ms - minutes_ago * MINUTE_MS, 100.0, 101.0, 99.0, 100.0, 1.0]

    @staticmethod
    def now_ms() -> int:
        anchor = datetime.now(UTC).replace(second=0, microsecond=0)
        return int(anchor.timestamp() * 1000)

    async def insert_recent(self, store: CandleStore, now_ms: int, minutes_ago: list[int]) -> None:
        feed_rows = [self.recent_row(m, now_ms) for m in minutes_ago]
        from tradebot.marketdata.live_feed import _row_to_candle  # test-only import

        await store.insert_batch(
            [_row_to_candle(row, "BTC/USDT", CandleInterval.M1) for row in feed_rows]
        )

    async def test_shallow_history_is_deepened_to_the_horizon(self, database: Database) -> None:
        """A database that predates a deeper setting still gets the depth."""
        store = CandleStore(database)
        now = self.now_ms()
        # Stored: only the last 10 minutes (what a young deployment has).
        await self.insert_recent(store, now, list(range(10, 0, -1)))
        # The venue can serve far deeper history than what is stored.
        exchange = FakeExchange([], rest_rows=[self.recent_row(m, now) for m in range(300, 0, -1)])
        feed = LiveMarketDataFeed(exchange, "BTC/USDT", store, EventBus(), history_days=1)

        inserted = await feed.backfill()

        earliest = await store.earliest_open_time("BTC/USDT", CandleInterval.M1)
        assert earliest is not None
        # Deepened to the venue's depth (300 minutes), well past the stored 10.
        assert earliest == datetime.fromtimestamp((now - 300 * MINUTE_MS) / 1000, tz=UTC)
        assert inserted >= 290  # the deepened span, without double-counting stored rows

    async def test_history_already_at_the_horizon_is_left_alone(self, database: Database) -> None:
        store = CandleStore(database)
        now = self.now_ms()
        two_days_back = 2 * 24 * 60
        await self.insert_recent(store, now, [two_days_back, 2, 1])
        exchange = FakeExchange(
            [], rest_rows=[self.recent_row(m, now) for m in range(3 * 24 * 60, 0, 60)]
        )
        feed = LiveMarketDataFeed(exchange, "BTC/USDT", store, EventBus(), history_days=1)

        await feed.backfill()

        earliest = await store.earliest_open_time("BTC/USDT", CandleInterval.M1)
        # Nothing older than the pre-existing earliest appeared: the stored
        # history already reaches past the 1-day horizon.
        assert earliest == datetime.fromtimestamp((now - two_days_back * MINUTE_MS) / 1000, tz=UTC)
