"""Replay materialization: the viewer must show exactly what the run saw."""

import os
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from tradebot.core.models import Candle, CandleInterval
from tradebot.evaluation.replay import load_replay, slice_replay
from tradebot.marketdata import aggregate_candles
from tradebot.persistence import CandleStore, Database
from tradebot.persistence.database import metadata

BASE_TIME = datetime(2026, 1, 2, 0, 0, tzinfo=UTC)
DEFAULT_URL = "postgresql+asyncpg://postgres:test@localhost:5432/tradebot_test"


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


def make_m1_candles(count: int, start: datetime = BASE_TIME) -> list[Candle]:
    return [
        Candle(
            symbol="BTC/USDT",
            interval=CandleInterval.M1,
            open_time=start + timedelta(minutes=index),
            close_time=start + timedelta(minutes=index + 1),
            open_quote=Decimal(100 + index),
            high_quote=Decimal(101 + index),
            low_quote=Decimal(99 + index),
            close_quote=Decimal(100 + index),
            volume_base=Decimal(1),
        )
        for index in range(count)
    ]


class TestSliceReplay:
    def test_window_closes_at_decision_and_horizon_opens_there(self) -> None:
        series = make_m1_candles(100)
        decision_time = BASE_TIME + timedelta(minutes=60)

        window, horizon = slice_replay(series, decision_time, 60, 30)

        assert len(window) == 60
        assert len(horizon) == 30
        assert window[-1].close_time == decision_time
        assert horizon[0].open_time == decision_time

    def test_short_series_yields_short_slices_not_errors(self) -> None:
        series = make_m1_candles(10)
        decision_time = BASE_TIME + timedelta(minutes=6)

        window, horizon = slice_replay(series, decision_time, 60, 30)

        assert len(window) == 6  # only what exists before the decision
        assert len(horizon) == 4  # only what exists after

    def test_storage_gap_around_the_decision_is_tolerated(self) -> None:
        series = [
            candle
            for candle in make_m1_candles(40)
            if not (15 <= (candle.open_time - BASE_TIME).total_seconds() // 60 < 25)
        ]
        decision_time = BASE_TIME + timedelta(minutes=20)

        window, horizon = slice_replay(series, decision_time, 60, 30)

        assert len(window) == 15
        assert len(horizon) == 15
        assert all(candle.close_time <= decision_time for candle in window)
        assert all(candle.open_time >= decision_time for candle in horizon)


class TestLoadReplay:
    async def test_m1_replay_round_trips_through_the_store(self, database: Database) -> None:
        store = CandleStore(database)
        await store.insert_batch(make_m1_candles(100))
        decision_time = BASE_TIME + timedelta(minutes=60)

        window, horizon = await load_replay(store, "BTC/USDT", "1m", decision_time, 60, 30)

        assert len(window) == 60
        assert len(horizon) == 30
        assert window[-1].close_time == decision_time
        assert horizon[0].open_time == decision_time

    async def test_aggregated_replay_matches_full_history_aggregation(
        self, database: Database
    ) -> None:
        """Subrange aggregation must reproduce the run's candles byte for byte.

        The run aggregated the whole history in one pass; the replay only
        fetches the scenario's neighborhood. Epoch-aligned buckets make the
        two identical — this is the proof.
        """
        store = CandleStore(database)
        base = make_m1_candles(60)
        await store.insert_batch(base)
        full_series = aggregate_candles(base, CandleInterval.M5)
        decision_time = BASE_TIME + timedelta(minutes=30)

        window, horizon = await load_replay(store, "BTC/USDT", "5m", decision_time, 6, 4)

        assert window == full_series[:6]
        assert horizon == full_series[6:10]
        assert window[-1].close_time == decision_time
        assert horizon[0].open_time == decision_time

    async def test_unknown_timeframe_raises(self, database: Database) -> None:
        store = CandleStore(database)
        with pytest.raises(ValueError):
            await load_replay(store, "BTC/USDT", "7m", BASE_TIME, 60, 30)
