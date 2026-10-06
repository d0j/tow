"""The page templates and what every page can call from them: texts, the header's status, the
"Undo" bar, the sign-in help and the commands as this system writes them.

``TEMPLATES`` renders every page; ``configure()`` (called by ``tow.web.app.create_app``) gives
the templates their globals.
"""

from __future__ import annotations

import hashlib
import socket
from pathlib import Path
from typing import Any

from fastapi.templating import Jinja2Templates

from tow import __version__, access, i18n, platform, undo
from tow.auth import password_hint
from tow.clients.factory import client_name
from tow.config import interval_sec_of, port_of
from tow.jsonish import as_dict
from tow.trackers import load_trackers, match_tracker
from tow.web import _context, services
from tow.web.text import t, tm
from tow.web.views import flash_ttl_sec

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
    site_tones: dict[str, str] = {}
    trs = load_trackers(cfg)
    for topic in state.get("topics") or []:
        tr = match_tracker(trs, topic.get("url") or "")
        if tr and tr.name not in site_tones:
            site_tones[tr.name] = "mut"
    for p in doc.get("probes") or []:
        name = str(p.get("tracker") or "")
        if name not in site_tones:
            continue
        if p.get("ok"):
            site_tones[name] = "ok"
        elif site_tones[name] != "ok":
            # A mirror that does not answer is transport trouble: amber, never red (AGENTS.md).
            site_tones[name] = "warn"
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
    qbit_known = "qbit_ok" in h or bool(doc.get("qbit"))
    return {
        "qbit_ok": qbit_ok,
        "qbit_tone": ("ok" if qbit_ok else "bad") if qbit_known else "mut",
        "qbit": t("web.header.connected")
        if qbit_ok
        else (f"{client_label} ?" if not h and not doc else t("web.header.disconnected")),
        "client_label": client_label,
        "bot": bot,
        "bot_ok": bot_ok,
        "sites": site_tones,
        "at": h.get("at") or "",
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


def undo_left_sec() -> int:
    return undo.undo_left_sec(_context.state())


def login_help() -> dict[str, str]:
    """What the sign-in page says under "Forgot your password?"."""
    record = (_context.secrets_or_none() or {}).get(access.RECORD_KEY)
    port = port_of(_context.config())
    return {"hint": password_hint(record), "machine": socket.gethostname(), "url": f"http://127.0.0.1:{port}"}


def _languages() -> list[dict[str, str]]:
    return [{"code": code, "name": name, "native": native} for code, name, native in i18n.available()]


def js_texts() -> dict[str, str]:
    """The ``js.*`` texts of app.js in the current language (base.html hands them over as JSON)."""
    return i18n.texts_with_prefix("js.")


def content_texts() -> dict[str, str]:
    return {
        **i18n.texts_with_prefix("content.js."),
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
    env["languages"] = _languages
    env["js_texts"] = js_texts
    env["content_texts"] = content_texts
    env["content_existing"] = content_existing
    env["header_health"] = _header_health_once
    env["static_version"] = STATIC_VERSION
    env["undo_label"] = undo_label
    # Commands, paths and examples the pages show, as this system writes them (tow.platform).
    env["os_launcher"] = lambda: "scripts\\tow.cmd" if platform.is_windows() else "scripts/tow"
    env["os_sep"] = lambda: "\\" if platform.is_windows() else "/"
    env["os_example_folder"] = lambda: "D:\\TV" if platform.is_windows() else "/srv/media"
    env["can_undo"] = can_undo
    env["undo_just_made"] = undo_just_made
    env["undo_left_sec"] = undo_left_sec
    env["flash_ttl_sec"] = flash_ttl_sec
    env["login_help"] = login_help
    env["client_name"] = client_name
