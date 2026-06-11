"""Risk management: the only component that turns signals into orders.

Sizing, the basic caps, and the account-level circuit breakers (daily loss,
drawdown, loss-streak cooldown, daily entry cap — ARCHITECTURE.md 4.3) live
here; the kill switch lives in the engine so it works even when strategy or
risk logic is wedged.
"""

from tradebot.risk.breakers import BreakerConfig, CircuitBreakers
from tradebot.risk.manager import RiskConfig, RiskManager
from tradebot.risk.stops import ManagedStop

__all__ = ["BreakerConfig", "CircuitBreakers", "ManagedStop", "RiskConfig", "RiskManager"]
