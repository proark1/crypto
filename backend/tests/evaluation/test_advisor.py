"""Tests for the AI research advisor (advisory-only, fail-safe).

No network: a fake async client stands in for the Anthropic SDK so the prompt
construction and every degrade-to-None path are exercised deterministically.
The one place the real SDK is touched is its error class, to prove a genuine
SDK failure is caught.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import anthropic
import httpx
import pytest

from tradebot.evaluation.advisor import (
    ResearchAdvice,
    ResearchHypothesis,
    synthesize_advice,
)

_REPORT: dict[str, Any] = {
    "trade_count": 142,
    "scenario_count": 1600,
    "expectancy_r": "0.1800",
    "profit_factor": "1.3000",
    "win_rate": "0.4200",
    "sortino_r": "0.5500",
    "tail_loss_r": "-1.9000",
    "return_fraction": "0.0730",
    "net_pnl_quote": "730.00",
    "by_archetype": {
        "calm_uptrend": {"expectancy_r": "0.4000", "trade_count": 60},
        "volatile_chop": {"expectancy_r": "-0.2000", "trade_count": 40},
    },
}

_FINDINGS: list[dict[str, Any]] = [
    {
        "pattern": "chasing_extended_moves",
        "suggestion": "require a pullback before entering breakouts",
        "affected_count": 18,
        "average_r_impact": "-0.6000",
        "confidence": "high",
    }
]

_ADVICE = ResearchAdvice(
    diagnosis="Positive expectancy is carried by calm uptrends; volatile chop bleeds it.",
    hypotheses=[
        ResearchHypothesis(
            title="Gate breakouts on a pullback",
            family="breakout",
            rationale="The chasing pattern shows late entries lose ~0.6R each.",
            parameter_hint="add a 1-2 candle pullback filter before the channel entry",
        )
    ],
)


class _FakeMessages:
    """Stand-in for ``client.messages`` recording calls and returning a canned
    response (or raising a canned error)."""

    def __init__(self, *, result: Any = None, error: BaseException | None = None) -> None:
        self._result = result
        self._error = error
        self.calls: list[dict[str, Any]] = []

    async def parse(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        if self._error is not None:
            raise self._error
        return self._result


class _FakeClient:
    def __init__(self, *, result: Any = None, error: BaseException | None = None) -> None:
        self.messages = _FakeMessages(result=result, error=error)


class _Response:
    """The slice of an SDK response the advisor reads."""

    def __init__(self, *, stop_reason: str, parsed_output: Any) -> None:
        self.stop_reason = stop_reason
        self.parsed_output = parsed_output


async def _advise(client: _FakeClient, *, enabled: bool = True) -> ResearchAdvice | None:
    return await synthesize_advice(
        report=_REPORT,
        findings=_FINDINGS,
        enabled=enabled,
        model="claude-opus-4-8",
        max_tokens=4000,
        timeout_seconds=90.0,
        client=client,
    )


async def test_disabled_returns_none_without_calling_the_model() -> None:
    client = _FakeClient(result=_Response(stop_reason="end_turn", parsed_output=_ADVICE))
    result = await _advise(client, enabled=False)
    assert result is None
    assert client.messages.calls == []


async def test_happy_path_returns_parsed_advice_with_grounded_prompt() -> None:
    client = _FakeClient(result=_Response(stop_reason="end_turn", parsed_output=_ADVICE))
    result = await _advise(client)
    assert result is _ADVICE
    # The model is the configured one and the schema is the typed advice object.
    call = client.messages.calls[0]
    assert call["model"] == "claude-opus-4-8"
    assert call["output_format"] is ResearchAdvice
    # The prompt is grounded in the run's own numbers and mined patterns.
    content = call["messages"][0]["content"]
    assert "expectancy_r: 0.1800" in content
    assert "chasing_extended_moves" in content
    assert "calm_uptrend" in content


async def test_refusal_degrades_to_none() -> None:
    client = _FakeClient(result=_Response(stop_reason="refusal", parsed_output=None))
    assert await _advise(client) is None


async def test_unparsed_output_degrades_to_none() -> None:
    # Structured outputs should guarantee an object; a None (token limit, parse
    # failure) must show nothing rather than a half-formed result.
    client = _FakeClient(result=_Response(stop_reason="end_turn", parsed_output=None))
    assert await _advise(client) is None


async def test_sdk_error_is_caught_and_degrades_to_none() -> None:
    # A real SDK error class, to prove the catch covers genuine API failures.
    error = anthropic.APIConnectionError(
        request=httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    )
    client = _FakeClient(error=error)
    assert await _advise(client) is None


async def test_missing_api_key_stays_silent_when_no_client_injected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # No injected client and no credential: the advisor must not raise; it
    # returns None (the SDK is never constructed).
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    result = await synthesize_advice(
        report=_REPORT,
        findings=_FINDINGS,
        enabled=True,
        model="claude-opus-4-8",
        max_tokens=4000,
        timeout_seconds=90.0,
    )
    assert result is None


def test_advisor_never_imports_an_order_or_promotion_path() -> None:
    # The advisory boundary is structural: this module must not reach into the
    # execution, risk, engine, or portfolio layers. Guard it at the source so a
    # future edit that wires an order path trips the test.
    source = Path(__file__).resolve().parents[2] / "src" / "tradebot" / "evaluation" / "advisor.py"
    text = source.read_text(encoding="utf-8")
    for forbidden in (
        "tradebot.execution",
        "tradebot.risk",
        "tradebot.engine",
        "tradebot.portfolio",
    ):
        assert forbidden not in text
