"""Bake-off contestants: every energy preset must be a buildable strategy."""

import pytest

from tradebot.competition.lineup import PRODUCTION_BOT_ID
from tradebot.evaluation.presets import (
    BAKE_OFF_CONTESTANTS,
    CONTROL_CONTESTANTS,
    ENERGY_PRESETS,
    ENSEMBLE_CONTESTANTS,
    BakeOffContestant,
    _validate_contestant,
    contestant_for,
)
from tradebot.evaluation.sweep import STRATEGY_FAMILIES, SweepCandidate, build_candidate_strategy
from tradebot.strategies.controls import build_control_strategy


class TestRoster:
    def test_baseline_plus_presets_ensembles_and_controls(self) -> None:
        assert len(ENERGY_PRESETS) == 24
        assert len(ENSEMBLE_CONTESTANTS) == 2
        assert len(CONTROL_CONTESTANTS) == 1
        assert len(BAKE_OFF_CONTESTANTS) == (
            1 + len(ENERGY_PRESETS) + len(ENSEMBLE_CONTESTANTS) + len(CONTROL_CONTESTANTS)
        )
        # The baseline leads (the comparison's baseline slot) and names no
        # family — the worker builds the regime router for it.
        assert BAKE_OFF_CONTESTANTS[0].bot_id == PRODUCTION_BOT_ID
        assert BAKE_OFF_CONTESTANTS[0].family is None

    def test_contestant_ids_are_unique(self) -> None:
        ids = [c.bot_id for c in BAKE_OFF_CONTESTANTS]
        assert len(set(ids)) == len(ids)

    def test_every_price_family_appears_at_two_energies(self) -> None:
        # Everything is tested against everything: every price family in the
        # sweep registry competes at both energies. The non-price funding
        # family is excluded (inert without a funding series) and competes in
        # the live lineup instead.
        families = [c.family for c in ENERGY_PRESETS]
        price_families = set(STRATEGY_FAMILIES) - {"funding"}
        assert set(families) == price_families
        for family in price_families:
            assert families.count(family) == 2

    def test_each_preset_builds_a_valid_strategy(self) -> None:
        """The whole point of the validation: no preset can be unbuildable."""
        for contestant in ENERGY_PRESETS:
            assert contestant.family is not None
            candidate = SweepCandidate(
                name=contestant.bot_id,
                family=contestant.family,
                params=dict(contestant.params),
            )
            strategy = build_candidate_strategy(candidate)
            assert strategy.name == contestant.family

    def test_contestant_for_resolves_presets_and_the_baseline(self) -> None:
        assert contestant_for("trend_calm") is not None
        assert contestant_for(PRODUCTION_BOT_ID) is not None
        # A bot id that is not a contestant returns None so the worker's
        # evaluator factory can fall through to the lineup / custom paths.
        assert contestant_for("not_a_contestant") is None


class TestControlContestants:
    def test_each_control_builds_and_is_not_a_family(self) -> None:
        # Controls resolve like any contestant but build from the separate
        # control registry — never STRATEGY_FAMILIES — so they stay out of
        # sweeps and promotion.
        for contestant in CONTROL_CONTESTANTS:
            assert contestant.control is not None
            assert contestant.family is None
            assert contestant.control not in STRATEGY_FAMILIES
            assert contestant_for(contestant.bot_id) is contestant
            strategy = build_control_strategy(contestant.control, dict(contestant.params))
            assert strategy.name == contestant.control

    def test_the_random_entry_control_is_present(self) -> None:
        ids = [c.bot_id for c in CONTROL_CONTESTANTS]
        assert "random_entry" in ids

    def test_a_contestant_cannot_set_more_than_one_kind(self) -> None:
        # The resolver checks the kinds in order, so a multi-kind entry would
        # silently trade one and ignore the rest — caught at import.
        both = BakeOffContestant(
            bot_id="bad", label="bad", family="trend_following", control="random_entry"
        )
        with pytest.raises(ValueError, match="more than one"):
            _validate_contestant(both)
        family_and_recipe = BakeOffContestant(
            bot_id="bad2",
            label="bad2",
            family="trend_following",
            recipe={"entry_mode": "any", "families": {"momentum": {}}},
        )
        with pytest.raises(ValueError, match="more than one"):
            _validate_contestant(family_and_recipe)


class TestEnsembleContestants:
    def test_each_ensemble_builds_the_composite_it_describes(self) -> None:
        # Ensembles resolve like any contestant but build the multi-family
        # composite a custom bot would — research-only, never routed (§13.7).
        for contestant in ENSEMBLE_CONTESTANTS:
            assert contestant.recipe is not None
            assert contestant.family is None and contestant.control is None
            assert contestant_for(contestant.bot_id) is contestant
            strategy = build_candidate_strategy(
                SweepCandidate(name=contestant.bot_id, recipe=dict(contestant.recipe))
            )
            # A multi-family recipe builds a composite; its name lists members.
            assert strategy.name.startswith("composite[")

    def test_ensembles_cover_both_entry_modes(self) -> None:
        modes = {c.recipe["entry_mode"] for c in ENSEMBLE_CONTESTANTS if c.recipe is not None}
        assert modes == {"any", "all"}
