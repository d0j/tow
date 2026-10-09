"""The page templates and what every page can call from them: texts, the header's status, the
"Undo" bar, the sign-in help and the commands as this system writes them.

``TEMPLATES`` renders every page; ``configure()`` (called by ``tow.web.app.create_app``) gives
the templates their globals.
"""

from __future__ import annotations

import hashlib
import socket
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import Request
from fastapi.templating import Jinja2Templates

from tow import __version__, access, i18n, paths, platform, undo
from tow.auth import SESSION_COOKIE, password_hint
from tow.clients.factory import client_name
from tow.clock import parse_timestamp
from tow.config import interval_sec_of, port_of
from tow.jsonish import as_dict
from tow.status import site_check_tone
from tow.web import _context, services
from tow.web.text import t, tm
from tow.web.views import flash_ttl_sec, stored_ui_time

PACKAGE_DIR = Path(__file__).resolve().parent.parent
STATIC_DIR = PACKAGE_DIR / "static"


def _static_asset_version() -> str:
    digest = hashlib.sha256()
    for name in ("app.css", "app.js", "updates.css", "updates.js", "content.css", "content.js"):
        path = STATIC_DIR / name
        if path.is_file():
            digest.update(path.read_bytes())
    return f"{__version__}-{digest.hexdigest()[:12]}"


STATIC_VERSION = _static_asset_version()
TEMPLATES: Jinja2Templates = Jinja2Templates(directory=str(PACKAGE_DIR / "templates"))


def _default_client_label() -> str:
    """The name of the client new torrents go to, for the header."""
    from tow.clients.factory import client_configuration

    try:
        return client_name(client_configuration(_context.config()))
    except RuntimeError, ValueError:
        return t("web.header.client")


def _when(value: object) -> datetime | None:
    """A stored ISO time; None for none (or a text an old TOW wrote)."""
    try:
        return parse_timestamp(str(value)) if value else None
    except TypeError, ValueError, OverflowError:
        return None


def _later(at: datetime | None, than: datetime | None) -> bool:
    return at is not None and (than is None or at > than)


def site_tones() -> tuple[dict[str, str], list[str]]:
    """Every site's tone for the header and Sites, and the sites Home has topics on (in order).

    The tone is what the latest observation of the site says - a check that asked it or a
    Diagnostics probe of its mirrors, whichever came later (a probe without a time is older
    than any check): "ok" it answered, "warn" transport trouble (a mirror down or paused,
    Cloudflare, a sign-in, the day's limit), "mut" not asked yet. Once per request."""
    return _context.memo("site_tones", _site_tones)


def _site_tones() -> tuple[dict[str, str], list[str]]:
    state = _context.state()
    checked: dict[str, tuple[datetime | None, str]] = {}
    watched: dict[str, None] = {}
    for topic in state.get("topics") or []:
        name = _context.site_name(topic.get("url") or "")
        if name is None:
            continue
        watched.setdefault(name)
        tone = site_check_tone(topic)
        if tone is None:
            continue
        at = _when(topic.get("last_check"))
        if name not in checked or _later(at, checked[name][0]):
            checked[name] = (at, tone)
    probed: dict[str, tuple[datetime | None, bool]] = {}
    for probe in as_dict(state.get("doctor")).get("probes") or []:
        if not isinstance(probe, dict):
            continue
        name = str(probe.get("tracker") or "")
        at, answered = probed.get(name, (None, False))
        when = _when(probe.get("at"))
        probed[name] = (when if _later(when, at) else at, answered or bool(probe.get("ok")))
    tones: dict[str, str] = {}
    for name in {*watched, *checked, *probed}:
        check, probe = checked.get(name), probed.get(name)
        if check is not None and (probe is None or not _later(probe[0], check[0])):
            tones[name] = check[1]
        elif probe is not None:
            # A mirror that does not answer is transport trouble: amber, never red (AGENTS.md).
            tones[name] = "ok" if probe[1] else "warn"
        else:
            tones[name] = "mut"
    return tones, list(watched)


def header_health() -> dict[str, Any]:
    state = _context.state()
    h = state.get("health") or {}
    doc = state.get("doctor") or {}
    secrets = _context.secrets_or_none()
    secret_store_error = secrets is None
    secrets = secrets or {}
    cfg = _context.config()
    qbit_ok = bool(h.get("qbit_ok"))
    if not h and str(doc.get("qbit") or "").startswith("FAIL"):
        qbit_ok = False
    elif not h and doc.get("qbit"):
        qbit_ok = True
    from tow.clients.factory import default_client_id

    # Settings' "Check" found the main client not answering after the last check: say so now.
    ping_failed = default_client_id(cfg) in as_dict(h.get("ping_failed"))
    qbit_ok = qbit_ok and not ping_failed
    _context.trackers()  # a site whose link pattern is broken fails the header, as it always did
    tones, watched = site_tones()
    from tow.notifiers import health as notify_health
    from tow.notifiers import summary as notify_summary

    if secret_store_error:
        bot_ok: bool | None = False
        bot = t("web.header.secrets_migration")
    else:
        # Every connected messenger, judged by its real last delivery (no extra traffic).
        # None: nothing connected (grey), True: every channel delivers (green), False: one fails (red).
        bot_ok = notify_health(secrets, state)
        bot = notify_summary(secrets, state)
    client_label = _default_client_label()
    interval_sec = interval_sec_of(cfg)
    next_at = services.next_check_at(interval_sec, h)
    check_ok = bool(h.get("check_ok", True)) and not secret_store_error
    check_error = str(h.get("check_error") or ("secrets_migration_required" if secret_store_error else ""))
    # Grey until a check or the diagnostics have asked the client at all: "not asked" is not "down".
    qbit_known = "qbit_ok" in h or bool(doc.get("qbit")) or ping_failed
    return {
        "qbit_ok": qbit_ok,
        "qbit_tone": ("ok" if qbit_ok else "bad") if qbit_known else "mut",
        "qbit": t("web.header.connected")
        if qbit_ok
        else (f"{client_label} ?" if not h and not doc else t("web.header.disconnected")),
        "client_label": client_label,
        "bot": bot,
        "bot_ok": bot_ok,
        "sites": {name: tones.get(name, "mut") for name in watched},
        "at": stored_ui_time(h.get("at")),  # stored as an ISO time (older: already written out)
        "at_ts": int(h.get("at_ts") or 0),
        # The countdown runs to next_from_ts + interval_sec: the real next scheduled check
        # (tow run's own plan), never a manual check or a progress pass; 0 when none is known.
        "next_at_ts": int(next_at) if next_at else 0,
        "next_from_ts": int(next_at - interval_sec) if next_at else 0,
        "check_ok": check_ok,
        "check_error": check_error,
        "interval_sec": interval_sec,
        "ver": __version__,
    }


def _header_health_once() -> dict[str, Any]:
    """The header is rendered once per page but asked for twice on /settings: compute it once."""
    return _context.memo("header", header_health)


# The "Undo" bar (base.html) asks these; the engine and every kind live in tow.undo.
def undo_label() -> str:
    """What "Undo" will put back, in words (the button's tooltip); ``tow.undo`` has the kinds."""
    return undo.undo_label(_context.state())


def can_undo() -> bool:
    return undo.can_undo(_context.state())


def undo_just_made() -> bool:
    """The undo belongs to the action whose result is shown now (D3)."""
    return undo.undo_just_made(_context.state())


def undo_in_message(flash: Mapping[str, Any] | None) -> bool:
    """The message shown is the one of the action that made the live undo: its "Undo" goes in it.
    A later message (a check, a mirror chosen, a pause) never carries an earlier change's undo."""
    stamp = str((flash or {}).get("undo") or "")
    return bool(stamp) and stamp == undo.stamp_of(_context.state())


def undo_left_sec() -> int:
    return undo.undo_left_sec(_context.state())


def login_help() -> dict[str, str]:
    """What the sign-in page says under "Forgot your password?"."""
    record = (_context.secrets_or_none() or {}).get(access.RECORD_KEY)
    port = port_of(_context.config())
    return {"hint": password_hint(record), "machine": socket.gethostname(), "url": f"http://127.0.0.1:{port}"}


def site_title(name: object) -> str:
    """How pages name a site: its settings' title ("NNM-Club"), as on Sites; else its key."""
    title = _site_titles().get(str(name))
    return str(name) if title is None else title


def _site_titles() -> dict[str, str]:
    """Every site's title by its key, built once per request: Home names a site in every row."""

    def build() -> dict[str, str]:
        sites = {str(key): value for key, value in as_dict(_context.config().get("trackers")).items()}
        return {key: str(as_dict(value).get("title") or key) for key, value in sites.items()}

    return _context.memo("site_titles", build)


def network_session(request: Request) -> bool:
    """This page was opened from another device with a session: there is something to sign out
    of (this computer needs no password, so it has none)."""
    return not access.is_local(request) and bool(request.cookies.get(SESSION_COOKIE))


def theme() -> str:
    """The theme Settings chose for every page, "light" or "dark" (``data-theme`` on <html>);
    "" follows the system - also when config.yaml cannot be read (the setup and error pages)."""
    try:
        value = _context.config().get("theme")
    except OSError, ValueError:
        return ""
    return value if value in ("light", "dark") else ""


def _languages() -> list[dict[str, str]]:
    return [{"code": code, "name": name, "native": native} for code, name, native in i18n.available()]


def js_texts() -> dict[str, str]:
    """The ``js.*`` texts of app.js in the current language (base.html hands them over as JSON),
    and the language's date pattern under ``_datetime`` (not a catalog key)."""
    return {**i18n.texts_with_prefix("js."), "_datetime": i18n.datetime_pattern()}


def content_texts() -> dict[str, str]:
    return {
        **i18n.texts_with_prefix("content.js."),
        **i18n.texts_with_prefix("web.bytes."),
        **{key: t(key) for key in ("content.cached_hint", "content.cache_failed", "content.limited_confirm")},
    }


def content_existing(topic: dict[str, Any] | None) -> list[dict[str, str]]:
    """A browser must not round file identities above JavaScript's safe integer range."""
    selection = as_dict((topic or {}).get("selection"))
    if selection.get("mode") != "exact":
        return []
    return [{"path": row["path"], "size": str(row["size"])} for row in selection.get("files") or []]


def sentence(text: object) -> str:
    """A message as a sentence: its first letter upper-case (catalog texts that are also parts
    of longer messages start in lower case)."""
    value = str(text or "")
    return value[:1].upper() + value[1:]


def configure(templates: Jinja2Templates = TEMPLATES) -> None:
    """The globals every template may call."""
    templates.env.filters["sentence"] = sentence
    env = templates.env.globals
    env["t"] = i18n.translate
    env["tm"] = tm
    env["lang"] = i18n.current
    env["theme"] = theme
    env["languages"] = _languages
    env["js_texts"] = js_texts
    env["content_texts"] = content_texts
    env["content_existing"] = content_existing
    env["header_health"] = _header_health_once
    env["static_version"] = STATIC_VERSION
    env["undo_label"] = undo_label
    # Commands, paths and examples the pages show, as this system writes them (tow.platform).
    env["os_launcher"] = lambda: paths.launcher(windows=platform.is_windows())
    env["os_sep"] = lambda: "\\" if platform.is_windows() else "/"
    env["os_example_folder"] = lambda: "D:\\TV" if platform.is_windows() else "/srv/media"
    env["can_undo"] = can_undo
    env["undo_just_made"] = undo_just_made
    env["undo_in_message"] = undo_in_message
    env["undo_left_sec"] = undo_left_sec
    env["flash_ttl_sec"] = flash_ttl_sec
    env["login_help"] = login_help
    env["network_session"] = network_session
    env["site_title"] = site_title
    env["client_name"] = client_name
