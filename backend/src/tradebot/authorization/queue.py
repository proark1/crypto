"""The pending-proposal queue for co-pilot mode.

Safety properties enforced here:

- proposals **expire** after a TTL — an unanswered proposal never lingers;
- approval is refused once price **drifts** beyond a configured fraction of
  the proposal price — a stale approval can never fill far from the price
  the user actually looked at;
- one pending proposal per symbol — duplicates would invite double entries;
- recently **resolved** proposals are remembered (bounded), so acting on one
  that expired or drifted moments ago yields a truthful "already expired"
  instead of a misleading "never existed".

In-memory by design: a restart drops proposal state, which is the safe
direction (pending proposals would only have expired), and live state is
rebuilt from fresh signals.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
from typing import NoReturn

from tradebot.core.models import Proposal, ProposalStatus, Signal

_RESOLVED_HISTORY_LIMIT = 100


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
        self._history: dict[str, Proposal] = {}

    def pending(self) -> tuple[Proposal, ...]:
        """Return pending proposals, oldest first."""
        return tuple(self._pending.values())

    def status_of(self, signal_id: str) -> ProposalStatus | None:
        """Return the proposal's status (pending or resolved), ``None`` if unknown."""
        if signal_id in self._pending:
            return ProposalStatus.PENDING
        resolved = self._history.get(signal_id)
        return None if resolved is None else resolved.status

    def get(self, signal_id: str) -> Proposal | None:
        """Return the proposal, pending or recently resolved."""
        return self._pending.get(signal_id) or self._history.get(signal_id)

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
                removed.append(self._resolve(proposal, ProposalStatus.EXPIRED))
                del self._pending[signal_id]
            elif self._has_drifted(proposal, current_price_quote):
                removed.append(self._resolve(proposal, ProposalStatus.DRIFTED))
                del self._pending[signal_id]
        return removed

    def approve(self, signal_id: str, now: datetime, current_price_quote: Decimal) -> Proposal:
        """Take a pending proposal for execution; re-validates at approval time.

        Raises ``KeyError`` for never-seen ids and ``ValueError`` when the
        proposal already resolved, expired, or drifted — the user's yes was
        given to a market that no longer exists.
        """
        proposal = self._pending.get(signal_id)
        if proposal is None:
            self._raise_unknown_or_resolved(signal_id)
        del self._pending[signal_id]
        if now >= proposal.expires_at:
            self._resolve(proposal, ProposalStatus.EXPIRED)
            raise ValueError(f"proposal {signal_id!r} expired at {proposal.expires_at.isoformat()}")
        if self._has_drifted(proposal, current_price_quote):
            self._resolve(proposal, ProposalStatus.DRIFTED)
            raise ValueError(
                f"proposal {signal_id!r} cancelled: price drifted from "
                f"{proposal.proposal_price_quote} to {current_price_quote}"
            )
        return self._resolve(proposal, ProposalStatus.APPROVED)

    def reject(self, signal_id: str) -> Proposal:
        """Reject a pending proposal.

        Raises ``KeyError`` for never-seen ids, ``ValueError`` for already
        resolved ones.
        """
        proposal = self._pending.pop(signal_id, None)
        if proposal is None:
            self._raise_unknown_or_resolved(signal_id)
        return self._resolve(proposal, ProposalStatus.REJECTED)

    def _raise_unknown_or_resolved(self, signal_id: str) -> NoReturn:
        resolved = self._history.get(signal_id)
        if resolved is not None:
            raise ValueError(f"proposal {signal_id!r} is already {resolved.status.value}")
        raise KeyError(f"no pending proposal {signal_id!r}")

    def _resolve(self, proposal: Proposal, status: ProposalStatus) -> Proposal:
        resolved = proposal.model_copy(update={"status": status})
        self._history[proposal.signal.signal_id] = resolved
        while len(self._history) > _RESOLVED_HISTORY_LIMIT:
            self._history.pop(next(iter(self._history)))
        return resolved

    def _has_drifted(self, proposal: Proposal, current_price_quote: Decimal) -> bool:
        drift = abs(current_price_quote - proposal.proposal_price_quote)
        return drift / proposal.proposal_price_quote > self._max_drift
