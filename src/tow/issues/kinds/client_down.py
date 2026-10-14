"""The main torrent client did not answer the last check: new versions are not added."""

from __future__ import annotations

from tow.clients import factory
from tow.issues.model import Fix, Issue, Message, Snapshot, Target
from tow.log import error_class
from tow.status import error_tone

KIND = "client_down"
ORDER = 40


def collect(snapshot: Snapshot) -> list[Issue]:
    if snapshot.health.get("qbit_ok") is not False:
        return []
    client = factory.default_client_id(dict(snapshot.config))
    affected = tuple(
        str(topic["id"])
        for topic in snapshot.topics
        if topic.get("id")
        and topic.get("last_error")
        and not topic.get("paused")
        and str(topic.get("client_id") or client) == client
        and (topic.get("last_error_class") or error_class(str(topic["last_error"]), topic.get("last_error_code")))
        == "qbit"
    )
    return [
        Issue(
            kind=KIND,
            id=KIND,
            severity=error_tone("qbit"),
            message=Message("issues.client_down"),
            subjects=(client, *affected),
            target=Target("settings", open="clients", anchor=f"client-{client}"),
            fix=Fix("check_client", client, label="settings.common.check"),
        )
    ]
