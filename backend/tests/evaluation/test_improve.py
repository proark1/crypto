"""Automated improvement: candidate derivation and the promote-on-validated loop."""

import asyncio
import contextlib
from collections.abc import Awaitable, Callable, Mapping
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from tradebot.evaluation.improve import AutoImprover, build_improvement_candidates
from tradebot.evaluation.models import LearningFinding
from tradebot.evaluation.runner import EvaluationRunConfig
from tradebot.evaluation.sweep import SweepConfig, build_candidate_strategy


class ScriptedSweeps:
    """Stands in for the SweepManager: records configs, scripts outcomes."""

    def __init__(self, running: bool = False) -> None:
        self.running = running
        self.configs: list[SweepConfig] = []

    async def start(self, config: SweepConfig) -> int:
        if self.running:
            raise RuntimeError("sweep 1 is already in progress")
        self.configs.append(config)
        return len(self.configs)


class ScriptedStore:
    """Scripted research record: one sweep row, runs, and findings."""

    def __init__(
        self,
        status: str,
        report: dict[str, Any] | None,
        runs: list[dict[str, Any]] | None = None,
        findings: list[tuple[int, LearningFinding]] | None = None,
    ) -> None:
        self._row = {"status": status, "report": report}
        self.runs = runs if runs is not None else [fresh_completed_run()]
        self.findings = findings or []

    async def fetch_sweep(self, sweep_id: int) -> dict[str, Any] | None:
        return dict(self._row)

    async def list_runs(self, limit: int = 50) -> list[dict[str, Any]]:
        return list(self.runs)

    async def fetch_findings(self, run_id: int) -> list[tuple[int, LearningFinding]]:
        return list(self.findings)


class ScriptedEvaluations:
    """Records evaluation starts; optionally scripted busy."""

    def __init__(self, running: bool = False) -> None:
        self.running = running
        self.configs: list[EvaluationRunConfig] = []

    async def start(self, config: EvaluationRunConfig) -> int:
        if self.running:
            raise RuntimeError("evaluation run 1 is already in progress")
        self.configs.append(config)
        return len(self.configs)


def fresh_completed_run(run_id: int = 1) -> dict[str, Any]:
    return {"id": run_id, "status": "completed", "created_at": datetime.now(UTC)}


def make_finding(
    finding_id: int, pattern: str, status: str = "proposed"
) -> tuple[int, LearningFinding]:
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


def make_improver(
    sweeps: ScriptedSweeps,
    store: ScriptedStore,
    promoted: list[tuple[str, Mapping[str, Any], int | None, str | None]],
    symbols: tuple[str, ...] = ("BTC/USDT", "ETH/USDT"),
    notify: Callable[[str], Awaitable[None]] | None = None,
    evaluations: ScriptedEvaluations | None = None,
    confirm: Callable[[str, Mapping[str, Any], str], Awaitable[str | None]] | None = None,
) -> AutoImprover:
    async def promote(
        family: str, params: Mapping[str, Any], sweep_id: int | None, note: str | None
    ) -> int:
        promoted.append((family, params, sweep_id, note))
        return len(promoted)

    return AutoImprover(
        sweeps=sweeps,
        evaluations=evaluations if evaluations is not None else ScriptedEvaluations(),
        store=store,
        active_params=lambda: {"trend_following": {"fast_ema_period": 20}},
        symbols=lambda: symbols,
        promote=promote,
        confirm=confirm,
        interval=timedelta(hours=12),
        history_days=180,
        timeframe="1h",
        notify=notify,
    )


class TestBuildImprovementCandidates:
    def test_active_config_is_the_baseline_and_every_candidate_builds(self) -> None:
        candidates, _ = build_improvement_candidates(
            {"trend_following": {"fast_ema_period": 20, "slow_ema_period": 50}}
        )

        assert candidates[0].name.startswith("active_trend")
        assert candidates[0].params["fast_ema_period"] == 20
        names = [candidate.name for candidate in candidates]
        assert len(set(names)) == len(names)
        for candidate in candidates:  # every derived variant must be buildable
            build_candidate_strategy(candidate)

    def test_extreme_actives_clamp_into_valid_configs(self) -> None:
        """Scaling a tiny fast EMA down must never cross its slow EMA."""
        candidates, _ = build_improvement_candidates(
            {"trend_following": {"fast_ema_period": 3, "slow_ema_period": 5}}
        )
        for candidate in candidates:
            build_candidate_strategy(candidate)  # raises if fast >= slow

    def test_variants_that_collapse_into_duplicates_are_dropped(self) -> None:
        """A stop already at the floor makes tighter == active; drop it."""
        candidates, _ = build_improvement_candidates(
            {"trend_following": {"atr_stop_multiple": 0.5}}
        )
        keys = [(c.family, tuple(sorted(c.params.items()))) for c in candidates]
        assert len(set(keys)) == len(keys)

    def test_both_families_compete(self) -> None:
        candidates, _ = build_improvement_candidates({})
        families = {candidate.family for candidate in candidates}
        assert families == {"trend_following", "mean_reversion"}


class TestAutoImprover:
    async def test_validated_challenger_is_promoted_with_lineage(self) -> None:
        sweeps = ScriptedSweeps()
        promoted: list[tuple[str, Mapping[str, Any], int | None, str | None]] = []
        store = ScriptedStore(
            "completed",
            {
                "verdict": "validated",
                "winner": "tighter_stop",
                "explanation": "tighter_stop beat the active configuration",
            },
        )
        improver = make_improver(sweeps, store, promoted)

        sweep_id = await improver.run_cycle()

        assert sweep_id == 1
        assert len(promoted) == 1
        family, params, source_sweep_id, note = promoted[0]
        assert family == "trend_following"
        assert params["atr_stop_multiple"] == 1.5  # 2.0 * 0.75
        assert source_sweep_id == 1
        assert note is not None and "auto-promoted" in note

    async def test_unvalidated_verdicts_change_nothing(self) -> None:
        for verdict in ("baseline_best", "overfit", "insufficient_evidence"):
            promoted: list[tuple[str, Mapping[str, Any], int | None, str | None]] = []
            store = ScriptedStore("completed", {"verdict": verdict, "winner": "tighter_stop"})
            await make_improver(ScriptedSweeps(), store, promoted).run_cycle()
            assert promoted == [], verdict

    async def test_failed_sweeps_change_nothing(self) -> None:
        promoted: list[tuple[str, Mapping[str, Any], int | None, str | None]] = []
        store = ScriptedStore("failed", None)
        await make_improver(ScriptedSweeps(), store, promoted).run_cycle()
        assert promoted == []

    async def test_cycle_yields_when_a_sweep_is_already_running(self) -> None:
        promoted: list[tuple[str, Mapping[str, Any], int | None, str | None]] = []
        sweeps = ScriptedSweeps(running=True)
        improver = make_improver(sweeps, ScriptedStore("completed", None), promoted)

        assert await improver.run_cycle() is None
        assert promoted == []

    async def test_symbols_rotate_across_cycles(self) -> None:
        sweeps = ScriptedSweeps()
        store = ScriptedStore("completed", {"verdict": "baseline_best"})
        improver = make_improver(sweeps, store, [])

        await improver.run_cycle()
        await improver.run_cycle()
        await improver.run_cycle()

        assert [config.symbol for config in sweeps.configs] == [
            "BTC/USDT",
            "ETH/USDT",
            "BTC/USDT",
        ]

    async def test_engine_confirmation_veto_blocks_the_promotion(self) -> None:
        """A validated sweep winner still needs the engine's confirmation."""
        messages: list[str] = []
        confirmed: list[tuple[str, str]] = []
        promoted: list[tuple[str, Mapping[str, Any], int | None, str | None]] = []

        async def vetoing_confirm(family: str, params: Mapping[str, Any], symbol: str) -> str:
            confirmed.append((family, symbol))
            return "challenger final equity 9000 < incumbent 10000"

        async def notify(message: str) -> None:
            messages.append(message)

        store = ScriptedStore(
            "completed",
            {"verdict": "validated", "winner": "tighter_stop", "explanation": "won"},
        )
        improver = make_improver(
            ScriptedSweeps(), store, promoted, notify=notify, confirm=vetoing_confirm
        )

        sweep_id = await improver.run_cycle()

        assert sweep_id == 1
        assert confirmed == [("trend_following", "BTC/USDT")]  # the swept symbol
        assert promoted == []  # the veto is final for this cycle
        assert messages and "vetoed" in messages[0]

    async def test_engine_confirmation_pass_promotes(self) -> None:
        promoted: list[tuple[str, Mapping[str, Any], int | None, str | None]] = []

        async def passing_confirm(
            family: str, params: Mapping[str, Any], symbol: str
        ) -> str | None:
            return None

        store = ScriptedStore(
            "completed",
            {"verdict": "validated", "winner": "tighter_stop", "explanation": "won"},
        )
        improver = make_improver(ScriptedSweeps(), store, promoted, confirm=passing_confirm)

        await improver.run_cycle()
        assert len(promoted) == 1

    async def test_notify_carries_the_promotion_message(self) -> None:
        messages: list[str] = []

        async def notify(text: str) -> None:
            messages.append(text)

        sweeps = ScriptedSweeps()
        store = ScriptedStore(
            "completed",
            {"verdict": "validated", "winner": "wider_stop", "explanation": "it held up"},
        )
        promoted: list[tuple[str, Mapping[str, Any], int | None, str | None]] = []
        improver = make_improver(sweeps, store, promoted, notify=notify)

        await improver.run_cycle()

        assert len(messages) == 1
        assert "auto-promoted" in messages[0] and "wider_stop" in messages[0]

    async def test_run_loop_survives_a_failing_cycle(self) -> None:
        class ExplodingSweeps:
            async def start(self, config: SweepConfig) -> int:
                raise ValueError("boom")

        improver = AutoImprover(
            sweeps=ExplodingSweeps(),
            evaluations=ScriptedEvaluations(),
            store=ScriptedStore("completed", None),
            active_params=lambda: {},
            symbols=lambda: ("BTC/USDT",),
            promote=_never_promote,
            interval=timedelta(seconds=0.01),
            history_days=180,
            timeframe="1h",
        )
        task = asyncio.create_task(improver.run())
        await asyncio.sleep(0.1)  # several intervals: the loop must survive
        assert not task.done()  # a cycle error never kills the loop
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


async def _never_promote(
    family: str, params: Mapping[str, Any], sweep_id: int | None, note: str | None
) -> int:
    raise AssertionError("nothing should be promoted")


class TestFindingsDrivenCandidates:
    def test_a_downtrend_finding_adds_the_trend_filter_challenger(self) -> None:
        candidates, motivating = build_improvement_candidates(
            {}, findings=[(7, "entries lose money when trend is down")]
        )
        names = [candidate.name for candidate in candidates]
        assert "trend_filtered_reversion" in names
        assert motivating == (7,)
        filtered = next(c for c in candidates if c.name == "trend_filtered_reversion")
        assert filtered.params["trend_filter_ema_period"] == 50

    def test_a_chase_finding_adds_the_anti_chase_challenger(self) -> None:
        candidates, motivating = build_improvement_candidates(
            {}, findings=[(9, "entries chase moves that are already over")]
        )
        names = [candidate.name for candidate in candidates]
        assert "anti_chase" in names
        assert motivating == (9,)

    def test_an_already_active_filter_gets_its_removal_tested(self) -> None:
        """Symmetry: the loop can also unlearn a filter that stopped helping."""
        candidates, _ = build_improvement_candidates(
            {"mean_reversion": {"trend_filter_ema_period": 50}},
            findings=[(7, "entries lose money when trend is down")],
        )
        unfiltered = next(c for c in candidates if c.name == "unfiltered_reversion")
        assert unfiltered.params["trend_filter_ema_period"] == 0

    def test_a_wrong_hold_finding_adds_the_stop_management_challengers(self) -> None:
        candidates, motivating = build_improvement_candidates(
            {}, findings=[(3, "held positions ride into their stops")]
        )
        names = [candidate.name for candidate in candidates]
        assert "breakeven_lock" in names and "atr_trailing" in names
        assert motivating == (3,)
        trailing = next(c for c in candidates if c.name == "atr_trailing")
        assert trailing.params["trail_atr_multiple"] == 2.0  # the active stop width

    def test_unrelated_findings_add_no_candidates(self) -> None:
        baseline, _ = build_improvement_candidates({})
        candidates, motivating = build_improvement_candidates(
            {}, findings=[(3, "the bot stays flat through moves worth taking")]
        )
        assert len(candidates) == len(baseline)  # no knob exists for it yet
        assert motivating == ()


class TestEvaluateBeforeSweeping:
    async def test_no_completed_run_starts_an_evaluation_instead_of_sweeping(self) -> None:
        sweeps = ScriptedSweeps()
        evaluations = ScriptedEvaluations()
        store = ScriptedStore("completed", None, runs=[])
        improver = make_improver(sweeps, store, [], evaluations=evaluations)

        assert await improver.run_cycle() is None
        assert sweeps.configs == []  # nothing to learn from yet: no sweep
        (config,) = evaluations.configs
        assert config.scenario_count == 1600  # unstarved sample size

    async def test_a_stale_run_is_refreshed_before_sweeping(self) -> None:
        stale = {
            "id": 1,
            "status": "completed",
            "created_at": datetime.now(UTC) - timedelta(days=30),
        }
        evaluations = ScriptedEvaluations()
        improver = make_improver(
            ScriptedSweeps(),
            ScriptedStore("completed", None, runs=[stale]),
            [],
            evaluations=evaluations,
        )

        await improver.run_cycle()

        assert len(evaluations.configs) == 1

    async def test_fresh_findings_ride_into_the_sweep_as_motivation(self) -> None:
        sweeps = ScriptedSweeps()
        store = ScriptedStore(
            "completed",
            {"verdict": "baseline_best"},
            findings=[
                make_finding(7, "entries lose money when trend is down"),
                make_finding(8, "entries chase moves that are already over"),
                make_finding(9, "entries lose money when trend is down", status="rejected"),
            ],
        )
        improver = make_improver(sweeps, store, [])

        await improver.run_cycle()

        (config,) = sweeps.configs
        names = [candidate.name for candidate in config.candidates]
        assert "trend_filtered_reversion" in names and "anti_chase" in names
        # Rejected finding 9 contributes nothing; 7 and 8 are the lineage.
        assert set(config.motivating_finding_ids) == {7, 8}
        assert config.scenario_count == 1600

    async def test_busy_evaluation_manager_just_waits_for_the_next_cycle(self) -> None:
        evaluations = ScriptedEvaluations(running=True)
        improver = make_improver(
            ScriptedSweeps(), ScriptedStore("completed", None, runs=[]), [], evaluations=evaluations
        )
        assert await improver.run_cycle() is None
