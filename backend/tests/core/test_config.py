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


def test_symbol_in_wrong_quote_currency_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TRADEBOT_SYMBOLS", "BTC/USDT,BTC/EUR")
    with pytest.raises(ValueError, match="not quoted in"):
        AppConfig().symbol_list()


def test_empty_symbols_are_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TRADEBOT_SYMBOLS", " , ")
    with pytest.raises(ValueError, match="at least one"):
        AppConfig().symbol_list()


def test_non_positive_heartbeat_interval_fails_at_load(monkeypatch: pytest.MonkeyPatch) -> None:
    """A bad interval must fail config load, before any client or task exists."""
    monkeypatch.setenv("TRADEBOT_HEARTBEAT_INTERVAL_SECONDS", "0")
    with pytest.raises(ValidationError):
        AppConfig()


def test_config_is_frozen(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TRADEBOT_MODE", raising=False)
    config = AppConfig()
    with pytest.raises(ValidationError):
        config.mode = TradingMode.LIVE  # type: ignore[misc]
