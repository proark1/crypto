"""§13.7 routing candidacy: flag when a research family has earned the evidence.

Routing a research family (breakout, momentum, squeeze) into the production
regime router is the architecture decision ARCHITECTURE.md §13.7 deliberately
reserves for a human. This module never makes that decision — it reads the
accumulated record and reports, per research family, whether the three
evidence conditions the gate names are met yet:

1. a statistically **validated** out-of-sample edge (§12.5 bar) concentrated
   in an identifiable regime bucket (the archetype the router would activate
   it in), not a diffuse average;
2. it **beats the incumbent** router *in that regime's scenarios* — the
   family's expectancy in the edge regime bucket clears the incumbent's on
   byte-identical comparison scenarios, across at least two separate batches
   run weeks apart (a whole-run average could hide a regime where it loses);
3. **live paper evidence**: its solo competition account is positive over at
   least eight weeks without tripping its own circuit breakers.

Conditions 2 and 3 lean on the regime identified by condition 1: condition 2
reads the comparison runs' §12.3 ``by_archetype`` expectancy for that regime
rather than the blended top-line. (The live soak of condition 3 stays an
overall-account check — its return is not bucketed by regime; §13.7 records
that as the measurable form of the soak.)

Meeting all three flags a *candidacy*; a human still decides which regime
activates the family, at whose expense, and whether the added router
complexity is worth it. **Flag, never flip.** Every threshold below is frozen
like the §12.2 scoring constants — moving the bar changes which families look
ready — so it changes only by an explicit amendment.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

RESEARCH_FAMILIES: tuple[str, ...] = ("breakout", "momentum", "squeeze")
"""The families §13.7 governs: research-traded solo, unrouted in production
until this gate is met and a human routes them. ``trend_following`` and
``mean_reversion`` are already the production router's incumbents."""

MIN_EDGE_EXPECTANCY_R = Decimal("0")
"""A regime bucket must clear this expectancy to count as the family's edge —
strictly positive: a validated sweep whose best regime still loses money is
not a reason to route."""

MIN_WINNING_COMPARISONS = 2
"""How many separate comparison batches the family must beat the incumbent in
(§13.7: "at least two separate comparison batches")."""

COMPARISON_MIN_SPAN = timedelta(weeks=2)
"""The winning comparison batches must span at least this long ("run weeks
apart") — two wins on the same afternoon are one observation, not two."""

LIVE_PAPER_MIN = timedelta(weeks=8)
"""Minimum live-paper soak for the third condition (§13.7: "at least eight
weeks of live paper trading")."""

# How far back the candidacy read reaches. §13.7 evidence accumulates over
# months (an 8-week soak; comparison batches "weeks apart"), so the assembly
# must look past the store's small default page sizes — otherwise a qualifying
# sweep, run, or comparison batch silently scrolls out of view as newer
# research piles up and the flag flickers off. Generous but bounded: the read
# is on-demand (a control-plane GET), never the candle hot path.
EVIDENCE_SWEEP_LIMIT = 500
EVIDENCE_RUN_LIMIT = 500
EVIDENCE_COMPARISON_LIMIT = 200


@dataclass(frozen=True)
class ComparisonOutcome:
    """One comparison batch's family-vs-incumbent expectancy on identical scenarios."""

    decided_at: datetime
    family_expectancy_r: Decimal | None
    incumbent_expectancy_r: Decimal | None

    @property
    def family_won(self) -> bool:
        """True when both sides graded trades and the family's was the higher."""
        return (
            self.family_expectancy_r is not None
            and self.incumbent_expectancy_r is not None
            and self.family_expectancy_r > self.incumbent_expectancy_r
        )


@dataclass(frozen=True)
class CandidacyEvidence:
    """The assembled record for one family — pure inputs, fetched by the API."""

    family: str
    has_validated_sweep: bool
    edge_regime: str | None
    edge_regime_expectancy_r: Decimal | None
    comparisons: tuple[ComparisonOutcome, ...]
    live_return_fraction: Decimal | None
    live_started_at: datetime | None
    live_breaker_tripped: bool


@dataclass(frozen=True)
class Condition:
    """One gate condition: whether it is met, and a plain-words reason."""

    met: bool
    detail: str


@dataclass(frozen=True)
class RoutingCandidacy:
    """The §13.7 verdict for one family: three conditions, flagged not flipped."""

    family: str
    validated_edge: Condition
    beats_incumbent: Condition
    live_paper: Condition

    @property
    def is_candidate(self) -> bool:
        """A routing *candidate* only when all three conditions hold."""
        return self.validated_edge.met and self.beats_incumbent.met and self.live_paper.met


def evaluate_candidacy(evidence: CandidacyEvidence, now: datetime) -> RoutingCandidacy:
    """Grade the three §13.7 conditions from assembled evidence.

    Pure and deterministic given ``now``; the API fetches the evidence and
    renders the result. Never mutates anything, never routes anything.
    """
    return RoutingCandidacy(
        family=evidence.family,
        validated_edge=_validated_edge(evidence),
        beats_incumbent=_beats_incumbent(evidence),
        live_paper=_live_paper(evidence, now),
    )


def _validated_edge(evidence: CandidacyEvidence) -> Condition:
    if not evidence.has_validated_sweep:
        return Condition(False, "no statistically validated sweep yet (the §12.5 bar)")
    if evidence.edge_regime is None or evidence.edge_regime_expectancy_r is None:
        return Condition(
            False, "validated in a sweep, but no positive regime bucket identified yet"
        )
    if evidence.edge_regime_expectancy_r <= MIN_EDGE_EXPECTANCY_R:
        return Condition(
            False,
            f"validated, but its best regime ({evidence.edge_regime}) is not positive "
            f"({_r(evidence.edge_regime_expectancy_r)}R)",
        )
    return Condition(
        True,
        f"validated edge concentrated in {evidence.edge_regime} "
        f"(+{_r(evidence.edge_regime_expectancy_r)}R)",
    )


def _beats_incumbent(evidence: CandidacyEvidence) -> Condition:
    wins = sorted(outcome.decided_at for outcome in evidence.comparisons if outcome.family_won)
    if len(wins) < MIN_WINNING_COMPARISONS:
        return Condition(
            False,
            f"beat the incumbent in {len(wins)} of the needed {MIN_WINNING_COMPARISONS} "
            "comparison batches",
        )
    span = wins[-1] - wins[0]
    if span < COMPARISON_MIN_SPAN:
        return Condition(
            False,
            f"beat the incumbent {len(wins)} times, but only across {_weeks(span)} weeks — "
            f"the batches must be spread ≥ {_weeks(COMPARISON_MIN_SPAN)} weeks apart",
        )
    return Condition(
        True,
        f"beat the incumbent in {len(wins)} comparison batches spanning {_weeks(span)} weeks",
    )


def _live_paper(evidence: CandidacyEvidence, now: datetime) -> Condition:
    reasons: list[str] = []
    if evidence.live_breaker_tripped:
        reasons.append("a circuit breaker tripped")
    if not (evidence.live_return_fraction is not None and evidence.live_return_fraction > 0):
        reasons.append("live paper return is not positive")
    elapsed = now - evidence.live_started_at if evidence.live_started_at is not None else None
    if elapsed is None or elapsed < LIVE_PAPER_MIN:
        have = _weeks(elapsed) if elapsed is not None else "0"
        reasons.append(f"only {have} of {_weeks(LIVE_PAPER_MIN)} weeks of live paper")
    if reasons:
        return Condition(False, "; ".join(reasons))
    # No reasons means the soak cleared the minimum, so elapsed is set.
    assert elapsed is not None
    return Condition(
        True,
        f"positive over {_weeks(elapsed)} weeks of live paper with no breaker trips",
    )


def _weeks(span: timedelta) -> str:
    """Whole-week count of a span for plain-words detail (floored, never negative)."""
    return str(max(0, span.days // 7))


def _r(value: Decimal) -> str:
    """Four-decimal R for the detail strings, matching the report tables."""
    return str(value.quantize(Decimal("0.0001")))


def best_regime(
    archetype_expectancies: Sequence[tuple[str, Decimal]],
) -> tuple[str | None, Decimal | None]:
    """Pick the archetype with the highest expectancy (the edge's regime).

    ``archetype_expectancies`` is ``(archetype, expectancy_r)`` pairs pooled
    from the family's completed runs; the best one names the regime the router
    would activate the family in. Empty input (the family never traded a
    labeled archetype) yields ``(None, None)``.
    """
    best: tuple[str | None, Decimal | None] = (None, None)
    for archetype, expectancy in archetype_expectancies:
        if best[1] is None or expectancy > best[1]:
            best = (archetype, expectancy)
    return best


# --- Assembly from the persisted record (pure over store-shaped dicts) -------
#
# The API fetches sweeps, evaluation runs, comparison batches, and the
# competition leaderboard, plus each family's account start, and hands them
# here as plain dicts. Keeping the extraction pure (no store, no I/O) is what
# makes the §13.7 logic testable end to end without a database.


def assemble_candidacies(
    *,
    sweeps: Sequence[Mapping[str, Any]],
    runs: Sequence[Mapping[str, Any]],
    comparisons: Sequence[Sequence[Mapping[str, Any]]],
    competition: Sequence[Mapping[str, Any]],
    started_at: Mapping[str, datetime | None],
    now: datetime,
) -> list[RoutingCandidacy]:
    """Build and grade a candidacy for every research family from the record."""
    competition_by_bot = {row["bot_id"]: row for row in competition}
    return [
        evaluate_candidacy(
            _evidence_for(family, sweeps, runs, comparisons, competition_by_bot, started_at),
            now,
        )
        for family in RESEARCH_FAMILIES
    ]


def _evidence_for(
    family: str,
    sweeps: Sequence[Mapping[str, Any]],
    runs: Sequence[Mapping[str, Any]],
    comparisons: Sequence[Sequence[Mapping[str, Any]]],
    competition_by_bot: Mapping[str, Mapping[str, Any]],
    started_at: Mapping[str, datetime | None],
) -> CandidacyEvidence:
    regime, expectancy = best_regime(_family_archetype_expectancies(runs, family))
    row = competition_by_bot.get(family, {})
    return CandidacyEvidence(
        family=family,
        has_validated_sweep=_has_validated_sweep(sweeps, family),
        edge_regime=regime,
        edge_regime_expectancy_r=expectancy,
        comparisons=_comparison_outcomes(comparisons, family, regime),
        live_return_fraction=_as_decimal(row.get("return_fraction")),
        live_started_at=started_at.get(family),
        live_breaker_tripped=row.get("breaker_tripped_reason") is not None,
    )


def _has_validated_sweep(sweeps: Sequence[Mapping[str, Any]], family: str) -> bool:
    """Return whether any sweep statistically validated a winner of this family (§12.5)."""
    for sweep in sweeps:
        report = sweep.get("report") or {}
        if report.get("verdict") != "validated":
            continue
        winner = report.get("winner")
        if winner is not None and _winning_family(sweep.get("config") or {}, winner) == family:
            return True
    return False


def _winning_family(config: Mapping[str, Any], winner_name: str) -> str | None:
    for candidate in config.get("candidates") or []:
        if candidate.get("name") == winner_name:
            family = candidate.get("family")
            return family if isinstance(family, str) else None
    return None


def _family_archetype_expectancies(
    runs: Sequence[Mapping[str, Any]], family: str
) -> list[tuple[str, Decimal]]:
    """Mean expectancy per archetype across the family's completed solo runs.

    Averaging across runs (rather than taking one lucky run's best bucket)
    is what "concentrated in a regime", not "a one-run fluke", asks for.
    """
    by_archetype: dict[str, list[Decimal]] = defaultdict(list)
    for run in runs:
        if run.get("strategy") != family or run.get("status") != "completed":
            continue
        summary = run.get("summary") or {}
        for archetype, block in (summary.get("by_archetype") or {}).items():
            expectancy = _as_decimal((block or {}).get("expectancy_r"))
            if expectancy is not None:
                by_archetype[archetype].append(expectancy)
    return [
        (archetype, sum(values, Decimal(0)) / Decimal(len(values)))
        for archetype, values in by_archetype.items()
    ]


def _comparison_outcomes(
    comparisons: Sequence[Sequence[Mapping[str, Any]]], family: str, regime: str | None
) -> tuple[ComparisonOutcome, ...]:
    """One outcome per batch that graded both the family and the incumbent.

    Each side's expectancy is read in the edge ``regime`` (condition 1's
    bucket), so the head-to-head is "beats the incumbent *in that regime*",
    not on a blended whole-run average. A batch whose scenarios never visited
    the regime — the bucket is absent on a side — yields ``None`` there, so it
    cannot count as a win (``ComparisonOutcome.family_won`` needs both sides).
    """
    outcomes: list[ComparisonOutcome] = []
    for batch in comparisons:
        incumbent = _run_named(batch, "production")
        challenger = _run_named(batch, family)
        if incumbent is None or challenger is None:
            continue
        decided_at = challenger.get("created_at") or incumbent.get("created_at")
        if not isinstance(decided_at, datetime):
            continue
        outcomes.append(
            ComparisonOutcome(
                decided_at=decided_at,
                family_expectancy_r=_run_expectancy(challenger, regime),
                incumbent_expectancy_r=_run_expectancy(incumbent, regime),
            )
        )
    return tuple(outcomes)


def _run_named(batch: Sequence[Mapping[str, Any]], strategy: str) -> Mapping[str, Any] | None:
    return next((run for run in batch if run.get("strategy") == strategy), None)


def _run_expectancy(run: Mapping[str, Any], regime: str | None) -> Decimal | None:
    """Return a completed run's expectancy in the edge ``regime``'s bucket.

    ``None`` when the run did not complete, when no edge regime was identified
    (condition 1 has already failed in that case), or when the run's
    ``by_archetype`` breakdown has no entry for the regime (its scenarios
    never visited it) — never a silent fall back to the blended top-line.
    """
    if run.get("status") != "completed" or regime is None:
        return None
    by_archetype = (run.get("summary") or {}).get("by_archetype") or {}
    return _as_decimal((by_archetype.get(regime) or {}).get("expectancy_r"))


def _as_decimal(value: object) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
