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


def test_config_is_frozen(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TRADEBOT_MODE", raising=False)
    config = AppConfig()
    with pytest.raises(ValidationError):
        config.mode = TradingMode.LIVE  # type: ignore[misc]
