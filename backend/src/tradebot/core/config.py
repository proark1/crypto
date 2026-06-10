"""Application configuration, loaded from environment variables.

Railway injects configuration as environment variables (prefix ``TRADEBOT_``);
nothing secret ever lives in the repository. Every default here must fail
safe (CLAUDE.md invariant 6) — most importantly, the trading mode defaults to
paper, and going live is always an explicit, deliberate setting.
"""

from __future__ import annotations

import enum
from decimal import Decimal

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

    exchange_id: str = "binance"
    """CCXT exchange id for market data (and, in Phase 3, execution)."""

    symbol: str = "BTC/USDT"
    """The pair the worker trades; multi-coin arrives with the control API."""

    database_url: str | None = None
    """Postgres DSN (``postgresql+asyncpg://...``); required to run the worker."""

    paper_initial_balance_quote: Decimal = Decimal("10000")
    """Starting paper balance in the quote currency."""

    api_token: str | None = None
    """Bearer token for the control API. Unset means the API does not start:
    a control plane that can observe (and later command) the bot is never
    exposed unauthenticated (ARCHITECTURE.md 6.4)."""

    api_port: int = 8000
    """Port for the control API (Railway injects PORT; map it to this)."""
