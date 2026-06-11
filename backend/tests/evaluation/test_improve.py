"""Automated improvement: candidate derivation and the promote-on-validated loop."""

import asyncio
import contextlib
from collections.abc import Awaitable, Callable, Mapping
from datetime import timedelta
from typing import Any

from tradebot.evaluation.improve import AutoImprover, build_improvement_candidates
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
    """Returns a scripted terminal sweep row on first poll."""

    def __init__(self, status: str, report: dict[str, Any] | None) -> None:
        self._row = {"status": status, "report": report}

    async def fetch_sweep(self, sweep_id: int) -> dict[str, Any] | None:
        return dict(self._row)


def make_improver(
    sweeps: ScriptedSweeps,
    store: ScriptedStore,
    promoted: list[tuple[str, Mapping[str, Any], int | None, str | None]],
    symbols: tuple[str, ...] = ("BTC/USDT", "ETH/USDT"),
    notify: Callable[[str], Awaitable[None]] | None = None,
) -> AutoImprover:
    async def promote(
        family: str, params: Mapping[str, Any], sweep_id: int | None, note: str | None
    ) -> int:
        promoted.append((family, params, sweep_id, note))
        return len(promoted)

    return AutoImprover(
        sweeps=sweeps,
        store=store,
        active_params=lambda: {"trend_following": {"fast_ema_period": 20}},
        symbols=lambda: symbols,
        promote=promote,
        interval=timedelta(hours=12),
        history_days=180,
        timeframe="1h",
        notify=notify,
    )


class TestBuildImprovementCandidates:
    def test_active_config_is_the_baseline_and_every_candidate_builds(self) -> None:
        candidates = build_improvement_candidates(
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
        candidates = build_improvement_candidates(
            {"trend_following": {"fast_ema_period": 3, "slow_ema_period": 5}}
        )
        for candidate in candidates:
            build_candidate_strategy(candidate)  # raises if fast >= slow

    def test_variants_that_collapse_into_duplicates_are_dropped(self) -> None:
        """A stop already at the floor makes tighter == active; drop it."""
        candidates = build_improvement_candidates({"trend_following": {"atr_stop_multiple": 0.5}})
        keys = [(c.family, tuple(sorted(c.params.items()))) for c in candidates]
        assert len(set(keys)) == len(keys)

    def test_both_families_compete(self) -> None:
        families = {candidate.family for candidate in build_improvement_candidates({})}
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
