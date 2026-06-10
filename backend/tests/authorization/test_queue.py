from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from tradebot.authorization import ProposalQueue
from tradebot.core.models import ProposalStatus, Side, Signal

NOW = datetime(2026, 1, 2, 12, 0, tzinfo=UTC)


def make_signal(signal_id: str = "sig-1", symbol: str = "BTC/USDT") -> Signal:
    return Signal(
        signal_id=signal_id,
        strategy_name="trend_following",
        symbol=symbol,
        side=Side.BUY,
        confidence=1.0,
        stop_price_quote=Decimal("95"),
        reasons=("fast EMA crossed above slow EMA",),
        created_at=NOW,
    )


def make_queue(ttl_minutes: int = 15, drift: str = "0.01") -> ProposalQueue:
    return ProposalQueue(ttl=timedelta(minutes=ttl_minutes), max_drift_fraction=Decimal(drift))


class TestLifecycle:
    def test_create_and_list_pending(self) -> None:
        queue = make_queue()
        proposal = queue.create(make_signal(), Decimal("100"), NOW)

        assert proposal is not None
        assert proposal.status == ProposalStatus.PENDING
        assert proposal.expires_at == NOW + timedelta(minutes=15)
        assert queue.pending() == (proposal,)

    def test_one_pending_proposal_per_symbol(self) -> None:
        queue = make_queue()
        assert queue.create(make_signal("sig-1"), Decimal("100"), NOW) is not None
        assert queue.create(make_signal("sig-2"), Decimal("101"), NOW) is None  # same symbol
        assert queue.create(make_signal("sig-3", "ETH/USDT"), Decimal("50"), NOW) is not None

    def test_approve_returns_approved_copy_and_clears_queue(self) -> None:
        queue = make_queue()
        queue.create(make_signal(), Decimal("100"), NOW)

        approved = queue.approve("sig-1", NOW + timedelta(minutes=1), Decimal("100.5"))
        assert approved.status == ProposalStatus.APPROVED
        assert queue.pending() == ()

    def test_reject_clears_queue(self) -> None:
        queue = make_queue()
        queue.create(make_signal(), Decimal("100"), NOW)
        rejected = queue.reject("sig-1")
        assert rejected.status == ProposalStatus.REJECTED
        assert queue.pending() == ()

    def test_unknown_ids_raise(self) -> None:
        queue = make_queue()
        with pytest.raises(KeyError):
            queue.approve("ghost", NOW, Decimal("100"))
        with pytest.raises(KeyError):
            queue.reject("ghost")


class TestStaleness:
    def test_expired_approval_is_refused(self) -> None:
        queue = make_queue(ttl_minutes=15)
        queue.create(make_signal(), Decimal("100"), NOW)
        with pytest.raises(ValueError, match="expired"):
            queue.approve("sig-1", NOW + timedelta(minutes=16), Decimal("100"))
        assert queue.pending() == ()  # consumed either way

    def test_drifted_approval_is_refused_in_both_directions(self) -> None:
        for drifted_price in (Decimal("101.5"), Decimal("98.5")):  # > 1% away
            queue = make_queue(drift="0.01")
            queue.create(make_signal(), Decimal("100"), NOW)
            with pytest.raises(ValueError, match="drifted"):
                queue.approve("sig-1", NOW + timedelta(minutes=1), drifted_price)

    def test_drift_within_tolerance_is_accepted(self) -> None:
        queue = make_queue(drift="0.01")
        queue.create(make_signal(), Decimal("100"), NOW)
        approved = queue.approve("sig-1", NOW + timedelta(minutes=1), Decimal("100.9"))
        assert approved.status == ProposalStatus.APPROVED

    def test_sweep_expires_and_drift_cancels(self) -> None:
        queue = make_queue(ttl_minutes=15, drift="0.01")
        queue.create(make_signal("sig-1", "BTC/USDT"), Decimal("100"), NOW)
        queue.create(make_signal("sig-2", "ETH/USDT"), Decimal("100"), NOW + timedelta(minutes=10))

        # At NOW+16: sig-1 expired; sig-2 still fresh but price drifted 5%.
        removed = queue.sweep(NOW + timedelta(minutes=16), Decimal("105"))
        statuses = {p.signal.signal_id: p.status for p in removed}
        assert statuses == {
            "sig-1": ProposalStatus.EXPIRED,
            "sig-2": ProposalStatus.DRIFTED,
        }
        assert queue.pending() == ()

    def test_sweep_keeps_fresh_proposals(self) -> None:
        queue = make_queue()
        queue.create(make_signal(), Decimal("100"), NOW)
        assert queue.sweep(NOW + timedelta(minutes=1), Decimal("100.2")) == []
        assert len(queue.pending()) == 1
