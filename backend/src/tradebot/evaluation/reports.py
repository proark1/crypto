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

import math
import statistics
from decimal import ROUND_HALF_EVEN, Decimal
from typing import Any

from tradebot.core.models import ACCOUNTING_RESOLUTION
from tradebot.evaluation.classifier import archetype
from tradebot.evaluation.models import Scenario, ScenarioClass, ScenarioResult, Verdict
from tradebot.evaluation.statistics import overfit_diagnostics

_DISPLAY_RESOLUTION = Decimal("0.0001")
_MONEY_RESOLUTION = Decimal("0.01")

TAIL_LOSS_FRACTION = Decimal("0.1")
"""The worst slice of trades the tail-loss metric averages — the expected
shortfall in R over the worst decile (always at least one trade).

Like the downside deviation, this is a **distributional** risk measure: it
reads off the multiset of per-trade R, so it is well-defined on the
independently sampled scenarios this report grades. Path metrics such as max
drawdown or time-under-water are deliberately *not* added here — they need a
trade ordering, and the money result (§12.3) is explicitly order-independent.
Those live on the equity-curve reports (``backtest/report.py``,
``backtest/account_report.py``), where an ordering genuinely exists."""

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
    by_archetype = _breakdown(records, lambda s: archetype(s.conditions).value)
    summary: dict[str, Any] = {
        "scenario_count": len(records),
        "verdicts": _verdict_counts(records),
        **r_metrics(r_values),
        **money_result(r_values),
        "overfit_diagnostics": overfit_diagnostics(r_values),
        "hold_metrics": _hold_metrics(records),
        "by_trend": _breakdown(records, lambda s: s.conditions.trend.value),
        "by_volatility": _breakdown(records, lambda s: s.conditions.volatility.value),
        "by_archetype": by_archetype,
        "regime_diagnostics": _regime_diagnostics(by_archetype),
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
    expectancy = _mean(r_values)
    downside = _downside_deviation(r_values)
    metrics: dict[str, Any] = {
        "trade_count": len(r_values),
        "expectancy_r": _display(expectancy),
        "median_r": _display(statistics.median(r_values)),
        "win_rate": _display(Decimal(len(wins)) / Decimal(len(r_values))),
        "average_win_r": _display(_mean(wins)) if wins else None,
        "average_loss_r": _display(_mean(losses)) if losses else None,
        "profit_factor": (
            _display((gross_win / gross_loss).quantize(ACCOUNTING_RESOLUTION))
            if gross_loss > 0
            else None
        ),
        # Risk-adjusted and tail metrics, distributional (order-free): a bot
        # earning the same expectancy with a shallower downside / tail is the
        # better bet, and a +2R-per-win bot is not "good" if its worst trades
        # are ruinous. None where undefined (no losing trades ⇒ no downside).
        "downside_deviation_r": _display(downside),
        "sortino_r": _display(expectancy / downside) if downside > 0 else None,
        "tail_loss_r": _display(_tail_loss(r_values)),
        "worst_r": _display(min(r_values)),
    }
    return metrics


def _downside_deviation(r_values: list[Decimal]) -> Decimal:
    """Root-mean-square of the losing R's (downside semi-deviation, target 0).

    The denominator of the per-trade Sortino: it penalizes losing trades
    only, dividing the sum of squared losses by the *total* trade count (the
    conventional target semivariance), so upside volatility never counts as
    risk. A symmetric function of the R multiset — no ordering needed.
    """
    squared_losses = [r * r for r in r_values if r < 0]
    if not squared_losses:
        return Decimal(0)
    mean_square = sum(squared_losses, Decimal(0)) / Decimal(len(r_values))
    return mean_square.sqrt().quantize(ACCOUNTING_RESOLUTION, rounding=ROUND_HALF_EVEN)


def _tail_loss(r_values: list[Decimal]) -> Decimal:
    """Mean R of the worst ``TAIL_LOSS_FRACTION`` of trades (expected shortfall).

    At least one trade, so even a thin sample reports its single worst R. Like
    the downside deviation, the worst-k R's are a function of the multiset,
    not the order — so this is honest on independently sampled scenarios.
    """
    count = max(1, math.ceil(TAIL_LOSS_FRACTION * len(r_values)))
    worst = sorted(r_values)[:count]
    return (sum(worst, Decimal(0)) / Decimal(len(worst))).quantize(
        ACCOUNTING_RESOLUTION, rounding=ROUND_HALF_EVEN
    )


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


def _regime_diagnostics(by_archetype: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Compact read of where the bot has evidence to trade or stand down."""
    traded = [
        (label, block)
        for label, block in by_archetype.items()
        if block.get("trade_count", 0) > 0 and block.get("expectancy_r") is not None
    ]
    if not traded:
        return {"best": None, "weakest": None, "sit_out_candidates": []}
    ranked = sorted(traded, key=lambda item: Decimal(str(item[1]["expectancy_r"])))
    sit_out = [
        {
            "archetype": label,
            "expectancy_r": block["expectancy_r"],
            "trade_count": block["trade_count"],
        }
        for label, block in ranked
        if Decimal(str(block["expectancy_r"])) <= 0
    ]
    weakest_label, weakest = ranked[0]
    best_label, best = ranked[-1]
    return {
        "best": {
            "archetype": best_label,
            "expectancy_r": best["expectancy_r"],
            "trade_count": best["trade_count"],
        },
        "weakest": {
            "archetype": weakest_label,
            "expectancy_r": weakest["expectancy_r"],
            "trade_count": weakest["trade_count"],
        },
        "sit_out_candidates": sit_out,
    }


def _mean(values: list[Decimal]) -> Decimal:
    return (sum(values, Decimal(0)) / Decimal(len(values))).quantize(
        ACCOUNTING_RESOLUTION, rounding=ROUND_HALF_EVEN
    )
