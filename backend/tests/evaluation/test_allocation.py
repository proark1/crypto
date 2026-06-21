"""Performance-weighted research allocation (§12.7).

The policy is pure given a standings snapshot, so the fakes are trivial: a
``read_standings`` closure over a dict the test controls (and may mutate
between passes to prove the selector re-reads the world each pass). The focus
is the contract — protected targets never park, the evidence boosts winners
and parks losers, parked targets are re-probed on a cadence, and symbol
coverage is preserved.
"""

from collections.abc import Awaitable, Callable, Mapping
from decimal import Decimal

from tradebot.evaluation.allocation import (
    BOOST_WEIGHT,
    PARK_MIN_TRADES,
    PARK_REPROBE_PERIOD,
    AllocationTier,
    PerformanceWeightedSelector,
    TargetStanding,
    classify,
    standings_from_competition,
)

TARGETS = ("production", "momentum", "keltner")
SYMBOLS = ("BTC/USDT", "ETH/USDT")


def standing(
    target: str,
    *,
    ret: str | None = None,
    trades: int = 0,
    candidate: bool = False,
) -> TargetStanding:
    return TargetStanding(
        target=target,
        return_fraction=Decimal(ret) if ret is not None else None,
        trades=trades,
        is_candidate=candidate,
    )


def reader(
    standings: Mapping[str, TargetStanding],
) -> Callable[[], Awaitable[Mapping[str, TargetStanding]]]:
    async def read() -> Mapping[str, TargetStanding]:
        return dict(standings)

    return read


class TestStandingsFromCompetition:
    def test_maps_rows_and_candidates_into_standings(self) -> None:
        rows = [
            {
                "bot_id": "momentum",
                "return_fraction": Decimal("0.05"),
                "entry_fills": 12,
                "exit_fills": 10,
                "breaker_tripped_reason": None,
                "paused": False,
            },
            {
                "bot_id": "keltner",
                "return_fraction": Decimal("-0.10"),
                "entry_fills": 30,
                "exit_fills": 28,
                "breaker_tripped_reason": "daily loss",
                "paused": True,
            },
        ]
        standings = standings_from_competition(rows, {"momentum"}, TARGETS)
        # Keyed and ordered by targets, including the one with no row.
        assert list(standings) == list(TARGETS)
        momentum = standings["momentum"]
        assert momentum.return_fraction == Decimal("0.05")
        assert momentum.trades == 22
        assert momentum.is_candidate is True
        keltner = standings["keltner"]
        assert keltner.trades == 58
        assert keltner.is_candidate is False
        assert keltner.breaker_tripped is True
        assert keltner.paused is True

    def test_target_without_a_row_has_unknown_return(self) -> None:
        standings = standings_from_competition([], set(), TARGETS)
        production = standings["production"]
        assert production.return_fraction is None
        assert production.trades == 0
        assert classify(production) is AllocationTier.NORMAL


class TestClassify:
    def test_a_routing_candidate_boosts(self) -> None:
        assert classify(standing("momentum", candidate=True)) is AllocationTier.BOOSTED

    def test_up_with_enough_trades_boosts(self) -> None:
        s = standing("momentum", ret="0.05", trades=PARK_MIN_TRADES)
        assert classify(s) is AllocationTier.BOOSTED

    def test_down_with_enough_trades_parks(self) -> None:
        s = standing("keltner", ret="-0.10", trades=PARK_MIN_TRADES)
        assert classify(s) is AllocationTier.PARKED

    def test_down_but_too_few_trades_stays_normal(self) -> None:
        # A loss on a thin sample is not yet trustworthy — keep researching it.
        s = standing("keltner", ret="-0.10", trades=PARK_MIN_TRADES - 1)
        assert classify(s) is AllocationTier.NORMAL

    def test_unknown_return_stays_normal(self) -> None:
        s = standing("keltner", ret=None, trades=500)
        assert classify(s) is AllocationTier.NORMAL

    def test_production_never_parks_even_when_deeply_down(self) -> None:
        s = standing("production", ret="-0.50", trades=500)
        assert classify(s) is AllocationTier.NORMAL

    def test_production_still_boosts_when_up(self) -> None:
        s = standing("production", ret="0.05", trades=PARK_MIN_TRADES)
        assert classify(s) is AllocationTier.BOOSTED


class TestSelectorSchedule:
    async def test_all_normal_is_one_turn_each_per_pass(self) -> None:
        standings = {t: standing(t, ret="0.0", trades=0) for t in TARGETS}
        selector = PerformanceWeightedSelector(targets=TARGETS, read_standings=reader(standings))
        first_pass = [await selector.next_assignment(SYMBOLS) for _ in range(len(TARGETS))]
        targets = [t for t, _ in first_pass]
        assert sorted(targets) == sorted(TARGETS)
        # One symbol for the whole pass, then it advances next pass.
        assert {s for _, s in first_pass} == {SYMBOLS[0]}
        _next_target, next_symbol = await selector.next_assignment(SYMBOLS)
        assert next_symbol == SYMBOLS[1]

    async def test_a_boosted_target_gets_extra_turns(self) -> None:
        standings = {
            "production": standing("production", ret="0.0", trades=0),
            "momentum": standing("momentum", ret="0.05", trades=PARK_MIN_TRADES),
            "keltner": standing("keltner", ret="0.0", trades=0),
        }
        selector = PerformanceWeightedSelector(targets=TARGETS, read_standings=reader(standings))
        # Pass length is production(1) + momentum(BOOST_WEIGHT) + keltner(1).
        pass_len = 1 + BOOST_WEIGHT + 1
        picks = [(await selector.next_assignment(SYMBOLS))[0] for _ in range(pass_len)]
        assert picks.count("momentum") == BOOST_WEIGHT
        assert picks.count("production") == 1
        assert picks.count("keltner") == 1

    async def test_a_parked_target_is_skipped_off_reprobe_then_returns(self) -> None:
        standings = {
            "production": standing("production", ret="0.0", trades=0),
            "momentum": standing("momentum", ret="0.0", trades=0),
            "keltner": standing("keltner", ret="-0.10", trades=PARK_MIN_TRADES),
        }
        selector = PerformanceWeightedSelector(targets=TARGETS, read_standings=reader(standings))
        seen_keltner_by_pass: list[bool] = []
        for _ in range(PARK_REPROBE_PERIOD + 1):
            # Drain one pass: production + momentum (+ keltner only on re-probe).
            this_pass: list[str] = []
            # A pass has at least the two un-parked targets; pull exactly the
            # ring length by watching the plan's pass_index advance.
            start_pass = None
            while True:
                target, _ = await selector.next_assignment(SYMBOLS)
                plan = selector.last_plan
                assert plan is not None
                if start_pass is None:
                    start_pass = plan.pass_index
                this_pass.append(target)
                # Stop when the ring is about to roll: next call would rebuild.
                if len(this_pass) >= sum(plan.weights.values()):
                    break
            seen_keltner_by_pass.append("keltner" in this_pass)
        # Pass 0 is a re-probe (0 % period == 0) → keltner present; the next
        # PARK_REPROBE_PERIOD-1 passes skip it; then it returns.
        assert seen_keltner_by_pass[0] is True
        assert seen_keltner_by_pass[1] is False
        assert seen_keltner_by_pass[PARK_REPROBE_PERIOD] is True

    async def test_reads_standings_fresh_each_pass(self) -> None:
        live = {t: standing(t, ret="0.0", trades=0) for t in TARGETS}
        selector = PerformanceWeightedSelector(targets=TARGETS, read_standings=reader(live))
        # Drain the first (all-normal) pass.
        for _ in range(len(TARGETS)):
            await selector.next_assignment(SYMBOLS)
        # Momentum turns into a winner before the next pass.
        live["momentum"] = standing("momentum", ret="0.05", trades=PARK_MIN_TRADES)
        await selector.next_assignment(SYMBOLS)  # triggers a rebuild
        plan = selector.last_plan
        assert plan is not None
        assert plan.tiers["momentum"] is AllocationTier.BOOSTED
        assert plan.weights["momentum"] == BOOST_WEIGHT

    async def test_last_plan_records_the_reasoning(self) -> None:
        standings = {
            "production": standing("production", ret="0.0", trades=0),
            "momentum": standing("momentum", candidate=True),
            "keltner": standing("keltner", ret="-0.10", trades=PARK_MIN_TRADES),
        }
        selector = PerformanceWeightedSelector(targets=TARGETS, read_standings=reader(standings))
        await selector.next_assignment(SYMBOLS)
        plan = selector.last_plan
        assert plan is not None
        assert "momentum" in plan.boosted
        assert "keltner" in plan.parked
        assert plan.symbol == SYMBOLS[0]
