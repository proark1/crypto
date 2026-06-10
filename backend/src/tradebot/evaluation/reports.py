"""Aggregate run reports (ARCHITECTURE.md section 12.3).

Expectancy and profit factor lead; win rate follows — a 40%-right bot
earning +2R per win beats an 80%-right bot losing slowly. All Decimals are
stringified: the summary lives in a JSONB column and feeds a frontend that
never does float money math.
"""

from __future__ import annotations

import statistics
from decimal import ROUND_HALF_EVEN, Decimal
from typing import Any

from tradebot.core.models import ACCOUNTING_RESOLUTION
from tradebot.evaluation.models import Scenario, ScenarioClass, ScenarioResult, Verdict

_DISPLAY_RESOLUTION = Decimal("0.0001")


def _display(value: Decimal) -> str:
    return str(value.quantize(_DISPLAY_RESOLUTION, rounding=ROUND_HALF_EVEN))


def build_summary(records: list[tuple[Scenario, ScenarioResult]]) -> dict[str, Any]:
    """Build the persisted run report from graded scenarios."""
    summary: dict[str, Any] = {
        "scenario_count": len(records),
        "verdicts": _verdict_counts(records),
        **trade_metrics([result for _, result in records]),
        "hold_metrics": _hold_metrics(records),
        "by_trend": _breakdown(records, lambda s: s.conditions.trend.value),
        "by_volatility": _breakdown(records, lambda s: s.conditions.volatility.value),
        "by_timeframe": _breakdown(records, lambda s: s.timeframe),
        "by_symbol": _breakdown(records, lambda s: s.symbol),
        "by_event": _event_breakdown(records),
    }
    return summary


def _verdict_counts(records: list[tuple[Scenario, ScenarioResult]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for _, result in records:
        counts[result.verdict.value] = counts.get(result.verdict.value, 0) + 1
    return counts


def trade_metrics(results: list[ScenarioResult]) -> dict[str, Any]:
    """Build the trade-quality block from graded results (expectancy first)."""
    return r_metrics([r.r_multiple for r in results if r.r_multiple is not None])


def r_metrics(r_values: list[Decimal]) -> dict[str, Any]:
    """Build the trade-quality block from raw R-multiples (§12.3 format).

    Public because the parameter sweep reports per-candidate quality with
    exactly these numbers — two formats for one concept would drift.
    """
    if not r_values:
        return {"trade_count": 0}
    wins = [r for r in r_values if r > 0]
    losses = [r for r in r_values if r < 0]
    gross_win = sum(wins, Decimal(0))
    gross_loss = -sum(losses, Decimal(0))
    metrics: dict[str, Any] = {
        "trade_count": len(r_values),
        "expectancy_r": _display(_mean(r_values)),
        "median_r": _display(statistics.median(r_values)),
        "win_rate": _display(Decimal(len(wins)) / Decimal(len(r_values))),
        "average_win_r": _display(_mean(wins)) if wins else None,
        "average_loss_r": _display(_mean(losses)) if losses else None,
        "profit_factor": (
            _display((gross_win / gross_loss).quantize(ACCOUNTING_RESOLUTION))
            if gross_loss > 0
            else None
        ),
    }
    return metrics


def _hold_metrics(records: list[tuple[Scenario, ScenarioResult]]) -> dict[str, Any]:
    flat_holds = [
        result
        for _, result in records
        if result.decision == "hold"
        and result.verdict in (Verdict.CORRECT_HOLD, Verdict.MISSED_OPPORTUNITY)
    ]
    holding_holds = [
        scenario
        for scenario, result in records
        if scenario.scenario_class == ScenarioClass.HOLDING and result.decision == "hold"
    ]
    missed = sum(1 for result in flat_holds if result.verdict == Verdict.MISSED_OPPORTUNITY)
    return {
        "flat_hold_count": len(flat_holds),
        "missed_opportunity_rate": (
            _display(Decimal(missed) / Decimal(len(flat_holds))) if flat_holds else None
        ),
        "holding_hold_count": len(holding_holds),
    }


def _breakdown(
    records: list[tuple[Scenario, ScenarioResult]],
    key: Any,
) -> dict[str, dict[str, Any]]:
    groups: dict[str, list[tuple[Scenario, ScenarioResult]]] = {}
    for scenario, result in records:
        groups.setdefault(key(scenario), []).append((scenario, result))
    return {
        label: {
            "scenario_count": len(group),
            **trade_metrics([result for _, result in group]),
        }
        for label, group in sorted(groups.items())
    }


def _event_breakdown(
    records: list[tuple[Scenario, ScenarioResult]],
) -> dict[str, dict[str, Any]]:
    groups: dict[str, list[tuple[Scenario, ScenarioResult]]] = {}
    for scenario, result in records:
        for event in scenario.conditions.events:
            groups.setdefault(event.value, []).append((scenario, result))
    return {
        label: {
            "scenario_count": len(group),
            **trade_metrics([result for _, result in group]),
        }
        for label, group in sorted(groups.items())
    }


def _mean(values: list[Decimal]) -> Decimal:
    return (sum(values, Decimal(0)) / Decimal(len(values))).quantize(
        ACCOUNTING_RESOLUTION, rounding=ROUND_HALF_EVEN
    )
