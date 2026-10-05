"""Typed errors and messages: a catalog key and its values, shown in the reader's language.

An error is raised as ``TowError("selection.too_long", limit=8192)``: the key of its text in
the language files (``src/tow/locales/*.json``) and the values the text needs.

- ``str(error)`` is the text in the current language, so code that shows or stores
  ``str(exc)`` keeps working;
- ``error.record()`` is the code and the values (JSON), kept in the state and the log and
  rendered again later in the reader's language (``render``);
- ``error.error_class`` is the status class (the Home colours, see AGENTS.md). It never comes
  from the text: given at the raise site (``cls=``), else by the code (``CLASSES``: an exact
  key or a section prefix, the longest wins), else the error type's default. A reworded or
  translated message keeps its colour.

A value may itself be an error or a ``Msg`` (rendered in the same language), a number
(a fraction is written with the language's decimal sign) or a text (shown as it is).
``prefix`` names who reports the error ("Transmission"): the text reads ``prefix: message``.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from typing import Any

from tow import i18n

# Every status class (log.cls.<class> names them; AGENTS.md says which colour each one is).
STATUS_CLASSES = (
    "quota",
    "tracker_auth",
    "frozen",
    "tracker",
    "no_tracker",
    "no_path",
    "qbit",
    "cloudflare",
    "gone",
    "not_torrent",
    "disk",
    "auth",
    "error",
)

# A code or a whole section ("client.") -> its status class; the longest match wins. A section
# of a plugin (client.<kind>.*) inherits "client.": a new client needs no line here.
CLASSES: dict[str, str] = {
    "client.": "qbit",
    "client.managed.wrong_folder": "no_path",
    "client.managed.no_folder": "no_path",
    "client.factory.": "error",  # the client list in the settings
    "client.factory.not_configured": "qbit",
    "client.factory.disabled": "qbit",
    "client.factory.not_implemented": "qbit",
    "check.": "error",
    "check.add_unconfirmed": "qbit",
    "check.existing_unconfirmed": "qbit",
    "check.client_unreachable": "qbit",
    "check.removed_from_client": "qbit",
    "check.client_cannot_select": "qbit",
    "check.client_cannot_update": "qbit",
    "check.previous_revision_active": "qbit",
    "check.once_unconfirmed": "qbit",
    "check.not_owned_partial": "qbit",
    "check.not_owned_priorities": "qbit",
    "check.migration_unconfirmed": "qbit",
    "check.unexpected_identity": "qbit",
    "check.save_path_unconfirmed": "qbit",
    "check.selection_unconfirmed": "qbit",
    "check.low_disk": "disk",
    "check.daily_limit": "quota",
    "check.frozen": "frozen",
    "check.no_tracker": "no_tracker",
    "check.no_save_path": "no_path",
    "progress.path_differs": "no_path",
    "mirrors.": "tracker",
    "mirrors.daily_limit": "quota",
    "mirrors.tracker_daily_limit": "quota",
    "mirrors.not_torrent_login": "tracker_auth",
    "mirrors.http_auth": "tracker_auth",
    "mirrors.cloudflare": "cloudflare",
    "mirrors.frozen": "frozen",
    "mirrors.no_hosts": "error",
    "selection.": "error",
    "content.": "error",
    "content.limited": "quota",
    "tracker.": "error",
    "tracker.no_download_link": "tracker_auth",
    "tracker.not_torrent": "not_torrent",
}


def class_of(code: str) -> str | None:
    """The status class ``CLASSES`` gives ``code`` (None: the code is not listed)."""
    best = ""
    for key in CLASSES:
        if (code == key or (key.endswith(".") and code.startswith(key))) and len(key) > len(best):
            best = key
    return CLASSES[best] if best else None


# --- values -----------------------------------------------------------------------------------

_MSG = "$msg"  # a stored value that is itself a message: {"$msg": {"code": ..., "params": ...}}
_PREFIX = "_prefix"


def _stored(value: Any) -> Any:
    """A value as it is kept in JSON."""
    if isinstance(value, (TowError, Msg)):
        return {_MSG: value.record()}
    if isinstance(value, Mapping) and isinstance(value.get(_MSG), Mapping):
        return {_MSG: dict(value[_MSG])}  # a stored message passed on (``as_value``)
    if isinstance(value, bool) or value is None or isinstance(value, (int, float, str)):
        return value
    if isinstance(value, BaseException):
        return str(value)
    return str(value)


def _shown(value: Any, lang: str) -> str:
    """A value as the text shows it in ``lang``."""
    if isinstance(value, (TowError, Msg)):
        return value.text(lang)
    if isinstance(value, Mapping) and isinstance(value.get(_MSG), Mapping):
        return render(value[_MSG], lang)
    if isinstance(value, float) and not isinstance(value, bool):
        return i18n.format_decimal(value, 1, lang)
    return "" if value is None else str(value)


def _text(code: str, params: Mapping[str, Any], lang: str | None) -> str:
    language = lang or i18n.current()
    values = {name: _shown(value, language) for name, value in params.items() if name != _PREFIX}
    text = i18n.translate(code, language, **values)
    prefix = params.get(_PREFIX)
    return f"{prefix}: {text}" if prefix else text


class Msg:
    """A message (not an error): a catalog key and its values, rendered when shown."""

    __slots__ = ("code", "params")

    def __init__(self, code: str, /, **params: Any) -> None:
        self.code = code
        self.params = params

    def text(self, lang: str | None = None) -> str:
        return _text(self.code, self.params, lang)

    def record(self) -> dict[str, Any]:
        return {"code": self.code, "params": {name: _stored(value) for name, value in self.params.items()}}

    def __str__(self) -> str:
        return self.text()

    def __repr__(self) -> str:
        return f"Msg({self.code!r}, **{self.params!r})"

    def __eq__(self, other: object) -> bool:
        return isinstance(other, Msg) and (self.code, self.params) == (other.code, other.params)

    def __hash__(self) -> int:
        return hash(self.code)


class TowError(RuntimeError):
    """An error TOW reports to the owner: a catalog key, its values and a status class.

    A ``RuntimeError`` like the literal errors it replaces; a subclass may add ``ValueError``."""

    default_class = "error"

    def __init__(self, code: str, /, *, cls: str | None = None, prefix: str = "", **params: Any) -> None:
        super().__init__(code)
        self.code = code
        self.params = {**params, **({_PREFIX: prefix} if prefix else {})}
        self.error_class = cls or class_of(code) or self.default_class

    def text(self, lang: str | None = None) -> str:
        return _text(self.code, self.params, lang)

    def record(self) -> dict[str, Any]:
        """``{"code", "params", "cls"}``: what the state and the log keep."""
        return {
            "code": self.code,
            "params": {name: _stored(value) for name, value in self.params.items()},
            "cls": self.error_class,
        }

    def __str__(self) -> str:
        return self.text()

    def __reduce__(self) -> tuple[Any, ...]:
        return (_rebuild, (type(self), self.code, self.error_class, self.params))


def _rebuild(kind: type[TowError], code: str, cls: str, params: dict[str, Any]) -> TowError:
    error = kind.__new__(kind)
    Exception.__init__(error, code)
    error.code, error.error_class, error.params = code, cls, params
    return error


# --- stored records ---------------------------------------------------------------------------


def record_of(error: BaseException | None) -> dict[str, Any] | None:
    """The record of a TOW error (also one raised ``from`` it), None for any other exception."""
    seen = 0
    while error is not None and seen < 5:
        if isinstance(error, TowError):
            return error.record()
        error = error.__cause__
        seen += 1
    return None


def render(record: Mapping[str, Any] | None, lang: str | None = None, fallback: str = "") -> str:
    """A stored ``{"code", "params"}`` in ``lang``; ``fallback`` (the text stored with it) when
    the record is missing or its code is unknown to this TOW (an older or newer one wrote it)."""
    if not isinstance(record, Mapping):
        return fallback
    code = record.get("code")
    if not isinstance(code, str) or not i18n.has(code):
        return fallback
    params = record.get("params")
    return _text(code, params if isinstance(params, Mapping) else {}, lang)


def render_stored(code: Any, params: Any, fallback: str, lang: str | None = None) -> str:
    """``render`` for a code and its values kept in two fields (``last_error_code`` / ``_params``)."""
    if not code:
        return fallback
    return render({"code": code, "params": params if isinstance(params, Mapping) else {}}, lang, fallback)


def as_value(record: Mapping[str, Any] | None, fallback: str = "") -> Any:
    """A stored record as a value of another message (rendered with it); ``fallback`` without one."""
    return {_MSG: dict(record)} if isinstance(record, Mapping) and record.get("code") else fallback


def codes_in(record: Mapping[str, Any] | None) -> Iterator[str]:
    """The code of a record and of every message among its values (an error inside an error)."""
    if not isinstance(record, Mapping):
        return
    code = record.get("code")
    if isinstance(code, str):
        yield code
    params = record.get("params")
    for value in (params or {}).values() if isinstance(params, Mapping) else ():
        if isinstance(value, Mapping) and isinstance(value.get(_MSG), Mapping):
            yield from codes_in(value[_MSG])


def text_of(value: Any, lang: str | None = None) -> str:
    """Any error-ish value as text in ``lang``: an error or a message, a stored record, a plain text."""
    if isinstance(value, (TowError, Msg)):
        return value.text(lang)
    if isinstance(value, Mapping):
        return render(value, lang, str(value.get("text") or ""))
    return "" if value is None else str(value)
