"""The bake-off contestants: each strategy family at two energies.

A "bake-off" pits a fixed roster of bots against each other across a grid
of timeframes and history windows (see ``bakeoff.py``) and ranks them by
the money they made. This module defines that roster.

Each solo *price* family appears at two *energies* — ``calm`` and ``bold`` —
that trade the same idea at different tempers: calm waits for slower,
higher-conviction signals and gives the trade more room (a wider ATR stop);
bold fires on faster signals and keeps a tighter stop. Every price family in
``STRATEGY_FAMILIES`` is here (the non-price ``funding`` family is excluded:
without a funding series it would trade inert, so it competes in the live
lineup instead), plus the live production router as a baseline — so the
leaderboard answers "did any energy of any family beat the bot we actually
run?" That is the tournament where everything is tested against everything.

The presets are deliberately code-defined and frozen: a bake-off is only
comparable to a past bake-off if the contestants are the same, so the
roster is an architecture decision, not configuration. Indicator windows
are kept moderate (nothing slower than an 80-period EMA) so a preset still
warms up inside the shorter history windows the grid sweeps.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from tradebot.competition.lineup import PRODUCTION_BOT_ID
from tradebot.evaluation.sweep import (
    STRATEGY_FAMILIES,
    validate_family_params,
    validate_recipe_params,
)
from tradebot.strategies.controls import validate_control_params


@dataclass(frozen=True)
class BakeOffContestant:
    """One bake-off entry: a stable id, a label, and what it trades.

    Exactly one of ``family`` / ``control`` / ``recipe`` is set, or none:
    - ``family`` names a single strategy family with the parameter overrides
      — its "energy" — applied over that family's defaults (the energy presets);
    - ``control`` names a reference control (``strategies/controls.py``), a
      no-skill yardstick like the random-entry noise floor;
    - ``recipe`` is a multi-family ensemble (``{entry_mode, families}``, the
      same shape a custom bot trades) graded as the composite it forms —
      research-only contestants, never routed into production (§13.7);
    - all ``None`` marks the production baseline, whose strategy the worker
      builds (the regime router needs the live regime wiring).
    """

    bot_id: str
    label: str
    family: str | None
    params: Mapping[str, Any] = field(default_factory=dict)
    control: str | None = None
    recipe: Mapping[str, Any] | None = None


# The production router as a reference line: not an energy preset, but the
# yardstick every preset is measured against — the shape the bot runs today.
PRODUCTION_BASELINE = BakeOffContestant(
    bot_id=PRODUCTION_BOT_ID,
    label="Production (baseline)",
    family=None,
)

# Every price family x {calm, bold}. Calm: slower entries, wider 3x ATR stop.
# Bold: faster entries, tighter 1.5x ATR stop. Each preset's bot_id is the
# stable key its bake-off results are recorded under — never rename one.
ENERGY_PRESETS: tuple[BakeOffContestant, ...] = (
    BakeOffContestant(
        bot_id="trend_calm",
        label="Trend (calm)",
        family="trend_following",
        params={"fast_ema_period": 30, "slow_ema_period": 80, "atr_stop_multiple": 3.0},
    ),
    BakeOffContestant(
        bot_id="trend_bold",
        label="Trend (bold)",
        family="trend_following",
        params={"fast_ema_period": 8, "slow_ema_period": 21, "atr_stop_multiple": 1.5},
    ),
    BakeOffContestant(
        bot_id="reversion_calm",
        label="Mean reversion (calm)",
        family="mean_reversion",
        params={
            "oversold_threshold": 25.0,
            "exit_rsi": 55.0,
            "trend_filter_ema_period": 80,
            "atr_stop_multiple": 3.0,
        },
    ),
    BakeOffContestant(
        bot_id="reversion_bold",
        label="Mean reversion (bold)",
        family="mean_reversion",
        params={
            "oversold_threshold": 35.0,
            "exit_rsi": 60.0,
            "trend_filter_ema_period": 0,
            "atr_stop_multiple": 1.5,
        },
    ),
    BakeOffContestant(
        bot_id="breakout_calm",
        label="Breakout (calm)",
        family="breakout",
        params={
            "channel_period": 40,
            "exit_channel_period": 20,
            "atr_stop_multiple": 3.0,
            "min_volume_ratio": 1.2,
        },
    ),
    BakeOffContestant(
        bot_id="breakout_bold",
        label="Breakout (bold)",
        family="breakout",
        params={"channel_period": 10, "exit_channel_period": 5, "atr_stop_multiple": 1.5},
    ),
    BakeOffContestant(
        bot_id="momentum_calm",
        label="Momentum (calm)",
        family="momentum",
        params={
            "fast_ema_period": 19,
            "slow_ema_period": 39,
            "signal_ema_period": 12,
            "require_positive_macd": True,
            "atr_stop_multiple": 3.0,
        },
    ),
    BakeOffContestant(
        bot_id="momentum_bold",
        label="Momentum (bold)",
        family="momentum",
        params={
            "fast_ema_period": 8,
            "slow_ema_period": 17,
            "signal_ema_period": 6,
            "require_positive_macd": False,
            "atr_stop_multiple": 1.5,
        },
    ),
    BakeOffContestant(
        bot_id="squeeze_calm",
        label="Squeeze (calm)",
        family="squeeze",
        params={"keltner_atr_multiple": 1.0, "atr_stop_multiple": 3.0, "min_volume_ratio": 1.2},
    ),
    BakeOffContestant(
        bot_id="squeeze_bold",
        label="Squeeze (bold)",
        family="squeeze",
        params={"keltner_atr_multiple": 2.0, "atr_stop_multiple": 1.5},
    ),
    BakeOffContestant(
        bot_id="supertrend_calm",
        label="Supertrend (calm)",
        family="supertrend",
        params={"atr_period": 14, "atr_multiple": 4.0, "atr_stop_multiple": 3.0},
    ),
    BakeOffContestant(
        bot_id="supertrend_bold",
        label="Supertrend (bold)",
        family="supertrend",
        params={"atr_period": 7, "atr_multiple": 2.0, "atr_stop_multiple": 1.5},
    ),
    BakeOffContestant(
        bot_id="bollinger_calm",
        label="Bollinger reversion (calm)",
        family="bollinger_reversion",
        params={"bollinger_period": 30, "num_stddev": 2.5, "atr_stop_multiple": 3.0},
    ),
    BakeOffContestant(
        bot_id="bollinger_bold",
        label="Bollinger reversion (bold)",
        family="bollinger_reversion",
        params={"bollinger_period": 14, "num_stddev": 2.0, "atr_stop_multiple": 1.5},
    ),
    BakeOffContestant(
        bot_id="adx_calm",
        label="ADX trend (calm)",
        family="adx_trend",
        params={"adx_period": 20, "adx_threshold": 30.0, "atr_stop_multiple": 3.0},
    ),
    BakeOffContestant(
        bot_id="adx_bold",
        label="ADX trend (bold)",
        family="adx_trend",
        params={"adx_period": 10, "adx_threshold": 20.0, "atr_stop_multiple": 1.5},
    ),
    BakeOffContestant(
        bot_id="keltner_calm",
        label="Keltner breakout (calm)",
        family="keltner",
        params={
            "ema_period": 30,
            "atr_period": 15,
            "channel_atr_multiple": 2.5,
            "atr_stop_multiple": 3.0,
        },
    ),
    BakeOffContestant(
        bot_id="keltner_bold",
        label="Keltner breakout (bold)",
        family="keltner",
        params={
            "ema_period": 10,
            "atr_period": 7,
            "channel_atr_multiple": 1.5,
            "atr_stop_multiple": 1.5,
        },
    ),
    BakeOffContestant(
        bot_id="vol_breakout_calm",
        label="Volatility breakout (calm)",
        family="vol_breakout",
        params={
            "channel_period": 40,
            "expansion_ratio": 1.5,
            "exit_ema_period": 30,
            "atr_stop_multiple": 3.0,
        },
    ),
    BakeOffContestant(
        bot_id="vol_breakout_bold",
        label="Volatility breakout (bold)",
        family="vol_breakout",
        params={
            "channel_period": 10,
            "expansion_ratio": 1.1,
            "exit_ema_period": 10,
            "atr_stop_multiple": 1.5,
        },
    ),
    BakeOffContestant(
        bot_id="tsmom_calm",
        label="Time-series momentum (calm)",
        family="tsmom",
        params={"lookback": 40, "atr_stop_multiple": 3.0},
    ),
    BakeOffContestant(
        bot_id="tsmom_bold",
        label="Time-series momentum (bold)",
        family="tsmom",
        params={"lookback": 10, "atr_stop_multiple": 1.5},
    ),
    BakeOffContestant(
        bot_id="rsi_trend_calm",
        label="RSI trend (calm)",
        family="rsi_trend",
        params={
            "rsi_period": 21,
            "entry_level": 55.0,
            "exit_level": 45.0,
            "atr_stop_multiple": 3.0,
        },
    ),
    BakeOffContestant(
        bot_id="rsi_trend_bold",
        label="RSI trend (bold)",
        family="rsi_trend",
        params={"rsi_period": 9, "entry_level": 50.0, "exit_level": 45.0, "atr_stop_multiple": 1.5},
    ),
)

# Ensemble contestants: combine several families on one symbol, the audit's
# "the best bot may be a combination, not a soloist" thesis put on the
# leaderboard. Built from the existing composite the custom-bot builder uses
# (any = first family to fire wins, a wider net; all = every family must agree
# on the same candle, a confluence filter). Research-only like every preset:
# winning the bake-off never routes a recipe into production — that stays the
# §13.7 human decision. Member windows are kept moderate so the composite
# warms up inside the shorter grid windows.
ENSEMBLE_CONTESTANTS: tuple[BakeOffContestant, ...] = (
    BakeOffContestant(
        bot_id="ensemble_confluence",
        label="Ensemble (confluence)",
        family=None,
        recipe={
            "entry_mode": "all",
            "families": {
                "breakout": {"channel_period": 20},
                "momentum": {},
            },
        },
    ),
    BakeOffContestant(
        bot_id="ensemble_breadth",
        label="Ensemble (breadth)",
        family=None,
        recipe={
            "entry_mode": "any",
            "families": {
                "trend_following": {"fast_ema_period": 12, "slow_ema_period": 26},
                "breakout": {"channel_period": 20},
                "squeeze": {},
            },
        },
    ),
)

# Reference controls: no-skill yardsticks, not energy presets. The
# random-entry control is the tournament's noise floor — a family that
# cannot out-earn random coin-flip trading (paying the same fees, stops, and
# slippage) has no demonstrable edge (ARCHITECTURE.md §13.8). Controls are
# built from the separate control registry, never swept or promoted.
CONTROL_CONTESTANTS: tuple[BakeOffContestant, ...] = (
    BakeOffContestant(
        bot_id="buy_hold",
        label="Buy and hold (baseline)",
        family=None,
        control="buy_hold",
    ),
    BakeOffContestant(
        bot_id="dca",
        label="DCA (baseline)",
        family=None,
        control="dca",
    ),
    BakeOffContestant(
        bot_id="grid",
        label="Spot grid (baseline)",
        family=None,
        control="grid",
    ),
    BakeOffContestant(
        bot_id="random_entry",
        label="Random entry (control)",
        family=None,
        control="random_entry",
    ),
)

# The full roster the bake-off grades each cell: the baseline first (it
# leads the comparison group, the sweep contract's baseline slot), then the
# energy presets (every price family at two energies), the ensembles, and the
# reference controls.
BAKE_OFF_CONTESTANTS: tuple[BakeOffContestant, ...] = (
    PRODUCTION_BASELINE,
    *ENERGY_PRESETS,
    *ENSEMBLE_CONTESTANTS,
    *CONTROL_CONTESTANTS,
)

_BY_ID: Mapping[str, BakeOffContestant] = {c.bot_id: c for c in BAKE_OFF_CONTESTANTS}


def contestant_for(bot_id: str) -> BakeOffContestant | None:
    """Return the bake-off contestant for ``bot_id``, or ``None`` if not one.

    ``None`` lets the worker's evaluator factory fall through to the lineup
    and custom-bot paths: a bake-off preset id is just one more kind of bot
    a run can grade.
    """
    return _BY_ID.get(bot_id)


def _validate_contestant(contestant: BakeOffContestant) -> None:
    """Validate one contestant's wiring; raise on a bad entry.

    ``family`` / ``control`` / ``recipe`` are mutually exclusive — the
    resolver checks them in order, so a contestant that set two would silently
    trade one and ignore the rest. Catch that here rather than ship a roster
    with a quietly dead field. Neither set marks the production baseline.
    """
    kinds = [
        name
        for name, value in (
            ("family", contestant.family),
            ("control", contestant.control),
            ("recipe", contestant.recipe),
        )
        if value is not None
    ]
    if len(kinds) > 1:
        raise ValueError(
            f"bake-off contestant {contestant.bot_id!r} sets more than one of "
            f"{kinds}; a contestant is one of family / control / recipe (or the baseline)"
        )
    if contestant.control is not None:
        validate_control_params(contestant.control, contestant.params)
        return
    if contestant.recipe is not None:
        validate_recipe_params(contestant.recipe)
        return
    if contestant.family is None:
        return
    if contestant.family not in STRATEGY_FAMILIES:
        raise ValueError(
            f"bake-off preset {contestant.bot_id!r} names unknown family {contestant.family!r}"
        )
    validate_family_params(contestant.family, contestant.params)


def validate_presets() -> None:
    """Raise ``ValueError`` if any contestant names an unknown family/control or param.

    Called once at import time below so a typo in a contestant fails the
    build, not the first bake-off hours later. The baseline (neither family
    nor control) is skipped.
    """
    for contestant in BAKE_OFF_CONTESTANTS:
        _validate_contestant(contestant)


validate_presets()
