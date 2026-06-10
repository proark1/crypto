"""Regime detector and gate: trend passes, chop and crashes block, stale blocks."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from tradebot.core.models import Candle, CandleInterval, Side, Signal
from tradebot.signals import (
    RANGING,
    RISK_OFF,
    TRENDING,
    WARMING_UP,
    MarketRegimeDetector,
    RegimeConfig,
    RegimeGate,
)

BASE_TIME = datetime(2026, 1, 2, 0, 0, tzinfo=UTC)

# 1m classification timeframe so each candle is one bucket; small ADX period
# so tests stay readable. Thresholds are the production defaults.
CONFIG = RegimeConfig(
    timeframe=CandleInterval.M1,
    adx_period=3,
    drawdown_window_candles=50,
)


def make_candle(index: int, close: float, spread: float = 0.5) -> Candle:
    open_time = BASE_TIME + timedelta(minutes=index)
    price = Decimal(str(close))
    return Candle(
        symbol="BTC/USDT",
        interval=CandleInterval.M1,
        open_time=open_time,
        close_time=open_time + timedelta(minutes=1),
        open_quote=price,
        high_quote=price + Decimal(str(spread)),
        low_quote=max(Decimal("0.01"), price - Decimal(str(spread))),
        close_quote=price,
        volume_base=Decimal("1"),
    )


def feed(detector: MarketRegimeDetector, closes: list[float]) -> None:
    for index, close in enumerate(closes):
        detector.update(make_candle(index, close))


def make_signal(minute: int) -> Signal:
    created = BASE_TIME + timedelta(minutes=minute)
    return Signal(
        signal_id=f"test:{minute}",
        strategy_name="trend_following",
        symbol="ETH/USDT",
        side=Side.BUY,
        confidence=0.8,
        stop_price_quote=Decimal("95"),
        reasons=("fast EMA crossed above slow EMA",),
        created_at=created,
    )


class TestDetector:
    def test_starts_warming_up_and_blocks_entries(self) -> None:
        detector = MarketRegimeDetector("BTC/USDT", CONFIG)
        feed(detector, [100.0, 101.0])  # far short of the ADX lookback

        assert detector.regime.label == WARMING_UP
        verdict = RegimeGate(detector).evaluate(make_signal(3))
        assert verdict.allowed is False
        assert any("warming up" in reason for reason in verdict.reasons)

    def test_steady_climb_is_trending_and_allows_entries(self) -> None:
        detector = MarketRegimeDetector("BTC/USDT", CONFIG)
        feed(detector, [100.0 + 2 * i for i in range(12)])

        assert detector.regime.label == TRENDING
        verdict = RegimeGate(detector).evaluate(make_signal(12))
        assert verdict.allowed is True
        assert any("ADX" in reason for reason in verdict.reasons)

    def test_chop_is_ranging_and_blocks_trend_entries(self) -> None:
        detector = MarketRegimeDetector("BTC/USDT", CONFIG)
        feed(detector, [100.0 + (1.0 if i % 2 == 0 else -1.0) for i in range(20)])

        assert detector.regime.label == RANGING
        verdict = RegimeGate(detector).evaluate(make_signal(20))
        assert verdict.allowed is False
        assert any("ranging" in reason for reason in verdict.reasons)

    def test_crash_below_the_peak_is_risk_off_even_while_trending(self) -> None:
        detector = MarketRegimeDetector("BTC/USDT", CONFIG)
        # A strong climb (high ADX) and then a fast slide >20% below the peak:
        # the downtrend itself trends, but risk-off must win.
        closes = [100.0 + 5 * i for i in range(10)] + [145.0 - 12 * i for i in range(1, 6)]
        feed(detector, closes)

        assert detector.regime.label == RISK_OFF
        verdict = RegimeGate(detector).evaluate(make_signal(len(closes)))
        assert verdict.allowed is False
        assert any("risk-off" in reason for reason in verdict.reasons)

    def test_replayed_and_foreign_candles_are_ignored(self) -> None:
        detector = MarketRegimeDetector("BTC/USDT", CONFIG)
        feed(detector, [100.0 + 2 * i for i in range(12)])
        before = detector.regime

        detector.update(make_candle(5, 1.0))  # bus replay after a reconnect
        foreign = make_candle(12, 1.0).model_copy(update={"symbol": "ETH/USDT"})
        detector.update(foreign)

        assert detector.regime == before

    def test_prime_equals_live_updates(self) -> None:
        closes = [100.0 + 2 * i for i in range(12)]
        live = MarketRegimeDetector("BTC/USDT", CONFIG)
        feed(live, closes)
        primed = MarketRegimeDetector("BTC/USDT", CONFIG)
        primed.prime([make_candle(index, close) for index, close in enumerate(closes)])

        assert primed.regime == live.regime

    def test_required_m1_candles_covers_warmup_and_drawdown_window(self) -> None:
        hourly = RegimeConfig()  # production defaults: 1h, ADX 14, window 240
        # 241 hourly buckets dominate the ADX warm-up; each is 60 minutes.
        assert hourly.required_m1_candles() == 241 * 60


class TestStaleness:
    def test_old_assessment_blocks_until_data_resumes(self) -> None:
        detector = MarketRegimeDetector("BTC/USDT", CONFIG)
        feed(detector, [100.0 + 2 * i for i in range(12)])
        assert detector.regime.label == TRENDING

        gate = RegimeGate(detector)
        fresh = gate.evaluate(make_signal(13))
        # stale_after_buckets=2 on a 1m timeframe: three minutes later the
        # assessment no longer describes the market the signal sees.
        stale = gate.evaluate(make_signal(16))

        assert fresh.allowed is True
        assert stale.allowed is False
        assert any("wait until the feed resumes" in reason for reason in stale.reasons)
