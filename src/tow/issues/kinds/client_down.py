"""The main torrent client did not answer the last check: new versions are not added."""

from __future__ import annotations

from tow.clients.factory import default_client_id
from tow.issues.model import Fix, Issue, Message, Snapshot, Target
from tow.status import error_tone

KIND = "client_down"
ORDER = 40


def collect(snapshot: Snapshot) -> list[Issue]:
    if snapshot.health.get("qbit_ok") is not False:
        return []
    client = default_client_id(dict(snapshot.config))
    return [
        Issue(
            kind=KIND,
            id=KIND,
            severity=error_tone("qbit"),
            message=Message("issues.client_down"),
            subjects=(client,),
            target=Target("settings", open="clients", anchor=f"client-{client}"),
            fix=Fix("check_client", client, label="settings.common.check"),
        )
    ]
