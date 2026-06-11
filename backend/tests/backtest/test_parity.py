"""Divergence metric: fill streams compared by (side, fill time)."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from tradebot.backtest import compare_fills
from tradebot.core.models import Fill, Side

WINDOW_START = datetime(2026, 1, 2, 0, 0, tzinfo=UTC)
WINDOW_END = WINDOW_START + timedelta(hours=24)


def make_fill(side: Side, minute: int, price: str = "100") -> Fill:
    return Fill(
        client_order_id=f"ord-{side}-{minute}",
        symbol="BTC/USDT",
        side=side,
        price_quote=Decimal(price),
        quantity_base=Decimal("1"),
        fee_quote=Decimal("0.1"),
        filled_at=WINDOW_START + timedelta(minutes=minute),
    )


class TestCompareFills:
    def test_identical_streams_diverge_by_zero(self) -> None:
        fills = [make_fill(Side.BUY, 1), make_fill(Side.SELL, 30)]
        # Prices/quantities differ on purpose: sizing drift is a consequence
        # of divergence, never independent evidence of it.
        replay = [make_fill(Side.BUY, 1, price="101"), make_fill(Side.SELL, 30, price="99")]

        report = compare_fills(fills, replay, WINDOW_START, WINDOW_END)
        assert report.divergence_fraction == 0.0
        assert report.matched_count == 2
        assert report.mismatches == ()

    def test_one_sided_fills_are_reported_both_ways(self) -> None:
        live = [make_fill(Side.BUY, 1), make_fill(Side.SELL, 30)]
        replay = [make_fill(Side.BUY, 1), make_fill(Side.SELL, 45)]

        report = compare_fills(live, replay, WINDOW_START, WINDOW_END)
        assert report.matched_count == 1
        assert report.divergence_fraction == 0.5  # 2 of 4 fills unmatched
        assert any(m.startswith("live only: sell") for m in report.mismatches)
        assert any(m.startswith("replay only: sell") for m in report.mismatches)

    def test_empty_windows_diverge_by_zero(self) -> None:
        report = compare_fills([], [], WINDOW_START, WINDOW_END)
        assert report.divergence_fraction == 0.0
        assert report.live_fill_count == 0 and report.replay_fill_count == 0

    def test_total_mismatch_is_one(self) -> None:
        report = compare_fills(
            [make_fill(Side.BUY, 1)], [make_fill(Side.BUY, 2)], WINDOW_START, WINDOW_END
        )
        assert report.divergence_fraction == 1.0
