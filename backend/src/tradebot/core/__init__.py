"""Core building blocks shared by every component.

Domain models (``models``), the deterministic event bus (``events``), the clock
abstraction that makes backtests time-travel safely (``clock``), and application
configuration (``config``). Nothing in here may import from any other tradebot
subpackage — ``core`` sits at the bottom of the dependency graph.
"""
