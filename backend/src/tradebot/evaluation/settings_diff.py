"""Field-level diffs between two strategy-parameter sets — the "what changed".

Shared by the research timeline (§12.8, a settings version vs the family's
previous version) and research campaigns (§12.7, the active config vs a
promoted winner): both answer "what did this promotion change", so they
compute it the same way and render the same shape. Values are compared and
carried as display strings — a strategy parameter is never money, so this
only shows the move, never arithmetic on it.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from pydantic import BaseModel, ConfigDict

MAX_CHANGES = 8
"""Cap the change list. A strategy family carries a handful of parameters;
past a handful the note and the version link carry the rest."""


class SettingChange(BaseModel):
    """One parameter a promotion changed, as display strings.

    ``before`` is ``None`` when the field is new in this version (no in-view
    predecessor set it); ``after`` is ``None`` when the field was dropped.
    """

    model_config = ConfigDict(frozen=True)

    field: str
    before: str | None
    after: str | None


def settings_changes(
    params: Mapping[str, Any], previous: Mapping[str, Any] | None
) -> tuple[SettingChange, ...]:
    """Diff ``params`` against ``previous`` field by field, newest values winning.

    Returns only the fields that moved, sorted by name, each before -> after
    as display strings (``None`` at either end means the field was added or
    dropped). With no predecessor at all the diff is honestly empty rather
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
