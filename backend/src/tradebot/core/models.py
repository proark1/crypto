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
from datetime import UTC, datetime
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
    reasons: tuple[str, ...] = ()
    created_at: UtcDatetime = Field(default_factory=utc_now)


class Order(BaseModel):
    """A risk-approved instruction for the execution engine.

    Only the risk manager constructs these (CLAUDE.md invariant 4); the
    ``signal_id`` lineage is mandatory so every order traces back to the signal
    and gate decisions that produced it. ``client_order_id`` is deterministic
    per intent at the call site, making resubmission after a disconnect
    idempotent.
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
    created_at: UtcDatetime = Field(default_factory=utc_now)


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
