"""Baseline controls: no-skill reference strategies for the tournament.

A strategy only earns its complexity if it beats doing something trivial.
The **random-entry control** is that trivial thing: it buys and sells on a
seeded coin flip, with the *same* ATR stop, fees, and slippage as every real
family, so its graded expectancy is the tournament's **noise floor**. A
family whose edge does not clear the random control's is indistinguishable
from luck — exactly the "is this signal or noise?" check the research system
(ARCHITECTURE.md §12, §13.8) exists to make, and the one benchmark a backtest
yardstick (buy-and-hold) cannot give, because random trading pays the same
fees and stops the family does.

The randomness is **seeded**, so a graded run reproduces bit-for-bit like
every other evaluation (§12.1): the same series, config, and seed produce the
same decisions. The RNG only ever decides *whether* to act (a boolean); the
stop price is ATR-derived ``Decimal`` at the signal boundary, same convention
as the families, so the risk manager sizes the control identically (CLAUDE.md
invariant 1 — floats never feed an order size).

These live apart from the ``STRATEGY_FAMILIES`` registry on purpose: a
control has no edge to tune, so it must never enter the sweep grids, the
competition lineup, the custom-bot builder, or the §12.7 auto-improvement
rotation. It is a yardstick, not a competitor to promote.
"""

from __future__ import annotations

import random
from collections.abc import Callable, Mapping
from datetime import datetime
from decimal import ROUND_HALF_EVEN, Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from tradebot.core.models import ACCOUNTING_RESOLUTION, Candle, Side, Signal
from tradebot.indicators import Atr
from tradebot.portfolio import Position
from tradebot.strategies.base import Strategy


class RandomEntryConfig(BaseModel):
    """Coin-flip probabilities and the shared ATR stop convention."""

    model_config = ConfigDict(frozen=True)

    entry_probability: float = Field(default=0.05, gt=0.0, le=1.0)
    """Per-candle chance of proposing a buy while flat. Kept low so the
    control mostly sits flat (and so a buy on the decision candle grades as a
    fresh entry, not a hold); paired with ``exit_probability`` it trades often
    enough to give the noise floor a usable sample without swamping it."""

    exit_probability: float = Field(default=0.25, gt=0.0, le=1.0)
    """Per-candle chance of proposing an exit while holding, so the control
    churns back to flat instead of riding one entry to the horizon (which
    would grade as a hold, contributing no R). Higher than the entry chance so
    holds are short and the control spends most of its time flat."""

    atr_period: int = 14
    atr_stop_multiple: float = 2.0
    """The families' own stop convention (2 x ATR), so the only thing the
    comparison measures is the (absent) entry edge, not a different risk
    geometry."""

    seed: int = 7
    """Seeds the decision RNG; part of the config snapshot, so a graded run
    reproduces bit-for-bit (§12.1)."""


class RandomEntryStrategy:
    """A no-skill control: buys and sells on a seeded coin flip.

    Indicator math runs in floats (permitted: it never feeds an order size);
    the stop price is converted to ``Decimal`` at the signal boundary because
    the risk manager derives the position size from it.
    """

    def __init__(self, config: RandomEntryConfig) -> None:
        """Reset the ATR and seed the decision RNG from the config."""
        self._config = config
        self._atr = Atr(config.atr_period)
        self._rng = random.Random(config.seed)
        self._last_open_time: datetime | None = None

    @property
    def name(self) -> str:
        """Stable identifier for signal lineage."""
        return "random_entry"

    def on_candle(self, candle: Candle, position: Position | None) -> Signal | None:
        """Flip a coin: maybe buy when flat, maybe sell when holding.

        Candles must arrive in strictly increasing time order, same contract
        as every stateful strategy: disorder raises rather than silently
        poisoning the indicator or the RNG stream.
        """
        if self._last_open_time is not None and candle.open_time <= self._last_open_time:
            raise ValueError(
                f"out-of-order or duplicate candle: {candle.open_time.isoformat()} after "
                f"{self._last_open_time.isoformat()}"
            )
        self._last_open_time = candle.open_time

        close = float(candle.close_quote)
        atr = self._atr.update(float(candle.high_quote), float(candle.low_quote), close)
        # Draw both coins every candle regardless of state or warm-up, so the
        # RNG stream — and thus the decision sequence — is a pure function of
        # how many candles have passed, identical for a given seed no matter
        # the price path. Reproducibility (§12.1) depends on this.
        entry_roll = self._rng.random()
        exit_roll = self._rng.random()
        if atr is None:
            return None  # ATR still warming; no defined stop distance yet

        signal_id = f"{self.name}:{candle.symbol}:{candle.close_time.isoformat()}"
        if position is None:
            if entry_roll >= self._config.entry_probability:
                return None
            stop = Decimal(str(close - self._config.atr_stop_multiple * atr)).quantize(
                ACCOUNTING_RESOLUTION, rounding=ROUND_HALF_EVEN
            )
            if stop <= 0:
                return None  # degenerate volatility; no defined invalidation point
            return Signal(
                signal_id=signal_id,
                strategy_name=self.name,
                symbol=candle.symbol,
                side=Side.BUY,
                confidence=1.0,
                stop_price_quote=stop,
                reasons=(
                    f"random control: entry coin-flip under {self._config.entry_probability:g}",
                    f"stop at {self._config.atr_stop_multiple} x ATR below close",
                ),
                created_at=candle.close_time,
            )
        if exit_roll < self._config.exit_probability:
            exit_reason = f"random control: exit coin-flip under {self._config.exit_probability:g}"
            return Signal(
                signal_id=signal_id,
                strategy_name=self.name,
                symbol=candle.symbol,
                side=Side.SELL,
                confidence=1.0,
                # Informational for exits: the position is being closed, not stopped.
                stop_price_quote=candle.close_quote,
                reasons=(exit_reason,),
                created_at=candle.close_time,
            )
        return None


CONTROL_STRATEGIES: Mapping[str, tuple[type[BaseModel], Callable[..., Strategy]]] = {
    "random_entry": (RandomEntryConfig, RandomEntryStrategy),
}
"""Reference controls: id -> (config model, constructor). Deliberately
separate from ``STRATEGY_FAMILIES`` (``evaluation/sweep.py``): controls are
yardsticks, never swept, lineup'd, custom-built, or auto-promoted."""


def validate_control_params(control_id: str, params: Mapping[str, Any]) -> None:
    """Raise ``ValueError`` for an unknown control or parameter, loudly.

    Pydantic ignores unknown keys by default; a typo'd parameter would
    silently grade the control's defaults instead. Mirrors
    ``evaluation.sweep.validate_family_params`` for the control registry.
    """
    if control_id not in CONTROL_STRATEGIES:
        raise ValueError(f"unknown control {control_id!r}; known: {sorted(CONTROL_STRATEGIES)}")
    config_model, _ = CONTROL_STRATEGIES[control_id]
    unknown = set(params) - set(config_model.model_fields)
    if unknown:
        raise ValueError(f"unknown {control_id} parameters: {sorted(unknown)}")


def build_control_strategy(control_id: str, params: Mapping[str, Any]) -> Strategy:
    """Build one fresh control instance for ``control_id`` with ``params``."""
    validate_control_params(control_id, params)
    config_model, constructor = CONTROL_STRATEGIES[control_id]
    return constructor(config_model(**params))
