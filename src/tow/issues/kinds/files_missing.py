"""Selected files absent on an available, identified disk, independently of site health."""

from __future__ import annotations

from tow import file_presence, file_repair
from tow.issues.model import Fix, Issue, Message, Snapshot, Target
from tow.issues.recovery import client_retrying
from tow.records import shown_title

KIND = "files_missing"
ORDER = 46


def collect(snapshot: Snapshot) -> list[Issue]:
    recovering = client_retrying(snapshot)
    found = []
    for topic in snapshot.topics:
        identity = str(topic.get("id") or "")
        missing = file_presence.count(topic, snapshot.config)
        if (
            not identity
            or not missing
            or topic.get("paused")
            or topic.get("volume_unseen")
            or topic.get("move_pending")
        ):
            continue
        found.append(
            Issue(
                kind=KIND,
                id=f"{KIND}:{identity}",
                severity="warn",
                message=Message("issues.files_missing", {"title": shown_title(topic), "count": missing}),
                subjects=(identity,),
                target=Target("home", open=identity, anchor=f"row-{identity}"),
                fix=Fix("check_topic", identity, "site_access.retry_check")
                if file_repair.continuation_pending(topic, now=snapshot.now)
                and file_repair.current(topic, file_repair.receipt_of(topic), snapshot.config)
                else Fix("repair_files", identity, "repair.action"),
                requires_attention=identity not in recovering,
            )
        )
    return found
