"""Volatility-breakout family: Donchian breaks gated on volatility expansion."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from tradebot.core.models import Candle, CandleInterval, Side, Signal
from tradebot.evaluation.sweep import SweepCandidate, build_candidate_strategy
from tradebot.portfolio import Position
from tradebot.strategies import VolBreakoutConfig, VolBreakoutStrategy

BASE_TIME = datetime(2026, 1, 2, 0, 0, tzinfo=UTC)

CONFIG = VolBreakoutConfig(
    channel_period=3,
    atr_period=3,
    atr_baseline_period=3,
    expansion_ratio=1.2,
    exit_ema_period=3,
    atr_stop_multiple=1.0,
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


def run(
    strategy: VolBreakoutStrategy, candles: list[tuple[float, float]]
) -> list[tuple[int, Signal]]:
    position: Position | None = None
    emitted: list[tuple[int, Signal]] = []
    for index, (close, span) in enumerate(candles):
        signal = strategy.on_candle(make_candle(index, close, span), position)
        if signal is None:
            continue
        emitted.append((index, signal))
        position = position_of() if signal.side == Side.BUY else None
    return emitted


# A quiet base (small ranges → low ATR baseline), then a wide breakout candle
# (a new 3-candle high *and* a true-range spike → volatility expands → entry),
# then a decline back below the EMA basis (exit).
EXPANSION_BREAKOUT = [
    *[
        (100.0, 1.0),
        (100.5, 1.0),
        (99.5, 1.0),
        (100.0, 1.0),
        (100.5, 1.0),
        (99.5, 1.0),
        (100.0, 1.0),
    ],
    (112.0, 6.0),  # new high with a range spike: volatility expansion
    (108.0, 1.0),
    (100.0, 1.0),  # falls back below the basis
    (92.0, 1.0),
]

# A slow, steady climb: every close sets a new 3-candle high, but the range
# (and so the ATR) never expands — the expansion gate must block every entry.
STEADY_CLIMB = [(100.0 + i, 1.0) for i in range(16)]


class TestVolBreakout:
    def test_an_expansion_breakout_buys(self) -> None:
        emitted = run(VolBreakoutStrategy(CONFIG), EXPANSION_BREAKOUT)
        buys = [(i, s) for i, s in emitted if s.side == Side.BUY]
        assert buys, "a Donchian break under expanding volatility must buy"
        index, buy = buys[0]
        assert "broke above" in buy.reasons[0] and "baseline" in buy.reasons[0]
        assert buy.stop_price_quote is not None
        close = make_candle(index, EXPANSION_BREAKOUT[index][0]).close_quote
        assert buy.stop_price_quote < close

    def test_a_fall_to_the_basis_exits(self) -> None:
        emitted = run(VolBreakoutStrategy(CONFIG), EXPANSION_BREAKOUT)
        sides = [s.side for _, s in emitted]
        assert Side.BUY in sides and Side.SELL in sides
        first_buy = next(i for i, (_, s) in enumerate(emitted) if s.side == Side.BUY)
        assert any(s.side == Side.SELL for _, s in emitted[first_buy + 1 :])

    def test_a_quiet_climb_without_expansion_never_buys(self) -> None:
        # New highs every candle, but the range never expands: the gate holds.
        emitted = run(VolBreakoutStrategy(CONFIG), STEADY_CLIMB)
        assert not any(s.side == Side.BUY for _, s in emitted)

    def test_out_of_order_candles_raise(self) -> None:
        strategy = VolBreakoutStrategy(CONFIG)
        strategy.on_candle(make_candle(5, 100.0), None)
        with pytest.raises(ValueError, match="out-of-order or duplicate"):
            strategy.on_candle(make_candle(3, 100.0), None)

    def test_built_through_the_sweep_factory(self) -> None:
        strategy = build_candidate_strategy(
            SweepCandidate(name="a", family="vol_breakout", params={"expansion_ratio": 1.5})
        )
        assert strategy.name == "vol_breakout"
