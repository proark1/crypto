"""Autonomy modes and the co-pilot proposal queue (ARCHITECTURE.md 4.8).

In co-pilot mode, entry signals become pending proposals the user must
approve. Protective actions — exits, stops, the kill switch — never wait
for approval: a human queue in front of capital protection is itself a risk.
"""

from tradebot.authorization.queue import ProposalQueue

__all__ = ["ProposalQueue"]
