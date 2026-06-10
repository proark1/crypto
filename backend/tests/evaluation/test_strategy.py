"""Scenarios evaluate the production strategy shape, self-routed by regime."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from tradebot.core.models import Candle, CandleInterval, Side, Signal
from tradebot.evaluation.strategy import SelfRoutedRegimeStrategy, build_traded_strategy
from tradebot.portfolio import Position
from tradebot.signals import RegimeConfig

BASE_TIME = datetime(2026, 1, 2, 0, 0, tzinfo=UTC)


def make_candle(index: int, price: float) -> Candle:
    open_time = BASE_TIME + timedelta(minutes=index)
    quote = Decimal(str(round(price, 4)))
    return Candle(
        symbol="ETH/USDT",
        interval=CandleInterval.M1,
        open_time=open_time,
        close_time=open_time + timedelta(minutes=1),
        open_quote=quote,
        high_quote=quote + Decimal("0.2"),
        low_quote=quote - Decimal("0.2"),
        close_quote=quote,
        volume_base=Decimal("1"),
    )


def make_signal(name: str, side: Side) -> Signal:
    return Signal(
        signal_id=f"{name}:ETH/USDT:x",
        strategy_name=name,
        symbol="ETH/USDT",
        side=side,
        confidence=1.0,
        stop_price_quote=Decimal("95"),
        reasons=(),
        created_at=BASE_TIME,
    )


class ScriptedStrategy:
    """Emits a fixed signal every candle."""

    def __init__(self, name: str, signal: Signal | None) -> None:
        self._name = name
        self.signal = signal

    @property
    def name(self) -> str:
        return self._name

    def on_candle(self, candle: Candle, position: Position | None) -> Signal | None:
        return self.signal


def make_self_routed() -> SelfRoutedRegimeStrategy:
    return SelfRoutedRegimeStrategy(
        ScriptedStrategy("trend_following", make_signal("trend_following", Side.BUY)),
        ScriptedStrategy("mean_reversion", make_signal("mean_reversion", Side.BUY)),
        # A short ADX so the regime forms within a test-sized window.
        regime_config=RegimeConfig(adx_period=5, drawdown_window_candles=10),
    )


class TestSelfRoutedRegimeStrategy:
    def test_trend_family_is_preferred_while_the_regime_warms_up(self) -> None:
        strategy = make_self_routed()

        routed = strategy.on_candle(make_candle(0, 100.0), None)

        assert routed is not None and routed.strategy_name == "trend_following"

    def test_a_directional_stream_routes_to_the_trend_family(self) -> None:
        strategy = make_self_routed()

        routed = None
        for index in range(30):
            routed = strategy.on_candle(make_candle(index, 100.0 + 2.0 * index), None)

        assert routed is not None and routed.strategy_name == "trend_following"

    def test_an_oscillating_stream_routes_to_the_reversion_family(self) -> None:
        strategy = make_self_routed()

        routed = None
        for index in range(30):
            price = 100.0 + (0.3 if index % 2 == 0 else -0.3)
            routed = strategy.on_candle(make_candle(index, price), None)

        assert routed is not None and routed.strategy_name == "mean_reversion"

    def test_exits_pass_from_either_family_whatever_the_regime(self) -> None:
        strategy = SelfRoutedRegimeStrategy(
            ScriptedStrategy("trend_following", None),
            ScriptedStrategy("mean_reversion", make_signal("mean_reversion", Side.SELL)),
            regime_config=RegimeConfig(adx_period=5, drawdown_window_candles=10),
        )
        position = Position(
            symbol="ETH/USDT", quantity_base=Decimal("1"), cost_basis_quote=Decimal("100")
        )

        routed = strategy.on_candle(make_candle(0, 100.0), position)

        assert routed is not None and routed.side == Side.SELL

    def test_name_is_the_routers_for_signal_lineage(self) -> None:
        assert make_self_routed().name == "regime_router"


class TestBuildTradedStrategy:
    def test_regime_routed_builds_the_family_router(self) -> None:
        assert build_traded_strategy(regime_routed=True).name == "regime_router"

    def test_without_the_gate_the_trend_family_trades_alone(self) -> None:
        assert build_traded_strategy(regime_routed=False).name == "trend_following"

    def test_every_call_builds_a_fresh_instance(self) -> None:
        # The ScenarioEvaluator contract: indicator state must never bleed
        # across scenarios.
        assert build_traded_strategy(True) is not build_traded_strategy(True)
