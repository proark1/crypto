"""Cost sensitivity: whether an edge survives worse fees and slippage.

A backtest edge that only exists at today's fees is not an edge — it is a
loan from the fee schedule. ARCHITECTURE.md §10 makes "beats buy-and-hold net
of *pessimistic* fees and slippage" a promotion gate, and the §13.7 routing
decision asks the same of a research family. This module is the mechanism: it
re-grades a candidate at multiplied trading costs and reports whether its
expectancy stays positive when the costs get worse.

It is deliberately a thin, pure summary over R-multiples already graded by
the one blind pipeline (``ScenarioEvaluator``) — the caller does the
re-grading at each scaled cost (it owns the candle series and specs); this
module only scales a fill config and turns the resulting R streams into a
plain-words verdict. Reusing ``reports`` keeps every expectancy and money
figure defined in exactly one place.
"""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal
from typing import Any

from tradebot.evaluation.reports import money_result, r_metrics
from tradebot.execution import FillSimulatorConfig

DEFAULT_COST_MULTIPLIERS: tuple[float, ...] = (1.5, 2.0)
"""The stressed cost levels a human-initiated sweep checks by default: 1.5x
and 2x the configured fees and slippage. The auto-improver omits these (it
sweeps often, and tripling the validation grading on every cycle is not worth
it); the robustness read belongs at the deliberate, human promotion moment."""


def scale_fill_costs(fills: FillSimulatorConfig, multiplier: float) -> FillSimulatorConfig:
    """Return a copy of ``fills`` with every fee and slippage scaled by ``multiplier``.

    Fees and slippage are ``Decimal`` bps; the multiplier is converted through
    ``str`` so a float like 1.5 never contaminates the money math with binary
    rounding (CLAUDE.md invariant 1).
    """
    factor = Decimal(str(multiplier))
    return fills.model_copy(
        update={
            "maker_fee_bps": fills.maker_fee_bps * factor,
            "taker_fee_bps": fills.taker_fee_bps * factor,
            "market_slippage_bps": fills.market_slippage_bps * factor,
        }
    )


def summarize_cost_sensitivity(
    graded: Sequence[tuple[float, Sequence[Decimal]]],
) -> dict[str, Any]:
    """Turn per-multiplier R streams into a plain, JSON-able sensitivity block.

    ``graded`` is ``[(multiplier, r_values), ...]`` with the unscaled 1.0
    baseline first and the stressed levels after, each carrying the R the
    candidate earned at that cost. The block reports, per level, expectancy
    and the illustrative return on the §12.3 stake, and a
    ``survives_worse_costs`` flag: the candidate's expectancy is still
    positive at the *worst* cost level tested. The flag is a read, not a gate
    — thin samples make it noisy, and it never changes a sweep verdict.
    """
    points = [_point(multiplier, r_values) for multiplier, r_values in graded]
    worst = points[-1] if points else None
    survives = (
        worst is not None
        and worst["expectancy_r"] is not None
        and Decimal(worst["expectancy_r"]) > 0
    )
    return {"points": points, "survives_worse_costs": survives}


def _point(multiplier: float, r_values: Sequence[Decimal]) -> dict[str, Any]:
    """One cost level's quality: expectancy and return on the fixed stake."""
    values = list(r_values)
    quality = r_metrics(values)
    return {
        "multiplier": f"{multiplier:g}",
        "trade_count": len(values),
        "expectancy_r": quality.get("expectancy_r"),
        "return_fraction": money_result(values)["return_fraction"],
    }
