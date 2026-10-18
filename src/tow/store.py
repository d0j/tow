"""The state and the download history: load, save, validation, the parsed cache.

``STATE_SCHEMA_VERSION`` stays a literal in this file: ``scripts/update.py`` reads it from the
text of the version it installs. What the stores are built on lives in ``tow.storage``; the rest
of TOW imports it from here (``__all__``).
"""

from __future__ import annotations

import hashlib
import json
import os
import pickle
import threading
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from tow.errors import TowError
from tow.paths import download_history_path
from tow.paths import state_path as state_path  # noqa: PLC0414 - re-exported: the web package imports it from here
from tow.storage.atomic import (
    RegularReadError,
    atomic_create_bytes,
    atomic_write_bytes,
    atomic_write_text,
    read_regular_bytes,
    replace_with_retry,
    restrict_file,
    write_generation,
)
from tow.storage.errors import SecretStoreError, StoreCorruptionError
from tow.storage.folders import install_folders, protect_install_folders
from tow.storage.jsonio import JSON_MAX_DEPTH, decode_json_bytes, encode_json
from tow.storage.keys import (
    KEY_SOURCES,
    MASTER_KEY_KINDS,
    InvalidMasterKeyPathError,
    MasterKeyError,
    MissingMasterKeyError,
    derive_local_secret,
    master_fernet,
    master_key_file,
)
from tow.storage.locks import (
    RECOVERY_STEPS,
    STALE_TEMPORARY_AGE_SEC,
    CheckBusyError,
    check_run_lock,
    init_lock_file,
    is_temporary_of,
    persistence_lock,
    remove_stale_temporaries,
)
from tow.storage.secrets import (
    STORAGE_STATES,
    adopt_master_key,
    decrypt_secret_undo_bytes,
    decrypt_secrets_bytes,
    delete_secret_undo,
    encrypt_secrets_bytes,
    encrypted_secrets_path,
    generate_master_key,
    load_secret_undo,
    load_secrets,
    migrate_legacy_secrets,
    save_secret_undo,
    save_secrets,
    secret_store_status,
    secret_undo_path,
)

# The public names of the stores: what is defined here, and what tow.storage defines and the
# rest of TOW imports from here.
__all__ = [
    "JSON_MAX_DEPTH",
    "KEY_SOURCES",
    "MASTER_KEY_KINDS",
    "RECOVERY_STEPS",
    "STALE_TEMPORARY_AGE_SEC",
    "STATE_SCHEMA_VERSION",
    "STORAGE_STATES",
    "CheckBusyError",
    "FileStamp",
    "InvalidMasterKeyPathError",
    "MasterKeyError",
    "MissingMasterKeyError",
    "RegularReadError",
    "SecretStoreError",
    "StateVersionError",
    "StoreCorruptionError",
    "StoreReadError",
    "StoreWriteError",
    "adopt_master_key",
    "atomic_create_bytes",
    "atomic_write_bytes",
    "atomic_write_text",
    "check_run_lock",
    "check_state_version",
    "decode_json_bytes",
    "decrypt_secret_undo_bytes",
    "decrypt_secrets_bytes",
    "delete_secret_undo",
    "derive_local_secret",
    "download_history_from_bytes",
    "encrypt_secrets_bytes",
    "encrypted_secrets_path",
    "file_stamp",
    "forget_parsed",
    "generate_master_key",
    "init_lock_file",
    "install_folders",
    "is_temporary_of",
    "load_download_history",
    "load_json",
    "load_secret_undo",
    "load_secrets",
    "load_state",
    "master_fernet",
    "master_key_file",
    "migrate_legacy_secrets",
    "persistence_lock",
    "protect_install_folders",
    "read_regular_bytes",
    "remove_stale_temporaries",
    "replace_with_retry",
    "restrict_file",
    "save_download_history",
    "save_json",
    "save_secret_undo",
    "save_secrets",
    "save_state",
    "secret_store_status",
    "secret_undo_path",
    "state_from_bytes",
    "state_schema_version",
    "validate_download_history_bytes",
    "validate_state_bytes",
    "write_generation",
]


class StoreReadError(StoreCorruptionError):
    """A store could not be read right now (I/O error); the file itself is left alone."""


class StoreWriteError(OSError):
    """A store could not be written right now (another program keeps it open, the disk is
    full); the store is as it was. Raised by the web pages' single-file saves."""


class StateVersionError(TowError, StoreCorruptionError):
    """state.json has a data version this TOW does not know (``store.*``): it is neither read
    nor written, so a newer TOW's data is never corrupted by an older one."""


# The data version of state.json this TOW writes. A TOW that finds a newer one refuses to
# start; an older one (or none: TOW 1.18 and before) is brought up to date in _migrate_state.
STATE_SCHEMA_VERSION = 2


def _quarantine(path: Path) -> Path:
    stamp = time.strftime("%Y%m%d-%H%M%S")
    quarantine = path.with_name(f"{path.name}.corrupt-{stamp}-{os.getpid()}-{threading.get_ident()}")
    try:
        path.replace(quarantine)
    except OSError as exc:
        raise StoreCorruptionError(f"persisted JSON is unreadable: {path.name}") from exc
    return quarantine


def _has_quarantined_copy(path: Path) -> bool:
    prefix = f"{path.name}.corrupt-"
    try:
        return any(entry.name.startswith(prefix) for entry in path.parent.iterdir())
    except OSError:
        return False


def _validated_json(raw: bytes, validate: Callable[[Any], None] | None) -> Any:
    value = decode_json_bytes(raw)
    if validate is not None:
        validate(value)
    return value


def load_json(
    path: Path, default: Any, *, quarantine: bool = True, validate: Callable[[Any], None] | None = None
) -> Any:
    if not path.is_file():
        if _has_quarantined_copy(path):
            # Never fall back to an empty default after quarantine: the next commit
            # would silently persist "no topics" over the user's data.
            raise StoreCorruptionError(
                f"{path.name} was quarantined as corrupt; restore it from a restore point or backup"
            )
        return default
    try:
        raw = path.read_bytes()
    except OSError as exc:
        # Transient (sharing violation, antivirus, permissions): not corruption.
        raise StoreReadError(f"persisted JSON cannot be read now: {path.name}") from exc
    try:
        return _validated_json(raw, validate)
    except (UnicodeError, ValueError, RecursionError) as exc:
        if quarantine:
            # A lock-free reader may have caught a moment a writer replaced the file: look
            # again under the lock and set aside only a file that is still unreadable.
            with persistence_lock():
                try:
                    reread = path.read_bytes()
                except OSError as read_exc:
                    # A transient lock is not evidence of corruption. In particular,
                    # never quarantine a good replacement we could not read yet.
                    raise StoreReadError(f"persisted JSON cannot be read now: {path.name}") from read_exc
                try:
                    return _validated_json(reread, validate)
                except UnicodeError, ValueError, RecursionError:
                    if path.is_file():
                        _quarantine(path)
        raise StoreCorruptionError(f"persisted JSON is unreadable: {path.name}") from exc


def save_json(path: Path, data: Any, *, compact: bool = False) -> None:
    try:
        content = encode_json(data, compact=compact) + b"\n"
    except (TypeError, ValueError, UnicodeError, RecursionError) as exc:
        raise StoreCorruptionError(f"JSON payload is not serializable: {path.name}") from exc
    atomic_write_bytes(path, content)


type FileStamp = tuple[str, int, int, int]


def file_stamp(path: Path) -> FileStamp | None:
    """(path, mtime, size, file id): changes with every write of a file TOW replaces atomically."""
    try:
        stat = path.stat()
    except OSError:
        return None
    return (str(path), stat.st_mtime_ns, stat.st_size, stat.st_ino)


# The state and the download history are read by every page and every check: parse them again
# only when the file changed. Writers replace them atomically, so (mtime, size, file id) changes
# with every write (as for the config). Each caller gets its own copy, made from a pickle of the
# parsed data: several times faster than parsing the JSON again. The pickle never leaves this
# process.
_PARSED_LOCK = threading.Lock()
_parsed: dict[str, tuple[FileStamp, bytes]] = {}


def _parsed_copy(stamp: FileStamp | None) -> Any:
    """A private copy of the store parsed at ``stamp``, or None when it changed since."""
    if stamp is None:
        return None
    with _PARSED_LOCK:
        cached = _parsed.get(stamp[0])
    return pickle.loads(cached[1]) if cached is not None and cached[0] == stamp else None


def _keep_parsed(path: Path, stamp: FileStamp | None, data: Any) -> None:
    if stamp is not None and file_stamp(path) == stamp:  # not replaced while it was read
        with _PARSED_LOCK:
            _parsed[stamp[0]] = (stamp, pickle.dumps(data, protocol=pickle.HIGHEST_PROTOCOL))


def state_schema_version(data: Mapping[str, Any]) -> int:
    """The data version of a loaded state.json (0: written before versions existed); a version
    this TOW cannot use raises ``StateVersionError``."""
    version = data.get("schema_version", 0)
    if isinstance(version, bool) or not isinstance(version, int) or version < 0:
        raise StateVersionError("store.state_bad_version", found=str(version)[:20])
    if version > STATE_SCHEMA_VERSION:
        raise StateVersionError("store.state_newer", found=version, known=STATE_SCHEMA_VERSION)
    return version


def _migrate_state(data: dict[str, Any], version: int) -> dict[str, Any]:
    """Bring a state written with data ``version`` up to the current one: the only place that
    knows older layouts (each step from N to N + 1, in order, when the layout changes)."""
    if version < 1:
        pass  # 0 -> 1 (TOW 1.19): only the version field is new; the layout is the same.
    return data


def _validate_state_container(value: Any) -> None:
    mapping = isinstance(value, dict)
    if not mapping:
        raise ValueError("state JSON must be an object")
    # A future data version is not corruption: preserve it untouched and report
    # the version error before applying this version's container requirements.
    state_schema_version(value)
    topics = value.get("topics", [])
    valid = isinstance(topics, list) and all(isinstance(topic, dict) for topic in topics)
    if not valid or not isinstance(value.get("mirrors", {}), dict):
        raise ValueError("state JSON has malformed containers")
    from tow.topic_timers import interval_of

    for topic in topics:
        try:
            interval_of(topic)
        except TowError as exc:
            raise ValueError("invalid topic check interval") from exc
        selection = topic.get("selection")
        if isinstance(selection, dict) and selection.get("mode") == "exact":
            _check_exact_selection(topic, selection)


# Exact selections proven in this process, by a digest of everything their check reads: a state is
# checked on every read and write, and a selection of 20 000 files takes about 150 ms to check
# file by file, its digest about 8 ms. Bounded; a digest that is not here is checked in full.
_PROVEN_SELECTIONS: dict[bytes, None] = {}
_PROVEN_SELECTIONS_MAX = 1024


def _check_exact_selection(topic: Mapping[str, Any], selection: Mapping[str, Any]) -> None:
    from tow.selection import policy_from_topic

    try:
        digest: bytes | None = hashlib.sha256(
            json.dumps([selection, topic.get("tracking_mode", "watch")], ensure_ascii=False).encode()
        ).digest()
    except TypeError, ValueError, UnicodeError, RecursionError:
        digest = None  # not JSON: the full check (and the writer) refuses it
    if digest is not None and digest in _PROVEN_SELECTIONS:
        return
    try:
        policy_from_topic(topic)
    except TowError as exc:
        raise ValueError("invalid exact file selection") from exc
    if digest is not None:
        with _PARSED_LOCK:
            if len(_PROVEN_SELECTIONS) >= _PROVEN_SELECTIONS_MAX:
                _PROVEN_SELECTIONS.clear()
            _PROVEN_SELECTIONS[digest] = None


def _validate_history_container(value: Any) -> None:
    mapping = isinstance(value, dict)
    if not mapping:
        raise ValueError("download history JSON must be an object")
    topics = value.get("topics", {})
    valid = isinstance(topics, dict) and all(isinstance(record, dict) for record in topics.values())
    if not valid:
        raise ValueError("download history JSON has malformed topics")
    for record in topics.values():
        items = record.get("items", {})
        valid = isinstance(items, dict) and all(isinstance(item, dict) for item in items.values())
        if not valid:
            raise ValueError("download history JSON has malformed items")


def validate_state_bytes(content: bytes) -> None:
    """Check the live-store read contract without migration, quarantine or other writes."""
    _validated_json(content, _validate_state_container)


def validate_download_history_bytes(content: bytes) -> None:
    """Check history by the same parser and containers as its live reader."""
    _validated_json(content, _validate_history_container)


def _state_as_read(data: dict[str, Any]) -> dict[str, Any]:
    version = state_schema_version(data)
    data.pop("schema_version", None)  # a file-format detail: callers see the topics, not it
    data = _migrate_state(data, version)
    data.setdefault("topics", [])
    data.setdefault("mirrors", {})
    undo = data.get("undo")
    if isinstance(undo, dict) and "secrets" in undo:
        data.pop("undo", None)
    return data


def load_state(*, quarantine: bool = True) -> dict[str, Any]:
    path = state_path()
    stamp = file_stamp(path)
    cached: dict[str, Any] | None = _parsed_copy(stamp)
    if cached is not None:
        return cached
    data = load_json(path, {"topics": [], "mirrors": {}}, quarantine=quarantine, validate=_validate_state_container)
    data = _state_as_read(data)
    _keep_parsed(path, stamp, data)
    return data


def state_from_bytes(content: bytes | None) -> dict[str, Any]:
    """The state as ``load_state`` returns it, from the bytes of state.json (None: no file) - for
    a reader that took the bytes itself and must write nothing (no quarantine, no cache).
    Raises ValueError (also UnicodeError, RecursionError) for bytes it cannot use."""
    if content is None:
        return _state_as_read({"topics": [], "mirrors": {}})
    data = _validated_json(content, _validate_state_container)
    return _state_as_read(data)


def download_history_from_bytes(content: bytes | None) -> dict[str, Any]:
    """The download history as ``load_download_history`` returns it, from its bytes (None: no file)."""
    data: dict[str, Any] = (
        {"schema_version": 1, "topics": {}}
        if content is None
        else _validated_json(content, _validate_history_container)
    )
    data.setdefault("schema_version", 1)
    data.setdefault("topics", {})
    return data


def forget_parsed(path: Path) -> None:
    """Drop the parsed copy of ``path``: its content changed although its stamp did not (a coarse
    clock, a same-size rewrite in the same tick). The next read parses the file again."""
    with _PARSED_LOCK:
        _parsed.pop(str(path), None)


def check_state_version() -> None:
    """Refuse to start on a state.json written by a newer TOW (``StateVersionError`` with the
    reason in words); nothing is read further or written."""
    path = state_path()
    try:
        data = load_json(path, {}, quarantine=False) if path.is_file() else {}
    except StoreCorruptionError:
        return  # an unreadable file is handled where it is read (quarantine, restore point)
    if isinstance(data, dict):
        state_schema_version(data)


def save_state(data: dict[str, Any]) -> None:
    undo = data.get("undo")
    if isinstance(undo, dict) and "secrets" in undo:
        raise SecretStoreError("plaintext secret undo is not permitted")
    payload = {"schema_version": STATE_SCHEMA_VERSION, **{k: v for k, v in data.items() if k != "schema_version"}}
    try:
        _validate_state_container(payload)
    except ValueError as exc:
        raise StoreCorruptionError("state JSON payload has malformed containers") from exc
    save_json(state_path(), payload)


def load_download_history(*, quarantine: bool = True) -> dict[str, Any]:
    path = download_history_path()
    stamp = file_stamp(path)
    cached: dict[str, Any] | None = _parsed_copy(stamp)
    if cached is not None:
        return cached
    data: dict[str, Any] = load_json(
        path,
        {"schema_version": 1, "topics": {}},
        quarantine=quarantine,
        validate=_validate_history_container,
    )
    data.setdefault("schema_version", 1)
    data.setdefault("topics", {})
    _keep_parsed(path, stamp, data)
    return data


def save_download_history(data: Mapping[str, Any]) -> None:
    try:
        _validate_history_container(data)
    except ValueError as exc:
        raise StoreCorruptionError("download history JSON payload has malformed containers") from exc
    # Machine data rewritten twice per apply-check: compact (~22% smaller than indent=2).
    save_json(download_history_path(), data, compact=True)
