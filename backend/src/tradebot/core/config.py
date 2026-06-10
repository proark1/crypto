"""Application configuration, loaded from environment variables.

Railway injects configuration as environment variables (prefix ``TRADEBOT_``);
nothing secret ever lives in the repository. Every default here must fail
safe (CLAUDE.md invariant 6) — most importantly, the trading mode defaults to
paper, and going live is always an explicit, deliberate setting.
"""

from __future__ import annotations

import enum

from pydantic_settings import BaseSettings, SettingsConfigDict


class TradingMode(enum.StrEnum):
    """Which execution adapter the bot runs against."""

    BACKTEST = "backtest"
    PAPER = "paper"
    LIVE = "live"


class AppConfig(BaseSettings):
    """Top-level runtime configuration.

    Per-coin strategy/risk configuration is a separate, versioned concern
    (ARCHITECTURE.md section 11); this object holds only process-wide settings.
    """

    model_config = SettingsConfigDict(env_prefix="TRADEBOT_", frozen=True)

    mode: TradingMode = TradingMode.PAPER
    """Execution mode. Defaults to paper; live is never a default anywhere."""

    quote_currency: str = "USDT"
    """The single accounting currency: only pairs quoted in it are tradable."""

    log_level: str = "INFO"
    """Root log level for structured logging."""
