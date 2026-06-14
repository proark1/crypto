"""Scenario evaluator tests — above all, that the future cannot leak.

Two strategy stand-ins: the real trend follower proves integration (leak
test, a found-in-the-wild winning entry), and a scripted strategy gives the
grading tests precise control over when the bot acts and where its stop
sits — verdict and timing rules are then assertable exactly, not "if the
EMAs happened to cross".
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from tradebot.core.models import Candle, CandleInterval, Side, Signal
from tradebot.evaluation import (
    ScenarioClass,
    ScenarioEvaluator,
    ScenarioSpec,
    TimingLabel,
    Verdict,
)
from tradebot.strategies import Strategy, TrendFollowingConfig, TrendFollowingStrategy

BASE_TIME = datetime(2026, 1, 2, 0, 0, tzinfo=UTC)
LOOKBACK = 30
HORIZON = 20


def make_trend_strategy() -> Strategy:
    """Short periods so crosses happen inside small test windows."""
    return TrendFollowingStrategy(
        TrendFollowingConfig(fast_ema_period=3, slow_ema_period=6, atr_period=3)
    )


class ScriptedStrategy:
    """Buys/sells on the n-th candle it sees, with a chosen stop distance.

    Counting from the window start (the evaluator feeds the window first,
    then the horizon, through one instance), ``buy_on`` / ``sell_on`` pick
    the exact decision moments; the final window candle is call number
    ``LOOKBACK``.
    """

    def __init__(self, buy_on: int | None, sell_on: int | None, stop_distance: str) -> None:
        self._buy_on = buy_on
        self._sell_on = sell_on
        self._stop_distance = Decimal(stop_distance)
        self._calls = 0

    @property
    def name(self) -> str:
        return "scripted"

    def on_candle(self, candle: Candle, position: object) -> Signal | None:
        self._calls += 1
        signal_id = f"scripted:{candle.symbol}:{self._calls}"
        common = {
            "signal_id": signal_id,
            "strategy_name": self.name,
            "symbol": candle.symbol,
            "confidence": 1.0,
            "reasons": ("scripted",),
            "created_at": candle.close_time,
        }
        if self._calls == self._buy_on:
            return Signal(
                side=Side.BUY,
                stop_price_quote=candle.close_quote - self._stop_distance,
                **common,
            )
        if self._calls == self._sell_on:
            return Signal(side=Side.SELL, stop_price_quote=candle.close_quote, **common)
        return None


def scripted(
    buy_on: int | None = None, sell_on: int | None = None, stop_distance: str = "10"
) -> ScenarioEvaluator:
    return ScenarioEvaluator(lambda: ScriptedStrategy(buy_on, sell_on, stop_distance))


def make_series(closes: list[float]) -> list[Candle]:
    candles: list[Candle] = []
    closes = [max(close, 2.0) for close in closes]  # prices stay positive
    previous = closes[0]
    for index, close in enumerate(closes):
        open_time = BASE_TIME + timedelta(minutes=index)
        body_high = max(previous, close)
        body_low = min(previous, close)
        candles.append(
            Candle(
                symbol="BTC/USDT",
                interval=CandleInterval.M1,
                open_time=open_time,
                close_time=open_time + timedelta(minutes=1),
                open_quote=Decimal(str(round(previous, 8))),
                high_quote=Decimal(str(round(body_high + 0.5, 8))),
                low_quote=Decimal(str(round(body_low - 0.5, 8))),
                close_quote=Decimal(str(round(close, 8))),
                volume_base=Decimal("1"),
            )
        )
        previous = close
    return candles


def spec_at(index: int) -> ScenarioSpec:
    return ScenarioSpec(decision_index=index, lookback=LOOKBACK, horizon=HORIZON)


def ohlc(open_: float, high: float, low: float, close: float, index: int) -> Candle:
    """A candle with explicit OHLC — for gaps ``make_series`` cannot express."""
    open_time = BASE_TIME + timedelta(minutes=index)
    return Candle(
        symbol="BTC/USDT",
        interval=CandleInterval.M1,
        open_time=open_time,
        close_time=open_time + timedelta(minutes=1),
        open_quote=Decimal(str(open_)),
        high_quote=Decimal(str(high)),
        low_quote=Decimal(str(low)),
        close_quote=Decimal(str(close)),
        volume_base=Decimal("1"),
    )


# A decline longer than the lookback, then a rise: the real strategy's
# cross lands on the final window candle at decision index 42.
DOWN = [100.0 - 0.3 * i for i in range(40)]
RISE = [88.5 + 1.5 * i for i in range(25)]


class TestLeakProofing:
    """The same window must produce the same decision under any future."""

    def test_decision_is_identical_under_opposite_futures(self) -> None:
        prefix = DOWN + RISE
        boom = make_series(prefix + [126.0 + 2.0 * i for i in range(HORIZON + 5)])
        crash = make_series(prefix + [126.0 - 6.0 * i for i in range(HORIZON + 5)])
        evaluator = ScenarioEvaluator(make_trend_strategy)
        spec = spec_at(len(prefix))

        in_boom = evaluator.evaluate(boom, spec)
        in_crash = evaluator.evaluate(crash, spec)

        # Blind fields are identical bit for bit...
        assert in_boom.decision == in_crash.decision
        assert in_boom.scenario_class == in_crash.scenario_class
        assert in_boom.confidence == in_crash.confidence
        assert in_boom.reasons == in_crash.reasons
        # ...while the graded outcome differs, proving the reveal happened.
        assert in_boom.verdict != in_crash.verdict

    def test_horizon_bounds_are_enforced(self) -> None:
        candles = make_series(DOWN + RISE)
        evaluator = ScenarioEvaluator(make_trend_strategy)
        with pytest.raises(ValueError, match="past the end"):
            evaluator.evaluate(candles, spec_at(len(candles) - 5))
        with pytest.raises(ValueError, match="before the start"):
            evaluator.evaluate(
                candles, ScenarioSpec(decision_index=10, lookback=LOOKBACK, horizon=5)
            )


class TestRealStrategyIntegration:
    def test_cross_into_rally_is_an_excellent_on_time_entry(self) -> None:
        candles = make_series(DOWN + [88.0, 90.0, 92.0] + [94.0 + 3.0 * i for i in range(HORIZON)])
        outcome = ScenarioEvaluator(make_trend_strategy).evaluate(candles, spec_at(42))

        assert outcome.scenario_class == ScenarioClass.FLAT
        assert outcome.decision == "buy"
        assert outcome.reasons  # the bot's own words survive into the record
        assert outcome.verdict == Verdict.EXCELLENT
        assert outcome.timing == TimingLabel.ON_TIME
        assert outcome.stop_hit is False
        assert outcome.oracle_r is not None and outcome.mfe_r is not None
        assert outcome.r_multiple is not None
        # The hindsight-best exit can never be worse than what was achieved.
        assert outcome.oracle_r >= outcome.mfe_r >= outcome.r_multiple


class TestEntryGrading:
    """Scripted entries: the stop and the moment of action are exact."""

    def test_immediate_crash_is_very_bad_and_late(self) -> None:
        flat = [100.0] * LOOKBACK
        crash = [96.0 - 4.0 * i for i in range(HORIZON)]
        candles = make_series(flat + crash)
        outcome = scripted(buy_on=LOOKBACK, stop_distance="10").evaluate(candles, spec_at(LOOKBACK))

        assert outcome.stop_hit is True
        assert outcome.r_multiple is not None and outcome.r_multiple <= Decimal("-1")
        assert outcome.verdict == Verdict.VERY_BAD
        # No favorable excursion to speak of: the move was over at entry.
        assert outcome.timing == TimingLabel.LATE_ENTRY

    def test_deep_dip_then_recovery_is_an_early_entry(self) -> None:
        flat = [100.0] * LOOKBACK
        # Dip ~0.6R below entry (stop is 10 away), then a strong recovery.
        dip_then_rally = [95.0, 94.0, 95.0] + [98.0 + 2.0 * i for i in range(HORIZON)]
        candles = make_series(flat + dip_then_rally)
        outcome = scripted(buy_on=LOOKBACK, stop_distance="10").evaluate(candles, spec_at(LOOKBACK))

        assert outcome.stop_hit is False
        assert outcome.r_multiple is not None and outcome.r_multiple >= 0
        assert outcome.mae_r is not None and outcome.mae_r <= Decimal("-0.5")
        assert outcome.timing == TimingLabel.EARLY_ENTRY

    def test_strategy_exit_that_gaps_through_the_stop_is_a_stop_out(self) -> None:
        # Buy at the decision (stop 10 below → 90), the strategy sells on the
        # first horizon candle, and the next candle gaps open to 85 — below the
        # resting stop. The exit fills at that open either way, but it must be
        # graded a stop-out and the MAE must capture the gap, not stay at the
        # prior candle's shallow low.
        window = [ohlc(100, 100.5, 99.5, 100, i) for i in range(LOOKBACK)]
        horizon = [ohlc(100, 101, 99, 100, LOOKBACK)]  # strategy sells here; no breach
        horizon.append(ohlc(85, 86, 80, 82, LOOKBACK + 1))  # gaps below the 90 stop
        horizon += [ohlc(82, 83, 81, 82, LOOKBACK + 2 + i) for i in range(HORIZON - 2)]
        outcome = scripted(buy_on=LOOKBACK, sell_on=LOOKBACK + 1, stop_distance="10").evaluate(
            window + horizon, spec_at(LOOKBACK)
        )

        assert outcome.decision == "buy"
        assert outcome.stop_hit is True  # the gap-down open reached the resting stop
        assert outcome.mae_r is not None and outcome.mae_r <= Decimal("-1")
        assert outcome.r_multiple is not None and outcome.r_multiple < 0

    def test_fixed_time_exit_closes_at_horizon_end(self) -> None:
        flat = [100.0] * LOOKBACK
        drift = [100.5 + 0.4 * i for i in range(HORIZON)]
        candles = make_series(flat + drift)
        outcome = scripted(buy_on=LOOKBACK, stop_distance="10").evaluate(candles, spec_at(LOOKBACK))

        assert outcome.stop_hit is False
        assert outcome.duration_candles == HORIZON  # held to the fixed-time exit
        assert outcome.r_multiple is not None and outcome.r_multiple > 0


class TestHoldGrading:
    def test_flat_hold_in_a_gentle_decline_is_correct(self) -> None:
        candles = make_series([100.0 - 0.05 * i for i in range(LOOKBACK + HORIZON + 10)])
        outcome = ScenarioEvaluator(make_trend_strategy).evaluate(candles, spec_at(LOOKBACK))

        assert outcome.scenario_class == ScenarioClass.FLAT
        assert outcome.decision == "hold"
        assert outcome.verdict == Verdict.CORRECT_HOLD
        assert outcome.r_multiple is None  # no trade happened

    def test_flat_hold_before_a_rocket_is_a_missed_opportunity(self) -> None:
        decline = [100.0 - 0.05 * i for i in range(LOOKBACK + 5)]
        rocket = [101.0 + 4.0 * i for i in range(HORIZON + 5)]
        candles = make_series(decline + rocket)
        # Decision still inside the decline: the bot cannot see the rocket.
        outcome = ScenarioEvaluator(make_trend_strategy).evaluate(candles, spec_at(LOOKBACK + 4))

        assert outcome.decision == "hold"
        assert outcome.verdict == Verdict.MISSED_OPPORTUNITY
        # oracle_r carries the reference trade's R: the size of the miss.
        assert outcome.oracle_r is not None and outcome.oracle_r >= Decimal(1)


class TestHoldingClass:
    """Scripted positions: entry on window candle 10, decision at the end."""

    def test_exit_after_giving_back_the_peak_is_late(self) -> None:
        # Entry near 100, run-up to ~115, slide back to ~104 by the decision.
        window = (
            [100.0] * 10
            + [101.0 + 2.0 * i for i in range(8)]
            + [115.0 - 1.0 * i for i in range(12)]
        )
        horizon = [104.0] * HORIZON
        candles = make_series(window + horizon)
        outcome = scripted(buy_on=10, sell_on=LOOKBACK, stop_distance="10").evaluate(
            candles, spec_at(LOOKBACK)
        )

        assert outcome.scenario_class == ScenarioClass.HOLDING
        assert outcome.decision == "sell"
        assert outcome.r_multiple is not None and outcome.r_multiple > 0  # still a winner
        assert outcome.timing == TimingLabel.LATE_EXIT  # but it gave back > 0.5R

    def test_holding_exit_oracle_includes_the_in_window_peak(self) -> None:
        # Ran up to ~115 in-window, slid to ~104 by the decision; the horizon
        # never revisits the peak. The hindsight-best exit reflects the
        # in-window run-up, so oracle_r can never read below the trade's own
        # MFE the way a horizon-only peak could.
        window = (
            [100.0] * 10
            + [101.0 + 2.0 * i for i in range(8)]
            + [115.0 - 1.0 * i for i in range(12)]
        )
        horizon = [104.0] * HORIZON
        candles = make_series(window + horizon)
        outcome = scripted(buy_on=10, sell_on=LOOKBACK, stop_distance="10").evaluate(
            candles, spec_at(LOOKBACK)
        )

        assert outcome.scenario_class == ScenarioClass.HOLDING
        assert outcome.decision == "sell"
        assert outcome.oracle_r is not None and outcome.mfe_r is not None
        assert outcome.oracle_r >= outcome.mfe_r

    def test_exit_right_before_a_rocket_is_early(self) -> None:
        window = [100.0] * LOOKBACK
        rocket = [102.0 + 3.0 * i for i in range(HORIZON)]
        candles = make_series(window + rocket)
        outcome = scripted(buy_on=10, sell_on=LOOKBACK, stop_distance="10").evaluate(
            candles, spec_at(LOOKBACK)
        )

        assert outcome.decision == "sell"
        assert outcome.timing == TimingLabel.EARLY_EXIT

    def test_holding_through_a_crash_is_a_wrong_hold(self) -> None:
        window = [100.0] * LOOKBACK
        crash = [96.0 - 4.0 * i for i in range(HORIZON)]
        candles = make_series(window + crash)
        outcome = scripted(buy_on=10, stop_distance="10").evaluate(candles, spec_at(LOOKBACK))

        assert outcome.scenario_class == ScenarioClass.HOLDING
        assert outcome.decision == "hold"
        assert outcome.stop_hit is True
        assert outcome.verdict == Verdict.WRONG_HOLD

    def test_holding_through_calm_water_is_a_correct_hold(self) -> None:
        window = [100.0] * LOOKBACK
        calm = [100.0 + (0.3 if i % 2 == 0 else -0.3) for i in range(HORIZON)]
        candles = make_series(window + calm)
        outcome = scripted(buy_on=10, stop_distance="10").evaluate(candles, spec_at(LOOKBACK))

        assert outcome.verdict == Verdict.CORRECT_HOLD
        assert outcome.stop_hit is False
