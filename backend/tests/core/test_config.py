from decimal import Decimal

import pytest
from pydantic import ValidationError

from tradebot.core.config import AppConfig, TradingMode


def test_mode_defaults_to_paper_fail_safe(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TRADEBOT_MODE", raising=False)
    config = AppConfig()
    assert config.mode == TradingMode.PAPER


def test_default_quote_currency_is_usdt(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TRADEBOT_QUOTE_CURRENCY", raising=False)
    assert AppConfig().quote_currency == "USDT"


def test_live_mode_requires_explicit_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TRADEBOT_MODE", "live")
    assert AppConfig().mode == TradingMode.LIVE


def test_invalid_mode_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TRADEBOT_MODE", "yolo")
    with pytest.raises(ValidationError):
        AppConfig()


def test_trade_and_research_timeframes_default_coherent(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in (
        "TRADEBOT_TRADE_TIMEFRAME",
        "TRADEBOT_AUTO_IMPROVE_TIMEFRAME",
        "TRADEBOT_CAMPAIGN_TIMEFRAME",
    ):
        monkeypatch.delenv(var, raising=False)
    config = AppConfig()
    # The bot trades the same timeframe it researches — the coherence invariant.
    assert config.trade_timeframe == config.campaign_timeframe == config.auto_improve_timeframe
    assert config.trade_timeframe == "4h"


def test_trading_timeframe_must_match_research(monkeypatch: pytest.MonkeyPatch) -> None:
    # Researching 4h while trading 1h would apply every promotion at the wrong
    # cadence; the config refuses to load rather than trade an incoherent setup.
    monkeypatch.setenv("TRADEBOT_TRADE_TIMEFRAME", "1h")
    monkeypatch.delenv("TRADEBOT_CAMPAIGN_TIMEFRAME", raising=False)
    monkeypatch.delenv("TRADEBOT_AUTO_IMPROVE_TIMEFRAME", raising=False)
    with pytest.raises(ValidationError, match="timeframes must match"):
        AppConfig()


def test_api_port_falls_back_to_platform_port(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TRADEBOT_API_PORT", raising=False)
    monkeypatch.setenv("PORT", "7777")  # what Railway injects
    assert AppConfig().api_port == 7777


def test_explicit_api_port_beats_platform_port(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TRADEBOT_API_PORT", "9000")
    monkeypatch.setenv("PORT", "7777")
    assert AppConfig().api_port == 9000


def test_symbols_parse_strip_and_dedupe(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TRADEBOT_SYMBOLS", " BTC/USDT, ETH/USDT,BTC/USDT ,")
    assert AppConfig().symbol_list() == ("BTC/USDT", "ETH/USDT")


def test_legacy_singular_symbol_env_still_works(monkeypatch: pytest.MonkeyPatch) -> None:
    """Existing deployments set TRADEBOT_SYMBOL; renaming must not break them."""
    monkeypatch.delenv("TRADEBOT_SYMBOLS", raising=False)
    monkeypatch.setenv("TRADEBOT_SYMBOL", "ETH/USDT")
    assert AppConfig().symbol_list() == ("ETH/USDT",)


def test_symbol_in_wrong_quote_currency_fails_at_load(monkeypatch: pytest.MonkeyPatch) -> None:
    """A bad pair list stops the deploy at config load, not at first use."""
    monkeypatch.setenv("TRADEBOT_SYMBOLS", "BTC/USDT,BTC/EUR")
    with pytest.raises(ValidationError, match="not quoted in"):
        AppConfig()


def test_empty_symbols_fail_at_load(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TRADEBOT_SYMBOLS", " , ")
    with pytest.raises(ValidationError, match="at least one"):
        AppConfig()


def test_non_positive_heartbeat_interval_fails_at_load(monkeypatch: pytest.MonkeyPatch) -> None:
    """A bad interval must fail config load, before any client or task exists."""
    monkeypatch.setenv("TRADEBOT_HEARTBEAT_INTERVAL_SECONDS", "0")
    with pytest.raises(ValidationError):
        AppConfig()


def test_backfill_shallower_than_research_window_fails_at_load(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The improvement loop must never silently evaluate on a sliver."""
    monkeypatch.setenv("TRADEBOT_HISTORY_BACKFILL_DAYS", "180")
    monkeypatch.setenv("TRADEBOT_AUTO_IMPROVE_HISTORY_DAYS", "365")
    with pytest.raises(ValidationError, match="must cover"):
        AppConfig()


def test_disabled_backfill_skips_research_window_check(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """0 means deep backfill is off; stored history is the operator's choice."""
    monkeypatch.setenv("TRADEBOT_HISTORY_BACKFILL_DAYS", "0")
    monkeypatch.setenv("TRADEBOT_AUTO_IMPROVE_HISTORY_DAYS", "365")
    assert AppConfig().history_backfill_days == 0


def test_disabled_auto_improve_skips_research_window_check(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TRADEBOT_AUTO_IMPROVE_ENABLED", "false")
    monkeypatch.setenv("TRADEBOT_HISTORY_BACKFILL_DAYS", "180")
    assert AppConfig().history_backfill_days == 180


def test_campaign_is_off_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """Campaigns are opt-in; merging the wiring changes nothing until enabled."""
    monkeypatch.delenv("TRADEBOT_CAMPAIGN_ENABLED", raising=False)
    assert AppConfig().campaign_enabled is False


def test_campaign_backfill_must_cover_history_plus_holdout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An enabled campaign needs both its history and its holdout depth of candles."""
    monkeypatch.setenv("TRADEBOT_CAMPAIGN_ENABLED", "true")
    monkeypatch.setenv("TRADEBOT_HISTORY_BACKFILL_DAYS", "400")  # short of 1280 + 180
    with pytest.raises(ValidationError, match="must cover"):
        AppConfig()


def test_disabled_campaign_skips_the_backfill_check(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The campaign window check only bites when campaigns are enabled."""
    monkeypatch.setenv("TRADEBOT_CAMPAIGN_ENABLED", "false")
    # 750 covers the auto-improve window (730) but is short of the campaign's
    # 1460 (1280 history + 180 holdout), so a fired campaign check would reject
    # it — disabled, it does not.
    monkeypatch.setenv("TRADEBOT_HISTORY_BACKFILL_DAYS", "750")
    assert AppConfig().history_backfill_days == 750


def test_campaign_diagnostic_timeframes_parse_without_primary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TRADEBOT_CAMPAIGN_TIMEFRAME", "4h")
    monkeypatch.setenv("TRADEBOT_CAMPAIGN_DIAGNOSTIC_TIMEFRAMES", "15m, 1h, 4h, 1d,1h")
    assert AppConfig().campaign_diagnostic_timeframe_list() == ("15m", "1h", "1d")


def test_simulator_realism_knobs_load_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TRADEBOT_SIMULATOR_SPREAD_BPS", "2")
    monkeypatch.setenv("TRADEBOT_SIMULATOR_MARKET_SLIPPAGE_BPS", "7.5")
    monkeypatch.setenv("TRADEBOT_SIMULATOR_MAX_VOLUME_FRACTION", "0.25")
    monkeypatch.setenv("TRADEBOT_SIMULATOR_VOLUME_IMPACT_BPS", "20")
    monkeypatch.setenv("TRADEBOT_SIMULATOR_SUBMIT_LATENCY_CANDLES", "1")

    config = AppConfig()

    assert config.simulator_spread_bps == Decimal("2")
    assert config.simulator_market_slippage_bps == Decimal("7.5")
    assert config.simulator_max_volume_fraction == Decimal("0.25")
    assert config.simulator_volume_impact_bps == Decimal("20")
    assert config.simulator_submit_latency_candles == 1


def test_sentiment_thresholds_load_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TRADEBOT_SENTIMENT_EXTREME_FEAR_AT_OR_BELOW", "10")
    monkeypatch.setenv("TRADEBOT_SENTIMENT_EXTREME_GREED_AT_OR_ABOVE", "85")
    config = AppConfig()
    assert config.sentiment_extreme_fear_at_or_below == 10
    assert config.sentiment_extreme_greed_at_or_above == 85


def test_overlapping_sentiment_thresholds_fail_at_load(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fear floor at the greed ceiling would block every entry; a typo, not a choice."""
    monkeypatch.setenv("TRADEBOT_SENTIMENT_EXTREME_FEAR_AT_OR_BELOW", "90")
    monkeypatch.setenv("TRADEBOT_SENTIMENT_EXTREME_GREED_AT_OR_ABOVE", "90")
    with pytest.raises(ValidationError, match="must be below"):
        AppConfig()


def test_config_is_frozen(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TRADEBOT_MODE", raising=False)
    config = AppConfig()
    with pytest.raises(ValidationError):
        config.mode = TradingMode.LIVE  # type: ignore[misc]


def test_money_config_rejects_float_input() -> None:
    """Money config rejects float like every monetary domain field (invariant 1).

    Env vars arrive as strings, so a float can only reach config via a
    programmatic kwarg — exactly how tests and tools construct AppConfig. The
    float kwargs below are deliberately wrong-typed to exercise that guard.
    """
    with pytest.raises(ValidationError, match="float is not allowed"):
        AppConfig(paper_initial_balance_quote=10000.0)
    with pytest.raises(ValidationError, match="float is not allowed"):
        AppConfig(buy_fee_bps=10.0)
    with pytest.raises(ValidationError, match="float is not allowed"):
        AppConfig(sell_fee_bps=10.0)
    with pytest.raises(ValidationError, match="float is not allowed"):
        AppConfig(proposal_max_drift_fraction=0.01)


def test_money_config_enforces_sane_bounds() -> None:
    """Positive balance and drift, non-negative fees — an unsafe value fails loudly."""
    with pytest.raises(ValidationError):
        AppConfig(paper_initial_balance_quote=Decimal("0"))  # must be > 0
    with pytest.raises(ValidationError):
        AppConfig(buy_fee_bps=Decimal("-1"))  # a fee cannot be negative
    with pytest.raises(ValidationError):
        AppConfig(proposal_max_drift_fraction=Decimal("0"))  # must be > 0


def test_money_config_accepts_decimal_kwarg_and_string_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A Decimal kwarg and a string env var (how config really loads) both parse exactly."""
    monkeypatch.setenv("TRADEBOT_BUY_FEE_BPS", "7")
    config = AppConfig(paper_initial_balance_quote=Decimal("500"))
    assert config.paper_initial_balance_quote == Decimal("500")
    assert config.buy_fee_bps == Decimal("7")
