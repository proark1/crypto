"""Funding contrarian: longs crowded-short capitulation, exits on recovery."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from tradebot.core.models import Candle, CandleInterval, Side, Signal
from tradebot.evaluation.sweep import SweepCandidate, build_candidate_strategy
from tradebot.portfolio import Position
from tradebot.strategies import FundingConfig, FundingStrategy

BASE_TIME = datetime(2026, 1, 2, 0, 0, tzinfo=UTC)

FAST_CONFIG = FundingConfig(
    enter_funding_at_or_below=-0.0005,
    exit_funding_at_or_above=0.0,
    atr_period=3,
    atr_stop_multiple=2.0,
)


class _ConstantFunding:
    """A funding provider that reports one rate for every lookup (or none)."""

    def __init__(self, rate: Decimal | None) -> None:
        self._rate = rate

    def rate_as_of(self, symbol: str, at: datetime) -> Decimal | None:
        return self._rate


def make_candle(index: int, close: float = 100.0) -> Candle:
    open_time = BASE_TIME + timedelta(hours=index)
    price = Decimal(str(close))
    return Candle(
        symbol="BTC/USDT",
        interval=CandleInterval.H1,
        open_time=open_time,
        close_time=open_time + timedelta(hours=1),
        open_quote=price,
        high_quote=price + Decimal("1"),
        low_quote=max(price - Decimal("1"), Decimal("0.01")),
        close_quote=price,
        volume_base=Decimal("1"),
    )


def run_series(
    strategy: FundingStrategy, count: int, position: Position | None = None
) -> list[Signal | None]:
    return [strategy.on_candle(make_candle(i), position) for i in range(count)]


def make_position() -> Position:
    return Position(symbol="BTC/USDT", quantity_base=Decimal("1"), cost_basis_quote=Decimal("100"))


class TestSignals:
    def test_enters_long_on_crowded_short_funding(self) -> None:
        # Deeply negative funding (shorts paying longs) with no position: buy.
        strategy = FundingStrategy(FAST_CONFIG, _ConstantFunding(Decimal("-0.001")))
        signals = run_series(strategy, 5, position=None)

        assert signals[0] is None  # ATR still warming
        entry = next(s for s in signals if s is not None)
        assert entry.side == Side.BUY
        assert entry.strategy_name == "funding"
        assert entry.stop_price_quote < make_candle(0).close_quote  # stop below close

    def test_exits_when_funding_recovers(self) -> None:
        # Positive funding while holding: the short-crowding has unwound, exit.
        strategy = FundingStrategy(FAST_CONFIG, _ConstantFunding(Decimal("0.0001")))
        signals = run_series(strategy, 5, position=make_position())

        exit_signal = next(s for s in signals if s is not None)
        assert exit_signal.side == Side.SELL

    def test_no_entry_while_funding_sits_in_the_neutral_band(self) -> None:
        # Between the thresholds (-0.0002 is above entry, below exit): no opinion.
        strategy = FundingStrategy(FAST_CONFIG, _ConstantFunding(Decimal("-0.0002")))
        assert run_series(strategy, 6, position=None) == [None] * 6

    def test_inert_without_a_provider(self) -> None:
        # The sweep-default construction (no provider) never trades — fail-safe.
        strategy = FundingStrategy(FAST_CONFIG, None)
        assert run_series(strategy, 6, position=None) == [None] * 6

    def test_silent_when_funding_is_unknown(self) -> None:
        strategy = FundingStrategy(FAST_CONFIG, _ConstantFunding(None))
        assert run_series(strategy, 6, position=None) == [None] * 6

    def test_out_of_order_candle_raises(self) -> None:
        strategy = FundingStrategy(FAST_CONFIG, _ConstantFunding(Decimal("-0.001")))
        strategy.on_candle(make_candle(5), None)
        with pytest.raises(ValueError, match="out-of-order"):
            strategy.on_candle(make_candle(4), None)


class TestConfig:
    def test_rejects_entry_at_or_above_exit(self) -> None:
        # An entry threshold not below the exit would never let a trade close.
        with pytest.raises(ValueError, match="must sit below"):
            FundingStrategy(
                FundingConfig(enter_funding_at_or_below=0.001, exit_funding_at_or_above=0.0)
            )


class TestFactoryInjection:
    def test_build_candidate_strategy_wires_the_provider(self) -> None:
        # The sweep factory must hand the funding family its provider, or the
        # family could never be graded on funding.
        candidate = SweepCandidate(name="funding", family="funding", params={"atr_period": 3})
        strategy = build_candidate_strategy(candidate, _ConstantFunding(Decimal("-0.001")))

        signals = [strategy.on_candle(make_candle(i), None) for i in range(5)]
        assert any(s is not None and s.side == Side.BUY for s in signals)

    def test_build_candidate_strategy_funding_is_inert_without_a_provider(self) -> None:
        candidate = SweepCandidate(name="funding", family="funding", params={"atr_period": 3})
        strategy = build_candidate_strategy(candidate)  # no provider

        assert [strategy.on_candle(make_candle(i), None) for i in range(6)] == [None] * 6
