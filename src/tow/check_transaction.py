from __future__ import annotations

import hashlib
import json
import stat
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from tow.paths import data_dir, download_history_path, state_path
from tow.store import atomic_write_bytes, decode_json_bytes, persistence_lock

_FORMAT = "tow-check-transaction/v1"
_MARKER = "TRANSACTION.json"
_BACKUPS = {"state": "state.before", "history": "download_history.before"}
_TARGETS = {"state": state_path, "history": download_history_path}
_STATUS = {"prepared", "history_committed", "committed"}
_ALLOWED = frozenset({_MARKER, *_BACKUPS.values()})


class CheckTransactionError(RuntimeError):
    """Raised when a check persistence transaction cannot be recovered safely."""


def _root() -> Path:
    root = data_dir() / ".tow-check-transaction"
    if _is_link(root):
        raise CheckTransactionError("check transaction directory must not be a symlink or reparse point")
    return root


def _is_link(path: Path) -> bool:
    if path.is_symlink():
        return True
    try:
        attributes = getattr(path.lstat(), "st_file_attributes", 0)
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise CheckTransactionError(f"cannot inspect check path: {path.name}") from exc
    return bool(attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT)


def _marker(root: Path) -> Path:
    return root / _MARKER


def _digest(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _read_target(path: Path) -> tuple[bool, bytes | None]:
    if _is_link(path):
        raise CheckTransactionError(f"check store must not be a symlink: {path.name} (or reparse point)")
    if not path.exists():
        return False, None
    if not path.is_file():
        raise CheckTransactionError(f"check store is not a regular file: {path.name}")
    try:
        return True, path.read_bytes()
    except OSError as exc:
        raise CheckTransactionError(f"cannot read check store: {path.name}") from exc


def _snapshot_spec(name: str, content: bytes | None) -> dict[str, Any]:
    exists = content is not None
    return {
        "path": _TARGETS[name]().name,
        "backup": _BACKUPS[name] if exists else None,
        "before_exists": exists,
        "before_sha256": _digest(content) if content is not None else None,
    }


def _write_json(path: Path, value: dict[str, Any]) -> None:
    try:
        content = (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")
    except (TypeError, ValueError, UnicodeError) as exc:
        raise CheckTransactionError("check transaction marker is not serializable") from exc
    atomic_write_bytes(path, content)


def _is_own_temporary(name: str) -> bool:
    """Leftover of an interrupted atomic_write_bytes() into this directory."""
    return name.endswith(".tmp") and any(name.startswith(f".{allowed}.") for allowed in _ALLOWED)


def _validate_root(root: Path, *, marker_required: bool) -> None:
    if not root.is_dir() or _is_link(root):
        raise CheckTransactionError("check transaction directory is invalid")
    try:
        entries = list(root.iterdir())
    except OSError as exc:
        raise CheckTransactionError("cannot inspect check transaction directory") from exc
    if any(
        _is_link(entry) or not entry.is_file() or (entry.name not in _ALLOWED and not _is_own_temporary(entry.name))
        for entry in entries
    ):
        raise CheckTransactionError("check transaction directory contains an unexpected entry")
    if marker_required and not _marker(root).is_file():
        raise CheckTransactionError("check transaction marker is missing")


def _valid_fingerprint(exists: Any, digest: Any) -> bool:
    if not isinstance(exists, bool):
        return False
    if not exists:
        return digest is None
    return isinstance(digest, str) and len(digest) == 64 and all(c in "0123456789abcdef" for c in digest)


def _validate_target(root: Path, name: str, spec: Any, status: str) -> None:
    if not isinstance(spec, dict) or spec.get("path") != _TARGETS[name]().name:
        raise CheckTransactionError("check transaction target binding mismatch")
    if not _valid_fingerprint(spec.get("before_exists"), spec.get("before_sha256")):
        raise CheckTransactionError("malformed check transaction snapshot fingerprint")
    if spec["before_exists"]:
        if spec.get("backup") != _BACKUPS[name]:
            raise CheckTransactionError("check transaction backup binding mismatch")
        backup_path = root / _BACKUPS[name]
        # Committed transactions only need cleanup; an interrupted cleanup may
        # already have removed a backup, and later store writes are legitimate.
        if status != "committed" and (_is_link(backup_path) or not backup_path.is_file()):
            raise CheckTransactionError("check transaction backup is missing")
    elif spec.get("backup") is not None:
        raise CheckTransactionError("malformed absent-store snapshot")


def _load_manifest(root: Path) -> dict[str, Any]:
    try:
        manifest = decode_json_bytes(_marker(root).read_bytes())
    except (OSError, UnicodeError, ValueError, RecursionError) as exc:
        raise CheckTransactionError("check transaction marker is unreadable") from exc
    if not isinstance(manifest, dict) or manifest.get("format") != _FORMAT:
        raise CheckTransactionError("unsupported check transaction marker")
    status = manifest.get("status")
    targets = manifest.get("targets")
    if (
        not isinstance(status, str)
        or status not in _STATUS
        or not isinstance(targets, dict)
        or set(targets) != set(_TARGETS)
    ):
        raise CheckTransactionError("malformed check transaction marker")
    for name, spec in targets.items():
        _validate_target(root, name, spec, status)
    if status == "committed":
        after = manifest.get("after")
        if not isinstance(after, dict) or set(after) != set(_TARGETS):
            raise CheckTransactionError("committed check transaction lacks read-back")
        for spec in after.values():
            if (
                not isinstance(spec, dict)
                or set(spec) != {"exists", "sha256"}
                or not _valid_fingerprint(spec.get("exists"), spec.get("sha256"))
            ):
                raise CheckTransactionError("malformed committed read-back")
    return manifest


def _current_readback(name: str) -> dict[str, Any]:
    exists, content = _read_target(_TARGETS[name]())
    return {"exists": exists, "sha256": _digest(content) if content is not None else None}


def _verified_backups(root: Path, manifest: dict[str, Any]) -> dict[str, bytes]:
    _validate_root(root, marker_required=False)
    backups: dict[str, bytes] = {}
    # Check every target and every backup before the first write or unlink. Keep
    # the verified bytes: rereading a backup afterwards could restore other data.
    for name, spec in manifest["targets"].items():
        _read_target(_TARGETS[name]())
        if spec["before_exists"]:
            exists, content = _read_target(root / _BACKUPS[name])
            if not exists or content is None:
                raise CheckTransactionError(f"cannot restore check store: {_TARGETS[name]().name}")
            if _digest(content) != spec["before_sha256"]:
                raise CheckTransactionError(f"check transaction backup checksum mismatch: {_BACKUPS[name]}")
            backups[name] = content
    return backups


def _restore(root: Path, manifest: dict[str, Any]) -> None:
    backups = _verified_backups(root, manifest)
    for name, spec in manifest["targets"].items():
        target = _TARGETS[name]()
        if spec["before_exists"]:
            try:
                atomic_write_bytes(target, backups[name])
            except OSError as exc:
                raise CheckTransactionError(f"cannot restore check store: {target.name}") from exc
        else:
            exists, _ = _read_target(target)
            if exists:
                try:
                    target.unlink()
                except OSError as exc:
                    raise CheckTransactionError(f"cannot remove new check store: {target.name}") from exc
        actual = _current_readback(name)
        expected = {"exists": spec["before_exists"], "sha256": spec["before_sha256"]}
        if actual != expected:
            raise CheckTransactionError(f"check store restore read-back mismatch: {target.name}")


def _cleanup(root: Path) -> None:
    _validate_root(root, marker_required=False)
    # The marker goes first: an interrupted cleanup then leaves either the complete
    # journal (recovered again) or a folder without a marker (just removed). Removing
    # the backups first left a marker without backups, which blocked every later write.
    marker = _marker(root)
    if marker.exists():
        marker.unlink()
    for name in _BACKUPS.values():
        backup = root / name
        if backup.exists():
            backup.unlink()
    for entry in root.iterdir():
        if _is_own_temporary(entry.name):
            entry.unlink(missing_ok=True)
    try:
        root.rmdir()
    except OSError as exc:
        raise CheckTransactionError("cannot remove check transaction directory") from exc


def recover_locked() -> None:
    root = _root()
    if not root.exists():
        return
    _validate_root(root, marker_required=False)
    marker = _marker(root)
    if not marker.exists():
        _cleanup(root)
        return
    manifest = _load_manifest(root)
    if manifest["status"] != "committed":
        _restore(root, manifest)
    # A committed transaction is final: only its cleanup was interrupted. Later
    # writes (topic edits, other checks) legitimately change the stores, so the
    # stored read-back is not compared again - that used to block every check.
    _cleanup(root)


def recover_check_transaction() -> None:
    """Recover one unfinished check persistence transaction, failing closed."""
    with persistence_lock():
        recover_locked()


@dataclass
class CheckTransaction:
    root: Path
    manifest: dict[str, Any]
    cleanup_pending: bool = False

    def _persist(self) -> None:
        _write_json(_marker(self.root), self.manifest)

    def mark_history_committed(self) -> None:
        self.manifest["status"] = "history_committed"
        self._persist()

    def mark_committed(self) -> None:
        self.manifest["after"] = {name: _current_readback(name) for name in _TARGETS}
        self.manifest["status"] = "committed"
        self._persist()

    def rollback(self) -> None:
        _restore(self.root, self.manifest)
        _cleanup(self.root)

    def finish(self) -> bool:
        try:
            _cleanup(self.root)
        except OSError, CheckTransactionError:
            self.cleanup_pending = True
            return False
        return True


def _begin_locked() -> CheckTransaction:
    # Callers hold the lock for the whole transaction (check_store_transaction). Taking
    # it here too is a no-op for them, and for anyone else it guarantees the recovery
    # hook runs before - never in the middle of - preparing a new transaction.
    with persistence_lock():
        return _begin_under_lock()


def _begin_under_lock() -> CheckTransaction:
    root = _root()
    if root.exists():
        recover_locked()
    created = False
    try:
        root.mkdir(parents=False, exist_ok=False)
        created = True
        contents = {name: _read_target(_TARGETS[name]())[1] for name in _TARGETS}
        manifest: dict[str, Any] = {
            "format": _FORMAT,
            "status": "prepared",
            "targets": {name: _snapshot_spec(name, content) for name, content in contents.items()},
        }
        for name, content in contents.items():
            if content is not None:
                atomic_write_bytes(root / _BACKUPS[name], content)
        _verified_backups(root, manifest)
        _write_json(_marker(root), manifest)
    except (OSError, CheckTransactionError) as exc:
        if created:
            with suppress(OSError, CheckTransactionError):
                _cleanup(root)
        if isinstance(exc, CheckTransactionError):
            raise
        raise CheckTransactionError("cannot prepare check persistence transaction") from exc
    return CheckTransaction(root=root, manifest=manifest)


@contextmanager
def check_store_transaction() -> Iterator[CheckTransaction]:
    """Commit history and state together with durable recovery metadata."""
    with persistence_lock():
        recover_check_transaction()
        transaction = _begin_locked()
        try:
            yield transaction
        except BaseException:
            try:
                transaction.rollback()
            except CheckTransactionError as recovery_exc:
                raise CheckTransactionError("check persistence failed and recovery failed") from recovery_exc
            raise
        else:
            transaction.finish()
