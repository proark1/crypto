"""Control plane: the FastAPI app the dashboard and operators talk to.

Read-only in this first cut (status, position, fills); commands — pause,
approvals, the kill switch — arrive with the authorization module. Every
endpoint requires the bearer token; without a configured token the app is
never even started (fail safe, ARCHITECTURE.md 6.4).
"""

from tradebot.api.app import BotState, create_app, create_health_only_app

__all__ = ["BotState", "create_app", "create_health_only_app"]
