"""Finding miner tests: patterns appear with evidence and vanish below thresholds."""

from datetime import UTC, datetime
from decimal import Decimal

from tradebot.evaluation.learning import MIN_EVIDENCE, WRONG_HOLD_R, mine_findings
from tradebot.evaluation.models import (
    MarketConditions,
    Scenario,
    ScenarioClass,
    ScenarioResult,
    TimingLabel,
    TrendLabel,
    Verdict,
    VolatilityLabel,
)

NOW = datetime(2026, 1, 2, 0, 0, tzinfo=UTC)


def make_record(
    scenario_id: int,
    *,
    trend: TrendLabel = TrendLabel.UP,
    r_multiple: str | None = None,
    verdict: Verdict = Verdict.NEUTRAL,
    timing: TimingLabel | None = None,
    oracle_r: str | None = None,
    decision: str = "buy",
) -> tuple[Scenario, ScenarioResult]:
    scenario = Scenario(
        run_id=1,
        symbol="BTC/USDT",
        timeframe="1h",
        decision_time=NOW,
        lookback_candles=60,
        scenario_class=ScenarioClass.FLAT,
        conditions=MarketConditions(trend=trend, volatility=VolatilityLabel.NORMAL),
        seed=7,
    )
    result = ScenarioResult(
        scenario_id=scenario_id,
        decision=decision,
        r_multiple=Decimal(r_multiple) if r_multiple is not None else None,
        oracle_r=Decimal(oracle_r) if oracle_r is not None else None,
        verdict=verdict,
        timing=timing,
        created_at=NOW,
    )
    return scenario, result


class TestLosingBuckets:
    def test_losing_condition_bucket_becomes_a_finding(self) -> None:
        records = [
            make_record(index, trend=TrendLabel.RANGING, r_multiple="-0.5")
            for index in range(MIN_EVIDENCE)
        ]

        findings = mine_findings(1, records, NOW)

        ranging = [f for f in findings if "trend is ranging" in f.pattern]
        assert len(ranging) == 1
        assert ranging[0].affected_count == MIN_EVIDENCE
        assert ranging[0].average_r_impact == Decimal("-0.5")
        assert ranging[0].evidence_scenario_ids == tuple(range(MIN_EVIDENCE))
        assert ranging[0].status == "proposed"
        assert ranging[0].confidence == "low"

    def test_profitable_buckets_and_thin_evidence_stay_silent(self) -> None:
        profitable = [
            make_record(index, trend=TrendLabel.UP, r_multiple="1.0")
            for index in range(MIN_EVIDENCE)
        ]
        # Thin losses are kept small so the shared buckets (volatility,
        # timeframe, symbol) stay profitable — only the under-evidenced
        # trend=down bucket is losing, and it must stay silent.
        thin = [
            make_record(100 + index, trend=TrendLabel.DOWN, r_multiple="-0.5")
            for index in range(MIN_EVIDENCE - 1)
        ]

        findings = mine_findings(1, profitable + thin, NOW)

        assert findings == []

    def test_confidence_scales_with_evidence(self) -> None:
        records = [
            make_record(index, trend=TrendLabel.RANGING, r_multiple="-0.5") for index in range(20)
        ]
        (finding,) = [f for f in mine_findings(1, records, NOW) if "ranging" in f.pattern]
        assert finding.confidence == "high"


class TestTimingPatterns:
    def test_late_entries_become_a_finding_above_the_share(self) -> None:
        late = [
            make_record(
                index, r_multiple="-0.6", timing=TimingLabel.LATE_ENTRY, verdict=Verdict.BAD
            )
            for index in range(MIN_EVIDENCE)
        ]
        on_time = [
            make_record(100 + index, r_multiple="0.5", timing=TimingLabel.ON_TIME)
            for index in range(5)
        ]

        findings = mine_findings(1, late + on_time, NOW)

        chasing = [f for f in findings if "chase" in f.pattern]
        assert len(chasing) == 1
        assert chasing[0].affected_count == MIN_EVIDENCE

    def test_a_few_late_entries_among_many_trades_are_not_a_pattern(self) -> None:
        late = [
            make_record(index, r_multiple="-0.6", timing=TimingLabel.LATE_ENTRY)
            for index in range(MIN_EVIDENCE)
        ]
        on_time = [
            make_record(100 + index, r_multiple="0.5", timing=TimingLabel.ON_TIME)
            for index in range(30)
        ]

        findings = mine_findings(1, late + on_time, NOW)

        assert [f for f in findings if "chase" in f.pattern] == []

    def test_early_exits_report_the_r_left_on_table(self) -> None:
        early = [
            make_record(
                index,
                r_multiple="0.5",
                oracle_r="2.0",
                timing=TimingLabel.EARLY_EXIT,
                verdict=Verdict.GOOD,
            )
            for index in range(MIN_EVIDENCE)
        ]

        findings = mine_findings(1, early, NOW)

        cutting = [f for f in findings if "cut winners" in f.pattern]
        assert len(cutting) == 1
        assert cutting[0].average_r_impact == Decimal("-1.5")  # 0.5 achieved vs 2.0 possible


class TestHoldPatterns:
    def test_missed_opportunities_become_a_finding(self) -> None:
        missed = [
            make_record(
                index,
                decision="hold",
                verdict=Verdict.MISSED_OPPORTUNITY,
                oracle_r="1.5",
            )
            for index in range(MIN_EVIDENCE)
        ]
        correct = [
            make_record(100 + index, decision="hold", verdict=Verdict.CORRECT_HOLD)
            for index in range(5)
        ]

        findings = mine_findings(1, missed + correct, NOW)

        timid = [f for f in findings if "stays flat" in f.pattern]
        assert len(timid) == 1
        assert timid[0].average_r_impact == Decimal("-1.5")  # foregone reference R

    def test_wrong_holds_are_counted_at_minus_one_r(self) -> None:
        wrong = [
            make_record(index, decision="hold", verdict=Verdict.WRONG_HOLD)
            for index in range(MIN_EVIDENCE)
        ]

        findings = mine_findings(1, wrong, NOW)

        stops = [f for f in findings if "ride into their stops" in f.pattern]
        assert len(stops) == 1
        assert stops[0].average_r_impact == WRONG_HOLD_R

    def test_mostly_correct_holds_are_not_a_pattern(self) -> None:
        missed = [
            make_record(index, decision="hold", verdict=Verdict.MISSED_OPPORTUNITY, oracle_r="1.5")
            for index in range(MIN_EVIDENCE)
        ]
        correct = [
            make_record(100 + index, decision="hold", verdict=Verdict.CORRECT_HOLD)
            for index in range(30)
        ]

        findings = mine_findings(1, missed + correct, NOW)

        assert [f for f in findings if "stays flat" in f.pattern] == []
