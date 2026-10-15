"""Messages after an action: one record per message, the subject it is about, and the redirect
that shows it once.

A record is ``{"id", "kind", "subject", "text", "at"}`` (and ``"undo"``, the stamp of the undo
the action made): ``kind`` is one of ``KINDS``; ``subject`` names the topic, site or client the
message is about ("" for a global one) and is never a part of ``text``; the page says it as
``message_text`` - "{subject} — {result}" - in base.html's strip (``_message.html``) and in the
script's history (static/messages.js), which keeps the record by its ``id``. A route of one topic
or one site gets its subject before it changes anything (``resolve_subject``, the app's
dependency); a route that creates one or acts on a client names it (``use_subject``); a check
that waits in the queue keeps the one it was asked with.
"""

from __future__ import annotations

import secrets
import threading
import time
from collections.abc import Mapping
from typing import Any
from urllib.parse import parse_qs, quote, unquote, urlsplit

from fastapi import Request
from fastapi.responses import RedirectResponse

from tow import i18n
from tow.errors import Msg, TowError
from tow.log import owner_language
from tow.records import shown_title
from tow.web import _context, services
from tow.web.text import sentence, t

# ok: done; info: accepted, queued, skipped or awaiting confirmation; warn: transport trouble or a
# real caution; err: an actionable failure (the status-colour contract).
KINDS = ("ok", "info", "warn", "err")
FLASH_KINDS = KINDS


class TokenStore:
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


# A message shown once after an action (typed flash). It lives in the memory of the web process
# only, on purpose: it matters for the next page, a few seconds after the action. A restart of the
# web server (an update, the supervisor bringing it back) forgets it - the page after it simply
# shows no message, the action itself is saved or refused as before.
FLASHES = TokenStore(ttl=600, limit=256)


def _local_href(href: str) -> str:
    """A link a message may carry: a page of this TOW ("/..."), else none."""
    value = str(href or "")
    if not value.startswith("/") or value.startswith("//") or "\\" in value or any(ord(ch) <= 32 for ch in value):
        return ""
    return value[:500]


def known_names() -> frozenset[str]:
    """The names a message may start with that keep their own spelling (``text.sentence``): every
    site's key and title, every torrent client's id, kind and title, every messenger's kind and
    name - a lower-case "nnmclub" or "ntfy" is a name, not a word. Once per request."""

    def build() -> frozenset[str]:
        from tow.clients.factory import client_configurations
        from tow.jsonish import as_dict
        from tow.notifiers.registry import kinds

        names: set[str] = set()
        try:
            cfg = _context.config()
            for key, spec in as_dict(cfg.get("trackers")).items():
                names.update((str(key), str(as_dict(spec).get("title") or "")))
            for row in client_configurations(cfg):
                names.update(str(row.get(field) or "") for field in ("id", "kind", "title"))
        except OSError, ValueError, TypeError, RuntimeError:
            pass  # unreadable settings: the words alone decide
        for kind, module in kinds().items():
            names.update((str(kind), str(getattr(module, "TITLE", "") or "")))
        names.discard("")
        return frozenset(names)

    return _context.memo("message_names", build)


def starts_with_name(text: str) -> bool:
    """``text`` starts with a name that keeps its spelling at the start of a message
    (``text.sentence``: "qBittorrent …", "nnmclub: …", "rutor.info …")."""
    return bool(text[:1].islower()) and sentence(text, known_names()) == text


def record(
    text: str,
    kind: str = "ok",
    *,
    subject: str = "",
    message_id: str = "",
    at: float | None = None,
    href: str = "",
    link: str = "",
    cancel: str = "",
    value_first: bool = False,
) -> dict[str, str]:
    """One message: ``text`` in the page's language, its kind, what it is about, its identity in
    the page's history (a new random one by default), when it was said, the page it offers
    (``href``, named ``link``: "View results") and the address its Cancel posts to (``cancel``:
    a check that waits) - addresses of this TOW only.

    ``named`` ("1") says that the words start with a name - one ``text.sentence`` knows, or a value
    its catalog text puts first (``value_first``: "{tracker}: all mirrors are paused") - which a
    message said on its own keeps as written; other words start it as a sentence
    (``message_text``, messages.js)."""
    said = {
        "id": message_id or secrets.token_urlsafe(9),
        "text": text,
        "kind": kind if kind in KINDS else "ok",
        "subject": " ".join(str(subject or "").split())[:200],
        "at": str(int(time.time() if at is None else at)),
    }
    if value_first or starts_with_name(str(text or "")):
        said["named"] = "1"
    if (target := _local_href(href)) and link:
        said.update(href=target, link=" ".join(str(link).split())[:80])
    if cancel_at := _local_href(cancel):
        said["cancel"] = cancel_at
    return said


def request_flash(request: Request) -> dict[str, str] | None:
    """The message a redirect left for this page (its record); None for no, an unknown or an
    expired token - and for a ``?flash=<text>`` link of an older TOW (never shown)."""
    token = request.query_params.get("flash") or ""
    return FLASHES.get(token) if token else None


def flash_at(location: str) -> dict[str, str] | None:
    """The message a redirect to ``location`` leaves (as ``request_flash`` reads it there)."""
    try:
        token = (parse_qs(urlsplit(location).query).get("flash") or [""])[0]
    except ValueError:
        return None
    return FLASHES.get(token) if token else None


def flash_text(message: Any, /, **params: Any) -> str:
    """``message`` as text in the page's language: a catalog key (filled with ``params``), a
    ``Msg`` or a typed error, or a text already composed from catalog texts."""
    if isinstance(message, (Msg, TowError)):
        return message.text()
    text = str(message or "")
    return t(text, **params) if text and i18n.has(text) else text


def starts_with_value(message: Any) -> bool:
    """The catalog text of ``message`` (a key, a ``Msg`` or a typed error) starts with a value - a
    name its caller gave ("{tracker}: all mirrors are paused", an error's site prefix) - which
    keeps its own spelling at the start of a message."""
    if isinstance(message, (Msg, TowError)):
        code, prefixed = message.code, bool(message.params.get("_prefix"))
    else:
        code, prefixed = str(message or ""), False
    return prefixed or (i18n.has(code) and i18n.translate(code).lstrip().startswith("{"))


def use_subject(value: str) -> None:
    """What this request's messages are about from now on: the topic a route has just added, the
    client it checks. Topic and site routes get theirs before they change anything
    (``resolve_subject``)."""
    _context.use_subject(" ".join(str(value or "").split())[:200])


def topic_subject(title: object) -> str:
    """A topic as a message names it: the short series title ("Harbour Clock", not the whole
    "Harbour Clock / Часовая гавань [2024]")."""
    return services.short_series_title(str(title or ""), owner_language()) if str(title or "").strip() else ""


def resolve_subject(request: Request) -> None:
    """The app's dependency for every route: a route of one topic (``/topics/<id>/...``) or one
    site (``/sites/<name>...``) names it for its messages before it changes anything, so a
    deleted or renamed one is named as it was. Every other route says no subject."""
    params = request.path_params
    path = request.url.path
    if path.startswith("/topics/") and "tid" in params:
        tid = str(params["tid"])
        topic = next((item for item in _context.state().get("topics") or [] if str(item.get("id")) == tid), None)
        use_subject(topic_subject(shown_title(topic)) if isinstance(topic, dict) else "")
    elif path.startswith("/sites/") and "name" in params:
        name = str(params["name"])
        site = (_context.config().get("trackers") or {}).get(name)
        # By the title Sites shows ("NNM-Club"), not its settings key.
        use_subject(str((site.get("title") if isinstance(site, Mapping) else "") or name) if site is not None else "")


def message_text(message: Mapping[str, Any] | None) -> str:
    """A message as the page says it: "{subject} — {result}" for one about a topic, a site or a
    client, the result alone, as a sentence, for a global one; never the subject twice. Words
    that start with a name (``named``, ``record``) keep it as written: never "QBittorrent"."""
    message = message or {}
    text = str(message.get("text") or "").strip()
    subject = str(message.get("subject") or "").strip()
    if not subject or text.casefold().startswith(f"{subject} — ".casefold()):
        return text if message.get("named") else text[:1].upper() + text[1:]
    return t("web.message.subject_result", subject=subject, result=text)


def flash_location(
    url: str,
    message: Any,
    kind: str = "ok",
    /,
    *,
    subject: str | None = None,
    href: str = "",
    link: str = "",
    **params: Any,
) -> str:
    """``url`` with ``flash=<token>`` of a message kept on the server (see ``flash_redirect``).
    ``subject``: what it is about; None takes the request's (``resolve_subject``), "" is none.
    ``href``/``link``: a page of this TOW the message offers, and its name."""
    text = flash_text(message, **params)
    if not text:
        return url
    said = record(
        text,
        kind,
        subject=_context.subject() if subject is None else subject,
        href=href,
        link=link,
        value_first=starts_with_value(message),
    )
    # The undo this action made goes with its message: a check right after a save says
    # "Connected" without an "Undo" that would put the save back.
    if stamped := services.undo_stamped_here():
        said["undo"] = stamped
    token = FLASHES.put(said)
    path, hash_mark, fragment = url.partition("#")
    joined = f"{path}{'&' if '?' in path else '?'}flash={token}"
    return joined + hash_mark + fragment


def flash_redirect(url: str, message: Any, kind: str = "ok", /, **params: Any) -> RedirectResponse:
    """A 303 to ``url`` that shows ``message`` once, as ``kind``: ``ok`` (done), ``warn`` (done
    in part, or wait and retry) or ``err`` (nothing done). The kind comes from the route, never
    from the words; ``subject=`` names what it is about (default: the request's)."""
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
    """Home with ``message`` (``flash_redirect``), and the sign-in prompt of ``credential_topic``
    (with the browser sign-in ``browser_auth_id`` it follows)."""
    location = "/"
    query = []
    if credential_topic:
        query.append("credential_topic=" + quote(str(credential_topic)))
    if browser_auth_id:
        query.append("browser_auth_id=" + quote(str(browser_auth_id)))
    if query:
        location += "?" + "&".join(query)
    return flash_redirect(location, message, kind, **params)
