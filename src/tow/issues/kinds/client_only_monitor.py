"""An unavailable one-time handoff journal needs the owner, not a replay."""

from __future__ import annotations

from collections.abc import Mapping

from tow.issues.model import Issue, Message, Snapshot, Target

KIND = "client_only_monitor"
ORDER = 23


def collect(snapshot: Snapshot) -> list[Issue]:
    marker = snapshot.health.get(KIND)
    if not isinstance(marker, Mapping) or marker.get("ok") is not False:
        return []
    return [
        Issue(
            kind=KIND,
            id=KIND,
            severity="bad",
            message=Message("issues.client_only_monitor"),
            target=Target("history", filter=(("group", "errors"),)),
        )
    ]
