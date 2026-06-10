from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from tradebot.backtest import split_rolling_by_fraction, split_walk_forward
from tradebot.core.models import Candle, CandleInterval

BASE_TIME = datetime(2026, 1, 2, 0, 0, tzinfo=UTC)


def make_candles(count: int) -> list[Candle]:
    candles = []
    for index in range(count):
        open_time = BASE_TIME + timedelta(minutes=index)
        candles.append(
            Candle(
                symbol="BTC/USDT",
                interval=CandleInterval.M1,
                open_time=open_time,
                close_time=open_time + timedelta(minutes=1),
                open_quote=Decimal("100"),
                high_quote=Decimal("101"),
                low_quote=Decimal("99"),
                close_quote=Decimal("100"),
                volume_base=Decimal("1"),
            )
        )
    return candles


def test_windows_roll_forward_by_validation_size() -> None:
    windows = split_walk_forward(make_candles(10), train_size=4, validate_size=2)

    assert len(windows) == 3
    for window in windows:
        assert len(window.train) == 4
        assert len(window.validation) == 2
        # Validation strictly follows training in time.
        assert window.validation[0].open_time > window.train[-1].open_time
    # Consecutive validation windows tile history with no gaps or overlap.
    assert windows[1].validation[0].open_time == windows[0].validation[-1].open_time + timedelta(
        minutes=1
    )


def test_every_candle_after_first_train_is_validated_exactly_once() -> None:
    candles = make_candles(12)
    windows = split_walk_forward(candles, train_size=4, validate_size=2)

    validated = [candle.open_time for window in windows for candle in window.validation]
    expected = [candle.open_time for candle in candles[4:12]]
    assert validated == expected


def test_tail_short_of_full_validation_window_is_dropped() -> None:
    windows = split_walk_forward(make_candles(11), train_size=4, validate_size=2)
    assert len(windows) == 3  # candle 11 has no full validation window


def test_too_few_candles_raise() -> None:
    with pytest.raises(ValueError, match="need at least 6"):
        split_walk_forward(make_candles(5), train_size=4, validate_size=2)


def test_non_positive_sizes_raise() -> None:
    with pytest.raises(ValueError, match=">= 1"):
        split_walk_forward(make_candles(10), train_size=0, validate_size=2)


class TestSplitRollingByFraction:
    def test_exact_window_count_tiling_all_held_out_candles(self) -> None:
        candles = make_candles(10)
        windows = split_rolling_by_fraction(candles, training_fraction=0.7, window_count=3)

        assert len(windows) == 3
        for window in windows:
            assert len(window.train) == 7  # int(10 * 0.7)
            assert window.validation[0].open_time > window.train[-1].open_time
        validated = [candle.open_time for window in windows for candle in window.validation]
        assert validated == [candle.open_time for candle in candles[7:]]

    def test_validation_slice_sizes_differ_by_at_most_one(self) -> None:
        windows = split_rolling_by_fraction(make_candles(100), 0.7, window_count=4)

        sizes = [len(window.validation) for window in windows]
        assert sum(sizes) == 30
        assert max(sizes) - min(sizes) <= 1

    def test_each_window_trains_on_the_span_preceding_its_slice(self) -> None:
        candles = make_candles(20)
        windows = split_rolling_by_fraction(candles, 0.5, window_count=2)

        for window in windows:
            assert window.train[-1].open_time + timedelta(minutes=1) == (
                window.validation[0].open_time
            )

    def test_one_window_reproduces_the_single_chronological_split(self) -> None:
        candles = make_candles(10)
        (window,) = split_rolling_by_fraction(candles, training_fraction=0.6, window_count=1)

        assert [c.open_time for c in window.train] == [c.open_time for c in candles[:6]]
        assert [c.open_time for c in window.validation] == [c.open_time for c in candles[6:]]

    def test_too_few_held_out_candles_raise(self) -> None:
        with pytest.raises(ValueError, match="cannot host"):
            split_rolling_by_fraction(make_candles(10), training_fraction=0.9, window_count=2)

    def test_degenerate_arguments_raise(self) -> None:
        with pytest.raises(ValueError, match="window_count"):
            split_rolling_by_fraction(make_candles(10), 0.7, window_count=0)
        with pytest.raises(ValueError, match="training_fraction"):
            split_rolling_by_fraction(make_candles(10), 1.0, window_count=2)
