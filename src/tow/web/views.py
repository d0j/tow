"""Shared view helpers: messages after an action (flash) and redirects, the add form's draft, the
topic rows of Home with their status and attention lines, and the messages of a manual check."""

from __future__ import annotations

import re
import secrets
import threading
import time
from collections.abc import Mapping
from typing import Any
from urllib.parse import quote, unquote

from fastapi import Request
from fastapi.responses import RedirectResponse

from tow import i18n
from tow.check import blocked_by_previous_revision
from tow.clock import format_ui_timestamp
from tow.config import flash_ttl, interval_sec_of
from tow.episodes import expected_for_topic
from tow.errors import Msg, TowError, render_stored
from tow.log import error_class, owner_language
from tow.net_errors import humanize
from tow.progress import progress_summary
from tow.records import CheckRow
from tow.status import TRACKER_WARNING_CLASSES, project_home_status
from tow.trackers import GenericHttpTracker, load_trackers, match_tracker
from tow.web import _context, services
from tow.web.text import format_bytes, t


def flash_ttl_sec() -> int:
    try:
        n = int(_context.config().get("flash_ttl_sec") or 60)
    except TypeError, ValueError:
        n = 60
    return flash_ttl(n)


class _TokenStore:
    """Short-lived records a redirect points at by a random token (``?add=<token>``,
    ``?flash=<token>``). The next page shows only what this server stored: a crafted link can
    neither plant text on a TOW page nor a link or folder in the owner's form. Bounded: the
    oldest record goes first; a record lives ``ttl`` seconds."""

    def __init__(self, ttl: float, limit: int) -> None:
        self.ttl = ttl
        self.limit = limit
        self._records: dict[str, tuple[float, dict[str, str]]] = {}
        self._lock = threading.Lock()

    def put(self, record: dict[str, str]) -> str:
        token = secrets.token_urlsafe(16)
        now = time.monotonic()
        with self._lock:
            for old in [key for key, (at, _) in self._records.items() if now - at > self.ttl]:
                del self._records[old]
            while len(self._records) >= self.limit:
                del self._records[min(self._records, key=lambda key: self._records[key][0])]
            self._records[token] = (now, dict(record))
        return token

    def get(self, token: str) -> dict[str, str] | None:
        with self._lock:
            entry = self._records.get(token)
        if entry is None or time.monotonic() - entry[0] > self.ttl:
            return None
        return dict(entry[1])

    def clear(self) -> None:
        with self._lock:
            self._records.clear()


# A refused add's draft and reason (D2/H2); a message shown once after an action (typed flash).
# Both live in the memory of the web process only, on purpose: they matter for the next page,
# a few seconds after the action. A restart of the web server (an update, the supervisor bringing
# it back) forgets them - the page after it simply shows no message and an empty add form, the
# action itself is saved or refused as before. Nothing of the owner's input touches the disk.
_ADD_DRAFTS = _TokenStore(ttl=600, limit=64)
_FLASHES = _TokenStore(ttl=600, limit=256)
FLASH_KINDS = ("ok", "warn", "err")


def request_flash(request: Request) -> dict[str, str] | None:
    """The message a redirect left for this page: ``{"text", "kind"}``; None for no, an unknown
    or an expired token - and for a ``?flash=<text>`` link of an older TOW (never shown)."""
    token = request.query_params.get("flash") or ""
    return _FLASHES.get(token) if token else None


def flash_text(message: Any, /, **params: Any) -> str:
    """``message`` as text in the page's language: a catalog key (filled with ``params``), a
    ``Msg`` or a typed error, or a text already composed from catalog texts."""
    if isinstance(message, (Msg, TowError)):
        return message.text()
    text = str(message or "")
    return t(text, **params) if text and i18n.has(text) else text


def flash_location(url: str, message: Any, kind: str = "ok", /, **params: Any) -> str:
    """``url`` with ``flash=<token>`` of a message kept on the server (see ``flash_redirect``)."""
    text = flash_text(message, **params)
    if not text:
        return url
    token = _FLASHES.put({"text": text, "kind": kind if kind in FLASH_KINDS else "ok"})
    path, hash_mark, fragment = url.partition("#")
    joined = f"{path}{'&' if '?' in path else '?'}flash={token}"
    return joined + hash_mark + fragment


def flash_redirect(url: str, message: Any, kind: str = "ok", /, **params: Any) -> RedirectResponse:
    """A 303 to ``url`` that shows ``message`` once, as ``kind``: ``ok`` (done; it fades),
    ``warn`` (done in part, or wait and retry; it stays) or ``err`` (nothing done; an alert that
    stays). The kind comes from the route, never from the words."""
    decoded = unquote(url)
    if (
        not decoded.startswith("/")
        or decoded.startswith("//")
        or "\\" in decoded
        or any(ord(ch) < 32 for ch in decoded)
    ):
        url = "/"
    return RedirectResponse(flash_location(url, message, kind, **params), status_code=303)


def home_redirect(
    message: Any = "",
    kind: str = "ok",
    /,
    *,
    credential_topic: str | None = None,
    browser_auth_id: str | None = None,
    **params: Any,
) -> RedirectResponse:
    location = "/"
    query = []
    if credential_topic:
        query.append("credential_topic=" + quote(str(credential_topic)))
    if browser_auth_id:
        query.append("browser_auth_id=" + quote(str(browser_auth_id)))
    if query:
        location += "?" + "&".join(query)
    return flash_redirect(location, message, kind, **params)


def add_refused_redirect(problem: str, draft: dict[str, str], *, kind: str = "", page: str = "/") -> RedirectResponse:
    """D2/H2: back to the page (Home, or Sites for a new site) with the add form open, the draft
    and the reason shown once (no flash). The draft never holds a password."""
    record = {key: str(value) for key, value in draft.items() if value and key != "password"}
    record.update(error=problem, kind=kind)
    token = _ADD_DRAFTS.put(record)
    # No #new fragment: the browser's fragment scroll after load would take the focus away from
    # the field app.js puts it in (the open form is at the top of the page anyway).
    return RedirectResponse(page + "?add=" + quote(token), status_code=303)


def add_draft(request: Request) -> dict[str, str] | None:
    return _ADD_DRAFTS.get(request.query_params.get("add") or "")


def credential_prompt(request: Request) -> dict[str, Any] | None:
    topic_id = (request.query_params.get("credential_topic") or "").strip()
    if not topic_id or len(topic_id) > 64 or not re.fullmatch(r"[A-Za-z0-9_-]+", topic_id):
        return None
    state = _context.state()
    topic = next((item for item in state.get("topics") or [] if str(item.get("id")) == topic_id), None)
    if not isinstance(topic, dict):
        return None
    tracker = match_tracker(load_trackers(_context.config()), str(topic.get("url") or ""))
    if tracker is None or not tracker.spec.get("login_path"):
        return None
    credentials = ((_context.secrets_or_none() or {}).get("trackers") or {}).get(tracker.name) or {}
    username = str(credentials.get("username") or "")
    browser_enabled = bool(tracker.spec.get("browser_auth"))
    operation_id = (request.query_params.get("browser_auth_id") or "").strip()
    browser_status = (
        services.browser_auth.status(topic_id, operation_id or None) if browser_enabled else {"status": "idle"}
    )
    return {
        "id": topic_id,
        "title": str(topic.get("tracker_title") or topic.get("title") or ""),
        "tracker": tracker.name,
        "username": username,
        "browser_auth": browser_enabled,
        "browser_auth_status": browser_status,
    }


def row_class(row: CheckRow) -> str:
    """The class of a check row's error: the typed error's own, the text's for an old one."""
    return str(row.get("error_class") or error_class(str(row.get("error") or "")))


def manual_check_flash(row: CheckRow | None, *, topic_id: str, tracker_name: str = "") -> RedirectResponse | None:
    if not row or row.get("ok"):
        return None
    error = humanize(str(row.get("error") or t("web.check.not_confirmed")).strip())
    cls = row_class(row)
    if cls == "qbit":
        flash = t("web.check.added_client_refused", error=error)
    else:
        flash = t("web.check.added_check_failed", error=error)
    # The status-colour contract (AGENTS.md): a site that does not answer is amber, not an error.
    kind = "warn" if cls in TRACKER_WARNING_CLASSES else "err"
    return home_redirect(flash, kind, credential_topic=topic_id if cls == "tracker_auth" and tracker_name else None)


def topic_check_row(topic_id: str, out: dict[str, Any]) -> CheckRow | None:
    return next((r for r in out.get("results") or [] if str(r.get("id")) == str(topic_id)), None)


def ui_time(value: str | None) -> str:
    if not value:
        return ""
    try:
        return format_ui_timestamp(value)
    except TypeError, ValueError:
        return "—"


# Event kind -> catalog key of its short label.
_EVENT_LABELS = {
    "torrent_completed": "web.event.file",
    "client_added": "web.event.file",
    "new_file": "web.event.file",
    "episode_completed": "web.event.file",
    "file_completed": "web.event.file",
    "client_removed": "web.event.file",
}


def _event_display(event: Mapping[str, Any]) -> str:
    kind = str(event.get("kind") or "")
    if event.get("label_code"):  # TOW's own wording ("Back in the client"): the reader's language
        return render_stored(event["label_code"], event.get("label_params"), str(event.get("label") or ""))
    if event.get("label"):
        return str(event["label"])
    label = _EVENT_LABELS.get(kind)
    return t(label) if label else kind


def event_output(event: Mapping[str, Any]) -> dict[str, Any]:
    row = dict(event)
    if row.get("at"):
        row["event_at"] = ui_time(row["at"])
    row["display"] = _event_display(row)
    return row


# A scheduled check is late after two intervals plus this slack (as the watchdog says).
_STALE_SLACK_SEC = 10 * 60


def attention(state: Mapping[str, Any], cfg: Mapping[str, Any]) -> list[str]:
    """G3: what needs the owner now - shown above the list, empty when all is well."""
    import time
    from datetime import UTC, datetime

    items: list[str] = []
    raw_health = state.get("health")
    health: dict[str, Any] = raw_health if isinstance(raw_health, dict) else {}
    interval = interval_sec_of(cfg)
    # Late against the real next scheduled check (never a manual check or a progress pass).
    next_at = services.next_check_at(interval, health)
    if next_at and time.time() - next_at > interval + _STALE_SLACK_SEC:
        last = next_at - interval
        items.append(t("web.attention.stale", at=ui_time(datetime.fromtimestamp(last, UTC).isoformat())))
    if health.get("check_error") == "secrets_migration_required" or health.get("check_ok") is False:
        items.append(t("web.attention.blocked"))
    if health.get("qbit_ok") is False:
        items.append(t("web.attention.client_down"))
    if isinstance(health.get("history_rebuilt_at"), int):  # kept a week by the check
        items.append(t("web.attention.history_rebuilt"))
    need_login: dict[str, int] = {}
    trackers = load_trackers(cfg)
    for topic in state.get("topics") or []:
        if topic.get("paused") or topic.get("last_error_class") != "tracker_auth":
            continue
        tracker = match_tracker(trackers, str(topic.get("url") or ""))
        name = tracker.name if tracker else t("web.site_unknown")
        need_login[name] = need_login.get(name, 0) + 1
    for name, count in sorted(need_login.items()):
        items.append(t("web.attention.need_login", site=name, count=count))
    return items


def _search_href(tracker: GenericHttpTracker | None, state: Mapping[str, Any], query: str) -> str:
    """E2: the site's search link for ``query`` (the site's "search_path", else its preset's).
    A link only - TOW never fetches or scrapes search results."""
    from urllib.parse import quote

    from tow.trackers import presets

    if tracker is None or not query.strip():
        return ""
    spec = getattr(tracker, "spec", {}) or {}
    path = str(spec.get("search_path") or presets.search_path(tracker.name))
    hosts = [str(h).rstrip("/") for h in spec.get("fetch_hosts") or [] if h]
    mirror = ((state.get("mirrors") or {}).get(tracker.name) or {}).get("active")
    host = str(mirror or (hosts[0] if hosts else "")).rstrip("/")
    if not path or not host:
        return ""
    return host + path.replace("{q}", quote(query.strip()))


def _next_season_query(title: str, series: str, summary: Mapping[str, Any]) -> str:
    """For a complete season: "<name> <season + 1>" (E2)."""
    from tow.episodes import parse_season_hint

    if not summary.get("is_complete"):
        return ""
    season = parse_season_hint(title)
    return f"{series} {season + 1}" if season else ""


def _error_line(error: str, cls: str) -> str:
    """One short line for the row (D4): the error's class in words, then its advice
    (the part after " — ") or its start; the full text stays in the edit panel."""
    from tow.log import CLS_RU

    text = " ".join(str(error or "").split())
    if not text:
        return ""
    lang = owner_language()
    detail = text.split(" — ", 1)[1] if " — " in text else text
    for internal, key in _ERROR_WORDS:
        detail = detail.replace(internal, i18n.t(key, lang) if key else "")
    if len(detail) > 90:
        detail = detail[:89].rstrip() + "…"
    label = CLS_RU.label(cls, lang, i18n.t("web.error_line.error", lang))
    line = detail if detail.lower().startswith(label.lower()) else f"{label}: {detail}"
    return line[:1].upper() + line[1:]


# Frequent internal phrases in the owner's words for the row (catalog keys); the raw text stays
# in the tooltip.
_ERROR_WORDS = (
    ("all hosts failed: ", ""),
    ("all hosts failed", "web.error_words.all_hosts_failed"),
    ("previous torrent revision is still active on an overlapping file: ", "web.error_words.previous_revision"),
    ("no download link on page", "web.error_words.no_download_link"),
    ("network unavailable", "web.error_words.network"),
    ("timeout", "web.error_words.timeout"),
    ("http 503", "web.error_words.http_503"),
    ("http 502", "web.error_words.http_502"),
    ("http 404", "web.error_words.http_404"),
    ("not a torrent", "web.error_words.not_torrent"),
)


def topic_rows(state: Mapping[str, Any]) -> list[dict[str, Any]]:
    trs = load_trackers(_context.config())
    history = services.load_download_history()
    rows = []
    for topic in state.get("topics") or []:
        url = topic.get("url") or ""
        tr = match_tracker(trs, url)
        title = topic.get("tracker_title") or topic.get("title") or ""
        summary = topic_progress_summary(topic, history)
        record = (history.get("topics") or {}).get(str(topic.get("id") or "")) or {}
        event = record.get("last_event") or {}
        status = project_home_status(topic, record)
        series = title.split(" / ")[0]
        next_season = _next_season_query(title, series, summary)
        # The error in the reader's language (from its code); an old record shows its stored text.
        last_error = render_stored(
            topic.get("last_error_code"), topic.get("last_error_params"), str(topic.get("last_error") or "")
        )
        rows.append(
            {
                **topic,
                "last_error": last_error,
                "replaces_revision": blocked_by_previous_revision(topic),
                "tracker": tr.name if tr else t("web.site_unknown"),
                "series": title.split(" / ")[0],
                "download_summary": summary,
                "last_event_label": _event_display(event) if event else "",
                "last_event_at": ui_time(event.get("at")) if event else "",
                "last_event_iso": str(event.get("at") or "") if event else "",
                "last_error_class": (
                    cls := str(topic.get("last_error_class") or "")
                    or (
                        error_class(str(topic.get("last_error") or ""), topic.get("last_error_code"))
                        if last_error
                        else ""
                    )
                ),
                "last_error_human": humanize(last_error),
                "error_line": _error_line(humanize(last_error), cls),
                "search_href": _search_href(tr, state, series),
                "next_season_href": _search_href(tr, state, next_season) if next_season else "",
                "torrent_tone": status.torrent_tone,
                "torrent_label": status.torrent_label,
                "tracker_tone": status.tracker_tone,
                "tracker_label": status.tracker_label,
            }
        )
    return rows


def topic_progress_summary(topic: Mapping[str, Any], history: Mapping[str, Any]) -> dict[str, Any]:
    summary = progress_summary(str(topic.get("id") or ""), history)
    if summary.get("expected") is not None:
        return summary
    expected = expected_for_topic(topic)
    if not expected or not expected.get("total"):
        return summary
    return {
        **summary,
        "expected": int(expected["total"]),
        "completion_known": False,
        "is_complete": False,
    }


def backup_view(cfg: dict[str, Any], request: Request) -> dict[str, Any]:
    """Night copies: folder, last result, the newest copies; folders of both kinds of copies."""
    from tow.locations import LOCATIONS, free_bytes, is_default, resolve
    from tow.pulse import clock
    from tow.restore_points import cleanup_pending
    from tow.snapshots import list_snapshots, status

    folders = {}
    for kind, location in LOCATIONS.items():
        raw = str(cfg.get(location.key) or "")
        path = resolve(raw, location)
        free = free_bytes(path)
        folders[kind] = {
            "kind": kind,
            "title": location.title,
            "value": "" if is_default(raw, location) else raw,
            "path": str(path),
            "default_path": str(resolve("", location)),
            "is_default": is_default(raw, location),
            "free": format_bytes(free) if free is not None else "",
        }
    st = status()
    snapshots = [
        {"name": row["name"], "at": clock(row["created_ts"]), "size": format_bytes(row["bytes"])}
        for row in list_snapshots(limit=7)
    ]
    last_ok = st.get("last_ok_at")
    failed_at = st.get("last_error_at")
    return {
        "folders": folders,
        "snapshots": snapshots,
        "last_ok": clock(last_ok) if isinstance(last_ok, (int, float)) else (snapshots[0]["at"] if snapshots else ""),
        "failed": bool(isinstance(failed_at, (int, float)) and (not last_ok or failed_at > last_ok)),
        "error": str(st.get("last_error") or ""),
        "error_at": clock(failed_at) if isinstance(failed_at, (int, float)) else "",
        "cleanup_pending": st.get("last_cleanup_pending") is True,
        "point_cleanup_pending": cleanup_pending(),
    }
