"""Cost-sensitivity helpers: scaling costs and summarizing the R streams."""

from decimal import Decimal

from tradebot.evaluation.sensitivity import (
    DEFAULT_COST_MULTIPLIERS,
    scale_fill_costs,
    summarize_cost_sensitivity,
)
from tradebot.execution import FillSimulatorConfig


class TestScaleFillCosts:
    def test_scales_every_cost_by_an_integer_multiplier(self) -> None:
        fills = FillSimulatorConfig(
            maker_fee_bps=Decimal(8),
            taker_fee_bps=Decimal(10),
            spread_bps=Decimal(2),
            market_slippage_bps=Decimal(5),
        )
        doubled = scale_fill_costs(fills, 2.0)
        assert doubled.maker_fee_bps == Decimal(16)
        assert doubled.taker_fee_bps == Decimal(20)
        assert doubled.spread_bps == Decimal(4)
        assert doubled.market_slippage_bps == Decimal(10)

    def test_fractional_multiplier_stays_exact(self) -> None:
        fills = FillSimulatorConfig(taker_fee_bps=Decimal(10), market_slippage_bps=Decimal(5))
        scaled = scale_fill_costs(fills, 1.5)
        assert scaled.taker_fee_bps == Decimal("15")
        assert scaled.market_slippage_bps == Decimal("7.5")

    def test_the_base_config_is_not_mutated(self) -> None:
        fills = FillSimulatorConfig(taker_fee_bps=Decimal(10))
        scale_fill_costs(fills, 2.0)
        assert fills.taker_fee_bps == Decimal(10)


class TestSummarize:
    def test_points_carry_per_level_quality_and_survives_when_positive(self) -> None:
        graded = [
            (1.0, (Decimal("1"), Decimal("1"), Decimal("-1"))),
            (1.5, (Decimal("0.5"), Decimal("0.5"), Decimal("-1"))),
            (2.0, (Decimal("0.3"), Decimal("0.2"))),
        ]
        block = summarize_cost_sensitivity(graded)

        assert [point["multiplier"] for point in block["points"]] == ["1", "1.5", "2"]
        assert block["points"][0]["trade_count"] == 3
        assert block["points"][0]["expectancy_r"] == "0.3333"
        # Worst level still averages +0.25R, so the edge survives.
        assert block["survives_worse_costs"] is True

    def test_does_not_survive_when_the_worst_level_goes_negative(self) -> None:
        graded = [(1.0, (Decimal("0.5"),)), (2.0, (Decimal("-0.5"), Decimal("0.1")))]
        block = summarize_cost_sensitivity(graded)
        assert block["survives_worse_costs"] is False

    def test_does_not_survive_when_the_worst_level_has_no_trades(self) -> None:
        graded: list[tuple[float, tuple[Decimal, ...]]] = [(1.0, (Decimal("0.5"),)), (2.0, ())]
        block = summarize_cost_sensitivity(graded)
        assert block["points"][-1]["expectancy_r"] is None
        assert block["survives_worse_costs"] is False

    def test_default_multipliers_are_one_and_a_half_and_double(self) -> None:
        assert DEFAULT_COST_MULTIPLIERS == (1.5, 2.0)
