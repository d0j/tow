"""No password or token outside the encrypted secrets: the scan for credential-named fields in
config.yaml, state.json and the download history, and the check that an exported event log is
redacted."""

from __future__ import annotations

import re
from typing import Any

from tow.bundle.errors import ExportImportError
from tow.log import EXPORT_EVENT_KEYS

# Field names that hold credentials. Matched exactly (plus the suffixes below) - a substring
# match also refused TOW's own metadata (secret_scope, secret_undo_ref, secrets_ref, ...)
# and blocked every export and restore point after a settings save.
_SECRET_FIELD_NAMES = frozenset(
    {
        "password",
        "passwd",
        "pass",
        "token",
        "secret",
        "api_key",
        "cookie",
        "cookies",
        "cookies_by_origin",
        "uid",
        "master_key",
    }
)
_SECRET_FIELD_SUFFIXES = ("_password", "_passwd", "_token", "_cookies", "_api_key")

# The one field of that shape TOW itself writes that is not a credential: a messenger delivery's
# claim in state.json (``notify_lease.<recipient>.token``, "<pid>:<thread>:<12 hex>"), as TOW
# 1.28 and older named it (now ``owner``). Exempt only there and only in that exact form: a
# lease left by an older version blocked every restore point, and so the web update, while a
# messenger could not be reached.
_LEGACY_LEASE_OWNER = re.compile(r"[0-9]{1,10}:[0-9]{1,20}:[0-9a-f]{12}")


def _legacy_lease_owner(path: list[str | int], key: str, item: Any) -> bool:
    return (
        len(path) == 2
        and path[0] == "notify_lease"
        and key == "token"
        and isinstance(item, str)
        and _LEGACY_LEASE_OWNER.fullmatch(item) is not None
    )


def _secret_key_path(value: Any, path: str = "", *, state: bool = False) -> str | None:
    """Path of the first credential-named field in ``value``, or None (``state``: state.json,
    where an old messenger lease's claim is not one)."""
    segments = _secret_segments(value, set(), state=state)
    if segments is None:
        return None
    for segment in segments:
        if isinstance(segment, int):
            path += f"[{segment}]"
        else:
            path += ("." if path else "") + segment
    return path


def _secret_segments(value: Any, seen: set[int], *, state: bool = False) -> list[str | int] | None:
    # Shared containers need one scan. Build a path only for the actual match,
    # not one long prefix per innocent child of a large-keyed mapping.
    if not isinstance(value, (dict, list)):
        return None
    seen.add(id(value))
    stack = [(isinstance(value, dict), iter(value.items()) if isinstance(value, dict) else iter(enumerate(value)))]
    path: list[str | int] = []
    while stack:
        mapping, children = stack[-1]
        try:
            key, item = next(children)
        except StopIteration:
            stack.pop()
            if path:
                path.pop()
            continue
        segment = str(key) if mapping else key
        if mapping:
            lowered = str(key).lower()
            is_secret_name = lowered in _SECRET_FIELD_NAMES or lowered.endswith(_SECRET_FIELD_SUFFIXES)
            if (
                is_secret_name
                and item not in (None, "", False, True)
                and not (state and _legacy_lease_owner(path, str(key), item))
            ):
                return [*path, segment]
        if isinstance(item, (dict, list)) and id(item) not in seen:
            seen.add(id(item))
            path.append(segment)
            stack.append(
                (isinstance(item, dict), iter(item.items()) if isinstance(item, dict) else iter(enumerate(item)))
            )
    return None


def refuse_plaintext_secrets(config_data: Any, state_data: Any, history_data: Any) -> None:
    for label, data in (
        ("config.yaml", config_data),
        ("state.json", state_data),
        ("download_history.json", history_data),
    ):
        found = _secret_key_path(data, state=label == "state.json")
        if found:
            raise ExportImportError(f"plaintext secret-shaped field outside encrypted payload: {label}:{found}")


def events_are_redacted(value: Any) -> bool:
    if not isinstance(value, dict) or set(value) != {"events"} or not isinstance(value["events"], list):
        return False
    for event in value["events"]:
        if not isinstance(event, dict) or not set(event).issubset(EXPORT_EVENT_KEYS):
            return False
        for item in event.values():
            if item is not None and not isinstance(item, (str, int, float, bool)):
                return False
    return True
