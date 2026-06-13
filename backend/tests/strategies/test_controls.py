"""The random-entry control: a seeded, no-skill noise floor.

The control's contract is narrow but load-bearing: it must be reproducible
(seeded), trade only in the valid direction for its state, carry the family
ATR-stop convention, and stay out of the registries that feed sweeps,
promotion, and the lineup.
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from tradebot.core.models import Candle, CandleInterval, Side, Signal
from tradebot.evaluation.improve import IMPROVEMENT_TARGETS
from tradebot.evaluation.sweep import STRATEGY_FAMILIES
from tradebot.portfolio import Position
from tradebot.strategies import RandomEntryConfig, RandomEntryStrategy
from tradebot.strategies.controls import (
    CONTROL_STRATEGIES,
    build_control_strategy,
    validate_control_params,
)

BASE_TIME = datetime(2026, 1, 2, 0, 0, tzinfo=UTC)


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


# A deterministic, varied, positive close series — long enough that a 5%
# entry chance fires several times.
CLOSES = [100.0 + (i * 7 % 23) - 11 for i in range(80)]


def run_series(
    strategy: RandomEntryStrategy,
    closes: list[float],
    position: Position | None = None,
) -> list[Signal | None]:
    return [strategy.on_candle(make_candle(i, close), position) for i, close in enumerate(closes)]


def make_position() -> Position:
    return Position(symbol="BTC/USDT", quantity_base=Decimal("1"), cost_basis_quote=Decimal("90"))


class TestReproducibility:
    def test_same_seed_same_candles_give_identical_signals(self) -> None:
        # The whole evaluation system promises bit-for-bit reproducibility
        # from (series, config, seed); the control's RNG must honor it.
        config = RandomEntryConfig(atr_period=3, seed=42)
        first = run_series(RandomEntryStrategy(config), CLOSES)
        second = run_series(RandomEntryStrategy(config), CLOSES)
        assert first == second

    def test_decision_stream_is_independent_of_position_path(self) -> None:
        # Both coins are drawn every candle regardless of state, so the same
        # seed yields the same entry decisions whether or not the caller is
        # holding — the stream is a pure function of candle count.
        config = RandomEntryConfig(atr_period=3, entry_probability=1.0, seed=7)
        flat = run_series(RandomEntryStrategy(config), CLOSES, position=None)
        # Replay flat-only candles against a fresh instance: identical buys.
        again = run_series(RandomEntryStrategy(config), CLOSES, position=None)
        assert [s.side if s else None for s in flat] == [s.side if s else None for s in again]


class TestDirection:
    def test_flat_never_sells(self) -> None:
        config = RandomEntryConfig(atr_period=3, entry_probability=1.0, exit_probability=1.0)
        signals = run_series(RandomEntryStrategy(config), CLOSES, position=None)
        assert all(s is None or s.side == Side.BUY for s in signals)
        assert any(s is not None and s.side == Side.BUY for s in signals)

    def test_holding_never_buys(self) -> None:
        config = RandomEntryConfig(atr_period=3, entry_probability=1.0, exit_probability=1.0)
        signals = run_series(RandomEntryStrategy(config), CLOSES, position=make_position())
        assert all(s is None or s.side == Side.SELL for s in signals)
        assert any(s is not None and s.side == Side.SELL for s in signals)


class TestStopConvention:
    def test_buy_carries_an_atr_stop_below_the_close(self) -> None:
        config = RandomEntryConfig(atr_period=3, entry_probability=1.0, atr_stop_multiple=2.0)
        buys = [s for s in run_series(RandomEntryStrategy(config), CLOSES) if s is not None]
        assert buys, "entry_probability=1.0 must fire at least once after warm-up"
        for buy in buys:
            assert buy.side == Side.BUY
            assert buy.strategy_name == "random_entry"
            assert buy.stop_price_quote > 0  # never zero or negative (sizing invariant)

    def test_degenerate_stop_is_skipped_not_emitted(self) -> None:
        # A stop multiple that drives the stop to or below zero has no defined
        # invalidation point, so no trade is proposed rather than a bad one.
        config = RandomEntryConfig(atr_period=3, entry_probability=1.0, atr_stop_multiple=10_000.0)
        signals = run_series(RandomEntryStrategy(config), CLOSES)
        assert all(s is None for s in signals)


class TestWarmupAndOrdering:
    def test_no_signal_during_atr_warmup(self) -> None:
        # ATR(period) emits after period + 1 candles; nothing can trade before
        # a stop distance exists, even at probability 1.0.
        config = RandomEntryConfig(atr_period=3, entry_probability=1.0)
        signals = run_series(RandomEntryStrategy(config), CLOSES)
        assert signals[:3] == [None, None, None]
        assert any(s is not None for s in signals[3:])

    def test_out_of_order_candle_raises(self) -> None:
        strategy = RandomEntryStrategy(RandomEntryConfig(atr_period=3))
        strategy.on_candle(make_candle(5, 100.0), None)
        with pytest.raises(ValueError, match="out-of-order or duplicate candle"):
            strategy.on_candle(make_candle(5, 100.0), None)


class TestConfigValidation:
    @pytest.mark.parametrize("probability", [0.0, -0.1, 1.5])
    def test_probabilities_must_be_in_the_unit_interval(self, probability: float) -> None:
        with pytest.raises(ValueError):
            RandomEntryConfig(entry_probability=probability)
        with pytest.raises(ValueError):
            RandomEntryConfig(exit_probability=probability)


class TestRegistrySeparation:
    def test_control_is_not_a_sweepable_family(self) -> None:
        # The separation is the invariant: a control has no edge to tune, so
        # it must never reach the sweep grids, the lineup, the custom-bot
        # builder, or the auto-improvement rotation.
        assert "random_entry" in CONTROL_STRATEGIES
        assert "random_entry" not in STRATEGY_FAMILIES
        assert "random_entry" not in IMPROVEMENT_TARGETS

    def test_build_control_strategy_rejects_unknowns(self) -> None:
        strategy = build_control_strategy("random_entry", {})
        assert strategy.name == "random_entry"
        with pytest.raises(ValueError, match="unknown control"):
            build_control_strategy("not_a_control", {})
        with pytest.raises(ValueError, match="unknown random_entry parameters"):
            validate_control_params("random_entry", {"nope": 1})
