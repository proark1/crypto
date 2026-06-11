"""DataHealthGate tests: entries pause on a degraded feed, with the reason."""

from decimal import Decimal

from tradebot.core.models import Side, Signal
from tradebot.signals import DataHealthGate


class FakeFeed:
    """Stands in for a market-data feed's health latch."""

    def __init__(self, healthy: bool, reason: str | None) -> None:
        self._healthy = healthy
        self._reason = reason

    @property
    def healthy(self) -> bool:
        return self._healthy

    @property
    def health_reason(self) -> str | None:
        return self._reason


def buy_signal() -> Signal:
    return Signal(
        strategy_name="trend_following",
        symbol="BTC/USDT",
        side=Side.BUY,
        confidence=1.0,
        stop_price_quote=Decimal("90"),
    )


def test_healthy_feed_allows_the_entry() -> None:
    gate = DataHealthGate(FakeFeed(healthy=True, reason=None))
    verdict = gate.evaluate(buy_signal())
    assert verdict.allowed is True
    assert verdict.reasons == ()


def test_degraded_feed_blocks_with_its_reason_verbatim() -> None:
    gate = DataHealthGate(FakeFeed(healthy=False, reason="backfill failed: ConnectionError"))
    verdict = gate.evaluate(buy_signal())
    assert verdict.allowed is False
    assert verdict.reasons == ("data health: backfill failed: ConnectionError",)


def test_degraded_feed_without_a_reason_still_blocks_with_a_default() -> None:
    gate = DataHealthGate(FakeFeed(healthy=False, reason=None))
    verdict = gate.evaluate(buy_signal())
    assert verdict.allowed is False
    assert verdict.reasons == ("data health: market data is degraded",)
