"""Mine mistake patterns from a completed run (ARCHITECTURE.md section 12).

Every miner here is mechanical and explainable: a pattern is a named group
of graded scenarios (a losing condition bucket, a cluster of late entries),
its impact is measured in R, and the evidence ids point straight at the
scenarios a human can replay. Findings only ever *recommend* — accepting or
rejecting one is a human action through the API, and nothing in this module
touches strategy configuration.

R-multiples are ratios of money and stay Decimal, like everywhere else.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from decimal import ROUND_HALF_EVEN, Decimal

from tradebot.core.models import ACCOUNTING_RESOLUTION
from tradebot.evaluation.models import (
    LearningFinding,
    Scenario,
    ScenarioResult,
    TimingLabel,
    Verdict,
)

MIN_EVIDENCE = 5
"""Patterns below this many scenarios are noise, not findings — a human's
attention is the scarcest resource in the loop."""

LOSING_BUCKET_EXPECTANCY_R = Decimal("-0.1")
"""A condition bucket is reported as losing when its expectancy is at or
below this; around zero is indistinguishable from fees and luck."""

TIMING_SHARE = Decimal("0.3")
"""A timing mistake (late entry, early exit) becomes a pattern when it
affects at least this share of the trades it could have affected."""

MISSED_OPPORTUNITY_SHARE = Decimal("0.3")
"""Flat holds are expected most of the time; only a high miss rate says the
bot is structurally too timid."""

HIGH_CONFIDENCE_EVIDENCE = 20
MEDIUM_CONFIDENCE_EVIDENCE = 10
"""Confidence is sample size, nothing fancier: a label a human can weigh."""

WRONG_HOLD_R = Decimal("-1")
"""A wrong hold rides the position into its stop, which is -1R by the
definition of R."""

_Record = tuple[Scenario, ScenarioResult]


def _knob_hint(dimension: str, label: str) -> str:
    """Name the sweepable knob that targets a losing bucket, when one exists.

    A suggestion that names a real setting closes the loop: the automated
    improver (§12.7) adds the matching challenger to its next sweep, with
    this finding as the recorded motivation.
    """
    if dimension == "trend" and label in ("down", "ranging"):
        return (
            " (sweepable knob: mean_reversion.trend_filter_ema_period — skip "
            "dip-buys while the coin trends down; the automated improver "
            "tests it when this finding appears)"
        )
    return ""


def mine_findings(run_id: int, records: list[_Record], now: datetime) -> list[LearningFinding]:
    """Return every pattern in ``records`` worth a human's accept/reject.

    Deterministic for a given run: same records, same findings, in a stable
    order (losing buckets first, then timing, then hold patterns).
    """
    findings = [
        *_losing_buckets(run_id, records, now),
        *_late_entries(run_id, records, now),
        *_early_exits(run_id, records, now),
        *_missed_opportunities(run_id, records, now),
        *_wrong_holds(run_id, records, now),
    ]
    return findings


def _losing_buckets(run_id: int, records: list[_Record], now: datetime) -> list[LearningFinding]:
    """One finding per market condition whose entries lose money."""
    dimensions: list[tuple[str, Callable[[Scenario], list[str]]]] = [
        ("trend", lambda scenario: [scenario.conditions.trend.value]),
        ("volatility", lambda scenario: [scenario.conditions.volatility.value]),
        ("timeframe", lambda scenario: [scenario.timeframe]),
        ("symbol", lambda scenario: [scenario.symbol]),
        ("event", lambda scenario: [event.value for event in scenario.conditions.events]),
    ]
    findings: list[LearningFinding] = []
    for dimension, labels_of in dimensions:
        groups: dict[str, list[ScenarioResult]] = {}
        for scenario, result in records:
            if result.r_multiple is None:
                continue
            for label in labels_of(scenario):
                groups.setdefault(label, []).append(result)
        for label, trades in sorted(groups.items()):
            if len(trades) < MIN_EVIDENCE:
                continue
            expectancy = _mean(
                [result.r_multiple for result in trades if result.r_multiple is not None]
            )
            if expectancy > LOSING_BUCKET_EXPECTANCY_R:
                continue
            findings.append(
                LearningFinding(
                    run_id=run_id,
                    pattern=f"entries lose money when {dimension} is {label}",
                    evidence_scenario_ids=tuple(result.scenario_id for result in trades),
                    affected_count=len(trades),
                    average_r_impact=expectancy,
                    suggestion=(
                        f"gate entries behind extra confirmation when {dimension} is "
                        f"{label}; expectancy there is {expectancy}R over {len(trades)} trades"
                        + _knob_hint(dimension, label)
                    ),
                    confidence=_confidence(len(trades)),
                    created_at=now,
                )
            )
    return findings


def _late_entries(run_id: int, records: list[_Record], now: datetime) -> list[LearningFinding]:
    """Losing trades that entered after the move was already over."""
    trades = [result for _, result in records if result.r_multiple is not None]
    late = [result for result in trades if result.timing == TimingLabel.LATE_ENTRY]
    if not _is_pattern(late, trades, TIMING_SHARE):
        return []
    impact = _mean([result.r_multiple for result in late if result.r_multiple is not None])
    return [
        LearningFinding(
            run_id=run_id,
            pattern="entries chase moves that are already over",
            evidence_scenario_ids=tuple(result.scenario_id for result in late),
            affected_count=len(late),
            average_r_impact=impact,
            suggestion=(
                "signals confirm too slowly; consider faster confirmation or skipping "
                "entries when the move already ran, instead of buying its top "
                "(sweepable knob: trend_following.max_entry_extension_atr — the "
                "automated improver tests it when this finding appears)"
            ),
            confidence=_confidence(len(late)),
            created_at=now,
        )
    ]


def _early_exits(run_id: int, records: list[_Record], now: datetime) -> list[LearningFinding]:
    """Report exits that left at least 0.5R of the move on the table."""
    trades = [result for _, result in records if result.r_multiple is not None]
    early = [
        result
        for result in trades
        if result.timing == TimingLabel.EARLY_EXIT and result.oracle_r is not None
    ]
    if not _is_pattern(early, trades, TIMING_SHARE):
        return []
    # Impact is the R given up relative to the hindsight-best exit —
    # negative, because the mistake cost money that was on the table.
    foregone = [
        (result.r_multiple - result.oracle_r)
        for result in early
        if result.r_multiple is not None and result.oracle_r is not None
    ]
    return [
        LearningFinding(
            run_id=run_id,
            pattern="exits cut winners while the move keeps going",
            evidence_scenario_ids=tuple(result.scenario_id for result in early),
            affected_count=len(early),
            average_r_impact=_mean(foregone),
            suggestion=(
                "exit rules sell into strength; consider a trailing stop or a later "
                "exit condition so winners are given room to finish"
            ),
            confidence=_confidence(len(early)),
            created_at=now,
        )
    ]


def _missed_opportunities(
    run_id: int, records: list[_Record], now: datetime
) -> list[LearningFinding]:
    """Flat passes through moves the reference trade caught for >= 1R."""
    flat_holds = [
        result
        for _, result in records
        if result.verdict in (Verdict.CORRECT_HOLD, Verdict.MISSED_OPPORTUNITY)
        and result.decision == "hold"
    ]
    missed = [
        result
        for result in flat_holds
        if result.verdict == Verdict.MISSED_OPPORTUNITY and result.oracle_r is not None
    ]
    if not _is_pattern(missed, flat_holds, MISSED_OPPORTUNITY_SHARE):
        return []
    # The foregone reference R, negated: staying flat cost this much.
    impact = -_mean([result.oracle_r for result in missed if result.oracle_r is not None])
    return [
        LearningFinding(
            run_id=run_id,
            pattern="the bot stays flat through moves worth taking",
            evidence_scenario_ids=tuple(result.scenario_id for result in missed),
            affected_count=len(missed),
            average_r_impact=impact,
            suggestion=(
                "entry conditions are too strict for these conditions; review the "
                "evidence scenarios and consider loosening one gate at a time"
            ),
            confidence=_confidence(len(missed)),
            created_at=now,
        )
    ]


def _wrong_holds(run_id: int, records: list[_Record], now: datetime) -> list[LearningFinding]:
    """Held positions that the revealed horizon stopped out."""
    wrong = [result for _, result in records if result.verdict == Verdict.WRONG_HOLD]
    if len(wrong) < MIN_EVIDENCE:
        return []
    return [
        LearningFinding(
            run_id=run_id,
            pattern="held positions ride into their stops",
            evidence_scenario_ids=tuple(result.scenario_id for result in wrong),
            affected_count=len(wrong),
            average_r_impact=WRONG_HOLD_R,
            suggestion=(
                "exit signals fire too late on deteriorating positions; review the "
                "evidence scenarios for a common warning sign worth exiting on "
                "(sweepable knobs: breakeven_at_r and trail_atr_multiple — the "
                "automated improver tests them when this finding appears)"
            ),
            confidence=_confidence(len(wrong)),
            created_at=now,
        )
    ]


def _is_pattern(
    evidence: list[ScenarioResult], population: list[ScenarioResult], share: Decimal
) -> bool:
    """Require both enough cases and a meaningful share of the population."""
    if len(evidence) < MIN_EVIDENCE or not population:
        return False
    return Decimal(len(evidence)) / Decimal(len(population)) >= share


def _confidence(evidence_count: int) -> str:
    """Sample-size confidence label for the human reviewing the finding."""
    if evidence_count >= HIGH_CONFIDENCE_EVIDENCE:
        return "high"
    if evidence_count >= MEDIUM_CONFIDENCE_EVIDENCE:
        return "medium"
    return "low"


def _mean(values: list[Decimal]) -> Decimal:
    return (sum(values, Decimal(0)) / Decimal(len(values))).quantize(
        ACCOUNTING_RESOLUTION, rounding=ROUND_HALF_EVEN
    )
