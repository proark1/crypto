"""The research timeline: one feed of what the learning loop did (§12.8).

Read-side composition over the persisted research record — runs, findings,
sweeps, and the strategy-settings journal. The timeline never writes; it
exists so progress ("what did the loop try, what came of it, what changed")
reads as one story instead of four tables. Headlines are plain words,
composed server-side like sweep explanations, so every surface tells the
same sentence.

Finding lifecycle is computed here, not stored: a finding's ``pattern``
text is deterministic for a given mistake (frozen miners, §12.2), so the
same pattern appearing in successive runs of the same bot IS the
recurrence record. A completed run's event therefore reports which
patterns are new versus its predecessor and which ones no longer fire —
the closest honest reading of "did the change help" without pretending two
different history windows are the same experiment.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict

from tradebot.core.models import UtcDatetime
from tradebot.evaluation.models import LearningFinding, RunStatus

WINDOW = 100
"""How many recent rows of each source feed the merge. Bounded on purpose:
the timeline is a story of recent progress, not an archive query."""

MAX_PATTERN_LIST = 5
"""Cap the per-event new/resolved pattern lists; past a handful the counts
say more than the prose."""

MAX_CHANGES = 8
"""Cap the per-promotion settings-change list. A strategy family carries a
handful of parameters; past a handful the note and version link carry the
rest."""


class ResearchRecord(Protocol):
    """The slice of ``EvaluationStore`` the timeline reads."""

    async def list_runs(self, limit: int = 50) -> list[dict[str, Any]]:
        """Return evaluation runs, newest first."""
        ...

    async def list_sweeps(self, limit: int = 50) -> list[dict[str, Any]]:
        """Return sweeps, newest first."""
        ...

    async def fetch_findings_for_runs(
        self, run_ids: list[int]
    ) -> dict[int, list[tuple[int, LearningFinding]]]:
        """Return findings grouped by run for a window of runs."""
        ...


class SettingsJournal(Protocol):
    """The slice of ``StrategySettingsStore`` the timeline reads."""

    async def history(self, limit: int = 50) -> list[dict[str, Any]]:
        """Return settings versions, newest first."""
        ...


class SettingChange(BaseModel):
    """One parameter a promotion changed, for the "what changed" line.

    ``before`` is ``None`` when the field is new in this version (no in-view
    predecessor set it); ``after`` is ``None`` when the field was dropped.
    Both are pre-stringified — a strategy parameter is never money and the
    timeline only displays the move, so this never does arithmetic on it.
    """

    model_config = ConfigDict(frozen=True)

    field: str
    before: str | None
    after: str | None


class TimelineEvent(BaseModel):
    """One entry in the research story, ready to render.

    ``headline``/``detail`` are the prose; the structured fields exist so
    the UI can tone and link without re-parsing sentences.
    """

    model_config = ConfigDict(frozen=True)

    at: UtcDatetime
    kind: str
    """"evaluation" | "sweep" | "promotion"."""

    headline: str
    detail: str | None = None
    status: str | None = None
    """Run/sweep terminal status; ``None`` for promotions."""

    strategy: str | None = None
    run_id: int | None = None
    sweep_id: int | None = None
    version_id: int | None = None
    expectancy_r: str | None = None
    verdict: str | None = None
    new_patterns: tuple[str, ...] = ()
    """Patterns mined in this run but not in the same bot's previous
    completed run (capped; only computed when a predecessor is in view)."""

    resolved_patterns: tuple[str, ...] = ()
    """Patterns the previous run mined that this run no longer does."""

    changes: tuple[SettingChange, ...] = ()
    """For a promotion: the field-level diff against the same family's
    previous in-view version — what this version actually changed. Empty for
    runs, sweeps, and a family's first in-view version (no truthful
    predecessor to diff against)."""


_TERMINAL_RUN = {
    RunStatus.COMPLETED.value,
    RunStatus.FAILED.value,
    RunStatus.INTERRUPTED.value,
}


async def build_timeline(
    record: ResearchRecord, settings: SettingsJournal, limit: int = 50
) -> list[TimelineEvent]:
    """Compose the merged feed, newest first, capped to ``limit`` events.

    In-flight runs and sweeps are excluded — the timeline is history; the
    improver status card carries what is happening right now.
    """
    runs = [run for run in await record.list_runs(WINDOW) if run["status"] in _TERMINAL_RUN]
    completed_ids = [run["id"] for run in runs if run["status"] == RunStatus.COMPLETED.value]
    findings_by_run = await record.fetch_findings_for_runs(completed_ids)
    events = [_run_event(run, runs, findings_by_run) for run in runs]
    events += [
        _sweep_event(sweep)
        for sweep in await record.list_sweeps(WINDOW)
        if sweep["status"] in _TERMINAL_RUN
    ]
    versions = await settings.history(WINDOW)
    events += [_promotion_event(version, versions) for version in versions]
    events.sort(key=lambda event: event.at, reverse=True)
    return events[:limit]


def _patterns(findings: list[tuple[int, LearningFinding]]) -> set[str]:
    return {finding.pattern for _, finding in findings}


def _previous_completed_run(
    run: dict[str, Any], runs: list[dict[str, Any]]
) -> dict[str, Any] | None:
    """Return the same bot's newest completed run before ``run``, if in view."""
    candidates = [
        other
        for other in runs
        if other["strategy"] == run["strategy"]
        and other["status"] == RunStatus.COMPLETED.value
        and other["id"] < run["id"]
    ]
    return max(candidates, key=lambda other: other["id"], default=None)


def _run_event(
    run: dict[str, Any],
    runs: list[dict[str, Any]],
    findings_by_run: dict[int, list[tuple[int, LearningFinding]]],
) -> TimelineEvent:
    run_id, strategy, status = run["id"], run["strategy"], run["status"]
    if status != RunStatus.COMPLETED.value:
        return TimelineEvent(
            at=run["created_at"],
            kind="evaluation",
            status=status,
            strategy=strategy,
            run_id=run_id,
            headline=f"run #{run_id} ({strategy}) {status}",
        )
    summary = run.get("summary") or {}
    expectancy = summary.get("expectancy_r")
    trade_count = summary.get("trade_count")
    headline = (
        f"run #{run_id} graded {strategy}: {expectancy}R per trade over {trade_count} trades"
        if expectancy is not None and trade_count
        else f"run #{run_id} graded {strategy}: no trades to grade"
    )
    findings = findings_by_run.get(run_id, [])
    new_patterns: tuple[str, ...] = ()
    resolved_patterns: tuple[str, ...] = ()
    diff_sentence = ""
    previous = _previous_completed_run(run, runs)
    if previous is not None:
        mined = _patterns(findings)
        before = _patterns(findings_by_run.get(previous["id"], []))
        new_patterns = tuple(sorted(mined - before))
        resolved_patterns = tuple(sorted(before - mined))
        diff_sentence = (
            f"; vs run #{previous['id']}: {len(new_patterns)} new pattern(s), "
            f"{len(resolved_patterns)} no longer firing"
        )
    accepted = sum(1 for _, finding in findings if finding.status == "accepted")
    rejected = sum(1 for _, finding in findings if finding.status == "rejected")
    detail = (
        f"mined {len(findings)} finding(s) ({accepted} accepted, {rejected} rejected so far)"
        f"{diff_sentence}"
    )
    return TimelineEvent(
        at=run["created_at"],
        kind="evaluation",
        status=status,
        strategy=strategy,
        run_id=run_id,
        headline=headline,
        detail=detail,
        expectancy_r=None if expectancy is None else str(expectancy),
        new_patterns=new_patterns[:MAX_PATTERN_LIST],
        resolved_patterns=resolved_patterns[:MAX_PATTERN_LIST],
    )


def _sweep_event(sweep: dict[str, Any]) -> TimelineEvent:
    sweep_id, status = sweep["id"], sweep["status"]
    if status != RunStatus.COMPLETED.value:
        return TimelineEvent(
            at=sweep["created_at"],
            kind="sweep",
            status=status,
            sweep_id=sweep_id,
            headline=f"sweep #{sweep_id} on {sweep['symbol']} {status}",
        )
    report = sweep.get("report") or {}
    verdict = report.get("verdict")
    winner = report.get("winner")
    headline = f"sweep #{sweep_id} on {sweep['symbol']}: {verdict}"
    if verdict == "validated" and winner:
        headline += f" — {winner} beat the baseline out of sample"
    motivating = list(sweep.get("motivating_finding_ids") or [])
    explanation = str(report.get("explanation", "")) or None
    detail = (
        f"motivated by {len(motivating)} finding(s) · {explanation}"
        if motivating and explanation
        else explanation
    )
    return TimelineEvent(
        at=sweep["created_at"],
        kind="sweep",
        status=status,
        sweep_id=sweep_id,
        headline=headline,
        detail=detail,
        verdict=None if verdict is None else str(verdict),
    )


def _previous_version(
    version: dict[str, Any], versions: list[dict[str, Any]]
) -> dict[str, Any] | None:
    """Return the same family's newest version before ``version``, if in view."""
    candidates = [
        other
        for other in versions
        if other["family"] == version["family"] and other["id"] < version["id"]
    ]
    return max(candidates, key=lambda other: other["id"], default=None)


def _settings_changes(
    params: Mapping[str, Any], previous: Mapping[str, Any] | None
) -> tuple[SettingChange, ...]:
    """Diff a version's params against its predecessor's, field by field.

    Values are compared and rendered as strings: a strategy parameter is
    never money, and the timeline only shows the move, so this never does
    arithmetic. With no in-view predecessor the diff is honestly empty rather
    than claiming every field is new against unknown family defaults.
    """
    if previous is None:
        return ()
    changes: list[SettingChange] = []
    for name in sorted(set(params) | set(previous)):
        before = str(previous[name]) if name in previous else None
        after = str(params[name]) if name in params else None
        if before != after:
            changes.append(SettingChange(field=name, before=before, after=after))
    return tuple(changes[:MAX_CHANGES])


def _promotion_event(version: dict[str, Any], versions: list[dict[str, Any]]) -> TimelineEvent:
    version_id = version["id"]
    source_sweep_id = version["source_sweep_id"]
    headline = f"settings v{version_id} activated for {version['family']}"
    if source_sweep_id is not None:
        headline += f" (from sweep #{source_sweep_id})"
    previous = _previous_version(version, versions)
    changes = _settings_changes(
        version.get("params") or {},
        previous["params"] if previous is not None else None,
    )
    return TimelineEvent(
        at=version["activated_at"],
        kind="promotion",
        strategy=version["family"],
        version_id=version_id,
        sweep_id=source_sweep_id,
        headline=headline,
        detail=version.get("note"),
        changes=changes,
    )
