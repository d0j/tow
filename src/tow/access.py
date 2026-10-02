"""Who may use TOW, and how: one place for the password, sessions and this-computer-only actions.

- This computer (a loopback peer) needs no password. A home program: a device on the network
  that signed in with the password is the owner (owner's decision, 01.10.2026).
- The password record is the ``lan_auth`` entry of the encrypted secrets (PBKDF2, with an
  optional reminder). Setting it - the first-start page, Settings, ``tow password`` - goes
  through ``new_record`` and ``save_password``; a new password signs every device out.
- An external access key (``TOW_LAN_AUTH_TOKEN``, a file named by ``TOW_LAN_AUTH_TOKEN_FILE``, else
  ``data/lan-auth.token``) is an explicit alternative for an install that has no password record at all. It is never a
  fallback: a damaged password record, or secrets that cannot be read, refuse every device on
  the network with a clear reason until the password is set again on this computer.
- Turning network access on or off and the first-start page are for this computer only
  (``require_local``), so nobody on the network can lock the owner out.
"""

from __future__ import annotations

import copy
import ipaddress
from dataclasses import dataclass
from typing import Any

from starlette.requests import Request
from starlette.responses import Response

from tow import auth
from tow.auth import AuthConfigurationError

RECORD_KEY = "lan_auth"  # the password record in the secrets (not the old config flag of that name)


def is_local(request: Request) -> bool:
    """The request comes from this computer."""
    host = request.client.host if request.client else ""
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def network_open(cfg: dict[str, Any]) -> bool:
    """Network access is on: other devices may ask, always with the password."""
    from tow.config import as_bool

    return as_bool(cfg.get("allow_lan"))


def password_record(secrets: object) -> dict[str, Any] | None:
    """The usable password record, or None (none set, or a damaged one)."""
    record = secrets.get(RECORD_KEY) if isinstance(secrets, dict) else None
    try:
        auth.lan_password_session_key(record)
    except AuthConfigurationError:
        return None
    return record if isinstance(record, dict) else None


def password_is_set(secrets: object) -> bool:
    return password_record(secrets) is not None


@dataclass(frozen=True)
class Credential:
    """What a device on the network signs in with: ``kind`` is "password" or "token" (the
    external access key); sessions are signed with ``session_key``."""

    kind: str
    secret: object
    session_key: str

    def matches(self, candidate: str | None) -> bool:
        if self.kind == "password":
            return auth.lan_password_matches(candidate, self.secret)
        return auth.token_matches(candidate, str(self.secret))


def credential(secrets: dict[str, Any] | None) -> Credential:
    """The credential devices on the network sign in with; ``secrets`` None means they could not
    be read. Raises AuthConfigurationError (``auth.*``) when there is none - fail closed."""
    if secrets is None:
        raise AuthConfigurationError("auth.store_unavailable")
    if RECORD_KEY in secrets:  # a password was set: it alone counts, even when it is damaged
        record = secrets[RECORD_KEY]
        return Credential("password", record, auth.lan_password_session_key(record))
    token = auth.load_lan_auth_token()
    return Credential("token", token, token)


def new_record(password: str, repeat: str, hint: str = "") -> dict[str, Any]:
    """A checked password record with its reminder (both typed fields must agree)."""
    if password != repeat:
        raise AuthConfigurationError("web.password.differ")
    return auth.lan_password_record(password, hint)


def save_password(secrets: dict[str, Any], record: dict[str, Any]) -> dict[str, Any]:
    """Store ``record`` as the password (the caller holds the persistence lock); the first-start
    page is done from now on. Returns the secrets as saved."""
    from tow.config import as_bool, load_config, save_config
    from tow.store import save_secrets

    updated = copy.deepcopy(secrets)
    updated[RECORD_KEY] = record
    save_secrets(updated)
    cfg = load_config()
    if not as_bool(cfg.get("setup_done")):
        cfg["setup_done"] = True
        save_config(cfg)
    return updated


def set_password(password: str, repeat: str, hint: str = "") -> dict[str, Any]:
    """A new password, as ``tow password`` and the first-start page set it: checked, stored, and
    every device signed out. Raises AuthConfigurationError or SecretStoreError."""
    from tow.store import load_secrets, persistence_lock

    record = new_record(password, repeat, hint)
    with persistence_lock():
        save_password(load_secrets(), record)
        sign_out_everywhere()
    return record


def set_session_cookie(response: Response, request: Request, session_key: str) -> None:
    """Sign this device in: a session cookie valid for ``auth.SESSION_TTL_SEC``."""
    response.set_cookie(
        auth.SESSION_COOKIE,
        auth.issue_session(session_key),
        max_age=auth.SESSION_TTL_SEC,
        httponly=True,
        samesite="lax",
        secure=request.url.scheme == "https",
        path="/",
    )


def session_is_valid(request: Request, session_key: str) -> bool:
    return auth.session_is_valid(request.cookies.get(auth.SESSION_COOKIE), session_key)


def sign_out(request: Request, response: Response) -> None:
    """This device signs out: its session is revoked and its cookie removed."""
    auth.revoke_session(request.cookies.get(auth.SESSION_COOKIE))
    response.delete_cookie(auth.SESSION_COOKIE, path="/")


def sign_out_everywhere(
    request: Request | None = None, response: Response | None = None, *, session_key: str | None = None
) -> None:
    """Every device signs in again (sessions of earlier epochs stop matching, also if an old
    password comes back by an undo). The device on the network that asked stays signed in with
    a new session signed by ``session_key``."""
    auth.sign_out_everywhere()
    if request is not None and response is not None and session_key and not is_local(request):
        set_session_cookie(response, request, session_key)


class LocalOnly(Exception):
    """A this-computer-only route was asked from the network (tow.web answers with a redirect)."""

    def __init__(self, location: str, message: str = "", kind: str = "err") -> None:
        super().__init__(location)
        self.location = location
        self.message = message
        self.kind = kind


def require_local(location: str = "/", message: str = "", kind: str = "err") -> Any:
    """A FastAPI dependency for this-computer-only routes: from the network the request goes to
    ``location`` (with ``message``, a catalog key, shown once) and the route never runs."""
    from fastapi import Depends

    def only_this_computer(request: Request) -> None:
        if not is_local(request):
            raise LocalOnly(location, message, kind)

    return Depends(only_this_computer)
