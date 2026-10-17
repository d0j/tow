"""A folder is not on the disk TOW recorded for it: the disk of a topic's save folder or of a
backup folder is not connected, or the folder is on another disk now (tow.folders.recorded_volume).
TOW calls nothing missing there and writes nothing there until the disk is back, or until the owner
says on this computer that it is the same disk (the Volumes page: accept)."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from tow.issues.model import Fix, Issue, Message, Snapshot, Target

KIND = "volume_missing"
ORDER = 45
# Backup folders, by their name in state.json's backup_volumes (tow.locations.LOCATIONS), and their words.
BACKUPS = {"night": "issues.volume_missing_night", "manual": "issues.volume_missing_manual"}


def collect(snapshot: Snapshot) -> list[Issue]:
    issues = [_topic_issue(topic) for topic in snapshot.topics if _unseen(topic.get("volume_unseen"))]
    volumes = snapshot.state.get("backup_volumes")
    for name, key in BACKUPS.items():
        entry = volumes.get(name) if isinstance(volumes, Mapping) else None
        if isinstance(entry, Mapping) and isinstance(entry.get("unseen_at"), int):
            subject = f"backup:{name}"
            issues.append(
                Issue(
                    kind=KIND,
                    id=f"{KIND}:{subject}",
                    severity="warn",
                    message=Message(key, {"folder": str(entry.get("path") or "")}),
                    subjects=(subject,),
                    target=Target("volumes", anchor=_anchor(subject)),
                    fix=Fix("accept_volume", subject, label="volumes.accept"),
                )
            )
    return [issue for issue in issues if issue is not None]


def _unseen(value: Any) -> bool:
    return isinstance(value, Mapping) and isinstance(value.get("folder"), str)


def _topic_issue(topic: Mapping[str, Any]) -> Issue | None:
    identity = str(topic.get("id") or "")
    if not identity or topic.get("paused"):
        return None
    subject = f"topic:{identity}"
    return Issue(
        kind=KIND,
        id=f"{KIND}:{subject}",
        severity="warn",
        message=Message(
            "issues.volume_missing_topic",
            {"title": str(topic.get("title") or identity), "folder": str(topic["volume_unseen"]["folder"])},
        ),
        subjects=(identity,),
        target=Target("volumes", anchor=_anchor(subject)),
        fix=Fix("accept_volume", subject, label="volumes.accept"),
    )


def _anchor(subject: str) -> str:
    """The entry's id on the Volumes page."""
    return "volume-" + subject.replace(":", "-")
