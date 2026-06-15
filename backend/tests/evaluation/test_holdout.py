"""Holdout honesty-read tests.

The read is pure-ish: ``_read`` composes the verdict from numbers (unit
tested directly), and ``make_holdout_grader`` runs the blind pipeline over a
fetched span (tested with an in-memory candle reader, no Postgres). The
contract that matters: both configurations are graded on identical
scenarios, a thin slice withholds the verdict, and nothing raises.
"""

from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from tradebot.core.models import Candle, CandleInterval, utc_now
from tradebot.evaluation.holdout import _read, make_holdout_grader
from tradebot.evaluation.sweep import SweepCandidate, build_candidate_strategy
from tradebot.strategies import Strategy

_T0 = datetime(2026, 6, 13, tzinfo=UTC)
_T1 = datetime(2026, 6, 15, tzinfo=UTC)


class TestRead:
    def test_an_improvement_is_judged(self) -> None:
        read = _read(_T0, _T1, 1500, Decimal("0.05"), 30, Decimal("0.20"), 28)
        assert read["judged"] is True and read["improved"] is True
        assert read["delta_r"] == "0.15"
        assert read["start_expectancy_r"] == "0.05"
        assert "an improvement" in read["explanation"]

    def test_a_regression_is_judged_but_not_improved(self) -> None:
        read = _read(_T0, _T1, 1500, Decimal("0.20"), 30, Decimal("0.05"), 28)
        assert read["judged"] is True and read["improved"] is False
        assert "no improvement" in read["explanation"]

    def test_a_thin_holdout_withholds_the_verdict(self) -> None:
        read = _read(_T0, _T1, 80, Decimal("0.9"), 3, None, 0)
        assert read["judged"] is False and read["improved"] is False
        assert read["final_expectancy_r"] is None
        assert "too thin" in read["explanation"]

    def test_the_explanation_rounds_r_but_the_fields_keep_full_precision(self) -> None:
        read = _read(_T0, _T1, 1500, Decimal("0.050000000000"), 30, Decimal("0.200000000000"), 28)
        assert "from 0.0500R to 0.2000R" in read["explanation"]
        assert "0.050000000000" not in read["explanation"]
        # raw fields keep full ACCOUNTING_RESOLUTION precision for the API
        assert read["start_expectancy_r"] == "0.050000000000"
        assert read["final_expectancy_r"] == "0.200000000000"


class FakeCandles:
    """An in-memory ``fetch_range`` that records the span it was asked for."""

    def __init__(self, candles: list[Candle]) -> None:
        self._candles = sorted(candles, key=lambda candle: candle.open_time)
        self.start: datetime | None = None
        self.end: datetime | None = None

    async def fetch_range(
        self, symbol: str, interval: CandleInterval, start: datetime, end: datetime
    ) -> list[Candle]:
        self.start, self.end = start, end
        return [candle for candle in self._candles if start <= candle.open_time < end]


def _minute_candles(last_open: datetime, count: int) -> list[Candle]:
    """``count`` ascending 1m candles with alternating drift regimes."""
    candles: list[Candle] = []
    price = 100.0
    for index in range(count):
        open_time = last_open - timedelta(minutes=count - 1 - index)
        drift = 0.003 if (index // 40) % 2 == 0 else -0.002
        previous = price
        price = max(1.0, price * (1.0 + drift + (0.0005 if index % 2 == 0 else -0.0005)))
        candles.append(
            Candle(
                symbol="BTC/USDT",
                interval=CandleInterval.M1,
                open_time=open_time,
                close_time=open_time + timedelta(minutes=1),
                open_quote=Decimal(str(round(previous, 8))),
                high_quote=Decimal(str(round(max(previous, price) + 0.1, 8))),
                low_quote=Decimal(str(round(min(previous, price) - 0.1, 8))),
                close_quote=Decimal(str(round(price, 8))),
                volume_base=Decimal("1"),
            )
        )
    return candles


def _trend_strategy_for(params: Mapping[str, Mapping[str, Any]]) -> Strategy:
    return build_candidate_strategy(
        SweepCandidate(
            name="holdout",
            family="trend_following",
            params=dict(params.get("trend_following", {})),
        )
    )


def _grader(reader: FakeCandles, now: datetime) -> Any:
    return make_holdout_grader(
        symbol="BTC/USDT",
        timeframe="1m",
        candles=reader,
        strategy_for=_trend_strategy_for,
        scenario_count=300,
        lookback_candles=60,
        horizon_candles=30,
        clock=lambda: now,
    )


class TestHoldoutGrader:
    async def test_grades_on_the_reserved_span(self) -> None:
        now = utc_now().replace(second=0, microsecond=0)
        holdout_start = now - timedelta(days=2)
        reader = FakeCandles(_minute_candles(now - timedelta(minutes=1), 1500))
        grade = _grader(reader, now)

        read = await grade(
            {"trend_following": {"fast_ema_period": 10, "slow_ema_period": 30}},
            {"trend_following": {"fast_ema_period": 5, "slow_ema_period": 20}},
            holdout_start,
        )

        assert read is not None
        # the span fetched is exactly the reserved holdout, nothing earlier
        assert reader.start == holdout_start and reader.end == now
        assert read["holdout_candles"] == 1500
        assert set(read) >= {
            "start_expectancy_r",
            "final_expectancy_r",
            "delta_r",
            "judged",
            "improved",
            "explanation",
        }

    async def test_identical_configs_show_no_move(self) -> None:
        now = utc_now().replace(second=0, microsecond=0)
        reader = FakeCandles(_minute_candles(now - timedelta(minutes=1), 1500))
        grade = _grader(reader, now)
        config = {"trend_following": {"fast_ema_period": 5, "slow_ema_period": 20}}

        read = await grade(config, config, now - timedelta(days=2))

        # Same config on the same scenarios must grade identically.
        assert read is not None
        assert read["start_expectancy_r"] == read["final_expectancy_r"]
        assert read["start_trades"] == read["final_trades"]
        assert read["improved"] is False
        if read["delta_r"] is not None:
            assert Decimal(read["delta_r"]) == 0

    async def test_a_short_holdout_resolves_to_a_thin_read_not_an_error(self) -> None:
        now = utc_now().replace(second=0, microsecond=0)
        reader = FakeCandles(
            _minute_candles(now - timedelta(minutes=1), 50)
        )  # < lookback + horizon
        grade = _grader(reader, now)

        read = await grade(
            {"trend_following": {}}, {"trend_following": {}}, now - timedelta(days=2)
        )

        assert read is not None
        assert read["judged"] is False
        assert read["start_expectancy_r"] is None and read["final_expectancy_r"] is None
        assert "too thin" in read["explanation"]
