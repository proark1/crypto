"""Notifications: pushing what the bot does to the user's phone.

Telegram first (ARCHITECTURE.md 6.1); approve/reject buttons for co-pilot
mode arrive with the authorization module.
"""

from tradebot.notify.telegram import TelegramNotifier

__all__ = ["TelegramNotifier"]
