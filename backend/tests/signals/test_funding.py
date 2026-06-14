"""The funding poller feeds positioning readings best-effort; failures cost only freshness."""

from datetime import UTC, datetime

from tradebot.signals import FundingMonitor, MarketSentiment, SentimentConfig


class TestFundingMonitor:
    async def test_poll_records_a_crowded_reading_that_then_blocks(self) -> None:
        sentiment = MarketSentiment(SentimentConfig(funding_crowded_long_at_or_above=0.001))

        async def fetch(symbol: str) -> float | None:
            assert symbol == "BTC/USDT:USDT"
            return 0.0015

        await FundingMonitor(sentiment, fetch, "BTC/USDT:USDT").poll_once()

        now = datetime.now(tz=UTC)
        assert "crowded longs" in str(sentiment.risk_off_reason(now))

    async def test_a_fetch_failure_is_skipped_not_raised(self) -> None:
        sentiment = MarketSentiment()

        async def fetch(symbol: str) -> float | None:
            raise RuntimeError("venue exposes no funding for this symbol")

        # A best-effort poll must never crash the worker.
        await FundingMonitor(sentiment, fetch, "BTC/USDT").poll_once()
        assert sentiment._funding is None

    async def test_a_none_reading_records_nothing(self) -> None:
        sentiment = MarketSentiment()

        async def fetch(symbol: str) -> float | None:
            return None

        await FundingMonitor(sentiment, fetch, "BTC/USDT").poll_once()
        assert sentiment._funding is None
