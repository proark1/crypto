"""Custom-bot recipes: validation, naming, and strategy construction.

A recipe ("rules") is what the bot-builder UI submits: which strategy
families the bot trades, optional parameter overrides per family, and
how entries combine when several families are picked (``any`` = first
buy wins, ``all`` = every family must agree on the same candle). It is
validated loudly here — a typo'd family or parameter must fail the
create call, never produce a bot that silently trades defaults.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from tradebot.evaluation.sweep import STRATEGY_FAMILIES, validate_family_params
from tradebot.strategies import CompositeStrategy, Strategy

CUSTOM_BOT_PREFIX = "custom-"
"""Every user bot's id starts with this, so a custom id can never collide
with a built-in lineup id (or a future one)."""

ENTRY_MODES = ("any", "all")

FAMILY_DESCRIPTIONS: Mapping[str, str] = {
    "trend_following": "buys when a fast average crosses above a slow one (a trend starting)",
    "mean_reversion": "buys oversold dips that start recovering, sells once price normalizes",
    "breakout": "buys when price breaks above its recent range",
    "momentum": "buys when upward momentum accelerates (MACD turns bullish)",
}
"""Plain-words one-liners per family, shared by the builder UI and the
generated bot descriptions — one copy, no drift."""


def slugify_bot_label(label: str) -> str:
    """Derive the permanent bot id from a display name.

    Ids are forever (journals are keyed by them), so this is intentionally
    boring and stable: lowercase, alphanumerics and dashes, prefixed.
    Raises ``ValueError`` when nothing usable remains.
    """
    slug = re.sub(r"[^a-z0-9]+", "-", label.strip().lower()).strip("-")
    if not slug:
        raise ValueError("the bot needs a name with at least one letter or digit")
    return f"{CUSTOM_BOT_PREFIX}{slug[:40]}"


def validate_rules(rules: Mapping[str, Any]) -> dict[str, Any]:
    """Normalize and validate a recipe; raises ``ValueError`` loudly.

    Returns ``{"entry_mode": ..., "families": {family: {params}}}`` with
    every family and parameter checked against the real config models.
    """
    unknown_keys = set(rules) - {"entry_mode", "families"}
    if unknown_keys:
        raise ValueError(f"unknown rule fields: {sorted(unknown_keys)}")
    entry_mode = rules.get("entry_mode", "any")
    if entry_mode not in ENTRY_MODES:
        raise ValueError(f"entry_mode must be one of {ENTRY_MODES}, got {entry_mode!r}")
    families = rules.get("families")
    if not isinstance(families, Mapping) or not families:
        raise ValueError(
            f"pick at least one rule: families must map a known strategy family "
            f"({sorted(STRATEGY_FAMILIES)}) to its parameter overrides"
        )
    normalized: dict[str, dict[str, Any]] = {}
    for family, params in families.items():
        if not isinstance(params, Mapping):
            raise ValueError(f"{family} parameters must be an object, got {type(params).__name__}")
        validate_family_params(family, params)
        # Round-trip through the config model so stored rules are complete
        # and typed — the bot detail page shows exactly what will trade.
        config_model, _ = STRATEGY_FAMILIES[family]
        normalized[family] = config_model(**params).model_dump(mode="json")
    return {"entry_mode": entry_mode, "families": normalized}


def describe_rules(rules: Mapping[str, Any]) -> str:
    """One plain-words sentence for the UI, built from the recipe."""
    families: list[str] = list(rules.get("families", {}))
    parts: list[str] = [FAMILY_DESCRIPTIONS.get(family, family) for family in families]
    if len(parts) == 1:
        sentence = parts[0]
    else:
        joiner = " AND " if rules.get("entry_mode") == "all" else ", or "
        sentence = joiner.join(parts)
        sentence = (
            f"enters only when all rules agree: {sentence}"
            if rules.get("entry_mode") == "all"
            else f"enters when any rule fires: {sentence}"
        )
    return sentence[0].upper() + sentence[1:] + "."


def build_rules_strategy(rules: Mapping[str, Any]) -> Strategy:
    """One fresh, unscoped strategy instance for a validated recipe.

    Callers scope it per bot (see ``ScopedSignalStrategy``); scenarios use
    it bare, like every other research strategy.
    """
    members: list[Strategy] = []
    for family, params in rules["families"].items():
        config_model, strategy_constructor = STRATEGY_FAMILIES[family]
        members.append(strategy_constructor(config_model(**params)))
    if len(members) == 1:
        return members[0]
    return CompositeStrategy(members, require_all_entries=rules["entry_mode"] == "all")
