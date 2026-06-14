"""AI research advisor — advisory-only experiment synthesis (ARCHITECTURE.md §12.9).

Given a completed research run's report and its mined findings, this module
optionally asks a Claude model to diagnose *why* the run looks the way it does
and to propose a few experiment hypotheses a human can then choose to sweep. It
is deliberately powerless: it returns a recommendation object and nothing else.

Safety contract — never weaken (CLAUDE.md invariants 3, 4, 6):

- It places no orders and promotes no configuration. Accepting a hypothesis
  arms a sweep through the existing human-initiated path; this module does not.
- It never runs on the candle hot path and never feeds the deterministic
  backtest — its output is presentation, not a trading input, so the golden
  backtest stays byte-identical whether or not the advisor ran.
- It fails safe: a disabled flag, the optional ``anthropic`` package being
  absent, a missing ``ANTHROPIC_API_KEY``, a model refusal, a timeout, or any
  SDK error all resolve to ``None`` (no advice) — never a raised error that
  could fail the surrounding research request.

Units note: this reads R-multiples and money *strings* off an already-built
report purely to compose prose. It does no arithmetic on them and produces no
order size, so the ``Decimal``-only money invariant is not in play here.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Mapping, Sequence
from typing import Any

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

# The advisor is gated by config, but the underlying SDK is an optional
# dependency (the ``ai`` extra). The model id is configured; this is only the
# environment variable the SDK itself reads for the credential, named here so
# the "missing key → stay silent" path is explicit and no key is ever stored.
_API_KEY_ENV = "ANTHROPIC_API_KEY"

_SYSTEM_PROMPT = (
    "You are a quantitative trading research assistant. You read the report of a "
    "single evaluation — where a strategy's decisions over independently sampled "
    "historical moments were graded — and help a human researcher decide what to "
    "investigate next.\n\n"
    "Metrics are in R-multiples: R is a trade's result in units of its initial "
    "risk, and expectancy is the average R per trade (above 0 is profitable). A "
    "fixed-stake money result is shown alongside for intuition. Ground every claim "
    "in the numbers and mined patterns you are given; never invent data you were "
    "not shown, and say plainly when the trade sample looks too small to trust.\n\n"
    "You only advise. Each hypothesis is an experiment a human may choose to run as "
    "a parameter sweep — nothing you write is executed, trades, or changes the "
    "strategy automatically."
)

# Report fields worth putting in front of the model, in the order they read
# best. Missing or null fields are skipped — older runs predate some of them.
_REPORT_FIELDS: tuple[str, ...] = (
    "trade_count",
    "scenario_count",
    "expectancy_r",
    "profit_factor",
    "win_rate",
    "sortino_r",
    "tail_loss_r",
    "worst_r",
    "return_fraction",
    "net_pnl_quote",
)


class ResearchHypothesis(BaseModel):
    """One proposed experiment a human may choose to run as a sweep.

    Advisory only: ``parameter_hint`` describes a direction to try in words and
    is never parsed into an applied configuration.
    """

    title: str = Field(description="A short, specific name for the experiment.")
    family: str = Field(
        description=(
            "The strategy family or knob the experiment targets, e.g. 'breakout', "
            "'mean_reversion', or 'risk'."
        )
    )
    rationale: str = Field(
        description="Why the run's evidence motivates this experiment, in two or three sentences."
    )
    parameter_hint: str = Field(
        description=(
            "The concrete parameter direction to try, in plain words, e.g. 'widen the "
            "breakout channel from 20 to 30 candles'. Advisory only — never applied "
            "automatically."
        )
    )


class ResearchAdvice(BaseModel):
    """The advisor's read of one run: a diagnosis plus experiments to consider.

    Recommendation only. Nothing here is applied automatically; a human arms a
    sweep from a hypothesis through the existing approval path.
    """

    diagnosis: str = Field(
        description="A plain-language read of what the run's numbers and findings suggest, "
        "two to four sentences."
    )
    hypotheses: list[ResearchHypothesis] = Field(
        description="Zero to four experiments worth considering, most promising first."
    )


def _import_anthropic() -> Any | None:
    """Return the ``anthropic`` module, or ``None`` when the extra is not installed.

    Imported lazily so the worker and API boot without the optional dependency;
    the advisor simply stays silent when it is absent.
    """
    try:
        import anthropic
    except ModuleNotFoundError:
        return None
    return anthropic


def _build_prompt(
    report: Mapping[str, Any],
    findings: Sequence[Mapping[str, Any]],
) -> str:
    """Compose the user message from a run's report and mined findings.

    Deterministic in its inputs (no timestamps or ids), so the same run yields
    the same prompt — friendly to prompt caching and to test assertions.
    """
    lines = [
        "A research evaluation just graded a trading strategy's decisions over "
        "independently sampled historical moments.",
        "",
        "Headline metrics:",
    ]
    for field in _REPORT_FIELDS:
        value = report.get(field)
        if value is not None:
            lines.append(f"- {field}: {value}")

    breakdown = report.get("by_archetype") or report.get("by_trend")
    if isinstance(breakdown, Mapping) and breakdown:
        lines.append("")
        lines.append("Per-regime expectancy (R) and trade count:")
        for label, stats in breakdown.items():
            if isinstance(stats, Mapping):
                lines.append(
                    f"- {label}: expectancy {stats.get('expectancy_r', 'n/a')} "
                    f"over {stats.get('trade_count', 0)} trades"
                )

    if findings:
        lines.append("")
        lines.append("Mined mistake patterns (each with its measured impact):")
        for finding in findings:
            lines.append(
                f"- {finding.get('pattern')}: {finding.get('suggestion')} "
                f"(affects {finding.get('affected_count')} scenarios, "
                f"avg R impact {finding.get('average_r_impact')}, "
                f"confidence {finding.get('confidence')})"
            )

    lines.append("")
    lines.append(
        "Diagnose what these results suggest about the strategy, then propose a few "
        "experiments worth running to improve it. Be specific and tie each one to the "
        "numbers above."
    )
    return "\n".join(lines)


async def synthesize_advice(
    *,
    report: Mapping[str, Any],
    findings: Sequence[Mapping[str, Any]],
    enabled: bool,
    model: str,
    max_tokens: int,
    timeout_seconds: float,
    client: Any | None = None,
) -> ResearchAdvice | None:
    """Ask the configured Claude model to read a run and propose experiments.

    Returns the parsed :class:`ResearchAdvice`, or ``None`` whenever the advisor
    is unavailable or declines — the caller treats ``None`` as "no advice to
    show" and never as an error. ``client`` is injectable for tests; in
    production it is built from the environment when omitted.

    This is a best-effort, off-hot-path call: it must never raise into the
    research request, so every failure mode degrades to ``None`` with a log line.
    """
    if not enabled:
        return None

    module = _import_anthropic()
    if client is None:
        if module is None or not os.environ.get(_API_KEY_ENV):
            # The extra is not installed, or no credential is configured. Stay
            # silent — this is a normal, fail-safe state, not an error.
            logger.info("ai_advisor.unavailable")
            return None
        # AsyncAnthropic because the API path is async; the client timeout turns
        # a slow or hung call into a caught SDK error rather than a blocked request.
        client = module.AsyncAnthropic(timeout=timeout_seconds)

    # Catch the SDK's error base so any API/connection/timeout failure degrades
    # to None. The Exception fallback only applies when a fake client is injected
    # without the SDK installed (tests always install it); it is logged, never
    # swallowed silently (CLAUDE.md: no bare except).
    error_types: tuple[type[BaseException], ...] = (
        (module.APIError,) if module is not None else (Exception,)
    )
    try:
        response = await client.messages.parse(
            model=model,
            max_tokens=max_tokens,
            thinking={"type": "adaptive"},
            system=_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": _build_prompt(report, findings)}],
            output_format=ResearchAdvice,
        )
    except error_types as exc:
        logger.warning("ai_advisor.call_failed", extra={"error": str(exc)})
        return None

    if getattr(response, "stop_reason", None) == "refusal":
        logger.info("ai_advisor.refused")
        return None

    advice = getattr(response, "parsed_output", None)
    if not isinstance(advice, ResearchAdvice):
        # Structured outputs should guarantee a valid object; a None here means
        # the model hit a token limit or the parse failed. Show nothing rather
        # than a half-formed object.
        logger.info("ai_advisor.unparsed")
        return None
    return advice
