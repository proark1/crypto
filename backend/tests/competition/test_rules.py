"""Custom-bot recipes: validation is loud, names are stable, builds are real."""

import pytest

from tradebot.competition import (
    build_rules_strategy,
    describe_rules,
    slugify_bot_label,
    validate_rules,
)


class TestValidateRules:
    def test_normalizes_to_complete_typed_params(self) -> None:
        rules = validate_rules({"families": {"trend_following": {"fast_ema_period": 10}}})
        assert rules["entry_mode"] == "any"  # the default
        params = rules["families"]["trend_following"]
        assert params["fast_ema_period"] == 10
        assert params["slow_ema_period"] == 50  # defaults are materialized

    def test_unknown_family_is_refused(self) -> None:
        with pytest.raises(ValueError, match="unknown strategy family"):
            validate_rules({"families": {"martingale": {}}})

    def test_typo_parameter_is_refused(self) -> None:
        with pytest.raises(ValueError, match="unknown trend_following parameters"):
            validate_rules({"families": {"trend_following": {"fast_ema_perod": 10}}})

    def test_empty_recipe_is_refused(self) -> None:
        with pytest.raises(ValueError, match="at least one rule"):
            validate_rules({"families": {}})

    def test_bad_entry_mode_is_refused(self) -> None:
        with pytest.raises(ValueError, match="entry_mode"):
            validate_rules({"entry_mode": "most", "families": {"momentum": {}}})

    def test_unknown_top_level_field_is_refused(self) -> None:
        with pytest.raises(ValueError, match="unknown rule fields"):
            validate_rules({"familes": {"momentum": {}}})


class TestNaming:
    def test_slug_is_prefixed_and_stable(self) -> None:
        assert slugify_bot_label("My Dip Buyer!") == "custom-my-dip-buyer"

    def test_blank_name_is_refused(self) -> None:
        with pytest.raises(ValueError, match="needs a name"):
            slugify_bot_label("!!!")

    def test_description_speaks_plain_words(self) -> None:
        rules = validate_rules({"entry_mode": "all", "families": {"momentum": {}, "breakout": {}}})
        description = describe_rules(rules)
        assert "all rules agree" in description
        assert description.endswith(".")


class TestBuild:
    def test_single_family_builds_that_family(self) -> None:
        rules = validate_rules({"families": {"momentum": {}}})
        assert build_rules_strategy(rules).name == "momentum"

    def test_multiple_families_build_a_composite(self) -> None:
        rules = validate_rules({"entry_mode": "all", "families": {"momentum": {}, "breakout": {}}})
        strategy = build_rules_strategy(rules)
        assert strategy.name.startswith("composite[all:")
