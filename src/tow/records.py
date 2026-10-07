"""The records TOW keeps in state.json and download_history.json, as types.

They are plain JSON dicts on disk and in memory (``TypedDict``, so nothing changes at run
time): the types only say which fields exist and what they hold, for the reader and for mypy.
Every field is optional (``total=False``): records written by older TOW versions lack the
newer ones, and the code reads them with ``.get``.

A new field is added here first; a field read under another name is then a type error.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypedDict, cast

# --- state.json -------------------------------------------------------------------------------


class Selection(TypedDict, total=False):
    """Which files of a torrent the owner wants (``tow.selection``)."""

    mode: str  # all | episodes | files ...
    value: str
    files: list[dict[str, Any]]
    source_hash: str


# A client relocation the owner started; the check ends it when the client agrees.
# ("from" is a keyword: the functional form.)
MovePending = TypedDict("MovePending", {"from": str, "to": str, "since": str}, total=False)


class Topic(TypedDict, total=False):
    """One watched topic (``state.json`` → ``topics[]``)."""

    # what the owner set
    id: str
    title: str
    url: str
    save_path: str
    client_id: str
    selection: Selection
    content_token: str
    content_hash: str
    tracking_mode: str  # watch | once
    check_interval_min: int | None
    check_timer_revision: str
    check_timer_set_at_ts: float
    paused: bool
    move_pending: MovePending
    # what the check found
    hash: str
    tracker_title: str
    previous_hashes: list[str]
    once_done: bool
    selection_dirty: bool
    selection_hash: str
    selection_verified: bool
    selected_file_count: int
    torrent_file_count: int
    selected_files: list[str]
    selected_files_truncated: bool
    selected_episode_keys: list[str]
    file_aliases: dict[str, Any]  # original path/size identities whose portable spelling differs, bound to hash
    # the latest result (the Home status colour, AGENTS.md)
    last_ok: bool
    last_ok_at: str
    last_check: str
    last_changed: bool
    last_error: str | None
    last_error_code: str
    last_error_params: dict[str, Any]
    last_error_class: str
    error_notified: bool
    error_streak: int  # checks in a row with a site's transport trouble (tow.check notifications)


class Health(TypedDict, total=False):
    """The last check run (``state.json`` → ``health``): the header and the watchdog read it."""

    qbit: str
    client: str
    qbit_ok: bool
    clients_ok: dict[str, bool]
    check_ok: bool
    check_error: str
    check_failures: int
    at: str
    at_ts: int
    auto_at_ts: int | None
    auto_ok_at_ts: int | None
    history_rebuilt_at: int


class MirrorState(TypedDict, total=False):
    """One site's hosts (``state.json`` → ``mirrors[<site>]``, ``tow.mirrors``)."""

    active: str | None
    fail: dict[str, int]  # host -> failures in a row
    cool: dict[str, float]  # host -> until when it rests (epoch seconds)
    frozen: bool


class NotifyStatus(TypedDict, total=False):
    """A messenger recipient's last delivery (``state.json`` → ``notify_status[<target>]``)."""

    ok: bool
    at: int
    error: str
    error_code: str
    error_params: dict[str, Any]


# --- download_history.json ------------------------------------------------------------------


class HistoryItem(TypedDict, total=False):
    """One file (or episode) of a topic as the progress reconcile saw it."""

    identity: str
    kind: str  # episode | file
    label: str
    episode_key: str | None
    episode_keys: list[str]
    relative_path: str
    client_id: str | None
    client_kind: str | None
    source_hash: str
    size: int | None
    first_seen_at: str
    status: str  # seen | downloading | completed | missing | unverified
    new_after_baseline: bool
    superseded: bool
    progress: float
    completion_seen: bool
    completed_observed_at: str
    completion_carried_from: str | None
    confirmed_ts: int
    verification_unavailable: bool
    torrent_completed_at: Any


class HistoryRecord(TypedDict, total=False):
    """Everything the history keeps about one topic (``topics[<topic id>]``)."""

    items: dict[str, HistoryItem]
    baseline_at: str
    last_scan_at: str
    expected: dict[str, Any]
    summary: dict[str, Any]
    last_completed: HistoryItem
    last_event: dict[str, Any]
    client_present: bool
    season_complete_at: str


class DownloadHistory(TypedDict, total=False):
    """download_history.json."""

    schema_version: int
    topics: dict[str, HistoryRecord]


class ErrorFields(TypedDict, total=False):
    """What a log event keeps about an error (`tow.log.error_fields`)."""

    error: str  # the text in the language of the moment (older readers)
    cls: str  # its status class
    error_code: str  # a typed error's catalog key and values, rendered in the reader's language
    error_params: dict[str, Any]


# A check's result for one topic (``run_check`` → ``results[]``): a free-form dict, its
# fields grow with every kind of outcome.
CheckRow = dict[str, Any]


# --- typed views of a loaded store (no copies: the same dicts) ------------------------------


def topics_of(state: Mapping[str, Any]) -> list[Topic]:
    """The state's topics (an empty list when there are none or the field is broken)."""
    topics = state.get("topics")
    return [cast(Topic, topic) for topic in topics if isinstance(topic, dict)] if isinstance(topics, list) else []


def health_of(state: Mapping[str, Any]) -> Health:
    """The state's ``health`` record ({} when there is none yet)."""
    health = state.get("health")
    return cast(Health, health) if isinstance(health, dict) else Health()


def mirror_of(state: Mapping[str, Any], site: str) -> MirrorState | None:
    """One site's mirror record, None when the site has none."""
    mirrors = state.get("mirrors")
    mirror = mirrors.get(site) if isinstance(mirrors, dict) else None
    return cast(MirrorState, mirror) if isinstance(mirror, dict) else None
