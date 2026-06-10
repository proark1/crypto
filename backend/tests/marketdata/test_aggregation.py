from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from tests.marketdata.conftest import BASE_TIME, MakeM1
from tradebot.core.models import CandleInterval
from tradebot.marketdata import TimeframeAggregator, aggregate_candles


def test_five_minutes_aggregate_into_one_5m_candle(make_m1: MakeM1) -> None:
    aggregator = TimeframeAggregator(CandleInterval.M5)
    candles = [
        make_m1(0, open_quote="100", high_quote="101", low_quote="99", close_quote="100"),
        make_m1(1, open_quote="100", high_quote="115", low_quote="98", close_quote="110"),
        make_m1(2, open_quote="110", high_quote="111", low_quote="85", close_quote="90"),
        make_m1(3, open_quote="90", high_quote="95", low_quote="89", close_quote="94"),
        make_m1(4, open_quote="94", high_quote="96", low_quote="93", close_quote="95"),
    ]
    assert all(aggregator.add(candle) is None for candle in candles)

    completed = aggregator.add(make_m1(5))
    assert completed is not None
    assert completed.interval == CandleInterval.M5
    assert completed.open_time == BASE_TIME
    assert completed.close_time == BASE_TIME + timedelta(minutes=5)
    assert completed.open_quote == Decimal("100")
    assert completed.high_quote == Decimal("115")
    assert completed.low_quote == Decimal("85")
    assert completed.close_quote == Decimal("95")
    assert completed.volume_base == Decimal("5")


def test_buckets_align_to_epoch_when_stream_starts_mid_bucket(make_m1: MakeM1) -> None:
    aggregator = TimeframeAggregator(CandleInterval.M5)
    assert aggregator.add(make_m1(3)) is None
    assert aggregator.add(make_m1(4)) is None

    completed = aggregator.add(make_m1(5))
    assert completed is not None
    assert completed.open_time == BASE_TIME  # bucket start, not first-seen candle
    assert completed.volume_base == Decimal("2")  # but only the seen volume


def test_missing_minutes_inside_bucket_are_tolerated(make_m1: MakeM1) -> None:
    aggregator = TimeframeAggregator(CandleInterval.M5)
    aggregator.add(make_m1(0, volume_base="1"))
    aggregator.add(make_m1(4, volume_base="3"))  # minutes 1-3 missing

    completed = aggregator.add(make_m1(5))
    assert completed is not None
    assert completed.volume_base == Decimal("4")


def test_gap_spanning_multiple_buckets_emits_only_the_open_bucket(make_m1: MakeM1) -> None:
    aggregator = TimeframeAggregator(CandleInterval.M5)
    aggregator.add(make_m1(0))
    completed = aggregator.add(make_m1(23))  # feed died for ~20 minutes

    assert completed is not None
    assert completed.open_time == BASE_TIME
    assert completed.volume_base == Decimal("1")  # never invents data for dead buckets


def test_daily_candles_align_to_midnight_utc(make_m1: MakeM1) -> None:
    aggregator = TimeframeAggregator(CandleInterval.D1)
    aggregator.add(make_m1(7 * 60))  # 07:00
    aggregator.add(make_m1(22 * 60))  # 22:00

    completed = aggregator.add(make_m1(24 * 60 + 30))  # next day 00:30
    assert completed is not None
    assert completed.open_time == datetime(2026, 1, 2, 0, 0, tzinfo=UTC)
    assert completed.close_time == datetime(2026, 1, 3, 0, 0, tzinfo=UTC)


def test_out_of_order_candle_raises(make_m1: MakeM1) -> None:
    aggregator = TimeframeAggregator(CandleInterval.M5)
    aggregator.add(make_m1(2))
    with pytest.raises(ValueError, match="out-of-order or duplicate"):
        aggregator.add(make_m1(1))


def test_duplicate_candle_raises(make_m1: MakeM1) -> None:
    aggregator = TimeframeAggregator(CandleInterval.M5)
    aggregator.add(make_m1(2))
    with pytest.raises(ValueError, match="out-of-order or duplicate"):
        aggregator.add(make_m1(2))


def test_symbol_mismatch_raises(make_m1: MakeM1) -> None:
    aggregator = TimeframeAggregator(CandleInterval.M5)
    aggregator.add(make_m1(0))
    with pytest.raises(ValueError, match="bound to BTC/USDT"):
        aggregator.add(make_m1(1, symbol="ETH/USDT"))


def test_non_1m_input_raises(make_m1: MakeM1) -> None:
    aggregator = TimeframeAggregator(CandleInterval.M15)
    five_minute = TimeframeAggregator(CandleInterval.M5)
    for minute in range(6):
        result = five_minute.add(make_m1(minute))
    assert result is not None
    with pytest.raises(ValueError, match="consumes 1m candles"):
        aggregator.add(result)


def test_1m_target_is_rejected() -> None:
    with pytest.raises(ValueError, match="coarser than the 1m base"):
        TimeframeAggregator(CandleInterval.M1)


class TestBatchAggregation:
    """The batch helper must mirror the incremental semantics exactly."""

    def test_unordered_duplicated_input_aggregates_cleanly(self, make_m1: MakeM1) -> None:
        candles = [make_m1(minute) for minute in range(11)]
        shuffled = candles[5:] + candles[:5] + [candles[3]]  # disorder + duplicate

        aggregated = aggregate_candles(shuffled, CandleInterval.M5)

        # Minutes 0-9 form two complete buckets; minute 10 opens a third
        # bucket that is still partial and therefore not emitted.
        assert [c.open_time for c in aggregated] == [
            BASE_TIME,
            BASE_TIME + timedelta(minutes=5),
        ]
        assert all(c.interval == CandleInterval.M5 for c in aggregated)

    def test_trailing_partial_bucket_is_never_emitted(self, make_m1: MakeM1) -> None:
        aggregated = aggregate_candles([make_m1(m) for m in range(4)], CandleInterval.M5)
        assert aggregated == []  # four minutes never make a 5m candle

    def test_empty_input_is_empty_output(self) -> None:
        assert aggregate_candles([], CandleInterval.H1) == []

    def test_mixed_symbols_are_rejected_loudly(self, make_m1: MakeM1) -> None:
        """De-duplication keys on open time, so mixed symbols would silently
        swallow one symbol's candles — it must fail instead."""
        mixed = [make_m1(0), make_m1(0, symbol="ETH/USDT")]
        with pytest.raises(ValueError, match="one symbol at a time"):
            aggregate_candles(mixed, CandleInterval.M5)
