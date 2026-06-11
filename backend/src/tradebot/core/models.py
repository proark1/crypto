"""Domain models that cross module boundaries.

Two repository-wide invariants are enforced here at the type level so that no
caller can violate them accidentally (CLAUDE.md, "Non-negotiable safety
invariants"):

- every monetary amount — price, quantity, fee, PnL — is ``Decimal``; ``float``
  input is rejected, not coerced, because the rounding error of a silent
  float→Decimal conversion is exactly the bug the rule exists to prevent;
- every timestamp is timezone-aware UTC; naive datetimes are rejected and
  non-UTC timezones are normalized to UTC.

Units are explicit in field names: ``*_quote`` is an amount in the quote
currency (e.g. USDT), ``*_base`` an amount of the traded asset.
"""

from __future__ import annotations

import enum
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Annotated

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field


def _reject_float(value: object) -> object:
    """Refuse float input for monetary fields; require Decimal, int, or str."""
    if isinstance(value, float):
        raise ValueError("float is not allowed for monetary values; pass Decimal or str")
    return value


def _ensure_utc(value: datetime) -> datetime:
    """Reject naive datetimes and normalize timezone-aware ones to UTC."""
    if value.tzinfo is None:
        raise ValueError("naive datetime is not allowed; timestamps must be UTC-aware")
    return value.astimezone(UTC)


ACCOUNTING_RESOLUTION = Decimal("1e-12")
"""Book-keeping granularity for derived (divided) monetary amounts.

Division is the one Decimal operation that produces unbounded digits;
quantizing its results to a fixed resolution keeps every subsequent sum exact
within Decimal's 28-digit context. 1e-12 is far below any exchange's quote
precision (typically 1e-8), so it never affects a real amount."""

Amount = Annotated[Decimal, BeforeValidator(_reject_float)]
"""A monetary amount that may legitimately be zero or negative (e.g. PnL)."""

PositiveAmount = Annotated[Decimal, BeforeValidator(_reject_float), Field(gt=0)]
"""A monetary amount that must be strictly positive (prices, order quantities)."""

UtcDatetime = Annotated[datetime, BeforeValidator(_ensure_utc)]
"""A timestamp guaranteed to be timezone-aware and normalized to UTC."""


def utc_now() -> datetime:
    """Return the current wall-clock time as a UTC-aware datetime.

    Production event-flow code should prefer :class:`tradebot.core.clock.Clock`
    so backtests can control time; this helper is for timestamps where wall
    time is genuinely meant (e.g. audit records, default model timestamps).
    """
    return datetime.now(tz=UTC)


class Side(enum.StrEnum):
    """Direction of a signal, order, or fill."""

    BUY = "buy"
    SELL = "sell"


class CandleInterval(enum.StrEnum):
    """Supported candle resolutions; 1m is the base, larger ones are aggregates."""

    M1 = "1m"
    M5 = "5m"
    M15 = "15m"
    H1 = "1h"
    H4 = "4h"
    D1 = "1d"

    @property
    def duration(self) -> timedelta:
        """Wall-clock length of one candle at this interval."""
        return _INTERVAL_DURATIONS[self]


_INTERVAL_DURATIONS: dict[CandleInterval, timedelta] = {
    CandleInterval.M1: timedelta(minutes=1),
    CandleInterval.M5: timedelta(minutes=5),
    CandleInterval.M15: timedelta(minutes=15),
    CandleInterval.H1: timedelta(hours=1),
    CandleInterval.H4: timedelta(hours=4),
    CandleInterval.D1: timedelta(days=1),
}


class OrderType(enum.StrEnum):
    """Order types the execution engine knows how to place."""

    LIMIT = "limit"
    MARKET = "market"
    STOP_LIMIT = "stop_limit"


class Candle(BaseModel):
    """One OHLCV candle for a symbol at a fixed interval.

    ``open_time`` is the inclusive start of the interval, ``close_time`` its
    exclusive end. Price-shape sanity (high >= low etc.) is deliberately NOT
    enforced here: the market-data validation layer decides whether odd data is
    quarantined; models only guarantee types and units.
    """

    model_config = ConfigDict(frozen=True)

    symbol: str
    interval: CandleInterval
    open_time: UtcDatetime
    close_time: UtcDatetime
    open_quote: PositiveAmount
    high_quote: PositiveAmount
    low_quote: PositiveAmount
    close_quote: PositiveAmount
    volume_base: Amount


class Signal(BaseModel):
    """A strategy's proposal to trade — never an order.

    Signals carry everything the risk manager needs to size or veto the trade,
    plus the human-readable ``reasons`` that the UI's decision-pipeline view and
    co-pilot approvals display. ``stop_price_quote`` is mandatory: a trade idea
    without a defined invalidation point is not a signal.
    """

    model_config = ConfigDict(frozen=True)

    signal_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    strategy_name: str
    symbol: str
    side: Side
    confidence: float = Field(ge=0.0, le=1.0)
    entry_price_quote: PositiveAmount | None = None
    stop_price_quote: PositiveAmount
    target_price_quote: PositiveAmount | None = None
    breakeven_at_r: float = 0.0
    """Stop management: once the trade reaches this many R of open profit,
    the protective stop ratchets to the entry price. ``0`` disables."""

    trail_distance_quote: PositiveAmount | None = None
    """Stop management: trail the stop this far (quote) below the highest
    high since entry, frozen at signal time (k x ATR at entry) so the
    behavior is deterministic and replayable. ``None`` disables."""

    reasons: tuple[str, ...] = ()
    created_at: UtcDatetime = Field(default_factory=utc_now)


class SymbolFilters(BaseModel):
    """One trading pair's venue rules, as the exchange enforces them.

    Zero means unconstrained — paper trading without a market catalog and
    every backtest run this way, so the golden fixture never depends on a
    venue lookup. With real values (from ccxt's market metadata) the risk
    manager aligns quantities/prices and vetoes entries the venue would
    reject, keeping paper fills exchange-plausible.
    """

    model_config = ConfigDict(frozen=True)

    price_tick_quote: Amount = Decimal(0)
    """Price increment; order prices must be a multiple of it."""

    quantity_step_base: Amount = Decimal(0)
    """Quantity increment (lot step); order sizes must be a multiple."""

    min_quantity_base: Amount = Decimal(0)
    """Smallest order size the venue accepts."""

    min_notional_quote: Amount = Decimal(0)
    """Smallest order value (price x quantity) the venue accepts."""

    def align_quantity(self, quantity_base: Decimal) -> Decimal:
        """Round ``quantity_base`` down to the lot step (never up: caps hold)."""
        if self.quantity_step_base <= 0:
            return quantity_base
        return (quantity_base // self.quantity_step_base) * self.quantity_step_base

    def align_price_down(self, price_quote: Decimal) -> Decimal:
        """Round ``price_quote`` down to the tick.

        Down on purpose for protective sell stops: rounding a stop *up*
        could trigger above the level the position was sized against. The
        one exception is a level below a single tick — zero is not a price
        the venue (or ``PositiveAmount``) accepts, so it clamps to the
        tick, the smallest representable price.
        """
        if self.price_tick_quote <= 0:
            return price_quote
        aligned = (price_quote // self.price_tick_quote) * self.price_tick_quote
        return max(aligned, self.price_tick_quote)

    def entry_block_reason(self, quantity_base: Decimal, price_quote: Decimal) -> str | None:
        """Why the venue would reject this entry, or ``None`` if it passes."""
        if self.min_quantity_base > 0 and quantity_base < self.min_quantity_base:
            return f"quantity {quantity_base} is below the venue minimum {self.min_quantity_base}"
        notional = quantity_base * price_quote
        if self.min_notional_quote > 0 and notional < self.min_notional_quote:
            return f"notional {notional} is below the venue minimum {self.min_notional_quote}"
        return None


class ProtectiveExitPlan(BaseModel):
    """How the position opened by an entry order will be protected.

    Deliberately separate from ``Signal.stop_price_quote`` (the *risk
    invalidation* level used for sizing and evaluation grading): this is the
    *exchange order* — a stop-limit whose trigger starts at the invalidation
    level and whose limit price sits below it by a configured offset,
    bounding how far a fast market can fill past the stop. The ratchet
    policy (breakeven, trail) is carried here too, so the whole protection
    plan survives restarts with the entry order. Keeping the three meanings
    of "stop" (invalidation, resting exchange order, evaluation reference)
    in distinct places stops them drifting apart silently.
    """

    model_config = ConfigDict(frozen=True)

    stop_price_quote: PositiveAmount
    """Initial trigger level — the signal's risk-invalidation price."""

    limit_price_quote: PositiveAmount
    """Limit floor of the stop-limit order, below the trigger."""

    breakeven_at_r: float = 0.0
    """Ratchet policy, copied from the signal: lock the stop to the entry
    price once the trade has earned this many R. ``0`` disables."""

    trail_distance_quote: PositiveAmount | None = None
    """Ratchet policy, copied from the signal: trail the stop this far
    below the highest high since entry. ``None`` disables."""


class Order(BaseModel):
    """A risk-approved instruction for the execution engine.

    Only the risk manager constructs these (CLAUDE.md invariant 4); the
    ``signal_id`` lineage is mandatory so every order traces back to the signal
    and gate decisions that produced it. ``client_order_id`` is deterministic
    per intent at the call site, making resubmission after a disconnect
    idempotent.

    ``protective_exit`` rides on entry orders only: when the entry fills, the
    engine arms the planned stop-limit through the risk manager. It persists
    with the order so a restart between submission and fill (or between fill
    and stop placement) can still protect the position.
    """

    model_config = ConfigDict(frozen=True)

    client_order_id: str
    signal_id: str
    symbol: str
    side: Side
    order_type: OrderType
    quantity_base: PositiveAmount
    limit_price_quote: PositiveAmount | None = None
    stop_price_quote: PositiveAmount | None = None
    protective_exit: ProtectiveExitPlan | None = None
    created_at: UtcDatetime = Field(default_factory=utc_now)


class OrderStatus(enum.StrEnum):
    """Lifecycle of a journaled order intent (paper-mode order journal).

    ``OPEN`` covers both pending market orders and resting limit/stop-limit
    orders; ``FILLED`` and ``CANCELLED`` are terminal. There is no partial
    state today because the simulator fills whole orders only — when partial
    fills arrive, remaining quantity becomes a property of the fill journal,
    not a new status.
    """

    OPEN = "open"
    FILLED = "filled"
    CANCELLED = "cancelled"


class Fill(BaseModel):
    """An execution (possibly partial) of an order reported by an adapter.

    ``fee_quote`` is the fee already converted to the quote currency by the
    adapter, so portfolio accounting never needs exchange-specific fee logic.
    """

    model_config = ConfigDict(frozen=True)

    client_order_id: str
    symbol: str
    side: Side
    price_quote: PositiveAmount
    quantity_base: PositiveAmount
    fee_quote: Amount
    filled_at: UtcDatetime


class DecisionOutcome(enum.StrEnum):
    """What happened to a signal at the risk/authorization boundary."""

    SUBMITTED = "submitted"
    VETOED = "vetoed"
    GATED = "gated"
    PAUSED = "paused"
    PROPOSED = "proposed"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED = "expired"
    DRIFTED = "drifted"
    SUPERSEDED = "superseded"
    """An exit signal arrived while another exit order was already in
    flight for the same position; honoring both would over-sell."""


class AutonomyMode(enum.StrEnum):
    """Who has the final word on entries (ARCHITECTURE.md 4.8)."""

    AUTONOMOUS = "autonomous"
    COPILOT = "copilot"


class ProposalStatus(enum.StrEnum):
    """Lifecycle of a co-pilot proposal."""

    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED = "expired"
    DRIFTED = "drifted"
    SUPERSEDED = "superseded"
    """An exit signal arrived while another exit order was already in
    flight for the same position; honoring both would over-sell."""


class Proposal(BaseModel):
    """An entry signal awaiting the user's word (co-pilot mode).

    ``proposal_price_quote`` is the close at proposal time: approval is
    refused if price has drifted too far from it, so a stale approval can
    never execute at a price the user did not look at.
    """

    model_config = ConfigDict(frozen=True)

    signal: Signal
    proposal_price_quote: PositiveAmount
    created_at: UtcDatetime
    expires_at: UtcDatetime
    status: ProposalStatus = ProposalStatus.PENDING


class Decision(BaseModel):
    """One signal and its fate — the raw material of explainability.

    The UI's decision-pipeline view shows these verbatim (ARCHITECTURE.md
    6.2): the bot never does, or declines to do, something it cannot explain.
    """

    model_config = ConfigDict(frozen=True)

    signal_id: str
    strategy_name: str
    symbol: str
    side: Side
    stop_price_quote: PositiveAmount
    reasons: tuple[str, ...]
    outcome: DecisionOutcome
    created_at: UtcDatetime
