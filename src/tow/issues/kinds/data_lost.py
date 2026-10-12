"""The data folder was deleted or replaced while TOW ran, and no topic is watched again yet."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from tow.issues.model import Fix, Issue, Message, Snapshot, Target
from tow.status import error_tone

KIND = "data_lost"
ORDER = 10


def folder_lost(note: Mapping[str, Any] | None, topics: Sequence[Any]) -> str | None:
    """The folder named by the watchdog's note of a lost data folder (``tow.supervisor.data_lost``),
    while no topic is watched again: a night copy restored or a new start ends it."""
    if topics or not isinstance(note, Mapping):
        return None
    folder = note.get("folder")
    return folder if isinstance(folder, str) else None


def collect(snapshot: Snapshot) -> list[Issue]:
    folder = folder_lost(snapshot.data_lost, snapshot.topics)
    if folder is None:
        return []
    return [
        Issue(
            kind=KIND,
            id=KIND,
            severity=error_tone("error"),
            message=Message("issues.data_lost", {"folder": folder}),
            # The copies live outside the data folder: the newest night copy puts it back.
            target=Target("settings", open="transfer", anchor="backup-night"),
            fix=Fix("restore_night", label="settings.common.restore"),
        )
    ]
