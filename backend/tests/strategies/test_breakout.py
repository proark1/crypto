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


def make_candle(
    index: int, close: float, high: float | None = None, low: float | None = None
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
        volume_base=Decimal("10"),
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
