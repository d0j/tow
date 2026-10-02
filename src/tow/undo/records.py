"""What an undo record holds, kind by kind.

The record lives in state.json under ``"undo"``; older TOW versions wrote the same shapes, so
the field names never change. Secrets are never in the record: a kind that changes secrets
keeps a reference (``secrets_undo_ref``, ``secret_undo_ref`` for a site) to the one encrypted
snapshot file, secrets-undo.enc.
"""

from __future__ import annotations

from typing import Any, Literal, NotRequired, TypedDict

# The fields that point at the encrypted snapshot (a site change used the singular).
SECRET_REFS = ("secrets_undo_ref", "secret_undo_ref")


class TopicDeleted(TypedDict):
    """kind "topic": the deleted topic and where it stood in the list."""

    kind: Literal["topic"]
    ts: str
    item: dict[str, Any]
    index: NotRequired[int]


class TopicEdited(TypedDict):
    """kind "topic_put": the topic as it was before the edit form saved it."""

    kind: Literal["topic_put"]
    ts: str
    item: dict[str, Any]


class TopicAdded(TypedDict):
    """kind "topic_add": the id of the topic that was added."""

    kind: Literal["topic_add"]
    ts: str
    id: str


class SiteChanged(TypedDict):
    """kind "site": a site's settings and mirror state before a save, rename or delete."""

    kind: Literal["site"]
    ts: str
    name: str
    spec: dict[str, Any]
    mirror: NotRequired[Any]
    mirror_present: NotRequired[bool]
    renamed_to: NotRequired[str]
    secret_undo_ref: NotRequired[str]  # snapshot {"name", "present", "value"} of the site's login


class SettingsChanged(TypedDict):
    """kind "settings": a client login, a messenger, the check interval or the message time."""

    kind: Literal["settings"]
    ts: str
    secrets_undo_ref: str  # snapshot: the whole secrets store before the change
    secret_scope: NotRequired[list[str] | None]  # the part of it the change touched (None: all, old)
    interval_sec: NotRequired[int]
    flash_ttl_sec: NotRequired[int]


class AccessChanged(TypedDict):
    """kind "settings_access": network access, or the password (then with its snapshot).

    Records written before 1.19 also carry ``old_lan_auth`` (a config flag that no longer
    switches anything): it is ignored.
    """

    kind: Literal["settings_access"]
    ts: str
    old_bind: str
    old_allow_lan: bool
    secrets_undo_ref: NotRequired[str]
    secret_scope: NotRequired[list[str]]


class ClientsChanged(TypedDict):
    """kind "settings_clients": the list of torrent clients and their logins."""

    kind: Literal["settings_clients"]
    ts: str
    config_before: dict[str, Any]  # "client" and "clients" of config.yaml (None: absent)
    secrets_undo_ref: str
    secret_scope: NotRequired[list[str]]


UndoRecord = TopicDeleted | TopicEdited | TopicAdded | SiteChanged | SettingsChanged | AccessChanged | ClientsChanged


def secret_refs(record: object) -> list[str]:
    """The snapshot references a record holds (none for a record that is not a mapping)."""
    if not isinstance(record, dict):
        return []
    return [str(record[key]) for key in SECRET_REFS if record.get(key)]
