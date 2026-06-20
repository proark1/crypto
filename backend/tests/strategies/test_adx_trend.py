"""ADX-trend family: +DI cross-up entries gated on ADX strength."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from tradebot.core.models import Candle, CandleInterval, Side, Signal
from tradebot.evaluation.sweep import SweepCandidate, build_candidate_strategy
from tradebot.portfolio import Position
from tradebot.strategies import AdxTrendConfig, AdxTrendStrategy

BASE_TIME = datetime(2026, 1, 2, 0, 0, tzinfo=UTC)

# A modest gate so a clear trend in the test series clears it.
CONFIG = AdxTrendConfig(adx_period=3, adx_threshold=15.0, atr_period=3, atr_stop_multiple=1.0)


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


def run(strategy: AdxTrendStrategy, closes: list[float]) -> list[tuple[int, Signal]]:
    position: Position | None = None
    emitted: list[tuple[int, Signal]] = []
    for index, close in enumerate(closes):
        signal = strategy.on_candle(make_candle(index, close), position)
        if signal is None:
            continue
        emitted.append((index, signal))
        position = position_of() if signal.side == Side.BUY else None
    return emitted


# A sustained decline (builds -DI dominance and ADX), then a sustained rally
# (flips +DI above -DI with ADX still strong → entry), then a decline (flips
# direction back down → exit).
DOWN_THEN_UP_THEN_DOWN = (
    [108.0, 105.0, 102.0, 99.0, 96.0, 93.0, 90.0]  # decline
    + [94.0, 99.0, 105.0, 112.0, 120.0]  # rally: +DI cross-up under strong ADX
    + [114.0, 107.0, 99.0, 91.0]  # decline: direction flips down
)


class TestAdxTrend:
    def test_a_di_cross_up_under_strong_adx_buys(self) -> None:
        emitted = run(AdxTrendStrategy(CONFIG), DOWN_THEN_UP_THEN_DOWN)
        buys = [(i, s) for i, s in emitted if s.side == Side.BUY]
        assert buys, "a +DI cross-up with a strong ADX must buy"
        index, buy = buys[0]
        assert "+DI" in buy.reasons[0] and "strong trend" in buy.reasons[0]
        assert buy.stop_price_quote is not None
        assert buy.stop_price_quote < make_candle(index, DOWN_THEN_UP_THEN_DOWN[index]).close_quote

    def test_a_weak_trend_is_gated_out(self) -> None:
        # An impossibly high ADX gate blocks every entry, however the price moves.
        strict = AdxTrendConfig(
            adx_period=3, adx_threshold=100.0, atr_period=3, atr_stop_multiple=1.0
        )
        emitted = run(AdxTrendStrategy(strict), DOWN_THEN_UP_THEN_DOWN)
        assert not any(s.side == Side.BUY for _, s in emitted)

    def test_a_direction_flip_down_exits(self) -> None:
        emitted = run(AdxTrendStrategy(CONFIG), DOWN_THEN_UP_THEN_DOWN)
        sides = [s.side for _, s in emitted]
        assert Side.BUY in sides and Side.SELL in sides
        first_buy = next(i for i, (_, s) in enumerate(emitted) if s.side == Side.BUY)
        assert any(s.side == Side.SELL for _, s in emitted[first_buy + 1 :])

    def test_out_of_order_candles_raise(self) -> None:
        strategy = AdxTrendStrategy(CONFIG)
        strategy.on_candle(make_candle(5, 100.0), None)
        with pytest.raises(ValueError, match="out-of-order or duplicate"):
            strategy.on_candle(make_candle(3, 100.0), None)

    def test_built_through_the_sweep_factory(self) -> None:
        strategy = build_candidate_strategy(
            SweepCandidate(name="a", family="adx_trend", params={"adx_threshold": 20.0})
        )
        assert strategy.name == "adx_trend"
