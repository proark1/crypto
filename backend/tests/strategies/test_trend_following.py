from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from tradebot.core.models import Candle, CandleInterval, Side, Signal
from tradebot.portfolio import Position
from tradebot.strategies import TrendFollowingConfig, TrendFollowingStrategy

BASE_TIME = datetime(2026, 1, 2, 0, 0, tzinfo=UTC)

FAST_CONFIG = TrendFollowingConfig(
    fast_ema_period=2, slow_ema_period=4, atr_period=2, atr_stop_multiple=2.0
)


def make_candle(index: int, close: float) -> Candle:
    open_time = BASE_TIME + timedelta(minutes=index)
    price = Decimal(str(close))
    return Candle(
        symbol="BTC/USDT",
        interval=CandleInterval.M1,
        open_time=open_time,
        close_time=open_time + timedelta(minutes=1),
        open_quote=price,
        high_quote=price + Decimal("1"),
        low_quote=max(price - Decimal("1"), Decimal("0.01")),
        close_quote=price,
        volume_base=Decimal("1"),
    )


def run_series(
    strategy: TrendFollowingStrategy,
    closes: list[float],
    position: Position | None = None,
    start_index: int = 0,
) -> list[Signal | None]:
    return [
        strategy.on_candle(make_candle(start_index + i, close), position)
        for i, close in enumerate(closes)
    ]


def make_position() -> Position:
    return Position(symbol="BTC/USDT", quantity_base=Decimal("1"), cost_basis_quote=Decimal("100"))


DOWN_THEN_UP = [100.0, 98.0, 96.0, 94.0, 92.0, 90.0, 100.0, 112.0, 126.0]
UP_THEN_DOWN = [100.0, 102.0, 104.0, 106.0, 108.0, 110.0, 100.0, 88.0, 74.0]


class TestSignals:
    def test_no_signals_during_warmup(self) -> None:
        strategy = TrendFollowingStrategy(FAST_CONFIG)
        signals = run_series(strategy, [100.0, 100.0, 100.0, 100.0], position=None)
        assert signals == [None, None, None, None]

    def test_cross_up_emits_buy_with_atr_stop_below_close(self) -> None:
        strategy = TrendFollowingStrategy(FAST_CONFIG)
        signals = run_series(strategy, DOWN_THEN_UP)
        buys = [s for s in signals if s is not None]
        assert len(buys) == 1
        signal = buys[0]
        assert signal.side == Side.BUY
        assert signal.strategy_name == "trend_following"
        assert signal.stop_price_quote < Decimal("126")
        assert signal.reasons  # explainability is part of the contract
        assert signal.created_at.tzinfo is not None

    def test_cross_up_with_open_position_is_silent(self) -> None:
        strategy = TrendFollowingStrategy(FAST_CONFIG)
        signals = run_series(strategy, DOWN_THEN_UP, position=make_position())
        assert all(s is None for s in signals)

    def test_cross_down_with_position_emits_full_exit(self) -> None:
        strategy = TrendFollowingStrategy(FAST_CONFIG)
        signals = run_series(strategy, UP_THEN_DOWN, position=make_position())
        sells = [s for s in signals if s is not None]
        assert len(sells) == 1
        assert sells[0].side == Side.SELL

    def test_cross_down_without_position_is_silent(self) -> None:
        strategy = TrendFollowingStrategy(FAST_CONFIG)
        signals = run_series(strategy, UP_THEN_DOWN, position=None)
        assert all(s is None for s in signals)

    def test_stop_distance_scales_with_atr_multiple(self) -> None:
        def stop_distance_for(multiple: float) -> Decimal:
            config = TrendFollowingConfig(
                fast_ema_period=2, slow_ema_period=4, atr_period=2, atr_stop_multiple=multiple
            )
            signals = run_series(TrendFollowingStrategy(config), DOWN_THEN_UP)
            index, signal = next((i, s) for i, s in enumerate(signals) if s is not None)
            signal_close = Decimal(str(DOWN_THEN_UP[index]))
            return signal_close - signal.stop_price_quote

        distance_at_2 = stop_distance_for(2.0)
        distance_at_3 = stop_distance_for(3.0)
        assert distance_at_2 > 0
        # Stops come from float indicator math, so compare with a tolerance.
        assert float(distance_at_3) == pytest.approx(float(distance_at_2) * 1.5, rel=1e-9)

    def test_duplicate_candle_raises_instead_of_corrupting_indicators(self) -> None:
        strategy = TrendFollowingStrategy(FAST_CONFIG)
        strategy.on_candle(make_candle(0, 100.0), None)
        with pytest.raises(ValueError, match="out-of-order or duplicate"):
            strategy.on_candle(make_candle(0, 100.0), None)

    def test_out_of_order_candle_raises(self) -> None:
        strategy = TrendFollowingStrategy(FAST_CONFIG)
        strategy.on_candle(make_candle(5, 100.0), None)
        with pytest.raises(ValueError, match="out-of-order or duplicate"):
            strategy.on_candle(make_candle(4, 100.0), None)


class TestConfig:
    def test_fast_period_must_be_shorter_than_slow(self) -> None:
        with pytest.raises(ValueError, match="must be shorter"):
            TrendFollowingStrategy(TrendFollowingConfig(fast_ema_period=50, slow_ema_period=20))


class TestAntiChaseFilter:
    def test_extended_crosses_are_skipped(self) -> None:
        """A violent rally crosses the EMAs far above them — the chase case."""
        config = TrendFollowingConfig(
            fast_ema_period=3, slow_ema_period=6, atr_period=3, max_entry_extension_atr=1.0
        )
        filtered = TrendFollowingStrategy(config)
        unfiltered = TrendFollowingStrategy(
            config.model_copy(update={"max_entry_extension_atr": 0.0})
        )

        filtered_signals = run_series(filtered, DOWN_THEN_UP)
        unfiltered_signals = run_series(unfiltered, DOWN_THEN_UP)

        # The bare config buys this vertical move; the filter refuses to
        # chase a close sitting far above the slow EMA.
        assert any(s is not None and s.side == Side.BUY for s in unfiltered_signals)
        assert all(s is None or s.side != Side.BUY for s in filtered_signals)

    def test_exits_ignore_the_filter(self) -> None:
        config = TrendFollowingConfig(
            fast_ema_period=3, slow_ema_period=6, atr_period=3, max_entry_extension_atr=1.0
        )
        strategy = TrendFollowingStrategy(config)
        signals = run_series(strategy, UP_THEN_DOWN, position=make_position())
        assert any(s is not None and s.side == Side.SELL for s in signals)
