"""Sentiment tighteners: extremes block, staleness disarms, gate stays one-way."""

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import httpx

from tradebot.core.models import CandleInterval, Side, Signal
from tradebot.signals import (
    MarketRegimeDetector,
    MarketSentiment,
    RegimeConfig,
    RegimeGate,
    SentimentConfig,
    SentimentMonitor,
)
from tradebot.signals.sentiment import COINGECKO_GLOBAL_URL, FEAR_GREED_URL

from .test_regime import CONFIG as REGIME_CONFIG
from .test_regime import feed

NOW = datetime(2026, 6, 10, 12, 0, tzinfo=UTC)


class TestRiskOffReason:
    def test_fear_and_greed_extremes_both_block(self) -> None:
        sentiment = MarketSentiment()
        sentiment.record_fear_greed(15, NOW)
        assert "extreme fear" in str(sentiment.risk_off_reason(NOW))

        sentiment.record_fear_greed(95, NOW)
        assert "euphoria" in str(sentiment.risk_off_reason(NOW))

        sentiment.record_fear_greed(50, NOW)
        assert sentiment.risk_off_reason(NOW) is None

    def test_extreme_fear_exempts_mean_reversion_entries_only(self) -> None:
        """Mean-reversion buys fear by design; greed still pauses it."""
        sentiment = MarketSentiment()
        sentiment.record_fear_greed(12, NOW)
        assert sentiment.risk_off_reason(NOW, mean_reversion_entry=True) is None
        assert "extreme fear" in str(sentiment.risk_off_reason(NOW))

        sentiment.record_fear_greed(95, NOW)
        assert "euphoria" in str(sentiment.risk_off_reason(NOW, mean_reversion_entry=True))

    def test_stale_readings_contribute_nothing(self) -> None:
        """Advisory data expires quietly; it must never freeze an opinion."""
        sentiment = MarketSentiment(SentimentConfig(reading_ttl=timedelta(hours=2)))
        sentiment.record_fear_greed(10, NOW)

        assert sentiment.risk_off_reason(NOW + timedelta(hours=1)) is not None
        assert sentiment.risk_off_reason(NOW + timedelta(hours=3)) is None

    def test_crowded_long_funding_blocks_every_family(self) -> None:
        """High positive funding is euphoria-like: no mean-reversion exemption."""
        sentiment = MarketSentiment(SentimentConfig(funding_crowded_long_at_or_above=0.001))
        sentiment.record_funding_rate(0.0012, NOW)
        reason = sentiment.risk_off_reason(NOW)
        assert reason is not None and "crowded longs" in reason
        # Unlike extreme fear, this pauses mean-reversion entries too.
        assert "crowded longs" in str(sentiment.risk_off_reason(NOW, mean_reversion_entry=True))

        # Below the threshold: no opinion.
        calm = MarketSentiment(SentimentConfig(funding_crowded_long_at_or_above=0.001))
        calm.record_funding_rate(0.0003, NOW)
        assert calm.risk_off_reason(NOW) is None

        # A stale reading contributes nothing.
        aged = MarketSentiment(
            SentimentConfig(funding_crowded_long_at_or_above=0.001, reading_ttl=timedelta(hours=2))
        )
        aged.record_funding_rate(0.0012, NOW)
        assert aged.risk_off_reason(NOW + timedelta(hours=3)) is None

    def test_dominance_surge_blocks_and_drift_does_not(self) -> None:
        sentiment = MarketSentiment()
        for hours, percent in enumerate([52.0, 52.5, 53.0, 55.5]):
            sentiment.record_btc_dominance(percent, NOW + timedelta(hours=hours))
        at = NOW + timedelta(hours=3, minutes=30)
        assert "dominance up 3.5 points" in str(sentiment.risk_off_reason(at))

        calm = MarketSentiment()
        for hours, percent in enumerate([52.0, 52.4, 52.9]):
            calm.record_btc_dominance(percent, NOW + timedelta(hours=hours))
        assert calm.risk_off_reason(NOW + timedelta(hours=2, minutes=30)) is None

    def test_broad_negative_news_flow_blocks_inside_the_window(self) -> None:
        sentiment = MarketSentiment()
        for minutes in range(5):
            sentiment.record_negative_news(NOW + timedelta(minutes=minutes))

        soon = sentiment.risk_off_reason(NOW + timedelta(minutes=30))
        assert soon is not None and "broad negative news flow" in soon
        # The same headlines age out of the window.
        assert sentiment.risk_off_reason(NOW + timedelta(hours=3)) is None


class TestGateIntegration:
    def test_sentiment_blocks_an_otherwise_trending_market(self) -> None:
        detector = MarketRegimeDetector("BTC/USDT", REGIME_CONFIG)
        feed(detector, [100.0 + 2 * i for i in range(12)])
        sentiment = MarketSentiment()
        gate = RegimeGate(detector, sentiment)
        signal_time = datetime(2026, 1, 2, 0, 13, tzinfo=UTC)  # within staleness
        signal = Signal(
            signal_id="test:1",
            strategy_name="trend_following",
            symbol="ETH/USDT",
            side=Side.BUY,
            confidence=0.8,
            stop_price_quote=Decimal("95"),
            reasons=(),
            created_at=signal_time,
        )

        assert gate.evaluate(signal).allowed is True  # trending, no sentiment data

        sentiment.record_fear_greed(10, signal_time)
        blocked = gate.evaluate(signal)
        assert blocked.allowed is False
        assert any("extreme fear" in reason for reason in blocked.reasons)

    def test_extreme_fear_lets_mean_reversion_buy_in_a_ranging_market(self) -> None:
        """The family routed into a fearful chop may still buy it."""
        detector = MarketRegimeDetector(
            "BTC/USDT", RegimeConfig(timeframe=CandleInterval.M1, adx_period=3)
        )
        feed(detector, [100.0 + (1.0 if i % 2 == 0 else -1.0) for i in range(20)])
        signal_time = datetime(2026, 1, 2, 0, 21, tzinfo=UTC)
        sentiment = MarketSentiment()
        sentiment.record_fear_greed(12, signal_time)
        gate = RegimeGate(detector, sentiment)
        signal = Signal(
            signal_id="test:3",
            strategy_name="mean_reversion",
            symbol="ETH/USDT",
            side=Side.BUY,
            confidence=0.8,
            stop_price_quote=Decimal("95"),
            reasons=(),
            created_at=signal_time,
        )

        assert gate.evaluate(signal).allowed is True

        trend_signal = signal.model_copy(
            update={"signal_id": "test:4", "strategy_name": "trend_following"}
        )
        # The trend family is no longer gated by a ranging regime, but
        # extreme fear still pauses it: the mean-reversion exemption widens
        # nothing for trend entries, so the fear veto applies here.
        assert gate.evaluate(trend_signal).allowed is False

    def test_sentiment_never_overrides_a_blocking_regime(self) -> None:
        """One-way valve: greed cannot reopen a risk-off market."""
        detector = MarketRegimeDetector("BTC/USDT", REGIME_CONFIG)
        # A strong climb then a fast slide >20% below the peak: risk-off.
        closes = [100.0 + 5 * i for i in range(10)] + [145.0 - 12 * i for i in range(1, 6)]
        feed(detector, closes)
        signal_time = datetime(2026, 1, 2, 0, 0, tzinfo=UTC) + timedelta(minutes=len(closes))
        sentiment = MarketSentiment()
        sentiment.record_fear_greed(50, signal_time)  # neutral — no veto, no loosening
        gate = RegimeGate(detector, sentiment)
        signal = Signal(
            signal_id="test:2",
            strategy_name="trend_following",
            symbol="ETH/USDT",
            side=Side.BUY,
            confidence=0.8,
            stop_price_quote=Decimal("95"),
            reasons=(),
            created_at=signal_time,
        )
        assert gate.evaluate(signal).allowed is False  # still risk-off


class TestSentimentMonitor:
    async def test_poll_records_both_sources_and_survives_failures(self) -> None:
        calls: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(str(request.url))
            if "alternative.me" in str(request.url):
                return httpx.Response(
                    200, text=json.dumps({"data": [{"value": "12", "value_classification": ""}]})
                )
            return httpx.Response(
                200, text=json.dumps({"data": {"market_cap_percentage": {"btc": 56.4}}})
            )

        sentiment = MarketSentiment()
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            await SentimentMonitor(sentiment, client).poll_once()

        assert any(FEAR_GREED_URL in call for call in calls)
        assert any(COINGECKO_GLOBAL_URL in call for call in calls)
        now = datetime.now(tz=UTC)
        assert "extreme fear" in str(sentiment.risk_off_reason(now))

    async def test_one_dead_source_does_not_lose_the_other(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if "alternative.me" in str(request.url):
                return httpx.Response(500)
            return httpx.Response(
                200, text=json.dumps({"data": {"market_cap_percentage": {"btc": 56.4}}})
            )

        sentiment = MarketSentiment()
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            await SentimentMonitor(sentiment, client).poll_once()  # must not raise

        # Dominance reading landed despite the F&G failure.
        assert sentiment._dominance
