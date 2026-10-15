"""A torrent client does not answer (the last check, or Settings' "Check" since then): new
versions are not added. One issue per client, the main one first; its topics that failed for it
are counted in it."""

from __future__ import annotations

from tow.clients import factory
from tow.issues import clients
from tow.issues.model import Fix, Issue, Message, Snapshot, Target
from tow.log import error_class
from tow.status import error_tone

KIND = "client_down"
ORDER = 40


def collect(snapshot: Snapshot) -> list[Issue]:
    found = clients.down(snapshot)
    if not found:
        return []
    cfg = dict(snapshot.config)
    default = factory.default_client_id(cfg)
    rows = {str(row["id"]): row for row in factory.client_configurations(cfg) if row.get("enabled", True)}
    issues = []
    for client in found:
        affected = tuple(
            str(topic["id"])
            for topic in snapshot.topics
            if topic.get("id")
            and topic.get("last_error")
            and not topic.get("paused")
            and str(topic.get("client_id") or default) == client
            and (topic.get("last_error_class") or error_class(str(topic["last_error"]), topic.get("last_error_code")))
            == "qbit"
        )
        # With several clients each entry names its own ("Transmission (tr) does not answer").
        message = (
            Message("issues.client_down_named", {"client": factory.client_name(rows.get(client) or {"id": client})})
            if len(rows) > 1
            else Message("issues.client_down")
        )
        issues.append(
            Issue(
                kind=KIND,
                # The main client keeps the id it always had; another one is named by its own.
                id=KIND if client == default else f"{KIND}:{client}",
                severity=error_tone("qbit"),
                message=message,
                subjects=(client, *affected),
                target=Target("settings", open="clients", anchor=f"client-{client}"),
                fix=Fix("check_client", client, label="settings.common.check"),
            )
        )
    return issues
