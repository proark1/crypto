"""Time-series-momentum family: lookback-return entries and exits."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from tradebot.core.models import Candle, CandleInterval, Side, Signal
from tradebot.evaluation.sweep import SweepCandidate, build_candidate_strategy
from tradebot.portfolio import Position
from tradebot.strategies import TsmomConfig, TsmomStrategy

BASE_TIME = datetime(2026, 1, 2, 0, 0, tzinfo=UTC)

CONFIG = TsmomConfig(lookback=3, atr_period=3, atr_stop_multiple=1.0)


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


def run(strategy: TsmomStrategy, closes: list[float]) -> list[tuple[int, Signal]]:
    position: Position | None = None
    emitted: list[tuple[int, Signal]] = []
    for index, close in enumerate(closes):
        signal = strategy.on_candle(make_candle(index, close), position)
        if signal is None:
            continue
        emitted.append((index, signal))
        position = position_of() if signal.side == Side.BUY else None
    return emitted


# A steady rally (positive lookback return → entry) then a steady decline (the
# return turns negative → exit).
UP_THEN_DOWN = [
    *[100.0, 100.0, 100.0],  # warm-up
    *[105.0, 110.0, 120.0, 135.0, 150.0],  # rally: positive lookback return
    *[140.0, 125.0, 110.0, 95.0, 85.0],  # decline: return turns negative
]


class TestTsmom:
    def test_a_positive_lookback_return_buys(self) -> None:
        emitted = run(TsmomStrategy(CONFIG), UP_THEN_DOWN)
        buys = [(i, s) for i, s in emitted if s.side == Side.BUY]
        assert buys, "a positive lookback return must buy"
        index, buy = buys[0]
        assert "return" in buy.reasons[0]
        assert buy.stop_price_quote is not None
        assert buy.stop_price_quote < make_candle(index, UP_THEN_DOWN[index]).close_quote

    def test_a_negative_turn_exits(self) -> None:
        emitted = run(TsmomStrategy(CONFIG), UP_THEN_DOWN)
        sides = [s.side for _, s in emitted]
        assert Side.BUY in sides and Side.SELL in sides
        first_buy = next(i for i, (_, s) in enumerate(emitted) if s.side == Side.BUY)
        assert any(s.side == Side.SELL for _, s in emitted[first_buy + 1 :])

    def test_a_high_entry_threshold_gates_weak_momentum(self) -> None:
        # A 50% entry threshold no move in the series ever clears blocks entry.
        strict = TsmomConfig(lookback=3, atr_period=3, entry_threshold=0.5, atr_stop_multiple=1.0)
        emitted = run(TsmomStrategy(strict), UP_THEN_DOWN)
        assert not any(s.side == Side.BUY for _, s in emitted)

    def test_out_of_order_candles_raise(self) -> None:
        strategy = TsmomStrategy(CONFIG)
        strategy.on_candle(make_candle(5, 100.0), None)
        with pytest.raises(ValueError, match="out-of-order or duplicate"):
            strategy.on_candle(make_candle(3, 100.0), None)

    def test_built_through_the_sweep_factory(self) -> None:
        strategy = build_candidate_strategy(
            SweepCandidate(name="a", family="tsmom", params={"lookback": 30})
        )
        assert strategy.name == "tsmom"
