"""Application configuration, loaded from environment variables.

Railway injects configuration as environment variables (prefix ``TRADEBOT_``);
nothing secret ever lives in the repository. Every default here must fail
safe (CLAUDE.md invariant 6) — most importantly, the trading mode defaults to
paper, and going live is always an explicit, deliberate setting.
"""

from __future__ import annotations

import enum
from decimal import Decimal

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from tradebot.core.models import AutonomyMode


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

    model_config = SettingsConfigDict(env_prefix="TRADEBOT_", frozen=True, populate_by_name=True)

    mode: TradingMode = TradingMode.PAPER
    """Execution mode. Defaults to paper; live is never a default anywhere."""

    quote_currency: str = "USDT"
    """The single accounting currency: only pairs quoted in it are tradable."""

    log_level: str = "INFO"
    """Root log level for structured logging."""

    exchange_id: str = "binance"
    """CCXT exchange id for market data (and, in Phase 3, execution)."""

    symbols: str = Field(
        default="BTC/USDT",
        validation_alias=AliasChoices("TRADEBOT_SYMBOLS", "TRADEBOT_SYMBOL"),
    )
    """Comma-separated pairs the worker trades (e.g. ``BTC/USDT,ETH/USDT``).
    All must be quoted in ``quote_currency``. The singular ``TRADEBOT_SYMBOL``
    is accepted as an alias so existing deployments keep working."""

    def symbol_list(self) -> tuple[str, ...]:
        """Parse ``symbols`` into an ordered, de-duplicated tuple.

        Raises ``ValueError`` when empty or when a pair is not quoted in the
        accounting currency — one quote currency is a portfolio invariant.
        """
        seen: dict[str, None] = {}
        for raw in self.symbols.split(","):
            symbol = raw.strip()
            if symbol:
                seen[symbol] = None
        if not seen:
            raise ValueError("TRADEBOT_SYMBOLS must name at least one trading pair")
        for symbol in seen:
            if not symbol.endswith(f"/{self.quote_currency}"):
                raise ValueError(
                    f"symbol {symbol!r} is not quoted in {self.quote_currency}; "
                    "all pairs must share the accounting currency"
                )
        return tuple(seen)

    database_url: str | None = None
    """Postgres DSN (``postgresql+asyncpg://...``); required to run the worker."""

    paper_initial_balance_quote: Decimal = Decimal("10000")
    """Starting paper balance in the quote currency."""

    api_token: str | None = None
    """Bearer token for the control API. Unset means the API does not start:
    a control plane that can observe (and later command) the bot is never
    exposed unauthenticated (ARCHITECTURE.md 6.4)."""

    api_port: int = Field(
        default=8000,
        validation_alias=AliasChoices("TRADEBOT_API_PORT", "PORT"),
    )
    """Port for the control API. Falls back to the platform's ``PORT``
    (Railway injects it), so no manual port mapping is needed on deploy."""

    api_cors_origins: str = "*"
    """Comma-separated origins allowed to call the API from a browser.

    The dashboard is served from a different domain than the API, so CORS
    headers are required for it to work at all. ``*`` is acceptable here
    because auth is a bearer header (no cookies): a foreign page cannot
    read the token, and every request still requires it. Restrict to the
    dashboard's origin for defence in depth once its URL is known."""

    heartbeat_url: str | None = None
    """Dead-man's switch ping URL (e.g. healthchecks.io). The bot GETs it on
    an interval while candles keep arriving; the external monitor alerts
    when the pings stop. Unset means no heartbeat (fail-safe default: the
    bot never phones anywhere it was not pointed at)."""

    heartbeat_interval_seconds: int = Field(default=60, gt=0)
    """Seconds between heartbeat pings while healthy. Validated here so a
    bad value fails at config load, before any client or task exists."""

    telegram_bot_token: str | None = None
    """Telegram bot token; alerts are disabled unless token and chat id are set."""

    telegram_chat_id: str | None = None
    """The only chat the bot talks to (allowlist of exactly one)."""

    autonomy_mode: AutonomyMode = AutonomyMode.AUTONOMOUS
    """Whether entries execute directly or wait for user approval (co-pilot)."""

    proposal_ttl_seconds: int = 900
    """Co-pilot proposals expire after this many seconds unanswered."""

    proposal_max_drift_fraction: Decimal = Decimal("0.01")
    """Approval refused once price moves this fraction from the proposal price."""
