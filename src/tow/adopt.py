"""Adopt into TOW: the owner's explicit action for a topic whose torrent is already in its
torrent client without TOW's mark - added by hand, or by Monitorrent before the move to TOW.

TOW changes only torrents marked as its own (AGENTS.md), and a check shows such a topic red
("check.not_owned_existing"). Adopting puts TOW's mark on that torrent - the qBittorrent tag,
the Transmission or Deluge label "tow" - and nothing else: its files, folder, file selection and
state stay as they are. The mark is read back, the topic records the torrent as its revision
and the History has it; from the next check on TOW manages the torrent like one it added.

Never done by a check on its own: only the row's button, ``tow adopt`` or the Monitorrent
import's explicit ``--adopt``.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from tow.clients import factory as client_factory
from tow.config import load_config
from tow.errors import TowError
from tow.log import error_fields, log_event
from tow.records import topics_of
from tow.store import check_run_lock, load_secrets, load_state, persistence_lock, save_state

# What a check reports for a torrent in the client without TOW's mark (tow.check).
UNMARKED_CODES = frozenset({"check.not_owned_existing", "check.not_owned_partial"})
_HASH = re.compile(r"[0-9A-Fa-f]{40}|[0-9A-Fa-f]{64}")


class AdoptError(TowError, RuntimeError):
    """The torrent was not adopted (``adopt.*``); nothing was changed in the client."""

    default_class = "qbit"


def unmarked_hash(topic: Mapping[str, Any]) -> str:
    """The hash a check found in the client without TOW's mark ("" when none was found)."""
    if topic.get("last_error_code") not in UNMARKED_CODES:
        return ""
    value = (topic.get("last_error_params") or {}).get("hash")
    return value.upper() if isinstance(value, str) and _HASH.fullmatch(value) else ""


def candidate_hash(topic: Mapping[str, Any]) -> str:
    """The torrent adopting this topic would mark: the one a check found unmarked, else the
    topic's own revision (a torrent accepted unmarked by TOW 1.24 or older)."""
    own = str(topic.get("hash") or "")
    return unmarked_hash(topic) or (own.upper() if _HASH.fullmatch(own) else "")


def _owned(tags: Any) -> bool:
    return any(str(tag).strip().casefold() == "tow" for tag in tags or [])


def unmarked_topics(*, ids: list[str] | None = None) -> list[dict[str, Any]]:
    """Topics whose torrent is in their client without TOW's mark ({id, title, hash, client_id}),
    by asking the clients (read only). ``ids``: only these topics."""
    cfg, secrets = load_config(), load_secrets()
    clients: dict[str, Any] = {}
    found: list[dict[str, Any]] = []
    for topic in topics_of(load_state(quarantine=False)):
        if ids is not None and str(topic.get("id")) not in ids:
            continue
        h = candidate_hash(topic)
        if not h:
            continue
        client_id = str(topic.get("client_id") or client_factory.default_client_id(cfg))
        try:
            if client_id not in clients:
                clients[client_id] = client_factory.from_secrets(cfg, secrets, client_id)
            info = clients[client_id].inspect_torrent(h)
        except Exception:  # noqa: BLE001 - a client that does not answer has nothing to list here
            continue
        if info is not None and not _owned(info.get("tags")):
            title = str(topic.get("tracker_title") or topic.get("title") or "")
            found.append({"id": str(topic.get("id")), "title": title, "hash": h, "client_id": client_id})
    return found


def adopt_topic(topic_id: str, *, how: str) -> dict[str, Any]:
    """Mark the topic's torrent as TOW's in its client (read back), record it on the topic and
    in the History. Refused while a check runs (``CheckBusyError``): one changes the client at a
    time. Returns {id, hash, client_id, already}."""
    with check_run_lock(wait=False):
        try:
            return _adopt(topic_id, how=how)
        except Exception as exc:
            log_event("client_adopt_failed", topic=topic_id, **error_fields(exc), how=how)
            raise


def _adopt(topic_id: str, *, how: str) -> dict[str, Any]:
    cfg = load_config()
    topic = next((t for t in topics_of(load_state(quarantine=False)) if str(t.get("id")) == topic_id), None)
    if topic is None:
        raise AdoptError("adopt.not_found", cls="error")
    h = candidate_hash(topic)
    if not h:
        raise AdoptError("adopt.no_torrent")
    client_id = str(topic.get("client_id") or client_factory.default_client_id(cfg))
    client = client_factory.from_secrets(cfg, load_secrets(), client_id)
    adopt = getattr(client, "adopt_torrent", None)
    if not callable(adopt):
        raise AdoptError("adopt.unsupported")
    info = client.inspect_torrent(h)
    if info is None:
        raise AdoptError("adopt.missing")
    already = _owned(info.get("tags"))
    if not already:
        adopt(h)
        after = client.inspect_torrent(h)
        if after is None or not _owned(after.get("tags")):
            raise AdoptError("adopt.unconfirmed")
    _record(topic_id, h)
    log_event(
        "client_adopted",
        topic=topic_id,
        title=topic.get("title"),
        hash=h,
        client_id=client_id,
        client_kind=getattr(client, "client_kind", None),
        status="already" if already else "succeeded",
        how=how,
    )
    return {"id": topic_id, "hash": h, "client_id": client_id, "already": already}


def _record(topic_id: str, h: str) -> None:
    """The adopted torrent is the topic's revision; its file selection is not TOW-verified (a
    check applies a partial selection to it, "all files" leaves it as it is)."""
    with persistence_lock():
        state = load_state()
        topic = next((t for t in topics_of(state) if str(t.get("id")) == topic_id), None)
        if topic is None:
            return  # deleted meanwhile: the mark stays in the client, the History says so
        old = str(topic.get("hash") or "").upper()
        if old == h:
            return
        if old:
            previous = [str(value) for value in topic.get("previous_hashes") or [] if str(value) != old]
            topic["previous_hashes"] = [old, *previous][:20]
        topic["hash"] = h
        topic["selection_verified"] = False
        save_state(state)
