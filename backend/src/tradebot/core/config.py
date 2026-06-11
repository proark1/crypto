"""Application configuration, loaded from environment variables.

Railway injects configuration as environment variables (prefix ``TRADEBOT_``);
nothing secret ever lives in the repository. Every default here must fail
safe (CLAUDE.md invariant 6) — most importantly, the trading mode defaults to
paper, and going live is always an explicit, deliberate setting.
"""

from __future__ import annotations

import enum
from decimal import Decimal
from typing import Literal

from pydantic import AliasChoices, Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from tradebot.core.models import AutonomyMode


def validate_symbol_quote(symbol: str, quote_currency: str) -> None:
    """Reject a pair that is not ``BASE/<quote_currency>``.

    One accounting currency is a portfolio invariant; this is the single
    check used by config parsing and the runtime add-a-coin flow alike.
    """
    base, separator, quote = symbol.partition("/")
    if not base or separator != "/" or quote != quote_currency:
        raise ValueError(
            f"symbol {symbol!r} is not quoted in {quote_currency}; "
            "all pairs must share the accounting currency"
        )


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

    log_format: Literal["json", "text"] = "json"
    """Log output format: ``json`` (one structured event per line, for
    production aggregation) or ``text`` (human-readable, for local tailing)."""

    exchange_id: str = "binance"
    """CCXT exchange id for market data (and, in Phase 3, execution)."""

    symbols: str = Field(
        default="BTC/USDT",
        validation_alias=AliasChoices("TRADEBOT_SYMBOLS", "TRADEBOT_SYMBOL"),
    )
    """Comma-separated pairs the worker trades (e.g. ``BTC/USDT,ETH/USDT``).
    All must be quoted in ``quote_currency``. The singular ``TRADEBOT_SYMBOL``
    is accepted as an alias so existing deployments keep working."""

    @model_validator(mode="after")
    def _symbols_must_parse(self) -> AppConfig:
        """Validate the pair list at config load, not first use.

        A bad list must stop the deploy before any component is built
        around it.
        """
        self.symbol_list()
        return self

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
            validate_symbol_quote(symbol, self.quote_currency)
        return tuple(seen)

    database_url: str | None = None
    """Postgres DSN (``postgresql+asyncpg://...``); required to run the worker."""

    paper_initial_balance_quote: Decimal = Decimal("10000")
    """Starting paper balance in the quote currency."""

    competition_enabled: bool = True
    """Run the strategy competition: alongside the production bot, four
    challenger paper accounts (trend following, mean reversion, breakout,
    momentum solo) trade the same coins through the same gates, each from
    its own journal-backed balance, so the leaderboard can say who is
    best. Paper-scoped by construction — the worker refuses any other
    mode — and challengers never notify, never propose, and are never
    promoted to production routing by winning."""

    regime_gate_enabled: bool = True
    """Gate every coin's entries on the reference market's regime
    (ARCHITECTURE.md 5.2). On by default — the fail-safe direction for a
    filter is filtering. The worker disables it loudly when the reference
    symbol is not among the traded coins (no data, no gate)."""

    regime_reference_symbol: str = "BTC/USDT"
    """The market-wide reference whose regime gates all entries."""

    sentiment_enabled: bool = True
    """Poll Fear & Greed and BTC dominance (free, keyless APIs) as advisory
    tighteners for the regime gate. They can only block entries, never
    allow them, so the fail-safe direction is on."""

    sentiment_poll_minutes: int = Field(default=15, ge=1)

    sentiment_extreme_fear_at_or_below: int = Field(default=20, ge=0, le=100)
    """Fear & Greed at or below this pauses trend-family entries
    (mean-reversion entries are exempt: that family buys fear by design,
    behind its protective stop and the regime gate's drawdown risk-off)."""

    sentiment_extreme_greed_at_or_above: int = Field(default=90, ge=0, le=100)
    """Fear & Greed at or above this pauses every family's entries —
    euphoria is historically where tops form."""

    @model_validator(mode="after")
    def _sentiment_thresholds_must_not_overlap(self) -> AppConfig:
        """Stop the deploy when the fear floor reaches the greed ceiling.

        That overlap would block entries at every Fear & Greed value — a
        typo, not a choice.
        """
        if self.sentiment_extreme_fear_at_or_below >= self.sentiment_extreme_greed_at_or_above:
            raise ValueError(
                f"TRADEBOT_SENTIMENT_EXTREME_FEAR_AT_OR_BELOW "
                f"({self.sentiment_extreme_fear_at_or_below}) must be below "
                f"TRADEBOT_SENTIMENT_EXTREME_GREED_AT_OR_ABOVE "
                f"({self.sentiment_extreme_greed_at_or_above})"
            )
        return self

    cryptopanic_token: str | None = None
    """CryptoPanic API token. Unset disables news polling; the news gate
    still runs (scheduled-event windows work without any news source)."""

    news_poll_seconds: int = Field(default=90, ge=30)
    """News poll interval (ARCHITECTURE.md 5.3: every 1-2 minutes); floored
    at 30s to stay polite to free-tier APIs."""

    news_flag_ttl_hours: int = Field(default=24, gt=0)
    """How long a negative-news flag blocks a coin's entries unless renewed."""

    event_calendar_json: str = ""
    """Scheduled no-entry windows (FOMC/CPI/unlocks) as JSON:
    ``[{"name": "FOMC", "time": "2026-06-17T18:00:00Z", "window_minutes": 120}]``.
    Validated at load — a typo stops the deploy, it never silently disables
    event awareness."""

    @model_validator(mode="after")
    def _calendar_must_parse(self) -> AppConfig:
        from tradebot.news.calendar import EventCalendar

        EventCalendar.from_json(self.event_calendar_json)
        return self

    backup_s3_endpoint: str | None = None
    """S3-compatible endpoint for scheduled DB backups (e.g. an R2 URL).
    Backups run only when endpoint, bucket, and both keys are all set."""

    backup_s3_bucket: str | None = None
    backup_s3_access_key: str | None = None
    backup_s3_secret_key: str | None = None
    backup_s3_region: str = "auto"
    """R2 uses the literal region ``auto``; AWS wants a real one."""

    backup_interval_hours: int = Field(default=24, ge=1)
    backup_prefix: str = "tradebot"

    @model_validator(mode="after")
    def _backup_config_is_all_or_nothing(self) -> AppConfig:
        """Reject half-configured backups at deploy: a typo is not a choice.

        Backups that silently never run are exactly the failure §7 backups
        exist to prevent.
        """
        values = (
            self.backup_s3_endpoint,
            self.backup_s3_bucket,
            self.backup_s3_access_key,
            self.backup_s3_secret_key,
        )
        configured = [value for value in values if value]
        if configured and len(configured) != len(values):
            raise ValueError(
                "backup misconfigured: endpoint, bucket, access key, and secret key "
                "must all be set together (or none of them)"
            )
        return self

    history_backfill_days: int = Field(default=1460, ge=0)
    """How many days of candle history to fetch for a symbol that has none
    stored yet (first boot, newly added coin). Binance-class venues serve
    years of public 1m history for free; the only cost is database storage
    (roughly 0.5 GB per symbol-year of 1m candles) and a one-time deep
    crawl on first boot (a few minutes per symbol behind CCXT's rate
    limiter; later boots resume from the newest stored candle). Four years
    spans a full halving cycle — bull, bear, and chop — so evaluations and
    walk-forward sweeps are never blind to a regime the market has already
    shown, and it dwarfs the research system's one-year default evaluation
    window (§12) and the regime gate's ~10-day warm-up, so a fresh deploy
    trades and researches from day one. Databases backfilled under a
    shallower setting are deepened to this horizon on the next boot. ``0``
    disables deep backfill: only the gap from the newest stored candle
    forward is repaired."""

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

    auto_improve_enabled: bool = True
    """Run the automated improvement loop (ARCHITECTURE.md §12.7): sweep
    variants of the active configuration on a schedule and promote a
    challenger only when its verdict is *validated* — the Bonferroni-
    corrected, walk-forward-tested bar, never a training win. Promotions
    apply to the **paper** bot only (the worker refuses live mode outright),
    are versioned with their sweep as lineage, and are revertible through
    the API; that scoping is what keeps an on-by-default self-tuner
    fail-safe. Going live stays a human decision in every configuration."""

    auto_improve_interval_hours: int = Field(default=12, gt=0)
    """Hours between automated improvement cycles. Each cycle sweeps one
    coin (rotating), so with two coins every coin is revisited daily at the
    default."""

    auto_improve_history_days: int = Field(default=365, gt=0)
    """History window automated evaluations and sweeps learn from — a full
    year of the stored backfill, so the loop never judges on a sliver."""

    @model_validator(mode="after")
    def _backfill_must_cover_auto_improve_window(self) -> AppConfig:
        """Reject a backfill horizon shallower than the research window.

        The improvement loop trusts that ``auto_improve_history_days`` of
        candles exist; a deep backfill that stops short would have it judge
        on a sliver anyway — silently, which is the failure the window
        exists to prevent. ``0`` is exempt: deep backfill is off and the
        stored history (however it got there) is what the operator chose.
        """
        if self.auto_improve_enabled and 0 < self.history_backfill_days < (
            self.auto_improve_history_days
        ):
            raise ValueError(
                f"TRADEBOT_HISTORY_BACKFILL_DAYS ({self.history_backfill_days}) must "
                f"cover TRADEBOT_AUTO_IMPROVE_HISTORY_DAYS "
                f"({self.auto_improve_history_days}): the improvement loop would "
                "evaluate on less history than configured"
            )
        return self

    auto_improve_timeframe: str = "1h"
    """Candle timeframe the automated sweeps evaluate (validated at boot)."""

    proposal_ttl_seconds: int = 900
    """Co-pilot proposals expire after this many seconds unanswered."""

    proposal_max_drift_fraction: Decimal = Decimal("0.01")
    """Approval refused once price moves this fraction from the proposal price."""
