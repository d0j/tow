"""What every messenger module shares: its form fields, delivery errors, careful HTTP.

A messenger keeps its wording in the language files under ``notifier.<kind>.*``: its STEPS,
NOTE and Field label/placeholder/error hold keys, and the shared code translates them for
the page (``text``). A plain text instead of a key is shown as it is.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Protocol, get_protocol_members, runtime_checkable

import httpx

from tow import i18n
from tow.errors import Msg

# Retries for a busy or briefly unreachable service: three attempts in about four seconds.
RETRY_DELAYS = (1.0, 3.0)
MAX_RETRY_AFTER = 10.0


def ui_language() -> str:
    """The language of what the owner sees: the request's (or the CLI's); without one (a
    background thread, a direct call) the language of TOW's messages."""
    return i18n.current()


def message_language() -> str:
    """The language of messages and delivery reasons (what the messenger and the status keep)."""
    return i18n.message_language()


def text(value: str, lang: str | None = None, **params: Any) -> str:
    """A catalog key in ``lang`` (default: the owner's language), or a plain text as it is."""
    if not value or not i18n.has(value):
        return value
    return i18n.translate(value, lang or ui_language(), **params)


class DeliveryError(Exception):
    """A message was not delivered. ``reason`` is shown to the owner: it never contains
    a token, key or webhook address.

    - ``transient``: try again later (network, 429, 5xx); the message stays queued;
    - permanent (not transient, not ``drop``): the settings are wrong (401/403/404) - the
      recipient's queue waits until the owner changes them;
    - ``drop``: the messenger refused this one message (another 4xx) - it is dropped with the
      reason recorded, and the rest of the queue goes on.

    ``reason`` is a ``Msg`` (a catalog key and values: the status keeps it and the settings page
    shows it in the reader's language) or a plain text; ``.reason`` is its text in the language
    of TOW's messages, ``.message`` the ``Msg`` (None for a plain text).
    """

    def __init__(self, reason: str | Msg, *, transient: bool, drop: bool = False) -> None:
        self.message = reason if isinstance(reason, Msg) else None
        self.reason = reason.text(message_language()) if isinstance(reason, Msg) else reason
        super().__init__(self.reason)
        self.transient = transient
        self.drop = drop and not transient

    @property
    def detail(self) -> str | Msg:
        """What the status keeps: the ``Msg`` when there is one, else the text."""
        return self.message or self.reason


# Only these answers mean the settings themselves are wrong (token, webhook, topic access).
PERMANENT_CODES = frozenset({401, 403, 404})


def status_error(code: int, reason: str | Msg) -> DeliveryError:
    """The error for an HTTP answer that is not a success: 401/403/404 hold the queue until the
    settings change, 429/5xx are retried later, any other refusal drops just this message."""
    if code in PERMANENT_CODES:
        return DeliveryError(reason, transient=False)
    if code == 429 or code >= 500:
        return DeliveryError(reason, transient=True)
    return DeliveryError(reason, transient=False, drop=True)


@dataclass(frozen=True)
class Field:
    name: str
    label: str
    kind: str = "text"  # text | secret (masked, empty = keep) | list (comma separated)
    placeholder: str = ""
    pattern: str = ""  # each value must fully match
    error: str = ""
    required: bool = True
    default: str = ""


@runtime_checkable
class Notifier(Protocol):
    """A messenger module (``tow/notifiers/<kind>.py``) as the rest of TOW uses it.

    Required: the members below. Optional, looked up only where they matter: ``STORAGE`` (the
    secrets block it keeps its settings in, default ``notifiers.<kind>``), ``targets(settings)``
    (one recipient per chat), ``MAX_BYTES`` (a limit in UTF-8 bytes), ``CHUNK_PAUSE`` (seconds
    between parts) and ``suggestion(field)`` (a ready value for a form field).
    ``tests/test_plugin_contracts.py`` checks every messenger module against it.
    """

    KIND: str
    TITLE: str  # a product name (or ``notifier.<kind>.title`` in the language files)
    ORDER: int
    MAX_LEN: int  # the longest message; longer ones are split by lines
    STEPS: tuple[str, ...]  # "how to connect", language-file keys
    NOTE: str
    FIELDS: tuple[Field, ...]

    def send(self, settings: dict[str, Any], text: str) -> None:
        """Deliver ``text``; raise ``DeliveryError`` (a reason without secrets) when it fails."""
        ...


def notifier_problems(module: object) -> list[str]:
    """The members of the contract ``module`` lacks (empty: it is a complete messenger).

    ``hasattr``, not ``isinstance``: a module may give a member through its ``__getattr__``
    (ntfy's TITLE comes from the language files), which a Protocol check does not see."""
    missing = [name for name in get_protocol_members(Notifier) if not hasattr(module, name)]
    if hasattr(module, "send") and not callable(module.send):
        missing.append("send (not callable)")
    return sorted(missing)


def http_client() -> httpx.Client:
    """One place to build the client (tests replace it with a fake transport)."""
    from tow.config import as_bool, load_config
    from tow.net_guard import PublicOnlyTransport

    public_only = not as_bool(load_config().get("allow_private_notifier_hosts"))
    return httpx.Client(
        timeout=10.0,
        follow_redirects=False,
        transport=PublicOnlyTransport() if public_only else None,
        trust_env=not public_only,
    )


def backoff_sleep(seconds: float) -> None:
    time.sleep(seconds)


def _retry_after(response: httpx.Response) -> float | None:
    header = response.headers.get("retry-after")
    try:
        if header:
            return float(header)
        body = response.json()
        value = body.get("retry_after") if isinstance(body, dict) else None
        return float(value) if value is not None else None
    except TypeError, ValueError:
        return None


def request(method: str, url: str, *, what: str, **kwargs: Any) -> httpx.Response:
    """One HTTP call; network errors, 429 and 5xx are retried, honouring Retry-After.

    The URL is never put into an error: for webhooks and bots it contains the secret.
    """
    network_error = ""
    busy_code: int | None = None
    for attempt in range(len(RETRY_DELAYS) + 1):
        wait = RETRY_DELAYS[attempt] if attempt < len(RETRY_DELAYS) else 0.0
        try:
            with http_client() as client:
                response = client.request(method, url, **kwargs)
        except httpx.HTTPError as exc:
            network_error, busy_code = type(exc).__name__, None
        else:
            if response.status_code != 429 and response.status_code < 500:
                return response
            network_error, busy_code = "", response.status_code
            asked = _retry_after(response)
            if asked is not None:
                wait = min(max(asked, 0.0), MAX_RETRY_AFTER)
        if attempt < len(RETRY_DELAYS):
            backoff_sleep(wait)
    if busy_code is not None:
        reason = Msg("notifier.common.busy_service", what=what, code=busy_code)
    elif network_error:
        reason = Msg("notifier.common.network_down", what=what, error=network_error)
    else:
        reason = Msg("notifier.common.no_answer", what=what)
    raise DeliveryError(reason, transient=True)


def json_or_empty(response: httpx.Response) -> dict[str, Any]:
    try:
        value = response.json()
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}
