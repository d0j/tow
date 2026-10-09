"""What the web pages call outside the web package: the stores and their transactions, the check,
the clients and sites, the service lifecycle, copies of the data, diagnostics, the sessions, the
browser sign-in, the messengers and the event log.

This is the web layer's one seam. Route modules and view helpers call these through this
module - ``services.load_config()``, ``services.run_check(...)`` - never by importing them from
their home modules, so a test replaces one in one place and every page sees it::

    monkeypatch.setattr("tow.web.services.run_check", fake_check)

Pure helpers (formatting, parsing, validation, constants, error types) are imported from their
own modules as usual; anything that reads or writes the install's data, talks to a client or a
site, or changes the service belongs here. ``tests/test_web_boundary.py`` lists the pure helpers
the route and action modules import and refuses any other way out of ``tow.web``.
"""

from __future__ import annotations

from collections.abc import Callable
from functools import wraps
from threading import BoundedSemaphore
from typing import Any

from tow.access import credential as network_credential
from tow.access import set_session_cookie, sign_out, sign_out_everywhere
from tow.adopt import adopt_topic
from tow.browser_auth import browser_auth
from tow.check import await_relocation, client_owned_by_tow, record_check_failure, run_check
from tow.check.notices import notify_topic_error
from tow.check.space import folder_free
from tow.clients.factory import from_secrets as client_from_secrets
from tow.config import load_config
from tow.config import save_config as _save_config
from tow.content import metadata as content_metadata
from tow.content import read as read_content
from tow.doctor import doctor_report, stale_autostart
from tow.doctor import reason_text as doctor_reason
from tow.guess import guess_site
from tow.lifecycle import request_restart, service_status, set_autostart
from tow.locations import check_writable as folder_write_problem
from tow.locations import free_bytes
from tow.log import history_events, log_event, read_events
from tow.mirrors import prefer_host as _prefer_host
from tow.notifiers import test as test_notifier
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
from tow.snapshots import (
    check_snapshot,
    create_snapshot,
    delete_snapshot,
    restore_snapshot,
    snapshot_delete_view,
    snapshot_path,
)
from tow.snapshots import cleanup_status as night_cleanup_status
from tow.store import (
    StoreWriteError,
    load_download_history,
    load_secrets,
    load_state,
    persistence_lock,
)
from tow.store import save_secrets as _save_secrets
from tow.store import save_state as _save_state
from tow.store_transaction import commit as commit_stores
from tow.store_transaction import recover as recover_store_transaction
from tow.store_transaction import transaction as store_transaction
from tow.supervisor import data_lost
from tow.supervisor.layout import install_id, next_check_at, topic_timer_status
from tow.title import guess_topic_title
from tow.undo import apply as apply_undo
from tow.undo import cleanup as cleanup_secret_undo
from tow.undo import stamped_here as undo_stamped_here
from tow.watchdog import last_problem as watchdog_problem
from tow.web_update import log_tail as web_update_log
from tow.web_update import start as start_web_update
from tow.web_update import status as web_update_status

__all__ = [
    "adopt_topic",
    "apply_undo",
    "await_relocation",
    "browser_auth",
    "check_portable_bundle",
    "check_restore_point",
    "check_snapshot",
    "cleanup_secret_undo",
    "client_answers",
    "client_from_secrets",
    "client_owned_by_tow",
    "commit_stores",
    "content_context_title",
    "content_metadata",
    "create_restore_point",
    "create_snapshot",
    "data_lost",
    "delete_restore_point",
    "delete_snapshot",
    "doctor_reason",
    "doctor_report",
    "export_portable_bundle",
    "folder_free",
    "folder_write_problem",
    "free_bytes",
    "guess_site",
    "guess_topic_title",
    "history_events",
    "install_id",
    "list_restore_points",
    "load_config",
    "load_download_history",
    "load_secrets",
    "load_state",
    "locked_state_mutation",
    "log_event",
    "login_throttle",
    "network_credential",
    "next_check_at",
    "night_cleanup_status",
    "notify_topic_error",
    "persistence_lock",
    "prefer_host",
    "prepare_content",
    "prepare_magnet_content",
    "read_content",
    "read_events",
    "record_check_failure",
    "recover_store_transaction",
    "release_status",
    "request_restart",
    "restore_from_point",
    "restore_point_cleanup_status",
    "restore_point_delete_view",
    "restore_portable_bundle",
    "restore_snapshot",
    "run_check",
    "save_config",
    "save_secrets",
    "save_state",
    "service_status",
    "set_autostart",
    "set_session_cookie",
    "sign_out",
    "sign_out_everywhere",
    "snapshot_delete_view",
    "snapshot_path",
    "stale_autostart",
    "start_web_update",
    "store_transaction",
    "test_notifier",
    "topic_timer_status",
    "undo_stamped_here",
    "watchdog_problem",
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


def _user_agent(cfg: dict[str, Any]) -> str | None:
    """config.yaml's ``user_agent``; None lets the HTTP client send TOW's browser string, as a check does."""
    value = cfg.get("user_agent")
    return value if isinstance(value, str) and value.strip() else None


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
        # The User-Agent of a check: the configured one, else TOW's browser string (None).
        blob = tracker.fetch_torrent(url, load_secrets(), _user_agent(cfg), persist=True)
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
        magnet, identity = tracker.fetch_magnet(url, secrets, _user_agent(cfg), persist=True)
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


def client_answers(client_id: str | None, timeout: float = 3.0) -> bool:
    """Whether anything accepts a connection at the client's saved address, one try within
    ``timeout``: the manual "Check" fails fast instead of waiting out the client library's
    retries (qBittorrent: about 24 s on Windows when nothing listens). True when there is no
    address to try - the client's own check then says what is missing.

    This try is the whole fast path; the client adapters get no "check" mode of their own. A
    shorter connect timeout for qBittorrent's library would have nothing left to shorten once
    this connect succeeded, and its retries cannot be switched off through its public options:
    the request manager always sends a failed request twice (that is how it follows a Web UI
    moved between HTTP and HTTPS; the count is the private ``_retries``). What remains slow is
    a program that accepts the connection and never answers, bounded by the 8 s read timeout
    of each try."""
    import socket
    from urllib.parse import urlparse

    from tow.clients.factory import client_configuration, client_secret_block
    from tow.clients.spec import get as client_spec
    from tow.clients.transmission import base_url

    cfg = load_config()
    block = client_secret_block(cfg, load_secrets(), client_id)
    host = str(block.get("host") or "").strip()
    spec = client_spec(str(client_configuration(cfg, client_id).get("kind") or "qbittorrent").lower())
    if not host or spec is None:
        return True
    try:
        address = urlparse(base_url(host, int(block.get("port") or spec.default_port)))
        name, port = address.hostname, address.port or (443 if address.scheme == "https" else 80)
    except ValueError:
        return True
    if not name:
        return True
    try:
        with socket.create_connection((name, port), timeout=timeout):
            return True
    except OSError:
        return False


def locked_state_mutation[**P, R](function: Callable[P, R]) -> Callable[P, R]:
    """A route that reads, changes and writes the stores holds the persistence lock throughout,
    so a check finishing meanwhile cannot be lost (or lose the owner's change)."""

    @wraps(function)
    def wrapped(*args: P.args, **kwargs: P.kwargs) -> R:
        with persistence_lock():
            return function(*args, **kwargs)

    return wrapped


def _single_file_write[**P, R](function: Callable[P, R]) -> Callable[P, R]:
    """A save of one store (no journal) that fails with an OSError - another program keeps the
    file open longer than the replace retries, the disk is full - raises ``StoreWriteError``:
    the app answers it with a message to try again (``tow.web.app``), never a server error. An
    ``except OSError`` around the call still catches it."""

    @wraps(function)
    def wrapped(*args: P.args, **kwargs: P.kwargs) -> R:
        try:
            return function(*args, **kwargs)
        except StoreWriteError:
            raise
        except OSError as exc:
            raise StoreWriteError(exc.errno, exc.strerror or type(exc).__name__) from exc

    return wrapped


save_state = _single_file_write(_save_state)
save_config = _single_file_write(_save_config)
save_secrets = _single_file_write(_save_secrets)
prefer_host = _single_file_write(_prefer_host)
