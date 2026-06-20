"""Lineup contracts: identities stay stable, signal scoping prevents collisions."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from tradebot.competition import (
    LINEUP,
    PRODUCTION_BOT_ID,
    build_challenger_strategy,
    build_scenario_strategy,
    spec_for,
)
from tradebot.core.models import Candle, CandleInterval, Side
from tradebot.strategies import MomentumConfig, MomentumStrategy

BASE_TIME = datetime(2026, 1, 2, 0, 0, tzinfo=UTC)


class _ConstantFunding:
    """A funding provider reporting one rate for every lookup."""

    def __init__(self, rate: Decimal) -> None:
        self._rate = rate

    def rate_as_of(self, symbol: str, at: datetime) -> Decimal | None:
        return self._rate


def make_candle(index: int, close: float) -> Candle:
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
        volume_base=Decimal("10"),
    )


class TestLineup:
    def test_eleven_competitors_with_unique_stable_identities(self) -> None:
        assert len(LINEUP) == 11
        bot_ids = [spec.bot_id for spec in LINEUP]
        assert len(set(bot_ids)) == 11
        assert PRODUCTION_BOT_ID in bot_ids
        assert {"supertrend", "bollinger_reversion", "adx_trend", "keltner"} <= set(bot_ids)
        # Risk-state rows must never collide: each account's brakes are
        # persisted under its fixed row id.
        row_ids = [spec.risk_state_row_id for spec in LINEUP]
        assert len(set(row_ids)) == 11
        assert spec_for(PRODUCTION_BOT_ID).risk_state_row_id == 1
        assert spec_for("keltner").risk_state_row_id == 11

    def test_funding_challenger_trades_on_the_injected_series(self) -> None:
        # The funding bot is inert without a provider; given one, it trades —
        # the live wiring the worker supplies.
        strategy = build_challenger_strategy(
            spec_for("funding"), {"funding": {"atr_period": 3}}, _ConstantFunding(Decimal("-0.001"))
        )
        signals = [strategy.on_candle(make_candle(i, 100.0), None) for i in range(5)]
        assert any(s is not None and s.side == Side.BUY for s in signals)

    def test_unknown_bot_id_raises_with_the_known_ones(self) -> None:
        with pytest.raises(ValueError, match="unknown competitor"):
            spec_for("martingale")

    def test_production_has_no_solo_family_to_build(self) -> None:
        with pytest.raises(ValueError, match="no solo family"):
            build_challenger_strategy(spec_for(PRODUCTION_BOT_ID), {})


class TestScopedSignals:
    def test_challenger_signal_ids_are_prefixed_with_the_bot(self) -> None:
        """Two bots trading the same family must never mint the same order id."""
        params = MomentumConfig(
            fast_ema_period=3, slow_ema_period=6, signal_ema_period=3, atr_period=3
        ).model_dump()
        scoped = build_challenger_strategy(spec_for("momentum"), {"momentum": params})
        bare = MomentumStrategy(MomentumConfig(**params))
        scoped_signal = None
        bare_signal = None
        closes = [100.0] * 12 + [110.0]
        for index, close in enumerate(closes):
            candle = make_candle(index, close)
            scoped_signal = scoped.on_candle(candle, None)
            bare_signal = bare.on_candle(candle, None)
        assert scoped_signal is not None and bare_signal is not None
        assert scoped_signal.side == Side.BUY
        assert scoped_signal.signal_id == f"momentum/{bare_signal.signal_id}"
        # Lineage stays honest: gates and reports see the real family.
        assert scoped_signal.strategy_name == "momentum"
        assert scoped.name == "momentum"

    def test_challengers_use_their_family_active_params(self) -> None:
        scoped = build_challenger_strategy(
            spec_for("mean_reversion"), {"mean_reversion": {"rsi_period": 7}}
        )
        assert scoped.name == "mean_reversion"


class TestScenarioStrategies:
    def test_production_grades_the_routed_shape(self) -> None:
        strategy = build_scenario_strategy(spec_for(PRODUCTION_BOT_ID), {}, regime_routed=True)
        assert strategy.name == "regime_router"

    def test_production_without_the_gate_grades_bare_trend(self) -> None:
        strategy = build_scenario_strategy(spec_for(PRODUCTION_BOT_ID), {}, regime_routed=False)
        assert strategy.name == "trend_following"

    def test_challengers_grade_their_family_solo_and_unscoped(self) -> None:
        for bot_id in ("trend_following", "mean_reversion", "breakout", "momentum", "squeeze"):
            strategy = build_scenario_strategy(spec_for(bot_id), {}, regime_routed=True)
            assert strategy.name == bot_id
