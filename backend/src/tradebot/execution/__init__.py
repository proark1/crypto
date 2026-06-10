"""Order execution: the adapter interface and its implementations.

The only component allowed to talk to an exchange (ARCHITECTURE.md 4.4).
Everything upstream — strategies, signals, risk — sees the same
``ExecutionAdapter`` interface whether fills come from the backtest
simulator, the paper engine, or a live exchange; that is the one-code-path
invariant in concrete form.
"""

from tradebot.execution.adapter import ExecutionAdapter, FillHandler
from tradebot.execution.simulator import FillSimulatorConfig, SimulatedExecutionAdapter

__all__ = [
    "ExecutionAdapter",
    "FillHandler",
    "FillSimulatorConfig",
    "SimulatedExecutionAdapter",
]
