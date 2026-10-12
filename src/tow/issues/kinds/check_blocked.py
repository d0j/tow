"""The last scheduled check did not complete: blocked by passwords and tokens that cannot be
opened, or failed."""

from __future__ import annotations

from tow.issues.model import Fix, Issue, Message, Snapshot, Target
from tow.status import error_tone

KIND = "check_blocked"
ORDER = 30
SECRETS = "secrets_migration_required"


def collect(snapshot: Snapshot) -> list[Issue]:
    health = snapshot.health
    error = str(health.get("check_error") or "")
    if error == SECRETS:
        # The master key: Settings says why, and the command that tells more is one copy away.
        return [
            Issue(
                kind=KIND,
                id=KIND,
                severity=error_tone(error),
                message=Message("issues.check_blocked_secrets"),
                target=Target("settings", anchor="secret-store"),
                fix=Fix("copy_keys_status", label="settings.page.copy_command"),
            )
        ]
    if health.get("check_ok") is not False:
        return []
    return [
        Issue(
            kind=KIND,
            id=KIND,
            severity=error_tone(error or "error"),
            message=Message("issues.check_failed"),
            # The History's errors say why it failed; the header's button checks again.
            target=Target("history", filter=(("group", "errors"),), anchor="latest-check_blocked"),
            fix=Fix("check_all", label="base.check_all.title"),
        )
    ]
