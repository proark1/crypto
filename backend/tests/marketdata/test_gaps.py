from datetime import timedelta

import pytest

from tests.marketdata.conftest import BASE_TIME, MakeM1
from tradebot.core.models import CandleInterval
from tradebot.marketdata import find_gaps


def test_continuous_series_has_no_gaps(make_m1: MakeM1) -> None:
    candles = [make_m1(minute) for minute in range(5)]
    assert find_gaps(candles, CandleInterval.M1) == []


def test_single_gap_is_reported_as_missing_open_time_range(make_m1: MakeM1) -> None:
    candles = [make_m1(0), make_m1(1), make_m1(5)]
    assert find_gaps(candles, CandleInterval.M1) == [
        (BASE_TIME + timedelta(minutes=2), BASE_TIME + timedelta(minutes=5))
    ]


def test_multiple_gaps_are_all_reported(make_m1: MakeM1) -> None:
    candles = [make_m1(0), make_m1(2), make_m1(3), make_m1(7)]
    assert find_gaps(candles, CandleInterval.M1) == [
        (BASE_TIME + timedelta(minutes=1), BASE_TIME + timedelta(minutes=2)),
        (BASE_TIME + timedelta(minutes=4), BASE_TIME + timedelta(minutes=7)),
    ]


def test_empty_and_single_candle_series_have_no_gaps(make_m1: MakeM1) -> None:
    assert find_gaps([], CandleInterval.M1) == []
    assert find_gaps([make_m1(0)], CandleInterval.M1) == []


def test_unsorted_input_raises(make_m1: MakeM1) -> None:
    with pytest.raises(ValueError, match="unsorted or duplicated"):
        find_gaps([make_m1(5), make_m1(0)], CandleInterval.M1)


def test_duplicate_input_raises(make_m1: MakeM1) -> None:
    with pytest.raises(ValueError, match="unsorted or duplicated"):
        find_gaps([make_m1(0), make_m1(0)], CandleInterval.M1)
