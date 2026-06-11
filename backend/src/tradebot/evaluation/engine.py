"""Blind decision, reveal, and grading for one scenario (ARCHITECTURE.md §12.2).

The evaluator replays the strategy over the context window exactly as the
trading engine would (entries fill on the next open with adverse slippage,
stops are honored candle by candle), reads the decision made on the final
window candle, and only then opens the horizon to grade it. The decision
phase never touches a candle at or after the decision index — the leak tests
in ``tests/evaluation/test_engine.py`` prove a different future cannot
change the decision.

Money is Decimal throughout; R-multiples are ratios of money and stay
Decimal (quantized like all division results).
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from decimal import ROUND_HALF_EVEN, Decimal

from pydantic import BaseModel, ConfigDict

from tradebot.core.models import ACCOUNTING_RESOLUTION, Amount, Candle, Side, Signal
from tradebot.evaluation.models import ScenarioClass, TimingLabel, Verdict
from tradebot.execution import FillSimulatorConfig
from tradebot.indicators import Atr
from tradebot.portfolio import Position
from tradebot.risk import ManagedStop
from tradebot.strategies import Strategy

_BPS_DIVISOR = Decimal(10_000)

REFERENCE_ATR_PERIOD = 14
REFERENCE_ATR_STOP_MULTIPLE = 2
"""Stop convention of the reference trade used to grade a flat hold —
the strategy family's own (frozen in ARCHITECTURE.md section 12.2)."""

MISSED_OPPORTUNITY_R = Decimal(1)
EARLY_ENTRY_MAE_R = Decimal("-0.5")
LATE_ENTRY_MFE_R = Decimal("0.25")
EXIT_TIMING_R = Decimal("0.5")


class ScenarioSpec(BaseModel):
    """Coordinates of one blind decision inside a candle series.

    ``decision_index`` is the index of the first *future* candle: the
    window is ``candles[decision_index - lookback : decision_index]`` and
    the horizon ``candles[decision_index : decision_index + horizon]``.
    """

    model_config = ConfigDict(frozen=True)

    decision_index: int
    lookback: int
    horizon: int


class EvaluatedDecision(BaseModel):
    """One scenario's blind decision and its grade after the reveal."""

    model_config = ConfigDict(frozen=True)

    scenario_class: ScenarioClass
    decision: str
    confidence: float | None = None
    reasons: tuple[str, ...] = ()
    entry_price_quote: Amount | None = None
    exit_price_quote: Amount | None = None
    r_multiple: Amount | None = None
    pnl_quote: Amount | None = None
    mfe_r: Amount | None = None
    mae_r: Amount | None = None
    duration_candles: int | None = None
    stop_hit: bool | None = None
    oracle_r: Amount | None = None
    verdict: Verdict
    timing: TimingLabel | None = None


class _OpenTrade(BaseModel):
    """A simulated unit-quantity long while it is held.

    ``stop_price_quote`` stays the *initial* invalidation point — R is
    always money over initial risk. ``current_stop_quote`` is the managed
    (possibly ratcheted) level the trade would actually exit at; the
    trail's peak memory is embedded in that level, so a ``ManagedStop``
    rebuilt from it continues exactly where the replay left off.
    """

    model_config = ConfigDict(frozen=True)

    entry_price_quote: Amount
    stop_price_quote: Amount
    peak_high_quote: Amount
    current_stop_quote: Amount
    breakeven_at_r: float = 0.0
    trail_distance_quote: Amount | None = None

    def managed_stop(self) -> ManagedStop:
        return ManagedStop(
            entry_price_quote=self.entry_price_quote,
            initial_stop_quote=self.current_stop_quote,
            breakeven_at_r=self.breakeven_at_r,
            trail_distance_quote=self.trail_distance_quote,
        )

    def as_position(self, symbol: str) -> Position:
        return Position(
            symbol=symbol,
            quantity_base=Decimal(1),
            cost_basis_quote=self.entry_price_quote,
        )


class _ReplayState(BaseModel):
    """The world as of the decision moment, after the blind window replay."""

    model_config = ConfigDict(frozen=True)

    trade: _OpenTrade | None
    final_signal: Signal | None


class ScenarioEvaluator:
    """Decides blind and grades against the revealed horizon."""

    def __init__(
        self,
        strategy_factory: Callable[[], Strategy],
        fills: FillSimulatorConfig | None = None,
    ) -> None:
        """Bind the strategy factory.

        A fresh strategy instance is built per scenario, so indicator state
        can never bleed across scenarios.
        """
        self._strategy_factory = strategy_factory
        self._fills = fills or FillSimulatorConfig()

    def evaluate(self, candles: Sequence[Candle], spec: ScenarioSpec) -> EvaluatedDecision:
        """Run one scenario; only ``spec`` decides how much future is revealed."""
        if spec.decision_index - spec.lookback < 0:
            raise ValueError("lookback reaches before the start of the series")
        if spec.decision_index + spec.horizon > len(candles):
            raise ValueError("horizon reaches past the end of the series")
        window = list(candles[spec.decision_index - spec.lookback : spec.decision_index])
        # The decision is made strictly before this slice is taken; keeping
        # the materialization separate makes the boundary auditable.
        horizon = list(candles[spec.decision_index : spec.decision_index + spec.horizon])

        strategy = self._strategy_factory()
        state = self._replay_window(strategy, window)

        if state.trade is None:
            if state.final_signal is not None and state.final_signal.side == Side.BUY:
                return self._grade_entry(strategy, state.final_signal, horizon)
            return self._grade_flat_hold(window, horizon, state.final_signal)
        if state.final_signal is not None and state.final_signal.side == Side.SELL:
            return self._grade_exit(state.trade, state.final_signal, horizon)
        return self._grade_holding_hold(state.trade, horizon, state.final_signal)

    def _replay_window(self, strategy: Strategy, window: Sequence[Candle]) -> _ReplayState:
        """Walk the visible past exactly as the live loop would have."""
        trade: _OpenTrade | None = None
        managed: ManagedStop | None = None
        pending: Signal | None = None
        for candle in window:
            if pending is not None:
                if pending.side == Side.BUY and trade is None:
                    entry = self._slipped(candle.open_quote, buying=True)
                    trade = _OpenTrade(
                        entry_price_quote=entry,
                        stop_price_quote=pending.stop_price_quote,
                        peak_high_quote=candle.high_quote,
                        current_stop_quote=pending.stop_price_quote,
                        breakeven_at_r=pending.breakeven_at_r,
                        trail_distance_quote=pending.trail_distance_quote,
                    )
                    managed = ManagedStop.from_signal(pending, entry)
                elif pending.side == Side.SELL and trade is not None:
                    trade = None  # exit fills at this open; grading not needed in-window
                    managed = None
                pending = None
            if trade is not None and managed is not None:
                # Breach before ratchet, the engine's exact order of ops.
                if managed.is_breached_by(candle):
                    trade = None  # stopped out inside the window
                    managed = None
                else:
                    managed.ratchet(candle)
                    trade = trade.model_copy(
                        update={
                            "peak_high_quote": max(trade.peak_high_quote, candle.high_quote),
                            "current_stop_quote": managed.stop_price_quote,
                        }
                    )
            position = trade.as_position(candle.symbol) if trade is not None else None
            pending = strategy.on_candle(candle, position)
        return _ReplayState(trade=trade, final_signal=pending)

    def _grade_entry(
        self, strategy: Strategy, signal: Signal, horizon: Sequence[Candle]
    ) -> EvaluatedDecision:
        """Reveal the future and simulate the proposed entry to its end."""
        entry = self._slipped(horizon[0].open_quote, buying=True)
        stop = signal.stop_price_quote
        risk = entry - stop
        if risk <= 0:
            # Slippage pushed the fill at or below its own stop: no defined
            # risk unit, so the trade is ungradeable rather than misgraded.
            return EvaluatedDecision(
                scenario_class=ScenarioClass.FLAT,
                decision="buy",
                confidence=signal.confidence,
                reasons=signal.reasons,
                verdict=Verdict.NEUTRAL,
            )

        exit_price, exit_index, stop_hit, peak, trough = self._simulate_long(
            strategy, horizon, entry, stop, managed=ManagedStop.from_signal(signal, entry)
        )
        fees = self._fee(entry) + self._fee(exit_price)
        pnl = exit_price - entry - fees
        r = _ratio(pnl, risk)
        mfe = _ratio(peak - entry, risk)
        mae = _ratio(trough - entry, risk)
        oracle = _ratio(max(candle.high_quote for candle in horizon) - entry, risk)
        if r >= 0 and mae <= EARLY_ENTRY_MAE_R:
            timing = TimingLabel.EARLY_ENTRY
        elif r < 0 and mfe <= LATE_ENTRY_MFE_R:
            timing = TimingLabel.LATE_ENTRY
        else:
            timing = TimingLabel.ON_TIME
        return EvaluatedDecision(
            scenario_class=ScenarioClass.FLAT,
            decision="buy",
            confidence=signal.confidence,
            reasons=signal.reasons,
            entry_price_quote=entry,
            exit_price_quote=exit_price,
            r_multiple=r,
            pnl_quote=pnl,
            mfe_r=mfe,
            mae_r=mae,
            duration_candles=exit_index + 1,
            stop_hit=stop_hit,
            oracle_r=oracle,
            verdict=_trade_verdict(r),
            timing=timing,
        )

    def _grade_flat_hold(
        self, window: Sequence[Candle], horizon: Sequence[Candle], signal: Signal | None
    ) -> EvaluatedDecision:
        """Grade a pass against the strategy family's reference trade.

        The reference enters at the next open with a 2 x ATR(14) stop (the
        family's own convention) and exits on stop or horizon end — purely
        mechanical, no strategy exits. ``oracle_r`` carries its R so the
        report can size what was left on the table.
        """
        base = EvaluatedDecision(
            scenario_class=ScenarioClass.FLAT,
            decision="hold",
            confidence=signal.confidence if signal is not None else None,
            reasons=signal.reasons if signal is not None else (),
            verdict=Verdict.CORRECT_HOLD,
        )
        atr = Atr(REFERENCE_ATR_PERIOD)
        atr_value: float | None = None
        for candle in window:
            atr_value = atr.update(
                float(candle.high_quote), float(candle.low_quote), float(candle.close_quote)
            )
        if atr_value is None:
            return base  # window too short to build the reference; never guess
        entry = self._slipped(horizon[0].open_quote, buying=True)
        stop = entry - Decimal(str(REFERENCE_ATR_STOP_MULTIPLE * atr_value))
        risk = entry - stop
        if risk <= 0 or stop <= 0:
            return base
        exit_price, _, _, _, _ = self._simulate_long(None, horizon, entry, stop)
        fees = self._fee(entry) + self._fee(exit_price)
        reference_r = _ratio(exit_price - entry - fees, risk)
        if reference_r >= MISSED_OPPORTUNITY_R:
            return base.model_copy(
                update={"verdict": Verdict.MISSED_OPPORTUNITY, "oracle_r": reference_r}
            )
        return base.model_copy(update={"oracle_r": reference_r})

    def _grade_exit(
        self, trade: _OpenTrade, signal: Signal, horizon: Sequence[Candle]
    ) -> EvaluatedDecision:
        """Grade closing the held position at the decision."""
        exit_price = self._slipped(horizon[0].open_quote, buying=False)
        entry = trade.entry_price_quote
        risk = entry - trade.stop_price_quote
        fees = self._fee(entry) + self._fee(exit_price)
        pnl = exit_price - entry - fees
        r = _ratio(pnl, risk)
        peak_after_exit = max(candle.high_quote for candle in horizon)
        gave_back = _ratio(max(trade.peak_high_quote, horizon[0].high_quote) - exit_price, risk)
        left_on_table = _ratio(peak_after_exit - exit_price, risk)
        if left_on_table >= EXIT_TIMING_R:
            timing = TimingLabel.EARLY_EXIT
        elif gave_back >= EXIT_TIMING_R:
            timing = TimingLabel.LATE_EXIT
        else:
            timing = TimingLabel.ON_TIME
        return EvaluatedDecision(
            scenario_class=ScenarioClass.HOLDING,
            decision="sell",
            confidence=signal.confidence,
            reasons=signal.reasons,
            entry_price_quote=entry,
            exit_price_quote=exit_price,
            r_multiple=r,
            pnl_quote=pnl,
            mfe_r=_ratio(trade.peak_high_quote - entry, risk),
            duration_candles=1,
            stop_hit=False,
            oracle_r=_ratio(peak_after_exit - entry, risk),
            verdict=_trade_verdict(r),
            timing=timing,
        )

    def _grade_holding_hold(
        self, trade: _OpenTrade, horizon: Sequence[Candle], signal: Signal | None
    ) -> EvaluatedDecision:
        """Grade keeping the position (wrong hold if the horizon stops it out).

        The managed stop keeps ratcheting through the horizon — a trade
        that breakeven-locked is no longer a -1R wrong hold when it stops
        out, which is exactly the improvement the policy exists to buy.
        """
        managed = trade.managed_stop()
        stop_hit = False
        for candle in horizon:
            if managed.is_breached_by(candle):
                stop_hit = True
                break
            managed.ratchet(candle)
        return EvaluatedDecision(
            scenario_class=ScenarioClass.HOLDING,
            decision="hold",
            confidence=signal.confidence if signal is not None else None,
            reasons=signal.reasons if signal is not None else (),
            entry_price_quote=trade.entry_price_quote,
            stop_hit=stop_hit,
            verdict=Verdict.WRONG_HOLD if stop_hit else Verdict.CORRECT_HOLD,
        )

    def _simulate_long(
        self,
        strategy: Strategy | None,
        horizon: Sequence[Candle],
        entry: Decimal,
        stop: Decimal,
        managed: ManagedStop | None = None,
    ) -> tuple[Decimal, int, bool, Decimal, Decimal]:
        """Walk the horizon holding a unit long.

        Returns (exit price, exit index, stop hit, peak high, trough low).

        Exit causes, pessimistic order inside each candle: gap below stop at
        the open, stop touched intra-candle (filled at the stop, minus
        slippage), the strategy's own exit signal (fills next open), or the
        fixed-time exit at the final close. ``strategy=None`` simulates the
        mechanical reference trade (no strategy exits); ``managed`` runs
        the signal's stop policy — the same ``ManagedStop`` the live engine
        enforces, breach checked before each candle ratchets.
        """
        peak = entry
        trough = entry
        pending_exit = False
        for index, candle in enumerate(horizon):
            current_stop = managed.stop_price_quote if managed is not None else stop
            if pending_exit:
                return self._slipped(candle.open_quote, buying=False), index, False, peak, trough
            if candle.open_quote <= current_stop:
                exit_price = self._slipped(candle.open_quote, buying=False)
                trough = min(trough, candle.open_quote)
                return exit_price, index, True, peak, trough
            if candle.low_quote <= current_stop:
                trough = min(trough, candle.low_quote)
                return self._slipped(current_stop, buying=False), index, True, peak, trough
            if managed is not None:
                managed.ratchet(candle)
            peak = max(peak, candle.high_quote)
            trough = min(trough, candle.low_quote)
            if strategy is not None:
                position = Position(
                    symbol=candle.symbol, quantity_base=Decimal(1), cost_basis_quote=entry
                )
                signal = strategy.on_candle(candle, position)
                if signal is not None and signal.side == Side.SELL:
                    pending_exit = True
        final = horizon[-1]
        return self._slipped(final.close_quote, buying=False), len(horizon) - 1, False, peak, trough

    def _slipped(self, price: Decimal, buying: bool) -> Decimal:
        """Apply adverse market slippage, mirroring the fill simulator."""
        slip = self._fills.market_slippage_bps / _BPS_DIVISOR
        factor = (1 + slip) if buying else (1 - slip)
        return (price * factor).quantize(ACCOUNTING_RESOLUTION, rounding=ROUND_HALF_EVEN)

    def _fee(self, price: Decimal) -> Decimal:
        """Taker fee per unit, mirroring the fill simulator."""
        return (price * self._fills.taker_fee_bps / _BPS_DIVISOR).quantize(
            ACCOUNTING_RESOLUTION, rounding=ROUND_HALF_EVEN
        )


def _ratio(amount: Decimal, risk: Decimal) -> Decimal:
    """Return the R-multiple: money over the initial risk unit, quantized."""
    return (amount / risk).quantize(ACCOUNTING_RESOLUTION, rounding=ROUND_HALF_EVEN)


def _trade_verdict(r_multiple: Decimal) -> Verdict:
    """Map a final R onto the frozen bands of ARCHITECTURE.md section 12.2.

    very bad <= -1R < bad <= -0.25R < neutral < +0.25R <= good < +1.5R <= excellent.
    """
    if r_multiple <= Decimal("-1"):
        return Verdict.VERY_BAD
    if r_multiple <= Decimal("-0.25"):
        return Verdict.BAD
    if r_multiple < Decimal("0.25"):
        return Verdict.NEUTRAL
    if r_multiple < Decimal("1.5"):
        return Verdict.GOOD
    return Verdict.EXCELLENT
