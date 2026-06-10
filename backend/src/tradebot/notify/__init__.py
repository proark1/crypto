"""Notifications and outbound monitoring: what the bot tells the world.

Telegram alerts (ARCHITECTURE.md 6.1) and the dead-man's heartbeat; both
share the rule that a notification failure never disturbs trading.
"""

from tradebot.notify.heartbeat import HeartbeatPinger
from tradebot.notify.telegram import TelegramNotifier

__all__ = ["HeartbeatPinger", "TelegramNotifier"]
