"""Baseline controls: reference strategies for the tournament.

A strategy only earns its complexity if it beats doing something trivial.
These controls are deliberately simple: random-entry is the no-skill noise
floor, buy-and-hold is the passive spot benchmark, DCA is a time-based
accumulator, and grid is the obvious sideways-market alternative. A family
whose edge does not clear these has not proved it deserves complexity.

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


class BuyHoldConfig(BaseModel):
    """Passive spot benchmark: enter once, then let the horizon mark it."""

    model_config = ConfigDict(frozen=True)

    atr_period: int = Field(default=14, ge=2)
    atr_stop_multiple: float = Field(default=2.0, gt=0.0)


class BuyHoldStrategy:
    """Enter once after ATR warm-up; never emits an active exit."""

    def __init__(self, config: BuyHoldConfig) -> None:
        """Initialize the passive benchmark with incremental ATR state."""
        self._config = config
        self._atr = Atr(config.atr_period)
        self._entered = False
        self._last_open_time: datetime | None = None

    @property
    def name(self) -> str:
        """Return the stable strategy identifier used in reports."""
        return "buy_hold"

    def on_candle(self, candle: Candle, position: Position | None) -> Signal | None:
        """Emit the single benchmark entry after ATR warm-up while flat."""
        self._check_order(candle)
        atr = self._atr.update(
            float(candle.high_quote), float(candle.low_quote), float(candle.close_quote)
        )
        if position is not None or self._entered or atr is None:
            return None
        stop = _atr_stop(candle.close_quote, atr, self._config.atr_stop_multiple)
        if stop is None:
            return None
        self._entered = True
        return _buy_signal(self.name, candle, stop, ("buy-and-hold benchmark entry",))

    def _check_order(self, candle: Candle) -> None:
        if self._last_open_time is not None and candle.open_time <= self._last_open_time:
            raise ValueError(
                f"out-of-order or duplicate candle: {candle.open_time.isoformat()} after "
                f"{self._last_open_time.isoformat()}"
            )
        self._last_open_time = candle.open_time


class DcaConfig(BaseModel):
    """Time-based accumulator benchmark."""

    model_config = ConfigDict(frozen=True)

    interval_candles: int = Field(default=24, ge=1)
    atr_period: int = Field(default=14, ge=2)
    atr_stop_multiple: float = Field(default=2.0, gt=0.0)


class DcaStrategy:
    """Buy on a fixed candle cadence while flat; never predicts direction."""

    def __init__(self, config: DcaConfig) -> None:
        """Initialize the cadence benchmark with incremental ATR state."""
        self._config = config
        self._atr = Atr(config.atr_period)
        self._seen = 0
        self._last_open_time: datetime | None = None

    @property
    def name(self) -> str:
        """Return the stable strategy identifier used in reports."""
        return "dca"

    def on_candle(self, candle: Candle, position: Position | None) -> Signal | None:
        """Emit a time-based benchmark entry while flat on the configured cadence."""
        self._check_order(candle)
        self._seen += 1
        atr = self._atr.update(
            float(candle.high_quote), float(candle.low_quote), float(candle.close_quote)
        )
        if position is not None or atr is None or self._seen % self._config.interval_candles != 0:
            return None
        stop = _atr_stop(candle.close_quote, atr, self._config.atr_stop_multiple)
        if stop is None:
            return None
        return _buy_signal(
            self.name,
            candle,
            stop,
            (f"DCA benchmark: every {self._config.interval_candles} candles",),
        )

    def _check_order(self, candle: Candle) -> None:
        if self._last_open_time is not None and candle.open_time <= self._last_open_time:
            raise ValueError(
                f"out-of-order or duplicate candle: {candle.open_time.isoformat()} after "
                f"{self._last_open_time.isoformat()}"
            )
        self._last_open_time = candle.open_time


class GridConfig(BaseModel):
    """Simple spot grid benchmark around a rolling anchor."""

    model_config = ConfigDict(frozen=True)

    grid_step_fraction: Decimal = Field(default=Decimal("0.02"), gt=0, lt=1)
    stop_step_multiple: Decimal = Field(default=Decimal("2"), gt=0)


class GridStrategy:
    """Buy dips below a rolling anchor; sell rebounds above entry/anchor."""

    def __init__(self, config: GridConfig) -> None:
        """Initialize the grid benchmark with a lazy price anchor."""
        self._config = config
        self._anchor_quote: Decimal | None = None
        self._last_open_time: datetime | None = None

    @property
    def name(self) -> str:
        """Return the stable strategy identifier used in reports."""
        return "grid"

    def on_candle(self, candle: Candle, position: Position | None) -> Signal | None:
        """Emit grid entries and exits relative to the rolling anchor."""
        self._check_order(candle)
        close = candle.close_quote
        if self._anchor_quote is None:
            self._anchor_quote = close
            return None
        step = self._config.grid_step_fraction
        if position is None:
            buy_level = self._anchor_quote * (Decimal(1) - step)
            if close <= buy_level:
                stop = close * (Decimal(1) - step * self._config.stop_step_multiple)
                if stop <= 0:
                    return None
                self._anchor_quote = close
                return _buy_signal(
                    self.name,
                    candle,
                    stop.quantize(ACCOUNTING_RESOLUTION, rounding=ROUND_HALF_EVEN),
                    (f"grid benchmark: close fell {step:.4f} below anchor",),
                )
            self._anchor_quote = close
            return None
        entry = position.average_entry_price_quote
        sell_level = max(self._anchor_quote, entry) * (Decimal(1) + step)
        if close >= sell_level:
            self._anchor_quote = close
            return Signal(
                signal_id=_signal_id(self.name, candle),
                strategy_name=self.name,
                symbol=candle.symbol,
                side=Side.SELL,
                confidence=1.0,
                stop_price_quote=candle.close_quote,
                reasons=(f"grid benchmark: close rebounded {step:.4f} above grid level",),
                created_at=candle.close_time,
            )
        return None

    def _check_order(self, candle: Candle) -> None:
        if self._last_open_time is not None and candle.open_time <= self._last_open_time:
            raise ValueError(
                f"out-of-order or duplicate candle: {candle.open_time.isoformat()} after "
                f"{self._last_open_time.isoformat()}"
            )
        self._last_open_time = candle.open_time


CONTROL_STRATEGIES: Mapping[str, tuple[type[BaseModel], Callable[..., Strategy]]] = {
    "random_entry": (RandomEntryConfig, RandomEntryStrategy),
    "buy_hold": (BuyHoldConfig, BuyHoldStrategy),
    "dca": (DcaConfig, DcaStrategy),
    "grid": (GridConfig, GridStrategy),
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


def _atr_stop(close_quote: Decimal, atr: float, multiple: float) -> Decimal | None:
    stop = (close_quote - Decimal(str(multiple * atr))).quantize(
        ACCOUNTING_RESOLUTION, rounding=ROUND_HALF_EVEN
    )
    return stop if stop > 0 else None


def _signal_id(strategy_name: str, candle: Candle) -> str:
    return f"{strategy_name}:{candle.symbol}:{candle.close_time.isoformat()}"


def _buy_signal(
    strategy_name: str, candle: Candle, stop_price_quote: Decimal, reasons: tuple[str, ...]
) -> Signal:
    return Signal(
        signal_id=_signal_id(strategy_name, candle),
        strategy_name=strategy_name,
        symbol=candle.symbol,
        side=Side.BUY,
        confidence=1.0,
        stop_price_quote=stop_price_quote,
        reasons=reasons,
        created_at=candle.close_time,
    )
