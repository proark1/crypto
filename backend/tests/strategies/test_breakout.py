"""Breakout family: Donchian entries, channel exits, ATR stop convention."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from tradebot.core.models import Candle, CandleInterval, Side
from tradebot.evaluation.sweep import SweepCandidate, build_candidate_strategy
from tradebot.portfolio import Position
from tradebot.strategies import BreakoutConfig, BreakoutStrategy

BASE_TIME = datetime(2026, 1, 2, 0, 0, tzinfo=UTC)

CONFIG = BreakoutConfig(channel_period=5, exit_channel_period=3, atr_period=3)


def volume_filtered(min_volume_ratio: float, volume_ema_period: int = 3) -> BreakoutConfig:
    """CONFIG plus an armed volume-confirmation filter."""
    return CONFIG.model_copy(
        update={"volume_ema_period": volume_ema_period, "min_volume_ratio": min_volume_ratio}
    )


def make_candle(
    index: int,
    close: float,
    high: float | None = None,
    low: float | None = None,
    volume: float = 10.0,
) -> Candle:
    open_time = BASE_TIME + timedelta(minutes=index)
    close_price = Decimal(str(close))
    return Candle(
        symbol="BTC/USDT",
        interval=CandleInterval.M1,
        open_time=open_time,
        close_time=open_time + timedelta(minutes=1),
        open_quote=close_price,
        high_quote=Decimal(str(high if high is not None else close + 0.5)),
        low_quote=Decimal(str(low if low is not None else close - 0.5)),
        close_quote=close_price,
        volume_base=Decimal(str(volume)),
    )


def position_of(quantity: str = "1") -> Position:
    return Position(
        symbol="BTC/USDT", quantity_base=Decimal(quantity), cost_basis_quote=Decimal("100")
    )


class TestBreakoutEntries:
    def test_close_above_the_prior_channel_ceiling_buys(self) -> None:
        strategy = BreakoutStrategy(CONFIG)
        for index in range(5):  # flat channel: highs at 100.5
            assert strategy.on_candle(make_candle(index, 100.0), None) is None

        signal = strategy.on_candle(make_candle(5, 103.0), None)
        assert signal is not None and signal.side == Side.BUY
        assert signal.stop_price_quote < Decimal("103")
        assert "broke above" in signal.reasons[0]

    def test_no_entry_while_inside_the_channel(self) -> None:
        strategy = BreakoutStrategy(CONFIG)
        for index in range(5):
            strategy.on_candle(make_candle(index, 100.0), None)
        # Close equals the ceiling: inside, not through.
        assert strategy.on_candle(make_candle(5, 100.5), None) is None

    def test_breakout_candle_never_competes_with_its_own_high(self) -> None:
        """The channel is built from prior candles only."""
        strategy = BreakoutStrategy(CONFIG)
        for index in range(5):
            strategy.on_candle(make_candle(index, 100.0), None)
        # This candle's own high (103.5) would swallow the breakout if the
        # channel wrongly included it; the prior ceiling is 100.5.
        signal = strategy.on_candle(make_candle(5, 103.0, high=103.5), None)
        assert signal is not None and signal.side == Side.BUY

    def test_flat_channels_are_filtered_by_minimum_width(self) -> None:
        wide_filter = BreakoutStrategy(
            BreakoutConfig(
                channel_period=5, exit_channel_period=3, atr_period=3, min_channel_width_atr=5.0
            )
        )
        for index in range(5):
            wide_filter.on_candle(make_candle(index, 100.0), None)
        # The flat channel's width (~0) sits far below 5 ATRs: no entry.
        assert wide_filter.on_candle(make_candle(5, 103.0), None) is None

    def test_warmup_emits_nothing(self) -> None:
        strategy = BreakoutStrategy(CONFIG)
        for index in range(4):  # channel not yet full
            assert strategy.on_candle(make_candle(index, 100.0 + index * 5), None) is None

    def test_low_volume_breakouts_are_filtered(self) -> None:
        strategy = BreakoutStrategy(volume_filtered(1.5))
        for index in range(5):
            strategy.on_candle(make_candle(index, 100.0), None)
        # Baseline EMA sits at 10; a breakout on 10 misses the 1.5x bar.
        assert strategy.on_candle(make_candle(5, 103.0, volume=10.0), None) is None

    def test_high_volume_breakouts_pass_the_filter(self) -> None:
        strategy = BreakoutStrategy(volume_filtered(1.5))
        for index in range(5):
            strategy.on_candle(make_candle(index, 100.0), None)
        signal = strategy.on_candle(make_candle(5, 103.0, volume=20.0), None)
        assert signal is not None and signal.side == Side.BUY

    def test_the_filter_waits_for_the_volume_baseline(self) -> None:
        """An unformed baseline blocks entries rather than guessing."""
        strategy = BreakoutStrategy(volume_filtered(1.0, volume_ema_period=10))
        for index in range(5):
            strategy.on_candle(make_candle(index, 100.0), None)
        # The channel is full but the 10-candle volume EMA is not formed.
        assert strategy.on_candle(make_candle(5, 103.0, volume=1000.0), None) is None


class TestBreakoutExits:
    def test_close_below_the_exit_channel_floor_sells(self) -> None:
        strategy = BreakoutStrategy(CONFIG)
        for index in range(5):
            strategy.on_candle(make_candle(index, 100.0), position_of())
        # Prior 3-candle floor is 99.5; close 95 breaks it.
        signal = strategy.on_candle(make_candle(5, 95.0), position_of())
        assert signal is not None and signal.side == Side.SELL
        assert "channel floor" in signal.reasons[0]

    def test_holding_inside_the_channel_does_nothing(self) -> None:
        strategy = BreakoutStrategy(CONFIG)
        for index in range(5):
            strategy.on_candle(make_candle(index, 100.0), position_of())
        assert strategy.on_candle(make_candle(5, 100.0), position_of()) is None

    def test_exits_are_never_volume_filtered(self) -> None:
        strategy = BreakoutStrategy(volume_filtered(5.0))
        for index in range(5):
            strategy.on_candle(make_candle(index, 100.0), position_of())
        # Volume far below the 5x bar; the exit must still fire.
        signal = strategy.on_candle(make_candle(5, 95.0, volume=0.1), position_of())
        assert signal is not None and signal.side == Side.SELL


class TestContracts:
    def test_out_of_order_candles_raise(self) -> None:
        strategy = BreakoutStrategy(CONFIG)
        strategy.on_candle(make_candle(1, 100.0), None)
        with pytest.raises(ValueError, match="out-of-order"):
            strategy.on_candle(make_candle(0, 100.0), None)

    def test_degenerate_periods_are_rejected(self) -> None:
        with pytest.raises(ValueError, match="channel period"):
            BreakoutStrategy(BreakoutConfig(channel_period=1))

    def test_family_is_sweepable(self) -> None:
        candidate = SweepCandidate(
            name="breakout_20_10", family="breakout", params=BreakoutConfig().model_dump()
        )
        assert build_candidate_strategy(candidate).name == "breakout"
