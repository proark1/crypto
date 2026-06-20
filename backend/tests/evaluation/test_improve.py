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


def fresh_completed_run(run_id: int = 1, strategy: str = "production") -> dict[str, Any]:
    return {
        "id": run_id,
        "status": "completed",
        "strategy": strategy,
        "created_at": datetime.now(UTC),
    }


def runs_for_every_target() -> list[dict[str, Any]]:
    """One fresh completed run per rotation target, so every cycle sweeps."""
    from tradebot.evaluation.improve import IMPROVEMENT_TARGETS

    return [
        fresh_completed_run(run_id=index + 1, strategy=target)
        for index, target in enumerate(IMPROVEMENT_TARGETS)
    ]


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
    campaign_active: Callable[[], bool] | None = None,
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
        campaign_active=campaign_active,
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

    async def test_targets_rotate_first_then_symbols(self) -> None:
        """Every family gets its turn before any symbol repeats."""
        sweeps = ScriptedSweeps()
        store = ScriptedStore(
            "completed", {"verdict": "baseline_best"}, runs=runs_for_every_target()
        )
        improver = make_improver(sweeps, store, [])

        # Five targets, two symbols: ten cycles is one full pass over the
        # symbols, every target visited before either symbol repeats.
        for _ in range(10):
            await improver.run_cycle()

        baseline_families = [config.candidates[0].family for config in sweeps.configs]
        assert baseline_families == [
            "trend_following",  # the production grid leads with the trend baseline
            "breakout",
            "momentum",
            "squeeze",
            "supertrend",
            "funding",
            "trend_following",
            "breakout",
            "momentum",
            "squeeze",
        ]
        # Six targets now, so the symbol advances after every sixth cycle.
        assert [config.symbol for config in sweeps.configs] == [
            "BTC/USDT",
            "BTC/USDT",
            "BTC/USDT",
            "BTC/USDT",
            "BTC/USDT",
            "BTC/USDT",
            "ETH/USDT",
            "ETH/USDT",
            "ETH/USDT",
            "ETH/USDT",
        ]

    async def test_a_target_without_a_fresh_run_gets_evaluated_first(self) -> None:
        """Cycle two serves breakout; with only production runs on record it
        starts a breakout evaluation instead of sweeping blind."""
        sweeps = ScriptedSweeps()
        evaluations = ScriptedEvaluations()
        store = ScriptedStore("completed", {"verdict": "baseline_best"})  # production run only
        improver = make_improver(sweeps, store, [], evaluations=evaluations)

        await improver.run_cycle()  # production: sweeps
        await improver.run_cycle()  # breakout: no run -> evaluates

        assert len(sweeps.configs) == 1
        (started,) = evaluations.configs
        assert started.strategy == "breakout"

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

    async def test_accepted_findings_curate_the_cycle_grid(self) -> None:
        """Once a human accepts, proposed findings wait their turn."""
        sweeps = ScriptedSweeps()
        promoted: list[tuple[str, Mapping[str, Any], int | None, str | None]] = []
        store = ScriptedStore(
            "completed",
            {"verdict": "baseline_best", "winner": "active_trend_20_50"},
            findings=[
                make_finding(31, "entries lose money when trend is down", status="accepted"),
                make_finding(32, "entries chase moves that are already over"),
            ],
        )
        improver = make_improver(sweeps, store, promoted)

        await improver.run_cycle()

        (config,) = sweeps.configs
        assert config.motivating_finding_ids == (31,)
        assert all(candidate.name != "anti_chase" for candidate in config.candidates)

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


class TestPerFamilyGrids:
    def test_the_breakout_grid_is_single_family_and_buildable(self) -> None:
        from tradebot.evaluation.improve import build_candidates_for

        candidates, motivating = build_candidates_for("breakout", {})
        assert candidates[0].name.startswith("active_breakout")
        assert {candidate.family for candidate in candidates} == {"breakout"}
        assert motivating == ()
        names = {candidate.name for candidate in candidates}
        assert {"wider_channel", "tighter_channel", "wider_stop", "tighter_stop"} <= names
        for candidate in candidates:
            build_candidate_strategy(candidate)

    def test_fake_breakout_losses_toggle_the_min_width_filter(self) -> None:
        from tradebot.evaluation.improve import build_candidates_for

        candidates, motivating = build_candidates_for(
            "breakout", {}, [(9, "entries lose money when event is breakout_fake")]
        )
        widths = next(c for c in candidates if c.name == "min_width_filter")
        assert widths.params["min_channel_width_atr"] == 0.5
        volumes = next(c for c in candidates if c.name == "volume_confirm")
        assert volumes.params["min_volume_ratio"] == 1.0
        assert motivating == (9,)

    def test_an_active_volume_filter_toggles_off_for_fake_breakouts(self) -> None:
        from tradebot.evaluation.improve import build_candidates_for

        candidates, _ = build_candidates_for(
            "breakout",
            {"breakout": {"min_volume_ratio": 1.5}},
            [(9, "entries lose money when event is breakout_fake")],
        )
        toggled = next(c for c in candidates if c.name == "no_volume_confirm")
        assert toggled.params["min_volume_ratio"] == 0.0

    def test_an_early_exit_finding_lengthens_the_breakout_exit_channel(self) -> None:
        from tradebot.evaluation.improve import build_candidates_for

        candidates, _ = build_candidates_for(
            "breakout", {}, [(5, "exits cut winners while the move keeps going")]
        )
        later = next(c for c in candidates if c.name == "later_channel_exit")
        assert later.params["exit_channel_period"] == 15  # 10 * 1.5

    def test_the_momentum_grid_toggles_its_zero_line_filter_by_finding(self) -> None:
        from tradebot.evaluation.improve import build_candidates_for

        # Filter on (the default): staying flat tests removing it...
        flat, flat_motivating = build_candidates_for(
            "momentum", {}, [(7, "the bot stays flat through moves worth taking")]
        )
        assert any(c.name == "no_zero_line_filter" for c in flat)
        assert flat_motivating == (7,)
        # ...and chasing cannot test adding it — it is already on — but the
        # volume-confirmation toggle still gives the finding a knob to test.
        chase, chase_motivating = build_candidates_for(
            "momentum", {}, [(8, "entries chase moves that are already over")]
        )
        assert all(c.name != "zero_line_filter" for c in chase)
        assert any(c.name == "volume_confirm" for c in chase)
        assert chase_motivating == (8,)
        # With the filter off, losing entries test turning it on.
        losing, losing_motivating = build_candidates_for(
            "momentum",
            {"momentum": {"require_positive_macd": False}},
            [(9, "entries lose money when trend is down")],
        )
        assert any(c.name == "zero_line_filter" for c in losing)
        assert losing_motivating == (9,)
        for candidate in (*flat, *chase, *losing):
            build_candidate_strategy(candidate)

    def test_momentum_ema_steps_clamp_like_the_trend_familys(self) -> None:
        from tradebot.evaluation.improve import build_candidates_for

        candidates, _ = build_candidates_for(
            "momentum", {"momentum": {"fast_ema_period": 3, "slow_ema_period": 5}}
        )
        for candidate in candidates:
            build_candidate_strategy(candidate)  # raises if fast >= slow

    def test_the_squeeze_grid_is_single_family_and_buildable(self) -> None:
        from tradebot.evaluation.improve import build_candidates_for

        candidates, motivating = build_candidates_for("squeeze", {})
        assert candidates[0].name.startswith("active_squeeze")
        assert {candidate.family for candidate in candidates} == {"squeeze"}
        assert motivating == ()
        names = {candidate.name for candidate in candidates}
        assert {"looser_squeeze", "tighter_squeeze", "wider_stop", "tighter_stop"} <= names
        for candidate in candidates:
            build_candidate_strategy(candidate)

    def test_the_funding_grid_is_single_family_and_buildable(self) -> None:
        from tradebot.evaluation.improve import build_candidates_for

        candidates, motivating = build_candidates_for("funding", {})
        assert candidates[0].name.startswith("active_funding")
        assert {candidate.family for candidate in candidates} == {"funding"}
        assert motivating == ()
        names = {candidate.name for candidate in candidates}
        assert {"deeper_entry", "shallower_entry", "later_exit", "wider_stop"} <= names
        # Each variant must keep entry below exit, or FundingStrategy rejects it.
        for candidate in candidates:
            build_candidate_strategy(candidate)

    def test_losing_squeeze_entries_tighten_it_and_add_volume_confirmation(self) -> None:
        from tradebot.evaluation.improve import build_candidates_for

        candidates, motivating = build_candidates_for(
            "squeeze", {}, [(9, "entries lose money when trend is down")]
        )
        stricter = next(c for c in candidates if c.name == "stricter_squeeze")
        assert stricter.params["keltner_atr_multiple"] == 0.9  # round(1.5 * 0.6, 2)
        volume = next(c for c in candidates if c.name == "volume_confirm")
        assert volume.params["min_volume_ratio"] == 1.0
        assert motivating == (9,)
        for candidate in candidates:
            build_candidate_strategy(candidate)

    def test_an_early_exit_finding_adds_a_squeeze_trail(self) -> None:
        from tradebot.evaluation.improve import build_candidates_for

        candidates, _ = build_candidates_for(
            "squeeze", {}, [(5, "exits cut winners while the move keeps going")]
        )
        trail = next(c for c in candidates if c.name == "atr_trailing")
        assert trail.params["trail_atr_multiple"] == trail.params["atr_stop_multiple"]

    def test_solo_production_families_share_the_production_grid(self) -> None:
        from tradebot.evaluation.improve import build_candidates_for

        production, _ = build_candidates_for("production", {})
        solo_trend, _ = build_candidates_for("trend_following", {})
        assert [c.name for c in solo_trend] == [c.name for c in production]

    def test_unknown_targets_are_refused_loudly(self) -> None:
        import pytest

        from tradebot.evaluation.improve import build_candidates_for

        with pytest.raises(ValueError, match="no improvement grid"):
            build_candidates_for("custom-my-recipe", {})


class TestRecipeCandidates:
    def test_every_variant_is_the_whole_recipe_baseline_first(self) -> None:
        from tradebot.evaluation.improve import build_recipe_candidates

        recipe: dict[str, Any] = {
            "entry_mode": "any",
            "families": {"trend_following": {}, "breakout": {}},
        }
        candidates, _ = build_recipe_candidates(recipe)

        assert candidates[0].name == "active_recipe"
        assert candidates[0].recipe == recipe
        assert all(c.recipe is not None for c in candidates)
        # Variants vary one family in place; the other family stays at baseline.
        trend_variant = next(c for c in candidates if c.name.startswith("trend_following:"))
        assert trend_variant.recipe is not None
        assert trend_variant.recipe["families"]["breakout"] == recipe["families"]["breakout"]
        assert trend_variant.recipe["families"]["trend_following"] != {}
        # Both families contribute challengers, and every candidate builds.
        prefixes = {c.name.split(":")[0] for c in candidates if ":" in c.name}
        assert prefixes == {"trend_following", "breakout"}
        for candidate in candidates:
            build_candidate_strategy(candidate)

    def test_findings_lift_a_family_knob_into_recipe_space(self) -> None:
        from tradebot.evaluation.improve import build_recipe_candidates

        recipe: dict[str, Any] = {"entry_mode": "any", "families": {"breakout": {}}}
        candidates, motivating = build_recipe_candidates(
            recipe, [(9, "entries lose money when event is breakout_fake")]
        )

        widened = next(c for c in candidates if c.name == "breakout:min_width_filter")
        assert widened.recipe is not None
        assert widened.recipe["families"]["breakout"]["min_channel_width_atr"] == 0.5
        assert 9 in motivating

    def test_a_single_family_recipe_preserves_its_entry_mode(self) -> None:
        from tradebot.evaluation.improve import build_recipe_candidates

        recipe: dict[str, Any] = {
            "entry_mode": "all",
            "families": {"momentum": {}, "trend_following": {}},
        }
        candidates, _ = build_recipe_candidates(recipe)
        assert all(c.recipe is not None and c.recipe["entry_mode"] == "all" for c in candidates)


class TestSelectTargetingFindings:
    def test_accepted_findings_outrank_proposed(self) -> None:
        findings = [
            make_finding(1, "entries lose money when trend is down", "accepted"),
            make_finding(2, "entries chase moves that are already over", "proposed"),
            make_finding(3, "held positions ride into their stops", "rejected"),
        ]
        from tradebot.evaluation.improve import select_targeting_findings

        assert select_targeting_findings(findings) == [(1, "entries lose money when trend is down")]

    def test_without_verdicts_every_non_rejected_finding_steers(self) -> None:
        from tradebot.evaluation.improve import select_targeting_findings

        findings = [
            make_finding(1, "entries lose money when trend is down", "proposed"),
            make_finding(2, "held positions ride into their stops", "rejected"),
        ]
        assert select_targeting_findings(findings) == [(1, "entries lose money when trend is down")]


class TestExpandedKnobMap:
    def test_an_early_exit_finding_adds_trail_and_later_exit_challengers(self) -> None:
        candidates, motivating = build_improvement_candidates(
            {}, [(5, "exits cut winners while the move keeps going")]
        )
        names = {candidate.name for candidate in candidates}
        assert "atr_trailing" in names
        assert "later_reversion_exit" in names
        assert motivating == (5,)
        later = next(c for c in candidates if c.name == "later_reversion_exit")
        assert later.params["exit_rsi"] == 66.0  # 55 * 1.2, under the 80 cap
        for candidate in candidates:
            build_candidate_strategy(candidate)

    def test_the_later_exit_is_capped_at_a_sane_rsi(self) -> None:
        candidates, _ = build_improvement_candidates(
            {"mean_reversion": {"exit_rsi": 75.0}},
            [(5, "exits cut winners while the move keeps going")],
        )
        later = next(c for c in candidates if c.name == "later_reversion_exit")
        assert later.params["exit_rsi"] == 80.0  # capped, not 90

    def test_a_missed_opportunity_finding_loosens_the_oversold_gate(self) -> None:
        candidates, motivating = build_improvement_candidates(
            {}, [(7, "the bot stays flat through moves worth taking")]
        )
        looser = next(c for c in candidates if c.name == "looser_oversold")
        assert looser.params["oversold_threshold"] == 36.0  # 30 * 1.2
        assert motivating == (7,)
        for candidate in candidates:
            build_candidate_strategy(candidate)

    def test_the_loosened_gate_never_crosses_its_own_exit(self) -> None:
        """An oversold threshold already near the exit midline is left alone."""
        candidates, _ = build_improvement_candidates(
            {"mean_reversion": {"oversold_threshold": 50.0, "exit_rsi": 55.0}},
            [(7, "the bot stays flat through moves worth taking")],
        )
        assert all(candidate.name != "looser_oversold" for candidate in candidates)

    def test_overlapping_findings_share_the_trail_candidate(self) -> None:
        """Wrong-hold and early-exit both toggle the trail; the grid holds one."""
        candidates, _ = build_improvement_candidates(
            {},
            [
                (5, "exits cut winners while the move keeps going"),
                (6, "held positions ride into their stops"),
            ],
        )
        names = [candidate.name for candidate in candidates]
        assert names.count("atr_trailing") == 1


class TestImprovementStatusTracking:
    """The status surface mirrors what each cycle did, in plain words."""

    async def test_a_promotion_lands_in_the_status(self) -> None:
        sweeps = ScriptedSweeps()
        promoted: list[tuple[str, Mapping[str, Any], int | None, str | None]] = []
        store = ScriptedStore(
            "completed",
            {"verdict": "validated", "winner": "tighter_stop", "explanation": "it won"},
        )
        improver = make_improver(sweeps, store, promoted)

        await improver.run_cycle()

        assert improver.status.last_outcome is not None
        assert "auto-promoted" in improver.status.last_outcome
        assert improver.status.last_cycle_started_at is not None
        assert improver.status.last_cycle_finished_at is not None
        assert improver.status.last_cycle_finished_at >= improver.status.last_cycle_started_at

    async def test_a_kept_configuration_names_the_verdict(self) -> None:
        sweeps = ScriptedSweeps()
        promoted: list[tuple[str, Mapping[str, Any], int | None, str | None]] = []
        store = ScriptedStore("completed", {"verdict": "overfit", "winner": "tighter_stop"})
        improver = make_improver(sweeps, store, promoted)

        await improver.run_cycle()

        assert improver.status.last_outcome is not None
        assert "kept the active configuration" in improver.status.last_outcome
        assert "overfit" in improver.status.last_outcome

    async def test_a_busy_sweep_reports_the_skip(self) -> None:
        sweeps = ScriptedSweeps(running=True)
        promoted: list[tuple[str, Mapping[str, Any], int | None, str | None]] = []
        store = ScriptedStore("completed", None)
        improver = make_improver(sweeps, store, promoted)

        await improver.run_cycle()

        assert improver.status.last_outcome is not None
        assert "another sweep is already in flight" in improver.status.last_outcome

    async def test_a_refresh_evaluation_reports_the_run_id(self) -> None:
        sweeps = ScriptedSweeps()
        promoted: list[tuple[str, Mapping[str, Any], int | None, str | None]] = []
        store = ScriptedStore("completed", None, runs=[])  # nothing to learn from yet
        evaluations = ScriptedEvaluations()
        improver = make_improver(sweeps, store, promoted, evaluations=evaluations)

        await improver.run_cycle()

        assert improver.status.last_outcome is not None
        assert "started evaluation run #1" in improver.status.last_outcome


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
        """Patterns with no mapped knob stay human-facing — e.g. volatility
        buckets: no production family carries a volatility entry filter."""
        baseline, _ = build_improvement_candidates({})
        candidates, motivating = build_improvement_candidates(
            {}, findings=[(3, "entries lose money when volatility is low")]
        )
        assert len(candidates) == len(baseline)
        assert motivating == ()


class TestScale:
    """Coarse-to-fine refine: ``scale`` shrinks the base grid toward the baseline."""

    def test_scale_one_reproduces_the_default_grid(self) -> None:
        active = {"trend_following": {"fast_ema_period": 20, "slow_ema_period": 50}}
        default, _ = build_improvement_candidates(active)
        explicit, _ = build_improvement_candidates(active, scale=1.0)
        assert [c.name for c in default] == [c.name for c in explicit]
        assert [c.params for c in default] == [c.params for c in explicit]

    def test_a_finer_scale_steps_closer_to_the_baseline(self) -> None:
        active = {
            "trend_following": {
                "fast_ema_period": 20,
                "slow_ema_period": 50,
                "atr_stop_multiple": 2.0,
            }
        }
        coarse = {c.name: c for c in build_improvement_candidates(active, scale=1.0)[0]}
        fine = {c.name: c for c in build_improvement_candidates(active, scale=0.5)[0]}
        # faster_cross drops the fast EMA from 20; the finer step stays closer.
        assert coarse["faster_cross"].params["fast_ema_period"] == 12  # round(20 * 0.6)
        assert fine["faster_cross"].params["fast_ema_period"] == 16  # round(20 * 0.8)
        # wider_stop widens the 2.0 ATR stop; the finer step widens it less.
        assert coarse["wider_stop"].params["atr_stop_multiple"] == 3.0
        assert fine["wider_stop"].params["atr_stop_multiple"] == 2.5

    def test_scale_zero_collapses_every_base_variant_into_the_baseline(self) -> None:
        active = {
            "trend_following": {"fast_ema_period": 20, "slow_ema_period": 50},
            "mean_reversion": {},
        }
        candidates, _ = build_improvement_candidates(active, scale=0.0)
        # Every magnitude step is scaled by 1.0 now, so the base variants equal the
        # baseline and dedup leaves only the two family baselines.
        assert [c.name for c in candidates] == [candidates[0].name, "active_reversion"]
        assert {c.family for c in candidates} == {"trend_following", "mean_reversion"}

    def test_scale_threads_through_to_a_research_family(self) -> None:
        from tradebot.evaluation.improve import build_candidates_for

        coarse, _ = build_candidates_for("breakout", {}, scale=1.0)
        collapsed, _ = build_candidates_for("breakout", {}, scale=0.0)
        assert len(coarse) > 1  # the usual channel/stop neighbourhood
        assert len(collapsed) == 1  # all base variants collapse into the baseline
        assert collapsed[0].name.startswith("active_breakout")

    def test_an_out_of_range_scale_is_rejected(self) -> None:
        import pytest

        from tradebot.evaluation.improve import build_candidates_for

        for bad in (-0.1, 1.5):
            with pytest.raises(ValueError, match="scale must be between"):
                build_candidates_for("production", {}, scale=bad)


class TestCandidateProvider:
    async def test_builds_the_target_grid_from_the_active_params(self) -> None:
        from tradebot.evaluation.improve import make_candidate_provider

        store = ScriptedStore(
            "completed", None, runs=[fresh_completed_run(strategy="momentum")], findings=[]
        )
        provider = make_candidate_provider("momentum", store)

        candidates, motivating = await provider(
            {"momentum": {"fast_ema_period": 12, "slow_ema_period": 26}}, 1.0
        )

        assert candidates[0].name.startswith("active_momentum")
        assert candidates[0].family == "momentum"
        assert motivating == ()

    async def test_threads_scale_into_the_grid(self) -> None:
        from tradebot.evaluation.improve import make_candidate_provider

        store = ScriptedStore(
            "completed", None, runs=[fresh_completed_run(strategy="momentum")], findings=[]
        )
        provider = make_candidate_provider("momentum", store)

        coarse, _ = await provider({}, 1.0)
        collapsed, _ = await provider({}, 0.0)

        assert len(coarse) > 1
        assert len(collapsed) == 1  # scale 0 collapses every variant into the baseline

    async def test_findings_steer_the_grid_and_ride_as_motivation(self) -> None:
        from tradebot.evaluation.improve import make_candidate_provider

        store = ScriptedStore(
            "completed",
            None,
            runs=[fresh_completed_run(strategy="momentum")],
            findings=[make_finding(7, "entries chase moves already over")],
        )
        provider = make_candidate_provider("momentum", store)

        candidates, motivating = await provider({}, 1.0)

        assert 7 in motivating
        assert any("volume_confirm" in candidate.name for candidate in candidates)

    async def test_no_completed_run_means_no_findings_just_the_base_grid(self) -> None:
        from tradebot.evaluation.improve import make_candidate_provider

        store = ScriptedStore("completed", None, runs=[])
        provider = make_candidate_provider("momentum", store)

        candidates, motivating = await provider({}, 1.0)

        assert motivating == ()
        assert candidates[0].name.startswith("active_momentum")


class TestCampaignStandDown:
    async def test_stands_down_while_a_campaign_runs(self) -> None:
        sweeps = ScriptedSweeps()
        store = ScriptedStore("completed", None, runs=runs_for_every_target())
        improver = make_improver(sweeps, store, [], campaign_active=lambda: True)

        assert await improver.run_cycle() is None
        assert sweeps.configs == []  # stood down: never started a sweep
        assert "stood down" in (improver.status.last_outcome or "")

    async def test_runs_normally_when_no_campaign_is_active(self) -> None:
        sweeps = ScriptedSweeps()
        store = ScriptedStore("completed", None, runs=runs_for_every_target())
        improver = make_improver(sweeps, store, [], campaign_active=lambda: False)

        await improver.run_cycle()
        assert sweeps.configs  # not stood down: a sweep ran


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
            "strategy": "production",
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
