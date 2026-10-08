"""Pure steps of a check run: hash identity, revision bookkeeping, result merging.

They take and return plain data (no I/O), so run_check stays readable and they can be
tested in isolation.
"""

from __future__ import annotations

from collections.abc import Collection, Iterable, Mapping
from typing import Any

from tow import errors
from tow.clients.spec import TorrentClientAdapter
from tow.log import error_class, scrub_text
from tow.records import Topic
from tow.torrent import TorrentFile, windows_path_key


def resolve_client_hash(
    metadata: Any, old: str, topic_client: TorrentClientAdapter | None, row: dict[str, Any]
) -> tuple[str, bool]:
    """The hash the client knows this torrent by, and whether it only re-labels ``old``.

    libtorrent 1.x exposes hybrid (v1+v2) torrents by their v1 hash; keep the proven
    client identity while the check result retains both source hashes.
    """
    h = metadata.client_hash
    hash_v1 = str(getattr(metadata, "hash_v1", None) or "").upper()
    hash_v2 = str(getattr(metadata, "hash_v2", None) or "")
    if (
        topic_client is not None
        and hash_v1
        and h != hash_v1
        and not topic_client.has_hash(h)
        and topic_client.has_hash(hash_v1)
    ):
        h = hash_v1
        row["client_hash_legacy_v1"] = True
    alias_migration = bool(old and h != old and old in {hash_v1, hash_v2[:40].upper()})
    if alias_migration and topic_client is not None and not topic_client.has_hash(h) and topic_client.has_hash(old):
        h = old
        alias_migration = False
        row["client_hash_legacy_v1"] = True
    return h, alias_migration


def verify_magnet_metadata(metadata: Any, magnet_hash: str) -> None:
    expected = str(magnet_hash).upper()
    actual = getattr(metadata, "hash_v2", None) if len(expected) == 64 else getattr(metadata, "hash_v1", None)
    if (actual or getattr(metadata, "client_hash", None)) != expected:
        raise errors.TowError("check.magnet_mismatch")


def client_identities(metadata: Any) -> set[str]:
    """Every hash under which the client may legitimately report this torrent."""
    hash_v2 = getattr(metadata, "hash_v2", None)
    values = (
        getattr(metadata, "client_hash", None),
        getattr(metadata, "hash_v1", None),
        hash_v2,
        str(hash_v2)[:40] if hash_v2 else None,
    )
    return {str(value).upper() for value in values if value}


# Errors that mean "the client took the torrent, but the add was not confirmed".
UNCONFIRMED_ADD_CODES = frozenset(
    {
        "check.add_unconfirmed",
        "client.managed.not_visible",
        "client.managed.no_owner_mark",
        "client.managed.pending_not_cleared",
    }
)
# The same errors as TOW 1.17 and older stored them (text only, English or Russian): frozen.
_LEGACY_UNCONFIRMED_ADD = (
    "ownership was not confirmed after add",
    "add was not visible by read-back",
    "add не подтверждён",
    "marker removal was not confirmed",
    "добавление не подтверждено",
    "не показывает её (read-back)",
    "не сохранил метку tow",
    "метка tow-pending не снята",
    "the add was not confirmed by reading it back",
    "the client accepted the torrent but does not show it",
    "the client did not keep the tow label",
    "the tow-pending label was not removed",
    "клиент принял раздачу, но не показывает её",
)


def is_owned_add_recovery(topic: Topic) -> bool:
    """The previous run added this revision but could not confirm it."""
    code = topic.get("last_error_code")
    if code:
        record = {"code": code, "params": topic.get("last_error_params") or {}}
        return any(found in UNCONFIRMED_ADD_CODES for found in errors.codes_in(record))
    previous_error = str(topic.get("last_error") or "").casefold()
    return any(marker in previous_error for marker in _LEGACY_UNCONFIRMED_ADD)


# Topic fields a check owns; everything else on disk (edits made meanwhile) is kept.
_CHECK_OWNED_FIELDS = (
    "hash",
    "title",
    "tracker_title",
    "client_id",
    "last_ok",
    "last_error",
    "last_error_class",
    "last_check",
    "last_ok_at",
    "last_changed",
    "previous_hashes",
    "selected_file_count",
    "torrent_file_count",
    "selected_files",
    "selected_files_truncated",
    "selected_episode_keys",
    "selection_hash",
    "selection_dirty",
    "selection_verified",
    "once_done",
)

# Check-owned markers that the check may also remove (e.g. reconcile ends a client move, an
# error without a code - a foreign exception - drops the previous error's code).
_CHECK_CLEARABLE_FIELDS = (
    "file_aliases",
    "move_pending",
    "error_notified",
    "error_streak",
    "last_error_code",
    "last_error_params",
    "content_token",
    "content_hash",
    "waiting_space",
)


# Fields the owner edits in the UI (topics_edit, pause, undo; checked against what they store).
# A topic whose owner fields changed while the check ran keeps the owner's title, client,
# pending selection change and relocation; the check's facts still land.
OWNER_FIELDS = (
    "title",
    "url",
    "save_path",
    "client_id",
    "selection",
    "tracking_mode",
    "check_interval_min",
    "check_timer_revision",
    "check_timer_set_at_ts",
    "paused",
    "selection_dirty",
    "once_done",
    "move_pending",
    "content_token",
    "content_hash",
)
_OWNER_WINS = frozenset({"title", "client_id", "selection_dirty", "once_done"})


def owner_fields(topic: Topic) -> tuple[Any, ...]:
    return tuple(repr(topic.get(key)) for key in OWNER_FIELDS)


def changed_owner_fields(started: tuple[Any, ...] | None, topic: Topic) -> frozenset[str]:
    """The owner fields of ``topic`` that differ from the snapshot taken when the check started."""
    if started is None:
        return frozenset()
    now = owner_fields(topic)
    return frozenset(key for key, before, after in zip(OWNER_FIELDS, started, now, strict=True) if before != after)


def merge_check_results(
    disk: dict[str, Any],
    state: dict[str, Any],
    *,
    edited: Mapping[str, Collection[str]] | Collection[str] = frozenset(),
) -> None:
    """Copy the check-owned fields of every checked topic onto the freshly loaded state.

    ``edited``: topic id -> owner fields changed while the check ran (a plain collection of
    ids means "any of them").
    """
    changes: Mapping[str, Collection[str]] = (
        edited if isinstance(edited, Mapping) else {str(tid): OWNER_FIELDS for tid in edited}
    )
    checked = {str(topic.get("id")): topic for topic in state.get("topics") or []}
    for topic in disk.get("topics") or []:
        source = checked.get(str(topic.get("id")))
        if not source:
            continue
        changed = changes.get(str(topic.get("id"))) or ()
        owner_edited = bool(changed)
        for key in _CHECK_OWNED_FIELDS:
            if owner_edited and key in _OWNER_WINS:
                continue
            if key in source:
                topic[key] = source[key]
        for key in _CHECK_CLEARABLE_FIELDS:
            if key in changed:
                continue  # the owner started (or undid) a move meanwhile: theirs wins (M2)
            if key in source:
                topic[key] = source[key]
            else:
                topic.pop(key, None)
        if topic.get("hash") and source.get("selection") != topic.get("selection"):
            # The check applied the selection it started with; the owner's newer one has not
            # reached the client yet - the next check must apply it (M1).
            topic["selection_dirty"] = True


def store_revision(
    topic: Topic,
    *,
    h: str,
    old: str,
    keep_previous: bool,
    selection_verified: bool,
    plan: Any,
    once: bool,
    files: Iterable[TorrentFile] | None = None,
) -> None:
    """Record a revision the client has confirmed, with its verified file selection."""
    if keep_previous:
        previous_hashes = [str(value) for value in topic.get("previous_hashes") or []]
        topic["previous_hashes"] = ([old] + [value for value in previous_hashes if value != old])[:20]
    topic["hash"] = h
    topic["selection_verified"] = selection_verified
    if selection_verified:
        topic["selected_file_count"] = len(plan.selected_indices)
        topic["torrent_file_count"] = plan.total_files
        topic["selected_files"] = list(plan.selected_files[:200])
        topic["selected_files_truncated"] = len(plan.selected_files) > 200
        topic["selected_episode_keys"] = list(plan.selected_episode_keys)
        topic["selection_hash"] = h
        if files is not None:
            store_file_aliases(topic, h, files, mode=plan.mode)
    topic["selection_dirty"] = False
    topic.pop("content_token", None)
    topic.pop("content_hash", None)
    if once:
        topic["once_done"] = True


def store_file_aliases(topic: Topic, h: str, files: Iterable[TorrentFile], *, mode: str) -> None:
    """Original names of validated metadata, independent of client priorities or UI previews."""
    aliases = [
        {"path": row.path, "size": row.size}
        for row in files
        if not row.is_pad and windows_path_key(row.path) != row.path.casefold()
    ]
    if aliases or mode == "files":
        topic["file_aliases"] = {"hash": h, "files": aliases}
    else:
        topic.pop("file_aliases", None)


def mark_reconcile_failure(row: dict[str, Any], exc: Exception) -> None:
    """A failed progress reconcile fails the row, keeping an earlier tracker error visible.

    The row's class is the reconcile failure's (the client or the folder needs the owner)."""
    tracker_failed = row.get("ok") is False and bool(row.get("error"))
    cls = error_class(exc)
    cause: Any = exc if isinstance(exc, errors.TowError) else (scrub_text(str(exc)) or type(exc).__name__)
    reconcile = errors.TowError("check.reconcile_failed", cls=cls, error=cause)
    row["progress_error"] = str(reconcile)
    row["reconcile_error"] = cls
    error = reconcile
    if tracker_failed:
        row["tracker_error"] = str(row["error"])
        earlier = errors.as_value(row.get("error_record"), row["tracker_error"])
        error = errors.TowError("check.two_errors", cls=cls, first=earlier, second=reconcile)
    row.update(error=str(error), error_record=error.record(), error_class=cls, ok=False, status="failed")


_RECONCILE_NOTIFICATIONS = {
    "client_removed": "removed",
    "client_restored": "restored",
    "new_file": "new_file",
    "revision_updated": "revision",
    "episode_completed": "completed",
    "file_completed": "completed",
}


def reconcile_notification(event_kind: str, reconcile: dict[str, Any]) -> tuple[str, str] | None:
    """(notification kind, episode label) for reconcile events worth a Telegram message."""
    kind = _RECONCILE_NOTIFICATIONS.get(event_kind)
    if kind is None:
        return None
    if event_kind == "episode_completed":
        return kind, str(reconcile.get("completed_episode_label") or "")
    if event_kind == "file_completed":
        return kind, str(reconcile.get("completed_file_label") or "")
    return kind, ""
