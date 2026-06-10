"""Generator tests: stratified, seeded, in-bounds scenario sampling."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from tradebot.core.models import Candle, CandleInterval
from tradebot.evaluation import GeneratorConfig, TrendLabel, generate_specs

BASE_TIME = datetime(2026, 1, 2, 0, 0, tzinfo=UTC)


def make_series(closes: list[float]) -> list[Candle]:
    candles: list[Candle] = []
    previous = closes[0]
    for index, close in enumerate(closes):
        open_time = BASE_TIME + timedelta(minutes=index)
        candles.append(
            Candle(
                symbol="BTC/USDT",
                interval=CandleInterval.M1,
                open_time=open_time,
                close_time=open_time + timedelta(minutes=1),
                open_quote=Decimal(str(round(previous, 8))),
                high_quote=Decimal(str(round(max(previous, close) + 0.5, 8))),
                low_quote=Decimal(str(round(min(previous, close) - 0.5, 8))),
                close_quote=Decimal(str(round(close, 8))),
                volume_base=Decimal("1"),
            )
        )
        previous = close
    return candles


def mixed_market() -> list[Candle]:
    """A long uptrend, a chop block, and a downtrend — several strata."""
    up = [100.0 * (1.0 + 0.004) ** i for i in range(400)]
    top = up[-1]
    chop = [top + (1.0 if i % 2 == 0 else -1.0) for i in range(400)]
    down = [top * (1.0 - 0.004) ** i for i in range(400)]
    return make_series(up + chop + down)


CONFIG = GeneratorConfig(scenario_count=40, lookback_candles=60, horizon_candles=30, seed=7)


class TestGeneration:
    def test_specs_are_in_bounds_and_time_ordered(self) -> None:
        candles = mixed_market()
        specs = generate_specs(candles, CONFIG)

        assert len(specs) == CONFIG.scenario_count
        indices = [spec.decision_index for spec, _ in specs]
        assert indices == sorted(indices)
        for spec, _ in specs:
            assert spec.decision_index >= CONFIG.lookback_candles
            assert spec.decision_index + CONFIG.horizon_candles <= len(candles)

    def test_sampling_is_deterministic_per_seed(self) -> None:
        candles = mixed_market()
        first = generate_specs(candles, CONFIG)
        second = generate_specs(candles, CONFIG)
        reseeded = generate_specs(candles, CONFIG.model_copy(update={"seed": 8}))

        assert first == second  # same series, config, seed: byte-identical
        assert first != reseeded

    def test_strata_cover_distinct_market_conditions(self) -> None:
        """A mixed market must yield scenarios from more than one regime —
        that coverage is the whole point of stratification."""
        specs = generate_specs(mixed_market(), CONFIG)
        trends = {conditions.trend for _, conditions in specs}
        assert TrendLabel.UP in trends
        assert TrendLabel.DOWN in trends
        assert len(trends) >= 2

    def test_short_series_is_refused(self) -> None:
        candles = mixed_market()[:80]  # cannot host lookback 60 + horizon 30
        with pytest.raises(ValueError, match="cannot host"):
            generate_specs(candles, CONFIG)

    def test_requesting_more_than_exists_returns_what_exists(self) -> None:
        candles = mixed_market()[:200]
        greedy = GeneratorConfig(scenario_count=10_000, lookback_candles=60, horizon_candles=30)
        specs = generate_specs(candles, greedy)
        assert 0 < len(specs) <= 10_000
        # Never the same decision point twice.
        indices = [spec.decision_index for spec, _ in specs]
        assert len(indices) == len(set(indices))
