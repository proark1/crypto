"""Risk management: the only component that turns signals into orders.

Phase 1 ships sizing and the basic caps; circuit breakers, cooldowns, and the
kill switch land with paper trading (ARCHITECTURE.md 4.3 has the full list).
"""

from tradebot.risk.manager import RiskConfig, RiskManager

__all__ = ["RiskConfig", "RiskManager"]
