"""The download history file was damaged and started over (said for a week after it happened)."""

from __future__ import annotations

from tow.issues.model import Issue, Message, Snapshot, Target

KIND = "history_rebuilt"
ORDER = 50


def collect(snapshot: Snapshot) -> list[Issue]:
    if not isinstance(snapshot.health.get("history_rebuilt_at"), int):  # kept a week by the check
        return []
    # Nothing to do: the History says what happened, in a note of its own.
    return [
        Issue(kind=KIND, id=KIND, severity="warn", message=Message("issues.history_rebuilt"), target=Target("history"))
    ]
