"""Scheduled checks have stopped: the last one is older than two intervals and a slack."""

from __future__ import annotations

from tow.config import interval_sec_of
from tow.issues.model import Fix, Issue, Message, Snapshot, Target

KIND = "check_stale"
ORDER = 20

# Checks are late when the last scheduled one is older than two intervals plus this slack (the
# watchdog uses the same rule: tow.watchdog imports it from here).
STALE_SLACK_SEC = 10 * 60


def late(since: float, now: float, interval_sec: int) -> bool:
    """A scheduled check that started at ``since`` (epoch seconds) is followed by none in time."""
    return now - since > 2 * interval_sec + STALE_SLACK_SEC


def collect(snapshot: Snapshot) -> list[Issue]:
    # Against the real next scheduled check (never a manual check or a progress pass).
    if not snapshot.next_check_at:
        return []
    interval = interval_sec_of(snapshot.config)
    since = snapshot.next_check_at - interval
    if not late(since, snapshot.now, interval):
        return []
    return [
        Issue(
            kind=KIND,
            id=KIND,
            severity="warn",
            message=Message("issues.check_stale", {"at": since}),
            # tow run plans the checks: its card says they are late (the note) and how it is, and a
            # restart starts it again.
            target=Target("settings", open="service", anchor="acc-service", note=True),
            fix=Fix("restart_service", label="settings.service.restart"),
        )
    ]
