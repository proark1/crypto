"""§13.7 routing-candidacy logic: the three conditions, flagged not flipped."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from tradebot.competition.candidacy import (
    RESEARCH_FAMILIES,
    CandidacyEvidence,
    ComparisonOutcome,
    assemble_candidacies,
    best_regime,
    evaluate_candidacy,
)

NOW = datetime(2026, 6, 13, tzinfo=UTC)


# ``Any`` so a single override forwards cleanly to ``dataclasses.replace``;
# the field names are checked by replace against the frozen dataclass.
def full_evidence(**overrides: Any) -> CandidacyEvidence:
    """Evidence that meets all three conditions; override one to break it."""
    base = CandidacyEvidence(
        family="breakout",
        has_validated_sweep=True,
        edge_regime="breakout",
        edge_regime_expectancy_r=Decimal("0.4"),
        comparisons=(
            ComparisonOutcome(NOW - timedelta(weeks=5), Decimal("0.5"), Decimal("0.2")),
            ComparisonOutcome(NOW - timedelta(weeks=1), Decimal("0.3"), Decimal("0.1")),
        ),
        live_return_fraction=Decimal("0.05"),
        live_started_at=NOW - timedelta(weeks=9),
        live_breaker_tripped=False,
    )
    return replace(base, **overrides)


class TestFullCandidacy:
    def test_all_three_conditions_met_is_a_candidate(self) -> None:
        candidacy = evaluate_candidacy(full_evidence(), NOW)
        assert candidacy.validated_edge.met
        assert candidacy.beats_incumbent.met
        assert candidacy.live_paper.met
        assert candidacy.is_candidate is True
        assert "breakout" in candidacy.validated_edge.detail


class TestValidatedEdge:
    def test_no_validated_sweep_blocks(self) -> None:
        candidacy = evaluate_candidacy(full_evidence(has_validated_sweep=False), NOW)
        assert not candidacy.validated_edge.met
        assert not candidacy.is_candidate

    def test_validated_but_no_regime_bucket_blocks(self) -> None:
        candidacy = evaluate_candidacy(
            full_evidence(edge_regime=None, edge_regime_expectancy_r=None), NOW
        )
        assert not candidacy.validated_edge.met

    def test_validated_but_best_regime_not_positive_blocks(self) -> None:
        candidacy = evaluate_candidacy(full_evidence(edge_regime_expectancy_r=Decimal("-0.1")), NOW)
        assert not candidacy.validated_edge.met
        assert "not positive" in candidacy.validated_edge.detail


class TestBeatsIncumbent:
    def test_one_win_is_not_enough(self) -> None:
        one_win = (ComparisonOutcome(NOW - timedelta(weeks=3), Decimal("0.5"), Decimal("0.2")),)
        candidacy = evaluate_candidacy(full_evidence(comparisons=one_win), NOW)
        assert not candidacy.beats_incumbent.met

    def test_losing_comparisons_do_not_count(self) -> None:
        # Two batches, but the family lost the incumbent in both.
        losses = (
            ComparisonOutcome(NOW - timedelta(weeks=5), Decimal("0.1"), Decimal("0.4")),
            ComparisonOutcome(NOW - timedelta(weeks=1), Decimal("0.2"), Decimal("0.3")),
        )
        candidacy = evaluate_candidacy(full_evidence(comparisons=losses), NOW)
        assert not candidacy.beats_incumbent.met

    def test_two_wins_too_close_together_block(self) -> None:
        # Both wins within three days — one observation, not two weeks apart.
        close = (
            ComparisonOutcome(NOW - timedelta(days=4), Decimal("0.5"), Decimal("0.2")),
            ComparisonOutcome(NOW - timedelta(days=1), Decimal("0.3"), Decimal("0.1")),
        )
        candidacy = evaluate_candidacy(full_evidence(comparisons=close), NOW)
        assert not candidacy.beats_incumbent.met
        assert "apart" in candidacy.beats_incumbent.detail

    def test_two_wins_spread_apart_pass(self) -> None:
        assert evaluate_candidacy(full_evidence(), NOW).beats_incumbent.met

    def test_a_tie_is_not_a_win(self) -> None:
        outcome = ComparisonOutcome(NOW, Decimal("0.2"), Decimal("0.2"))
        assert outcome.family_won is False

    def test_an_ungraded_side_is_not_a_win(self) -> None:
        assert ComparisonOutcome(NOW, None, Decimal("0.2")).family_won is False
        assert ComparisonOutcome(NOW, Decimal("0.5"), None).family_won is False


class TestLivePaper:
    def test_breaker_trip_blocks(self) -> None:
        candidacy = evaluate_candidacy(full_evidence(live_breaker_tripped=True), NOW)
        assert not candidacy.live_paper.met
        assert "breaker" in candidacy.live_paper.detail

    def test_non_positive_return_blocks(self) -> None:
        candidacy = evaluate_candidacy(full_evidence(live_return_fraction=Decimal("-0.01")), NOW)
        assert not candidacy.live_paper.met

    def test_too_short_a_soak_blocks_and_reports_progress(self) -> None:
        candidacy = evaluate_candidacy(full_evidence(live_started_at=NOW - timedelta(weeks=4)), NOW)
        assert not candidacy.live_paper.met
        assert "4 of 8 weeks" in candidacy.live_paper.detail

    def test_never_started_blocks(self) -> None:
        candidacy = evaluate_candidacy(full_evidence(live_started_at=None), NOW)
        assert not candidacy.live_paper.met


class TestBestRegime:
    def test_picks_the_highest_expectancy_archetype(self) -> None:
        pairs = [("chop", Decimal("-0.2")), ("breakout", Decimal("0.6")), ("bull", Decimal("0.1"))]
        assert best_regime(pairs) == ("breakout", Decimal("0.6"))

    def test_empty_is_none(self) -> None:
        assert best_regime([]) == (None, None)


class TestAssembly:
    def test_one_candidacy_per_research_family_and_empty_record_is_no_candidate(self) -> None:
        result = assemble_candidacies(
            sweeps=[], runs=[], comparisons=[], competition=[], started_at={}, now=NOW
        )
        assert [c.family for c in result] == list(RESEARCH_FAMILIES)
        assert all(not c.is_candidate for c in result)

    def test_extracts_all_three_conditions_from_the_record(self) -> None:
        sweeps = [
            {
                "report": {"verdict": "validated", "winner": "wider_channel"},
                "config": {
                    "candidates": [
                        {"name": "active_breakout", "family": "breakout"},
                        {"name": "wider_channel", "family": "breakout"},
                    ]
                },
            }
        ]
        runs = [
            {
                "strategy": "breakout",
                "status": "completed",
                "summary": {
                    "by_archetype": {
                        "breakout": {"expectancy_r": "0.6"},
                        "chop": {"expectancy_r": "-0.2"},
                    }
                },
            }
        ]
        comparisons = [
            [
                _crun("production", "0.1", NOW - timedelta(weeks=5)),
                _crun("breakout", "0.4", NOW - timedelta(weeks=5)),
            ],
            [
                _crun("production", "0.1", NOW - timedelta(weeks=1)),
                _crun("breakout", "0.3", NOW - timedelta(weeks=1)),
            ],
        ]
        competition = [
            {
                "bot_id": "breakout",
                "return_fraction": Decimal("0.05"),
                "breaker_tripped_reason": None,
            }
        ]
        started_at = {"breakout": NOW - timedelta(weeks=9)}

        result = assemble_candidacies(
            sweeps=sweeps,
            runs=runs,
            comparisons=comparisons,
            competition=competition,
            started_at=started_at,
            now=NOW,
        )
        breakout = next(c for c in result if c.family == "breakout")
        assert breakout.validated_edge.met
        assert "breakout" in breakout.validated_edge.detail  # the best regime named
        assert breakout.beats_incumbent.met
        assert breakout.live_paper.met
        assert breakout.is_candidate

    def test_a_validated_sweep_with_a_losing_best_regime_is_not_an_edge(self) -> None:
        sweeps = [
            {
                "report": {"verdict": "validated", "winner": "w"},
                "config": {"candidates": [{"name": "w", "family": "momentum"}]},
            }
        ]
        runs = [
            {
                "strategy": "momentum",
                "status": "completed",
                "summary": {"by_archetype": {"chop": {"expectancy_r": "-0.3"}}},
            }
        ]
        result = assemble_candidacies(
            sweeps=sweeps, runs=runs, comparisons=[], competition=[], started_at={}, now=NOW
        )
        momentum = next(c for c in result if c.family == "momentum")
        assert not momentum.validated_edge.met

    def test_a_tripped_breaker_blocks_the_live_condition(self) -> None:
        competition = [
            {
                "bot_id": "squeeze",
                "return_fraction": Decimal("0.1"),
                "breaker_tripped_reason": "daily loss",
            }
        ]
        result = assemble_candidacies(
            sweeps=[],
            runs=[],
            comparisons=[],
            competition=competition,
            started_at={"squeeze": NOW - timedelta(weeks=10)},
            now=NOW,
        )
        squeeze = next(c for c in result if c.family == "squeeze")
        assert not squeeze.live_paper.met
        assert "breaker" in squeeze.live_paper.detail


def _crun(strategy: str, expectancy_r: str, created_at: datetime) -> dict[str, object]:
    """A comparison-batch run row: completed, with an expectancy and a time."""
    return {
        "strategy": strategy,
        "status": "completed",
        "summary": {"expectancy_r": expectancy_r},
        "created_at": created_at,
    }
