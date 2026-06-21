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

from tradebot.core.models import AutonomyMode, NonNegativeAmount, PositiveAmount


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
        default="BTC/USDT,ETH/USDT,SOL/USDT",
        validation_alias=AliasChoices("TRADEBOT_SYMBOLS", "TRADEBOT_SYMBOL"),
    )
    """Comma-separated pairs the worker trades (e.g. ``BTC/USDT,ETH/USDT``).
    All must be quoted in ``quote_currency``. The singular ``TRADEBOT_SYMBOL``
    is accepted as an alias so existing deployments keep working.

    Defaults to a small basket of liquid majors rather than BTC alone: the
    research campaign auto-rotates across every active coin, so more coins
    means more independent samples per cycle (more statistical power) and a
    broader search for where an edge might exist. Only seeds the coin table on
    first boot — an existing deployment changes its set in the coin manager."""

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

    paper_initial_balance_quote: PositiveAmount = Decimal("10000")
    """Starting paper balance in the quote currency (must be > 0)."""

    buy_fee_bps: NonNegativeAmount = Decimal("10")
    """Trading fee charged on every *buy* fill, in basis points of notional
    (10 bps = 0.1%, the conventional spot taker fee). This is only the boot
    default: once an operator sets fees in the UI, the persisted value wins.
    Applies to live paper fills across every bot; backtests keep their own
    deterministic cost model."""

    sell_fee_bps: NonNegativeAmount = Decimal("10")
    """Trading fee charged on every *sell* fill, in basis points of notional
    (see ``buy_fee_bps``)."""

    competition_enabled: bool = True
    """Run the strategy competition: alongside the production bot, five
    challenger paper accounts (trend following, mean reversion, breakout,
    momentum, and squeeze-breakout solo) trade the same coins through the
    same gates, each from its own journal-backed balance, so the
    leaderboard can say who is best. Paper-scoped by construction — the
    worker refuses any other mode — and challengers never notify, never
    propose, and are never promoted to production routing by winning."""

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

    funding_signal_enabled: bool = False
    """Poll perpetual funding rate as an advisory tightener for the regime
    gate (ARCHITECTURE.md §5.2). Off by default and opt-in: it is a newer,
    less-proven positioning signal than Fear & Greed / dominance, and it needs
    the venue to expose funding for ``funding_reference_symbol``. Like every
    sentiment tightener it is one-way — it can only pause new entries (crowded
    longs ≈ top risk), never open the gate — and a missing or stale feed
    contributes nothing, so enabling it cannot trade more aggressively. It has
    no effect on backtests: funding is a live signal only, never fed to the
    deterministic scenario engine, so the golden backtest is unchanged.
    Requires the regime gate (and sentiment) to exist — there is nothing to
    tighten otherwise."""

    funding_reference_symbol: str = ""
    """The perpetual to read funding from (e.g. ``BTC/USDT:USDT``). Empty
    disables the funding poll even when ``funding_signal_enabled`` is set: the
    perp's market notation is venue-specific, so it is named explicitly rather
    than guessed from the spot reference symbol."""

    funding_crowded_long_at_or_above: float = Field(default=0.001, gt=0.0)
    """Funding rate (per-interval fraction; 0.001 = 0.1% paid by longs each
    funding window) at or above which new entries pause — persistently high
    positive funding is crowded, over-leveraged longs."""

    funding_history_enabled: bool = True
    """Backfill and keep each traded coin's perpetual funding history in the
    store — the researchable series the funding strategy grades on, derived from
    the spot pair's matching USDT perp. Pure data collection (no trading effect
    on its own), so on by default; a coin with no perp funding degrades to an
    empty series. Depth follows ``history_backfill_days``."""

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

    auto_improve_history_days: int = Field(default=730, gt=0)
    """History window automated evaluations and sweeps learn from — two years
    of the stored backfill, so a verdict spans more regimes and grades more
    trades (tighter confidence intervals), not a sliver."""

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

    trade_timeframe: str = "1h"
    """Candle timeframe the **live** bot trades on. The 1m feed is rolled up to
    this interval before it reaches the engines, so the strategy decides on the
    same bars its research grades and its promotions are tuned on. Must equal
    the research timeframes (validated below) — a live bot trading a different
    timeframe than its sweeps grade would apply every promotion at the wrong
    cadence (a 50-period EMA means 50 hours on 1h, 50 minutes on 1m)."""

    campaign_enabled: bool = False
    """Run the §12.7 loop as an iterated walk-forward *campaign* rather than
    the single-sweep auto-improver: sweep, promote every validated challenger,
    climb from it, and refine, back to back until a budget is spent — the
    "adapt and re-run until it is good" loop. **Off by default**; when on it
    supersedes the auto-improver (the two share the one research lane).
    Identical safety scope: promotions are paper-only, validated, versioned
    with their sweep, and revertible."""

    campaign_timeframe: str = "1h"
    """Candle timeframe campaign sweeps evaluate (validated at boot)."""

    campaign_history_days: int = Field(default=730, gt=0)
    """History each campaign round's walk-forward sweep is graded over, ending
    at the reserved holdout boundary — two years, so each round spans more
    regimes and grades more trades (more statistical power per verdict). The
    four-year default backfill covers this with the holdout to spare."""

    campaign_holdout_days: int = Field(default=60, gt=0)
    """Most-recent days reserved as the untouched holdout — graded once, at
    the end, for the campaign's non-gating honesty read; never swept."""

    campaign_scenario_count: int = Field(default=1600, gt=0)
    """Scenarios per candidate per period (matches sweep.DEFAULT_SCENARIO_COUNT
    — the unstarved default that clears the minimum-trades bar)."""

    campaign_max_rounds: int = Field(default=8, ge=1)
    """Hard cap on rounds per campaign — the budget that bounds how many
    chances the iterated search gets at a lucky winner."""

    campaign_max_hours: float = Field(default=6.0, gt=0.0)
    """Wall-clock budget per campaign; it shares one CPU with live trading."""

    campaign_refine_factor: float = Field(default=0.5, gt=0.0, lt=1.0)
    """How much the step shrinks after a round finds no validated gain."""

    campaign_min_scale: float = Field(default=0.25, gt=0.0, le=1.0)
    """Below this step the search has converged; the campaign stops."""

    campaign_cooldown_minutes: float = Field(default=30.0, gt=0.0)
    """Rest between campaigns, to leave CPU for live trading."""

    campaign_max_lifetime_promotions_per_target: int = Field(default=0, ge=0)
    """Per-target lifetime cap on the campaign loop's auto-promotions; ``0``
    (the default) disables it. The loop runs forever, so without an outer
    bound its cumulative multiple-comparisons exposure grows without limit.
    Past this many promotions for a target (summed across all its campaigns
    from the durable history), its campaigns keep researching but no longer
    change the live config, until a human reviews and promotes manually or
    raises the cap. Disabled by default so existing behaviour is unchanged
    until an operator opts in."""

    research_weighted_allocation: bool = True
    """Spend the §12.7 research lane by live standing rather than a flat
    round-robin. On (the default), the loops boost families the evidence likes
    (routing candidates, live-paper winners) and park families it has judged
    losers (down past a threshold with enough trades), re-probing the parked
    ones on a cadence so none is abandoned. ``production`` is never parked.
    Moves no money and pauses no account — it only reallocates research time;
    off restores the flat rotation."""

    @model_validator(mode="after")
    def _backfill_must_cover_campaign_window(self) -> AppConfig:
        """Reject a backfill shallower than the campaign's full data span.

        A campaign grades ``campaign_history_days`` ending at the holdout
        boundary and reserves ``campaign_holdout_days`` after it, so it needs
        both depths of candles; a backfill that stops short would have it judge
        on a sliver. ``0`` is exempt (deep backfill off — the stored history is
        the operator's choice), and the check only bites when campaigns are on.
        """
        needed = self.campaign_history_days + self.campaign_holdout_days
        if self.campaign_enabled and 0 < self.history_backfill_days < needed:
            raise ValueError(
                f"TRADEBOT_HISTORY_BACKFILL_DAYS ({self.history_backfill_days}) must cover "
                f"TRADEBOT_CAMPAIGN_HISTORY_DAYS + TRADEBOT_CAMPAIGN_HOLDOUT_DAYS ({needed}): "
                "a campaign would otherwise grade on less history than configured"
            )
        return self

    @model_validator(mode="after")
    def _trading_and_research_timeframes_agree(self) -> AppConfig:
        """Require the live, auto-improve, and campaign timeframes to be equal.

        Promotions flow from research straight onto the live strategies, so a
        parameter tuned on one timeframe and traded on another means something
        different live than where it was validated. Locking the three together
        is the invariant that keeps the research loop's output applicable to the
        bot that trades it; the individual strings are parsed for validity at
        boot (see the worker).
        """
        timeframes = {
            "TRADEBOT_TRADE_TIMEFRAME": self.trade_timeframe,
            "TRADEBOT_AUTO_IMPROVE_TIMEFRAME": self.auto_improve_timeframe,
            "TRADEBOT_CAMPAIGN_TIMEFRAME": self.campaign_timeframe,
        }
        if len(set(timeframes.values())) > 1:
            raise ValueError(
                "live and research timeframes must match — a promotion is graded on the "
                f"research timeframe and traded on the live one: {timeframes}"
            )
        return self

    accept_sweep_enabled: bool = True
    """Queue a findings-targeted sweep when a finding is accepted, so a
    human verdict has a visible consequence within the hour instead of
    waiting for the next scheduled cycle. Research-only compute: the
    sweep's verdict feeds the same validated-only, paper-scoped promotion
    path as every other sweep, so the default is safe."""

    accept_sweep_delay_seconds: int = Field(default=600, gt=0)
    """Coalescing window after the first acceptance on a run: every further
    acceptance inside it rides the same sweep (one Bonferroni budget for
    the whole curated set), and the timer never resets, so latency stays
    bounded."""

    ai_advisor_enabled: bool = False
    """Run the AI research advisor (ARCHITECTURE.md §12.9): on demand, ask a
    Claude model to read a completed research run's report and mined findings
    and propose experiment hypotheses a human can choose to sweep. Off by
    default and advisory-only — it never places an order, never promotes a
    configuration, never runs on the candle hot path, and never feeds the
    deterministic backtest; its output is a recommendation surfaced in the
    research UI, and arming a sweep from a hypothesis stays the existing
    human-initiated path. Enabling it also needs ``ANTHROPIC_API_KEY`` in the
    environment and the optional ``anthropic`` dependency (the ``ai`` extra);
    absent either, the advisor stays silent rather than failing a request —
    the fail-safe direction for an advisory feature that moves no money
    (CLAUDE.md invariants 4 and 6)."""

    ai_advisor_model: str = "claude-opus-4-8"
    """Claude model id the advisor calls. Advisory text only — never on the
    candle hot path, never a deterministic input."""

    ai_advisor_max_tokens: int = Field(default=4000, gt=0)
    """Output-token ceiling for one advisory response (a diagnosis plus a few
    hypotheses is small); bounds the cost and latency of a best-effort call."""

    ai_advisor_timeout_seconds: float = Field(default=90.0, gt=0)
    """Hard per-call timeout in seconds. The advisor is best-effort: a slow or
    hung call degrades to no advice rather than blocking the API response."""

    proposal_ttl_seconds: int = 900
    """Co-pilot proposals expire after this many seconds unanswered."""

    proposal_max_drift_fraction: PositiveAmount = Decimal("0.01")
    """Approval refused once price moves this fraction from the proposal price
    (must be > 0)."""
