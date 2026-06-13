"""Momentum family: MACD crossover entries/exits, ATR stop convention."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from tradebot.core.models import Candle, CandleInterval, Side
from tradebot.evaluation.sweep import SweepCandidate, build_candidate_strategy
from tradebot.portfolio import Position
from tradebot.strategies import MomentumConfig, MomentumStrategy

BASE_TIME = datetime(2026, 1, 2, 0, 0, tzinfo=UTC)

CONFIG = MomentumConfig(fast_ema_period=3, slow_ema_period=6, signal_ema_period=3, atr_period=3)


def volume_filtered(min_volume_ratio: float) -> MomentumConfig:
    """CONFIG plus an armed volume-confirmation filter."""
    return CONFIG.model_copy(update={"volume_ema_period": 3, "min_volume_ratio": min_volume_ratio})


def make_candle(index: int, close: float, volume: float = 10.0) -> Candle:
    open_time = BASE_TIME + timedelta(minutes=index)
    close_price = Decimal(str(close))
    return Candle(
        symbol="BTC/USDT",
        interval=CandleInterval.M1,
        open_time=open_time,
        close_time=open_time + timedelta(minutes=1),
        open_quote=close_price,
        high_quote=close_price + Decimal("0.5"),
        low_quote=close_price - Decimal("0.5"),
        close_quote=close_price,
        volume_base=Decimal(str(volume)),
    )


def position_of(quantity: str = "1") -> Position:
    return Position(
        symbol="BTC/USDT", quantity_base=Decimal(quantity), cost_basis_quote=Decimal("100")
    )


def warmed_up(strategy: MomentumStrategy, candles: int = 12, close: float = 100.0) -> None:
    """Feed a flat warm-up: every indicator formed, histogram resting at 0."""
    for index in range(candles):
        assert strategy.on_candle(make_candle(index, close), None) is None


class TestMomentumEntries:
    def test_bullish_histogram_crossover_buys(self) -> None:
        strategy = MomentumStrategy(CONFIG)
        warmed_up(strategy)

        signal = strategy.on_candle(make_candle(12, 110.0), None)
        assert signal is not None and signal.side == Side.BUY
        assert signal.stop_price_quote < Decimal("110")
        assert "histogram crossed positive" in signal.reasons[0]

    def test_no_entry_without_a_crossover(self) -> None:
        strategy = MomentumStrategy(CONFIG)
        warmed_up(strategy)
        strategy.on_candle(make_candle(12, 110.0), None)  # the crossover
        # Still rising, but the histogram is already positive: no re-entry
        # signal — one crossover, one proposal.
        assert strategy.on_candle(make_candle(13, 112.0), None) is None

    def test_zero_line_filter_blocks_bounces_inside_a_decline(self) -> None:
        """A crossover with MACD still negative is a bounce, not an advance."""
        closes = [100.0 - 2.0 * index for index in range(12)] + [80.0]
        filtered = MomentumStrategy(CONFIG)
        unfiltered = MomentumStrategy(
            MomentumConfig(**{**CONFIG.model_dump(), "require_positive_macd": False})
        )
        filtered_signals = []
        unfiltered_signals = []
        for index, close in enumerate(closes):
            filtered_signals.append(filtered.on_candle(make_candle(index, close), None))
            unfiltered_signals.append(unfiltered.on_candle(make_candle(index, close), None))
        assert all(signal is None for signal in filtered_signals)
        bounce = unfiltered_signals[-1]
        assert bounce is not None and bounce.side == Side.BUY

    def test_warmup_emits_nothing(self) -> None:
        strategy = MomentumStrategy(CONFIG)
        # Slow EMA (6) + signal EMA (3) + previous histogram: the first
        # decision-capable candle is the 10th; everything before is None.
        for index in range(9):
            assert strategy.on_candle(make_candle(index, 100.0 + index), None) is None

    def test_low_volume_crossovers_are_filtered(self) -> None:
        strategy = MomentumStrategy(volume_filtered(1.5))
        warmed_up(strategy)
        # Baseline EMA sits at 10; a crossover on 10 misses the 1.5x bar.
        assert strategy.on_candle(make_candle(12, 110.0, volume=10.0), None) is None

    def test_high_volume_crossovers_pass_the_filter(self) -> None:
        strategy = MomentumStrategy(volume_filtered(1.5))
        warmed_up(strategy)
        signal = strategy.on_candle(make_candle(12, 110.0, volume=20.0), None)
        assert signal is not None and signal.side == Side.BUY


class TestMomentumExits:
    def test_histogram_crossing_negative_sells(self) -> None:
        strategy = MomentumStrategy(CONFIG)
        warmed_up(strategy)
        entry = strategy.on_candle(make_candle(12, 110.0), None)
        assert entry is not None and entry.side == Side.BUY

        signal = strategy.on_candle(make_candle(13, 90.0), position_of())
        assert signal is not None and signal.side == Side.SELL
        assert "momentum turned down" in signal.reasons[0]

    def test_holding_through_positive_momentum_does_nothing(self) -> None:
        strategy = MomentumStrategy(CONFIG)
        warmed_up(strategy)
        strategy.on_candle(make_candle(12, 110.0), None)
        assert strategy.on_candle(make_candle(13, 112.0), position_of()) is None

    def test_exits_are_never_volume_filtered(self) -> None:
        strategy = MomentumStrategy(volume_filtered(5.0))
        warmed_up(strategy)
        strategy.on_candle(make_candle(12, 110.0), None)
        # Volume far below the 5x bar; the momentum-down exit still fires.
        signal = strategy.on_candle(make_candle(13, 90.0, volume=0.1), position_of())
        assert signal is not None and signal.side == Side.SELL


class TestContracts:
    def test_out_of_order_candles_raise(self) -> None:
        strategy = MomentumStrategy(CONFIG)
        strategy.on_candle(make_candle(1, 100.0), None)
        with pytest.raises(ValueError, match="out-of-order"):
            strategy.on_candle(make_candle(0, 100.0), None)

    def test_fast_period_must_sit_below_slow(self) -> None:
        with pytest.raises(ValueError, match="fast EMA period"):
            MomentumStrategy(MomentumConfig(fast_ema_period=26, slow_ema_period=12))

    def test_family_is_sweepable(self) -> None:
        candidate = SweepCandidate(
            name="momentum_12_26_9", family="momentum", params=MomentumConfig().model_dump()
        )
        assert build_candidate_strategy(candidate).name == "momentum"
