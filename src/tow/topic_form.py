"""A watched topic as the owner's form asks for it: the form's fields, the file-selection rule
(also an exact file list of a prepared torrent), a new topic's record and an edit's changes to
a copy of one.

Nothing here reads or writes a store, a client or a site: the web layer
(``tow.web.topic_actions``) loads and saves through ``tow.web.services`` and hands in what it
read (the configuration's flags, the prepared torrent).
"""

from __future__ import annotations

import json
import uuid
from dataclasses import asdict, dataclass
from typing import Any

from tow.content import selection as content_selection
from tow.errors import TowError
from tow.folders import paths_equal, save_path_policy_problem, save_path_problem
from tow.selection import normalize_policy, stored_policy
from tow.topic_timers import set_interval
from tow.torrent import TorrentMetadata


@dataclass(frozen=True)
class TopicForm:
    """The add and edit forms of a topic, as typed (the link already trimmed for an add).

    ``check_interval_min`` is None when an edit form had no such field: the topic keeps its
    timer (an empty field gives it back the global one)."""

    url: str = ""
    title: str = ""
    save_path: str = ""
    client_id: str = ""
    selection_mode: str = "all"
    selection_value: str = ""
    tracking_mode: str = "watch"
    check_interval_min: str | None = ""
    content_token: str = ""
    selection_indices: str = ""

    def draft(self) -> dict[str, str]:
        """The fields a refused add brings back into the form."""
        return {name: "" if value is None else str(value) for name, value in asdict(self).items()}


def selection_policy(
    mode: str,
    value: str,
    tracking: str,
    prepared: TorrentMetadata | None,
    indices: str,
    previous: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """The topic's selection rule from the form; an exact list needs the prepared torrent (an
    edit that sends none keeps the exact list it had)."""
    if mode != "exact":
        return normalize_policy(mode, value, tracking)
    if prepared is None and not indices and previous and previous.get("mode") == "exact":
        return normalize_policy(
            mode, tracking_mode=tracking, files=previous.get("files"), source_hash=previous.get("source_hash")
        )
    if prepared is None:
        raise TowError("content.expired")
    if len(indices) > 200_000:
        raise TowError("selection.exact_invalid")
    try:
        parsed = json.loads(indices)
    except (ValueError, RecursionError) as exc:
        raise TowError("selection.exact_invalid") from exc
    return content_selection(prepared, parsed, tracking)


def save_path_refusal(dest: str, *, allow_unc: bool, current: str = "") -> str | None:
    """Why the folder is refused (None: it is fine). Every owner device may choose a new valid
    folder; protected folders remain refused, except the one the topic already has."""
    if problem := save_path_problem(dest, allow_unc=allow_unc):
        return problem
    if current and paths_equal(dest, current):
        return None
    return save_path_policy_problem(dest)


def new_topic(
    form: TopicForm,
    *,
    title: str,
    url: str,
    save_path: str,
    client_id: str,
    policy: dict[str, Any],
    prepared: TorrentMetadata | None,
    interval: int | None,
) -> dict[str, Any]:
    """A new topic's record: no revision yet; a prepared torrent binds its first one."""
    topic: dict[str, Any] = {
        "id": uuid.uuid4().hex[:12],
        "title": title,
        "url": url,
        "save_path": save_path,
        "hash": None,
        "client_id": client_id,
        "selection": stored_policy(policy),
        "tracking_mode": policy["tracking_mode"],
    }
    if prepared is not None:
        topic["content_token"] = form.content_token
        topic["content_hash"] = prepared.infohash
    set_interval(topic, interval)
    return topic


def bind_content(
    topic: dict[str, Any],
    candidate: dict[str, Any],
    client_id: str,
    token: str,
    prepared: TorrentMetadata | None,
    mode: str,
) -> None:
    """An edit's prepared torrent on ``candidate`` (the edited copy of ``topic``): only a topic
    without a revision takes one, and a changed link or client drops the old one."""
    changed = candidate["url"] != topic.get("url") or client_id != topic.get("client_id", client_id)
    if mode == "exact" and prepared is None and changed:
        raise TowError("content.changed")
    if topic.get("hash"):
        return
    if prepared is not None:
        candidate["content_token"] = token
        candidate["content_hash"] = prepared.infohash
    elif changed:
        candidate.pop("content_token", None)
        candidate.pop("content_hash", None)


def apply_selection(topic: dict[str, Any], candidate: dict[str, Any], client_id: str, policy: dict[str, Any]) -> bool:
    """The edit's client and selection rule on ``candidate``; True when the selection changed. A
    changed selection of a topic with a revision is applied by the next check; a changed rule
    or tracking mode starts a one-time download again."""
    old_selection = previous_selection(topic)
    new_selection = stored_policy(policy)
    selection_changed = old_selection != new_selection
    tracking_changed = str(topic.get("tracking_mode") or "watch") != policy["tracking_mode"]
    candidate["client_id"] = client_id
    candidate["selection"] = new_selection
    candidate["tracking_mode"] = policy["tracking_mode"]
    if selection_changed and topic.get("hash"):
        candidate["selection_dirty"] = True
    if selection_changed or tracking_changed:
        candidate["once_done"] = False
    return selection_changed


def previous_selection(topic: dict[str, Any]) -> dict[str, Any]:
    """The topic's stored selection rule (everything, for a topic saved without one)."""
    selection = topic.get("selection")
    return selection if isinstance(selection, dict) else {"mode": "all", "value": ""}


def pending_move(outcome: str, old_dest: str, dest: str, since: str) -> dict[str, Any] | None:
    """What a topic keeps while its client has not shown the new folder yet (a long move, or a
    client that reports it late): reconcile accepts the old folder until the client agrees,
    instead of failing every check with "path differs". None once the move is done."""
    if outcome not in ("moving", "failed"):
        return None
    return {"from": old_dest, "to": dest, "since": since, **({"unconfirmed": True} if outcome == "failed" else {})}
