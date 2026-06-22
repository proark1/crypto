"""Performance-weighted research allocation (ARCHITECTURE.md §12.7).

The §12.7 loops (`AutoImprover`, `CampaignDriver`) share one serial,
CPU-shared research lane. The default autonomous target set is now
``production`` only, based on production evidence that broad solo-family
rotation spent most cycles on weak standalone edges. This selector remains
the general scheduler for any configured target set, including explicit
diagnostic runs that add solo families back in.

For multi-target use, this module re-spends that lane by *standing*. Each
pass it reads the live competition standing for every target and sorts them
into three tiers:

* **boosted** — a family the evidence likes: a §13.7 routing candidate, or a
  live-paper account that is *up* with enough trades to trust the sign. It
  earns extra research turns, so the lane sharpens what is working.
* **normal** — not enough evidence yet (few trades, near-breakeven). One turn,
  as before.
* **parked** — a family the evidence has judged: *down* past a threshold with
  enough trades to be sure, and not a routing candidate. It is re-probed only
  once every few passes, freeing the lane — but never abandoned, because a
  dead family can come alive in a new regime and the re-probe will notice.

Two invariants make this safe to run unattended:

1. ``production`` (the live bot's regime-routed shape) is **protected**: never
   parked, always at least a normal turn. The thing we trade most is the thing
   we keep researching.
2. Parking only ever changes *where the research lane spends time*. It moves no
   money, pauses no account, and deletes no evidence — a parked family keeps
   trading paper (cheaply) when it is part of the configured target set, so
   the standing that parked it stays current and the re-probe has fresh data.
   This is a scheduler, not a kill switch.

The policy is pure and deterministic given a standings snapshot, so it is
unit-tested with a fake reader (no worker, DB, or network). The worker builds
the snapshot from ``competition_snapshot`` + ``routing_candidacies`` and injects
the reader; the selector owns only the schedule.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any, Protocol

from tradebot.core.models import utc_now

logger = logging.getLogger(__name__)


PARK_RETURN_THRESHOLD = Decimal("-0.02")
"""A live-paper return at or below this (-2%) is a parking signal — *if* the
account also has enough trades (``PARK_MIN_TRADES``) for the sign to be real."""

PARK_MIN_TRADES = 20
"""Fills (entries + exits) a family needs before its return may park or boost
it. Below this the sample is too thin to judge; the family stays ``normal``."""

BOOST_RETURN_THRESHOLD = Decimal("0")
"""A live-paper return strictly above this (with ``PARK_MIN_TRADES`` trades)
boosts a family — it is making paper money, so the lane does more of it."""

BOOST_WEIGHT = 3
"""Research turns a boosted target gets per pass (a ``normal`` target gets 1).
Capped small: even a winning target should not starve the rest of the set."""

NORMAL_WEIGHT = 1
"""Research turns a normal target gets per pass."""

PARK_REPROBE_PERIOD = 5
"""A parked target is re-probed once every this many passes — frequent enough
to catch a regime change, rare enough to free the lane for the rest."""

PROTECTED_TARGETS = frozenset({"production"})
"""Targets that can never be parked and always get at least a normal turn. The
live bot's shape is researched every pass, however its paper account looks."""


class TargetSelector(Protocol):
    """The seam the §12.7 loops schedule through.

    The flat round-robin and ``PerformanceWeightedSelector`` both satisfy it,
    so a loop takes either without knowing which — and a fake satisfies it in
    tests. ``next_assignment`` is the per-turn pick; ``last_plan`` exposes the
    current pass's reasoning for the status surface.
    """

    async def next_assignment(self, symbols: Sequence[str]) -> tuple[str, str]:
        """Return the ``(target, symbol)`` to research next."""
        ...

    @property
    def last_plan(self) -> AllocationPlan | None:
        """The most recently computed pass plan, or ``None`` before the first."""
        ...


class AllocationTier(StrEnum):
    """Where a target sits this pass. A ``StrEnum`` so it serialises for the API."""

    BOOSTED = "boosted"
    NORMAL = "normal"
    PARKED = "parked"


@dataclass(frozen=True)
class TargetStanding:
    """One target's live evidence, the input the policy decides on.

    Built by the worker from the competition snapshot (``return_fraction``,
    ``trades``, ``breaker_tripped``, ``paused``) and the §13.7 candidacy
    verdicts (``is_candidate``). ``return_fraction`` is the account's
    ``(equity - initial) / initial`` and is ``None`` when equity is unknown
    (a held coin lacks a fresh mark) — an unknown return never parks or boosts.
    """

    target: str
    return_fraction: Decimal | None
    trades: int
    is_candidate: bool = False
    breaker_tripped: bool = False
    paused: bool = False


@dataclass(frozen=True)
class AllocationPlan:
    """An auditable snapshot of one pass's schedule, for status and logs.

    Published every time the selector rebuilds its ring so the operator can
    see *why* the lane is spending where it is — which targets the evidence
    boosted, which it parked, and on which symbol this pass runs.
    """

    computed_at: datetime
    symbol: str
    pass_index: int
    tiers: Mapping[str, AllocationTier]
    weights: Mapping[str, int]
    standings: Mapping[str, TargetStanding] = field(default_factory=dict)

    @property
    def boosted(self) -> tuple[str, ...]:
        """Targets boosted this pass, in target order."""
        return tuple(t for t, tier in self.tiers.items() if tier is AllocationTier.BOOSTED)

    @property
    def parked(self) -> tuple[str, ...]:
        """Targets parked this pass (re-probed only on re-probe passes)."""
        return tuple(t for t, tier in self.tiers.items() if tier is AllocationTier.PARKED)


def standings_from_competition(
    rows: Iterable[Mapping[str, Any]],
    candidate_families: Collection[str],
    targets: Sequence[str],
) -> dict[str, TargetStanding]:
    """Map competition leaderboard rows + §13.7 candidates to target standings.

    One ``TargetStanding`` per target, keyed and ordered by ``targets`` (target
    strings are competition ``bot_id``s, so the join is by equality). A target
    with no leaderboard row reports an unknown return — neither parks nor
    boosts. ``return_fraction`` passes through as-is (``Decimal | None``);
    ``trades`` sums entry and exit fills; the breaker and pause flags carry the
    account's live state. Pure, so it is unit-tested without a worker or DB.
    """
    by_id = {row["bot_id"]: row for row in rows}
    standings: dict[str, TargetStanding] = {}
    for target in targets:
        row = by_id.get(target)
        if row is None:
            standings[target] = TargetStanding(target=target, return_fraction=None, trades=0)
            continue
        standings[target] = TargetStanding(
            target=target,
            return_fraction=row.get("return_fraction"),
            trades=int(row.get("entry_fills", 0)) + int(row.get("exit_fills", 0)),
            is_candidate=target in candidate_families,
            breaker_tripped=row.get("breaker_tripped_reason") is not None,
            paused=bool(row.get("paused", False)),
        )
    return standings


def classify(standing: TargetStanding) -> AllocationTier:
    """Sort one target into its tier from its standing alone (pure).

    Protected targets (``production``) never park: they boost when up,
    otherwise stay normal. A research family boosts when the evidence likes it
    (a routing candidate, or up with enough trades) and parks when the evidence
    has judged it (down past the threshold with enough trades, and not a
    candidate). Everything else — too few trades, unknown return,
    near-breakeven — is normal: the loop keeps researching it at the base rate
    until the evidence says otherwise.
    """
    protected = standing.target in PROTECTED_TARGETS
    if standing.is_candidate:
        return AllocationTier.BOOSTED
    ret = standing.return_fraction
    judged = ret is not None and standing.trades >= PARK_MIN_TRADES
    if judged and ret is not None and ret > BOOST_RETURN_THRESHOLD:
        return AllocationTier.BOOSTED
    if not protected and judged and ret is not None and ret <= PARK_RETURN_THRESHOLD:
        return AllocationTier.PARKED
    return AllocationTier.NORMAL


def _pass_weights(
    targets: Sequence[str],
    standings: Mapping[str, TargetStanding],
    pass_index: int,
) -> tuple[dict[str, int], dict[str, AllocationTier]]:
    """Compute each target's turn count and tier for the pass at ``pass_index``.

    A parked target weighs 1 only on a re-probe pass (every
    ``PARK_REPROBE_PERIOD``), else 0; boosted weighs ``BOOST_WEIGHT``; normal
    weighs ``NORMAL_WEIGHT``. The tiers are returned alongside for the plan.
    """
    weights: dict[str, int] = {}
    tiers: dict[str, AllocationTier] = {}
    reprobe = pass_index % PARK_REPROBE_PERIOD == 0
    for target in targets:
        standing = standings.get(target) or TargetStanding(target, None, 0)
        tier = classify(standing)
        tiers[target] = tier
        if tier is AllocationTier.PARKED:
            weights[target] = 1 if reprobe else 0
        elif tier is AllocationTier.BOOSTED:
            weights[target] = BOOST_WEIGHT
        else:
            weights[target] = NORMAL_WEIGHT
    return weights, tiers


def _interleave(targets: Sequence[str], weights: Mapping[str, int]) -> list[str]:
    """Expand per-target weights into an interleaved ring (no bursts).

    Round-robin by weight rather than ``[t] * w`` concatenation: round 0 lists
    every target with weight ≥ 1, round 1 every target with weight ≥ 2, and so
    on. So a boosted target's extra turns are spread across the pass instead of
    running three of the same target back to back.
    """
    if not weights:
        return []
    max_weight = max(weights.values(), default=0)
    ring: list[str] = []
    for level in range(max_weight):
        for target in targets:
            if weights.get(target, 0) > level:
                ring.append(target)
    return ring


class PerformanceWeightedSelector:
    """Picks the next ``(target, symbol)`` weighted by live standing (§12.7).

    A drop-in for the loops' flat round-robin: same ``(target, symbol)``
    contract, but the target stream is the interleaved, standing-weighted ring
    rebuilt once per pass. Symbols advance one per pass, exactly as the flat
    scheme advanced them once per target cycle, so coverage of every symbol is
    preserved — only the *time spent per target* changes.

    Stateful across calls (ring, cursor, pass and symbol indices) but reads the
    world fresh each pass through the injected ``read_standings`` — a cycle
    always schedules on the standings as they are now. One selector per loop;
    the two loops are mutually exclusive on the lane, so they never contend.
    """

    def __init__(
        self,
        *,
        targets: Sequence[str],
        read_standings: Callable[[], Awaitable[Mapping[str, TargetStanding]]],
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        """Bind the selector to its target list and live standings reader.

        ``targets`` is the §12.7 target set (``IMPROVEMENT_TARGETS``);
        ``read_standings`` returns one ``TargetStanding`` per target and is
        awaited once per pass (cheap — a single competition snapshot), never
        per turn.
        """
        self._targets = tuple(targets)
        self._read_standings = read_standings
        self._clock = clock
        self._ring: list[str] = []
        self._ring_pos = 0
        self._pass_index = 0
        self._symbol_index = -1
        self._last_plan: AllocationPlan | None = None

    @property
    def last_plan(self) -> AllocationPlan | None:
        """The most recently computed pass plan, or ``None`` before the first."""
        return self._last_plan

    async def next_assignment(self, symbols: Sequence[str]) -> tuple[str, str]:
        """Return the ``(target, symbol)`` to research next.

        Rebuilds the weighted ring (and advances to the next symbol) whenever
        the current ring is exhausted; otherwise serves the next target from
        it. ``symbols`` must be non-empty — the loops guard that before calling.
        """
        if self._ring_pos >= len(self._ring):
            await self._rebuild(symbols)
        target = self._ring[self._ring_pos]
        self._ring_pos += 1
        symbol = symbols[self._symbol_index % len(symbols)]
        return target, symbol

    async def _rebuild(self, symbols: Sequence[str]) -> None:
        """Start a new pass: read standings, weight the ring, pick the symbol.

        A re-probe pass can leave the ring empty (every non-protected target
        parked and not due) — except ``production`` is protected, so the ring
        always holds at least it. The loop is therefore never starved.
        """
        standings = await self._read_standings()
        self._symbol_index += 1
        symbol = symbols[self._symbol_index % len(symbols)]
        weights, tiers = _pass_weights(self._targets, standings, self._pass_index)
        ring = _interleave(self._targets, weights)
        if not ring:
            # Defensive: protected targets guarantee a non-empty ring, but if a
            # caller ever drops them, fall back to a flat pass rather than spin.
            ring = list(self._targets)
        self._ring = ring
        self._ring_pos = 0
        plan = AllocationPlan(
            computed_at=self._clock(),
            symbol=symbol,
            pass_index=self._pass_index,
            tiers=tiers,
            weights=weights,
            standings=dict(standings),
        )
        self._last_plan = plan
        self._pass_index += 1
        if plan.boosted or plan.parked:
            logger.info(
                "research allocation pass %d on %s: boosted=%s parked=%s",
                plan.pass_index,
                symbol,
                ",".join(plan.boosted) or "none",
                ",".join(plan.parked) or "none",
            )
