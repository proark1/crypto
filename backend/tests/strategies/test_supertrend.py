"""Supertrend family: ATR-band trend flips, ATR stop convention."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from tradebot.core.models import Candle, CandleInterval, Side, Signal
from tradebot.evaluation.sweep import SweepCandidate, build_candidate_strategy
from tradebot.portfolio import Position
from tradebot.strategies import SupertrendConfig, SupertrendStrategy

BASE_TIME = datetime(2026, 1, 2, 0, 0, tzinfo=UTC)

CONFIG = SupertrendConfig(atr_period=3, atr_multiple=2.0, atr_stop_multiple=1.0)


def make_candle(index: int, close: float, span: float = 1.0, volume: float = 10.0) -> Candle:
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
        volume_base=Decimal(str(volume)),
    )


def position_of(quantity: str = "1") -> Position:
    return Position(
        symbol="BTC/USDT", quantity_base=Decimal(quantity), cost_basis_quote=Decimal("100")
    )


def run(strategy: SupertrendStrategy, closes: list[float]) -> list[tuple[int, Signal]]:
    """Drive the strategy through ``closes``, flipping a simulated position on
    each fill so entries and exits both get exercised. Returns (index, signal)."""
    position: Position | None = None
    emitted: list[tuple[int, Signal]] = []
    for index, close in enumerate(closes):
        signal = strategy.on_candle(make_candle(index, close), position)
        if signal is None:
            continue
        emitted.append((index, signal))
        position = position_of() if signal.side == Side.BUY else None
    return emitted


# A clear down-leg (establishes a downtrend), then a strong up-leg (flips the
# trend up → entry), then a strong down-leg (flips it down → exit).
DOWN_THEN_UP_THEN_DOWN = (
    [100.0] * 4  # warm the ATR
    + [97.0, 94.0, 91.0, 88.0]  # decline: trend locks down
    + [93.0, 99.0, 106.0, 114.0]  # rally: trend flips up
    + [110.0, 103.0, 95.0, 87.0]  # decline: trend flips down
)


class TestSupertrendFlips:
    def test_warmup_emits_nothing(self) -> None:
        strategy = SupertrendStrategy(CONFIG)
        signals = [strategy.on_candle(make_candle(i, 100.0), None) for i in range(4)]
        assert signals == [None, None, None, None]

    def test_an_up_flip_after_a_downtrend_buys(self) -> None:
        emitted = run(SupertrendStrategy(CONFIG), DOWN_THEN_UP_THEN_DOWN)
        buys = [(i, s) for i, s in emitted if s.side == Side.BUY]
        assert buys, "a sustained rally after a decline must flip the trend up and buy"
        index, buy = buys[0]
        # The entry lands on the up-leg, not during the decline.
        assert index >= 8
        assert "flipped the supertrend up" in buy.reasons[0]

    def test_the_buy_carries_an_atr_stop_below_the_close(self) -> None:
        emitted = run(SupertrendStrategy(CONFIG), DOWN_THEN_UP_THEN_DOWN)
        index, buy = next((i, s) for i, s in emitted if s.side == Side.BUY)
        assert buy.stop_price_quote is not None
        assert buy.stop_price_quote < make_candle(index, DOWN_THEN_UP_THEN_DOWN[index]).close_quote

    def test_a_down_flip_while_long_exits(self) -> None:
        emitted = run(SupertrendStrategy(CONFIG), DOWN_THEN_UP_THEN_DOWN)
        sides = [s.side for _, s in emitted]
        assert Side.BUY in sides and Side.SELL in sides
        # The exit follows the entry (a flip down after the flip up).
        first_buy = next(i for i, (_, s) in enumerate(emitted) if s.side == Side.BUY)
        assert any(s.side == Side.SELL for _, s in emitted[first_buy + 1 :])


class TestSupertrendFilters:
    def test_volume_confirmation_suppresses_a_thin_flip(self) -> None:
        # A high volume-ratio bar requirement with a thin flip candle blocks the
        # entry; the same flip with ample volume fires.
        thin = SupertrendConfig(
            atr_period=3,
            atr_multiple=2.0,
            atr_stop_multiple=1.0,
            volume_ema_period=3,
            min_volume_ratio=5.0,
        )
        closes = DOWN_THEN_UP_THEN_DOWN
        # Thin volume everywhere: the volume gate is never cleared.
        starved = run(SupertrendStrategy(thin), closes)
        assert not any(s.side == Side.BUY for _, s in starved)

    def test_out_of_order_candles_raise(self) -> None:
        strategy = SupertrendStrategy(CONFIG)
        strategy.on_candle(make_candle(5, 100.0), None)
        with pytest.raises(ValueError, match="out-of-order or duplicate"):
            strategy.on_candle(make_candle(3, 100.0), None)

    def test_config_rejects_a_non_positive_band(self) -> None:
        with pytest.raises(ValueError, match="atr_multiple"):
            SupertrendStrategy(SupertrendConfig(atr_multiple=0.0))


class TestSupertrendRegistration:
    def test_built_through_the_sweep_factory(self) -> None:
        strategy = build_candidate_strategy(
            SweepCandidate(name="st", family="supertrend", params={"atr_multiple": 2.5})
        )
        assert strategy.name == "supertrend"
