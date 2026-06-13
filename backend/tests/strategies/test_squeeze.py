"""Squeeze family: compression-then-release entries, basis exits, ATR stops."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from tradebot.core.models import Candle, CandleInterval, Side, Signal
from tradebot.evaluation.sweep import SweepCandidate, build_candidate_strategy
from tradebot.portfolio import Position
from tradebot.strategies import SqueezeConfig, SqueezeStrategy

BASE_TIME = datetime(2026, 1, 2, 0, 0, tzinfo=UTC)

# Short windows so the tests can hand-build a squeeze and its release.
CONFIG = SqueezeConfig(
    bollinger_period=5,
    bollinger_stddev=2.0,
    keltner_period=5,
    keltner_atr_multiple=1.5,
    atr_period=3,
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


def drive(
    strategy: SqueezeStrategy,
    closes: list[tuple[float, float, float]],
    position: Position | None = None,
    volume: float = 10.0,
) -> list[Signal | None]:
    """Feed (close, high, low) triples; return the per-candle signals."""
    signals: list[Signal | None] = []
    for index, (close, high, low) in enumerate(closes):
        signals.append(
            strategy.on_candle(
                make_candle(index, close, high=high, low=low, volume=volume), position
            )
        )
    return signals


def squeeze_then_upward_release() -> list[tuple[float, float, float]]:
    """A tight coil (low range -> bands inside Keltner) then a wide up-bar.

    The flat prelude keeps both ATR and the Bollinger spread tiny so the
    bands sit inside the Keltner channel (squeeze on); the final bar is a
    wide upward range that blows the bands back outside (release).
    """
    coil = [(100.0, 100.05, 99.95) for _ in range(12)]
    release = (104.0, 105.0, 100.0)
    return [*coil, release]


class TestSqueezeEntries:
    def test_upward_release_from_a_squeeze_buys(self) -> None:
        strategy = SqueezeStrategy(CONFIG)
        signals = drive(strategy, squeeze_then_upward_release())
        assert all(signal is None for signal in signals[:-1])
        entry = signals[-1]
        assert entry is not None and entry.side == Side.BUY
        assert entry.stop_price_quote < Decimal("104")
        assert "squeeze released upward" in entry.reasons[0]

    def test_no_entry_while_still_compressed(self) -> None:
        strategy = SqueezeStrategy(CONFIG)
        # A pure coil never releases: no entry on any candle.
        signals = drive(strategy, [(100.0, 100.05, 99.95) for _ in range(13)])
        assert all(signal is None for signal in signals)

    def test_downward_release_is_not_bought(self) -> None:
        """A spot long has no edge when the expansion breaks downward."""
        strategy = SqueezeStrategy(CONFIG)
        coil = [(100.0, 100.05, 99.95) for _ in range(12)]
        down_release = (96.0, 100.0, 95.0)
        signals = drive(strategy, [*coil, down_release])
        assert signals[-1] is None

    def test_low_volume_releases_are_filtered(self) -> None:
        strategy = SqueezeStrategy(
            CONFIG.model_copy(update={"volume_ema_period": 3, "min_volume_ratio": 1.5})
        )
        # Baseline volume EMA sits at 10; the release on 10 misses the 1.5x bar.
        signals = drive(strategy, squeeze_then_upward_release(), volume=10.0)
        assert signals[-1] is None


class TestSqueezeExits:
    def test_close_below_the_basis_sells(self) -> None:
        strategy = SqueezeStrategy(CONFIG)
        # Warm the indicators with a held position, then drop below the basis.
        rising = [(100.0 + index, 100.5 + index, 99.5 + index) for index in range(12)]
        signals = drive(strategy, rising, position=position_of())
        assert all(signal is None for signal in signals)
        drop = strategy.on_candle(make_candle(12, 90.0, high=100.0, low=89.0), position_of())
        assert drop is not None and drop.side == Side.SELL
        assert "below the Bollinger basis" in drop.reasons[0]

    def test_exits_are_never_volume_filtered(self) -> None:
        strategy = SqueezeStrategy(
            CONFIG.model_copy(update={"volume_ema_period": 3, "min_volume_ratio": 5.0})
        )
        rising = [(100.0 + index, 100.5 + index, 99.5 + index) for index in range(12)]
        drive(strategy, rising, position=position_of())
        # Volume far below the 5x bar; the basis exit must still fire.
        drop = strategy.on_candle(
            make_candle(12, 90.0, high=100.0, low=89.0, volume=0.1), position_of()
        )
        assert drop is not None and drop.side == Side.SELL


class TestContracts:
    def test_out_of_order_candles_raise(self) -> None:
        strategy = SqueezeStrategy(CONFIG)
        strategy.on_candle(make_candle(1, 100.0), None)
        with pytest.raises(ValueError, match="out-of-order"):
            strategy.on_candle(make_candle(0, 100.0), None)

    def test_degenerate_bollinger_period_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="period must be >= 2"):
            SqueezeStrategy(SqueezeConfig(bollinger_period=1))

    def test_family_is_sweepable(self) -> None:
        candidate = SweepCandidate(
            name="squeeze_20", family="squeeze", params=SqueezeConfig().model_dump()
        )
        assert build_candidate_strategy(candidate).name == "squeeze"
