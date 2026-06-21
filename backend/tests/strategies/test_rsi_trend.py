"""RSI-trend family: midline cross-up entries, exit-level exits."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from tradebot.core.models import Candle, CandleInterval, Side, Signal
from tradebot.evaluation.sweep import SweepCandidate, build_candidate_strategy
from tradebot.portfolio import Position
from tradebot.strategies import RsiTrendConfig, RsiTrendStrategy

BASE_TIME = datetime(2026, 1, 2, 0, 0, tzinfo=UTC)

CONFIG = RsiTrendConfig(
    rsi_period=3, entry_level=50.0, exit_level=45.0, atr_period=3, atr_stop_multiple=1.0
)


def make_candle(index: int, close: float, span: float = 1.0) -> Candle:
    open_time = BASE_TIME + timedelta(minutes=index)
    close_price = Decimal(str(close))
    return Candle(
        symbol="BTC/USDT",
        interval=CandleInterval.M1,
        open_time=open_time,
        close_time=open_time + timedelta(minutes=1),
        open_quote=close_price,
        high_quote=Decimal(str(close + span)),
        low_quote=Decimal(str(close - span)),
        close_quote=close_price,
        volume_base=Decimal("10"),
    )


def position_of() -> Position:
    return Position(symbol="BTC/USDT", quantity_base=Decimal("1"), cost_basis_quote=Decimal("100"))


def run(strategy: RsiTrendStrategy, closes: list[float]) -> list[tuple[int, Signal]]:
    position: Position | None = None
    emitted: list[tuple[int, Signal]] = []
    for index, close in enumerate(closes):
        signal = strategy.on_candle(make_candle(index, close), position)
        if signal is None:
            continue
        emitted.append((index, signal))
        position = position_of() if signal.side == Side.BUY else None
    return emitted


# A decline (RSI sinks below the midline), then a rally (RSI crosses up through
# 50 → entry), then a decline (RSI falls below the exit level → exit).
DOWN_THEN_UP_THEN_DOWN = [
    *[108.0, 105.0, 102.0, 99.0, 96.0, 93.0, 90.0],  # decline: RSI low
    *[94.0, 99.0, 105.0, 112.0, 120.0],  # rally: RSI crosses up through 50
    *[114.0, 107.0, 99.0, 91.0],  # decline: RSI back below the exit level
]

DECLINE_ONLY = [108.0, 105.0, 102.0, 99.0, 96.0, 93.0, 90.0, 87.0, 84.0, 81.0]


class TestRsiTrend:
    def test_a_cross_up_through_the_midline_buys(self) -> None:
        emitted = run(RsiTrendStrategy(CONFIG), DOWN_THEN_UP_THEN_DOWN)
        buys = [(i, s) for i, s in emitted if s.side == Side.BUY]
        assert buys, "an RSI cross up through the midline must buy"
        index, buy = buys[0]
        assert "RSI" in buy.reasons[0] and "crossed up" in buy.reasons[0]
        assert buy.stop_price_quote is not None
        assert buy.stop_price_quote < make_candle(index, DOWN_THEN_UP_THEN_DOWN[index]).close_quote

    def test_a_fall_below_the_exit_level_exits(self) -> None:
        emitted = run(RsiTrendStrategy(CONFIG), DOWN_THEN_UP_THEN_DOWN)
        sides = [s.side for _, s in emitted]
        assert Side.BUY in sides and Side.SELL in sides
        first_buy = next(i for i, (_, s) in enumerate(emitted) if s.side == Side.BUY)
        assert any(s.side == Side.SELL for _, s in emitted[first_buy + 1 :])

    def test_a_pure_decline_never_crosses_up_so_never_buys(self) -> None:
        emitted = run(RsiTrendStrategy(CONFIG), DECLINE_ONLY)
        assert not any(s.side == Side.BUY for _, s in emitted)

    def test_exit_level_above_entry_level_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="exit_level"):
            RsiTrendStrategy(RsiTrendConfig(entry_level=50.0, exit_level=60.0))

    def test_out_of_order_candles_raise(self) -> None:
        strategy = RsiTrendStrategy(CONFIG)
        strategy.on_candle(make_candle(5, 100.0), None)
        with pytest.raises(ValueError, match="out-of-order or duplicate"):
            strategy.on_candle(make_candle(3, 100.0), None)

    def test_built_through_the_sweep_factory(self) -> None:
        strategy = build_candidate_strategy(
            SweepCandidate(name="a", family="rsi_trend", params={"entry_level": 55.0})
        )
        assert strategy.name == "rsi_trend"
