"""Campaign-driver tests: target-aware strategy build, rotation, and assembly.

The driver is pure wiring, so the fakes are minimal: a research object that
scripts one verdict for every sweep (and serves as both the sweep starter
and the research store) and an empty candle reader (so the holdout read is
thin and nothing heavy runs). That keeps the focus on the driver's own job —
build the right strategy per target, rotate, and run a campaign end to end.
"""

import asyncio
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from tradebot.core.models import Candle, CandleInterval
from tradebot.evaluation.campaign import CampaignStatus
from tradebot.evaluation.campaign_driver import (
    CampaignDriver,
    CampaignDriverConfig,
    strategy_for_target,
)
from tradebot.evaluation.improve import IMPROVEMENT_TARGETS
from tradebot.evaluation.models import LearningFinding
from tradebot.evaluation.strategy import SelfRoutedRegimeStrategy
from tradebot.evaluation.sweep import SweepConfig

_NOW = datetime(2026, 6, 15, tzinfo=UTC)


class TestStrategyForTarget:
    def test_production_routed_builds_the_regime_router(self) -> None:
        strategy = strategy_for_target("production", regime_routed=True)(
            {"trend_following": {}, "mean_reversion": {}}
        )
        assert isinstance(strategy, SelfRoutedRegimeStrategy)

    def test_production_unrouted_builds_the_trend_family_alone(self) -> None:
        strategy = strategy_for_target("production", regime_routed=False)({})
        assert strategy.name == "trend_following"

    def test_a_research_family_builds_that_family(self) -> None:
        strategy = strategy_for_target("momentum", regime_routed=True)({"momentum": {}})
        assert strategy.name == "momentum"


class DriverResearch:
    """Sweep starter + research store, scripting one verdict for every sweep."""

    def __init__(self, verdict: str = "baseline_best") -> None:
        self._verdict = verdict
        self.started: list[SweepConfig] = []

    async def start(self, config: SweepConfig) -> int:
        self.started.append(config)
        return len(self.started)

    async def fetch_sweep(self, sweep_id: int) -> dict[str, Any] | None:
        return {
            "status": "completed",
            "report": {"verdict": self._verdict, "winner": "baseline", "explanation": "kept"},
        }

    async def list_runs(self, limit: int = 50) -> list[dict[str, Any]]:
        return []

    async def fetch_findings(self, run_id: int) -> list[tuple[int, LearningFinding]]:
        return []


class NoCandles:
    """A candle reader with nothing in the holdout span (a thin read, no grading)."""

    async def fetch_range(
        self, symbol: str, interval: CandleInterval, start: datetime, end: datetime
    ) -> list[Candle]:
        return []


async def _promote(
    family: str, params: Mapping[str, Any], sweep_id: int | None, note: str | None
) -> int:
    return 1


def _no_active_params() -> Mapping[str, Mapping[str, Any]]:
    return {}


def _driver(research: DriverResearch, symbols: tuple[str, ...] = ("BTC/USDT",)) -> CampaignDriver:
    return CampaignDriver(
        sweeps=research,
        store=research,
        candle_store=NoCandles(),
        active_params=_no_active_params,
        symbols=lambda: symbols,
        promote=_promote,
        confirm=None,
        config=CampaignDriverConfig(max_rounds=1, cooldown_minutes=0.001, scenario_count=10),
        clock=lambda: _NOW,
    )


class TestCampaignDriver:
    async def test_runs_a_campaign_for_the_first_target(self) -> None:
        research = DriverResearch("baseline_best")
        driver = _driver(research)

        status = await driver.run_one()

        assert status is not None
        assert status.config.target == "production"  # first on the rotation
        assert status.config.symbol == "BTC/USDT"
        assert status.status == "completed"
        assert research.started  # a sweep was actually launched
        assert driver.current is status

    async def test_records_each_finished_campaign(self) -> None:
        research = DriverResearch("baseline_best")
        recorded: list[CampaignStatus] = []

        async def record(status: CampaignStatus) -> None:
            recorded.append(status)

        driver = CampaignDriver(
            sweeps=research,
            store=research,
            candle_store=NoCandles(),
            active_params=_no_active_params,
            symbols=lambda: ("BTC/USDT",),
            promote=_promote,
            confirm=None,
            config=CampaignDriverConfig(max_rounds=1, cooldown_minutes=0.001, scenario_count=10),
            clock=lambda: _NOW,
            record=record,
        )

        status = await driver.run_one()

        # The finished campaign is handed to the durable history exactly once.
        assert status is not None and status.status == "completed"
        assert recorded == [status]

    async def test_rotation_advances_through_every_target_then_wraps(self) -> None:
        driver = _driver(DriverResearch("baseline_best"))

        targets: list[str] = []
        for _ in range(len(IMPROVEMENT_TARGETS) + 1):
            status = await driver.run_one()
            assert status is not None
            targets.append(status.config.target)

        assert targets[: len(IMPROVEMENT_TARGETS)] == list(IMPROVEMENT_TARGETS)
        assert targets[-1] == IMPROVEMENT_TARGETS[0]  # wraps back to the first

    async def test_no_active_coins_skips(self) -> None:
        driver = _driver(DriverResearch(), symbols=())

        assert await driver.run_one() is None

    async def test_run_one_idles_when_the_toggle_is_off(self) -> None:
        research = DriverResearch("baseline_best")
        driver = CampaignDriver(
            sweeps=research,
            store=research,
            candle_store=NoCandles(),
            active_params=_no_active_params,
            symbols=lambda: ("BTC/USDT",),
            promote=_promote,
            confirm=None,
            config=CampaignDriverConfig(max_rounds=1, cooldown_minutes=0.001, scenario_count=10),
            clock=lambda: _NOW,
            enabled=lambda: False,
        )

        assert await driver.run_one() is None
        assert research.started == []  # gated off: never started a sweep
        assert driver.current is None

    async def test_run_one_runs_when_the_toggle_is_on(self) -> None:
        research = DriverResearch("baseline_best")
        driver = CampaignDriver(
            sweeps=research,
            store=research,
            candle_store=NoCandles(),
            active_params=_no_active_params,
            symbols=lambda: ("BTC/USDT",),
            promote=_promote,
            confirm=None,
            config=CampaignDriverConfig(max_rounds=1, cooldown_minutes=0.001, scenario_count=10),
            clock=lambda: _NOW,
            enabled=lambda: True,
        )

        status = await driver.run_one()
        assert status is not None and status.status == "completed"
        assert research.started  # gated on: ran a campaign

    async def test_current_is_published_while_a_campaign_runs(self) -> None:
        gate = asyncio.Event()

        class GatedResearch(DriverResearch):
            async def list_runs(self, limit: int = 50) -> list[dict[str, Any]]:
                await gate.wait()  # hold the campaign open mid-run
                return []

        driver = _driver(GatedResearch("baseline_best"))
        task = asyncio.create_task(driver.run_one())
        for _ in range(10):  # let run() start and reach the gated lookup
            if driver.current is not None:
                break
            await asyncio.sleep(0)

        # The campaign is in flight, not finished — current already shows it.
        assert driver.current is not None
        assert driver.current.status == "running"
        assert driver.current.config.target == "production"

        gate.set()
        status = await task
        assert status is not None and status.status == "completed"
