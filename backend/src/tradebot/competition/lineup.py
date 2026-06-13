"""The competition lineup and its strategy builders.

The lineup is fixed in code, not configuration: which strategies compete
is an architecture decision (like production routing), and a stable
roster is what makes the leaderboard's history meaningful. Every entry
declares the identity its journals are namespaced under (``bot_id``) and
the ``risk_state`` row its brakes persist to — both must stay stable
across deploys, or a restart would orphan an account's history.

The production bot competes as itself: the regime-routed family router,
trading under the default ``production`` journal scope it has always
written to. The four challengers each trade one family solo, with the
family's *active* (possibly auto-promoted) parameters, so the comparison
is always against what the families actually trade today.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from tradebot.core.models import Candle, Signal
from tradebot.evaluation.strategy import build_traded_strategy
from tradebot.evaluation.sweep import STRATEGY_FAMILIES
from tradebot.portfolio import Position
from tradebot.strategies import Strategy

PRODUCTION_BOT_ID = "production"
"""The incumbent's journal scope — also the server default for every row
written before the competition existed."""


@dataclass(frozen=True)
class CompetitorSpec:
    """One competition entry: identity, label, and what it trades."""

    bot_id: str
    """Stable journal namespace (fills, orders, decisions). Never rename:
    the account's persisted history is keyed by it."""

    label: str
    """Human-readable name for leaderboards and reports."""

    family: str | None
    """The single strategy family this bot trades, or ``None`` for the
    production shape (the regime-routed router)."""

    risk_state_row_id: int
    """Fixed ``risk_state`` row this bot's brakes persist under (the
    production bot owns row 1, its historical row)."""

    description: str
    """One plain-words sentence for the UI: what the strategy does."""


LINEUP: tuple[CompetitorSpec, ...] = (
    CompetitorSpec(
        bot_id=PRODUCTION_BOT_ID,
        label="Regime router",
        family=None,
        risk_state_row_id=1,
        description=(
            "The production shape: trend following in trending markets, "
            "mean reversion in ranging ones, routed by the BTC regime."
        ),
    ),
    CompetitorSpec(
        bot_id="trend_following",
        label="Trend following",
        family="trend_following",
        risk_state_row_id=2,
        description="EMA crossover entries with ATR stops, always on.",
    ),
    CompetitorSpec(
        bot_id="mean_reversion",
        label="Mean reversion",
        family="mean_reversion",
        risk_state_row_id=3,
        description="Buys RSI oversold recoveries, exits at the RSI midline.",
    ),
    CompetitorSpec(
        bot_id="breakout",
        label="Breakout",
        family="breakout",
        risk_state_row_id=4,
        description="Buys Donchian-channel breakouts with turtle-style exits.",
    ),
    CompetitorSpec(
        bot_id="momentum",
        label="Momentum",
        family="momentum",
        risk_state_row_id=5,
        description="Buys bullish MACD crossovers, exits when momentum turns.",
    ),
    CompetitorSpec(
        bot_id="squeeze",
        label="Squeeze breakout",
        family="squeeze",
        risk_state_row_id=6,
        description="Buys upward breaks out of a volatility squeeze, exits at the basis.",
    ),
)
"""Six bots, six strategies. Order is leaderboard display order before
ranking; ``risk_state_row_id`` values are append-only — a removed entry's
row id is never reused."""


def spec_for(bot_id: str) -> CompetitorSpec:
    """Return the lineup entry for ``bot_id``; raises ``ValueError`` if unknown."""
    for spec in LINEUP:
        if spec.bot_id == bot_id:
            return spec
    raise ValueError(
        f"unknown competitor {bot_id!r}; known: {sorted(spec.bot_id for spec in LINEUP)}"
    )


class ScopedSignalStrategy:
    """Wraps a challenger's strategy to namespace its signal ids per bot.

    Order ids derive from signal ids (``ord-<signal_id>``), and signal ids
    derive from the strategy family — so without this prefix, a challenger
    and the production router trading the same family on the same candle
    would mint the *same* order id and collide in the shared order journal.
    The strategy name (signal lineage) is deliberately untouched: gates
    and reports still see the real family.
    """

    def __init__(self, inner: Strategy, bot_id: str) -> None:
        """Scope ``inner``'s signal ids under ``bot_id``."""
        self._inner = inner
        self._bot_id = bot_id

    @property
    def name(self) -> str:
        """The wrapped family's identifier — lineage stays honest."""
        return self._inner.name

    def on_candle(self, candle: Candle, position: Position | None) -> Signal | None:
        """Forward the candle; prefix any emitted signal's id with the bot."""
        signal = self._inner.on_candle(candle, position)
        if signal is None:
            return None
        return signal.model_copy(update={"signal_id": f"{self._bot_id}/{signal.signal_id}"})


def _family_strategy(family: str, params_by_family: Mapping[str, Mapping[str, Any]]) -> Strategy:
    """One fresh instance of ``family`` with its active parameters."""
    config_model, strategy_constructor = STRATEGY_FAMILIES[family]
    return strategy_constructor(config_model(**params_by_family.get(family, {})))


def build_challenger_strategy(
    spec: CompetitorSpec,
    params_by_family: Mapping[str, Mapping[str, Any]],
) -> Strategy:
    """Build one fresh, bot-scoped live strategy for a challenger.

    Challengers only — the production bot's strategy is built by the
    worker (its router needs the live regime detector) and trades
    unscoped, exactly as it always has.
    """
    if spec.family is None:
        raise ValueError(
            f"{spec.bot_id} has no solo family; the worker builds the production strategy"
        )
    return ScopedSignalStrategy(_family_strategy(spec.family, params_by_family), spec.bot_id)


def build_scenario_strategy(
    spec: CompetitorSpec,
    params_by_family: Mapping[str, Mapping[str, Any]],
    regime_routed: bool,
) -> Strategy:
    """Build one fresh scenario strategy for research runs.

    The production entry grades the shape production trades (router when
    the regime gate runs, bare trend otherwise — ``regime_routed`` mirrors
    the worker's wiring); challengers grade their family solo. No id
    scoping: scenarios never touch the shared journals.
    """
    if spec.family is None:
        return build_traded_strategy(regime_routed=regime_routed, params_by_family=params_by_family)
    return _family_strategy(spec.family, params_by_family)
