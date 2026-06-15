"""Research-campaign loop tests.

The campaign is pure orchestration, so every dependency here is a fake: a
scripted sweep verdict stream, a provider that records the steps it was
asked for, an in-memory "bot" that holds the live params and records
promotions, and a manual clock for the wall-clock budget. That lets the
tests pin the behaviour that matters — promote only on a validated,
engine-confirmed challenger; refine on a miss; stop at the budget or on
convergence; never peek into the reserved holdout — without a worker, a
database, or a network.
"""

import asyncio
from collections.abc import Awaitable, Callable, Coroutine, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from tradebot.evaluation.campaign import (
    CampaignConfig,
    CampaignManager,
    HoldoutGrader,
    ResearchCampaign,
)
from tradebot.evaluation.settings_diff import SettingChange
from tradebot.evaluation.sweep import SweepCandidate, SweepConfig

_T0 = datetime(2026, 6, 15, tzinfo=UTC)

ConfirmFn = Callable[[str, Mapping[str, Any], str], Awaitable[str | None]]


class ScriptedSweeps:
    """Serves as both the sweep starter and the verdict store for a campaign.

    Each ``start`` records the round's config and returns the next id;
    ``fetch_sweep`` replays the scripted report for that id (a missing or
    ``None`` entry reads as a failed sweep, i.e. no verdict).
    """

    def __init__(self, reports: list[dict[str, Any] | None]) -> None:
        self._reports = reports
        self.started: list[SweepConfig] = []

    async def start(self, config: SweepConfig) -> int:
        sweep_id = len(self.started)
        self.started.append(config)
        return sweep_id

    async def fetch_sweep(self, sweep_id: int) -> dict[str, Any] | None:
        if 0 <= sweep_id < len(self._reports) and self._reports[sweep_id] is not None:
            return {"status": "completed", "report": self._reports[sweep_id]}
        return {"status": "failed", "report": None}


class HangingSweeps:
    """A sweep that starts but never reaches a verdict (always running)."""

    async def start(self, config: SweepConfig) -> int:
        return 0

    async def fetch_sweep(self, sweep_id: int) -> dict[str, Any] | None:
        return {"status": "running", "report": None}


class VanishingSweeps:
    """A sweep whose row never appears in the store (``fetch_sweep`` is ``None``).

    Without the early abort this would poll to the full 8h timeout — so this
    test hanging is itself the regression signal.
    """

    async def start(self, config: SweepConfig) -> int:
        return 0

    async def fetch_sweep(self, sweep_id: int) -> dict[str, Any] | None:
        return None


class ManualClock:
    """A clock that only moves when told — and when the provider advances it."""

    def __init__(self, now: datetime) -> None:
        self._now = now

    def __call__(self) -> datetime:
        return self._now

    def advance(self, delta: timedelta) -> None:
        self._now += delta


class StaticProvider:
    """Returns a fixed (baseline, challenger) pair; records the steps it saw.

    The baseline mirrors the live incumbent so a promotion visibly moves it;
    the challenger is a fixed momentum variant named ``challenger``. When a
    clock is bound it advances by ``round_step`` each call, which is how the
    wall-clock-budget test makes time pass one round at a time.
    """

    def __init__(
        self, clock: ManualClock | None = None, round_step: timedelta = timedelta(0)
    ) -> None:
        self.scales: list[float] = []
        self._clock = clock
        self._round_step = round_step

    async def __call__(
        self, active_params: Mapping[str, Mapping[str, Any]], scale: float
    ) -> tuple[Sequence[SweepCandidate], Sequence[int]]:
        self.scales.append(scale)
        if self._clock is not None:
            self._clock.advance(self._round_step)
        baseline = SweepCandidate(
            name="active_momentum",
            family="momentum",
            params=dict(active_params.get("momentum", {})),
        )
        challenger = SweepCandidate(
            name="challenger",
            family="momentum",
            params={"fast_ema_period": 8, "slow_ema_period": 21},
        )
        return (baseline, challenger), ()


class FakeBot:
    """Holds the live params and records promotions, like the worker apply path."""

    def __init__(self, params: dict[str, dict[str, Any]]) -> None:
        self.params = params
        self.promotions: list[tuple[str, dict[str, Any], int | None, str | None]] = []

    def active(self) -> Mapping[str, Mapping[str, Any]]:
        return self.params

    async def promote(
        self, family: str, params: Mapping[str, Any], sweep_id: int | None, note: str | None
    ) -> int:
        self.params = {**self.params, family: dict(params)}
        self.promotions.append((family, dict(params), sweep_id, note))
        return len(self.promotions)


class Recorder:
    """Captures notify messages."""

    def __init__(self) -> None:
        self.messages: list[str] = []

    async def __call__(self, message: str) -> None:
        self.messages.append(message)


async def _allow(family: str, params: Mapping[str, Any], symbol: str) -> str | None:
    return None


async def _veto(family: str, params: Mapping[str, Any], symbol: str) -> str | None:
    return "engine replay underperformed the incumbent"


async def _holdout(
    start_params: Mapping[str, Mapping[str, Any]],
    final_params: Mapping[str, Mapping[str, Any]],
    holdout_start: datetime,
) -> dict[str, Any] | None:
    return {"holdout_start": holdout_start.isoformat(), "moved": start_params != final_params}


def _validated(winner: str = "challenger") -> dict[str, Any]:
    return {
        "verdict": "validated",
        "winner": winner,
        "explanation": f"{winner} survived walk-forward",
    }


def _kept(verdict: str) -> dict[str, Any]:
    return {"verdict": verdict, "winner": "challenger", "explanation": f"kept baseline ({verdict})"}


def _campaign(
    reports: list[dict[str, Any] | None],
    *,
    bot: FakeBot | None = None,
    provider: StaticProvider | None = None,
    clock: ManualClock | None = None,
    confirm: ConfirmFn | None = _allow,
    holdout: HoldoutGrader | None = None,
    notify: Callable[[str], Awaitable[None]] | None = None,
) -> tuple[ResearchCampaign, ScriptedSweeps, FakeBot, StaticProvider]:
    bot = bot or FakeBot({"momentum": {"fast_ema_period": 12, "slow_ema_period": 26}})
    provider = provider or StaticProvider()
    sweeps = ScriptedSweeps(reports)
    campaign = ResearchCampaign(
        sweeps=sweeps,
        store=sweeps,
        candidates=provider,
        active_params=bot.active,
        promote=bot.promote,
        confirm=confirm,
        holdout=holdout,
        clock=clock or ManualClock(_T0),
        notify=notify,
    )
    return campaign, sweeps, bot, provider


def _config(**overrides: Any) -> CampaignConfig:
    base: dict[str, Any] = {"target": "momentum", "symbol": "BTC/USDT", "max_rounds": 1}
    return CampaignConfig(**{**base, **overrides})


class TestResearchCampaign:
    async def test_validated_round_promotes_and_advances_the_incumbent(self) -> None:
        recorder = Recorder()
        campaign, _sweeps, bot, _provider = _campaign(
            [_validated()], notify=recorder, holdout=_holdout
        )

        await campaign.run(_config(max_rounds=1))

        status = campaign.status
        assert status is not None
        assert status.status == "completed"
        assert status.promotions == 1
        family, params, sweep_id, _note = bot.promotions[0]
        assert family == "momentum"
        assert params == {"fast_ema_period": 8, "slow_ema_period": 21}
        assert sweep_id == 0
        # the live incumbent actually moved to the promoted params
        assert bot.params["momentum"] == {"fast_ema_period": 8, "slow_ema_period": 21}
        first = status.rounds[0]
        assert first.verdict == "validated" and first.promoted_version == 1
        assert "promoted momentum settings v1" in first.note
        # The round records exactly what the promotion changed, before -> after.
        assert first.changes == (
            SettingChange(field="fast_ema_period", before="12", after="8"),
            SettingChange(field="slow_ema_period", before="26", after="21"),
        )
        assert status.holdout_read is not None and status.holdout_read["moved"] is True
        assert recorder.messages and "campaign promoted momentum" in recorder.messages[0]
        assert status.stop_reason is not None and "1-round limit" in status.stop_reason

    async def test_scale_resets_after_a_promotion_and_shrinks_after_a_miss(self) -> None:
        campaign, _sweeps, _bot, provider = _campaign(
            [_validated(), _kept("overfit"), _validated()]
        )

        await campaign.run(_config(max_rounds=3))

        # round0 validated (scale 1.0, reset to 1.0); round1 overfit (1.0, refine
        # to 0.5); round2 validated again (0.5).
        assert provider.scales == [1.0, 1.0, 0.5]
        assert campaign.status is not None and campaign.status.promotions == 2

    async def test_a_non_validated_round_never_promotes_and_refines(self) -> None:
        campaign, _sweeps, bot, provider = _campaign(
            [_kept("overfit"), _kept("insufficient_evidence"), _kept("baseline_best")]
        )

        await campaign.run(_config(max_rounds=3))

        assert bot.promotions == []
        status = campaign.status
        assert status is not None and status.promotions == 0
        assert provider.scales == [1.0, 0.5, 0.25]
        for round_record in status.rounds:
            assert round_record.promoted_version is None
            assert "kept the active configuration" in round_record.note
            assert round_record.changes == ()  # nothing promoted, nothing changed

    async def test_converges_when_the_step_shrinks_below_min_scale(self) -> None:
        # Ten rounds allowed, but every round misses, so the step shrinks below
        # min_scale and the campaign stops itself early.
        campaign, _sweeps, _bot, provider = _campaign([_kept("overfit")] * 10)

        await campaign.run(_config(max_rounds=10, refine_factor=0.5, min_scale=0.25))

        status = campaign.status
        assert status is not None and status.promotions == 0
        assert len(status.rounds) == 3  # 1.0, 0.5, 0.25, then 0.125 < 0.25 -> stop
        assert provider.scales == [1.0, 0.5, 0.25]
        assert status.stop_reason is not None and "converged" in status.stop_reason

    async def test_stops_at_the_wall_clock_budget(self) -> None:
        clock = ManualClock(_T0)
        provider = StaticProvider(clock=clock, round_step=timedelta(hours=2.5))
        campaign, _sweeps, _bot, _provider = _campaign(
            [_kept("overfit")] * 10, provider=provider, clock=clock
        )

        # min_scale tiny so convergence can't end it before the clock does.
        await campaign.run(_config(max_rounds=10, max_hours=6.0, min_scale=0.001))

        status = campaign.status
        assert status is not None
        assert len(status.rounds) == 3  # 2.5h * 3 = 7.5h >= 6h budget
        assert status.stop_reason is not None and "time limit" in status.stop_reason

    async def test_stops_at_the_round_budget(self) -> None:
        campaign, _sweeps, _bot, _provider = _campaign(
            [_kept("overfit")] * 10, clock=ManualClock(_T0)
        )

        await campaign.run(_config(max_rounds=2, min_scale=0.001))

        status = campaign.status
        assert status is not None and len(status.rounds) == 2
        assert status.stop_reason is not None and "2-round limit" in status.stop_reason

    async def test_a_validated_winner_vetoed_by_the_engine_is_not_promoted(self) -> None:
        campaign, _sweeps, bot, _provider = _campaign([_validated()], confirm=_veto)

        await campaign.run(_config(max_rounds=1))

        assert bot.promotions == []
        status = campaign.status
        assert status is not None and status.promotions == 0
        assert "vetoed the promotion" in status.rounds[0].note

    async def test_a_validated_baseline_winner_is_never_auto_promoted(self) -> None:
        # The sweep contract never crowns the baseline "validated", but if a
        # report ever did, the campaign must refuse rather than re-promote it.
        campaign, _sweeps, bot, _provider = _campaign([_validated(winner="active_momentum")])

        await campaign.run(_config(max_rounds=1))

        assert bot.promotions == []
        assert campaign.status is not None
        assert "not auto-promotable" in campaign.status.rounds[0].note

    async def test_every_round_reserves_the_same_frozen_holdout(self) -> None:
        campaign, sweeps, _bot, _provider = _campaign(
            [_kept("overfit"), _kept("overfit")], clock=ManualClock(_T0)
        )

        await campaign.run(_config(max_rounds=2, holdout_days=30))

        assert len(sweeps.started) == 2
        expected = _T0 - timedelta(days=30)
        # Every round's sweep ends exactly at the frozen holdout boundary, so
        # no round is ever graded on the reserved slice.
        assert {config.window_end for config in sweeps.started} == {expected}
        assert campaign.status is not None and campaign.status.holdout_start == expected

    async def test_holdout_read_is_skipped_without_a_grader(self) -> None:
        campaign, _sweeps, _bot, _provider = _campaign([_kept("overfit")])

        await campaign.run(_config(max_rounds=1))

        assert campaign.status is not None and campaign.status.holdout_read is None

    async def test_a_sweep_with_no_verdict_refines_without_promoting(self) -> None:
        campaign, _sweeps, bot, _provider = _campaign([None])  # sweep id 0 reads as failed

        await campaign.run(_config(max_rounds=1))

        assert bot.promotions == []
        status = campaign.status
        assert status is not None and status.promotions == 0
        assert "without a verdict" in status.rounds[0].note

    async def test_a_missing_sweep_row_aborts_the_round_promptly(self) -> None:
        # A row that never appears must not poll to the 8h timeout — the round
        # aborts at once, so this test returning at all is the assertion.
        bot = FakeBot({"momentum": {}})
        sweeps = VanishingSweeps()
        campaign = ResearchCampaign(
            sweeps=sweeps,
            store=sweeps,
            candidates=StaticProvider(),
            active_params=bot.active,
            promote=bot.promote,
            clock=ManualClock(_T0),
        )

        await campaign.run(_config(max_rounds=1))

        assert bot.promotions == []
        status = campaign.status
        assert status is not None and status.promotions == 0
        assert "without a verdict" in status.rounds[0].note


def _spawner() -> tuple[
    Callable[[Coroutine[Any, Any, None]], asyncio.Task[None]], list[asyncio.Task[None]]
]:
    tasks: list[asyncio.Task[None]] = []

    def spawn(coro: Coroutine[Any, Any, None]) -> asyncio.Task[None]:
        task = asyncio.create_task(coro)
        tasks.append(task)
        return task

    return spawn, tasks


class TestCampaignManager:
    async def test_one_campaign_at_a_time(self) -> None:
        campaign, _sweeps, _bot, _provider = _campaign([_kept("overfit")])
        spawn, tasks = _spawner()
        manager = CampaignManager(campaign, spawn=spawn)

        manager.start(_config(max_rounds=1))
        with pytest.raises(RuntimeError, match="already in progress"):
            manager.start(_config(max_rounds=1))
        await asyncio.gather(*tasks)

        assert manager.status is not None and manager.status.status == "completed"

    async def test_cancel_marks_a_running_campaign_interrupted(self) -> None:
        bot = FakeBot({"momentum": {}})
        sweeps = HangingSweeps()
        campaign = ResearchCampaign(
            sweeps=sweeps,
            store=sweeps,
            candidates=StaticProvider(),
            active_params=bot.active,
            promote=bot.promote,
            clock=ManualClock(_T0),
        )
        spawn, _tasks = _spawner()
        manager = CampaignManager(campaign, spawn=spawn)

        task = manager.start(_config(max_rounds=3))
        for _ in range(3):  # let the round reach the poll and park there
            await asyncio.sleep(0)
        assert manager.cancel() is True
        with pytest.raises(asyncio.CancelledError):
            await task

        assert manager.status is not None and manager.status.status == "interrupted"
        assert manager.cancel() is False
