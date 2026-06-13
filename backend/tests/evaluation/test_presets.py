"""Bake-off contestants: every energy preset must be a buildable strategy."""

from tradebot.competition.lineup import PRODUCTION_BOT_ID
from tradebot.evaluation.presets import (
    BAKE_OFF_CONTESTANTS,
    ENERGY_PRESETS,
    contestant_for,
)
from tradebot.evaluation.sweep import SweepCandidate, build_candidate_strategy


class TestRoster:
    def test_ten_energy_presets_plus_the_production_baseline(self) -> None:
        assert len(ENERGY_PRESETS) == 10
        assert len(BAKE_OFF_CONTESTANTS) == 11
        # The baseline leads (the comparison's baseline slot) and names no
        # family — the worker builds the regime router for it.
        assert BAKE_OFF_CONTESTANTS[0].bot_id == PRODUCTION_BOT_ID
        assert BAKE_OFF_CONTESTANTS[0].family is None

    def test_contestant_ids_are_unique(self) -> None:
        ids = [c.bot_id for c in BAKE_OFF_CONTESTANTS]
        assert len(set(ids)) == len(ids)

    def test_every_family_appears_at_two_energies(self) -> None:
        families = [c.family for c in ENERGY_PRESETS]
        for family in ("trend_following", "mean_reversion", "breakout", "momentum", "squeeze"):
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
