"""Accept-triggered sweeps: coalescing, accepted-first grids, busy retries."""

import asyncio
from collections.abc import Coroutine
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest

from tradebot.evaluation import triggered_sweeps
from tradebot.evaluation.models import LearningFinding
from tradebot.evaluation.sweep import SweepConfig
from tradebot.evaluation.triggered_sweeps import AcceptSweepScheduler

DELAY = timedelta(milliseconds=20)


class ScriptedSweeps:
    """Records sweep starts; optionally busy for the first N attempts."""

    def __init__(self, busy_attempts: int = 0) -> None:
        self.busy_attempts = busy_attempts
        self.configs: list[SweepConfig] = []

    async def start(self, config: SweepConfig) -> int:
        if self.busy_attempts > 0:
            self.busy_attempts -= 1
            raise RuntimeError("sweep 1 is already in progress")
        self.configs.append(config)
        return len(self.configs)


class ScriptedStore:
    """One run row and its (mutable) findings."""

    def __init__(self, run: dict[str, Any] | None, findings: list[tuple[int, LearningFinding]]):
        self.run = run
        self.findings = findings

    async def fetch_run(self, run_id: int) -> dict[str, Any] | None:
        return None if self.run is None else dict(self.run)

    async def fetch_findings(self, run_id: int) -> list[tuple[int, LearningFinding]]:
        return list(self.findings)


def make_finding(finding_id: int, pattern: str, status: str) -> tuple[int, LearningFinding]:
    return (
        finding_id,
        LearningFinding(
            run_id=1,
            pattern=pattern,
            evidence_scenario_ids=(1,),
            affected_count=1,
            average_r_impact=Decimal("-0.5"),
            suggestion="test",
            confidence="low",
            status=status,
            created_at=datetime.now(UTC),
        ),
    )


def make_run(symbols: list[str] | None = None, strategy: str = "production") -> dict[str, Any]:
    return {
        "id": 1,
        "symbols": symbols if symbols is not None else ["BTC/USDT", "ETH/USDT"],
        "strategy": strategy,
    }


def make_scheduler(
    sweeps: ScriptedSweeps,
    store: ScriptedStore,
    tasks: list["asyncio.Task[None]"],
    delay: timedelta = DELAY,
) -> AcceptSweepScheduler:
    def spawn(coroutine: Coroutine[Any, Any, None]) -> "asyncio.Task[None]":
        task = asyncio.ensure_future(coroutine)
        tasks.append(task)
        return task

    return AcceptSweepScheduler(
        sweeps=sweeps,
        store=store,
        active_params=lambda: {"trend_following": {"fast_ema_period": 20}},
        spawn=spawn,
        delay=delay,
        timeframe="1h",
        history_days=180,
    )


async def drain(tasks: list["asyncio.Task[None]"]) -> None:
    await asyncio.gather(*tasks)


async def test_acceptances_inside_the_window_share_one_sweep() -> None:
    sweeps = ScriptedSweeps()
    store = ScriptedStore(
        make_run(), [make_finding(10, "entries lose money when trend is down", "accepted")]
    )
    tasks: list[asyncio.Task[None]] = []
    scheduler = make_scheduler(sweeps, store, tasks)

    scheduler.note_acceptance(1)
    assert scheduler.pending_run_ids() == frozenset({1})
    # A second verdict lands inside the window — and its finding is read at
    # fire time, so it rides the same sweep with no timer reset.
    store.findings.append(make_finding(11, "held positions ride into their stops", "accepted"))
    scheduler.note_acceptance(1)
    await drain(tasks)

    assert len(sweeps.configs) == 1
    config = sweeps.configs[0]
    assert config.symbol == "BTC/USDT"  # the run's own first symbol
    assert set(config.motivating_finding_ids) == {10, 11}
    names = {candidate.name for candidate in config.candidates}
    assert "trend_filtered_reversion" in names  # downtrend knob
    assert "breakeven_lock" in names or "no_breakeven" in names  # wrong-hold knobs
    assert scheduler.pending_run_ids() == frozenset()


async def test_accepted_findings_outrank_proposed_in_the_grid() -> None:
    sweeps = ScriptedSweeps()
    store = ScriptedStore(
        make_run(),
        [
            make_finding(10, "entries lose money when trend is down", "accepted"),
            make_finding(11, "entries chase moves that are already over", "proposed"),
        ],
    )
    tasks: list[asyncio.Task[None]] = []
    scheduler = make_scheduler(sweeps, store, tasks)

    scheduler.note_acceptance(1)
    await drain(tasks)

    (config,) = sweeps.configs
    assert config.motivating_finding_ids == (10,)
    names = {candidate.name for candidate in config.candidates}
    assert "anti_chase" not in names  # the proposed finding waits its turn


async def test_a_busy_lane_is_retried_until_free(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(triggered_sweeps, "BUSY_RETRY_SECONDS", 0.01)
    sweeps = ScriptedSweeps(busy_attempts=2)
    store = ScriptedStore(
        make_run(), [make_finding(10, "entries lose money when trend is down", "accepted")]
    )
    tasks: list[asyncio.Task[None]] = []
    scheduler = make_scheduler(sweeps, store, tasks)

    scheduler.note_acceptance(1)
    await drain(tasks)

    assert len(sweeps.configs) == 1  # started once the lane freed up


async def test_a_vanished_run_fires_nothing() -> None:
    sweeps = ScriptedSweeps()
    store = ScriptedStore(None, [])
    tasks: list[asyncio.Task[None]] = []
    scheduler = make_scheduler(sweeps, store, tasks)

    scheduler.note_acceptance(1)
    await drain(tasks)

    assert sweeps.configs == []


async def test_the_grid_matches_the_run_that_was_graded() -> None:
    """A breakout run's verdicts sweep breakout knobs, never the production grid."""
    sweeps = ScriptedSweeps()
    store = ScriptedStore(
        make_run(strategy="breakout"),
        [make_finding(10, "entries lose money when event is breakout_fake", "accepted")],
    )
    tasks: list[asyncio.Task[None]] = []
    scheduler = make_scheduler(sweeps, store, tasks)

    scheduler.note_acceptance(1)
    await drain(tasks)

    (config,) = sweeps.configs
    assert {candidate.family for candidate in config.candidates} == {"breakout"}
    assert any(candidate.name == "min_width_filter" for candidate in config.candidates)
    assert config.motivating_finding_ids == (10,)


async def test_a_custom_bots_run_is_skipped_for_now() -> None:
    """No improvement grid for a recipe yet: skip loudly, never sweep wrong knobs."""
    sweeps = ScriptedSweeps()
    store = ScriptedStore(
        make_run(strategy="custom-my-recipe"),
        [make_finding(10, "entries lose money when trend is down", "accepted")],
    )
    tasks: list[asyncio.Task[None]] = []
    scheduler = make_scheduler(sweeps, store, tasks)

    scheduler.note_acceptance(1)
    await drain(tasks)

    assert sweeps.configs == []
