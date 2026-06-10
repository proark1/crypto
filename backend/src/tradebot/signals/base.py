"""The entry-gate contract shared by every signal filter (ARCHITECTURE.md 5.2).

A gate can only *block* an entry, never create or enlarge one — that keeps
the system testable and prevents filter soup. Exits are never gated:
protective actions execute autonomously in every mode (ARCHITECTURE.md 4.8),
so the trading engine consults gates for entries only.
"""

from __future__ import annotations

from typing import Protocol

from pydantic import BaseModel, ConfigDict

from tradebot.core.models import Signal


class GateDecision(BaseModel):
    """One gate's verdict on one entry signal, with its reasons verbatim."""

    model_config = ConfigDict(frozen=True)

    allowed: bool
    reasons: tuple[str, ...] = ()


class EntryGate(Protocol):
    """Anything that may veto an entry before risk sizing."""

    def evaluate(self, signal: Signal) -> GateDecision:
        """Return whether ``signal`` may proceed, and why."""
        ...
