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


def credential_name(key: str) -> bool:
    """A field so named holds a credential: refused outside the encrypted secrets (unless empty)."""
    lowered = key.lower()
    return lowered in _SECRET_FIELD_NAMES or lowered.endswith(_SECRET_FIELD_SUFFIXES)


# A field of that shape TOW itself writes that is not a credential: a messenger delivery's
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


def _legacy_mirror_order(path: list[str | int], key: str, item: Any) -> bool:
    """When a request a mirror's saved answer came from started (a time in seconds), as TOW
    1.31.0 and 1.31.1 named it in state.json (``mirrors.<site>.observed|access.<host>.token``;
    now ``seq``). Exempt only there and only as a number: it blocked every restore point, and so
    the web update, until each mirror answered again."""
    return (
        len(path) == 4
        and path[0] == "mirrors"
        and path[2] in {"observed", "access"}
        and key == "token"
        and isinstance(item, (int, float))
        and not isinstance(item, bool)
    )


_PREPARED_CONTENT_ID = re.compile(r"[0-9a-f]{32}")


def _legacy_content_ref(path: list[str | int], key: str, item: Any) -> bool:
    """The id of a topic's prepared torrent (``tow.content.prepare``, 32 hex digits) while the
    topic waits for its first add, as TOW 1.23.0-1.31.2 named it in state.json
    (``topics[<n>].content_token``, and ``undo.item.content_token`` once such a topic was
    deleted or edited; now ``content_ref``). Exempt only there and only in that form: it blocked
    every restore point, and so the web update and every restore, until the topic's first add."""
    return (
        len(path) == 2
        and ((path[0] == "topics" and isinstance(path[1], int)) or path == ["undo", "item"])
        and key == "content_token"
        and isinstance(item, str)
        and _PREPARED_CONTENT_ID.fullmatch(item) is not None
    )


# Maps whose keys are data, not field names: a site's name (from its domain: my-token.example is
# ``my_token``), a host, a client's id, a messenger, a topic's id, a file's identity
# (``file:<path>``). Their keys are never read as a credential's name - one site or file so named
# refused every restore point, and so the web update, for good - while the fields of what they
# hold still are. Positions per store, from its root; "*" is any key.
_HOST_MAPS = ("observed", "access", "cool", "rate_cool", "fail", "failed_at")
_DATA_KEYED: dict[str, frozenset[tuple[str, ...]]] = {
    "config.yaml": frozenset({("trackers",)}),
    "state.json": frozenset(
        {
            ("mirrors",),
            *(("mirrors", "*", name) for name in _HOST_MAPS),
            *(("undo", "mirror", name) for name in _HOST_MAPS),  # a site's mirror state an undo keeps
            ("daily_limit",),
            ("health", "clients_ok"),
            ("health", "clients_empty"),
            ("health", "ping_failed"),
            ("notify_outbox",),
            ("notify_lease",),
            ("notify_status",),
            ("notify_dropped",),
        }
    ),
    "download_history.json": frozenset({("topics",), ("topics", "*", "items")}),
}


def _data_keyed(store: str, path: list[str | int]) -> bool:
    """The mapping at ``path`` of ``store`` is keyed by data (``_DATA_KEYED``)."""
    return any(
        len(pattern) == len(path)
        and all((want == "*" and isinstance(got, str)) or want == got for want, got in zip(pattern, path, strict=True))
        for pattern in _DATA_KEYED.get(store, ())
    )


def secret_field_path(store: str, data: Any) -> str | None:
    """Path of the first credential-named field in ``data``, the contents of ``store``
    (config.yaml, state.json or download_history.json), or None."""
    return _secret_key_path(data, store=store)


def _secret_key_path(value: Any, path: str = "", *, state: bool = False, store: str = "") -> str | None:
    """Path of the first credential-named field in ``value``, or None (``state``: state.json,
    where an old messenger lease's claim is not one; ``store``: the file ``value`` is, whose
    data-keyed maps are known)."""
    segments = _secret_segments(value, set(), store="state.json" if state else store)
    if segments is None:
        return None
    for segment in segments:
        if isinstance(segment, int):
            path += f"[{segment}]"
        else:
            path += ("." if path else "") + segment
    return path


def _children(item: dict[Any, Any] | list[Any], store: str, path: list[str | int]) -> tuple[bool, bool, Any]:
    """A container's stack entry: (a mapping, its keys are data, its children)."""
    if isinstance(item, dict):
        return True, bool(store) and _data_keyed(store, path), iter(item.items())
    return False, False, iter(enumerate(item))


def _secret_segments(value: Any, seen: set[int], *, store: str = "") -> list[str | int] | None:
    # Shared containers need one scan. Build a path only for the actual match,
    # not one long prefix per innocent child of a large-keyed mapping.
    if not isinstance(value, (dict, list)):
        return None
    state = store == "state.json"
    seen.add(id(value))
    path: list[str | int] = []
    stack = [_children(value, store, path)]
    while stack:
        mapping, data_keys, children = stack[-1]
        try:
            key, item = next(children)
        except StopIteration:
            stack.pop()
            if path:
                path.pop()
            continue
        segment = str(key) if mapping else key
        if (
            mapping
            and not data_keys
            and credential_name(str(key))
            and item not in (None, "", False, True)
            and not (state and _legacy_lease_owner(path, str(key), item))
            and not (state and _legacy_mirror_order(path, str(key), item))
            and not (state and _legacy_content_ref(path, str(key), item))
        ):
            return [*path, segment]
        if isinstance(item, (dict, list)) and id(item) not in seen:
            seen.add(id(item))
            path.append(segment)
            stack.append(_children(item, store, path))
    return None


def refuse_plaintext_secrets(config_data: Any, state_data: Any, history_data: Any) -> None:
    for label, data in (
        ("config.yaml", config_data),
        ("state.json", state_data),
        ("download_history.json", history_data),
    ):
        found = secret_field_path(label, data)
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
