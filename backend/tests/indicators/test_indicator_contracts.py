"""Contract tests: warm-up behavior, input validation, and value stability."""

import pytest

from tradebot.indicators import Adx, Atr, Ema, Rsi


@pytest.mark.parametrize("indicator_class", [Ema, Rsi, Atr])
def test_period_below_one_is_rejected(indicator_class: type[Ema] | type[Rsi] | type[Atr]) -> None:
    with pytest.raises(ValueError, match="period must be >= 1"):
        indicator_class(0)


def test_adx_period_below_two_is_rejected() -> None:
    # ADX over one candle has no smoothing window; TA-Lib's own lookback
    # math degenerates there, so it is rejected rather than mis-seeded.
    with pytest.raises(ValueError, match="period must be >= 2"):
        Adx(1)


def test_adx_warms_up_for_two_periods_and_exposes_di_lines() -> None:
    adx = Adx(2)
    candles = [(11.0, 9.0, 10.0), (12.0, 10.0, 11.0), (13.0, 11.0, 12.0), (14.0, 12.0, 13.0)]
    results = [adx.update(high, low, close) for high, low, close in candles]
    assert results[:3] == [None, None, None]  # lookback is 2 * period - 1
    assert results[3] is not None
    assert adx.value == results[3]
    # A straight climb is pure +DM: the DI lines must say so.
    assert adx.plus_di is not None and adx.minus_di is not None
    assert adx.plus_di > adx.minus_di
    assert adx.minus_di == pytest.approx(0.0)


def test_ema_returns_none_for_exactly_period_minus_one_updates() -> None:
    ema = Ema(5)
    warmup = [ema.update(float(i)) for i in range(1, 5)]
    assert warmup == [None, None, None, None]
    assert ema.update(5.0) == pytest.approx(3.0)  # SMA seed of 1..5


def test_rsi_returns_none_until_period_changes_seen() -> None:
    rsi = Rsi(3)
    assert [rsi.update(v) for v in [10.0, 11.0, 12.0]] == [None, None, None]
    assert rsi.update(13.0) == pytest.approx(100.0)  # three straight gains


def test_rsi_is_50_when_gains_equal_losses_are_zero() -> None:
    rsi = Rsi(2)
    values = [10.0, 10.0, 10.0, 10.0]
    results = [rsi.update(v) for v in values]
    assert results[-1] == pytest.approx(50.0)  # flat series: no gain, no loss


def test_atr_returns_none_until_period_true_ranges_seen() -> None:
    atr = Atr(2)
    assert atr.update(11.0, 9.0, 10.0) is None  # no previous close yet
    assert atr.update(12.0, 10.0, 11.0) is None  # first TR
    value = atr.update(13.0, 11.0, 12.0)  # second TR -> seeded
    assert value == pytest.approx(2.0)


def test_value_property_tracks_last_update() -> None:
    ema = Ema(2)
    assert ema.value is None
    ema.update(1.0)
    last = ema.update(2.0)
    assert ema.value == last
