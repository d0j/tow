"""A watched topic's download history started over: the history had grown past what a restore
point holds even without anything else to drop (said for a week, tow.download_history.fit)."""

from __future__ import annotations

from tow.issues.model import Issue, Message, Snapshot, Target

KIND = "history_trimmed"
ORDER = 51


def collect(snapshot: Snapshot) -> list[Issue]:
    if not isinstance(snapshot.health.get("history_trimmed_at"), int):  # kept a week by the check
        return []
    return [
        Issue(kind=KIND, id=KIND, severity="warn", message=Message("issues.history_trimmed"), target=Target("history"))
    ]
