"""Adopt into TOW: the owner's explicit action for a topic whose torrent is already in its
torrent client without TOW's mark - added by hand or by another program.

TOW changes only torrents marked as its own (AGENTS.md), and a check shows such a topic red
("check.not_owned_existing"). Adopting puts TOW's mark on that torrent - the qBittorrent tag,
the Transmission or Deluge label "tow" - and nothing else: its files, folder, file selection and
state stay as they are. The mark is read back, the topic records the torrent as its revision
and the History has it; from the next check on TOW manages the torrent like one it added.

Never done by a check on its own: only the row's button or ``tow adopt``.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from tow.clients import factory as client_factory
from tow.config import load_config
from tow.errors import TowError
from tow.log import error_fields, log_event
from tow.records import shown_title, topics_of
from tow.store import check_run_lock, load_secrets, load_state, persistence_lock, save_state

# What a check reports for a torrent in the client without TOW's mark (tow.check).
UNMARKED_CODES = frozenset({"check.not_owned_existing", "check.not_owned_partial"})
_HASH = re.compile(r"[0-9A-Fa-f]{40}|[0-9A-Fa-f]{64}")


class AdoptError(TowError, RuntimeError):
    """The torrent was not adopted (``adopt.*``); nothing was changed in the client."""

    default_class = "qbit"


def _client_of(topic: Mapping[str, Any], cfg: Mapping[str, Any] | None = None) -> str:
    return str(topic.get("client_id") or client_factory.default_client_id(dict(cfg or load_config())))


def unmarked_hash(topic: Mapping[str, Any], cfg: Mapping[str, Any] | None = None) -> str:
    """The hash a check found in the client without TOW's mark ("" when none was found, or the
    topic has another link or client now: that torrent is not this topic's)."""
    if topic.get("last_error_code") not in UNMARKED_CODES:
        return ""
    params = topic.get("last_error_params") or {}
    value = params.get("hash")
    if not isinstance(value, str) or not _HASH.fullmatch(value):
        return ""
    if params.get("url") != str(topic.get("url") or "") or params.get("client") != _client_of(topic, cfg):
        return ""
    return value.upper()


def candidate_hash(topic: Mapping[str, Any]) -> str:
    """The torrent adopting this topic would mark: the one a check found unmarked, else the
    topic's own revision (a torrent accepted unmarked by TOW 1.24 or older)."""
    own = str(topic.get("hash") or "")
    return unmarked_hash(topic) or (own.upper() if _HASH.fullmatch(own) else "")


def _owned(tags: Any) -> bool:
    return any(str(tag).strip().casefold() == "tow" for tag in tags or [])


def unmarked_topics(*, ids: list[str] | None = None) -> dict[str, Any]:
    """Topics whose torrent is in their client without TOW's mark, by asking the clients (read
    only). ``ids``: only these topics. Returns {found: [{id, title, hash, client_id}], unknown:
    [asked ids TOW does not have], unreachable: {client_id: error text}}: a client that does
    not answer is said, never taken for "nothing to adopt"."""
    cfg, secrets = load_config(), load_secrets()
    clients: dict[str, Any] = {}
    found: list[dict[str, Any]] = []
    unreachable: dict[str, str] = {}
    topics = topics_of(load_state(quarantine=False))
    known = {str(topic.get("id")) for topic in topics}
    for topic in topics:
        if ids is not None and str(topic.get("id")) not in ids:
            continue
        h = candidate_hash(topic)
        if not h:
            continue
        client_id = _client_of(topic, cfg)
        if client_id in unreachable:
            continue
        try:
            if client_id not in clients:
                clients[client_id] = client_factory.from_secrets(cfg, secrets, client_id)
            info = clients[client_id].inspect_torrent(h)
        except Exception as exc:  # noqa: BLE001 - a client that does not answer is reported, by its error
            unreachable[client_id] = str(exc) or type(exc).__name__
            continue
        if info is not None and not _owned(info.get("tags")):
            title = shown_title(topic)
            found.append({"id": str(topic.get("id")), "title": title, "hash": h, "client_id": client_id})
    unknown = [tid for tid in dict.fromkeys(ids or []) if tid not in known]
    return {"found": found, "unknown": unknown, "unreachable": unreachable}


def adopt_topic(topic_id: str, *, how: str, replace_label: bool = False) -> dict[str, Any]:
    """Mark the topic's torrent as TOW's in its client (read back), record it on the topic and
    in the History. Refused while a check runs (``CheckBusyError``): one changes the client at a
    time. ``replace_label``: a client with one label per torrent (Deluge) may replace the
    owner's label with TOW's (only when the owner says so). Returns {id, hash, client_id, already}."""
    with check_run_lock(wait=False):
        try:
            return _adopt(topic_id, how=how, replace_label=replace_label)
        except Exception as exc:
            log_event("client_adopt_failed", topic=topic_id, **error_fields(exc), how=how)
            raise


def _adopt(topic_id: str, *, how: str, replace_label: bool = False) -> dict[str, Any]:
    cfg = load_config()
    topic = next((t for t in topics_of(load_state(quarantine=False)) if str(t.get("id")) == topic_id), None)
    if topic is None:
        raise AdoptError("adopt.not_found", cls="error")
    h = candidate_hash(topic)
    if not h:
        raise AdoptError("adopt.no_torrent")
    client_id = _client_of(topic, cfg)
    client = client_factory.from_secrets(cfg, load_secrets(), client_id)
    adopt = getattr(client, "adopt_torrent", None)
    if not callable(adopt):
        raise AdoptError("adopt.unsupported")
    info = client.inspect_torrent(h)
    if info is None:
        raise AdoptError("adopt.missing")
    already = _owned(info.get("tags"))
    if not already:
        if replace_label:
            adopt(h, replace_label=True)
        else:
            adopt(h)
        after = client.inspect_torrent(h)
        if after is None or not _owned(after.get("tags")):
            raise AdoptError("adopt.unconfirmed")
    _record(topic_id, h, url=str(topic.get("url") or ""), client_id=client_id)
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


def _record(topic_id: str, h: str, *, url: str, client_id: str) -> None:
    """The adopted torrent is the topic's revision; its file selection is not TOW-verified (a
    check applies a partial selection to it, "all files" leaves it as it is)."""
    with persistence_lock():
        state = load_state()
        topic = next((t for t in topics_of(state) if str(t.get("id")) == topic_id), None)
        if topic is None:
            return  # deleted meanwhile: the mark stays in the client, the History says so
        if str(topic.get("url") or "") != url or _client_of(topic) != client_id:
            raise AdoptError("adopt.topic_changed")  # edited meanwhile: not this topic's torrent now
        old = str(topic.get("hash") or "").upper()
        if old == h:
            return
        if old:
            previous = [str(value) for value in topic.get("previous_hashes") or [] if str(value) != old]
            topic["previous_hashes"] = [old, *previous][:20]
        topic["hash"] = h
        topic["selection_verified"] = False
        save_state(state)
