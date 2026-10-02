from __future__ import annotations

import hashlib
import json
import shutil
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from tow.paths import data_dir, download_history_path, state_path
from tow.store import atomic_write_bytes, persistence_lock

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
    if root.is_symlink():
        raise CheckTransactionError("check transaction directory must not be a symlink")
    return root


def _marker(root: Path) -> Path:
    return root / _MARKER


def _digest(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _read_target(path: Path) -> tuple[bool, bytes | None]:
    if path.is_symlink():
        raise CheckTransactionError(f"check store must not be a symlink: {path.name}")
    if not path.exists():
        return False, None
    if not path.is_file():
        raise CheckTransactionError(f"check store is not a regular file: {path.name}")
    try:
        return True, path.read_bytes()
    except OSError as exc:
        raise CheckTransactionError(f"cannot read check store: {path.name}") from exc


def _current_spec(name: str) -> dict[str, Any]:
    exists, content = _read_target(_TARGETS[name]())
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
    if not root.is_dir() or root.is_symlink():
        raise CheckTransactionError("check transaction directory is invalid")
    try:
        entries = list(root.iterdir())
    except OSError as exc:
        raise CheckTransactionError("cannot inspect check transaction directory") from exc
    if any(
        entry.is_symlink() or (entry.name not in _ALLOWED and not _is_own_temporary(entry.name)) for entry in entries
    ):
        raise CheckTransactionError("check transaction directory contains an unexpected entry")
    if marker_required and not _marker(root).is_file():
        raise CheckTransactionError("check transaction marker is missing")


def _load_manifest(root: Path) -> dict[str, Any]:
    try:
        manifest = json.loads(_marker(root).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise CheckTransactionError("check transaction marker is unreadable") from exc
    if not isinstance(manifest, dict) or manifest.get("format") != _FORMAT:
        raise CheckTransactionError("unsupported check transaction marker")
    status = manifest.get("status")
    targets = manifest.get("targets")
    if status not in _STATUS or not isinstance(targets, dict) or set(targets) != set(_TARGETS):
        raise CheckTransactionError("malformed check transaction marker")
    for name, spec in targets.items():
        if not isinstance(spec, dict) or spec.get("path") != _TARGETS[name]().name:
            raise CheckTransactionError("check transaction target binding mismatch")
        if spec.get("before_exists"):
            backup = spec.get("backup")
            if backup != _BACKUPS[name]:
                raise CheckTransactionError("check transaction backup binding mismatch")
            backup_path = root / backup
            # A committed transaction is final and never restored: its backups are not
            # needed (an interrupted cleanup may already have removed them).
            if status != "committed" and (backup_path.is_symlink() or not backup_path.is_file()):
                raise CheckTransactionError("check transaction backup is missing")
        elif spec.get("backup") is not None or spec.get("before_sha256") is not None:
            raise CheckTransactionError("malformed absent-store snapshot")
    if status == "committed":
        after = manifest.get("after")
        if not isinstance(after, dict) or set(after) != set(_TARGETS):
            raise CheckTransactionError("committed check transaction lacks read-back")
        for spec in after.values():
            if not isinstance(spec, dict) or set(spec) != {"exists", "sha256"}:
                raise CheckTransactionError("malformed committed read-back")
    return manifest


def _current_readback(name: str) -> dict[str, Any]:
    exists, content = _read_target(_TARGETS[name]())
    return {"exists": exists, "sha256": _digest(content) if content is not None else None}


def _restore(root: Path, manifest: dict[str, Any]) -> None:
    for name, spec in manifest["targets"].items():
        target = _TARGETS[name]()
        if spec["before_exists"]:
            backup = root / spec["backup"]
            try:
                atomic_write_bytes(target, backup.read_bytes())
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
    try:
        root.mkdir(parents=False, exist_ok=False)
        manifest: dict[str, Any] = {
            "format": _FORMAT,
            "status": "prepared",
            "targets": {name: _current_spec(name) for name in _TARGETS},
        }
        for name, spec in manifest["targets"].items():
            if spec["before_exists"]:
                atomic_write_bytes(root / spec["backup"], _TARGETS[name]().read_bytes())
        _write_json(_marker(root), manifest)
    except (OSError, CheckTransactionError) as exc:
        if root.exists():
            with suppress(OSError):
                shutil.rmtree(root)
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
