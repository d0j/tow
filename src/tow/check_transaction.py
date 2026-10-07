from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from tow.journal import Copy, Journal, digest, is_digest
from tow.paths import data_dir, download_history_path, state_path
from tow.store import atomic_write_bytes, persistence_lock

_FORMAT = "tow-check-transaction/v1"
_MARKER = "TRANSACTION.json"
_BACKUPS = {"state": "state.before", "history": "download_history.before"}
_TARGETS = {"state": state_path, "history": download_history_path}
_STATUS = {"prepared", "history_committed", "committed"}


class CheckTransactionError(RuntimeError):
    """Raised when a check persistence transaction cannot be recovered safely."""


def _journal() -> Journal:
    """The folder checks, the restore and the cleanup are tow.journal's; the marker is ours."""
    return Journal(
        data_dir() / ".tow-check-transaction",
        _MARKER,
        frozenset({_MARKER, *_BACKUPS.values()}),
        CheckTransactionError,
        label="check transaction",
        store="check store",
    )


def _snapshot_spec(name: str, content: bytes | None) -> dict[str, Any]:
    exists = content is not None
    return {
        "path": _TARGETS[name]().name,
        "backup": _BACKUPS[name] if exists else None,
        "before_exists": exists,
        "before_sha256": digest(content) if content is not None else None,
    }


def _write_json(path: Path, value: dict[str, Any]) -> None:
    try:
        content = (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")
    except (TypeError, ValueError, UnicodeError) as exc:
        raise CheckTransactionError("check transaction marker is not serializable") from exc
    atomic_write_bytes(path, content)


def _valid_fingerprint(exists: Any, sha256: Any) -> bool:
    if not isinstance(exists, bool):
        return False
    return is_digest(sha256) if exists else sha256 is None


def _validate_target(journal: Journal, name: str, spec: Any, status: str) -> None:
    if not isinstance(spec, dict) or spec.get("path") != _TARGETS[name]().name:
        raise CheckTransactionError("check transaction target binding mismatch")
    if not _valid_fingerprint(spec.get("before_exists"), spec.get("before_sha256")):
        raise CheckTransactionError("malformed check transaction snapshot fingerprint")
    if spec["before_exists"]:
        if spec.get("backup") != _BACKUPS[name]:
            raise CheckTransactionError("check transaction backup binding mismatch")
        backup_path = journal.root / _BACKUPS[name]
        # Committed transactions only need cleanup; an interrupted cleanup may
        # already have removed a backup, and later store writes are legitimate.
        if status != "committed" and (journal.is_link(backup_path) or not backup_path.is_file()):
            raise CheckTransactionError("check transaction backup is missing")
    elif spec.get("backup") is not None:
        raise CheckTransactionError("malformed absent-store snapshot")


def _validate_manifest(journal: Journal, manifest: Any) -> dict[str, Any]:
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
        _validate_target(journal, name, spec, status)
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


def _copies(manifest: dict[str, Any]) -> list[Copy]:
    return [
        Copy(_TARGETS[name]().name, _TARGETS[name](), spec["backup"], spec["before_sha256"])
        for name, spec in manifest["targets"].items()
    ]


def _current_readback(name: str) -> dict[str, Any]:
    content = _journal().read(_TARGETS[name]())
    return {"exists": content is not None, "sha256": digest(content) if content is not None else None}


def recover_locked() -> None:
    journal = _journal()

    def to_restore(marker: Any) -> list[Copy] | None:
        manifest = _validate_manifest(journal, marker)
        # A committed transaction is final: only its cleanup was interrupted. Later
        # writes (topic edits, other checks) legitimately change the stores, so the
        # stored read-back is not compared again - that used to block every check.
        return None if manifest["status"] == "committed" else _copies(manifest)

    journal.recover(to_restore)


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
        _write_json(self.root / _MARKER, self.manifest)

    def mark_history_committed(self) -> None:
        self.manifest["status"] = "history_committed"
        self._persist()

    def mark_committed(self) -> None:
        self.manifest["after"] = {name: _current_readback(name) for name in _TARGETS}
        self.manifest["status"] = "committed"
        self._persist()

    def rollback(self) -> None:
        journal = _journal()
        journal.restore(_copies(self.manifest))
        journal.remove()

    def finish(self) -> bool:
        try:
            _journal().remove()
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
    journal = _journal()
    root = journal.root
    if journal.exists():
        recover_locked()
    created = False
    try:
        root.mkdir(parents=False, exist_ok=False)
        created = True
        contents = {name: journal.read(_TARGETS[name]()) for name in _TARGETS}
        manifest: dict[str, Any] = {
            "format": _FORMAT,
            "status": "prepared",
            "targets": {name: _snapshot_spec(name, content) for name, content in contents.items()},
        }
        for name, content in contents.items():
            if content is not None:
                atomic_write_bytes(root / _BACKUPS[name], content)
        journal.verified(_copies(manifest))
        _write_json(root / _MARKER, manifest)
    except (OSError, CheckTransactionError) as exc:
        if created:
            with suppress(OSError, CheckTransactionError):
                journal.remove()
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
