from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import secrets as _secrets
import threading
import time
from pathlib import Path
from typing import Any

from tow.errors import TowError

AUTH_TOKEN_ENV = "TOW_LAN_AUTH_TOKEN"
AUTH_TOKEN_FILE_ENV = "TOW_LAN_AUTH_TOKEN_FILE"
SESSION_COOKIE = "tow_session"
# A device stays signed in for 90 days, across TOW restarts; changing the password signs every
# device out (the signing key is derived from the password record).
SESSION_TTL_SEC = 90 * 24 * 60 * 60
_MIN_TOKEN_LENGTH = 32
_MIN_PASSWORD_LENGTH = 8
_PASSWORD_ITERATIONS = 600_000
_PASSWORD_SALT_BYTES = 16
_PASSWORD_DIGEST_BYTES = 32
MAX_HINT_LENGTH = 120


class AuthConfigurationError(TowError):
    """The password or the LAN authentication token is missing or unusable (``auth.*``)."""


def hash_lan_password(password: str) -> dict[str, str | int]:
    if not isinstance(password, str) or len(password) < _MIN_PASSWORD_LENGTH:
        raise AuthConfigurationError("auth.password_too_short", n=_MIN_PASSWORD_LENGTH)
    salt = _secrets.token_bytes(_PASSWORD_SALT_BYTES)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, _PASSWORD_ITERATIONS, _PASSWORD_DIGEST_BYTES)
    return {
        "scheme": "pbkdf2-sha256",
        "iterations": _PASSWORD_ITERATIONS,
        "salt": base64.urlsafe_b64encode(salt).decode("ascii"),
        "digest": base64.urlsafe_b64encode(digest).decode("ascii"),
    }


def _password_record_parts(record: object) -> tuple[int, bytes, bytes] | None:
    if not isinstance(record, dict) or record.get("scheme") != "pbkdf2-sha256":
        return None
    try:
        iterations = int(record["iterations"])
        salt = base64.urlsafe_b64decode(str(record["salt"]).encode("ascii"))
        digest = base64.urlsafe_b64decode(str(record["digest"]).encode("ascii"))
    except KeyError, TypeError, ValueError, UnicodeError:
        return None
    if not 100_000 <= iterations <= 2_000_000:
        return None
    if len(salt) != _PASSWORD_SALT_BYTES or len(digest) != _PASSWORD_DIGEST_BYTES:
        return None
    return iterations, salt, digest


def lan_password_matches(password: str | None, record: object) -> bool:
    if not isinstance(password, str) or not password:
        return False
    parts = _password_record_parts(record)
    if parts is None:
        return False
    iterations, salt, expected = parts
    actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations, len(expected))
    return hmac.compare_digest(actual, expected)


def password_hint(record: object) -> str:
    """The owner's reminder kept with the password record ("" when none)."""
    hint = record.get("hint") if isinstance(record, dict) else None
    return hint if isinstance(hint, str) else ""


def clean_hint(hint: str, *, password: str) -> str:
    """The reminder as stored: one line, short, and no piece of the password.

    The login page shows it to anyone on the network, so a reminder that shares a run of four
    or more letters and digits with ``password``, forwards or backwards, is refused; they are
    compared without case, punctuation or spaces ("correct horse tow" for "Correct-Horse-Tow",
    "dog2024" for "mydog2024!", "horse tow correct" too); a password of punctuation alone is
    compared as it is, without spaces. A reminder needs the password to be
    compared with (``auth.hint_needs_password`` without it).
    """
    hint = " ".join(str(hint or "").split())
    if len(hint) > MAX_HINT_LENGTH:
        raise AuthConfigurationError("auth.hint_too_long", n=MAX_HINT_LENGTH)
    if not hint:
        return ""
    if not password:
        raise AuthConfigurationError("auth.hint_needs_password")
    if _squeezed(password):
        reveals = _reveals(_squeezed(hint), _squeezed(password))
    else:  # a password of punctuation alone ("__--__--") is compared as it is, without spaces
        reveals = _reveals("".join(hint.split()), "".join(password.split()))
    if reveals:
        raise AuthConfigurationError("auth.hint_reveals_password")
    return hint


_MIN_REVEALING_PART = 4


def _reveals(hint: str, password: str) -> bool:
    """The reminder and the password share a run of four characters (all of a shorter one),
    the password read forwards or backwards."""
    size = min(_MIN_REVEALING_PART, len(password))
    if not hint or not size:
        return False
    runs = {text[i : i + size] for text in (password, password[::-1]) for i in range(len(text) - size + 1)}
    return any(run in hint for run in runs)


def _squeezed(text: str) -> str:
    """Only the letters and digits, without case: punctuation and spaces do not hide a password."""
    return "".join(ch for ch in text.casefold() if ch.isalnum())


def lan_password_record(password: str, hint: str = "") -> dict[str, str | int]:
    """A new password record with its (optional) reminder."""
    record = hash_lan_password(password)
    if cleaned := clean_hint(hint, password=password):
        record["hint"] = cleaned
    return record


def with_hint(record: dict[str, Any], hint: str, *, password: str = "") -> dict[str, Any]:
    """The same password with a new reminder (sessions stay valid: the key is the digest).

    ``password`` is the current password, already checked against ``record`` by the caller: a
    new reminder is compared with it (``clean_hint``). The reminder field comes filled in, so
    most saves send it unchanged: that, and removing the reminder, need no password.
    """
    if " ".join(str(hint or "").split()) == password_hint(record):
        return dict(record)
    updated = {key: value for key, value in record.items() if key != "hint"}
    if cleaned := clean_hint(hint, password=password):
        updated["hint"] = cleaned
    return updated


def lan_password_session_key(record: object) -> str:
    parts = _password_record_parts(record)
    if parts is None:
        raise AuthConfigurationError("auth.record_invalid")
    return base64.urlsafe_b64encode(parts[2]).decode("ascii")


def _default_token_file() -> Path | None:
    """``<data>/lan-auth.token`` when it exists (before 1.21 only the Windows launcher found it)."""
    from tow.paths import lan_auth_token_file

    try:
        path = lan_auth_token_file()
    except RuntimeError:
        return None
    return path if path.exists() or path.is_symlink() else None


def load_lan_auth_token() -> str:
    """The external access key: TOW_LAN_AUTH_TOKEN > the file TOW_LAN_AUTH_TOKEN_FILE names >
    ``<data>/lan-auth.token``. Raises AuthConfigurationError (``auth.*``) without a usable one."""
    value = os.environ.get(AUTH_TOKEN_ENV, "").strip()
    if not value:
        location = os.environ.get(AUTH_TOKEN_FILE_ENV, "").strip()
        path = Path(location) if location else _default_token_file()
        if path is not None:
            if path.is_symlink() or not path.is_file():
                raise AuthConfigurationError("auth.token_file_missing")
            try:
                value = path.read_text(encoding="utf-8").strip()
            except OSError as exc:
                raise AuthConfigurationError("auth.token_file_unreadable") from exc
    if len(value) < _MIN_TOKEN_LENGTH:
        raise AuthConfigurationError("auth.token_missing")
    return value


def token_matches(candidate: str | None, expected: str) -> bool:
    if not candidate:
        return False
    return hmac.compare_digest(candidate.encode("utf-8"), expected.encode("utf-8"))


def _signature(session_id: str, token: str, epoch: int = 0) -> str:
    # Epoch 0 is the key sessions were always signed with, so devices stay signed in after an
    # update; "Sign out everywhere" moves to the next epoch and every older cookie stops matching.
    key = token if epoch == 0 else f"{token}\x00tow-session-epoch-{epoch}"
    digest = hmac.new(key.encode("utf-8"), session_id.encode("ascii"), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


# Sessions are self-contained (id, expiry, signature), so a restart or the watchdog bringing
# TOW back does not sign anyone out. Two small facts live in data/sessions.json, shared by
# every TOW process and kept across restarts: the session epoch ("Sign out everywhere") and
# the sessions signed out one by one (until they would have expired anyway).
_sessions_lock = threading.RLock()
_MAX_REVOKED = 2000  # beyond that, everyone signs in again rather than the file growing
_cache: dict[str, object] = {}


def _sessions_path() -> Path:
    from tow.paths import data_dir

    return data_dir() / "sessions.json"


def _revoked_key(session_id: str) -> str:
    return hashlib.sha256(session_id.encode("ascii")).hexdigest()[:32]


def _sessions_state() -> tuple[int, dict[str, int]]:
    """(epoch, {revoked session key: its expiry}) - read again only when the file changed."""
    path = _sessions_path()
    try:
        stat = path.stat()
        stamp = (str(path), stat.st_mtime_ns, stat.st_size)
        with _sessions_lock:
            if _cache.get("stamp") == stamp:
                return _cache["epoch"], _cache["revoked"]  # type: ignore[return-value]
        raw = path.read_bytes()
    except FileNotFoundError:
        return 0, {}  # never signed out: the original key
    except OSError:
        # Held for a moment by a writer (Windows): the last known state of this file, if any.
        with _sessions_lock:
            if _cache.get("path") == str(path):
                return _cache["epoch"], _cache["revoked"]  # type: ignore[return-value]
        return -1, {}
    try:
        data = json.loads(raw.decode("utf-8"))
        epoch = int(data.get("epoch") or 0)
        revoked = {str(key): int(value) for key, value in dict(data.get("revoked") or {}).items()}
    except UnicodeError, ValueError, TypeError, AttributeError:
        # A damaged file: a session signed in a later epoch no longer matches, so this fails
        # closed for "Sign out everywhere"; single sign-outs are forgotten.
        epoch, revoked = -1, {}
    with _sessions_lock:
        _cache.update(stamp=stamp, path=str(path), epoch=epoch, revoked=revoked)
    return epoch, revoked


def _save_sessions(epoch: int, revoked: dict[str, int]) -> None:
    from tow.store import atomic_write_text

    path = _sessions_path()
    atomic_write_text(path, json.dumps({"epoch": epoch, "revoked": revoked}, indent=1) + "\n")
    with _sessions_lock:
        _cache.clear()
        try:
            stat = path.stat()
        except OSError:
            return
        _cache.update(
            stamp=(str(path), stat.st_mtime_ns, stat.st_size), path=str(path), epoch=epoch, revoked=dict(revoked)
        )


def _signing_epoch() -> int:
    """The epoch a new session is signed with.

    A damaged sessions file (its epoch cannot be read) refuses every session: a sign-in signed
    for epoch 0 was refused at once, so a device was sent back to the sign-in page for ever. The
    file is rewritten first, with an epoch no earlier session was signed with (epochs count up
    one "Sign out everywhere" at a time; this one is the time in seconds), the History says so
    once, and every device signs in again. A file that cannot be read at all raises
    ``auth.sessions_unreadable``: the sign-in page says it instead of a cookie that never works.
    """
    epoch = _sessions_state()[0]
    if epoch >= 0:
        return epoch
    from tow.store import persistence_lock

    with persistence_lock(), _sessions_lock:
        epoch = _sessions_state()[0]
        if epoch >= 0:  # another sign-in repaired it meanwhile
            return epoch
        try:
            raw = _sessions_path().read_bytes()
        except OSError as exc:
            raise AuthConfigurationError("auth.sessions_unreadable") from exc
        # Readable now: the earlier reads may only have met a writer's moment (Windows). A good
        # file is never reset - that signed every device out after a brief sharing clash.
        try:
            good = int(json.loads(raw.decode("utf-8")).get("epoch") or 0)
        except UnicodeError, ValueError, TypeError, AttributeError:
            good = -1
        if good >= 0:
            clear_sessions()  # the next read takes the file as it is
            return good
        epoch = max(1, int(time.time()))
        _save_sessions(epoch, {})
    from tow.log import log_event

    log_event("sessions_reset", file="data/sessions.json", how="auto")
    return epoch


def issue_session(token: str, *, now: float | None = None) -> str:
    if len(token) < _MIN_TOKEN_LENGTH:
        raise AuthConfigurationError("auth.token_missing")
    session_id = _secrets.token_urlsafe(32)
    expires = int((time.time() if now is None else now) + SESSION_TTL_SEC)
    payload = f"{session_id}.{expires}"
    return f"{payload}.{_signature(payload, token, _signing_epoch())}"


def _parts(cookie: str | None) -> tuple[str, int, str] | None:
    if not cookie or len(cookie) > 256:
        return None
    pieces = cookie.split(".")
    if len(pieces) != 3:
        return None
    session_id, expires, signature = pieces
    if not re.fullmatch(r"[A-Za-z0-9_-]{16,128}", session_id) or not expires.isdigit():
        return None
    if not re.fullmatch(r"[A-Za-z0-9_-]{16,128}", signature):
        return None
    return session_id, int(expires), signature


def session_is_valid(cookie: str | None, token: str, *, now: float | None = None) -> bool:
    parts = _parts(cookie)
    if parts is None:
        return False
    session_id, expires, signature = parts
    epoch, revoked = _sessions_state()
    if epoch < 0 or not hmac.compare_digest(signature, _signature(f"{session_id}.{expires}", token, epoch)):
        return False
    timestamp = time.time() if now is None else now
    if expires <= timestamp or expires > timestamp + SESSION_TTL_SEC + 60:
        return False
    return _revoked_key(session_id) not in revoked


def revoke_session(cookie: str | None, token: str, *, now: float | None = None) -> None:
    """Sign one device out (logout): remembered until its cookie would have expired anyway.

    Only a valid session signed with ``token`` is remembered: made-up cookies (anyone may post
    /logout) would otherwise fill the list until everyone is signed out (``_MAX_REVOKED``).
    """
    if not session_is_valid(cookie, token, now=now):
        return
    parts = _parts(cookie)
    if parts is None:
        return
    session_id, expires, _signature_value = parts
    timestamp = time.time() if now is None else now
    from tow.store import persistence_lock

    with persistence_lock(), _sessions_lock:
        epoch, revoked = _sessions_state()
        kept = {key: until for key, until in revoked.items() if until > timestamp}
        kept[_revoked_key(session_id)] = min(expires, int(timestamp + SESSION_TTL_SEC + 60))
        if len(kept) > _MAX_REVOKED:
            epoch, kept = max(0, epoch) + 1, {}
        _save_sessions(max(0, epoch), kept)


def sign_out_everywhere() -> int:
    """Every device signs in again, without changing the password; returns the new epoch."""
    from tow.store import persistence_lock

    with persistence_lock(), _sessions_lock:
        epoch = _sessions_state()[0]
        # A damaged file's epoch is unknown: one no earlier session was signed with (_signing_epoch).
        epoch = epoch + 1 if epoch >= 0 else max(1, int(time.time()))
        _save_sessions(epoch, {})  # every older session is invalid now: nothing to remember
        return epoch


def clear_sessions() -> None:
    """Forget what this process cached (as a restart would); the file stays the truth."""
    with _sessions_lock:
        _cache.clear()
