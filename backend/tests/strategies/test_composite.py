"""Composite strategy: entry voting modes, exit pass-through, contracts."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from tradebot.core.models import Candle, CandleInterval, Side, Signal
from tradebot.portfolio import Position
from tradebot.strategies import CompositeStrategy

BASE_TIME = datetime(2026, 1, 2, 0, 0, tzinfo=UTC)


def make_candle(index: int) -> Candle:
    open_time = BASE_TIME + timedelta(minutes=index)
    return Candle(
        symbol="BTC/USDT",
        interval=CandleInterval.M1,
        open_time=open_time,
        close_time=open_time + timedelta(minutes=1),
        open_quote=Decimal("100"),
        high_quote=Decimal("101"),
        low_quote=Decimal("99"),
        close_quote=Decimal("100"),
        volume_base=Decimal("1"),
    )


class ScriptedMember:
    """Emits a fixed signal side on every candle (or stays silent)."""

    def __init__(self, name: str, side: Side | None) -> None:
        self._name = name
        self._side = side
        self.candles_seen = 0

    @property
    def name(self) -> str:
        return self._name

    def on_candle(self, candle: Candle, position: Position | None) -> Signal | None:
        self.candles_seen += 1
        if self._side is None:
            return None
        return Signal(
            signal_id=f"{self._name}:{candle.symbol}:{candle.close_time.isoformat()}",
            strategy_name=self._name,
            symbol=candle.symbol,
            side=self._side,
            confidence=1.0,
            stop_price_quote=Decimal("90"),
            reasons=(f"{self._name} fired",),
            created_at=candle.close_time,
        )


def position_of() -> Position:
    return Position(symbol="BTC/USDT", quantity_base=Decimal("1"), cost_basis_quote=Decimal("100"))


class TestEntryModes:
    def test_any_mode_takes_the_first_buy(self) -> None:
        composite = CompositeStrategy(
            [ScriptedMember("silent", None), ScriptedMember("buyer", Side.BUY)]
        )
        signal = composite.on_candle(make_candle(0), None)
        assert signal is not None and signal.side == Side.BUY
        assert signal.strategy_name == "buyer"  # lineage names the rule that fired

    def test_all_mode_needs_every_member_to_agree(self) -> None:
        half = CompositeStrategy(
            [ScriptedMember("buyer", Side.BUY), ScriptedMember("silent", None)],
            require_all_entries=True,
        )
        assert half.on_candle(make_candle(0), None) is None

        unanimous = CompositeStrategy(
            [ScriptedMember("a", Side.BUY), ScriptedMember("b", Side.BUY)],
            require_all_entries=True,
        )
        signal = unanimous.on_candle(make_candle(0), None)
        assert signal is not None and signal.side == Side.BUY
        assert any("confluence" in reason for reason in signal.reasons)

    def test_every_member_sees_every_candle(self) -> None:
        """Indicators must stay warm even for members that never fire."""
        silent = ScriptedMember("silent", None)
        buyer = ScriptedMember("buyer", Side.BUY)
        composite = CompositeStrategy([buyer, silent])
        composite.on_candle(make_candle(0), None)
        composite.on_candle(make_candle(1), None)
        assert silent.candles_seen == buyer.candles_seen == 2


class TestExits:
    def test_first_sell_wins_regardless_of_mode(self) -> None:
        """A position must never be trapped behind an entry vote."""
        composite = CompositeStrategy(
            [ScriptedMember("holder", None), ScriptedMember("seller", Side.SELL)],
            require_all_entries=True,
        )
        signal = composite.on_candle(make_candle(0), position_of())
        assert signal is not None and signal.side == Side.SELL
        assert signal.strategy_name == "seller"


class TestContracts:
    def test_needs_at_least_one_member(self) -> None:
        with pytest.raises(ValueError, match="at least one member"):
            CompositeStrategy([])

    def test_out_of_order_candles_raise(self) -> None:
        composite = CompositeStrategy([ScriptedMember("buyer", Side.BUY)])
        composite.on_candle(make_candle(1), None)
        with pytest.raises(ValueError, match="out-of-order"):
            composite.on_candle(make_candle(0), None)

    def test_name_lists_mode_and_members(self) -> None:
        composite = CompositeStrategy(
            [ScriptedMember("a", None), ScriptedMember("b", None)], require_all_entries=True
        )
        assert composite.name == "composite[all:a+b]"
