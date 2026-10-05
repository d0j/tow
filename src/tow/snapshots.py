"""Daily data snapshots to another drive, and restoring one (A4).

A snapshot is a plain folder ``<backup_dir>/tow-YYYYMMDD-HHMMSS`` with an allowlist of
files and a MANIFEST.json of their SHA-256. Secrets stay encrypted (secrets.enc); the
master key is never copied - it lives outside the data folder, and the owner keeps an
offline copy. Plaintext credentials (lan-auth.token, browser login profiles) are not
copied either.

The MANIFEST is signed (HMAC-SHA256 with a key derived from the master key), so a copy
whose file list was edited - or planted in the copies folder by someone else - is never
restored. Only the fixed member names and ``restore-points/<id>.towx`` are accepted, and
every target is checked to stay where it belongs.
"""

from __future__ import annotations

import contextlib
import hashlib
import hmac
import io
import json
import os
import re
import shutil
import stat
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from tow import __version__
from tow.backup_actions import copy_revision
from tow.backup_retention import MIB, copy_time, retained_copies, retention_settings
from tow.config import interval_sec_of, load_config
from tow.diagnostic_json import check_epochs, check_types, encode_object, read_object
from tow.i18n import t
from tow.log import locked_log_path, log_event, log_path, owner_language
from tow.paths import config_path, data_dir, download_history_path, state_path
from tow.store import (
    SecretStoreError,
    StateVersionError,
    atomic_write_bytes,
    decode_json_bytes,
    decrypt_secret_undo_bytes,
    decrypt_secrets_bytes,
    derive_local_secret,
    encrypt_secrets_bytes,
    encrypted_secrets_path,
    load_secrets,
    persistence_lock,
    secret_undo_path,
    validate_download_history_bytes,
    validate_state_bytes,
)
from tow.yaml_guard import MAX_INPUT_BYTES, YamlLimitError
from tow.yaml_guard import dump as dump_yaml
from tow.yaml_guard import load as load_yaml
from tow.yaml_guard import read_text as read_yaml_text

FORMAT = "tow-snapshot-v2"  # a signed MANIFEST
UNSIGNED_FORMAT = "tow-snapshot-v1"  # copies made before 1.17: checked, never restored automatically
_SIGNATURE_PURPOSE = "night-copies"
_POINT_MEMBER = re.compile(r"restore-points/(?P<id>[A-Za-z0-9-]{1,64})\.towx")
KEEP_DEFAULT = 14
MAX_MANIFEST_BYTES = 1024 * 1024
MAX_MEMBER_SIZE = 2**63 - 1
_READ_CHUNK_BYTES = 1024 * 1024
_VALIDATED_MEMBERS = ("config.yaml", "state.json", "download_history.json", "secrets.enc", "secrets-undo.enc")
# Exists only while a settings change can still be undone: its absence is normal.
_OPTIONAL_MEMBERS = frozenset({"secrets-undo.enc"})
_PREFIX = "tow-"
# Access settings belong to this machine, not to the copy (as for restore points).
_ACCESS_KEYS = ("bind", "port", "allow_lan")


class SnapshotError(RuntimeError):
    pass


STATUS_NAME = "backup-status.json"


def status_path() -> Path:
    return data_dir() / STATUS_NAME


def status() -> dict[str, Any]:
    """Last success and last failure of a night copy (for the watchdog and Settings)."""
    try:
        info = status_path().lstat()
        if not stat.S_ISREG(info.st_mode) or getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT:
            raise ValueError("backup record is not a regular file")
        value = read_object(status_path())
        check_epochs(value, ("last_ok_at", "last_error_at"))
        check_types(
            value,
            {
                "last_error": str,
                "last_snapshot": str,
                "location": str,
                "last_cleanup_pending": bool,
                "last_bytes": int,
                "last_missing": list,
            },
        )
        if value.get("last_bytes", 0) is not None and value.get("last_bytes", 0) < 0:
            raise ValueError("invalid service size")
        if any(not isinstance(item, str) for item in (value.get("last_missing") or [])):
            raise ValueError("invalid missing members")
    except FileNotFoundError:
        return {}
    except OSError, UnicodeError, ValueError, TypeError, RecursionError:
        return {"read_error": True, "last_error": t("watchdog.backup.status_unreadable")}
    return value


def _cleanup_inventory(location: Path) -> str:
    """Bind cleanup to committed candidate names; this does not grant deletion ownership."""
    digest = hashlib.sha256()
    try:
        info = location.lstat()
    except FileNotFoundError:
        return digest.hexdigest()
    if not stat.S_ISDIR(info.st_mode) or getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT:
        raise ValueError("cleanup folder is not a regular directory")
    # Partial copies begin with a dot. Do not inspect candidate contents or follow links.
    for name in sorted(path.name for path in location.iterdir() if path.name.startswith(_PREFIX)):
        digest.update(name.encode("utf-8"))
        digest.update(b"\0")
    return digest.hexdigest()


def cleanup_status(*, cfg: dict[str, Any] | None = None) -> dict[str, Any]:
    """Cleanup in the current folder: pending, complete or unknown, never inferred from absence."""
    with persistence_lock():  # inventory and record must describe the same completed copy
        return _cleanup_status(cfg=cfg)


def _cleanup_status(*, cfg: dict[str, Any] | None) -> dict[str, Any]:
    result: dict[str, Any] = {"pending": None, "read_error": False, "location": "", "legacy": False}
    try:
        location = backup_root(cfg)
        result["location"] = str(location.resolve())
        state = status()
        if state.get("read_error"):
            raise ValueError("unreadable backup record")
        if "last_cleanup_pending" not in state:
            # Copies made before cleanup monitoring have a full copy result but no flag.
            # This is migration, not lost observation; a prior bound watchdog still detects
            # loss of both new fields through its persistent bound marker.
            if (
                "cleanup_inventory" not in state
                and isinstance(state.get("last_ok_at"), (int, float))
                and isinstance(state.get("last_snapshot"), str)
                and re.fullmatch(r"tow-\d{8}-\d{6}(?:-[a-f0-9]{6})?", state["last_snapshot"])
                and state.get("location") == result["location"]
            ):
                _cleanup_inventory(location)  # old metadata cannot hide a folder-access failure
                result["legacy"] = True
            return result  # a new install or a copy failure alone has no cleanup observation
        if type(state["last_cleanup_pending"]) is not bool or not isinstance(state.get("location"), str):
            raise ValueError("invalid cleanup observation")
        if not state["location"]:
            raise ValueError("missing cleanup location")
        inventory = state.get("cleanup_inventory")
        if "cleanup_inventory" in state and (
            not isinstance(inventory, str) or re.fullmatch(r"[a-f0-9]{64}", inventory) is None
        ):
            raise ValueError("invalid cleanup inventory")
        if state["location"] == result["location"]:
            observed = _cleanup_inventory(location)
            if "cleanup_inventory" not in state:
                result["legacy"] = True
                if state["last_cleanup_pending"]:
                    result["pending"] = True
            elif observed != inventory:
                raise ValueError("cleanup observation belongs to an earlier inventory")
            else:
                result["pending"] = state["last_cleanup_pending"]
    except OSError, ValueError, TypeError, UnicodeError, RecursionError, SnapshotError:
        result["read_error"] = True
    return result


def status_failed(value: dict[str, Any]) -> bool:
    """The latest attempt's error wins, even with equal dates or a clock correction."""
    if value.get("read_error"):
        return True
    if "last_error" in value:
        return bool(value["last_error"])
    failed = value.get("last_error_at")
    return bool(failed is not None and (not value.get("last_ok_at") or failed >= value["last_ok_at"]))


def _record(**fields: Any) -> None:
    from tow.store import atomic_write_text

    current = status()
    if current.get("read_error"):
        current = {}
    current.update(fields)
    # The status is informative; the copy itself already succeeded or failed.
    with contextlib.suppress(OSError, ValueError, UnicodeError, RecursionError):
        atomic_write_text(status_path(), encode_object(current))


def record_failure(error: str) -> None:
    _record(last_error=error[:300], last_error_at=round(time.time(), 3))
    log_event("backup_failed", error=error[:300], how="auto")


def _fixed_members() -> list[tuple[str, Path]]:
    """(name inside the snapshot, live path) of the stores every copy carries."""
    return [
        ("state.json", state_path()),
        ("download_history.json", download_history_path()),
        ("secrets.enc", encrypted_secrets_path()),
        ("secrets-undo.enc", secret_undo_path()),
        ("tow.jsonl", log_path()),
        ("config.yaml", config_path()),
    ]


def _members() -> list[tuple[str, Path]]:
    """(name inside the snapshot, live path) - the allowlist, with this install's restore points."""
    members = _fixed_members()
    from tow.restore_points import RestorePointError, list_restore_points, point_path

    try:
        members.extend(
            (f"restore-points/{point['id']}.towx", point_path(point["id"])) for point in list_restore_points()
        )
    except RestorePointError as exc:  # restore_points_dir of another system: said, not skipped
        raise SnapshotError(str(exc)) from exc
    return members


def _member_target(name: str) -> Path:
    """Where member ``name`` of a copy goes, refusing every name TOW does not write itself.

    A restore point is accepted only under its real id pattern, and its target must stay
    directly inside the restore points folder (``restore-points/\\Windows\\...`` or
    ``restore-points//Users/...`` would otherwise escape it).
    """
    fixed = dict(_fixed_members())
    if name in fixed:
        return fixed[name]
    lang = owner_language()
    match = _POINT_MEMBER.fullmatch(name) if isinstance(name, str) else None
    if match is None:
        if isinstance(name, str) and name.startswith("restore-points/"):
            raise SnapshotError(t("backup.snapshot.unsafe_path", lang, name=name))
        raise SnapshotError(t("backup.snapshot.unexpected_file", lang, name=name))
    from tow.restore_points import RestorePointError, point_path, restore_points_dir

    try:
        target = point_path(match.group("id"), must_exist=False)
        root = restore_points_dir().resolve()
        inside = target.resolve().parent == root
    except yaml.YAMLError as exc:  # the live config, which says where restore points live, is broken
        raise SnapshotError(t("backup.snapshot.cannot_merge_config", lang)) from exc
    except (RestorePointError, OSError) as exc:
        raise SnapshotError(t("backup.snapshot.unsafe_path", lang, name=name)) from exc
    if not inside:
        raise SnapshotError(t("backup.snapshot.unsafe_path", lang, name=name))
    return target


def _signing_key() -> bytes:
    try:
        return derive_local_secret(_SIGNATURE_PURPOSE).encode("ascii")
    except SecretStoreError as exc:
        raise SnapshotError(t("backup.snapshot.no_key", owner_language())) from exc


def _signature(manifest: dict[str, Any], key: bytes) -> str:
    body = {name: value for name, value in manifest.items() if name != "signature"}
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")
    return hmac.new(key, canonical, hashlib.sha256).hexdigest()


def _signature_matches(manifest: dict[str, Any], key: bytes) -> bool:
    signature = manifest.get("signature")
    return (
        isinstance(signature, str) and signature.isascii() and hmac.compare_digest(signature, _signature(manifest, key))
    )


def _read_manifest(path: Path) -> dict[str, Any]:
    lang = owner_language()
    try:
        _ordinary_directory(path)
        _ordinary_file(path / "MANIFEST.json")
        with (path / "MANIFEST.json").open("rb") as handle:
            content = handle.read(MAX_MANIFEST_BYTES + 1)
        if len(content) > MAX_MANIFEST_BYTES:
            raise SnapshotError(t("backup.snapshot.manifest_too_large", lang))
        manifest = json.loads(content.decode("utf-8"))
    except (OSError, UnicodeError, ValueError, RecursionError) as exc:
        raise SnapshotError(t("backup.snapshot.manifest_unreadable", lang)) from exc
    if (
        not isinstance(manifest, dict)
        or manifest.get("format") not in (FORMAT, UNSIGNED_FORMAT)
        or not isinstance(manifest.get("files"), dict)
    ):
        raise SnapshotError(t("backup.snapshot.not_tow", lang))
    return manifest


def _member_size(name: str, meta: Any) -> int:
    """A file size, not a coerced float/string or an unrepresentable filesystem integer."""
    size = meta.get("size") if isinstance(meta, dict) else None
    if type(size) is not int or not 0 <= size <= MAX_MEMBER_SIZE:
        raise SnapshotError(t("backup.snapshot.file_damaged", owner_language(), name=name))
    return size


def _manifest_bytes(manifest: dict[str, Any]) -> bytes:
    with io.BytesIO() as output:
        for chunk in json.JSONEncoder(indent=2).iterencode(manifest):
            content = chunk.encode("utf-8")
            if output.tell() + len(content) > MAX_MANIFEST_BYTES:
                raise SnapshotError(t("backup.snapshot.manifest_too_large", owner_language()))
            output.write(content)
        return output.getvalue()


def _owned_manifest(path: Path, key: bytes) -> dict[str, Any] | None:
    """Only a signed copy proves that TOW owns this folder and may prune it."""
    try:
        manifest = _read_manifest(path)
    except SnapshotError:
        return None
    return manifest if manifest["format"] == FORMAT and _signature_matches(manifest, key) else None


def backup_root(cfg: dict[str, Any] | None = None) -> Path:
    """The night copies folder: ``backup_dir`` from config.yaml, by default ``backup/night``
    next to config.yaml (moves with the install); a network share (\\\\server\\share) works too.
    A path of another system (a Windows config on Linux) is refused, never created."""
    from tow.locations import NIGHT, LocationError, resolve_checked

    value = str((cfg if cfg is not None else load_config()).get("backup_dir") or "").strip()
    try:
        root = resolve_checked(value, NIGHT)
    except LocationError as exc:
        raise SnapshotError(str(exc)) from exc
    data = data_dir().resolve()
    if root.resolve() == data or data in root.resolve().parents:
        raise SnapshotError(t("backup.snapshot.inside_data", owner_language()))
    return root


def _snapshots(root: Path) -> list[Path]:
    if not root.is_dir():
        return []
    return sorted(
        p
        for p in root.iterdir()
        if p.is_dir()
        and not p.is_symlink()
        and not p.is_junction()
        and p.name.startswith(_PREFIX)
        and (p / "MANIFEST.json").is_file()
    )


def _previous_members(root: Path, key: bytes) -> set[str]:
    """Stores a signed earlier copy proves existed; their later disappearance is not normal."""
    present: set[str] = set()
    for folder in _snapshots(root):
        if manifest := _owned_manifest(folder, key):
            present.update(
                name for name in manifest["files"] if name in {"state.json", "download_history.json", "secrets.enc"}
            )
    return present


def create_snapshot(*, keep: int | None = None, how: str = "auto") -> dict[str, Any]:
    try:
        with persistence_lock():  # creation, read-back and pruning cannot race another copy
            result = _create_snapshot(keep=keep, how=how)
            location = Path(result["snapshot"]).parent
            fields: dict[str, Any] = {"location": str(location), "cleanup_inventory": None}
            # A diagnostic failure cannot invalidate a verified copy. A missing binding
            # remains unknown, rather than binding an earlier result to a later copy.
            with contextlib.suppress(OSError, ValueError, UnicodeError):
                fields["location"] = str(location.resolve())
                fields["cleanup_inventory"] = _cleanup_inventory(location)
            _record(
                last_ok_at=round(time.time(), 3),
                last_snapshot=Path(result["snapshot"]).name,
                last_bytes=result["bytes"],
                last_missing=result["missing"],
                last_cleanup_pending=bool(result.get("cleanup_warning")),
                location=fields["location"],
                cleanup_inventory=fields.get("cleanup_inventory"),
                last_error="",
            )
    except SnapshotError as exc:
        record_failure(str(exc))
        raise
    return result


def list_snapshots(limit: int | None = 10) -> list[dict[str, Any]]:
    """Newest night copies first: name, time, size (no verification)."""
    try:
        root = backup_root()
        folders = _snapshots(root)
    except SnapshotError, OSError:
        return []
    rows = []
    for folder in reversed(folders if limit is None else folders[-limit:]):
        try:
            manifest = _read_manifest(folder)
            created = datetime.fromisoformat(str(manifest.get("created_at")))
            created_ts = created.timestamp()
            size = sum(_member_size(name, meta) for name, meta in manifest["files"].items())
        except SnapshotError, OSError, ValueError, TypeError, AttributeError, OverflowError:
            continue
        rows.append(
            {
                "name": folder.name,
                "created_ts": created_ts,
                "created_at": created.isoformat(),
                "bytes": size,
                "version": manifest.get("version"),
            }
        )
    return rows


def snapshot_path(name: str) -> Path:
    """A night copy by name, refusing anything that is not one (no paths, no ..)."""
    if not name.startswith(_PREFIX) or not all(ch.isalnum() or ch in "-_" for ch in name):
        raise SnapshotError(t("backup.snapshot.unknown", owner_language()))
    root = backup_root()
    path = root / name
    if path.is_symlink() or path.is_junction() or path.resolve().parent != root.resolve():
        raise SnapshotError(t("backup.snapshot.unknown", owner_language()))
    if not (path / "MANIFEST.json").is_file():
        raise SnapshotError(t("backup.snapshot.unknown", owner_language()))
    return path


def check_snapshot(name: str) -> dict[str, Any]:
    with persistence_lock():
        return verify_snapshot(snapshot_path(name))


def snapshot_delete_view(name: str) -> dict[str, Any]:
    with persistence_lock():
        try:
            path = snapshot_path(name)
            manifest = _owned_manifest(path, _signing_key())
            fixed = {member for member, _target in _fixed_members()}
            if manifest is None or any(
                member not in fixed and _POINT_MEMBER.fullmatch(member) is None for member in manifest["files"]
            ):
                raise ValueError("copy ownership could not be verified")
            if not _owned_tree(path, {"MANIFEST.json", *manifest["files"]}):
                raise ValueError("copy contains foreign files")
            return {"name": name, "created_at": manifest["created_at"], "revision": copy_revision(path)}
        except (OSError, ValueError, KeyError) as exc:
            raise SnapshotError(t("web.backup.delete_failed", owner_language())) from exc


def delete_snapshot(name: str, revision: str) -> dict[str, Any]:
    with persistence_lock():
        view = snapshot_delete_view(name)
        if not revision or view["revision"] != revision:
            raise SnapshotError(t("web.backup.delete_stale", owner_language()))
        path = snapshot_path(name)
        manifest = _owned_manifest(path, _signing_key())
        if manifest is None:
            raise SnapshotError(t("web.backup.delete_failed", owner_language()))
        before = _cleanup_status(cfg=None)
        removed = _remove_copy(path, path.parent, "MANIFEST.json", {"MANIFEST.json", *manifest["files"]})
        # Rebind a known observation; explicit deletion never clears an earlier warning.
        if not removed or type(before["pending"]) is bool:
            with contextlib.suppress(OSError, ValueError, UnicodeError):
                _record(
                    last_cleanup_pending=not removed or before["pending"],
                    location=str(path.parent.resolve()),
                    cleanup_inventory=_cleanup_inventory(path.parent),
                )
        if not removed:
            raise SnapshotError(t("web.backup.delete_failed", owner_language()))
        return {"ok": True, "deleted": name}


def _copy_snapshot_member(source: Path, destination: Path, name: str) -> dict[str, Any] | None:
    """Copy/hash the same bounded blocks; a missing source is distinct from an unsafe one."""
    lang = owner_language()
    try:
        if not _ordinary_file(source, missing=True):
            return None
        digest = hashlib.sha256()
        seen = 0
        with source.open("rb") as reader:
            before = os.fstat(reader.fileno())
            size = before.st_size
            if not stat.S_ISREG(before.st_mode) or type(size) is not int or not 0 <= size <= MAX_MEMBER_SIZE:
                raise ValueError("source is not a regular file with a supported size")
            if name == "config.yaml" and size > MAX_INPUT_BYTES:
                raise SnapshotError(str(YamlLimitError("yaml_limits.size")))
            destination.parent.mkdir(parents=True, exist_ok=True)
            with destination.open("xb") as writer:
                while block := reader.read(min(_READ_CHUNK_BYTES, size - seen + 1)):
                    seen += len(block)
                    if seen > size:
                        raise ValueError("source grew while reading")
                    if writer.write(block) != len(block):
                        raise SnapshotError(t("backup.snapshot.write_incomplete", lang, name=name))
                    digest.update(block)
                after = os.fstat(reader.fileno())
                if seen != size or after.st_size != size or after.st_mtime_ns != before.st_mtime_ns:
                    raise ValueError("source changed while reading")
                writer.flush()
                os.fsync(writer.fileno())
        return {"sha256": digest.hexdigest(), "size": seen}
    except ValueError as exc:
        raise SnapshotError(t("backup.snapshot.source_changed", lang, name=name)) from exc


def _write_snapshot_manifest(path: Path, manifest: dict[str, Any]) -> None:
    content = _manifest_bytes(manifest)
    with path.open("xb") as writer:
        if writer.write(content) != len(content):
            raise SnapshotError(t("backup.snapshot.write_incomplete", owner_language(), name="MANIFEST.json"))
        writer.flush()
        os.fsync(writer.fileno())


def _create_snapshot(*, keep: int | None, how: str) -> dict[str, Any]:
    cfg = load_config()
    root = backup_root(cfg)
    policy = retention_settings(cfg)
    if keep:
        policy.update(mode="count", keep=max(1, keep))
    keep = int(policy["keep"])
    stamp = datetime.now(UTC).astimezone().strftime("%Y%m%d-%H%M%S")
    target = root / f"{_PREFIX}{stamp}"
    partial = root / f".{_PREFIX}{stamp}.partial"
    while target.exists() or partial.exists():
        suffix = uuid.uuid4().hex[:6]
        target = root / f"{_PREFIX}{stamp}-{suffix}"
        partial = root / f".{_PREFIX}{stamp}-{suffix}.partial"
    files: dict[str, dict[str, Any]] = {}
    missing: list[str] = []
    key = _signing_key()  # no key, no copy: an unsigned copy could never be restored
    previous_members = _previous_members(root, key)
    partial_created = False
    try:
        members = _members()
        _check_copy_space(root, members)
        partial.mkdir(parents=True, exist_ok=False)
        partial_created = True
        with persistence_lock():  # a consistent cut: no check or edit writes meanwhile
            for name, source in members:
                guard = locked_log_path() if name == "tow.jsonl" else contextlib.nullcontext(source)
                with guard as stable_source:
                    meta = _copy_snapshot_member(stable_source, partial / name, name)
                if meta is None:
                    if name == "config.yaml" or name in previous_members:
                        raise SnapshotError(t("backup.snapshot.source_missing", owner_language(), name=name))
                    if name not in _OPTIONAL_MEMBERS:
                        missing.append(name)  # said in the result, the log and the MANIFEST
                    continue
                files[name] = meta
        manifest = {
            "format": FORMAT,
            "created_at": datetime.now(UTC).isoformat(),
            "version": __version__,
            "files": files,
            "missing": missing,
        }
        manifest["signature"] = _signature(manifest, key)
        _write_snapshot_manifest(partial / "MANIFEST.json", manifest)
        _rename_with_retry(partial, target)
    except SnapshotError:
        if partial_created:
            shutil.rmtree(partial, ignore_errors=True)
        raise
    except OSError as exc:
        if partial_created:
            shutil.rmtree(partial, ignore_errors=True)
        raise SnapshotError(
            t("backup.snapshot.cannot_write", owner_language(), reason=exc.strerror or type(exc).__name__)
        ) from exc
    try:
        verify_snapshot(target)  # read every file back from the copies folder before deleting any older copy
    except SnapshotError as exc:
        raise SnapshotError(t("backup.snapshot.unverified", owner_language(), name=target.name, reason=exc)) from exc
    # Never the copy just verified, even when the clock went back and it does not sort last.
    pruned, cleanup_pending = _prune_night_copies(root, target, keep, key, policy=policy)
    size = sum(item["size"] for item in files.values())
    log_event(
        "backup_created",
        snapshot=target.name,
        files=len(files),
        bytes=size,
        pruned=len(pruned),
        cleanup_pending=cleanup_pending,
        missing=missing,
        how=how,
    )
    result = {
        "ok": True,
        "snapshot": str(target),
        "files": len(files),
        "bytes": size,
        "pruned": pruned,
        "missing": missing,
    }
    if cleanup_pending:
        result["cleanup_warning"] = t("backup.snapshot.cleanup_warning", owner_language())
        log_event("backup_cleanup_pending", copy_kind="night", how=how)
    return result


def _check_copy_space(root: Path, members: list[tuple[str, Path]]) -> None:
    """Keep room for a complete new copy; never erase the old one to make space."""
    size = 0
    for name, source in members:
        try:
            if _ordinary_file(source, missing=True):
                size += source.stat().st_size
        except ValueError as exc:
            raise SnapshotError(t("backup.snapshot.source_changed", owner_language(), name=name)) from exc
    existing = root
    while not existing.exists() and existing != existing.parent:
        existing = existing.parent
    required = size + MAX_MANIFEST_BYTES + max(32 * MIB, size // 20)
    if shutil.disk_usage(existing).free < required:
        raise SnapshotError(t("backup.snapshot.no_space", owner_language()))


def _copy_disk_bytes(folder: Path) -> int:
    # Ownership and ordinary-tree checks precede this; no links or foreign directories.
    total = 0
    for member in folder.iterdir():
        if member.name == "restore-points":
            total += sum(point.stat().st_size for point in member.iterdir())
        else:
            total += member.stat().st_size
    return total


def _prune_night_copies(
    root: Path, target: Path, keep: int, key: bytes, *, policy: dict[str, Any] | None = None
) -> tuple[list[str], bool]:
    removed: list[str] = []
    pending = False
    try:
        others = []
        entries = []
        fixed = {name for name, _path in _fixed_members()}
        for folder in _snapshots(root):
            manifest = _owned_manifest(folder, key)
            if manifest is None or any(
                name not in fixed and _POINT_MEMBER.fullmatch(name) is None for name in manifest["files"]
            ):
                continue
            if _owned_tree(folder, {"MANIFEST.json", *manifest["files"]}):
                created = copy_time(str(manifest["created_at"]))
                entries.append((folder.name, created, _copy_disk_bytes(folder)))
                if folder != target:
                    others.append((folder, {"MANIFEST.json", *manifest["files"]}))
        chosen = policy or {**retention_settings({"backup_keep": keep}), "keep": max(1, keep)}
        anchor = next((when for name, when, _size in entries if name == target.name), None)
        if anchor is None:
            return [], True  # the verified new copy no longer proves ownership; retain older copies
        retained, pending = retained_copies(entries, newest=target.name, now=anchor, policy=chosen)
        for folder, expected in others:
            if folder.name in retained:
                continue
            if _remove_copy(folder, root, "MANIFEST.json", expected):
                removed.append(folder.name)
            else:
                pending = True
    except OSError, ValueError, TypeError, KeyError, OverflowError, SnapshotError:
        pending = True  # the new verified copy is usable even when cleanup cannot be checked
    return removed, pending


def _owned_tree(folder: Path, expected: set[str]) -> bool:
    """No foreign file, special file or link may be swept up with an owned copy."""
    _ordinary_directory(folder)
    for path in folder.iterdir():
        if path.name == "restore-points":
            _ordinary_directory(path)
            for point in path.iterdir():
                if f"restore-points/{point.name}" not in expected:
                    return False
                _ordinary_file(point)
        elif path.name in expected:
            _ordinary_file(path)
        else:
            return False
    return True


def _remove_copy(folder: Path, parent: Path, marker: str, expected: set[str]) -> bool:
    """Remove an already-owned copy, retaining its proof until the final directory step."""
    try:
        if Path(marker).name != marker or marker not in expected:
            return False
        _ordinary_directory(folder)
        if folder.resolve().parent != parent.resolve():
            return False
        proof = folder / marker
        _ordinary_file(proof)
        limit = MAX_MANIFEST_BYTES if marker == "MANIFEST.json" else MAX_RESTORE_JOURNAL_BYTES
        content = _read_metadata_bytes(proof, limit=limit)
        if not _owned_tree(folder, expected):
            return False
        for child in sorted(folder.iterdir()):
            if child == proof:
                continue
            if child.name == "restore-points":
                _ordinary_directory(child)
                if not _owned_tree(folder, expected):
                    return False
                shutil.rmtree(child)
            else:
                if child.name not in expected:
                    return False
                _ordinary_file(child)
                child.unlink()
            try:
                child.lstat()
            except FileNotFoundError:
                continue
            return False  # a no-op or partial deletion is not a removed member
        proof.unlink()
        # Verify below, and retain the proof if the final directory is still held.
        with contextlib.suppress(OSError):
            folder.rmdir()
        try:
            folder.lstat()
        except FileNotFoundError:
            return True
        _ordinary_directory(folder)
        if folder.resolve().parent == parent.resolve():
            # Never overwrite a marker that a concurrent writer supplied.
            with proof.open("xb") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
    except OSError, ValueError:
        return False
    return False


def _rename_with_retry(source: Path, target: Path, *, attempts: int = 6) -> None:
    """A folder just written can be held for a moment by an antivirus or the search indexer."""
    for attempt in range(attempts):
        try:
            source.rename(target)
            return
        except PermissionError:
            if attempt == attempts - 1:
                raise
            time.sleep(0.05 * 2**attempt)


def _verified_member(source: Path, name: str, meta: Any, *, collect: bool) -> bytes | None:
    """Hash one regular file in bounded blocks, optionally retaining exactly those bytes."""
    size = _member_size(name, meta)
    lang = owner_language()
    if name == "config.yaml" and size > MAX_INPUT_BYTES:
        raise SnapshotError(str(YamlLimitError("yaml_limits.size")))
    digest = hashlib.sha256()
    content = bytearray() if collect else None
    seen = 0
    try:
        _ordinary_directory(source.parent)
        _ordinary_file(source)
        with source.open("rb") as handle:
            info = os.fstat(handle.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size != size:
                raise ValueError("member is not a regular file of the expected size")
            while block := handle.read(min(_READ_CHUNK_BYTES, size - seen + 1)):
                seen += len(block)
                if seen > size:
                    raise ValueError("member grew while reading")
                digest.update(block)
                if content is not None:
                    content.extend(block)
    except OSError as exc:
        raise SnapshotError(t("backup.snapshot.file_missing", lang, name=name)) from exc
    except (ValueError, OverflowError) as exc:
        raise SnapshotError(t("backup.snapshot.file_damaged", lang, name=name)) from exc
    if seen != size or digest.hexdigest() != meta.get("sha256"):
        raise SnapshotError(t("backup.snapshot.file_damaged", lang, name=name))
    return bytes(content) if content is not None else None


def _read_verified(
    path: Path, *, retain: bool = True, require_signed: bool = False
) -> tuple[dict[str, Any], dict[str, bytes], bool]:
    """(manifest, contents by member name, signed): all hashes pass before payload parsing.

    The bytes returned are the ones whose hash was checked, so a restore writes exactly
    what was verified (no second read the copies folder could change in between).
    Verification and preview hash everything, then re-read/hash only semantic stores
    one at a time: no parser sees changed bytes or an initially damaged copy. Restore
    reads each file once and retains its checked bytes, never re-reading for a write.
    A signed MANIFEST must carry this install's signature; an unsigned one (made before
    1.17) is still checked file by file, and the caller decides what it may be used for.
    """
    path = Path(path)
    lang = owner_language()
    manifest = _read_manifest(path)
    signed = manifest["format"] == FORMAT
    if signed and not _signature_matches(manifest, _signing_key()):
        raise SnapshotError(t("backup.snapshot.bad_signature", lang))
    if require_signed and not signed:
        raise SnapshotError(t("backup.snapshot.unsigned", lang))
    contents: dict[str, bytes] = {}
    for name, meta in manifest["files"].items():
        _member_target(name)  # a strict name, and a target that stays where it belongs
        content = _verified_member(path / name, name, meta, collect=retain)
        if content is not None:
            contents[name] = content
        del content
    if retain:
        if "config.yaml" in contents:
            _snapshot_config(contents["config.yaml"])
        _check_payloads(contents)
    else:
        for name in _VALIDATED_MEMBERS:
            if name not in manifest["files"]:
                continue
            content = _verified_member(path / name, name, manifest["files"][name], collect=True)
            assert content is not None
            if name == "config.yaml":
                _snapshot_config(content)
            else:
                _check_payloads({name: content})
            del content  # the next store must not overlap a previous verification-only buffer
    return manifest, contents, signed


def verify_snapshot(path: Path) -> dict[str, Any]:
    """The MANIFEST when hashes and store contents pass (``signed`` permits restoration)."""
    manifest, _contents, signed = _read_verified(path, retain=False)
    return {**manifest, "signed": signed}


def _check_payloads(contents: dict[str, bytes]) -> None:
    """Checksums alone do not make a usable copy; validate the exact bytes before any write or prune."""
    for name, validate in (
        ("state.json", validate_state_bytes),
        ("download_history.json", validate_download_history_bytes),
    ):
        if name in contents:
            try:
                validate(contents[name])
            except (UnicodeError, ValueError, RecursionError, StateVersionError) as exc:
                raise SnapshotError(t("backup.snapshot.file_unusable", owner_language(), name=name)) from exc
    for name, decrypt in (
        ("secrets.enc", decrypt_secrets_bytes),
        ("secrets-undo.enc", decrypt_secret_undo_bytes),
    ):
        if name in contents:
            try:
                decrypt(contents[name])
            except SecretStoreError as exc:
                raise SnapshotError(t("backup.snapshot.secrets_unusable", owner_language(), name=name)) from exc


def restore_snapshot(path: Path, *, apply: bool = False) -> dict[str, Any]:
    path = Path(path)
    manifest, contents, signed = _read_verified(path, retain=apply, require_signed=apply)
    result: dict[str, Any] = {
        "ok": True,
        "snapshot": str(path),
        "created_at": manifest.get("created_at"),
        "version": manifest.get("version"),
        "files": sorted(manifest["files"]),
        "signed": signed,
        "applied": False,
    }
    if not apply:
        return result
    with persistence_lock():
        before_interval = _interval()
        plan = _restore_plan(contents)  # everything merged and checked before the first write
        safety, cleanup_pending = _apply_restore(plan, snapshot=path.name)
    result.update({"applied": True, "safety_copy": str(safety)})
    if cleanup_pending:
        result["cleanup_warning"] = t("backup.snapshot.restore_cleanup_warning", owner_language())
    after_interval = _interval()
    if after_interval != before_interval:
        result["interval_changed"] = {"from": before_interval, "to": after_interval}
    log_event("backup_restored", snapshot=path.name, files=len(manifest["files"]), how="manual")
    return result


def _interval() -> int:
    try:
        return interval_sec_of(load_config())
    except TypeError, ValueError, OSError, yaml.YAMLError:  # a broken live config is what a restore fixes
        return 3600


# --- all-or-nothing restore --------------------------------------------------------------------
#
# Every new file is merged and checked in memory first. Then the live files the restore will
# change are copied to data/before-restore-<stamp>/ with a journal (RESTORE.json) and a marker
# in data/ that points at it. Only then are the files replaced, one atomic write each. Any error
# puts the saved files back; a crash leaves the marker, and the next process that takes the
# persistence lock (any TOW process: store.py imports this module when it sees the marker)
# puts them back before anything else is written. A marker that cannot be read or acted on
# (an antivirus holding the journal, a damaged file) is never dropped: after a few tries every
# write is refused with the reason until it is resolved (fail closed, as the site and check
# journals). Once a restore is committed or rolled back its before-restore copy no longer
# keeps the settings undo (that undo cannot be applied any more), and only the newest
# SAFETY_KEEP copies stay.

_MARKER = ".tow-night-restore.json"
_JOURNAL = "RESTORE.json"
_JOURNAL_FORMAT = "tow-night-restore/v1"
MAX_RESTORE_MARKER_BYTES = 64 * 1024
# A journal carries names/checksums, not payloads. Allow ample encoding overhead
# above the entire supported manifest; these limits never cap state or history.
MAX_RESTORE_JOURNAL_BYTES = 4 * MAX_MANIFEST_BYTES
_SAFETY_NAME = re.compile(r"before-restore-[0-9]{8}-[0-9]{6}(?:-[0-9a-f]{6})?")
SAFETY_KEEP = 3
_RECOVERY_ATTEMPTS = 5
# Secrets a finished before-restore copy must not keep: the settings undo of that moment.
_UNDO_IN_SAFETY = "secrets-undo.enc"
# A copy made when one of these stores did not exist yet: the restore removes the live one.
_REMOVED_WHEN_ABSENT = ("state.json", "download_history.json", "secrets-undo.enc")


def _restore_plan(contents: dict[str, bytes]) -> list[tuple[str, Path, bytes | None]]:
    """(member, live target, new bytes or None to remove) - config.yaml last, so that the
    targets of restore points are those of the config in force while the files are written."""
    plan: list[tuple[str, Path, bytes | None]] = []
    for name, content in contents.items():
        if name == "config.yaml":
            content = _keep_local_access(content)
        elif name == "secrets.enc":
            content = _keep_local_password(content)
        plan.append((name, _member_target(name), content))
    if "secrets.enc" not in contents and encrypted_secrets_path().is_file():
        plan.append(("secrets.enc", encrypted_secrets_path(), _keep_local_password(None)))
    plan.extend(
        (name, _member_target(name), None)
        for name in _REMOVED_WHEN_ABSENT
        if name not in contents and _member_target(name).is_file()
    )
    return sorted(plan, key=lambda step: step[0] == "config.yaml")


def _reason(exc: BaseException) -> str:
    if isinstance(exc, OSError):
        return exc.strerror or type(exc).__name__  # never a local path
    return str(exc) or type(exc).__name__


def _begin_restore(plan: list[tuple[str, Path, bytes | None]], *, snapshot: str) -> Path:
    """The before-restore copy and its journal, then the marker; nothing live is touched yet."""
    stamp = datetime.now(UTC).astimezone().strftime("%Y%m%d-%H%M%S")
    safety = data_dir() / f"before-restore-{stamp}"
    if safety.exists():
        safety = data_dir() / f"before-restore-{stamp}-{uuid.uuid4().hex[:6]}"
    entries = []
    try:
        safety.mkdir(parents=True, exist_ok=False)
        for name, target, _content in plan:
            if _ordinary_file(target, missing=True):
                saved = target.read_bytes()
                atomic_write_bytes(safety / name, saved)
                entries.append({"name": name, "existed": True, "sha256": hashlib.sha256(saved).hexdigest()})
            else:
                entries.append({"name": name, "existed": False})
        journal = {"format": _JOURNAL_FORMAT, "snapshot": snapshot, "entries": entries}
        # Every final status must remain writable before publishing a recovery marker.
        for status in ("prepared", "committed", "rolled_back"):
            _journal_bytes(journal, status)
        _write_journal(safety, journal, "prepared")
        _rollback_plan(safety)  # verify every just-written copy before publishing the marker
        marker = _restore_record_bytes({"safety": safety.name}, limit=MAX_RESTORE_MARKER_BYTES)
        atomic_write_bytes(data_dir() / _MARKER, marker)
    except (OSError, ValueError, TypeError, RecursionError, yaml.YAMLError) as exc:
        # A failed exclusive mkdir may belong to another writer. Retain partial
        # copies too: a marker published just before an I/O error still needs them.
        raise SnapshotError(t("backup.snapshot.restore_failed", owner_language(), reason=_reason(exc))) from exc
    return safety


def _read_metadata_bytes(path: Path, *, limit: int) -> bytes:
    """Bound a regular opened record before allocating or interpreting its contents."""
    try:
        _ordinary_file(path)
    except ValueError as exc:
        raise ValueError(t("backup.snapshot.record_type", owner_language())) from exc
    with path.open("rb") as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode):
            raise ValueError(t("backup.snapshot.record_type", owner_language()))
        if not 0 <= info.st_size <= limit:
            raise ValueError(t("backup.snapshot.record_size", owner_language()))
        raw = handle.read(info.st_size + 1)
    if len(raw) > limit:
        raise ValueError(t("backup.snapshot.record_size", owner_language()))
    if len(raw) != info.st_size:
        raise ValueError(t("backup.snapshot.record_changed", owner_language()))
    return raw


def _read_restore_record(path: Path, *, limit: int) -> dict[str, Any]:
    raw = _read_metadata_bytes(path, limit=limit)
    try:
        value = decode_json_bytes(raw)
    except (UnicodeError, ValueError, RecursionError) as exc:
        raise ValueError(t("backup.snapshot.record_invalid", owner_language())) from exc
    if not isinstance(value, dict):
        raise TypeError(t("backup.snapshot.record_invalid", owner_language()))
    return value


def _restore_record_bytes(value: dict[str, Any], *, limit: int) -> bytes:
    """Preflight strict JSON and its byte budget before replacing any existing record."""
    with io.BytesIO() as output:
        for chunk in json.JSONEncoder(indent=2, allow_nan=False).iterencode(value):
            content = chunk.encode("utf-8")
            if output.tell() + len(content) + 1 > limit:
                raise ValueError(t("backup.snapshot.record_size", owner_language()))
            output.write(content)
        output.write(b"\n")
        raw = output.getvalue()
    decode_json_bytes(raw)  # the same finite-number and depth contract as the reader
    return raw


def _journal_bytes(journal: dict[str, Any], status: str) -> bytes:
    # Fixed-width dates leave the same budget for every phase, including exact-second clocks.
    journal = {**journal, "status": status, "updated_at": datetime.now(UTC).isoformat(timespec="microseconds")}
    _validate_restore_journal(journal)
    return _restore_record_bytes(journal, limit=MAX_RESTORE_JOURNAL_BYTES)


def _write_journal(safety: Path, journal: dict[str, Any], status: str) -> None:
    atomic_write_bytes(safety / _JOURNAL, _journal_bytes(journal, status))


def _read_journal(safety: Path) -> dict[str, Any]:
    _ordinary_directory(safety)
    journal = _read_restore_record(safety / _JOURNAL, limit=MAX_RESTORE_JOURNAL_BYTES)
    _validate_restore_journal(journal)
    return journal


def _validate_restore_journal(journal: dict[str, Any]) -> None:
    if (
        not isinstance(journal, dict)
        or journal.get("format") != _JOURNAL_FORMAT
        or not isinstance(journal.get("status"), str)
        or journal["status"] not in {"prepared", "committed", "rolled_back"}
        or not isinstance(journal.get("entries"), list)
    ):
        raise ValueError("not a night-copy restore journal")
    names: set[str] = set()
    fixed = dict(_fixed_members())
    for entry in journal["entries"]:
        if not isinstance(entry, dict):
            raise TypeError("malformed night-copy restore entry")
        name = entry.get("name")
        if not isinstance(name, str) or (name not in fixed and _POINT_MEMBER.fullmatch(name) is None):
            raise ValueError("unexpected night-copy restore member")
        if name in names or not isinstance(entry.get("existed"), bool):
            raise ValueError("malformed night-copy restore entry")
        names.add(name)
        checksum = entry.get("sha256")
        if entry["existed"]:
            if not isinstance(checksum, str) or re.fullmatch(r"[0-9a-f]{64}", checksum) is None:
                raise ValueError("malformed night-copy restore checksum")
        elif checksum is not None:
            raise ValueError("unexpected night-copy restore checksum")


def _ordinary_directory(path: Path, *, missing: bool = False) -> bool:
    try:
        info = path.lstat()
    except FileNotFoundError:
        if missing:
            return False
        raise
    if not stat.S_ISDIR(info.st_mode) or getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT:
        raise ValueError("night-copy restore directory is unsafe")
    return True


def _ordinary_file(path: Path, *, missing: bool = False) -> bool:
    try:
        info = path.lstat()
    except FileNotFoundError:
        if missing:
            return False
        raise
    if not stat.S_ISREG(info.st_mode) or getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT:
        raise ValueError("night-copy restore file is unsafe")
    return True


def _rollback_target(name: str, config_bytes: bytes | None) -> Path:
    fixed = dict(_fixed_members())
    if name in fixed or config_bytes is None:
        return _member_target(name)
    # Point targets belong to the saved config, not to a partially restored one.
    # Resolve them without temporarily replacing the live config during preflight.
    from tow.locations import MANUAL, resolve_checked

    config = load_yaml(config_bytes)
    if not isinstance(config, dict):
        raise TypeError("night-copy restore config is malformed")
    match = _POINT_MEMBER.fullmatch(name)
    if match is None or re.fullmatch(r"[0-9]{8}T[0-9]{6}Z-[0-9a-f]{8}", match["id"]) is None:
        raise ValueError("night-copy restore point is malformed")
    root = resolve_checked(str(config.get("restore_points_dir") or ""), MANUAL).resolve()
    return root / f"{match['id']}.towx"


def _rollback_plan(safety: Path) -> list[tuple[str, Path, bytes | None]]:
    journal = _read_journal(safety)
    saved: dict[str, bytes | None] = {}
    for entry in journal["entries"]:
        name = entry["name"]
        backup = safety / name
        if backup.parent != safety:
            _ordinary_directory(backup.parent, missing=not entry["existed"])
        if entry["existed"]:
            _ordinary_file(backup)
            content = backup.read_bytes()
            if hashlib.sha256(content).hexdigest() != entry["sha256"]:
                raise SnapshotError(t("backup.snapshot.file_damaged", owner_language(), name=name))
            saved[name] = content
        else:
            if _ordinary_file(backup, missing=True):
                raise ValueError("unexpected night-copy restore backup")
            saved[name] = None
    plan = []
    for name, content in saved.items():
        target = _rollback_target(name, saved.get("config.yaml"))
        _ordinary_file(target, missing=True)
        if target.parent.exists():
            _ordinary_directory(target.parent)
        plan.append((name, target, content))
    return sorted(plan, key=lambda step: step[0] != "config.yaml")


def _owned_safety(safety: Path, journal: dict[str, Any]) -> bool:
    """Never prune an unrecognized file or descend into a link/junction."""
    expected = {_JOURNAL, *(entry["name"] for entry in journal["entries"] if entry["existed"])}
    return _owned_tree(safety, expected)


def _finish(safety: Path, status: str) -> None:
    """Record the outcome first, then drop the marker: a marker left behind is harmless."""
    _write_journal(safety, _read_journal(safety), status)
    (data_dir() / _MARKER).unlink(missing_ok=True)


def _marked_safety() -> str | None:
    """The before-restore folder the marker names (a restore in progress or to recover), if any."""
    try:
        name = _read_restore_record(data_dir() / _MARKER, limit=MAX_RESTORE_MARKER_BYTES)["safety"]
    except FileNotFoundError:
        return None
    except OSError, UnicodeError, ValueError, KeyError, TypeError, RecursionError:
        return ""  # unreadable: some folder may still be needed, so none is touched
    return name if isinstance(name, str) and _SAFETY_NAME.fullmatch(name) else ""


def _tidy_safety_copies(keep: int = SAFETY_KEEP) -> list[str]:
    """Finished before-restore copies: without the settings undo, the newest ``keep`` only.

    Never the one a marker names, never one whose journal still says ``prepared`` (a restore
    that is not finished needs every file). Best effort: a file held right now goes next time.
    Returns the names of the folders removed.
    """
    return _cleanup_safety_copies(keep)[0]


def _cleanup_safety_copies(keep: int = SAFETY_KEEP) -> tuple[list[str], bool]:
    removed, pending = _prune_safety_copies(keep)
    if pending:
        log_event("backup_cleanup_pending", copy_kind="safety", how="auto")
    return removed, pending


def _prune_safety_copies(keep: int) -> tuple[list[str], bool]:
    marked = _marked_safety()
    if marked == "":
        return [], True
    try:
        folders = sorted(
            p for p in data_dir().iterdir() if p.is_dir() and not p.is_symlink() and _SAFETY_NAME.fullmatch(p.name)
        )
    except OSError:
        return [], True
    finished = []
    pending = False
    for folder in folders:
        if folder.name == marked:
            continue
        try:
            journal = _read_journal(folder)
            if journal["status"] == "prepared" or not _owned_safety(folder, journal):
                continue
        except OSError, UnicodeError, ValueError, KeyError, TypeError, RecursionError:
            pending = True
            continue  # unreadable is not evidence that a restore has finished
        finished.append((folder, {_JOURNAL, *(entry["name"] for entry in journal["entries"] if entry["existed"])}))
        try:
            (folder / _UNDO_IN_SAFETY).unlink(missing_ok=True)
            if _ordinary_file(folder / _UNDO_IN_SAFETY, missing=True):
                pending = True
        except OSError, ValueError:
            pending = True
    removed = []
    for folder, expected in finished[: max(0, len(finished) - keep)]:
        if _remove_copy(folder, data_dir(), _JOURNAL, expected):
            removed.append(folder.name)
        else:
            pending = True
    return removed, pending


def _apply_restore(plan: list[tuple[str, Path, bytes | None]], *, snapshot: str) -> tuple[Path, bool]:
    """Under the data lock, hold one log barrier from safety capture through the final outcome."""
    failure: Exception | None = None
    rollback_failure: Exception | None = None
    with locked_log_path():
        safety = _begin_restore(plan, snapshot=snapshot)
        try:
            for name, target, content in plan:
                if content is None:
                    target.unlink(missing_ok=True)
                    continue
                atomic_write_bytes(target, content)
                if target.read_bytes() != content:
                    raise SnapshotError(t("backup.snapshot.read_back_failed", owner_language(), name=name))
            _finish(safety, "committed")
        except Exception as exc:  # noqa: BLE001 - every transaction failure needs rollback before unlock
            failure = exc
            try:
                _roll_back_locked(safety)
            except Exception as exc:  # noqa: BLE001 - retain the marker and report any failed rollback
                rollback_failure = exc
    # The OS log lock is not reentrant. Cleanup and audit events run only after its release.
    if failure is not None:
        log_event(
            "backup_restore_failed",
            error=_reason(failure),
            rollback="failed" if rollback_failure else "done",
            how="manual",
        )
        if rollback_failure is not None:
            raise SnapshotError(
                t("backup.snapshot.rollback_failed", owner_language(), path=str(safety))
            ) from rollback_failure
        _tidy_safety_copies()
        raise SnapshotError(t("backup.snapshot.restore_failed", owner_language(), reason=_reason(failure))) from failure
    # Cleanup is outside the transaction: its failure must not undo a committed restore.
    return safety, _cleanup_safety_copies()[1]


def _roll_back(safety: Path) -> None:
    """Recover under data-before-log ordering; append/rotation wait for the final read-back."""
    with locked_log_path():
        _roll_back_locked(safety)
    _tidy_safety_copies()


def _roll_back_locked(safety: Path) -> None:
    """Put back every file the journal saved (config.yaml first: it says where restore points live)."""
    try:
        _journal_bytes(_read_journal(safety), "rolled_back")  # legacy final metadata fits before any live write
        plan = _rollback_plan(safety)
    except (OSError, ValueError, TypeError, RecursionError, yaml.YAMLError) as exc:
        raise SnapshotError(t("backup.snapshot.restore_failed", owner_language(), reason=_reason(exc))) from exc
    for name, target, content in plan:
        if content is None:
            target.unlink(missing_ok=True)
        else:
            atomic_write_bytes(target, content)
            if target.read_bytes() != content:
                raise SnapshotError(t("backup.snapshot.read_back_failed", owner_language(), name=name))
    _finish(safety, "rolled_back")


class _MarkerGone(Exception):
    """The marker disappeared while it was read: another process finished the recovery."""


def _marked_journal(marker: Path) -> tuple[Path, str]:
    """(before-restore folder, journal status) the marker points at."""
    try:
        record = _read_restore_record(marker, limit=MAX_RESTORE_MARKER_BYTES)
    except FileNotFoundError as exc:
        raise _MarkerGone from exc
    name = record["safety"]
    if not isinstance(name, str) or _SAFETY_NAME.fullmatch(name) is None:
        raise ValueError("unexpected before-restore folder name")
    safety = data_dir() / name
    return safety, str(_read_journal(safety)["status"])


_recovery_failure_logged = False


def _refuse_writes(exc: BaseException, marker: Path) -> SnapshotError:
    """The restore cannot be checked or undone: every write is refused until it is (logged once)."""
    global _recovery_failure_logged
    if not _recovery_failure_logged:
        _recovery_failure_logged = True
        with contextlib.suppress(Exception):
            log_event("backup_restore_recovery_failed", error=type(exc).__name__, how="auto")
    return SnapshotError(
        t("backup.snapshot.recovery_blocked", owner_language(), reason=_reason(exc), marker=str(marker))
    )


def recover_interrupted_restore() -> None:
    """A restore that a crash stopped half-way is undone before any process writes again.

    The marker is never given up on: a journal held by another program (an antivirus) is tried
    a few times, and one that still cannot be read - or is damaged - refuses every write with
    the reason (fail closed), so a half-applied restore is never left as if it were done.
    """
    marker = data_dir() / _MARKER
    for attempt in range(_RECOVERY_ATTEMPTS):
        try:
            safety, status = _marked_journal(marker)
            break
        except _MarkerGone:
            return
        except OSError as exc:  # held for a moment (antivirus, indexer): try again, then refuse
            if attempt == _RECOVERY_ATTEMPTS - 1:
                raise _refuse_writes(exc, marker) from exc
            time.sleep(0.05 * 2**attempt)
        except (UnicodeError, ValueError, KeyError, TypeError, RecursionError) as exc:
            raise _refuse_writes(exc, marker) from exc
    if status != "prepared":
        marker.unlink(missing_ok=True)
        return
    _roll_back(safety)  # an error here keeps the marker: every later lock tries again (fail closed)
    log_event("backup_restore_recovered", safety_copy=safety.name, how="auto")


def _keep_local_password(copy_secrets: bytes | None) -> bytes:
    """The copy's secrets with this install's CURRENT password (``lan_auth``), as restore points do.

    The owner signs in with the password they know now, not with last night's. A live store that
    cannot be read has no usable password now either (sign-in treats it so), and then the copy's
    is dropped too: the password is set again on this PC.
    """
    try:
        secrets = decrypt_secrets_bytes(copy_secrets) if copy_secrets is not None else {}
    except SecretStoreError as exc:
        raise SnapshotError(t("backup.snapshot.other_key", owner_language(), name="secrets.enc")) from exc
    try:
        password = load_secrets().get("lan_auth")
    except SecretStoreError:
        password = None
    if copy_secrets is not None and secrets.get("lan_auth") == password:
        return copy_secrets  # nothing to change: the copy's own bytes
    if password is None:
        secrets.pop("lan_auth", None)
    else:
        secrets["lan_auth"] = password
    try:
        return encrypt_secrets_bytes(secrets)
    except SecretStoreError as exc:
        raise SnapshotError(
            t("backup.snapshot.cannot_check", owner_language(), name="secrets.enc", error=str(exc))
        ) from exc


def _snapshot_config(content: bytes) -> dict[str, Any]:
    try:
        parsed = load_yaml(content.decode("utf-8"))
    except YamlLimitError as exc:
        raise SnapshotError(str(exc)) from exc
    except (UnicodeError, yaml.YAMLError) as exc:
        raise SnapshotError(t("backup.snapshot.config_not_mapping", owner_language())) from exc
    if parsed is None:
        parsed = {}
    if not isinstance(parsed, dict):
        raise SnapshotError(t("backup.snapshot.config_not_mapping", owner_language()))
    return parsed


def _keep_local_access(snapshot_config: bytes) -> bytes:
    """The copy's config with this machine's access settings (the live ones).

    A live config.yaml that cannot be read does not stop the restore - replacing it is often
    the point - and then the restored config is local-only: network access is turned back on
    from this PC, as always.
    """
    restored = _snapshot_config(snapshot_config)
    try:
        current = load_yaml(read_yaml_text(config_path()))
    except OSError, UnicodeError, yaml.YAMLError:
        current = None
    if isinstance(current, dict):
        for key in _ACCESS_KEYS:
            if key in current:
                restored[key] = current[key]
            else:
                restored.pop(key, None)
    else:
        restored.update(bind="127.0.0.1", allow_lan=False)
    try:
        return dump_yaml(restored).encode("utf-8")
    except YamlLimitError as exc:
        raise SnapshotError(str(exc)) from exc
