from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from tradebot.backtest import split_walk_forward
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
