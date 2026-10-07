from __future__ import annotations

import copy
import logging
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, NamedTuple, cast

from tow import check_transaction, download_history, errors
from tow.check_steps import (
    changed_owner_fields,
    client_identities,
    is_owned_add_recovery,
    mark_reconcile_failure,
    merge_check_results,
    owner_fields,
    reconcile_notification,
    resolve_client_hash,
    store_file_aliases,
    store_revision,
    verify_magnet_metadata,
)
from tow.clients import factory as client_factory
from tow.clients.spec import TorrentClientAdapter
from tow.clock import iso_now, machine_now
from tow.config import load_config
from tow.episodes import parse_season_hint
from tow.errors import Msg, TowError
from tow.events import new_operation_id
from tow.folders import paths_equal
from tow.i18n import t
from tow.jsonish import as_dict
from tow.log import error_class, error_fields, is_daily_limit, log_event, owner_language
from tow.notify import NotificationBatch
from tow.progress import reconcile_topic
from tow.records import CheckRow, DownloadHistory, Health, HistoryRecord, Topic, health_of, mirror_of, topics_of
from tow.selection import SelectionPendingError, policy_from_topic, resolve_selection
from tow.status import TRACKER_WARNING_CLASSES
from tow.store import (
    FileStamp,
    SecretStoreError,
    StoreCorruptionError,
    StoreReadError,
    check_run_lock,
    encrypted_secrets_path,
    file_stamp,
    load_download_history,
    load_secrets,
    load_state,
    persistence_lock,
    save_download_history,
    save_state,
)
from tow.title import title_is_placeholder as _title_placeholder
from tow.torrent import TorrentPathConflictError, parse_magnet_hashes, parse_torrent_metadata, windows_path_key
from tow.trackers import GenericHttpTracker, load_trackers, match_tracker, presets

_LOG = logging.getLogger("tow.check")


def _fail_log(
    topic: Topic,
    url: str,
    error: BaseException | str,
    tr: GenericHttpTracker | None = None,
    *,
    how: str = "auto",
    persist: bool = True,
) -> None:
    if not persist:
        return
    log_event(
        "check_fail",
        topic=topic.get("id"),
        title=topic.get("title"),
        url=url,
        tracker=getattr(tr, "name", None),
        **error_fields(error),
        how=how,
    )


def _set_error(row: dict[str, Any], error: BaseException | str) -> None:
    """A failed row keeps the error's text (the language of the moment), its record (code and
    values, rendered again in the reader's language) and its class."""
    row["error"] = str(error)
    row["error_record"] = errors.record_of(error) if isinstance(error, BaseException) else None
    row["error_class"] = error_class(error)


def _row_error(row: dict[str, Any]) -> Any:
    """The row's error as a value a message can carry (the typed error's record, else its text)."""
    return errors.as_value(row.get("error_record"), str(row.get("error") or ""))


def _client_info_matches_hash(info: dict[str, Any], infohash: str) -> bool:
    wanted = str(infohash or "").strip().upper()
    if not wanted:
        return False
    primary = str(info.get("hash") or "").strip().upper()
    v1 = str(info.get("infohash_v1") or "").strip().upper()
    v2 = str(info.get("infohash_v2") or "").strip().upper()
    identities = {primary, v1, v2, v2[:40] if len(v2) == 64 else ""}
    return wanted in identities


def _magnet_matches_saved_hash(magnet_url: str, saved_hash: str, client: TorrentClientAdapter | None) -> bool:
    identities = parse_magnet_hashes(magnet_url)
    old = str(saved_hash or "").upper()
    if not identities or not old:
        return False
    btih, btmh = identities
    if old in btmh or any(old == value[:40] for value in btmh):
        return True
    if old not in btih:
        return False
    if not btmh:
        return True
    info = client.inspect_torrent(old) if client is not None else None
    if not isinstance(info, dict) or not _client_info_matches_hash(info, old):
        return False
    known_v2 = str(info.get("infohash_v2") or "").upper()
    return known_v2 in btmh


def client_owned_by_tow(client: TorrentClientAdapter, infohash: str) -> bool:
    if not client.has_hash(infohash):
        return False
    info = client.inspect_torrent(infohash)
    if not info or not _client_info_matches_hash(info, infohash):
        return False
    tags = info.get("tags")
    if not isinstance(tags, (list, tuple, set)):
        return False
    return any(str(tag).strip().casefold() == "tow" for tag in tags)


def _confirm_client_add(
    client: TorrentClientAdapter, infohash: str, save_path: str, *, require_tow_ownership: bool = False
) -> bool:
    if require_tow_ownership and not client_owned_by_tow(client, infohash):
        return False
    if not client.has_hash(infohash):
        return False
    info = client.inspect_torrent(infohash)
    if not info:
        return False
    if not _client_info_matches_hash(info, infohash):
        return False
    observed_path = str(info.get("save_path") or "").strip()
    return bool(observed_path) and paths_equal(observed_path, save_path)


RELOCATION_WAIT_SEC = 20.0


def await_relocation(
    client: TorrentClientAdapter, infohash: str, save_path: str, *, timeout: float | None = None
) -> str:
    """Wait for a client-side move after ``set_location``.

    qBittorrent reports the new ``save_path`` only once the data move finishes
    (instant on the same volume, minutes across disks). Returns ``"done"`` when
    the read-back confirms the path, ``"moving"`` when the client is still moving
    the data at the deadline, and ``"failed"`` otherwise.
    """
    deadline = time.monotonic() + max(0.0, RELOCATION_WAIT_SEC if timeout is None else timeout)
    while True:
        if _confirm_client_add(client, infohash, save_path, require_tow_ownership=True):
            return "done"
        info = client.inspect_torrent(infohash) or {}
        moving = str(info.get("state") or "").casefold() == "moving"
        if time.monotonic() >= deadline:
            return "moving" if moving else "failed"
        time.sleep(0.5)


def _active_revision_overlap(client: TorrentClientAdapter, old_hash: str, new_files: tuple[Any, ...]) -> str:
    if not old_hash or not client.has_hash(old_hash):
        return ""
    info = client.inspect_torrent(old_hash)
    if not info or not _client_info_matches_hash(info, old_hash):
        return ""
    state = str(info.get("state") or "").casefold()
    if not state or state.startswith(("stopped", "paused")):
        return ""
    wanted = [windows_path_key(str(file.path)) for file in new_files if not getattr(file, "is_pad", False)]
    for row in info.get("files") or []:
        try:
            if row.get("priority") is not None and int(row.get("priority")) == 0:
                continue
        except TypeError, ValueError:
            continue
        raw_actual = str(row.get("name") or "")
        actual = windows_path_key(raw_actual)
        # A client that could not decode a legacy (non-UTF-8) name shows it as `_`/`?`/U+FFFD;
        # such a name cannot be compared, so fail closed. Readable names compare exactly.
        actual_lost_text = "�" in raw_actual or not any(ord(ch) > 127 for ch in raw_actual)
        for path in wanted:
            if actual == path or actual.endswith("/" + path):
                return raw_actual or path
            if (
                actual_lost_text
                and any(ord(character) > 127 for character in path)
                and len(actual.split("/")) in {len(path.split("/")), len(path.split("/")) + 1}
            ):
                return raw_actual or path
    return ""


def _conflicting_topic_claim(
    state: dict[str, Any],
    topic: Topic,
    infohash: str,
    save_path: str,
    selected_files: tuple[str, ...],
    selection_mode: str,
) -> str:
    """Detect incompatible topic records that would mutate the same client task."""
    wanted_files = {value.replace("\\", "/").casefold() for value in selected_files}
    client_id = str(topic.get("client_id") or "default")
    for other in topics_of(state):
        if other is topic or str(other.get("id") or "") == str(topic.get("id") or ""):
            continue
        if str(other.get("client_id") or client_id) != client_id:
            continue
        if str(other.get("hash") or "").upper() != infohash.upper():
            continue
        other_path = str(other.get("save_path") or "")
        other_files = {str(value).replace("\\", "/").casefold() for value in other.get("selected_files") or []}
        other_selection = as_dict(other.get("selection"))
        other_mode = str(other_selection.get("mode") or "all")
        compatible_selection = selection_mode == "all" and other_mode == "all"
        if not compatible_selection:
            compatible_selection = (
                bool(other_files) and not bool(other.get("selected_files_truncated")) and other_files == wanted_files
            )
        if not paths_equal(other_path, save_path) or not compatible_selection:
            return str(other.get("id") or other.get("title") or "unknown")
    return ""


def _current_tracker_title(
    tracker: Any, url: str, secrets: dict[str, Any], ua: str | None, *, ignore_cool: bool, persist: bool
) -> str:
    fetch_title = getattr(tracker, "fetch_title", None)
    if not callable(fetch_title):
        return ""
    try:
        return str(fetch_title(url, secrets, ua, ignore_cool=ignore_cool, persist=persist) or "").strip()
    except Exception as exc:  # noqa: BLE001 - the title is a nicety: without it the saved one stays
        _LOG.warning("tracker title not read for %s: %s", getattr(tracker, "name", "?"), type(exc).__name__)
        return ""


def _now() -> str:
    """When a check ran, as stored: an ISO time with its offset. The pages write it in their own
    language when they show it (TOW 1.24 and before stored it already written out)."""
    return iso_now()


def _check_failure_code(error: str | BaseException) -> str:
    """The header clock's reason: by the error's type (its text is in the owner's language);
    only a plain text (from an older caller) is matched by its words."""
    if isinstance(error, SecretStoreError):
        return "secrets_migration_required"
    if isinstance(error, BaseException):
        return error_class(error)
    message = str(error or "").lower()
    if "legacy plaintext" in message or "master key" in message or "secret" in message:
        return "secrets_migration_required"
    return error_class(error)


def _last_good_scheduled_check(health: Health) -> int | None:
    """When the last scheduled check completed (state written by an older TOW: its last
    scheduled attempt, unless that attempt was itself a recorded failure)."""
    if "auto_ok_at_ts" in health:
        value = health.get("auto_ok_at_ts")
        return int(value) if isinstance(value, (int, float)) else None
    value = health.get("auto_at_ts")
    return int(value) if isinstance(value, (int, float)) and health.get("check_ok") is not False else None


def record_check_failure(error: str | BaseException, *, how: str = "auto") -> Health:
    """Persist a failed apply attempt so the UI clock has a fresh attempt time."""
    code = _check_failure_code(error)
    now = machine_now()
    with persistence_lock():  # read-modify-write: never lose a concurrent edit
        state = load_state()
        previous = health_of(state)
        # A blocked check learned nothing about qBit or the bot: keep what the last real
        # check saw. Writing qbit_ok=False showed "связи нет" and swallowed the next
        # real "торрент-клиент недоступен" notification.
        health = Health()
        if "qbit" in previous:
            health["qbit"] = previous["qbit"]
        if "client" in previous:
            health["client"] = previous["client"]
        if "qbit_ok" in previous:
            health["qbit_ok"] = previous["qbit_ok"]
        if "clients_ok" in previous:
            health["clients_ok"] = previous["clients_ok"]
        health.update(
            {
                "check_ok": False,
                "check_error": code,
                "at": now.isoformat(timespec="seconds"),
                "at_ts": int(now.timestamp()),
                # The header countdown runs from the last scheduled *attempt*...
                "auto_at_ts": int(now.timestamp()) if how == "auto" else previous.get("auto_at_ts"),
                # ...the watchdog from the last scheduled check that really ran (M6): a check
                # failing on every run must not look like a healthy schedule.
                "auto_ok_at_ts": _last_good_scheduled_check(previous),
                "check_failures": int(previous.get("check_failures") or 0) + (1 if how == "auto" else 0),
            }
        )
        state["health"] = health
        save_state(state)
    log_event("check_blocked", reason=code, how=how)
    return health


def _relabel_source_hash(record: HistoryRecord | None, old: str, new: str) -> None:
    for item in ((record or {}).get("items") or {}).values():
        if isinstance(item, dict) and str(item.get("source_hash") or "").casefold() == old.casefold():
            item["source_hash"] = new


# Keep this much free beyond the torrent itself (temporary files, other downloads).
FREE_SPACE_MARGIN = 512 * 1024 * 1024


def _bytes_already_there(dest: str, file: Any, name: str, is_multi: bool) -> int:
    """How much of ``file`` already lies in ``dest`` (the client keeps it and adds only the rest).

    A multi-file torrent lands in ``dest/<name>/`` (or straight in ``dest/`` when the client
    creates no subfolder); a single file is ``dest/<name>``.
    """
    from pathlib import Path

    relative = Path(*str(file.path).replace("\\", "/").split("/"))
    candidates = [Path(dest) / name / relative, Path(dest) / relative] if is_multi and name else [Path(dest) / relative]
    for candidate in candidates:
        try:
            if candidate.is_file():
                return min(int(candidate.stat().st_size), int(getattr(file, "size", 0) or 0))
        except OSError, ValueError:
            continue
    return 0


def free_space_problem(
    dest: str, files: tuple[Any, ...], selected_indices: Any, *, name: str = "", is_multi: bool = False
) -> TowError | None:
    """G6: refuse an add that cannot fit on the target drive. Unknown free space (the
    folder is on another machine or not reachable from here) does not block.

    Only NEW bytes count (M5): files of the selection already in the folder (a new revision
    of a season whose earlier episodes are there) are not downloaded again. The sizes are kept
    as numbers: the reader's language writes their decimal sign.
    """
    import shutil
    from pathlib import Path

    wanted = set(selected_indices or ())
    selected = [f for f in files if f.index in wanted and not getattr(f, "is_pad", False)]
    needed = sum(int(getattr(f, "size", 0) or 0) for f in selected)
    if needed <= 0:
        return None
    from tow.folders import seen_from_here

    probe = Path(dest)
    if not seen_from_here(dest):
        return None  # /downloads of a remote client, or a drive this PC does not have
    needed -= sum(_bytes_already_there(dest, f, name, is_multi) for f in selected)
    if needed <= 0:
        return None
    while not probe.exists() and probe.parent != probe:
        probe = probe.parent
    try:
        free = shutil.disk_usage(probe).free
    except OSError:
        return None
    if needed + FREE_SPACE_MARGIN <= free:
        return None
    gib = 1024**3
    return TowError("check.low_disk", needed=round(needed / gib, 1), free=round(free / gib, 1), path=str(probe))


def _progress_only_row(topic: Topic) -> dict[str, Any]:
    return {
        "id": topic.get("id"),
        "title": topic.get("title"),
        "url": topic.get("url"),
        "ok": True,
        "hash": topic.get("hash"),
        "changed": False,
        "status": "skipped",
        "skipped": t("check.progress_only", owner_language()),
    }


def _fail_row(topic: Topic, row: dict[str, Any], error: TowError) -> None:
    row.update({"ok": False, "status": "failed"})
    _set_error(row, error)
    _stamp(topic, row)


def _stamp(topic: Topic, row: dict[str, Any]) -> None:
    """The check's result on the topic: ``last_error`` keeps the text (older TOW versions read
    it), ``last_error_code``/``_params`` the typed error Home and History render in the reader's
    language, ``last_error_class`` its status class."""
    topic["last_ok"] = bool(row.get("ok"))
    topic["last_error"] = row.get("error")
    record = row.get("error_record") if row.get("error") else None
    if isinstance(record, dict):
        topic["last_error_code"] = record["code"]
        topic["last_error_params"] = record.get("params") or {}
    else:
        topic.pop("last_error_code", None)
        topic.pop("last_error_params", None)
    topic["last_error_class"] = (
        str(row.get("error_class") or error_class(str(row.get("error") or ""))) if row.get("error") else ""
    )
    topic["last_check"] = _now()
    if row.get("ok") and row.get("status") != "skipped":
        topic["last_ok_at"] = topic["last_check"]  # G3: when it last really worked
    topic["last_changed"] = bool(row.get("ok") and row.get("changed"))


def _without_scan_time(record: Any) -> Any:
    """A topic record as far as a change counts: ``last_scan_at`` changes on every pass and
    nothing reads it, so it is saved together with a real change, never alone."""
    return {k: v for k, v in record.items() if k != "last_scan_at"} if isinstance(record, dict) else record


def _merge_history(disk: DownloadHistory, seen: Mapping[str, Any], reconciled: Mapping[str, Any]) -> bool:
    """Put what the reconcile changed onto the history as it is on disk now, topic by topic;
    True when that changed the history (a new scan time alone does not).

    ``seen`` is the copy the reconcile started from: a topic record it did not change keeps
    whatever was written meanwhile (a restore, an import).
    """
    seen_topics: dict[str, Any] = as_dict(seen.get("topics"))
    new_topics: dict[str, Any] = as_dict(reconciled.get("topics"))
    disk_topics = disk.get("topics")
    changed = False
    if not isinstance(disk_topics, dict):
        disk_topics = disk["topics"] = {}
    for key in set(seen_topics) | set(new_topics):
        if key not in new_topics:
            changed = disk_topics.pop(key, _ABSENT) is not _ABSENT or changed
        elif new_topics[key] != seen_topics.get(key):
            before = disk_topics.get(key, _ABSENT)
            changed = changed or before is _ABSENT or _without_scan_time(before) != _without_scan_time(new_topics[key])
            disk_topics[key] = new_topics[key]
    others = {key: value for key, value in reconciled.items() if key != "topics" and seen.get(key) != value}
    changed = changed or any(key not in disk or disk.get(key) != value for key, value in others.items())
    cast(dict[str, Any], disk).update(others)
    return changed


_ABSENT: Any = object()


def _load_history(*, quarantine: bool) -> tuple[DownloadHistory, bool]:
    """The download history, and whether it had to start over.

    The history is rebuildable (the clients still have every torrent): a corrupt or
    quarantined download_history.json no longer blocks every check. The check goes on
    with an empty history, the quarantined file stays where it is, and the owner is told.
    A file that merely cannot be read right now (``StoreReadError``) still stops the run.
    """
    try:
        return cast(DownloadHistory, load_download_history(quarantine=quarantine)), False
    except StoreReadError:
        raise
    except StoreCorruptionError:
        return {"schema_version": 1, "topics": {}}, True


# How long the Home page says that the download history was started over.
HISTORY_REBUILT_NOTICE_SEC = 7 * 24 * 3600


def _note_history_rebuilt(health: Health, previous: Health, *, rebuilt: bool) -> None:
    if rebuilt:
        health["history_rebuilt_at"] = health["at_ts"]
        return
    earlier = previous.get("history_rebuilt_at")
    if isinstance(earlier, int) and health["at_ts"] - earlier < HISTORY_REBUILT_NOTICE_SEC:
        health["history_rebuilt_at"] = earlier


def _reconcile_and_commit(
    state: dict[str, Any],
    results: list[dict[str, Any]],
    run: _CheckRun,
    records: list[tuple[str, dict[str, Any]]],
    want: set[str] | None,
    commit_state: Callable[[bool], None],
) -> bool:
    """Reconcile with the clients, then commit history and state; True when a journal
    cleanup is left pending.

    Reconcile asks the torrent clients and looks at files (a sleeping NAS can take minutes):
    it works on a copy of the history, OUTSIDE the data lock, so the web UI and the watchdog
    are never held up by it. The lock is taken again only to merge and commit (H1).
    """
    with persistence_lock():
        history, rebuilt = _load_history(quarantine=True)
    history_seen = copy.deepcopy(history)
    _reconcile_all(state, results, run, history, records, want)
    _queue_recoveries(state, results, run)
    keep_days, max_items = run.history_retention
    watched = {str(topic.get("id")) for topic in topics_of(state)}
    with persistence_lock():
        disk_history, disk_rebuilt = _load_history(quarantine=True)
        if rebuilt and not disk_rebuilt:
            history = history_seen  # restored meanwhile: an empty-history reconcile must not overwrite it
        changed = _merge_history(disk_history, history_seen, history)
        pruned = download_history.prune(disk_history, watched=watched, keep_days=keep_days, max_items=max_items)
        if disk_rebuilt:
            run.record("download_history_rebuilt", component="storage", status="rebuilt", how=run.how)
        if not disk_rebuilt and not changed and not pruned:
            # Nothing new in the history (most progress passes): one atomic state write is
            # enough - no two-file journal, no backup copy of a half-megabyte file.
            commit_state(False)
            return False
        # A rebuilt history is written even when empty: the next run reads it without a warning.
        with check_transaction.check_store_transaction() as transaction:
            save_download_history(disk_history)
            transaction.mark_history_committed()
            commit_state(disk_rebuilt)
            transaction.mark_committed()
        return transaction.cleanup_pending


def _client_title(cfg: dict[str, Any], client_id: str) -> str:
    try:
        row = client_factory.client_configuration(cfg, client_id)
    except RuntimeError, ValueError:
        return client_id
    return str(row.get("title") or row.get("kind") or client_id)


def _reconcile_all(
    state: dict[str, Any],
    results: list[dict[str, Any]],
    run: Any,
    history: DownloadHistory,
    records: list[tuple[str, dict[str, Any]]],
    want: set[str] | None,
) -> None:
    result_by_id = {str(row.get("id")): row for row in results}
    for topic in topics_of(state):
        if want is not None and str(topic.get("id")) not in want:
            continue
        row = result_by_id.get(str(topic.get("id"))) or {}
        if not topic.get("hash"):
            continue
        _reconcile_one(topic, row, run, history, records)


def _audited_send(
    secrets: dict[str, Any],
    *,
    text: str,
    operation_id: str,
    topic: Topic | None = None,
    how: str = "auto",
) -> bool:
    """Deliver a message ``delivery.dispatch`` has already put into the messengers' queue, and log it."""
    from tow.notifiers import connected, deliver_queued

    topic_id = (topic or {}).get("id")
    fields = {
        "operation_id": operation_id,
        "component": "notification",
        "topic_id": topic_id,
        "topic": topic_id,
        "how": how,
    }
    channels = [kind for kind, _, _ in connected(secrets)]
    if not channels:
        # No messenger is connected: nothing is sent, so nothing is logged as a delivery (a
        # "sending to bot" / "bot delivery error" pair after every check meant nothing).
        return False
    log_event("bot_delivery_started", **fields, integration_id=",".join(channels))
    results = deliver_queued(secrets)
    for kind, (ok, reason) in results.items():
        log_event(
            "bot_delivery_succeeded" if ok else "bot_delivery_failed",
            **fields,
            integration_id=kind,
            status="succeeded" if ok else "queued",
            reason=None if ok else "send_failed",
            error=reason or None,
        )
    return bool(results) and all(ok for ok, _ in results.values())


def run_check(
    *,
    apply: bool,
    notify: bool,
    ids: list[str] | None = None,
    ignore_cool: bool = False,
    how: str = "auto",
    progress_only: bool = False,
    wait: bool = True,
    scheduled_scope: str = "",
) -> dict[str, Any]:
    """Run one check; applying checks never overlap (scheduled, progress, web, CLI).

    ``wait=False`` (web requests): raise ``CheckBusyError`` at once when another applying
    check runs, instead of holding the request - and every page behind it - for minutes.

    ``check_run_lock`` serializes applying checks across processes for the whole run. The
    shared persistence lock is taken only to read the state and to commit the result, so
    edits in the web UI, the watchdog and the night copy are not held up by network time;
    edits made meanwhile are kept (``merge_check_results``).
    """
    if scheduled_scope not in {"", "global", "timer"} or (progress_only and scheduled_scope):
        raise ValueError("invalid scheduled check scope")
    if not apply:
        return _run_check(
            apply=False,
            notify=notify,
            ids=ids,
            ignore_cool=ignore_cool,
            how=how,
            progress_only=progress_only,
            scheduled_scope=scheduled_scope,
        )
    with check_run_lock(wait=wait):
        return _run_check(
            apply=True,
            notify=notify,
            ids=ids,
            ignore_cool=ignore_cool,
            how=how,
            progress_only=progress_only,
            scheduled_scope=scheduled_scope,
        )


def _download_limited(tr: Any) -> bool:
    """The site's accounts have a daily .torrent download limit (its preset says so, its own
    ``download_limit`` setting overrides): a preview must not spend it."""
    spec = getattr(tr, "spec", None)
    if isinstance(spec, dict) and "download_limit" in spec:
        return bool(spec.get("download_limit"))
    return presets.daily_limited(str(getattr(tr, "name", "")))


def _skip_row(
    topic: Topic,
    row: dict[str, Any],
    tr: Any,
    *,
    old: str,
    quota: set[str],
    state: dict[str, Any],
    how: str,
    apply: bool,
) -> bool:
    """Fill ``row`` and return True when the topic is not fetched in this run."""
    url = str(topic.get("url") or "")
    if topic.get("paused") or topic.get("once_done"):
        row.update(
            {
                "ok": True,
                "hash": old,
                "changed": False,
                "status": "skipped",
                "skip": "paused" if topic.get("paused") else "once_done",
                "skipped": t("check.skip.paused" if topic.get("paused") else "check.skip.once_done", owner_language()),
            }
        )
        return True
    if not tr:
        error = TowError("check.no_tracker")
        _set_error(row, error)
        _fail_log(topic, url, error, how=how, persist=apply)
        _stamp(topic, row)
        return True
    row["tracker"] = tr.name
    mirror_state = mirror_of(state, tr.name)
    if tr.name in quota:
        error = TowError("check.daily_limit")
    elif mirror_state is not None and mirror_state.get("frozen") is True:
        error = TowError("check.frozen")
    elif not apply and _download_limited(tr):
        row.update(
            {
                "ok": True,
                "hash": old,
                "changed": False,
                "status": "skipped",
                "skipped": t("check.preview_skips_limited", owner_language()),
            }
        )
        return True
    else:
        return False
    _set_error(row, error)
    _fail_log(topic, url, error, tr, how=how, persist=apply)
    _stamp(topic, row)
    return True


def _mark_preview(
    row: dict[str, Any], *, preview: bool, would_add: bool, migrates: bool, updates_selection: bool
) -> None:
    """A dry run says what an apply would do to the client."""
    if preview:
        row["would_add"] = would_add
        row["would_migrate_hash_identity"] = migrates
        row["would_update_selection"] = updates_selection
        row["status"] = "preview"


# The code of "the previous revision still runs in the client" (Home offers to stop it).
PREVIOUS_REVISION_ACTIVE = "check.previous_revision_active"
_LEGACY_PREVIOUS_REVISION = "previous torrent revision is still active"


def blocked_by_previous_revision(topic: Topic) -> bool:
    """The topic waits for its previous revision to be stopped in the client (G1)."""
    if topic.get("last_error_code"):
        return topic.get("last_error_code") == PREVIOUS_REVISION_ACTIVE
    return str(topic.get("last_error") or "").startswith(_LEGACY_PREVIOUS_REVISION)


def _client_unreachable(run: Any, client_id: str) -> TowError:
    """The topic's client did not answer this run: its own error (or "no connection") as the reason."""
    cause = run.client_errors.get(client_id)
    if isinstance(cause, TowError):
        reason: Any = cause
    elif cause is not None:
        reason = str(cause) or type(cause).__name__
    else:
        reason = Msg("check.no_connection")
    return TowError("check.client_unreachable", reason=reason)


def _client_marks(topic: Topic) -> tuple[str, ...]:
    """What the client is changed with: the folder, the client and the file selection."""
    return tuple(repr(topic.get(key)) for key in ("save_path", "client_id", "selection"))


def _withdrawn_meanwhile(topic: Topic, row: dict[str, Any], old: str, started: tuple[str, ...] | None) -> bool:
    """The owner paused or deleted the topic, or changed its folder, client or file selection,
    after this run started (its copy is from the start): look at the state now, right before
    the client is changed, and skip it then - the next check works with what is saved."""
    try:
        current = next(
            (t for t in load_state(quarantine=False).get("topics") or [] if str(t.get("id")) == str(topic.get("id"))),
            None,
        )
    except StoreCorruptionError:
        return False  # the commit will fail closed on its own
    edited = current is not None and started is not None and _client_marks(current) != started
    if current is not None and not current.get("paused") and not edited:
        return False
    key = (
        "check.deleted_meanwhile"
        if current is None
        else "check.paused_meanwhile"
        if current.get("paused")
        else "check.edited_meanwhile"
    )
    row.update({"ok": True, "hash": old, "changed": False, "status": "skipped", "skipped": t(key, owner_language())})
    return True


def _assert_client_can_add(
    state: dict[str, Any],
    topic: Topic,
    topic_client: TorrentClientAdapter,
    *,
    h: str,
    old: str,
    dest: str,
    plan: Any,
    files: tuple[Any, ...],
    replaces_revision: bool,
) -> None:
    """Refuse a client mutation that could clash with another topic or a live revision."""
    conflicting_topic = _conflicting_topic_claim(state, topic, h, dest, plan.selected_files, plan.mode)
    if conflicting_topic:
        raise TowError("check.hash_claimed", topic=conflicting_topic)
    if replaces_revision:
        revision_hashes = [old, *[str(value) for value in topic.get("previous_hashes") or []]]
        for revision_hash in dict.fromkeys(revision_hashes):
            overlap = _active_revision_overlap(topic_client, revision_hash, files)
            if overlap:
                raise TowError(PREVIOUS_REVISION_ACTIVE, file=overlap)
    capabilities = topic_client.capabilities or {}
    if not all(
        capabilities.get(name) is True for name in ("stopped_add", "file_selection", "priority_readback", "start_stop")
    ):
        raise TowError("check.client_cannot_select")


@dataclass
class _CheckRun:
    """Shared state of one run_check call, passed to the per-topic step."""

    apply: bool
    notify: bool
    how: str
    ignore_cool: bool
    ua: str | None
    state: dict[str, Any]
    trackers: dict[str, Any]
    quota: set[str]
    secrets: dict[str, Any]
    client_errors: dict[str, Exception]
    get_client_for: Callable[[Topic], tuple[str, TorrentClientAdapter | None]]
    record: Callable[..., None]
    queue_notification: Callable[..., None]
    # How long the download history keeps what is no longer watched (tow.download_history).
    history_retention: tuple[int, int] = (download_history.DEFAULT_KEEP_DAYS, download_history.DEFAULT_MAX_ITEMS)
    # Clients on another computer: a folder they name is not measured on this one's disks (G6).
    remote_clients: frozenset[str] = frozenset()
    # The secrets.enc that ``secrets`` was decrypted from (see _reread_secrets).
    secrets_stamp: FileStamp | None = None


def _reread_secrets(run: _CheckRun) -> None:
    """Pick up what a login in this run persisted (new tracker cookies) before the next request.
    The file is decrypted again only when it changed: twice for every topic, it was decrypted
    4000 times in a check of 2000 topics."""
    stamp = file_stamp(encrypted_secrets_path())
    if stamp is None or stamp != run.secrets_stamp:
        run.secrets = load_secrets()
        run.secrets_stamp = stamp


def _confirm_matching_magnet(
    topic: Topic,
    run: _CheckRun,
    tr: Any,
    url: str,
    old: str,
    policy: dict[str, Any],
    topic_client: TorrentClientAdapter | None,
    row: dict[str, Any],
    torrent_error: Exception,
) -> bool:
    """The .torrent is unavailable (tracker auth), but the tracker's magnet still names
    the revision we already have: record an unchanged, confirmed check and return True."""
    try:
        magnet_url, _reported_hash = tr.fetch_magnet(
            url, run.secrets, run.ua, ignore_cool=run.ignore_cool, persist=run.apply
        )
    except Exception as exc:  # noqa: BLE001 - no magnet: the .torrent's own error stands
        _LOG.warning("magnet not read for %s: %s", tr.name, type(exc).__name__)
        magnet_url = ""
    if run.apply and magnet_url:
        _observe_cached_magnet(url, magnet_url, topic)
    if not _magnet_matches_saved_hash(magnet_url, old, topic_client):
        return False
    saved_selection_ready = bool(
        not topic.get("selection_dirty")
        and (
            policy["mode"] == "all"
            or (str(topic.get("selection_hash") or "").upper() == old and topic.get("selection_verified") is True)
        )
    )
    if not saved_selection_ready:
        raise TowError("check.link_unavailable_selection") from torrent_error
    if run.apply and policy["tracking_mode"] == "once":
        if topic_client is None or not _confirm_client_add(topic_client, old, str(topic.get("save_path") or "")):
            raise TowError("check.once_unconfirmed") from torrent_error
        topic["once_done"] = True
    tracker_title = _current_tracker_title(tr, url, run.secrets, run.ua, ignore_cool=run.ignore_cool, persist=False)
    if tracker_title:
        topic["tracker_title"] = tracker_title
        row["tracker_title"] = tracker_title
    row.update(
        {
            "ok": True,
            "hash": old,
            "changed": False,
            "source": "matching_magnet",
            "fallback_reason": "tracker_auth",
            "warning": "torrent link unavailable; existing magnet identity confirmed",
            "status": "skipped",
            "skipped": t("check.skip.magnet_confirmed", owner_language()),
        }
    )
    run.record(
        "tracker_checked",
        component="tracker",
        integration_id=tr.name,
        topic_id=topic.get("id"),
        topic=topic.get("id"),
        tracker=tr.name,
        hash=old,
        source="matching_magnet",
        fallback_reason="tracker_auth",
        status="succeeded",
        how=run.how,
    )
    return True


def _accept_existing_torrent(
    topic: Topic,
    run: _CheckRun,
    row: dict[str, Any],
    topic_client: TorrentClientAdapter,
    *,
    h: str,
    dest: str,
    plan: Any,
    operation_id: str | None,
    client_id: str,
    migrated: bool,
) -> bool:
    """The revision is already in the client (or only its hash label changed): verify it
    without touching it and return whether its file selection is TOW-verified."""
    if not client_owned_by_tow(topic_client, h):
        # Never green: TOW can change nothing about a torrent without its mark (the file
        # selection, the next revision), and it may be TOW's own add whose marking failed. The
        # hash lets the owner adopt it into TOW (tow.adopt) - never done without being asked.
        if plan.mode != "all":
            raise TowError("check.not_owned_partial", hash=h)
        raise TowError("check.not_owned_existing", cls="qbit", hash=h)
    if not _confirm_client_add(topic_client, h, dest):
        raise TowError("check.migration_unconfirmed" if migrated else "check.existing_unconfirmed")
    selection_verified = True
    row["added"] = False
    if migrated:
        row["hash_identity_migrated"] = True
        row["status"] = "skipped"
        row["skipped"] = t("check.skip.hash_normalized", owner_language())
    else:
        row["skipped"] = t("check.skip.already_in_client", owner_language())
        row["selection_verified"] = selection_verified
        row["status"] = "skipped"
    run.record(
        "client_updated",
        operation_id=operation_id,
        component="client",
        integration_id=client_id,
        client_id=client_id,
        client_kind=row.get("client_kind"),
        topic_id=topic.get("id"),
        topic=topic.get("id"),
        title=topic.get("title"),
        hash=h,
        status="skipped",
        reason="hash_identity_migrated" if migrated else "already_present",
        how=run.how,
    )
    return selection_verified


def _is_pending_tow_add(topic_client: TorrentClientAdapter, h: str) -> bool:
    """The torrent is still marked tow-pending: an earlier run added it but did not finish."""
    existing_info = topic_client.inspect_torrent(h)
    existing_tags = {str(tag).strip().casefold() for tag in (existing_info or {}).get("tags") or []}
    return bool(
        existing_info and _client_info_matches_hash(existing_info, h) and {"tow", "tow-pending"}.issubset(existing_tags)
    )


def _add_new_revision(
    topic: Topic,
    run: _CheckRun,
    row: dict[str, Any],
    topic_client: TorrentClientAdapter,
    *,
    blob: bytes,
    metadata: Any,
    h: str,
    old: str,
    dest: str,
    plan: Any,
    policy: dict[str, Any],
    operation_id: str,
    client_id: str,
) -> str:
    """Add the revision with its file selection, confirm it by read-back, log and notify.

    Returns the hash the client actually registered (a hybrid may come back as its v1 hash).
    """
    if client_id not in run.remote_clients and (
        problem := free_space_problem(
            dest,
            metadata.files,
            plan.selected_indices,
            name=str(getattr(metadata, "name", "") or ""),
            is_multi=bool(getattr(metadata, "is_multi", False)),
        )
    ):
        raise problem
    added_info = topic_client.add_torrent_selected(blob, dest, h, plan.selected_indices)
    resolved_hash = str((added_info or {}).get("hash") or "").upper()
    if resolved_hash:
        if resolved_hash not in client_identities(metadata):
            raise TowError("check.unexpected_identity")
        if resolved_hash != h:
            h = resolved_hash
            row["hash"] = h
            row["client_hash_legacy_v1"] = bool(h == str(getattr(metadata, "hash_v1", None) or "").upper())
    if not _confirm_client_add(topic_client, h, dest, require_tow_ownership=True):
        raise TowError("check.add_unconfirmed")
    row["added"] = True
    row["status"] = "succeeded"
    run.record(
        "client_added",
        operation_id=operation_id,
        component="client",
        integration_id=client_id,
        client_id=client_id,
        client_kind=row.get("client_kind"),
        topic_id=topic.get("id"),
        topic=topic.get("id"),
        title=topic.get("title"),
        hash=h,
        path=dest,
        selection_mode=plan.mode,
        selected_file_count=len(plan.selected_indices),
        total_file_count=plan.total_files,
        tracking_mode=policy["tracking_mode"],
        selected_files_preview=list(plan.selected_files[:20]),
        selection_truncated=len(plan.selected_files) > 20,
        status="succeeded",
        how=run.how,
    )
    if run.notify:
        run.queue_notification(
            topic,
            kind="updated" if old and h != old else "added",
            operation_id=operation_id,
            tracker=str(row.get("tracker") or ""),
        )
    return h


def _unrecorded_own_add(state: dict[str, Any], topic: Topic, h: str) -> bool:
    """A TOW-owned torrent no topic records: an earlier run added and confirmed it, but its
    commit (and with it the staged "added" message) was lost - this run finishes that add."""
    wanted = h.upper()
    if wanted in {str(value).upper() for value in topic.get("previous_hashes") or []}:
        return False  # an older revision of this topic, not a new add
    return not any(
        str(other.get("hash") or "").upper() == wanted
        for other in topics_of(state)
        if str(other.get("id")) != str(topic.get("id"))
    )


def _update_client_selection(
    topic: Topic,
    run: _CheckRun,
    row: dict[str, Any],
    topic_client: TorrentClientAdapter,
    *,
    blob: bytes,
    h: str,
    old: str,
    dest: str,
    plan: Any,
    policy: dict[str, Any],
    operation_id: str,
    client_id: str,
    resume: bool,
    pending_recovery: bool,
) -> None:
    """Apply a new file selection to a TOW-owned torrent already in the client, or finish
    an add an earlier run left pending (``resume``); confirm by read-back, log, notify."""
    if not client_owned_by_tow(topic_client, h):
        raise TowError("check.not_owned_priorities")
    if not _confirm_client_add(topic_client, h, dest, require_tow_ownership=True):
        raise TowError("check.save_path_unconfirmed")
    owned_add_recovery = h != old and (is_owned_add_recovery(topic) or _unrecorded_own_add(run.state, topic, h))
    completed_pending_add = resume or owned_add_recovery
    topic_client.configure_torrent_selection(blob, h, plan.selected_indices, ensure_started=completed_pending_add)
    if not _confirm_client_add(topic_client, h, dest, require_tow_ownership=True):
        raise TowError("check.selection_unconfirmed")
    row["added"] = completed_pending_add
    row["selection_updated"] = not completed_pending_add
    row["pending_add_recovered"] = pending_recovery
    row["status"] = "succeeded"
    run.record(
        "client_added" if completed_pending_add else "client_selection_updated",
        operation_id=operation_id,
        component="client",
        integration_id=client_id,
        client_id=client_id,
        client_kind=row.get("client_kind"),
        topic_id=topic.get("id"),
        topic=topic.get("id"),
        title=topic.get("title"),
        hash=h,
        selection_mode=plan.mode,
        selected_file_count=len(plan.selected_indices),
        total_file_count=plan.total_files,
        tracking_mode=policy["tracking_mode"],
        selected_files_preview=list(plan.selected_files[:20]),
        selection_truncated=len(plan.selected_files) > 20,
        recovered=pending_recovery or owned_add_recovery,
        status="succeeded",
        how=run.how,
    )
    if completed_pending_add and run.notify:
        run.queue_notification(
            topic,
            kind="updated" if old and h != old else "added",
            operation_id=operation_id,
            tracker=str(row.get("tracker") or ""),
        )


def _selection_plan(
    row: dict[str, Any], metadata: Any, policy: dict[str, Any], selection_title: str, *, old: str
) -> Any | None:
    """The file selection, or None while a watched range is entirely in the future (B8)."""
    try:
        return resolve_selection(metadata.files, policy, preferred_season=parse_season_hint(selection_title))
    except SelectionPendingError as pending:
        if policy["tracking_mode"] != "watch":
            raise
        # Waiting for episodes that are not out yet is not a failure (it errored every run).
        row.update({"ok": True, "hash": old, "changed": False, "status": "skipped"})
        row["skipped"] = t("check.waiting_episodes", owner_language(), pending=pending.params.get("episodes", ""))
        return None


@dataclass
class _TopicCheck:
    """One topic's check in progress: what its steps (fetch, identify, apply, record) share."""

    topic: Topic
    run: _CheckRun
    tracker: GenericHttpTracker
    url: str
    row: CheckRow
    old: str  # the revision the topic had (its hash), "" for a new topic
    client_id: str
    client: TorrentClientAdapter | None  # None: the client did not answer this run
    operation_id: str | None = None  # set once a client operation has started
    started: tuple[str, ...] | None = None  # the topic's _client_marks when its check began


class _Fetched(NamedTuple):
    blob: bytes
    magnet_hash: str | None  # the .torrent was built from the tracker's magnet (its hash)


def _check_topic(topic: Topic, run: _CheckRun) -> CheckRow:
    """Check one topic: fetch, identify, apply to its client. Returns the result row."""
    url = topic.get("url") or ""
    tr = match_tracker(run.trackers, url)
    row: CheckRow = {
        "id": topic.get("id"),
        "title": topic.get("title"),
        "url": url,
        "ok": False,
    }
    old = str(topic.get("hash") or "").upper()
    if _skip_row(topic, row, tr, old=old, quota=run.quota, state=run.state, how=run.how, apply=run.apply):
        return row
    assert tr is not None  # _skip_row handled "no tracker"
    started = _client_marks(topic)
    client_id, topic_client = run.get_client_for(topic)
    topic["client_id"] = client_id
    row["client_id"] = client_id
    row["client_kind"] = topic_client.client_kind if topic_client is not None else None
    work = _TopicCheck(topic, run, tr, url, row, old, client_id, topic_client, started=started)
    try:
        _check_revision(work)
    except TorrentPathConflictError:
        _topic_failed(work, TowError("content.path_conflict"))
    except Exception as error:  # noqa: BLE001 - any failure of one topic is its result, never the run's end
        _topic_failed(work, error)
    _stamp(topic, row)
    return row


def _check_revision(work: _TopicCheck) -> None:
    """Fetch the topic's current revision, identify it and bring the client in line with it."""
    topic, run, row, old = work.topic, work.run, work.row, work.old
    policy = policy_from_topic(topic)
    fetched = _fetch_revision(work, policy)
    if fetched is None:
        return  # the tracker's magnet confirmed the revision the topic already has
    metadata = parse_torrent_metadata(fetched.blob)
    if fetched.magnet_hash is not None:
        verify_magnet_metadata(metadata, fetched.magnet_hash)
    if run.apply:
        from tow import torrent_cache

        try:
            torrent_cache.remember(fetched.blob, work.url)
        except (TowError, OSError, ValueError, RuntimeError) as cache_exc:
            # Optional retention cannot turn a confirmed client result into a failure.
            log_event(
                "content_cache_failed", topic=topic.get("id"), code=getattr(cache_exc, "code", "content.unavailable")
            )
    h, hash_alias_migration = resolve_client_hash(metadata, old, work.client, row)
    if getattr(metadata, "hash_v1", None):
        row["source_hash_v1"] = metadata.hash_v1
    if getattr(metadata, "hash_v2", None):
        row["source_hash_v2"] = metadata.hash_v2
    plan = _plan_selection(work, metadata, policy)
    if plan is None:
        return  # waiting for episodes that are not out yet
    _record_found(work, metadata, plan, h=h, migrates=hash_alias_migration)
    needs_selection_update = bool(
        h == old
        and (
            topic.get("selection_dirty") or (plan.mode != "all" and str(topic.get("selection_hash") or "").upper() != h)
        )
    )
    _mark_preview(
        row,
        preview=not run.apply and (h != old or needs_selection_update),
        would_add=h != old and not hash_alias_migration,
        migrates=hash_alias_migration,
        updates_selection=needs_selection_update,
    )
    if run.apply and (h != old or needs_selection_update):
        if _withdrawn_meanwhile(topic, row, old, work.started):
            return
        _apply_revision(
            work,
            fetched,
            metadata,
            h=h,
            migrates=hash_alias_migration,
            plan=plan,
            policy=policy,
            needs_selection_update=needs_selection_update,
        )
    elif not old:
        topic["hash"] = h
    elif run.apply and policy["tracking_mode"] == "once":
        client = work.client
        if client is None or not _confirm_client_add(client, h, str(topic.get("save_path") or "")):
            raise TowError("check.once_unconfirmed")
        topic["once_done"] = True

    if (
        h == old
        and not needs_selection_update
        and topic.get("selection_verified") is True
        and str(topic.get("selection_hash") or "").upper() == h
    ):
        # The hash binds these original names to the already-confirmed rule.
        # Refresh legacy/corrupt caches without any client mutation. A preview
        # changes only its in-memory copy; the applying commit remains journaled.
        store_file_aliases(topic, h, metadata.files, mode=plan.mode)


def _fetch_revision(work: _TopicCheck, policy: dict[str, Any]) -> _Fetched | None:
    """The topic's .torrent; None when the tracker's magnet confirmed the saved revision."""
    run = work.run
    token = work.topic.get("content_token")
    if not work.old and token:
        from tow.content import site_revision

        try:
            prepared = site_revision(str(token), work.url, work.client_id)
            if prepared is not None:
                return _Fetched(prepared, None)
        except TowError as exc:
            # Retry an expired preparation only with proof of its revision. Fresh metadata
            # is verified before client operations, never silently replaced by a new "all".
            if exc.code != "content.expired" or not (work.topic.get("content_hash") or policy["mode"] == "exact"):
                raise
    if run.apply:
        # Pick up tracker cookies a previous topic's login persisted in this run;
        # the snapshot taken at the start would make every topic log in again.
        _reread_secrets(run)
    try:
        blob = work.tracker.fetch_torrent(work.url, run.secrets, run.ua, ignore_cool=run.ignore_cool, persist=run.apply)
    except Exception as torrent_error:  # a tracker refusal may fall back to its magnet; else re-raised
        if not work.old and token:
            raise  # no temporary magnet add before the prepared identity is verified
        return _fetch_by_magnet(work, policy, torrent_error)
    return _Fetched(blob, None)


def _fetch_by_magnet(work: _TopicCheck, policy: dict[str, Any], torrent_error: Exception) -> _Fetched | None:
    """The .torrent is unavailable (tracker auth): confirm the saved revision by the tracker's
    magnet, or build the .torrent from that magnet in a client that can; else the error stands."""
    topic, run, tr, client = work.topic, work.run, work.tracker, work.client
    fetch_magnet = getattr(tr, "fetch_magnet", None)
    auth_refused = error_class(torrent_error) == "tracker_auth"
    if (
        work.old
        and auth_refused
        and callable(fetch_magnet)
        and _confirm_matching_magnet(topic, run, tr, work.url, work.old, policy, client, work.row, torrent_error)
    ):
        return None
    if (
        not run.apply
        or not auth_refused
        or client is None
        or (client.capabilities or {}).get("magnet_metadata") is not True
        or not callable(fetch_magnet)
    ):
        raise torrent_error
    dest = str(topic.get("save_path") or "").strip()
    if not dest:
        raise TowError("check.no_save_path") from torrent_error
    magnet_url, magnet_hash = fetch_magnet(
        work.url,
        run.secrets,
        run.ua,
        ignore_cool=run.ignore_cool,
        persist=run.apply,
    )
    _observe_cached_magnet(work.url, magnet_url, topic)
    run.record(
        "tracker_magnet_found",
        component="tracker",
        integration_id=tr.name,
        topic_id=topic.get("id"),
        topic=topic.get("id"),
        tracker=tr.name,
        hash=magnet_hash,
        status="succeeded",
        how=run.how,
    )
    blob = client.materialize_magnet(magnet_url, dest, magnet_hash)
    work.row["source"] = "magnet"
    work.row["fallback_reason"] = "tracker_auth"
    return _Fetched(blob, magnet_hash)


def _observe_cached_magnet(url: str, magnet: str, topic: Topic) -> None:
    from tow import torrent_cache

    try:
        torrent_cache.observe_magnet(url, magnet)
    except (TowError, OSError, ValueError, RuntimeError) as exc:
        log_event("content_cache_failed", topic=topic.get("id"), code=getattr(exc, "code", "content.unavailable"))


def _plan_selection(work: _TopicCheck, metadata: Any, policy: dict[str, Any]) -> Any | None:
    """The tracker's current title (it names the season), then the file selection for it."""
    topic, run, row = work.topic, work.run, work.row
    if not work.old and topic.get("content_hash") and topic["content_hash"] != metadata.infohash:
        raise TowError("selection.preview_changed")
    if (
        policy["mode"] == "exact"
        and (not work.old or topic.get("selection_dirty"))
        and policy.get("source_hash") != metadata.infohash
    ):
        raise TowError("selection.preview_changed")
    if run.apply:
        # The fetch may have logged in again and persisted new cookies; the title
        # request must not go out with the stale ones (and log in yet again).
        _reread_secrets(run)
    tracker_title = _current_tracker_title(
        work.tracker,
        work.url,
        run.secrets,
        run.ua,
        ignore_cool=run.ignore_cool,
        persist=False,
    )
    if tracker_title:
        topic["tracker_title"] = tracker_title
        row["tracker_title"] = tracker_title
    selection_title = tracker_title or str(topic.get("tracker_title") or topic.get("title") or "")
    return _selection_plan(row, metadata, policy, selection_title, old=work.old)


def _record_found(work: _TopicCheck, metadata: Any, plan: Any, *, h: str, migrates: bool) -> None:
    """The check found the revision ``h``: the row says so, the log too; a placeholder title
    takes the torrent's name."""
    topic, run, row, tr, old = work.topic, work.run, work.row, work.tracker, work.old
    row["selection"] = plan.as_dict()
    row["hash"] = h
    row["changed"] = bool(old) and h != old and not migrates
    row["ok"] = True
    for kind in ("tracker_checked", "tracker_found") if (row["changed"] or not old) else ("tracker_checked",):
        run.record(
            kind,
            component="tracker",
            integration_id=tr.name,
            topic_id=topic.get("id"),
            topic=topic.get("id"),
            tracker=tr.name,
            hash=h,
            status="succeeded",
            how=run.how,
        )
    nm = metadata.name[:180]
    if nm and _title_placeholder(topic.get("title"), work.url):
        topic["title"] = nm
        row["title"] = nm


def _apply_revision(
    work: _TopicCheck,
    fetched: _Fetched,
    metadata: Any,
    *,
    h: str,
    migrates: bool,
    plan: Any,
    policy: dict[str, Any],
    needs_selection_update: bool,
) -> None:
    """Hand the revision (or its new file selection) to the client and record what it confirmed."""
    topic, run, row, old = work.topic, work.run, work.row, work.old
    dest = (topic.get("save_path") or "").strip()
    if not dest:
        raise TowError("check.no_save_path")
    operation_id = work.operation_id = new_operation_id("client-add")
    row["operation_id"] = operation_id
    run.record(
        "client_add_started",
        operation_id=operation_id,
        component="client",
        integration_id=work.client_id,
        client_id=work.client_id,
        client_kind=row.get("client_kind"),
        topic_id=topic.get("id"),
        topic=topic.get("id"),
        title=topic.get("title"),
        hash=h,
        path=dest,
        selection_mode=plan.mode,
        selected_file_count=len(plan.selected_indices),
        total_file_count=plan.total_files,
        tracking_mode=policy["tracking_mode"],
        status="started",
        how=run.how,
    )
    client = work.client
    if client is None:
        raise _client_unreachable(run, work.client_id)
    _assert_client_can_add(
        run.state,
        topic,
        client,
        h=h,
        old=old,
        dest=dest,
        plan=plan,
        files=metadata.files,
        replaces_revision=bool(h != old and old and not migrates),
    )
    h, selection_verified = _hand_to_client(
        work,
        client,
        fetched,
        metadata,
        h=h,
        dest=dest,
        migrates=migrates,
        plan=plan,
        policy=policy,
        needs_selection_update=needs_selection_update,
    )
    store_revision(
        topic,
        h=h,
        old=old,
        keep_previous=bool(old and old != h and not migrates),
        selection_verified=selection_verified,
        plan=plan,
        once=policy["tracking_mode"] == "once",
        files=metadata.files,
    )
    if migrates:
        row["hash_identity_from"] = old  # reconcile relabels the history (B7)


def _hand_to_client(
    work: _TopicCheck,
    client: TorrentClientAdapter,
    fetched: _Fetched,
    metadata: Any,
    *,
    h: str,
    dest: str,
    migrates: bool,
    plan: Any,
    policy: dict[str, Any],
    needs_selection_update: bool,
) -> tuple[str, bool]:
    """Add, reconfigure or just verify the torrent in the client: (the hash the client knows it
    by, whether its file selection is TOW-verified)."""
    topic, run, row, old = work.topic, work.run, work.row, work.old
    operation_id = str(work.operation_id)
    exists = client.has_hash(h)
    pending_recovery = exists and _is_pending_tow_add(client, h)
    if migrates and exists and not needs_selection_update:
        verified = _accept_existing_torrent(
            topic,
            run,
            row,
            client,
            h=h,
            dest=dest,
            plan=plan,
            operation_id=operation_id,
            client_id=work.client_id,
            migrated=True,
        )
        return h, verified
    if not exists:
        added = _add_new_revision(
            topic,
            run,
            row,
            client,
            blob=fetched.blob,
            metadata=metadata,
            h=h,
            old=old,
            dest=dest,
            plan=plan,
            policy=policy,
            operation_id=operation_id,
            client_id=work.client_id,
        )
        return added, True
    if needs_selection_update or (h != old and client_owned_by_tow(client, h)):
        _update_client_selection(
            topic,
            run,
            row,
            client,
            blob=fetched.blob,
            h=h,
            old=old,
            dest=dest,
            plan=plan,
            policy=policy,
            operation_id=operation_id,
            client_id=work.client_id,
            resume=fetched.magnet_hash is not None or pending_recovery,
            pending_recovery=pending_recovery,
        )
        return h, True
    verified = _accept_existing_torrent(
        topic,
        run,
        row,
        client,
        h=h,
        dest=dest,
        plan=plan,
        operation_id=operation_id,
        client_id=work.client_id,
        migrated=False,
    )
    return h, verified


def _topic_failed(work: _TopicCheck, error: Exception) -> None:
    """The topic's check failed: the row says why, the log and (once) the owner hear of it."""
    topic, run, row, tr = work.topic, work.run, work.row, work.tracker
    row["ok"] = False
    _set_error(row, error)
    row["status"] = "failed"
    if work.operation_id:
        run.record(
            "client_add_failed",
            operation_id=work.operation_id,
            component="client",
            integration_id=work.client_id,
            client_id=work.client_id,
            client_kind=row.get("client_kind"),
            topic_id=topic.get("id"),
            topic=topic.get("id"),
            title=topic.get("title"),
            hash=row.get("hash"),
            status="failed",
            **error_fields(error),
            how=run.how,
        )
    if is_daily_limit(error):
        run.quota.add(tr.name)
    _fail_log(topic, work.url, error, tr, how=run.how, persist=run.apply)
    if run.notify:
        run.queue_notification(
            topic,
            kind="error",
            operation_id=work.operation_id or new_operation_id("check-error"),
            tracker=str(row.get("tracker") or ""),
            error=error,
        )


def _reconcile_one(
    topic: Topic,
    row: dict[str, Any],
    run: _CheckRun,
    history: DownloadHistory,
    pending_records: list[tuple[str, dict[str, Any]]],
) -> None:
    """Reconcile one topic with its client: progress events, client presence (B4)."""
    client_id, topic_client = run.get_client_for(topic)
    checked_ok = bool(row.get("ok")) and row.get("status") != "skipped"
    if topic_client is None:
        if checked_ok:
            # B4: without its client the topic is not "ok" - TOW cannot see its torrent.
            _fail_row(topic, row, _client_unreachable(run, client_id))
        return
    history_topics = history.setdefault("topics", {})
    topic_key = str(topic.get("id"))
    if row.get("hash_identity_from"):
        # Same torrent, new hash label: its files are not a new revision. Kept even if
        # the reconcile below fails, because the topic already carries the new label.
        _relabel_source_hash(history_topics.get(topic_key), str(row["hash_identity_from"]), str(topic.get("hash")))
    had_history = topic_key in history_topics
    topic_history_before = copy.deepcopy(history_topics.get(topic_key))
    try:
        reconcile = reconcile_topic(topic, topic_client, history, now=iso_now())
    except Exception as exc:  # noqa: BLE001 - a failed reconcile is the topic's result (recorded), never the run's end
        if had_history:
            history_topics[topic_key] = cast(HistoryRecord, topic_history_before)
        else:
            history_topics.pop(topic_key, None)
        mark_reconcile_failure(row, exc)
        _stamp(topic, row)
        topic["last_error_class"] = str(row["reconcile_error"] or "error")
        run.queue_notification(
            topic,
            kind="error",
            operation_id=new_operation_id("reconcile-error"),
            tracker=str(row.get("tracker") or ""),
            error=_row_error(row),
            error_cls=topic["last_error_class"],
        )
        run.record(
            "reconcile_failed",
            operation_id=new_operation_id("reconcile"),
            component="client/filesystem",
            integration_id=client_id,
            client_id=client_id,
            client_kind=topic_client.client_kind,
            topic_id=topic.get("id"),
            topic=topic.get("id"),
            hash=topic.get("hash"),
            status="failed",
            **error_fields(exc),
            how=run.how,
        )
        return
    for event_kind in reconcile.get("events") or []:
        operation_id = new_operation_id("reconcile")
        pending_records.append(
            (
                event_kind,
                {
                    "operation_id": operation_id,
                    "component": "client/filesystem",
                    "integration_id": client_id,
                    "client_id": client_id,
                    "client_kind": topic_client.client_kind,
                    "topic_id": topic.get("id"),
                    "topic": topic.get("id"),
                    "hash": topic.get("hash"),
                    "status": "succeeded",
                    "how": run.how,
                },
            )
        )
        notification = reconcile_notification(event_kind, reconcile)
        if run.notify and notification:
            notify_kind, episodes = notification
            run.queue_notification(
                topic,
                kind=notify_kind,
                operation_id=operation_id,
                tracker=str(row.get("tracker") or ""),
                episodes=episodes,
            )
    _note_season_complete(topic, row, run, history_topics.get(topic_key), reconcile)
    if checked_ok and (history_topics.get(topic_key) or {}).get("client_present") is False:
        # B4: the torrent was removed from the client; "раздача исчезла" was sent once.
        _fail_row(topic, row, TowError("check.removed_from_client"))


def _note_season_complete(
    topic: Topic, row: dict[str, Any], run: _CheckRun, record: HistoryRecord | None, reconcile: dict[str, Any]
) -> None:
    """G5: "сезон собран N/N" once, when the last expected episode completes in this run.

    A topic that was already complete (e.g. on the first run after an upgrade) is marked
    silently; a season that becomes incomplete again (new episodes announced) is re-armed.
    """
    if not isinstance(record, dict):
        return
    summary = reconcile.get("summary") or {}
    if not (summary.get("is_complete") and summary.get("expected")):
        record.pop("season_complete_at", None)
        return
    if record.get("season_complete_at"):
        return
    record["season_complete_at"] = iso_now()
    events = set(reconcile.get("events") or [])
    if events & {"episode_completed", "file_completed"}:
        run.queue_notification(
            topic,
            kind="season_complete",
            operation_id=new_operation_id("bot"),
            tracker=str(row.get("tracker") or ""),
            episodes=f"{summary['completed']}/{summary['expected']}",
        )


def _queue_recoveries(state: dict[str, Any], results: list[dict[str, Any]], run: _CheckRun) -> None:
    """Close every reported failure that is over: the owner heard "Сбой", now hears it ended."""
    topics_by_id = {str(topic.get("id")): topic for topic in topics_of(state)}
    for row in results:
        topic = topics_by_id.get(str(row.get("id")))
        if topic is None or not row.get("ok") or topic.get("last_error"):
            continue
        topic.pop("error_streak", 0)
        # A check that sends nothing (tow check --apply without --notify) keeps the mark: the
        # next one that does still tells the owner it works again.
        if run.notify and topic.pop("error_notified", False):
            run.queue_notification(
                topic, kind="recovered", operation_id=new_operation_id("bot"), tracker=str(row.get("tracker") or "")
            )


def _remote_clients(cfg: dict[str, Any], secrets: dict[str, Any]) -> frozenset[str]:
    """The configured clients that run on another computer."""
    try:
        rows = client_factory.client_configurations(cfg)
    except TowError, RuntimeError, ValueError:
        return frozenset()
    return frozenset(
        str(row["id"]) for row in rows if not client_factory.on_this_computer(cfg, secrets, str(row["id"]))
    )


def _open_client(cfg: dict[str, Any], secrets: dict[str, Any], client_id: str, *, apply: bool) -> TorrentClientAdapter:
    """The client adapter; for a dry run in its read-only mode where it has one (Deluge does
    not attach its Web UI to a daemon during a preview)."""
    adapter = client_factory.from_secrets(cfg, secrets, client_id)
    if not apply:
        adapter.read_only = True
    return adapter


def _today() -> str:
    """The local date (YYYY-MM-DD) a daily download limit belongs to."""
    return iso_now()[:10]


def _daily_limits(
    limited_today: set[str], quota: set[str], results: list[dict[str, Any]], today: str
) -> dict[str, str]:
    """{site: today} for the sites under their daily download limit after this run.

    A manual check that reached a limited site without hitting the limit ends it early.
    """
    tried = {str(row.get("tracker")) for row in results if row.get("tracker")}
    kept = {name for name in limited_today if name in quota or name not in tried}
    return dict.fromkeys(sorted(kept | quota), today)


def _store_daily_limits(state: dict[str, Any], limits: dict[str, str]) -> None:
    if limits:
        state["daily_limit"] = limits  # yesterday's entries are dropped
    else:
        state.pop("daily_limit", None)


# Checks in a row a site's transport trouble (an amber class) lasts before the owner hears of it.
TRACKER_ERROR_NOTIFY_AFTER = 3


def _streak(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 0


@dataclass
class _RunNotifications:
    """The messages one run collects; an error is reported when it starts or changes class,
    not on every run while it lasts."""

    enabled: bool
    batch: NotificationBatch
    # Error class each topic ended the previous run with ("" = healthy).
    previous_error_class: dict[str, str]
    client_errors: dict[str, Exception]
    default_client_id: str

    def queue(
        self,
        topic: Topic,
        *,
        kind: str,
        operation_id: str,
        tracker: str = "",
        error: Any = "",
        episodes: str = "",
        error_cls: str = "",
    ) -> None:
        if not self.enabled:
            return
        if kind == "error":
            if str(topic.get("client_id") or self.default_client_id) in self.client_errors:
                return  # a single "торрент-клиент недоступен" covers every topic of a dead client
            cls = error_cls or error_class(error)
            if cls in TRACKER_WARNING_CLASSES:
                # A site's transport trouble (amber) often passes by itself: reported once it
                # lasted several checks in a row, not as an error/recovered pair every other run.
                streak = _streak(topic.get("error_streak")) + 1
                topic["error_streak"] = streak
                if streak < TRACKER_ERROR_NOTIFY_AFTER and not topic.get("error_notified"):
                    return
            else:
                topic.pop("error_streak", None)
            if self.previous_error_class.get(str(topic.get("id"))) == cls and topic.get("error_notified"):
                return  # already reported; "снова работает" will close it
            topic["error_notified"] = True
        self.batch.queue(
            topic,
            kind=kind,
            operation_id=operation_id,
            tracker=tracker,
            error=error,
            episodes=episodes,
        )


@dataclass
class _ClientPool:
    """The torrent clients one run talks to: opened on first use, a client that does not answer
    is not asked again in this run (and reported once, like its recovery)."""

    cfg: dict[str, Any]
    apply: bool
    notify: bool
    how: str
    record: Callable[..., None]
    batch: NotificationBatch
    default_id: str
    previous_qbit_ok: Any
    previous_clients_ok: dict[str, Any]
    clients: dict[str, TorrentClientAdapter] = field(default_factory=dict)
    errors: dict[str, Exception] = field(default_factory=dict)
    ok: dict[str, bool] = field(default_factory=dict)
    ping: str = ""
    # Clients that had TOW's torrents at the last check (a topic of theirs was fine then).
    expect_torrents: frozenset[str] = frozenset()

    def _answered(self, client_id: str, client: TorrentClientAdapter) -> str:
        """Ping the client; one that lists no torrent at all, although it had TOW's at the last
        check, is still loading them after a start (qBittorrent answers meanwhile): this run
        must not read its torrents as removed and add them again."""
        answer = client.ping()
        has_any = getattr(client, "has_any_torrent", None)
        if client_id in self.expect_torrents and callable(has_any) and has_any() is False:
            raise TowError("check.client_empty", cls="qbit")
        return answer

    def _client_event(self, client_id: str, kind: str, title: str = "") -> None:
        self.batch.queue(
            {"id": f"__client__:{client_id}", "title": title}, kind=kind, operation_id=new_operation_id("bot")
        )

    def open_default(self, secrets: dict[str, Any]) -> None:
        """Ping the main client; only a client that answered is used (a dead one made every
        topic fail, and notify)."""
        client_id = self.default_id
        try:
            client = _open_client(self.cfg, secrets, client_id, apply=self.apply)
            self.ping = self._answered(client_id, client)
            self.clients[client_id] = client
        except Exception as e:  # noqa: BLE001 - any failure to reach the client is 'the client is down' (recorded)
            self.ping = f"down: {e}"
            self.errors[client_id] = e
            self.record(
                "client_unreachable",
                component="client",
                integration_id=client_id,
                client_kind="unknown",
                **error_fields(e),
                how=self.how,
            )
            if self.notify and self.previous_qbit_ok is not False:
                self._client_event(client_id, "qbit_down")
        else:
            if self.notify and self.previous_qbit_ok is False:
                self._client_event(client_id, "qbit_up")

    def get(self, topic: Topic, secrets: dict[str, Any]) -> tuple[str, TorrentClientAdapter | None]:
        """The topic's client (None when it does not answer this run)."""
        wanted = str(topic.get("client_id") or self.default_id)
        if wanted in self.clients:
            return wanted, self.clients[wanted]
        if wanted in self.errors:
            return wanted, None
        try:
            adapter = _open_client(self.cfg, secrets, wanted, apply=self.apply)
            self._answered(wanted, adapter)  # a second client that is down is reported like the main one
        except Exception as e:  # noqa: BLE001 - any failure to reach the client is 'the client is down' (recorded)
            self.errors[wanted] = e
            self.ok[wanted] = False
            self.record(
                "client_unreachable", component="client", integration_id=wanted, **error_fields(e), how=self.how
            )
            if self.notify and self.previous_clients_ok.get(wanted) is not False:
                self._client_event(wanted, "qbit_down", _client_title(self.cfg, wanted))
            return wanted, None
        self.clients[wanted] = adapter
        self.ok[wanted] = True
        return wanted, adapter

    def queue_recovered(self) -> None:
        """A second client that was down in the previous run answered again."""
        for client_id, ok in self.ok.items():
            if client_id != self.default_id and ok and self.previous_clients_ok.get(client_id) is False and self.notify:
                self._client_event(client_id, "qbit_up", _client_title(self.cfg, client_id))

    @property
    def default_ok(self) -> bool:
        return not str(self.ping).startswith("down")

    def health(self) -> dict[str, bool]:
        """Every client's state for the header: the earlier ones, then what this run saw."""
        return {**self.previous_clients_ok, self.default_id: self.default_ok, **self.ok}


def _load_run_state(*, apply: bool) -> dict[str, Any]:
    """The state a run works on: an apply first finishes an interrupted commit; a dry run
    works on a copy (it never writes)."""
    if apply:
        with persistence_lock():
            check_transaction.recover_check_transaction()
            return load_state(quarantine=True)
    return copy.deepcopy(load_state(quarantine=False))


def _flush_notifications(cfg: dict[str, Any], secrets: dict[str, Any], *, how: str, notify: bool) -> None:
    """G4: grouped per tracker, with links, held during quiet hours, plus the digest. The
    messages were staged in the commit; here they are handed to the messengers."""
    from tow import delivery

    def send(*, text: str, operation_id: str, topic: Topic | None) -> bool:
        return _audited_send(secrets, text=text, operation_id=operation_id, topic=topic, how=how)

    delivery.dispatch(send)
    if notify:
        delivery.maybe_digest(cfg=cfg, send=send)


@dataclass
class _RunResult:
    """What a run hands to its commit."""

    state: dict[str, Any]
    results: list[CheckRow]
    started: dict[str, tuple[Any, ...]]  # owner fields per topic when the run started
    pool: _ClientPool
    notifications: NotificationBatch
    cfg: dict[str, Any]
    how: str
    limited_today: set[str]
    quota: set[str]
    today: str


def _commit_run_state(result: _RunResult, history_rebuilt: bool = False) -> None:
    """Write the run's result onto the state as it is on disk now (the owner's edits made
    meanwhile are kept), with the health record and the staged messages, in one write."""
    state, pool, how = result.state, result.pool, result.how
    health = Health(
        qbit=pool.ping,
        client=pool.ping,
        qbit_ok=pool.default_ok,
        at=_now(),
        at_ts=int(machine_now().timestamp()),
    )
    state["health"] = health
    # The header countdown runs from the last *scheduled* check; a manual check of
    # one topic must not move it.
    previous_health = health_of(load_state(quarantine=False))
    _note_history_rebuilt(health, previous_health, rebuilt=history_rebuilt)
    health["auto_at_ts"] = health["at_ts"] if how == "auto" else previous_health.get("auto_at_ts")
    health["auto_ok_at_ts"] = health["at_ts"] if how == "auto" else _last_good_scheduled_check(previous_health)
    health["clients_ok"] = pool.health()
    disk = load_state()
    edited = {
        str(topic.get("id")): changed
        for topic in topics_of(disk)
        if (changed := changed_owner_fields(result.started.get(str(topic.get("id"))), topic))
    }
    merge_check_results(disk, state, edited=edited)
    disk["health"] = health
    _store_daily_limits(disk, _daily_limits(result.limited_today, result.quota, result.results, result.today))
    # Staged in the same write as the result: a TOW stopped before sending loses nothing.
    from tow import delivery

    delivery.stage(disk, list(result.notifications), cfg=result.cfg)
    save_state(disk)


def _run_check(
    *,
    apply: bool,
    notify: bool,
    ids: list[str] | None = None,
    ignore_cool: bool = False,
    how: str = "auto",
    progress_only: bool = False,
    scheduled_scope: str = "",
) -> dict[str, Any]:
    cfg = load_config()
    secrets_stamp = file_stamp(encrypted_secrets_path())
    secrets = load_secrets()
    state = _load_run_state(apply=apply)
    started = {str(topic.get("id")): owner_fields(topic) for topic in topics_of(state)}
    notify = bool(notify and apply)
    trackers = load_trackers(cfg)
    ua = cfg.get("user_agent")
    batch = NotificationBatch()
    pending_reconcile_records: list[tuple[str, dict[str, Any]]] = []
    previous_health = health_of(state)

    def _record(kind: str, **fields: Any) -> None:
        if apply:
            log_event(kind, **fields)

    default_id = client_factory.default_client_id(cfg)
    pool = _ClientPool(
        cfg=cfg,
        apply=apply,
        notify=notify,
        how=how,
        record=_record,
        batch=batch,
        default_id=default_id,
        previous_qbit_ok=previous_health.get("qbit_ok"),
        previous_clients_ok=dict(previous_health.get("clients_ok") or {}),
        expect_torrents=frozenset(
            str(topic.get("client_id") or default_id)
            for topic in topics_of(state)
            if topic.get("hash") and topic.get("last_ok") is True and not topic.get("paused")
        ),
    )
    notifications = _RunNotifications(
        enabled=notify,
        batch=batch,
        previous_error_class={
            str(topic.get("id")): (str(topic.get("last_error_class") or "error") if topic.get("last_error") else "")
            for topic in topics_of(state)
        },
        client_errors=pool.errors,
        default_client_id=pool.default_id,
    )
    pool.open_default(secrets)
    want = {str(x) for x in ids} if ids is not None else None
    if scheduled_scope:
        from tow.supervisor import layout
        from tow.topic_timers import batch_ids, interval_of

        selected = (
            set(batch_ids(state, layout.read_json(layout.schedule_path())))
            if scheduled_scope == "timer"
            else {str(topic.get("id")) for topic in topics_of(state) if interval_of(topic) is None}
        )
        want = selected if want is None else want & selected
    # A site that said "download limit for today" is left alone until the next local day: the
    # limit is kept in the state, so the next scheduled runs do not ask it again. A manual
    # check (the owner pressed the button) still tries.
    today = _today()
    limited_today = {str(name) for name, day in as_dict(state.get("daily_limit")).items() if day == today}
    quota: set[str] = set(limited_today) if how in {"auto", "timer"} else set()
    run = _CheckRun(
        apply=apply,
        notify=notify,
        how=how,
        ignore_cool=ignore_cool,
        ua=ua,
        state=state,
        trackers=trackers,
        quota=quota,
        secrets=secrets,
        client_errors=pool.errors,
        get_client_for=lambda topic: pool.get(topic, run.secrets),
        record=_record,
        queue_notification=notifications.queue,
        history_retention=download_history.retention(cfg),
        remote_clients=_remote_clients(cfg, secrets),
        secrets_stamp=secrets_stamp,
    )
    results: list[CheckRow] = []
    for topic in topics_of(state):
        if want is not None and str(topic.get("id")) not in want:
            continue
        # G2: a progress-only pass asks qBittorrent alone - no tracker traffic at all.
        results.append(_progress_only_row(topic) if progress_only else _check_topic(topic, run))
    secrets = run.secrets  # the per-topic step may have re-read tracker cookies
    pool.queue_recovered()
    if not apply:
        history, _rebuilt = _load_history(quarantine=False)
        _reconcile_all(state, results, run, history, pending_reconcile_records, want)
        _queue_recoveries(state, results, run)
        return {"qbit": pool.ping, "results": results, "preview": True}

    outcome = _RunResult(state, results, started, pool, batch, cfg, how, limited_today, quota, today)
    cleanup_pending = _reconcile_and_commit(
        state,
        results,
        run,
        pending_reconcile_records,
        want,
        lambda rebuilt: _commit_run_state(outcome, rebuilt),
    )
    for event_kind, fields in pending_reconcile_records:
        _record(event_kind, **fields)
    _flush_notifications(cfg, secrets, how=how, notify=notify)
    if cleanup_pending:
        _record("check_persistence_cleanup_pending", how=how)
    _record("check", apply=apply, ok=sum(1 for r in results if r.get("ok")), n=len(results), how=how)
    return {"qbit": pool.ping, "results": results}
