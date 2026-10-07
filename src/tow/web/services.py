"""What the web pages call outside the web package: the stores, the check, the service lifecycle,
restore points and portable bundles, diagnostics, the browser sign-in and the event log.

This is the web layer's one seam. Route modules and view helpers call these through this
module - ``services.load_config()``, ``services.run_check(...)`` - never by importing them from
their home modules, so a test replaces one in one place and every page sees it::

    monkeypatch.setattr("tow.web.services.run_check", fake_check)

Pure helpers (formatting, parsing, validation) are imported from their own modules as usual;
anything that reads or writes the install's data, talks to a client or a site, or changes the
service belongs here.
"""

from __future__ import annotations

from collections.abc import Callable
from functools import wraps
from threading import BoundedSemaphore
from typing import Any

from tow.browser_auth import browser_auth
from tow.check import record_check_failure, run_check
from tow.config import load_config, save_config
from tow.content import read as read_content
from tow.doctor import doctor_report
from tow.lifecycle import request_restart, service_status, set_autostart
from tow.log import log_event
from tow.ratelimit import LoginThrottle
from tow.releases import release_status
from tow.restore_points import (
    check_portable_bundle,
    check_restore_point,
    create_restore_point,
    delete_restore_point,
    export_portable_bundle,
    list_restore_points,
    restore_from_point,
    restore_point_delete_view,
    restore_portable_bundle,
)
from tow.restore_points import cleanup_status as restore_point_cleanup_status
from tow.snapshots import check_snapshot, delete_snapshot, snapshot_delete_view
from tow.snapshots import cleanup_status as night_cleanup_status
from tow.store import (
    load_download_history,
    load_secrets,
    load_state,
    persistence_lock,
    save_secrets,
    save_state,
)
from tow.store_transaction import recover as recover_store_transaction
from tow.supervisor.layout import install_id, next_check_at, topic_timer_status
from tow.undo import cleanup as cleanup_secret_undo
from tow.web_update import log_tail as web_update_log
from tow.web_update import start as start_web_update
from tow.web_update import status as web_update_status

__all__ = [
    "browser_auth",
    "check_portable_bundle",
    "check_restore_point",
    "check_snapshot",
    "cleanup_secret_undo",
    "content_context_title",
    "create_restore_point",
    "delete_restore_point",
    "delete_snapshot",
    "doctor_report",
    "export_portable_bundle",
    "install_id",
    "list_restore_points",
    "load_config",
    "load_download_history",
    "load_secrets",
    "load_state",
    "locked_state_mutation",
    "log_event",
    "login_throttle",
    "next_check_at",
    "night_cleanup_status",
    "persistence_lock",
    "prepare_content",
    "prepare_magnet_content",
    "read_content",
    "record_check_failure",
    "recover_store_transaction",
    "release_status",
    "request_restart",
    "restore_from_point",
    "restore_point_cleanup_status",
    "restore_point_delete_view",
    "restore_portable_bundle",
    "run_check",
    "save_config",
    "save_secrets",
    "save_state",
    "service_status",
    "set_autostart",
    "snapshot_delete_view",
    "start_web_update",
    "topic_timer_status",
    "web_update_log",
    "web_update_status",
]

# Failed sign-ins and password checks, per address and in total: one budget for the sign-in
# page and the password card (a test gets a fresh one).
login_throttle = LoginThrottle()
# A closed/edited browser form cannot cancel a running native RPC. Bound active workers
# independently of browser buttons; cache writes still hold only their own short lock.
_MAGNET_PREVIEWS = BoundedSemaphore(2)


def content_context_title(topic_id: str, url: str, client_id: str, title: str) -> str:
    """Use saved tracker context only for the same topic source and client.

    A new or changed source uses the form title. This is a read-only preview;
    the applying check still obtains the current tracker title independently.
    """
    from tow.errors import TowError
    from tow.guess import canon_watch_url

    if not topic_id:
        return title
    topic = next(
        (item for item in load_state(quarantine=False).get("topics", []) if str(item.get("id")) == topic_id), None
    )
    if topic is None:
        raise TowError("content.changed")
    if canon_watch_url(str(topic.get("url") or "")) != url or (topic.get("client_id") or client_id) != client_id:
        return title
    return str(topic.get("tracker_title") or title.strip() or topic.get("title") or "")


def _content_source(url: str, client_id: str) -> tuple[dict[str, Any], str, str, Any]:
    """The configuration, canonical link, enabled client id and site of a preparation."""
    from tow.clients.factory import client_configuration
    from tow.errors import TowError
    from tow.guess import canon_watch_url
    from tow.trackers import load_trackers, match_tracker

    cfg = load_config()
    url = canon_watch_url(url.strip())
    client = client_configuration(cfg, client_id or None)
    if not client.get("enabled", True):
        raise TowError("web.topics.client_disabled")
    tracker = match_tracker(load_trackers(cfg), url)
    if tracker is None:
        raise TowError("check.no_tracker")
    return cfg, url, str(client["id"]), tracker


def prepare_content(
    url: str, client_id: str, blob: bytes | None, allow_limited: bool, *, fresh: bool = False
) -> dict[str, object]:
    from tow import content, torrent_cache
    from tow.errors import TowError
    from tow.trackers import presets

    cfg, url, client_id, tracker = _content_source(url, client_id)
    local = blob is not None
    cached = False
    if blob is None and not fresh and not allow_limited:
        blob = torrent_cache.read(url)
        cached = blob is not None
    if blob is None:
        limited = tracker.spec.get("download_limit", presets.daily_limited(tracker.name))
        if limited and not allow_limited:
            raise TowError("content.limited")
        # An explicit preparation is a normal tracker action, not dry-run: sign-in and
        # mirror state must work just as they do during a normal check.
        blob = tracker.fetch_torrent(url, load_secrets(), str(cfg.get("user_agent") or "TOW"), persist=True)
    # A local file is not evidence of the topic: it only previews contents, is never saved as
    # the topic's metadata and never becomes its revision (content.site_revision).
    result = content.prepare(blob, url, client_id, from_site=not local)
    result["cached"] = cached
    if not cached and not local:
        try:
            torrent_cache.remember(blob, url)
        except TowError, OSError, ValueError, RuntimeError:
            result["cache_failed"] = True
    return result


def prepare_fresh_content(url: str, client_id: str, allow_limited: bool) -> dict[str, object]:
    return prepare_content(url, client_id, None, allow_limited, fresh=True)


def forget_cached_content(url: str) -> None:
    from tow import torrent_cache

    torrent_cache.forget_if_unused(url)


def prepare_magnet_content(url: str, client_id: str) -> dict[str, object]:
    """Explicit peer-metadata action, separate from tracker downloads and check/dry-run.

    Do not call materialize_magnet here: some adapters implement it by adding a task.
    Only a declared native preview operation is eligible.
    """
    from tow import content
    from tow.clients.factory import from_secrets
    from tow.config import as_bool
    from tow.errors import TowError
    from tow.net_guard import tracker_address
    from tow.torrent import parse_magnet_hashes, parse_torrent_metadata, preview_magnet

    cfg, url, client_id, tracker = _content_source(url, client_id)
    secrets = load_secrets()
    adapter = from_secrets(cfg, secrets, client_id)
    if not adapter.capabilities.get("metadata_preview", False):
        raise TowError("content.magnet_unsupported")
    if not _MAGNET_PREVIEWS.acquire(blocking=False):
        raise TowError("content.magnet_busy")
    try:
        magnet, identity = tracker.fetch_magnet(url, secrets, str(cfg.get("user_agent") or "TOW"), persist=True)
        hashes = parse_magnet_hashes(magnet)
        if hashes is None or identity.upper() not in hashes[0] | hashes[1]:
            raise TowError("content.magnet_failed")
        # The page's link reaches the client only as its hashes and public trackers: a tr= or
        # x.pe= pointing into the home network would make the client knock there for us.
        public_only = not as_bool(cfg.get("allow_private_tracker_hosts"))
        preview = preview_magnet(magnet, lambda tracker_url: tracker_address(tracker_url, public_only=public_only))
        if preview is None:
            raise TowError("content.magnet_failed")
        blob = adapter.preview_magnet(preview)
        metadata = parse_torrent_metadata(blob)
        btih, btmh = hashes
        if (btih and metadata.hash_v1 not in btih) or (btmh and metadata.hash_v2 not in btmh):
            raise TowError("content.magnet_failed")
        from tow import torrent_cache

        result = content.prepare(blob, url, client_id, from_site=True)
        try:
            torrent_cache.remember(blob, url)
        except TowError, OSError, ValueError, RuntimeError:
            result["cache_failed"] = True
        return result
    except TowError:
        raise
    except Exception as exc:
        raise TowError("content.magnet_failed") from exc
    finally:
        _MAGNET_PREVIEWS.release()


def locked_state_mutation[**P, R](function: Callable[P, R]) -> Callable[P, R]:
    """A route that reads, changes and writes the stores holds the persistence lock throughout,
    so a check finishing meanwhile cannot be lost (or lose the owner's change)."""

    @wraps(function)
    def wrapped(*args: P.args, **kwargs: P.kwargs) -> R:
        with persistence_lock():
            return function(*args, **kwargs)

    return wrapped
