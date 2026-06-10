"""The pending-proposal queue for co-pilot mode.

Safety properties enforced here:

- proposals **expire** after a TTL — an unanswered proposal never lingers;
- approval is refused once price **drifts** beyond a configured fraction of
  the proposal price — a stale approval can never fill far from the price
  the user actually looked at;
- one pending proposal per symbol — duplicates would invite double entries.

In-memory by design: a restart drops pending proposals, which is the safe
direction (they would only have expired), and live state is rebuilt from
fresh signals.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal

from tradebot.core.models import Proposal, ProposalStatus, Signal


class ProposalQueue:
    """Holds pending proposals and rules on their lifecycle."""

    def __init__(self, ttl: timedelta, max_drift_fraction: Decimal) -> None:
        """Configure expiry and the acceptable price drift (e.g. 0.01 = 1%)."""
        if ttl <= timedelta(0):
            raise ValueError("proposal TTL must be positive")
        if max_drift_fraction <= 0:
            raise ValueError("max drift fraction must be positive")
        self._ttl = ttl
        self._max_drift = max_drift_fraction
        self._pending: dict[str, Proposal] = {}

    def pending(self) -> tuple[Proposal, ...]:
        """Return pending proposals, oldest first."""
        return tuple(self._pending.values())

    def create(self, signal: Signal, price_quote: Decimal, now: datetime) -> Proposal | None:
        """Queue a proposal for ``signal``; ``None`` if one is already pending.

        One pending proposal per symbol: a strategy re-signalling before the
        user answers must not stack entries.
        """
        if any(p.signal.symbol == signal.symbol for p in self._pending.values()):
            return None
        proposal = Proposal(
            signal=signal,
            proposal_price_quote=price_quote,
            created_at=now,
            expires_at=now + self._ttl,
        )
        self._pending[signal.signal_id] = proposal
        return proposal

    def sweep(self, now: datetime, current_price_quote: Decimal) -> list[Proposal]:
        """Expire and drift-cancel stale proposals; returns what was removed."""
        removed: list[Proposal] = []
        for signal_id, proposal in list(self._pending.items()):
            if now >= proposal.expires_at:
                removed.append(proposal.model_copy(update={"status": ProposalStatus.EXPIRED}))
                del self._pending[signal_id]
            elif self._has_drifted(proposal, current_price_quote):
                removed.append(proposal.model_copy(update={"status": ProposalStatus.DRIFTED}))
                del self._pending[signal_id]
        return removed

    def approve(self, signal_id: str, now: datetime, current_price_quote: Decimal) -> Proposal:
        """Take a pending proposal for execution; re-validates at approval time.

        Raises ``KeyError`` for unknown ids and ``ValueError`` when the
        proposal expired or price drifted between proposal and approval —
        the user's yes was given to a market that no longer exists.
        """
        proposal = self._pending.get(signal_id)
        if proposal is None:
            raise KeyError(f"no pending proposal {signal_id!r}")
        del self._pending[signal_id]
        if now >= proposal.expires_at:
            raise ValueError(f"proposal {signal_id!r} expired at {proposal.expires_at.isoformat()}")
        if self._has_drifted(proposal, current_price_quote):
            raise ValueError(
                f"proposal {signal_id!r} cancelled: price drifted from "
                f"{proposal.proposal_price_quote} to {current_price_quote}"
            )
        return proposal.model_copy(update={"status": ProposalStatus.APPROVED})

    def reject(self, signal_id: str) -> Proposal:
        """Reject a pending proposal; raises ``KeyError`` if unknown."""
        proposal = self._pending.pop(signal_id, None)
        if proposal is None:
            raise KeyError(f"no pending proposal {signal_id!r}")
        return proposal.model_copy(update={"status": ProposalStatus.REJECTED})

    def _has_drifted(self, proposal: Proposal, current_price_quote: Decimal) -> bool:
        drift = abs(current_price_quote - proposal.proposal_price_quote)
        return drift / proposal.proposal_price_quote > self._max_drift
