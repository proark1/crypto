"""Tests for the evaluation run report's money result (ARCHITECTURE.md §12.3).

The R-multiple metrics have their own coverage; here we pin the illustrative
"what would 10,000 have become" figure: the fixed-fractional, compounding
equity curve, its order-independence, and that it rides into the summary.
"""

from datetime import UTC, datetime
from decimal import Decimal

from tradebot.evaluation.models import (
    MarketConditions,
    Scenario,
    ScenarioClass,
    ScenarioResult,
    TrendLabel,
    Verdict,
    VolatilityLabel,
)
from tradebot.evaluation.reports import (
    EVALUATION_RISK_PER_TRADE_FRACTION,
    EVALUATION_START_BALANCE_QUOTE,
    build_summary,
    money_result,
)

BASE_TIME = datetime(2026, 1, 2, tzinfo=UTC)


def _record(r_multiple: Decimal | None, scenario_id: int) -> tuple[Scenario, ScenarioResult]:
    scenario = Scenario(
        run_id=1,
        symbol="BTC/USDT",
        timeframe="1h",
        decision_time=BASE_TIME,
        lookback_candles=200,
        scenario_class=ScenarioClass.FLAT,
        conditions=MarketConditions(trend=TrendLabel.UP, volatility=VolatilityLabel.NORMAL),
        seed=7,
    )
    result = ScenarioResult(
        scenario_id=scenario_id,
        decision="buy" if r_multiple is not None else "hold",
        r_multiple=r_multiple,
        verdict=Verdict.GOOD if (r_multiple or Decimal(0)) > 0 else Verdict.CORRECT_HOLD,
        created_at=BASE_TIME,
    )
    return scenario, result


class TestMoneyResult:
    def test_no_trades_returns_the_stake_unchanged(self) -> None:
        result = money_result([])
        assert result["starting_balance_quote"] == "10000.00"
        assert result["final_balance_quote"] == "10000.00"
        assert result["net_pnl_quote"] == "0.00"
        assert result["return_fraction"] == "0.0000"

    def test_one_winning_r_compounds_at_the_risk_fraction(self) -> None:
        # +2R at 1% risk grows a 10,000 stake by 2% to 10,200.
        result = money_result([Decimal("2")])
        assert result["final_balance_quote"] == "10200.00"
        assert result["net_pnl_quote"] == "200.00"
        assert result["return_fraction"] == "0.0200"

    def test_a_losing_run_shrinks_the_stake(self) -> None:
        result = money_result([Decimal("-1"), Decimal("-1")])
        # 10000 * 0.99 * 0.99 = 9801.00
        assert result["final_balance_quote"] == "9801.00"
        assert result["net_pnl_quote"] == "-199.00"

    def test_order_independent(self) -> None:
        forward = money_result([Decimal("2"), Decimal("-1"), Decimal("0.5")])
        backward = money_result([Decimal("0.5"), Decimal("-1"), Decimal("2")])
        assert forward["final_balance_quote"] == backward["final_balance_quote"]

    def test_constants_match_the_live_risk_default(self) -> None:
        assert Decimal("10000") == EVALUATION_START_BALANCE_QUOTE
        assert Decimal("0.01") == EVALUATION_RISK_PER_TRADE_FRACTION


class TestBuildSummary:
    def test_carries_the_money_result_alongside_the_r_metrics(self) -> None:
        records = [_record(Decimal("1"), 1), _record(Decimal("-0.5"), 2), _record(None, 3)]
        summary = build_summary(records)

        # Money keys are present and computed only from the two graded trades.
        assert summary["starting_balance_quote"] == "10000.00"
        # 10000 * 1.01 * 0.995 = 10049.50
        assert summary["final_balance_quote"] == "10049.50"
        assert summary["net_pnl_quote"] == "49.50"
        # The R-multiple block still rides along untouched.
        assert summary["trade_count"] == 2
        assert "expectancy_r" in summary

    def test_a_run_with_no_trades_still_reports_the_stake(self) -> None:
        summary = build_summary([_record(None, 1)])
        assert summary["trade_count"] == 0
        assert summary["final_balance_quote"] == "10000.00"
