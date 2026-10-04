from __future__ import annotations

import base64
import binascii
import contextlib
import copy
import hashlib
import io
import json
import logging
import math
import os
import re
import shutil
import stat
import uuid
import zipfile
from collections.abc import Iterable
from pathlib import Path, PurePosixPath
from typing import Any

import yaml

from tow import __version__
from tow.clock import iso_now
from tow.config import ConfigError, validated
from tow.log import EXPORT_EVENT_KEYS, export_event_projection, log_event, read_events
from tow.paths import config_path, data_dir, download_history_path, secrets_path, state_path
from tow.store import (
    SecretStoreError,
    StateVersionError,
    StoreCorruptionError,
    atomic_create_bytes,
    atomic_write_bytes,
    decode_json_bytes,
    encrypted_secrets_path,
    load_json,
    load_secret_undo,
    load_secrets,
    load_state,
    master_fernet,
    persistence_lock,
    save_secret_undo,
    save_secrets,
    secret_undo_path,
    state_schema_version,
)

FORMAT = "tow-export-v1"
CHECKPOINT_FORMAT = "tow-import-checkpoint-v1"
TRANSACTION_FORMAT = "tow-import-transaction-v1"
KDF_NAME = "pbkdf2-hmac-sha256"
KDF_ITERATIONS = 600_000
KDF_SALT_BYTES = 16
MAX_BUNDLE_BYTES = 64 * 1024 * 1024
MAX_MEMBER_BYTES = 16 * 1024 * 1024
REQUIRED_MEMBERS = ("config.yaml", "state.json", "download_history.json", "secrets.json")
OPTIONAL_MEMBERS = ("secrets_undo.json", "events.json")
ALLOWED_MEMBERS = {"manifest.json", *REQUIRED_MEMBERS, *OPTIONAL_MEMBERS}
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
_DRIVE_PATH = re.compile(r"^[A-Za-z]:[\\/]")


class ExportImportError(RuntimeError):
    """Raised when a portable TOW export/import cannot be handled safely."""


def _json_bytes(data: Any) -> bytes:
    try:
        return (json.dumps(data, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n").encode("utf-8")
    except (TypeError, ValueError, UnicodeError, RecursionError) as exc:
        raise ExportImportError("portable TOW data is not JSON serializable") from exc


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _read_limited(path: Path, *, label: str) -> bytes:
    try:
        size = path.stat().st_size
        if size > MAX_BUNDLE_BYTES:
            raise ExportImportError(f"{label} is too large")
        with path.open("rb") as handle:
            content = handle.read(MAX_BUNDLE_BYTES + 1)
        if len(content) > MAX_BUNDLE_BYTES:
            raise ExportImportError(f"{label} is too large")
        return content
    except ExportImportError:
        raise
    except OSError as exc:
        raise ExportImportError(f"cannot read {label}") from exc


def _restrict_file(path: Path) -> None:
    with contextlib.suppress(OSError):
        path.chmod(0o600)


def _atomic_write(path: Path, content: bytes) -> None:
    try:
        atomic_write_bytes(path, content)
    except OSError as exc:
        raise ExportImportError(f"cannot write imported {path.name}") from exc


def _secret_key_path(value: Any, path: str = "") -> str | None:
    """Path of the first credential-named field in ``value``, or None."""
    if isinstance(value, dict):
        for key, item in value.items():
            lowered = str(key).lower()
            child = f"{path}.{key}" if path else str(key)
            is_secret_name = lowered in _SECRET_FIELD_NAMES or lowered.endswith(_SECRET_FIELD_SUFFIXES)
            if is_secret_name and item not in (None, "", False, True):
                return child
            found = _secret_key_path(item, child)
            if found:
                return found
    elif isinstance(value, list):
        for index, item in enumerate(value):
            found = _secret_key_path(item, f"{path}[{index}]")
            if found:
                return found
    return None


def _contains_secret_keys(value: Any) -> bool:
    return _secret_key_path(value) is not None


def _refuse_plaintext_secrets(config_data: Any, state_data: Any, history_data: Any) -> None:
    for label, data in (
        ("config.yaml", config_data),
        ("state.json", state_data),
        ("download_history.json", history_data),
    ):
        found = _secret_key_path(data)
        if found:
            raise ExportImportError(f"plaintext secret-shaped field outside encrypted payload: {label}:{found}")


def _events_are_redacted(value: Any) -> bool:
    if not isinstance(value, dict) or set(value) != {"events"} or not isinstance(value["events"], list):
        return False
    for event in value["events"]:
        if not isinstance(event, dict) or not set(event).issubset(EXPORT_EVENT_KEYS):
            return False
        for item in event.values():
            if item is not None and not isinstance(item, (str, int, float, bool)):
                return False
    return True


def _parse_mapping(data: bytes, *, label: str) -> dict[str, Any]:
    try:
        value = yaml.safe_load(data) if label == "config.yaml" else json.loads(data.decode("utf-8"))
    except (UnicodeError, ValueError, RecursionError, yaml.YAMLError) as exc:
        raise ExportImportError(f"invalid {label}") from exc
    if not isinstance(value, dict):
        raise ExportImportError(f"{label} must be an object")
    return value


def _validate_tree(value: Any, *, label: str, depth: int = 0) -> None:
    if depth > 32:
        raise ExportImportError(f"{label} is nested too deeply")
    if isinstance(value, float) and not math.isfinite(value):
        raise ExportImportError(f"{label} contains a non-finite number")
    if value is None or isinstance(value, (str, int, float, bool)):
        return
    if isinstance(value, list):
        for item in value:
            _validate_tree(item, label=label, depth=depth + 1)
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise ExportImportError(f"{label} contains a non-string key")
            _validate_tree(item, label=label, depth=depth + 1)
        return
    raise ExportImportError(f"{label} contains an unsupported value type")


def _validate_field(mapping: dict[str, Any], key: str, expected: type | tuple[type, ...], *, label: str) -> None:
    if key not in mapping or mapping[key] is None:
        return
    value = mapping[key]
    if isinstance(value, bool) and (expected is int or (isinstance(expected, tuple) and int in expected)):
        raise ExportImportError(f"{label}.{key} has an invalid type")
    if not isinstance(value, expected):
        raise ExportImportError(f"{label}.{key} has an invalid type")


def _validate_config_schema(data: dict[str, Any]) -> None:
    """config.yaml of a bundle: the schema ``load_config`` applies (``tow.config.validated``),
    plus the shape of each site's settings an import must not let through."""
    _validate_tree(data, label="config.yaml")
    if isinstance(data.get("trackers"), dict):  # named per site below, before the shared check
        _validate_tracker_settings(data["trackers"])
    try:
        validated(data)
    except ConfigError as exc:
        raise ExportImportError(f"invalid {exc}") from None


def _validate_tracker_settings(trackers: dict[str, Any]) -> None:
    for name, tracker in trackers.items():
        if not isinstance(name, str) or not isinstance(tracker, dict):
            raise ExportImportError("config.yaml tracker entry has an invalid type")
        label = f"config.yaml.trackers.{name}"
        for key in (
            "title",
            "url_regex",
            "download_href_regex",
            "topic_path",
            "download_path",
            "login_path",
            "search_path",
        ):
            _validate_field(tracker, key, str, label=label)
        _validate_field(tracker, "browser_auth", bool, label=label)
        for key in ("login_hosts", "fetch_hosts", "cookie_names"):
            if key in tracker and (
                not isinstance(tracker[key], list) or not all(isinstance(item, str) for item in tracker[key])
            ):
                raise ExportImportError(f"{label}.{key} has an invalid type")
        _validate_field(tracker, "page_download", bool, label=label)
        form = tracker.get("login_form")
        if form is not None:
            if not isinstance(form, dict) or set(form) - {"user_field", "pw_field", "extra"}:
                raise ExportImportError(f"{label}.login_form has an invalid shape")
            for key in ("user_field", "pw_field"):
                _validate_field(form, key, str, label=f"{label}.login_form")
            extra = form.get("extra")
            if extra is not None and (
                not isinstance(extra, dict)
                or not all(isinstance(k, str) and isinstance(v, str) for k, v in extra.items())
            ):
                raise ExportImportError(f"{label}.login_form.extra has an invalid type")
        for key in ("fail_threshold", "cooldown_sec"):
            _validate_field(tracker, key, int, label=label)


def _validate_state_schema(data: dict[str, Any]) -> None:
    _validate_tree(data, label="state.json")
    topics = data.get("topics")
    mirrors = data.get("mirrors")
    if not isinstance(topics, list) or not isinstance(mirrors, dict):
        raise ExportImportError("state.json has an unsupported schema")
    try:
        state_schema_version(data)  # a newer TOW's state is never imported into this one
    except StateVersionError as exc:
        raise ExportImportError(f"state.json: {exc}") from exc
    for index, topic in enumerate(topics):
        _validate_state_topic(index, topic)
    for name, mirror in mirrors.items():
        _validate_state_mirror(name, mirror)
    for key in ("health", "doctor"):
        if key in data and data[key] is not None and not isinstance(data[key], dict):
            raise ExportImportError(f"state.json.{key} has an invalid type")
    if data.get("undo") is not None and not isinstance(data["undo"], dict):
        raise ExportImportError("state.json.undo has an invalid type")


def _validate_state_topic(index: int, topic: Any) -> None:
    if not isinstance(topic, dict):
        raise ExportImportError(f"state.json topic {index} has an invalid type")
    label = f"state.json.topic[{index}]"
    for key in (
        "id",
        "title",
        "url",
        "save_path",
        "hash",
        "client_id",
        "tracker_title",
        "tracking_mode",
        "selection_hash",
    ):
        _validate_field(topic, key, str, label=label)
    for key in (
        "paused",
        "once_done",
        "selection_dirty",
        "selection_verified",
        "selected_files_truncated",
        "error_notified",
    ):
        _validate_field(topic, key, bool, label=label)
    for key in ("selected_file_count", "torrent_file_count"):
        _validate_field(topic, key, int, label=label)
    for key in ("previous_hashes", "selected_files", "selected_episode_keys"):
        if key in topic and (not isinstance(topic[key], list) or not all(isinstance(item, str) for item in topic[key])):
            raise ExportImportError(f"{label}.{key} has an invalid type")
    selection = topic.get("selection")
    if selection is not None:
        if not isinstance(selection, dict):
            raise ExportImportError(f"{label}.selection has an invalid type")
        for key in ("mode", "value"):
            _validate_field(selection, key, str, label=f"{label}.selection")


def _validate_state_mirror(name: Any, mirror: Any) -> None:
    if not isinstance(name, str) or not isinstance(mirror, dict):
        raise ExportImportError("state.json mirror entry has an invalid type")
    if "active" in mirror and mirror["active"] is not None and not isinstance(mirror["active"], str):
        raise ExportImportError(f"state.json mirror {name}.active has an invalid type")
    if "frozen" in mirror and not isinstance(mirror["frozen"], bool):
        raise ExportImportError(f"state.json mirror {name}.frozen has an invalid type")
    for field in ("fail", "cool"):
        values = mirror.get(field, {})
        if not isinstance(values, dict):
            raise ExportImportError(f"state.json mirror {name}.{field} has an invalid type")
        for host, value in values.items():
            if (
                not isinstance(host, str)
                or not isinstance(value, (int, float))
                or isinstance(value, bool)
                or not math.isfinite(value)
                or value < 0
                or (field == "fail" and (not isinstance(value, int) or value < 0))
            ):
                raise ExportImportError(f"state.json mirror {name}.{field} has an invalid value")


def _validate_history_schema(data: dict[str, Any]) -> None:
    _validate_tree(data, label="download_history.json")
    version = data.get("schema_version")
    if not isinstance(version, int) or isinstance(version, bool):
        raise ExportImportError("download_history.json.schema_version has an invalid type")
    topics = data.get("topics")
    if not isinstance(topics, dict):
        raise ExportImportError("download_history.json.topics has an invalid type")
    for topic_id, record in topics.items():
        if not isinstance(topic_id, str) or not isinstance(record, dict):
            raise ExportImportError("download_history.json topic entry has an invalid type")
        for key in ("expected", "summary", "items", "last_completed", "last_event"):
            if key in record and record[key] is not None and not isinstance(record[key], dict):
                raise ExportImportError(f"download_history.json.{key} has an invalid type")


def _validate_secrets_schema(data: dict[str, Any], *, label: str) -> None:
    _validate_tree(data, label=label)


def _derive_key(passphrase: str, salt: bytes, iterations: int) -> bytes:
    if not isinstance(passphrase, str) or not passphrase:
        raise ExportImportError("export passphrase must not be empty")
    try:
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

        return PBKDF2HMAC(
            algorithm=hashes.SHA256(),
            length=32,
            salt=salt,
            iterations=iterations,
        ).derive(passphrase.encode("utf-8"))
    except (ImportError, TypeError, ValueError, UnicodeError) as exc:
        raise ExportImportError("cannot derive export bundle key") from exc


def _encrypt_outer(payload: bytes, passphrase: str, salt: bytes | None = None) -> str:
    if salt is None:
        salt = os.urandom(KDF_SALT_BYTES)
    try:
        from cryptography.fernet import Fernet

        key = base64.urlsafe_b64encode(_derive_key(passphrase, salt, KDF_ITERATIONS))
        return Fernet(key).encrypt(payload).decode("ascii")
    except ExportImportError:
        raise
    except (ImportError, TypeError, ValueError, UnicodeError) as exc:
        raise ExportImportError("cannot encrypt export bundle") from exc


def _decrypt_outer(outer: dict[str, Any], passphrase: str) -> bytes:
    if not isinstance(outer, dict) or outer.get("format") != FORMAT:
        raise ExportImportError("unsupported export bundle format")
    if outer.get("cipher") != "fernet":
        raise ExportImportError("unsupported export bundle cipher")
    kdf = outer.get("kdf")
    if not isinstance(kdf, dict) or kdf.get("name") != KDF_NAME:
        raise ExportImportError("unsupported export bundle KDF")
    try:
        iterations = int(kdf.get("iterations") or 0)  # 0 is rejected by the range check below
        salt = base64.urlsafe_b64decode(str(kdf.get("salt") or "").encode("ascii"))
    except (TypeError, ValueError, UnicodeError, binascii.Error) as exc:
        raise ExportImportError("malformed export bundle KDF") from exc
    if not 100_000 <= iterations <= 5_000_000 or not 8 <= len(salt) <= 64:
        raise ExportImportError("invalid export bundle KDF parameters")
    token = outer.get("payload")
    if not isinstance(token, str) or not token:
        raise ExportImportError("malformed export bundle payload")
    try:
        from cryptography.fernet import Fernet, InvalidToken

        key = base64.urlsafe_b64encode(_derive_key(passphrase, salt, iterations))
        return Fernet(key).decrypt(token.encode("ascii"))
    except ExportImportError:
        raise
    except (InvalidToken, TypeError, ValueError, UnicodeError, binascii.Error) as exc:
        raise ExportImportError("cannot decrypt export bundle; wrong passphrase or corrupted bundle") from exc


def _zip_payload(members: dict[str, bytes]) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name in sorted(members):
            archive.writestr(name, members[name])
    return output.getvalue()


def _validate_member_name(name: str) -> None:
    if not isinstance(name, str) or not name or "\\" in name:
        raise ExportImportError("bundle member must be a strict relative path")
    path = PurePosixPath(name)
    if path.is_absolute() or ".." in path.parts or name not in ALLOWED_MEMBERS:
        raise ExportImportError(f"unsupported or unsafe bundle member: {name}")


def _unzip_payload(payload: bytes) -> dict[str, bytes]:
    if len(payload) > MAX_BUNDLE_BYTES:
        raise ExportImportError("decrypted export bundle is too large")
    try:
        archive = zipfile.ZipFile(io.BytesIO(payload), "r")
        infos = archive.infolist()
    except (OSError, zipfile.BadZipFile) as exc:
        raise ExportImportError("invalid export bundle archive") from exc
    names = [info.filename for info in infos]
    if len(names) != len(set(names)):
        raise ExportImportError("duplicate bundle member")
    members: dict[str, bytes] = {}
    total = 0
    try:
        for info in infos:
            _validate_member_name(info.filename)
            if info.is_dir() or (info.external_attr >> 16) & 0o170000 == 0o120000:
                raise ExportImportError("bundle directories and links are not allowed")
            if info.file_size > MAX_MEMBER_BYTES:
                raise ExportImportError("bundle member is too large")
            total += info.file_size
            if total > MAX_BUNDLE_BYTES:
                raise ExportImportError("bundle contents are too large")
            members[info.filename] = archive.read(info)
    except ExportImportError:
        raise
    except (OSError, RuntimeError, zipfile.BadZipFile) as exc:
        raise ExportImportError("cannot read export bundle archive") from exc
    finally:
        archive.close()
    return members


def _validated_payload(payload: bytes) -> dict[str, Any]:
    members = _unzip_payload(payload)
    if "manifest.json" not in members:
        raise ExportImportError("bundle manifest is missing")
    try:
        manifest = json.loads(members["manifest.json"].decode("utf-8"))
    except (UnicodeError, ValueError, RecursionError) as exc:
        raise ExportImportError("invalid bundle manifest") from exc
    _validate_tree(manifest, label="manifest.json")
    if not isinstance(manifest, dict) or manifest.get("format") != FORMAT or manifest.get("schema_version") != 1:
        raise ExportImportError("unsupported bundle manifest")
    source_version = manifest.get("source_version")
    if not isinstance(source_version, str) or not source_version:
        raise ExportImportError("bundle source version is missing")
    if source_version.split(".", 1)[0] != __version__.split(".", 1)[0]:
        raise ExportImportError("bundle source version is incompatible")
    listed = manifest.get("members")
    checksums = manifest.get("sha256")
    if not isinstance(listed, list) or not isinstance(checksums, dict):
        raise ExportImportError("malformed bundle manifest")
    expected = {str(name) for name in listed}
    actual = set(members) - {"manifest.json"}
    if expected != actual or len(listed) != len(expected):
        raise ExportImportError("bundle manifest member list mismatch")
    if not set(REQUIRED_MEMBERS).issubset(actual):
        raise ExportImportError("bundle required member is missing")
    if set(checksums) != actual:
        raise ExportImportError("bundle checksum list mismatch")
    for name in actual:
        _validate_member_name(name)
        if checksums.get(name) != _sha256(members[name]):
            raise ExportImportError(f"bundle checksum mismatch: {name}")

    config_data = _parse_mapping(members["config.yaml"], label="config.yaml")
    state_data = _parse_mapping(members["state.json"], label="state.json")
    history_data = _parse_mapping(members["download_history.json"], label="download_history.json")
    secrets_data = _parse_mapping(members["secrets.json"], label="secrets.json")
    _validate_config_schema(config_data)
    _validate_state_schema(state_data)
    _validate_history_schema(history_data)
    _validate_secrets_schema(secrets_data, label="secrets.json")
    _refuse_plaintext_secrets(config_data, state_data, history_data)
    if "secrets" in (state_data.get("undo") or {}):
        raise ExportImportError("plaintext secret undo is not allowed in bundle state")

    undo_data = None
    undo = state_data.get("undo")
    if isinstance(undo, dict) and undo.get("kind") == "settings":
        if undo.get("secrets_undo_ref") != "settings-v1" or "secrets_undo.json" not in actual:
            raise ExportImportError("settings undo reference has no encrypted snapshot")
        undo_data = _parse_mapping(members["secrets_undo.json"], label="secrets_undo.json")
        if not isinstance(undo_data.get("secrets"), dict):
            raise ExportImportError("malformed encrypted settings undo payload")
        _validate_secrets_schema(undo_data["secrets"], label="secrets_undo.json.secrets")
    elif "secrets_undo.json" in actual:
        raise ExportImportError("orphan encrypted settings undo member")

    events = None
    if "events.json" in actual:
        events = _parse_mapping(members["events.json"], label="events.json")
        _validate_tree(events, label="events.json")
        if not isinstance(events.get("events"), list) or not _events_are_redacted(events):
            raise ExportImportError("events member is not redacted")
    return {
        "manifest": manifest,
        "members": members,
        "config_bytes": members["config.yaml"],
        "config": config_data,
        "state": state_data,
        "history": history_data,
        "secrets": secrets_data,
        "undo": undo_data,
        "events": events,
    }


def _build_export_members(*, include_log: bool) -> dict[str, bytes]:
    config_file = config_path()
    config_bytes = _read_limited(config_file, label="config.yaml")
    config_data = _parse_mapping(config_bytes, label="config.yaml")
    state_data = load_state()
    history_data = load_json_for_export(download_history_path(), {"schema_version": 1, "topics": {}})
    secrets_data = load_secrets()
    _refuse_plaintext_secrets(config_data, state_data, history_data)
    state_bytes = _json_bytes(state_data)
    history_bytes = _json_bytes(history_data)
    members = {
        "config.yaml": config_bytes,
        "state.json": state_bytes,
        "download_history.json": history_bytes,
        "secrets.json": _json_bytes(secrets_data),
    }
    undo = state_data.get("undo")
    if isinstance(undo, dict) and undo.get("kind") == "settings":
        if undo.get("secrets_undo_ref") != "settings-v1":
            raise ExportImportError("settings undo reference is invalid")
        try:
            undo_secrets = load_secret_undo("settings-v1")
        except SecretStoreError as exc:
            raise ExportImportError("cannot read encrypted settings undo snapshot") from exc
        members["secrets_undo.json"] = _json_bytes({"secrets": undo_secrets})
    if include_log:
        members["events.json"] = _json_bytes(
            {"events": [export_event_projection(event) for event in read_events(limit=200)]}
        )
    manifest = {
        "format": FORMAT,
        "schema_version": 1,
        "source_version": __version__,
        "members": sorted(members),
        "sha256": {name: _sha256(data) for name, data in sorted(members.items())},
        "excluded": [
            "media_bytes",
            "torrent_files",
            "torrent_client_database",
            "os_keyrings",
            "scheduler_registrations",
            "caches",
            "source_master_key",
        ],
    }
    return {"manifest.json": _json_bytes(manifest), **members}


def load_json_for_export(path: Path, default: dict[str, Any]) -> dict[str, Any]:
    try:
        if not _checkpoint_file(path, missing=True):
            # A quarantined history is not an empty history. Do not make an
            # apparently healthy backup that silently omits the original data.
            data = load_json(path, default, quarantine=False)
        else:
            raw = _read_limited(path, label=path.name)
            data = decode_json_bytes(raw)
    except (UnicodeError, ValueError, RecursionError, StoreCorruptionError) as exc:
        raise ExportImportError(f"invalid {path.name}") from exc
    if not isinstance(data, dict):
        raise ExportImportError(f"{path.name} must be an object")
    return data


def verify_bundle(path: Path, passphrase: str) -> None:
    """Decrypt and validate an archive without recovering or changing any live store."""
    _read_bundle(path, passphrase)


def export_bundle(
    output: Path, passphrase: str, *, include_log: bool = False, overwrite: bool = False
) -> dict[str, Any]:
    try:
        with persistence_lock():
            return _export_bundle(output, passphrase, include_log=include_log, overwrite=overwrite)
    except ExportImportError:
        raise
    except Exception as exc:
        raise ExportImportError("cannot create export bundle safely") from exc


def _export_bundle(
    output: Path, passphrase: str, *, include_log: bool = False, overwrite: bool = False
) -> dict[str, Any]:
    output = Path(output)
    if output.exists() and not overwrite:
        raise ExportImportError("export output already exists; choose another path or use --force")
    previous = output.read_bytes() if overwrite and output.is_file() else None
    members = _build_export_members(include_log=include_log)
    payload = _zip_payload(members)
    salt = os.urandom(KDF_SALT_BYTES)
    outer = {
        "format": FORMAT,
        "cipher": "fernet",
        "kdf": {
            "name": KDF_NAME,
            "iterations": KDF_ITERATIONS,
            "salt": base64.urlsafe_b64encode(salt).decode("ascii"),
        },
        "payload": _encrypt_outer(payload, passphrase, salt),
    }
    content = _json_bytes(outer)
    if overwrite:
        _atomic_write(output, content)
    else:
        atomic_create_bytes(output, content)
    _restrict_file(output)
    try:
        verified = _read_bundle(output, passphrase)
        if set(verified["members"]) != set(members):
            raise ExportImportError("export bundle read-back member mismatch")
    except ExportImportError:
        if previous is None:
            with contextlib.suppress(OSError):
                if not output.is_symlink() and output.read_bytes() == content:
                    output.unlink()  # never remove a replacement supplied by another writer
        else:
            _atomic_write(output, previous)
        raise
    return {
        "ok": True,
        "format": FORMAT,
        "preview": False,
        "source_mutation": False,
        "read_back": True,
        "output": str(output),
        "members": sorted(name for name in members if name != "manifest.json"),
        "bytes": output.stat().st_size,
        "secrets": "encrypted-in-bundle",
        "source_master_key": "not-included",
    }


def parse_path_maps(values: Iterable[str] | None) -> list[tuple[str, str]]:
    result = []
    for raw in values or ():
        if not isinstance(raw, str) or "=" not in raw:
            raise ExportImportError("path map must use OLD=NEW")
        old, new = raw.split("=", 1)
        old, new = old.strip(), new.strip()
        if not old or not new:
            raise ExportImportError("path map must contain non-empty OLD and NEW")
        if old == new:
            raise ExportImportError("path map OLD and NEW must differ")
        result.append((old, new))
    if len({old for old, _ in result}) != len(result):
        raise ExportImportError("duplicate path map source")
    return result


def _looks_absolute(value: str) -> bool:
    return bool(_DRIVE_PATH.match(value) or value.startswith(("/", "\\\\")))


def _map_one(value: str, mappings: list[tuple[str, str]]) -> tuple[str, bool]:
    for old, new in mappings:
        old_clean = old.rstrip("/\\")
        if value == old_clean:
            return new, True
        for separator in ("/", "\\"):
            prefix = old_clean + separator
            if value.startswith(prefix):
                return new.rstrip("/\\") + value[len(old_clean) :], True
    return value, False


def _apply_path_maps(state: dict[str, Any], mappings: list[tuple[str, str]]) -> tuple[dict[str, Any], list[str]]:
    result = copy.deepcopy(state)
    warnings: list[str] = []
    roots = result.get("save_roots")
    if isinstance(roots, list):
        for index, value in enumerate(roots):
            if isinstance(value, str):
                mapped, matched = _map_one(value, mappings)
                result["save_roots"][index] = mapped
                if _looks_absolute(value) and not matched:
                    warnings.append(f"unmapped save root: {value}")
    for topic in result.get("topics") or []:
        if not isinstance(topic, dict):
            continue
        value = topic.get("save_path")
        if isinstance(value, str):
            mapped, matched = _map_one(value, mappings)
            topic["save_path"] = mapped
            if _looks_absolute(value) and not matched:
                warnings.append(f"unmapped topic path: {value}")
    return result, warnings


def _checkpoint_targets() -> list[tuple[str, Path]]:
    return [
        ("config.yaml", config_path()),
        ("state.json", state_path()),
        ("download_history.json", download_history_path()),
        ("secrets.enc", encrypted_secrets_path()),
        ("secrets-undo.enc", secret_undo_path()),
    ]


def _create_import_checkpoint() -> Path:
    checkpoint = data_dir() / "import-checkpoints" / f"{iso_now().replace(':', '-')}-{uuid.uuid4().hex[:10]}"
    files_dir = checkpoint / "files"
    files_dir.mkdir(parents=True, exist_ok=False)
    entries = []
    for member, target in _checkpoint_targets():
        exists = _checkpoint_file(target, missing=True)
        entry = {"member": member, "target": str(target), "exists": exists}
        if exists:
            content = _read_limited(target, label=member)
            backup = files_dir / member
            backup.parent.mkdir(parents=True, exist_ok=True)
            _atomic_write(backup, content)
            entry["sha256"] = _sha256(content)
        else:
            entry["sha256"] = None
        entries.append(entry)
    manifest = {
        "format": CHECKPOINT_FORMAT,
        "created_at": iso_now(),
        "checkpoint_id": checkpoint.name,
        "targets": entries,
        "excluded": ["plaintext secrets", "TOW_MASTER_KEY", "TOW_MASTER_KEY_FILE", "media bytes", "qBittorrent data"],
    }
    _atomic_write(checkpoint / "MANIFEST.json", _json_bytes(manifest))
    _read_checkpoint(checkpoint)  # every copy is intact before the operation can start
    _write_import_transaction(checkpoint, status="prepared")
    return checkpoint


def _write_import_transaction(checkpoint: Path, *, status: str, **fields: Any) -> None:
    if status not in {"prepared", "committed", "rolled_back"}:
        raise ExportImportError("invalid import transaction status")
    transaction = {
        "format": TRANSACTION_FORMAT,
        "checkpoint": str(Path(checkpoint)),
        "status": status,
        "updated_at": iso_now(),
        **fields,
    }
    _atomic_write(Path(checkpoint) / "TRANSACTION.json", _json_bytes(transaction))


def _read_import_transaction(checkpoint: Path, *, bound: bool = True) -> dict[str, Any]:
    path = Path(checkpoint) / "TRANSACTION.json"
    try:
        _checkpoint_directory(Path(checkpoint))
        _checkpoint_file(path)
        value = decode_json_bytes(_read_limited(path, label="import transaction"))
    except (OSError, UnicodeError, ValueError, RecursionError) as exc:
        raise ExportImportError("import transaction is unreadable") from exc
    if not isinstance(value, dict) or value.get("format") != TRANSACTION_FORMAT:
        raise ExportImportError("unsupported import transaction")
    if not isinstance(value.get("status"), str) or value["status"] not in {"prepared", "committed", "rolled_back"}:
        raise ExportImportError("invalid import transaction status")
    if not isinstance(value.get("checkpoint"), str) or (bound and value["checkpoint"] != str(Path(checkpoint))):
        raise ExportImportError("import transaction checkpoint mismatch")
    return value


def recover_import_transactions() -> list[str]:
    with persistence_lock():
        return _recover_import_transactions_locked()


def _recover_import_transactions_locked() -> list[str]:
    root = data_dir() / "import-checkpoints"
    if not _checkpoint_directory(root, missing=True):
        return []
    recovered: list[str] = []
    try:
        checkpoints = sorted(path for path in root.iterdir() if path.is_dir())
    except OSError as exc:
        raise ExportImportError("cannot inspect import transactions") from exc
    for checkpoint in checkpoints:
        if not _checkpoint_file(checkpoint / "TRANSACTION.json", missing=True):
            continue
        transaction = _read_import_transaction(checkpoint, bound=False)
        if transaction["status"] != "prepared":
            continue
        try:
            _rollback_import(checkpoint, apply=True)
            _write_import_transaction(checkpoint, status="rolled_back", recovered=True)
        except ExportImportError as exc:
            raise ExportImportError("incomplete import recovery failed") from exc
        recovered.append(str(checkpoint))
    return recovered


def recover_interrupted_import() -> None:
    """A recovery step (``tow.store.RECOVERY_STEPS``), the data lock held.

    An import runs entirely under the persistence lock, so a "prepared" journal seen when
    a process takes the lock was left by a crash: roll it back before anyone writes.
    An unreadable marker cannot prove that an import finished: it also fails closed.
    """
    _recover_import_transactions_locked()


def _checkpoint_file(path: Path, *, missing: bool = False) -> bool:
    try:
        info = path.lstat()
    except FileNotFoundError:
        if missing:
            return False
        raise ExportImportError("import checkpoint file is missing") from None
    except OSError as exc:
        raise ExportImportError("import checkpoint file cannot be inspected") from exc
    if not stat.S_ISREG(info.st_mode) or getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT:
        raise ExportImportError("import checkpoint file is unsafe")
    return True


def _checkpoint_directory(path: Path, *, missing: bool = False) -> bool:
    try:
        info = path.lstat()
    except FileNotFoundError:
        if missing:
            return False
        raise ExportImportError("import checkpoint directory is missing") from None
    except OSError as exc:
        raise ExportImportError("import checkpoint directory cannot be inspected") from exc
    if not stat.S_ISDIR(info.st_mode) or getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT:
        raise ExportImportError("import checkpoint directory is unsafe")
    return True


def _read_checkpoint(checkpoint: Path) -> dict[str, Any]:
    checkpoint = Path(checkpoint)
    if not _checkpoint_directory(checkpoint, missing=True):
        raise ExportImportError("import checkpoint does not exist")
    try:
        _checkpoint_directory(checkpoint)
        _checkpoint_file(checkpoint / "MANIFEST.json")
        data = decode_json_bytes(_read_limited(checkpoint / "MANIFEST.json", label="import checkpoint manifest"))
    except (OSError, UnicodeError, ValueError, RecursionError) as exc:
        raise ExportImportError("import checkpoint manifest is unreadable") from exc
    if (
        not isinstance(data, dict)
        or data.get("format") != CHECKPOINT_FORMAT
        or not isinstance(data.get("targets"), list)
    ):
        raise ExportImportError("unsupported import checkpoint")
    expected_targets = dict(_checkpoint_targets())
    entries = data["targets"]
    valid_entries = all(isinstance(entry, dict) and isinstance(entry.get("member"), str) for entry in entries)
    if not valid_entries:
        raise ExportImportError("malformed import checkpoint target")
    target_members = [entry["member"] for entry in entries]
    if len(target_members) != len(set(target_members)) or set(target_members) != set(expected_targets):
        raise ExportImportError("import checkpoint target list is invalid")
    files_dir = checkpoint / "files"
    if not _checkpoint_directory(files_dir, missing=True):
        raise ExportImportError("import checkpoint files directory is invalid")
    for entry in data["targets"]:
        if not isinstance(entry, dict) or entry.get("member") not in expected_targets:
            raise ExportImportError("malformed import checkpoint target")
        member = str(entry["member"])
        target_value = entry.get("target")
        if not isinstance(target_value, str) or not target_value:
            raise ExportImportError("import checkpoint target is missing")
        try:
            if Path(target_value).resolve() != expected_targets[member].resolve():
                raise ExportImportError(f"import checkpoint target mismatch: {member}")
        except OSError as exc:
            raise ExportImportError("import checkpoint target is unsafe") from exc
        exists = entry.get("exists")
        if not isinstance(exists, bool):
            raise ExportImportError("import checkpoint exists flag is invalid")
        checksum = entry.get("sha256")
        backup = files_dir / member
        if exists:
            if not _checkpoint_file(backup, missing=True):
                raise ExportImportError(f"import checkpoint backup is missing: {member}")
            content = _read_limited(backup, label=f"checkpoint {member}")
            if (
                not isinstance(checksum, str)
                or not re.fullmatch(r"[0-9a-f]{64}", checksum)
                or checksum != _sha256(content)
            ):
                raise ExportImportError(f"import checkpoint checksum mismatch: {member}")
        elif checksum is not None or backup.exists():
            raise ExportImportError(f"import checkpoint has an unexpected backup: {member}")
    return data


def rollback_import(checkpoint: Path, *, apply: bool = False) -> dict[str, Any]:
    try:
        with persistence_lock():
            return _rollback_import(checkpoint, apply=apply)
    except ExportImportError:
        raise
    except Exception as exc:
        raise ExportImportError("cannot roll back import checkpoint safely") from exc


def _capture_runtime_targets(expected_targets: dict[str, Path]) -> dict[str, bytes | None]:
    snapshot: dict[str, bytes | None] = {}
    for member, target in expected_targets.items():
        try:
            exists = _checkpoint_file(target, missing=True)
        except ExportImportError as exc:
            raise ExportImportError(f"import rollback target is not a file: {member}") from exc
        if exists:
            try:
                snapshot[member] = target.read_bytes()
            except OSError as exc:
                raise ExportImportError(f"cannot snapshot rollback target: {member}") from exc
        else:
            snapshot[member] = None
    return snapshot


def _restore_runtime_targets(snapshot: dict[str, bytes | None], expected_targets: dict[str, Path]) -> None:
    for member, target in expected_targets.items():
        content = snapshot[member]
        if content is None:
            try:
                target.unlink()
            except FileNotFoundError:
                pass
            except OSError as exc:
                raise ExportImportError(f"cannot compensate rollback target: {member}") from exc
            if target.exists() or target.is_symlink():
                raise ExportImportError(f"rollback compensation could not remove {member}")
            continue
        _atomic_write(target, content)
        try:
            if target.read_bytes() != content:
                raise ExportImportError(f"rollback compensation read-back mismatch: {member}")
        except OSError as exc:
            raise ExportImportError(f"cannot read rollback compensation: {member}") from exc


def _rollback_import(checkpoint: Path, *, apply: bool = False) -> dict[str, Any]:
    checkpoint = Path(checkpoint)
    manifest = _read_checkpoint(checkpoint)
    expected_targets = dict(_checkpoint_targets())
    if not apply:
        return {
            "ok": True,
            "preview": True,
            "checkpoint": str(checkpoint),
            "targets": [entry.get("member") for entry in manifest["targets"] if isinstance(entry, dict)],
        }

    transaction = _read_import_transaction(checkpoint)
    operation_id = uuid.uuid4().hex
    snapshot = _capture_runtime_targets(expected_targets)
    restore_plan: list[tuple[str, Path, bytes | None]] = []
    for entry in manifest["targets"]:
        if not isinstance(entry, dict) or entry.get("member") not in expected_targets:
            raise ExportImportError("malformed import checkpoint target")
        member = str(entry["member"])
        target = expected_targets[member]
        target_value = entry.get("target")
        if not isinstance(target_value, str) or not target_value:
            raise ExportImportError("import checkpoint target is missing")
        if Path(target_value).resolve() != target.resolve():
            raise ExportImportError(f"import checkpoint target mismatch: {member}")
        backup = checkpoint / "files" / member
        if entry.get("exists"):
            content = _read_limited(backup, label=f"checkpoint {member}")
            if entry.get("sha256") != _sha256(content):
                raise ExportImportError(f"import checkpoint checksum mismatch: {member}")
        else:
            content = None
        restore_plan.append((member, target, content))

    _write_import_transaction(checkpoint, status="prepared", operation_id=operation_id, phase="rollback")
    restored: list[str] = []
    try:
        for member, target, content in restore_plan:
            if content is not None:
                _atomic_write(target, content)
                if target.read_bytes() != content:
                    raise ExportImportError(f"import rollback read-back mismatch: {member}")
            else:
                try:
                    target.unlink()
                except FileNotFoundError:
                    pass
                except OSError as exc:
                    raise ExportImportError(f"cannot remove imported {member}") from exc
                if target.exists():
                    raise ExportImportError(f"import rollback could not remove {member}")
            restored.append(member)
    except Exception as exc:
        try:
            _restore_runtime_targets(snapshot, expected_targets)
        except Exception as compensation_exc:
            raise ExportImportError("rollback failed; recovery remains pending") from compensation_exc
        # The destination was put back as it was, so the marker gets back its own status too.
        # Turning a "rolled_back" marker into "prepared" made the next lock holder roll back
        # once more - silently losing every edit made since the first rollback.
        previous = transaction.get("status")
        status = previous if previous in {"committed", "rolled_back"} else "prepared"
        try:
            _write_import_transaction(
                checkpoint,
                status=status,
                operation_id=operation_id,
                phase="rollback",
                rollback_status="failed_compensated",
                rollback_error=type(exc).__name__,
            )
        except Exception as marker_exc:
            raise ExportImportError("rollback failed; destination preserved but marker update failed") from marker_exc
        raise ExportImportError("rollback failed; destination preserved") from exc

    try:
        _write_import_transaction(checkpoint, status="rolled_back", operation_id=operation_id, phase="rollback")
    except Exception as exc:
        raise ExportImportError("rollback applied but transaction marker update failed") from exc
    return {"ok": True, "checkpoint": str(checkpoint), "restored": restored}


def _read_bundle(path: Path, passphrase: str) -> dict[str, Any]:
    try:
        raw = _read_limited(Path(path), label="export bundle")
        outer = json.loads(raw.decode("utf-8"))
    except (UnicodeError, ValueError, RecursionError) as exc:
        raise ExportImportError("invalid export bundle envelope") from exc
    _validate_tree(outer, label="export bundle envelope")
    return _validated_payload(_decrypt_outer(outer, passphrase))


def _destination_readback(parsed: dict[str, Any], state_bytes: bytes, history_bytes: bytes) -> None:
    if config_path().read_bytes() != parsed["config_bytes"]:
        raise ExportImportError("config read-back mismatch")
    if state_path().read_bytes() != state_bytes:
        raise ExportImportError("state read-back mismatch")
    if download_history_path().read_bytes() != history_bytes:
        raise ExportImportError("download history read-back mismatch")
    try:
        if load_secrets() != parsed["secrets"]:
            raise ExportImportError("secrets semantic read-back mismatch")
        if parsed["undo"] is not None and load_secret_undo("settings-v1") != parsed["undo"]["secrets"]:
            raise ExportImportError("settings undo semantic read-back mismatch")
    except SecretStoreError as exc:
        raise ExportImportError("encrypted destination secret read-back failed") from exc


def _record_import_event(checkpoint: Path, mappings: list[tuple[str, str]], members: list[str]) -> bool:
    try:
        return log_event(
            "portable_import_applied",
            operation_id=uuid.uuid4().hex,
            format=FORMAT,
            checkpoint=str(checkpoint),
            path_maps=mappings,
            members=members,
        )
    except Exception as exc:  # noqa: BLE001 - the import is committed; a lost audit line is not fatal
        logging.getLogger("tow.bundle").warning("import audit event not written: %s", type(exc).__name__)
        return False


def import_bundle(
    input_path: Path,
    passphrase: str,
    *,
    apply: bool = False,
    path_maps: Iterable[str] | None = None,
    config_overrides: dict[str, Any] | None = None,
    preserve_secret_keys: Iterable[str] | None = None,
) -> dict[str, Any]:
    try:
        with persistence_lock():
            return _import_bundle(
                input_path,
                passphrase,
                apply=apply,
                path_maps=path_maps,
                config_overrides=config_overrides,
                preserve_secret_keys=preserve_secret_keys,
            )
    except ExportImportError:
        raise
    except Exception as exc:
        raise ExportImportError("cannot import TOW bundle safely") from exc


def _import_bundle(
    input_path: Path,
    passphrase: str,
    *,
    apply: bool = False,
    path_maps: Iterable[str] | None = None,
    config_overrides: dict[str, Any] | None = None,
    preserve_secret_keys: Iterable[str] | None = None,
) -> dict[str, Any]:
    recover_import_transactions()
    parsed = _read_bundle(Path(input_path), passphrase)
    overrides = dict(config_overrides or {})
    if not set(overrides).issubset({"bind", "port", "allow_lan"}):
        raise ExportImportError("unsupported config override")
    if overrides:
        # Typed values from the CLI or the web form: no "yes" or "80" written for them.
        _validate_field(overrides, "bind", str, label="config.yaml")
        _validate_field(overrides, "port", int, label="config.yaml")
        _validate_field(overrides, "allow_lan", bool, label="config.yaml")
        effective_config = copy.deepcopy(parsed["config"])
        effective_config.update(copy.deepcopy(overrides))
        _validate_config_schema(effective_config)
        try:
            parsed["config_bytes"] = yaml.safe_dump(
                effective_config,
                allow_unicode=True,
                sort_keys=False,
                default_flow_style=False,
            ).encode("utf-8")
        except (TypeError, ValueError, UnicodeError, yaml.YAMLError) as exc:
            raise ExportImportError("cannot apply safe config overrides") from exc
        parsed["config"] = effective_config
    secret_keys = {str(key) for key in (preserve_secret_keys or ())}
    if not secret_keys.issubset({"lan_auth"}):
        raise ExportImportError("unsupported preserved secret key")
    if secret_keys:
        try:
            destination_secrets = load_secrets()
        except SecretStoreError as exc:
            raise ExportImportError("cannot read destination secrets to preserve access") from exc
        effective_secrets = copy.deepcopy(parsed["secrets"])
        for key in secret_keys:
            if key in destination_secrets:
                effective_secrets[key] = copy.deepcopy(destination_secrets[key])
            else:
                effective_secrets.pop(key, None)
        parsed["secrets"] = effective_secrets
    mappings = parse_path_maps(path_maps)
    mapped_state, warnings = _apply_path_maps(parsed["state"], mappings)
    state_bytes = _json_bytes(mapped_state)
    history_bytes = _json_bytes(parsed["history"])
    members = sorted(name for name in parsed["members"] if name != "manifest.json")
    result = {
        "ok": True,
        "format": FORMAT,
        "preview": not apply,
        "apply_required": not apply,
        "members": members,
        "path_maps": [{"old": old, "new": new} for old, new in mappings],
        "config_overrides": sorted(overrides),
        "preserved_secret_keys": sorted(secret_keys),
        "path_warnings": warnings,
        "scheduler": "not restored; turn autostart on separately with tow autostart on",
        "client_media": "not transferred; qBittorrent/client check is separate",
        "committed": False,
        "log_recorded": False,
    }
    if not apply:
        return result
    if secrets_path().is_file():
        raise ExportImportError("destination legacy plaintext secrets require explicit migration before import")
    try:
        master_fernet()
    except SecretStoreError as exc:
        raise ExportImportError("destination master key is unavailable or invalid") from exc

    checkpoint = _create_import_checkpoint()
    try:
        _atomic_write(config_path(), parsed["config_bytes"])
        _atomic_write(state_path(), state_bytes)
        _atomic_write(download_history_path(), history_bytes)
        save_secrets(parsed["secrets"])
        if parsed["undo"] is not None:
            save_secret_undo(parsed["undo"]["secrets"])
        else:
            try:
                secret_undo_path().unlink()
            except FileNotFoundError:
                pass
            except OSError as exc:
                raise ExportImportError("cannot remove stale settings undo snapshot") from exc
        _destination_readback(parsed, state_bytes, history_bytes)
        _write_import_transaction(checkpoint, status="committed", operation_id=uuid.uuid4().hex)
    except (ExportImportError, SecretStoreError, OSError) as exc:
        try:
            rollback_import(checkpoint, apply=True)
        except ExportImportError as rollback_exc:
            raise ExportImportError("import failed and destination rollback also failed") from rollback_exc
        if isinstance(exc, ExportImportError):
            raise
        raise ExportImportError("import failed; destination was restored") from exc

    result.update(
        {"preview": False, "apply_required": False, "checkpoint": str(checkpoint), "read_back": True, "committed": True}
    )
    result["log_recorded"] = _record_import_event(checkpoint, mappings, members)
    if not result["log_recorded"]:
        result["log_error"] = "audit event could not be persisted"
    try:
        _write_import_transaction(checkpoint, status="committed", log_recorded=result["log_recorded"])
    except ExportImportError:
        result["transaction_log_status"] = "unrecorded"
    result["pruned_checkpoints"] = prune_import_checkpoints(keep=IMPORT_CHECKPOINTS_KEPT)
    return result


IMPORT_CHECKPOINTS_KEPT = 5


def _owned_checkpoint(checkpoint: Path) -> bool:
    manifest = _read_checkpoint(checkpoint)
    allowed = {"MANIFEST.json", "TRANSACTION.json", "files"}
    if {entry.name for entry in checkpoint.iterdir()} != allowed:
        return False
    members = {entry["member"] for entry in manifest["targets"] if entry["exists"]}
    for entry in (checkpoint / "files").iterdir():
        if entry.name not in members:
            return False
        _checkpoint_file(entry)
    return True


def prune_import_checkpoints(*, keep: int = IMPORT_CHECKPOINTS_KEPT) -> int:
    """Remove finished import checkpoints beyond the newest ``keep``.

    Each one is a full copy of config, state, history and encrypted secrets, and they
    were never removed. Only ``committed``/``rolled_back`` ones are deleted; a
    ``prepared`` (in-flight or unrecovered) or unreadable checkpoint is always kept.
    """
    root = data_dir() / "import-checkpoints"
    try:
        if not _checkpoint_directory(root, missing=True):
            return 0
        checkpoints = sorted((p for p in root.iterdir() if p.is_dir() and not p.is_symlink()), reverse=True)
    except ExportImportError, OSError:
        return 0
    removed = 0
    for checkpoint in checkpoints[max(0, keep) :]:
        try:
            if _read_import_transaction(checkpoint)["status"] not in {"committed", "rolled_back"}:
                continue
            if not _owned_checkpoint(checkpoint):
                continue
            shutil.rmtree(checkpoint)
            removed += 1
        except ExportImportError, OSError:
            continue
    return removed
