"""Market-wide regime detection and the regime entry gate (ARCHITECTURE.md 5.2).

The reference market (BTC by convention) is classified on a coarse
timeframe into TRENDING / RANGING / RISK_OFF, and the gate decides whether
*any* coin may open a new position: trend-following entries are allowed
only while the reference market trends, and nothing opens during risk-off.
Exits are never gated.

This first increment classifies from price alone (ADX for trend strength,
drawdown from the recent peak for risk-off). The Fear & Greed and BTC
dominance inputs named in section 5.2 join when their data sources are
ingested (section 5.1, P1) — they can only make the gate stricter, so
shipping without them fails toward trading, not toward blowing up, and the
ADX core stays unchanged.

Like everything in ``signals/``, this module never knows whether it runs
in backtest, paper, or live: it consumes candles and answers questions.
"""

from __future__ import annotations

import logging
from collections import deque
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from tradebot.core.events import CandleClosed, EventBus
from tradebot.core.models import Candle, CandleInterval, Signal
from tradebot.indicators import Adx
from tradebot.marketdata import TimeframeAggregator
from tradebot.signals.base import GateDecision
from tradebot.signals.sentiment import MarketSentiment

logger = logging.getLogger(__name__)


class RegimeConfig(BaseModel):
    """Constants of the regime classification; part of any config snapshot."""

    model_config = ConfigDict(frozen=True)

    timeframe: CandleInterval = CandleInterval.H1
    """Classification timeframe. 1m is too noisy for ADX to mean anything."""

    adx_period: int = Field(default=14, ge=2)
    adx_trend_threshold: float = 25.0
    """The conventional ADX trend cut-off: below it the market is ranging."""

    risk_off_drawdown_fraction: float = Field(default=0.20, gt=0.0, lt=1.0)
    """Close this far below the recent peak means risk-off: no new entries
    anywhere while the reference market is in a drawdown of this size."""

    drawdown_window_candles: int = Field(default=240, ge=2)
    """How far back the peak is tracked (240 1h candles = 10 days)."""

    stale_after_buckets: int = Field(default=2, ge=1)
    """An assessment older than this many timeframe buckets no longer
    describes the current market; the gate blocks until data resumes."""

    def required_m1_candles(self) -> int:
        """1m candles needed to warm up fully (priming fetch size)."""
        buckets = max(2 * self.adx_period + 2, self.drawdown_window_candles + 1)
        return buckets * int(self.timeframe.duration.total_seconds() // 60)


class Regime(BaseModel):
    """One classification of the reference market, with its evidence."""

    model_config = ConfigDict(frozen=True)

    label: str  # "warming_up" | "trending" | "ranging" | "risk_off"
    reasons: tuple[str, ...]
    as_of: datetime | None = None
    """Close time of the bucket this was computed from; ``None`` pre-data."""


WARMING_UP = "warming_up"
TRENDING = "trending"
RANGING = "ranging"
RISK_OFF = "risk_off"

REVERSION_STRATEGY_NAMES = frozenset({"mean_reversion"})
"""Strategy names belonging to the mean-reversion family; everything else
is treated as trend family for routing purposes."""


class RegimeClassifier:
    """Pure per-bucket regime classification: ADX strength + drawdown risk-off.

    Consumes already-bucketed candles (one per classification timeframe)
    and keeps the latest :class:`Regime`. Split out of the detector so the
    evaluation system can classify a scenario's own candle stream with
    exactly the production thresholds — one classification, two consumers.
    """

    def __init__(self, symbol: str, config: RegimeConfig | None = None) -> None:
        """``symbol`` only labels the reasons; candles arrive pre-filtered."""
        self.symbol = symbol
        self._config = config or RegimeConfig()
        self._adx = Adx(self._config.adx_period)
        self._closes: deque[float] = deque(maxlen=self._config.drawdown_window_candles)
        self._regime = Regime(
            label=WARMING_UP,
            reasons=(f"regime warming up: no {symbol} candles seen yet",),
        )

    @property
    def config(self) -> RegimeConfig:
        """The frozen classification constants."""
        return self._config

    @property
    def regime(self) -> Regime:
        """The latest classification (``warming_up`` until ADX is formed)."""
        return self._regime

    def classify(self, bucket: Candle) -> Regime:
        """Consume one bucket candle and return the updated regime.

        The bucket's own interval labels the evidence, so the reasons stay
        truthful whatever timeframe the caller classifies on.
        """
        close = float(bucket.close_quote)
        adx = self._adx.update(float(bucket.high_quote), float(bucket.low_quote), close)
        self._closes.append(close)
        timeframe = bucket.interval.value

        if adx is None:
            self._regime = Regime(
                label=WARMING_UP,
                reasons=(
                    f"regime warming up: ADX({self._config.adx_period}) on "
                    f"{self.symbol} {timeframe} is not formed yet",
                ),
                as_of=bucket.close_time,
            )
            return self._regime
        peak = max(self._closes)
        drawdown = (peak - close) / peak if peak > 0 else 0.0
        if drawdown >= self._config.risk_off_drawdown_fraction:
            self._regime = Regime(
                label=RISK_OFF,
                reasons=(
                    f"risk-off: {self.symbol} closed {drawdown:.1%} below its "
                    f"{len(self._closes)}-bucket peak",
                ),
                as_of=bucket.close_time,
            )
        elif adx >= self._config.adx_trend_threshold:
            self._regime = Regime(
                label=TRENDING,
                reasons=(
                    f"trending: ADX({self._config.adx_period}) {adx:.1f} >= "
                    f"{self._config.adx_trend_threshold:g} on {self.symbol} {timeframe}",
                ),
                as_of=bucket.close_time,
            )
        else:
            self._regime = Regime(
                label=RANGING,
                reasons=(
                    f"ranging: ADX({self._config.adx_period}) {adx:.1f} < "
                    f"{self._config.adx_trend_threshold:g} on {self.symbol} {timeframe}",
                ),
                as_of=bucket.close_time,
            )
        return self._regime


class MarketRegimeDetector:
    """Classifies one reference symbol's regime from its 1m candle stream.

    Candles arrive via :meth:`update` (the worker subscribes it to the
    bus) or :meth:`prime` (stored history at startup, so the gate does not
    spend its first day warming up). Duplicate or older candles — bus
    replays after a reconnect — are dropped silently; only genuinely new
    time advances the state.
    """

    def __init__(self, symbol: str, config: RegimeConfig | None = None) -> None:
        """Track ``symbol`` (the market-wide reference, BTC by convention)."""
        self.symbol = symbol
        self._classifier = RegimeClassifier(symbol, config)
        self._aggregator = (
            None
            if self._classifier.config.timeframe == CandleInterval.M1
            else TimeframeAggregator(self._classifier.config.timeframe)
        )
        self._last_m1_open_time: datetime | None = None

    @property
    def config(self) -> RegimeConfig:
        """The frozen classification constants."""
        return self._classifier.config

    @property
    def regime(self) -> Regime:
        """The latest classification (``warming_up`` until ADX is formed)."""
        return self._classifier.regime

    def prime(self, candles: list[Candle]) -> None:
        """Warm up from stored 1m history (chronological order)."""
        for candle in candles:
            self.update(candle)

    def attach_to(self, bus: EventBus) -> None:
        """Follow the reference symbol's live candles on ``bus``."""
        bus.subscribe(CandleClosed, self._on_candle_event)

    async def _on_candle_event(self, event: CandleClosed) -> None:
        self.update(event.candle)

    def update(self, candle: Candle) -> None:
        """Consume one closed 1m candle of the reference symbol."""
        if candle.symbol != self.symbol or candle.interval != CandleInterval.M1:
            return
        if self._last_m1_open_time is not None and candle.open_time <= self._last_m1_open_time:
            return  # bus replay after a reconnect; already counted
        self._last_m1_open_time = candle.open_time
        if self._aggregator is None:
            self._classifier.classify(candle)
            return
        completed = self._aggregator.add(candle)
        if completed is not None:
            self._classifier.classify(completed)


class RegimeGate:
    """Entry gate over the reference market's regime (pipeline step 1, §5.2).

    The regime routes strategy families: trend entries pass only while the
    reference market trends, mean-reversion entries only while it ranges.
    Risk-off, warm-up, and stale data block every family — when the gate
    cannot see the market, it fails toward not trading.
    """

    def __init__(
        self, detector: MarketRegimeDetector, sentiment: MarketSentiment | None = None
    ) -> None:
        """Gate entries on ``detector``; ``sentiment`` can only tighten.

        Sentiment (Fear & Greed extremes, BTC dominance surges, broad
        negative news flow — §5.2 step 1's remaining inputs) is consulted
        only when the price regime would allow the entry: advisory data
        can veto, never approve, so a dead sentiment feed costs nothing.
        """
        self._detector = detector
        self._sentiment = sentiment

    def evaluate(self, signal: Signal) -> GateDecision:
        """Allow or block one entry signal; reasons are journaled verbatim.

        Staleness is judged against the signal's own clock (its candle
        time), never the wall clock — one code path across backtest,
        paper, and live.
        """
        regime = self._detector.regime
        stale_after = (
            self._detector.config.timeframe.duration * self._detector.config.stale_after_buckets
        )
        if regime.as_of is not None and signal.created_at - regime.as_of > stale_after:
            return GateDecision(
                allowed=False,
                reasons=(
                    f"regime gate: no {self._detector.symbol} data since "
                    f"{regime.as_of.isoformat()}; entries wait until the feed resumes",
                ),
            )
        if regime.label == WARMING_UP:
            return GateDecision(
                allowed=False,
                reasons=("regime gate: " + regime.reasons[0],),
            )
        if regime.label == RISK_OFF:
            return GateDecision(
                allowed=False,
                reasons=("regime gate: entries disabled — " + regime.reasons[0],),
            )
        # Family routing (§5.2): the regime decides which strategy family
        # may enter. Unknown strategy names are treated as trend family —
        # the conservative default for the family that exists today.
        is_reversion = signal.strategy_name in REVERSION_STRATEGY_NAMES
        if regime.label == TRENDING and is_reversion:
            return GateDecision(
                allowed=False,
                reasons=("regime gate: mean-reversion entries disabled — " + regime.reasons[0],),
            )
        if regime.label == RANGING and not is_reversion:
            return GateDecision(
                allowed=False,
                reasons=("regime gate: trend entries disabled — " + regime.reasons[0],),
            )
        if self._sentiment is not None:
            sentiment_reason = self._sentiment.risk_off_reason(signal.created_at)
            if sentiment_reason is not None:
                return GateDecision(
                    allowed=False,
                    reasons=(f"regime gate: entries disabled — {sentiment_reason}",),
                )
        return GateDecision(allowed=True, reasons=regime.reasons)
