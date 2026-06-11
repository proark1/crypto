"""Aggregate run reports (ARCHITECTURE.md section 12.3).

Expectancy and profit factor lead; win rate follows — a 40%-right bot
earning +2R per win beats an 80%-right bot losing slowly. The report also
carries an illustrative money result (``money_result``): the R-multiple
stream replayed as a fixed stake so a non-technical reader can rank
strategies by ending money, not only by R. All Decimals are stringified:
the summary lives in a JSONB column and feeds a frontend that never does
float money math.
"""

from __future__ import annotations

import statistics
from decimal import ROUND_HALF_EVEN, Decimal
from typing import Any

from tradebot.core.models import ACCOUNTING_RESOLUTION
from tradebot.evaluation.models import Scenario, ScenarioClass, ScenarioResult, Verdict

_DISPLAY_RESOLUTION = Decimal("0.0001")
_MONEY_RESOLUTION = Decimal("0.01")

EVALUATION_START_BALANCE_QUOTE = Decimal("10000")
"""Illustrative starting equity (quote currency) for the run's money result.

The evaluation grades decisions in R-multiples (ratios, not money); this
fixed stake turns that ratio stream into a "what would 10,000 have become"
figure. It is identical for every strategy in a comparison, so the gap
between two columns' ending balances stays the strategies' own."""

EVALUATION_RISK_PER_TRADE_FRACTION = Decimal("0.01")
"""Fraction of current equity risked per graded trade in the money result.

Matches the live risk manager default (``risk/manager.py``), so one R of
expectancy reads as ~1% of equity. The curve is fixed-fractional and
compounding: ending equity is the product of ``(1 + fraction * R)`` over the
graded trades, which is order-independent — fitting scenarios that are
independently sampled moments rather than one sequential path."""


def _display(value: Decimal) -> str:
    return str(value.quantize(_DISPLAY_RESOLUTION, rounding=ROUND_HALF_EVEN))


def _money(value: Decimal) -> str:
    return str(value.quantize(_MONEY_RESOLUTION, rounding=ROUND_HALF_EVEN))


def build_summary(records: list[tuple[Scenario, ScenarioResult]]) -> dict[str, Any]:
    """Build the persisted run report from graded scenarios."""
    r_values = [result.r_multiple for _, result in records if result.r_multiple is not None]
    summary: dict[str, Any] = {
        "scenario_count": len(records),
        "verdicts": _verdict_counts(records),
        **r_metrics(r_values),
        **money_result(r_values),
        "hold_metrics": _hold_metrics(records),
        "by_trend": _breakdown(records, lambda s: s.conditions.trend.value),
        "by_volatility": _breakdown(records, lambda s: s.conditions.volatility.value),
        "by_timeframe": _breakdown(records, lambda s: s.timeframe),
        "by_symbol": _breakdown(records, lambda s: s.symbol),
        "by_event": _event_breakdown(records),
    }
    return summary


def money_result(r_values: list[Decimal]) -> dict[str, Any]:
    """Replay the R-multiple stream as an illustrative money result.

    Starts from ``EVALUATION_START_BALANCE_QUOTE`` and risks a fixed fraction
    of current equity per trade, compounding (§12.3). Returns the starting and
    ending equity, net PnL (all quote currency, money-stringified), and the
    return as a dimensionless fraction — so the report can be read in money,
    not only in R. With no graded trades the stake is returned unchanged.
    """
    start = EVALUATION_START_BALANCE_QUOTE
    equity = start
    for r in r_values:
        equity *= Decimal(1) + EVALUATION_RISK_PER_TRADE_FRACTION * r
    net = equity - start
    return {
        "starting_balance_quote": _money(start),
        "final_balance_quote": _money(equity),
        "net_pnl_quote": _money(net),
        "return_fraction": _display(net / start),
    }


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
