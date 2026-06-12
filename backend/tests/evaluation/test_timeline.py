"""The research timeline: merged feed, plain-words headlines, finding diffs."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from tradebot.evaluation.models import LearningFinding
from tradebot.evaluation.timeline import TimelineEvent, build_timeline

BASE_TIME = datetime(2026, 6, 1, 0, 0, tzinfo=UTC)


class ScriptedRecord:
    """Stands in for the EvaluationStore's read surface."""

    def __init__(
        self,
        runs: list[dict[str, Any]] | None = None,
        sweeps: list[dict[str, Any]] | None = None,
        findings: dict[int, list[tuple[int, LearningFinding]]] | None = None,
    ) -> None:
        self.runs = runs or []
        self.sweeps = sweeps or []
        self.findings = findings or {}

    async def list_runs(self, limit: int = 50) -> list[dict[str, Any]]:
        return list(self.runs[:limit])

    async def list_sweeps(self, limit: int = 50) -> list[dict[str, Any]]:
        return list(self.sweeps[:limit])

    async def fetch_findings_for_runs(
        self, run_ids: list[int]
    ) -> dict[int, list[tuple[int, LearningFinding]]]:
        return {run_id: self.findings[run_id] for run_id in run_ids if run_id in self.findings}


class ScriptedJournal:
    def __init__(self, versions: list[dict[str, Any]] | None = None) -> None:
        self.versions = versions or []

    async def history(self, limit: int = 50) -> list[dict[str, Any]]:
        return list(self.versions[:limit])


def make_run(
    run_id: int,
    *,
    strategy: str = "production",
    status: str = "completed",
    minutes: int = 0,
    summary: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "id": run_id,
        "strategy": strategy,
        "status": status,
        "created_at": BASE_TIME + timedelta(minutes=minutes),
        "summary": summary
        if summary is not None
        else {"expectancy_r": "-0.1296", "trade_count": 187},
    }


def make_finding(run_id: int, pattern: str, status: str = "proposed") -> LearningFinding:
    return LearningFinding(
        run_id=run_id,
        pattern=pattern,
        evidence_scenario_ids=(1,),
        affected_count=1,
        average_r_impact=Decimal("-0.5"),
        suggestion="test",
        confidence="low",
        status=status,
        created_at=BASE_TIME,
    )


def make_sweep(
    sweep_id: int,
    *,
    status: str = "completed",
    minutes: int = 0,
    report: dict[str, Any] | None = None,
    motivating: list[int] | None = None,
) -> dict[str, Any]:
    return {
        "id": sweep_id,
        "symbol": "BTC/USDT",
        "status": status,
        "created_at": BASE_TIME + timedelta(minutes=minutes),
        "report": report,
        "motivating_finding_ids": motivating or [],
    }


async def test_completed_run_headline_carries_expectancy_and_findings() -> None:
    record = ScriptedRecord(
        runs=[make_run(1)],
        findings={
            1: [
                (10, make_finding(1, "entries lose money when trend is down", "accepted")),
                (11, make_finding(1, "held positions ride into their stops", "rejected")),
            ]
        },
    )

    events = await build_timeline(record, ScriptedJournal())

    assert len(events) == 1
    event = events[0]
    assert event.kind == "evaluation"
    assert "run #1 graded production: -0.1296R per trade over 187 trades" in event.headline
    assert event.detail is not None
    assert "mined 2 finding(s) (1 accepted, 1 rejected so far)" in event.detail
    assert event.expectancy_r == "-0.1296"
    # No predecessor in view: the diff is honestly absent, not "everything new".
    assert event.new_patterns == ()
    assert event.resolved_patterns == ()


async def test_successive_runs_of_one_bot_diff_their_patterns() -> None:
    record = ScriptedRecord(
        runs=[make_run(2, minutes=60), make_run(1)],  # newest first, like the store
        findings={
            1: [
                (10, make_finding(1, "entries lose money when trend is down")),
                (11, make_finding(1, "entries chase moves that are already over")),
            ],
            2: [
                (20, make_finding(2, "entries lose money when trend is down")),
                (21, make_finding(2, "held positions ride into their stops")),
            ],
        },
    )

    events = await build_timeline(record, ScriptedJournal())

    newest = events[0]
    assert newest.run_id == 2
    assert newest.new_patterns == ("held positions ride into their stops",)
    assert newest.resolved_patterns == ("entries chase moves that are already over",)
    assert newest.detail is not None
    assert "vs run #1: 1 new pattern(s), 1 no longer firing" in newest.detail


async def test_runs_of_different_bots_never_diff_against_each_other() -> None:
    record = ScriptedRecord(
        runs=[make_run(2, strategy="breakout", minutes=60), make_run(1)],
        findings={
            1: [(10, make_finding(1, "entries lose money when trend is down"))],
            2: [(20, make_finding(2, "entries chase moves that are already over"))],
        },
    )

    events = await build_timeline(record, ScriptedJournal())

    assert events[0].run_id == 2
    assert events[0].new_patterns == ()
    assert events[0].resolved_patterns == ()


async def test_sweep_and_promotion_events_tell_their_story() -> None:
    record = ScriptedRecord(
        sweeps=[
            make_sweep(
                4,
                minutes=10,
                report={
                    "verdict": "validated",
                    "winner": "tighter_stop",
                    "explanation": "tighter_stop beat the baseline",
                },
                motivating=[10, 11],
            )
        ]
    )
    journal = ScriptedJournal(
        versions=[
            {
                "id": 7,
                "family": "trend_following",
                "params": {},
                "source_sweep_id": 4,
                "note": "auto-promoted: tighter_stop beat the baseline",
                "activated_at": BASE_TIME + timedelta(minutes=20),
            }
        ]
    )

    events = await build_timeline(record, journal)

    assert [event.kind for event in events] == ["promotion", "sweep"]
    promotion, sweep = events
    assert promotion.headline == "settings v7 activated for trend_following (from sweep #4)"
    assert promotion.detail == "auto-promoted: tighter_stop beat the baseline"
    assert promotion.version_id == 7 and promotion.sweep_id == 4
    assert sweep.verdict == "validated"
    assert "tighter_stop beat the baseline out of sample" in sweep.headline
    assert sweep.detail is not None and "motivated by 2 finding(s)" in sweep.detail


async def test_in_flight_work_is_excluded_and_failures_are_not() -> None:
    record = ScriptedRecord(
        runs=[
            make_run(3, status="running", minutes=30),
            make_run(2, status="failed", minutes=20),
            make_run(1, minutes=10),
        ],
        sweeps=[make_sweep(5, status="running", minutes=40)],
    )

    events = await build_timeline(record, ScriptedJournal())

    assert [event.run_id for event in events] == [2, 1]
    assert events[0].headline == "run #2 (production) failed"


async def test_feed_is_newest_first_and_capped() -> None:
    record = ScriptedRecord(runs=[make_run(run_id, minutes=run_id) for run_id in (3, 2, 1)])

    events = await build_timeline(record, ScriptedJournal(), limit=2)

    assert [event.run_id for event in events] == [3, 2]
    assert all(isinstance(event, TimelineEvent) for event in events)
