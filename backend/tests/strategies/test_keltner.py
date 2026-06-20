"""Keltner family: upper-channel breakout entries, basis exits."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from tradebot.core.models import Candle, CandleInterval, Side, Signal
from tradebot.evaluation.sweep import SweepCandidate, build_candidate_strategy
from tradebot.portfolio import Position
from tradebot.strategies import KeltnerConfig, KeltnerStrategy

BASE_TIME = datetime(2026, 1, 2, 0, 0, tzinfo=UTC)

CONFIG = KeltnerConfig(ema_period=5, atr_period=3, channel_atr_multiple=1.0, atr_stop_multiple=1.0)


def make_candle(index: int, close: float, span: float = 0.5) -> Candle:
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


def run(strategy: KeltnerStrategy, closes: list[float]) -> list[tuple[int, Signal]]:
    position: Position | None = None
    emitted: list[tuple[int, Signal]] = []
    for index, close in enumerate(closes):
        signal = strategy.on_candle(make_candle(index, close), position)
        if signal is None:
            continue
        emitted.append((index, signal))
        position = position_of() if signal.side == Side.BUY else None
    return emitted


# Flat to set a tight channel, a gradual rally that clears the upper band
# (entry, without one giant jump that would spike ATR and widen the channel),
# then a drop back through the basis (exit).
BREAK_AND_FADE = [100.0] * 6 + [102.0, 104.0, 106.0, 108.0] + [100.0, 94.0]


class TestKeltner:
    def test_a_break_above_the_upper_channel_buys(self) -> None:
        emitted = run(KeltnerStrategy(CONFIG), BREAK_AND_FADE)
        buys = [(i, s) for i, s in emitted if s.side == Side.BUY]
        assert buys, "a thrust above the upper Keltner channel must buy"
        index, buy = buys[0]
        assert "broke above" in buy.reasons[0] and "Keltner channel" in buy.reasons[0]
        assert buy.stop_price_quote is not None
        assert buy.stop_price_quote < make_candle(index, BREAK_AND_FADE[index]).close_quote

    def test_a_fall_to_the_basis_exits(self) -> None:
        emitted = run(KeltnerStrategy(CONFIG), BREAK_AND_FADE)
        sides = [s.side for _, s in emitted]
        assert Side.BUY in sides and Side.SELL in sides
        exit_signal = next(s for _, s in emitted if s.side == Side.SELL)
        assert "fell back to the channel basis" in exit_signal.reasons[0]

    def test_out_of_order_candles_raise(self) -> None:
        strategy = KeltnerStrategy(CONFIG)
        strategy.on_candle(make_candle(5, 100.0), None)
        with pytest.raises(ValueError, match="out-of-order or duplicate"):
            strategy.on_candle(make_candle(3, 100.0), None)

    def test_config_rejects_a_non_positive_channel(self) -> None:
        with pytest.raises(ValueError, match="channel_atr_multiple"):
            KeltnerStrategy(KeltnerConfig(channel_atr_multiple=0.0))

    def test_built_through_the_sweep_factory(self) -> None:
        strategy = build_candidate_strategy(
            SweepCandidate(name="k", family="keltner", params={"channel_atr_multiple": 2.5})
        )
        assert strategy.name == "keltner"
