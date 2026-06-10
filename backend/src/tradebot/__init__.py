"""Autonomous crypto spot trading bot.

The package layout mirrors ARCHITECTURE.md section 4: each subpackage is one
component with a strict boundary. Strategies never place orders; the execution
engine accepts orders only from the risk manager; nothing outside ``execution``
knows whether the bot runs in backtest, paper, or live mode.
"""

__version__ = "0.1.0"
