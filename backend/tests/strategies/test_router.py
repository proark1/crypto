"""Router: entries follow the regime, exits pass from either family."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from tradebot.core.models import Candle, CandleInterval, Side, Signal
from tradebot.portfolio import Position
from tradebot.strategies import RegimeStrategyRouter

BASE_TIME = datetime(2026, 1, 2, 0, 0, tzinfo=UTC)


def make_candle(index: int = 0) -> Candle:
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


def make_signal(name: str, side: Side) -> Signal:
    return Signal(
        signal_id=f"{name}:BTC/USDT:x",
        strategy_name=name,
        symbol="BTC/USDT",
        side=side,
        confidence=1.0,
        stop_price_quote=Decimal("95"),
        reasons=(),
        created_at=BASE_TIME,
    )


class ScriptedStrategy:
    """Emits a fixed signal every candle and records what it saw."""

    def __init__(self, name: str, signal: Signal | None) -> None:
        self._name = name
        self.signal = signal
        self.candles_seen = 0

    @property
    def name(self) -> str:
        return self._name

    def on_candle(self, candle: Candle, position: Position | None) -> Signal | None:
        self.candles_seen += 1
        return self.signal


def make_position() -> Position:
    return Position(symbol="BTC/USDT", quantity_base=Decimal("1"), cost_basis_quote=Decimal("100"))


class TestRouting:
    def test_entries_prefer_the_regimes_family(self) -> None:
        trend = ScriptedStrategy("trend_following", make_signal("trend_following", Side.BUY))
        reversion = ScriptedStrategy("mean_reversion", make_signal("mean_reversion", Side.BUY))
        label = "trending"
        router = RegimeStrategyRouter(trend, reversion, lambda: label)

        routed = router.on_candle(make_candle(0), None)
        assert routed is not None and routed.strategy_name == "trend_following"

        label = "ranging"
        routed = router.on_candle(make_candle(1), None)
        assert routed is not None and routed.strategy_name == "mean_reversion"

    def test_inactive_family_is_forwarded_when_preferred_is_silent(self) -> None:
        """The non-preferred family's entry is still forwarded when the
        preferred one is quiet: a healthy regime allows either family, so
        forwarding it is what keeps the bot trading instead of idle (and any
        genuine gate block is journaled with the signal — §5.2)."""
        trend = ScriptedStrategy("trend_following", None)
        reversion = ScriptedStrategy("mean_reversion", make_signal("mean_reversion", Side.BUY))
        router = RegimeStrategyRouter(trend, reversion, lambda: "trending")

        routed = router.on_candle(make_candle(0), None)

        assert routed is not None and routed.strategy_name == "mean_reversion"

    def test_both_families_stay_warm_and_silent_stays_silent(self) -> None:
        trend = ScriptedStrategy("trend_following", None)
        reversion = ScriptedStrategy("mean_reversion", None)
        router = RegimeStrategyRouter(trend, reversion, lambda: "risk_off")

        for index in range(5):
            assert router.on_candle(make_candle(index), None) is None

        assert trend.candles_seen == 5
        assert reversion.candles_seen == 5  # indicators never go cold

    def test_exits_pass_from_either_family_in_any_regime(self) -> None:
        trend = ScriptedStrategy("trend_following", None)
        reversion = ScriptedStrategy("mean_reversion", make_signal("mean_reversion", Side.SELL))
        router = RegimeStrategyRouter(trend, reversion, lambda: "trending")

        routed = router.on_candle(make_candle(0), make_position())

        # The reversion family's exit passes even though trend is active:
        # a regime flip must never orphan a position with its exit logic off.
        assert routed is not None and routed.side == Side.SELL
        assert routed.strategy_name == "mean_reversion"

    def test_no_entries_while_holding(self) -> None:
        trend = ScriptedStrategy("trend_following", make_signal("trend_following", Side.BUY))
        reversion = ScriptedStrategy("mean_reversion", None)
        router = RegimeStrategyRouter(trend, reversion, lambda: "trending")

        assert router.on_candle(make_candle(0), make_position()) is None
