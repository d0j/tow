"""One topic's check: fetch its current revision from the tracker, identify it, choose its
files and bring its torrent client in line with it, confirmed by read-back."""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, NamedTuple

from tow import download_history
from tow.check import space
from tow.check.apply import apply_revision
from tow.check.client_ops import confirm_client_add, magnet_matches_saved_hash
from tow.check.rows import set_error, stamp_result
from tow.check_steps import resolve_client_hash, store_file_aliases, verify_magnet_metadata
from tow.clients.spec import TorrentClientAdapter
from tow.episodes import parse_season_hint
from tow.errors import TowError
from tow.events import new_operation_id
from tow.i18n import t
from tow.log import error_class, error_fields, is_daily_limit, log_event, owner_language
from tow.records import CheckRow, Topic, mirror_of
from tow.selection import SelectionPendingError, policy_from_topic, resolve_selection
from tow.store import (
    FileStamp,
    StoreCorruptionError,
    encrypted_secrets_path,
    file_stamp,
    load_secrets,
    load_state,
)
from tow.title import title_is_placeholder as _title_placeholder
from tow.torrent import TorrentPathConflictError, parse_torrent_metadata
from tow.trackers import GenericHttpTracker, match_tracker, presets

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
        set_error(row, error)
        _fail_log(topic, url, error, how=how, persist=apply)
        stamp_result(topic, row)
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
    set_error(row, error)
    _fail_log(topic, url, error, tr, how=how, persist=apply)
    stamp_result(topic, row)
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


def _client_marks(topic: Topic) -> tuple[str, ...]:
    """What the client is changed with: the link (which torrent), the folder, the client and
    the file selection."""
    return tuple(repr(topic.get(key)) for key in ("url", "save_path", "client_id", "selection"))


def _withdrawn_meanwhile(topic: Topic, row: dict[str, Any], old: str, started: tuple[str, ...] | None) -> bool:
    """The owner paused or deleted the topic, or changed its link, folder, client or file selection,
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


@dataclass
class CheckRun:
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
    # The owner checks these topics by hand (a row's check): a current revision gone from its
    # client is added again. A scheduled check only reports it - the owner may have removed it.
    readd_removed: bool = False


def read_secrets() -> tuple[dict[str, Any], FileStamp | None]:
    """The secrets as the run starts, and the stamp of the secrets.enc they came from."""
    secrets_stamp = file_stamp(encrypted_secrets_path())
    return load_secrets(), secrets_stamp


def _reread_secrets(run: CheckRun) -> None:
    """Pick up what a login in this run persisted (new tracker cookies) before the next request.
    The file is decrypted again only when it changed: twice for every topic, it was decrypted
    4000 times in a check of 2000 topics."""
    stamp = file_stamp(encrypted_secrets_path())
    if stamp is None or stamp != run.secrets_stamp:
        run.secrets = load_secrets()
        run.secrets_stamp = stamp


def _confirm_matching_magnet(
    topic: Topic,
    run: CheckRun,
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
    if not magnet_matches_saved_hash(magnet_url, old, topic_client):
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
        if topic_client is None or not confirm_client_add(topic_client, old, str(topic.get("save_path") or "")):
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
class TopicCheck:
    """One topic's check in progress: what its steps (fetch, identify, apply, record) share."""

    topic: Topic
    run: CheckRun
    tracker: GenericHttpTracker
    url: str
    row: CheckRow
    old: str  # the revision the topic had (its hash), "" for a new topic
    client_id: str
    client: TorrentClientAdapter | None  # None: the client did not answer this run
    operation_id: str | None = None  # set once a client operation has started
    started: tuple[str, ...] | None = None  # the topic's _client_marks when its check began


class Fetched(NamedTuple):
    blob: bytes
    magnet_hash: str | None  # the .torrent was built from the tracker's magnet (its hash)


def check_topic(topic: Topic, run: CheckRun) -> CheckRow:
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
    work = TopicCheck(topic, run, tr, url, row, old, client_id, topic_client, started=started)
    try:
        if not _waits_for_space(work):
            _check_revision(work)
    except TorrentPathConflictError:
        _topic_failed(work, TowError("content.path_conflict"))
    except Exception as error:  # noqa: BLE001 - any failure of one topic is its result, never the run's end
        _topic_failed(work, error)
    stamp_result(topic, row)
    return row


def _waits_for_space(work: TopicCheck) -> bool:
    """The topic's revision waits in the client for disk space (``tow.check.space``): only the
    client is asked - the site's .torrent is not downloaded again, nothing is added again.
    False: the check goes on as always (it was started now, or the waiting ended; a new file
    selection of the owner is applied by the check, which starts it when it fits)."""
    topic = work.topic
    if not topic.get("waiting_space") or topic.get("selection_dirty"):
        return False
    return space.recheck(topic, work.run, work.row, work.client, work.client_id) == space.WAITING


def _check_revision(work: TopicCheck) -> None:
    """Fetch the topic's current revision, identify it and bring the client in line with it."""
    topic, run, row, old = work.topic, work.run, work.row, work.old
    policy = policy_from_topic(topic)
    fetched = _fetch_revision(work, policy)
    row["site_answered"] = True  # whatever the client says next, the site gave the revision
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
    readd = _readds_removed(work, h=h, needs_selection_update=needs_selection_update)
    if run.apply and (h != old or needs_selection_update or readd):
        if _withdrawn_meanwhile(topic, row, old, work.started):
            return
        apply_revision(
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
        if client is None or not confirm_client_add(client, h, str(topic.get("save_path") or "")):
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


def _readds_removed(work: TopicCheck, *, h: str, needs_selection_update: bool) -> bool:
    """A check by hand finds the topic's current revision gone from its client: it is added
    again the normal way (stopped, its files chosen, read back, then started - or left waiting
    for disk space). Only the client is asked; a scheduled check leaves it reported."""
    if not (work.run.apply and work.run.readd_removed and work.old and h == work.old):
        return False
    if needs_selection_update or work.client is None:
        return False  # the selection update adds it anyway; a client that did not answer is reported
    return work.client.inspect_torrent(h) is None


def _fetch_revision(work: TopicCheck, policy: dict[str, Any]) -> Fetched | None:
    """The topic's .torrent; None when the tracker's magnet confirmed the saved revision."""
    run = work.run
    token = work.topic.get("content_token")
    if not work.old and token:
        from tow.content import site_revision

        try:
            prepared = site_revision(str(token), work.url, work.client_id)
            if prepared is not None:
                return Fetched(prepared, None)
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
    return Fetched(blob, None)


def _fetch_by_magnet(work: TopicCheck, policy: dict[str, Any], torrent_error: Exception) -> Fetched | None:
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
    try:
        magnet_url, magnet_hash = fetch_magnet(
            work.url,
            run.secrets,
            run.ua,
            ignore_cool=run.ignore_cool,
            persist=run.apply,
        )
    except Exception as magnet_error:  # the fallback failed too: the site's own answer stands
        # "No valid magnet link" hid why the .torrent was refused (a sign-in page, not a
        # torrent): the row keeps that reason; the magnet attempt is its cause and in the log.
        _LOG.warning("magnet fallback failed for %s: %s", tr.name, type(magnet_error).__name__)
        raise torrent_error from magnet_error
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
    return Fetched(blob, magnet_hash)


def _observe_cached_magnet(url: str, magnet: str, topic: Topic) -> None:
    from tow import torrent_cache

    try:
        torrent_cache.observe_magnet(url, magnet)
    except (TowError, OSError, ValueError, RuntimeError) as exc:
        log_event("content_cache_failed", topic=topic.get("id"), code=getattr(exc, "code", "content.unavailable"))


def _plan_selection(work: TopicCheck, metadata: Any, policy: dict[str, Any]) -> Any | None:
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


def _record_found(work: TopicCheck, metadata: Any, plan: Any, *, h: str, migrates: bool) -> None:
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


def _topic_failed(work: TopicCheck, error: Exception) -> None:
    """The topic's check failed: the row says why, the log and (once) the owner hear of it."""
    topic, run, row, tr = work.topic, work.run, work.row, work.tracker
    row["ok"] = False
    set_error(row, error)
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
