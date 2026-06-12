"""Backtest fill simulator: pessimistic, deterministic, candle-driven.

Fill assumptions err against the strategy on purpose (ARCHITECTURE.md §8,
"model fees and slippage pessimistically"):

- **Market** orders fill on the *next* candle's open — decisions made on a
  closed candle can never fill inside it — with slippage applied against the
  trade and taker fees.
- **Limit** orders fill at exactly the limit price (never with price
  improvement) when the candle range touches it, with maker fees.
- **Stop-limit** orders trigger when the candle range crosses the stop, then
  fill at the limit price with taker fees — unless the candle *opens* beyond
  the limit (gapped through), in which case they stay unfilled: stop-limit
  gap risk is real and must show up in backtests. Triggering is permanent,
  as on a real exchange: a gapped-through stop remains an active limit order
  and fills if price later returns to its limit.

Three optional fidelity knobs, all **off by default** so the golden
backtest and existing paper behavior are bit-identical until a config opts
in:

- ``max_volume_fraction`` caps how much of a candle's volume one market
  order may consume; the remainder stays pending and fills on later candles
  (**partial fills**). A zero-volume candle fills nothing — outage and
  missed-candle behavior falls out of the data.
- ``volume_impact_bps`` adds slippage proportional to the share of the
  candle's volume the fill consumes (**volume-aware slippage**) — large
  orders in thin candles pay for it.
- ``submit_latency_candles`` keeps a new order inactive for N candles of
  its symbol (**latency**): decisions do not reach the venue instantly.
"""

from __future__ import annotations

from decimal import ROUND_HALF_EVEN, Decimal

from pydantic import BaseModel, ConfigDict

from tradebot.core.models import ACCOUNTING_RESOLUTION, Candle, Fill, Order, OrderType, Side
from tradebot.execution.adapter import FillHandler

_BPS_DIVISOR = Decimal(10_000)

# A fat-finger guard, not a market rule: no spot venue charges 10% a side, so
# a value above this is almost certainly a units mistake (percent vs bps).
MAX_FEE_BPS = Decimal(1_000)


class FeeSchedule:
    """Live, mutable per-*side* trading fees in basis points.

    The operator sets one buy fee and one sell fee that apply to every paper
    fill (ARCHITECTURE.md §8: fees are part of the P&L the competition is
    judged on). A single instance is shared by every paper engine's
    simulator, so a fee change takes effect on the next fill without
    rebuilding engines — which hold live orders that must not be dropped.

    This is deliberately separate from :class:`FillSimulatorConfig`: backtests
    and research keep that frozen, order-type-based (maker/taker) cost model
    for determinism (the golden backtest must stay byte-identical), while live
    paper trading reflects whatever fee the operator has configured.
    """

    def __init__(self, buy_fee_bps: Decimal, sell_fee_bps: Decimal) -> None:
        """Start with the given per-side fees; both validated like :meth:`update`."""
        self._buy_fee_bps = Decimal(0)
        self._sell_fee_bps = Decimal(0)
        self.update(buy_fee_bps=buy_fee_bps, sell_fee_bps=sell_fee_bps)

    @classmethod
    def standard(cls) -> FeeSchedule:
        """Return the conventional ~Binance spot taker fee (0.1%) on both sides."""
        return cls(buy_fee_bps=Decimal(10), sell_fee_bps=Decimal(10))

    @property
    def buy_fee_bps(self) -> Decimal:
        """Fee charged on every buy fill, in basis points of notional."""
        return self._buy_fee_bps

    @property
    def sell_fee_bps(self) -> Decimal:
        """Fee charged on every sell fill, in basis points of notional."""
        return self._sell_fee_bps

    def fee_bps_for(self, side: Side) -> Decimal:
        """Return the fee in bps that applies to a fill on ``side``."""
        return self._buy_fee_bps if side == Side.BUY else self._sell_fee_bps

    def update(self, *, buy_fee_bps: Decimal, sell_fee_bps: Decimal) -> None:
        """Replace both fees in place; rejects negative or absurd values.

        Mutating in place (rather than swapping the object) is what lets a
        running engine's simulator pick up the new fee on its next fill.
        """
        for label, value in (("buy", buy_fee_bps), ("sell", sell_fee_bps)):
            if not value.is_finite():
                # NaN/Infinity would slip past the bound checks below (every
                # comparison with NaN is False) and then poison fee math and
                # the portfolio balance — reject it at the door.
                raise ValueError(f"{label} fee must be a finite number, got {value}")
            if value < 0:
                raise ValueError(f"{label} fee cannot be negative, got {value} bps")
            if value > MAX_FEE_BPS:
                raise ValueError(
                    f"{label} fee {value} bps exceeds the {MAX_FEE_BPS} bps sanity cap "
                    "(did you pass a percent instead of basis points?)"
                )
        self._buy_fee_bps = buy_fee_bps
        self._sell_fee_bps = sell_fee_bps


class FillSimulatorConfig(BaseModel):
    """Fee and slippage assumptions, in basis points (defaults ≈ Binance spot)."""

    model_config = ConfigDict(frozen=True)

    maker_fee_bps: Decimal = Decimal(10)
    taker_fee_bps: Decimal = Decimal(10)
    market_slippage_bps: Decimal = Decimal(5)

    max_volume_fraction: Decimal = Decimal(0)
    """Max share of one candle's volume a market order may fill; 0 = off
    (whole orders fill in one candle, the pre-partial-fill behavior)."""

    volume_impact_bps: Decimal = Decimal(0)
    """Extra slippage per 100% of candle volume consumed by the fill;
    0 = off (flat ``market_slippage_bps`` only)."""

    submit_latency_candles: int = 0
    """Candles of the order's symbol that pass before it becomes active;
    0 = off (active from the next candle, as before)."""


class SimulatedExecutionAdapter:
    """``ExecutionAdapter`` implementation driven by replayed candles.

    The backtest runner calls :meth:`process_candle` once per closed candle;
    any fills are delivered through the registered handler in deterministic
    order (market orders first, then resting orders in submission order).
    """

    def __init__(self, config: FillSimulatorConfig, fees: FeeSchedule | None = None) -> None:
        """Create a simulator with the given fee/slippage assumptions.

        When ``fees`` is given (live paper trading), every fill is charged the
        operator's per-side fee from that schedule instead of the config's
        maker/taker fee. Omitting it keeps the frozen maker/taker model, which
        backtests and the golden regression rely on for determinism.
        """
        self._config = config
        self._fees = fees
        self._pending_market: dict[str, Order] = {}
        self._resting: dict[str, Order] = {}
        self._triggered: set[str] = set()
        # Partial-fill remainder per market order; absent = untouched.
        self._remaining: dict[str, Decimal] = {}
        # Candles of the order's symbol still to pass before it activates.
        self._delay: dict[str, int] = {}
        self._fill_handler: FillHandler | None = None

    def set_fill_handler(self, handler: FillHandler) -> None:
        """Register the single consumer of simulated fills."""
        self._fill_handler = handler

    async def submit(self, order: Order) -> None:
        """Accept an order for simulation; validates shape and id uniqueness."""
        self._accept(order)

    def restore_order(self, order: Order, *, triggered: bool = False) -> None:
        """Re-accept a persisted open order after a restart.

        Same validation as :meth:`submit`, plus the stop-trigger latch:
        a stop-limit whose stop had already crossed must come back as an
        active limit order, not a re-armed stop.
        """
        if triggered and order.order_type != OrderType.STOP_LIMIT:
            raise ValueError(
                f"only stop_limit orders carry a trigger latch, got {order.order_type}"
            )
        self._accept(order)
        if triggered:
            self._triggered.add(order.client_order_id)

    def triggered_order_ids(self) -> frozenset[str]:
        """Ids of resting stop-limits whose trigger has latched."""
        return frozenset(self._triggered)

    def _accept(self, order: Order) -> None:
        if order.client_order_id in self._pending_market or order.client_order_id in self._resting:
            raise ValueError(f"duplicate client_order_id {order.client_order_id!r}")
        if self._config.submit_latency_candles > 0:
            self._delay[order.client_order_id] = self._config.submit_latency_candles
        if order.order_type == OrderType.MARKET:
            self._pending_market[order.client_order_id] = order
            return
        if order.limit_price_quote is None:
            raise ValueError(f"{order.order_type} order requires limit_price_quote")
        if order.order_type == OrderType.STOP_LIMIT and order.stop_price_quote is None:
            raise ValueError("stop_limit order requires stop_price_quote")
        self._resting[order.client_order_id] = order

    async def cancel(self, client_order_id: str) -> None:
        """Remove a not-yet-filled order; unknown ids are an upstream bug."""
        if self._pending_market.pop(client_order_id, None) is not None:
            self._remaining.pop(client_order_id, None)
            self._delay.pop(client_order_id, None)
            return
        if self._resting.pop(client_order_id, None) is not None:
            self._triggered.discard(client_order_id)
            self._delay.pop(client_order_id, None)
            return
        raise ValueError(f"cannot cancel unknown order {client_order_id!r}")

    def open_orders(self) -> tuple[Order, ...]:
        """Return pending market and resting orders, oldest first."""
        return tuple(self._pending_market.values()) + tuple(self._resting.values())

    async def process_candle(self, candle: Candle) -> None:
        """Evaluate every open order of this candle's symbol against it."""
        fills: list[Fill] = []
        for order in list(self._pending_market.values()):
            if order.symbol != candle.symbol:
                continue
            if self._tick_delay(order.client_order_id):
                continue
            fill = self._fill_market(order, candle)
            if fill is None:
                continue  # zero tradable volume this candle; keep waiting
            fills.append(fill)
        for order in list(self._resting.values()):
            if order.symbol != candle.symbol:
                continue
            if self._tick_delay(order.client_order_id):
                continue
            fill = self._try_fill_resting(order, candle)
            if fill is not None:
                del self._resting[order.client_order_id]
                self._triggered.discard(order.client_order_id)
                fills.append(fill)
        if self._fill_handler is None:
            if fills:
                raise RuntimeError("fills produced but no fill handler is registered")
            return
        for fill in fills:
            await self._fill_handler(fill)

    def _tick_delay(self, client_order_id: str) -> bool:
        """Advance the order's latency clock; True while it is still inactive."""
        delay = self._delay.get(client_order_id, 0)
        if delay <= 0:
            return False
        self._delay[client_order_id] = delay - 1
        return True

    def _fill_market(self, order: Order, candle: Candle) -> Fill | None:
        order_id = order.client_order_id
        remaining = self._remaining.get(order_id, order.quantity_base)
        quantity = remaining
        if self._config.max_volume_fraction > 0:
            tradable = candle.volume_base * self._config.max_volume_fraction
            quantity = min(remaining, tradable)
            if quantity <= 0:
                return None  # no volume this candle: nothing can fill
        slip = self._config.market_slippage_bps / _BPS_DIVISOR
        if self._config.volume_impact_bps > 0 and candle.volume_base > 0:
            # Impact scales with the share of the candle this fill consumes:
            # the same order pays more in a thin candle than a thick one.
            slip += (self._config.volume_impact_bps / _BPS_DIVISOR) * (
                quantity / candle.volume_base
            )
        direction = 1 if order.side == Side.BUY else -1
        price = (candle.open_quote * (1 + direction * slip)).quantize(
            ACCOUNTING_RESOLUTION, rounding=ROUND_HALF_EVEN
        )
        if quantity >= remaining:
            del self._pending_market[order_id]
            self._remaining.pop(order_id, None)
        else:
            self._remaining[order_id] = remaining - quantity
        # Market orders fill at the candle's open, so that is the fill time.
        return self._make_fill(
            order,
            price,
            self._fee_bps(order, self._config.taker_fee_bps),
            candle,
            at_open=True,
            quantity=quantity,
        )

    def _fee_bps(self, order: Order, fallback_bps: Decimal) -> Decimal:
        """Return the operator's live per-side fee if set, else the config's fee.

        ``fallback_bps`` is the order-type fee (maker/taker) the deterministic
        backtest path uses; the live paper path overrides it with the
        configured buy/sell fee for ``order.side``.
        """
        return self._fees.fee_bps_for(order.side) if self._fees is not None else fallback_bps

    def _try_fill_resting(self, order: Order, candle: Candle) -> Fill | None:
        assert order.limit_price_quote is not None  # validated at submit
        limit = order.limit_price_quote
        if order.order_type == OrderType.LIMIT:
            touched = (
                candle.low_quote <= limit if order.side == Side.BUY else candle.high_quote >= limit
            )
            if not touched:
                return None
            return self._make_fill(
                order, limit, self._fee_bps(order, self._config.maker_fee_bps), candle
            )

        assert order.stop_price_quote is not None  # validated at submit
        stop = order.stop_price_quote
        order_id = order.client_order_id
        if order_id not in self._triggered:
            crossed = (
                candle.low_quote <= stop if order.side == Side.SELL else candle.high_quote >= stop
            )
            if not crossed:
                return None
            # Triggering is permanent, like on a real exchange: from here on
            # the order is an active limit order even if this candle gaps
            # through the limit and cannot fill it.
            self._triggered.add(order_id)
            gapped_through = (
                candle.open_quote < limit if order.side == Side.SELL else candle.open_quote > limit
            )
            if gapped_through:
                return None
            return self._make_fill(
                order, limit, self._fee_bps(order, self._config.taker_fee_bps), candle
            )
        # Already triggered: behaves as a resting limit order awaiting its price.
        reachable = (
            candle.high_quote >= limit if order.side == Side.SELL else candle.low_quote <= limit
        )
        if not reachable:
            return None
        return self._make_fill(
            order, limit, self._fee_bps(order, self._config.taker_fee_bps), candle
        )

    def _make_fill(
        self,
        order: Order,
        price_quote: Decimal,
        fee_bps: Decimal,
        candle: Candle,
        *,
        at_open: bool = False,
        quantity: Decimal | None = None,
    ) -> Fill:
        filled_quantity = order.quantity_base if quantity is None else quantity
        fee = (price_quote * filled_quantity * fee_bps / _BPS_DIVISOR).quantize(
            ACCOUNTING_RESOLUTION, rounding=ROUND_HALF_EVEN
        )
        # Intracandle fills are only *known* once the candle closes, so they
        # are stamped with close_time; market fills happen at the open itself.
        return Fill(
            client_order_id=order.client_order_id,
            symbol=order.symbol,
            side=order.side,
            price_quote=price_quote,
            quantity_base=filled_quantity,
            fee_quote=fee,
            filled_at=candle.open_time if at_open else candle.close_time,
        )
