"""Mean reversion: buys the oversold recovery, exits at the midline."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from tradebot.core.models import Candle, CandleInterval, Side, Signal
from tradebot.portfolio import Position
from tradebot.strategies import MeanReversionConfig, MeanReversionStrategy

BASE_TIME = datetime(2026, 1, 2, 0, 0, tzinfo=UTC)

FAST_CONFIG = MeanReversionConfig(
    rsi_period=3, oversold_threshold=30.0, exit_rsi=55.0, atr_period=3, atr_stop_multiple=2.0
)

# A slide deep into oversold, then a recovery candle that lifts RSI(3)
# back over 30, then further strength toward the exit line.
SLIDE_THEN_RECOVER = [100.0, 97.0, 94.0, 91.0, 88.0, 85.0, 92.0, 97.0, 101.0]


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
    strategy: MeanReversionStrategy,
    closes: list[float],
    position: Position | None = None,
    start_index: int = 0,
) -> list[Signal | None]:
    return [
        strategy.on_candle(make_candle(start_index + i, close), position)
        for i, close in enumerate(closes)
    ]


def make_position() -> Position:
    return Position(symbol="BTC/USDT", quantity_base=Decimal("1"), cost_basis_quote=Decimal("90"))


class TestSignals:
    def test_no_signals_during_warmup_or_steady_decline(self) -> None:
        strategy = MeanReversionStrategy(FAST_CONFIG)
        signals = run_series(strategy, [100.0, 98.0, 96.0, 94.0, 92.0], position=None)
        assert signals == [None] * 5  # falling knife is never bought

    def test_oversold_recovery_emits_buy_with_atr_stop(self) -> None:
        strategy = MeanReversionStrategy(FAST_CONFIG)
        signals = run_series(strategy, SLIDE_THEN_RECOVER, position=None)
        buys = [signal for signal in signals if signal is not None]
        assert len(buys) == 1
        buy = buys[0]
        assert buy.side == Side.BUY
        assert buy.strategy_name == "mean_reversion"
        assert buy.stop_price_quote < Decimal("92")  # below the recovery close
        assert any("recovered above 30" in reason for reason in buy.reasons)

    def test_exit_when_rsi_reaches_the_midline(self) -> None:
        strategy = MeanReversionStrategy(FAST_CONFIG)
        position = make_position()
        # Warm up through the slide while "holding", then the strong recovery
        # carries RSI(3) past the exit line.
        signals = run_series(strategy, SLIDE_THEN_RECOVER, position=position)
        sells = [signal for signal in signals if signal is not None and signal.side == Side.SELL]
        assert sells, "the recovery must trigger the reversion exit"
        assert any("reversion played out" in reason for reason in sells[0].reasons)

    def test_no_entry_while_already_holding(self) -> None:
        strategy = MeanReversionStrategy(FAST_CONFIG)
        signals = run_series(strategy, SLIDE_THEN_RECOVER, position=make_position())
        assert all(signal is None or signal.side == Side.SELL for signal in signals)

    def test_out_of_order_candles_raise(self) -> None:
        strategy = MeanReversionStrategy(FAST_CONFIG)
        strategy.on_candle(make_candle(5, 100.0), None)
        with pytest.raises(ValueError, match="out-of-order"):
            strategy.on_candle(make_candle(5, 100.0), None)

    def test_inverted_thresholds_are_rejected(self) -> None:
        with pytest.raises(ValueError, match="must sit below"):
            MeanReversionStrategy(MeanReversionConfig(oversold_threshold=60.0, exit_rsi=55.0))
