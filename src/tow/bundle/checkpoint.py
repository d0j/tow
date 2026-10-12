"""The import checkpoint: a copy of every store taken before an import writes, its transaction
marker, the recovery of an interrupted import (a step of ``tow.store.RECOVERY_STEPS``), the
rollback and the pruning of finished checkpoints."""

from __future__ import annotations

import re
import shutil
import uuid
from pathlib import Path
from typing import Any

from tow.bundle.errors import ExportImportError
from tow.bundle.io import atomic_write, json_bytes, read_limited, sha256
from tow.clock import iso_now
from tow.errors import Msg
from tow.paths import config_path, data_dir, download_history_path, state_path
from tow.platform import plain_kind
from tow.store import decode_json_bytes, encrypted_secrets_path, persistence_lock, secret_undo_path

CHECKPOINT_FORMAT = "tow-import-checkpoint-v1"
TRANSACTION_FORMAT = "tow-import-transaction-v1"

IMPORT_CHECKPOINTS_KEPT = 5


def _checkpoint_targets() -> list[tuple[str, Path]]:
    return [
        ("config.yaml", config_path()),
        ("state.json", state_path()),
        ("download_history.json", download_history_path()),
        ("secrets.enc", encrypted_secrets_path()),
        ("secrets-undo.enc", secret_undo_path()),
    ]


def create_import_checkpoint() -> Path:
    checkpoint = data_dir() / "import-checkpoints" / f"{iso_now().replace(':', '-')}-{uuid.uuid4().hex[:10]}"
    files_dir = checkpoint / "files"
    files_dir.mkdir(parents=True, exist_ok=False)
    try:
        return _fill_import_checkpoint(checkpoint, files_dir)
    except BaseException:
        # Nothing was imported yet. A half copy of every store (a full disk, a file held open)
        # has no journal, so the pruning never removed it: one more per attempt, on that disk.
        shutil.rmtree(checkpoint, ignore_errors=True)
        raise


def _fill_import_checkpoint(checkpoint: Path, files_dir: Path) -> Path:
    entries = []
    for member, target in _checkpoint_targets():
        exists = checkpoint_file(target, missing=True)
        entry = {"member": member, "target": str(target), "exists": exists}
        if exists:
            content = read_limited(target, label=member)
            backup = files_dir / member
            backup.parent.mkdir(parents=True, exist_ok=True)
            atomic_write(backup, content)
            entry["sha256"] = sha256(content)
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
    atomic_write(checkpoint / "MANIFEST.json", json_bytes(manifest, readable=True))
    _read_checkpoint(checkpoint)  # every copy is intact before the operation can start
    write_import_transaction(checkpoint, status="prepared")
    return checkpoint


def write_import_transaction(checkpoint: Path, *, status: str, **fields: Any) -> None:
    if status not in {"prepared", "committed", "rolled_back"}:
        raise ExportImportError("invalid import transaction status")
    transaction = {
        "format": TRANSACTION_FORMAT,
        "checkpoint": str(Path(checkpoint)),
        "status": status,
        "updated_at": iso_now(),
        **fields,
    }
    atomic_write(Path(checkpoint) / "TRANSACTION.json", json_bytes(transaction, readable=True))


def _read_import_transaction(checkpoint: Path, *, bound: bool = True) -> dict[str, Any]:
    path = Path(checkpoint) / "TRANSACTION.json"
    try:
        _checkpoint_directory(Path(checkpoint))
        checkpoint_file(path)
        value = decode_json_bytes(read_limited(path, label="import transaction"))
    except (OSError, UnicodeError, ValueError, RecursionError) as exc:
        raise ExportImportError("import transaction is unreadable") from exc
    if not isinstance(value, dict) or value.get("format") != TRANSACTION_FORMAT:
        raise ExportImportError("unsupported import transaction")
    if not isinstance(value.get("status"), str) or value["status"] not in {"prepared", "committed", "rolled_back"}:
        raise ExportImportError("invalid import transaction status")
    # Bound by folder name, not by absolute path: a moved install (another drive letter, a
    # renamed folder, another system) still recovers its own interrupted import.
    if not isinstance(value.get("checkpoint"), str) or (
        bound and re.split(r"[\\/]", value["checkpoint"])[-1] != Path(checkpoint).name
    ):
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
        if not checkpoint_file(checkpoint / "TRANSACTION.json", missing=True):
            continue
        transaction = _read_import_transaction(checkpoint, bound=False)
        if transaction["status"] != "prepared":
            continue
        try:
            _rollback_import(checkpoint, apply=True)
            write_import_transaction(checkpoint, status="rolled_back", recovered=True)
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


def checkpoint_file(path: Path, *, missing: bool = False) -> bool:
    try:
        kind = plain_kind(path)
    except OSError as exc:
        raise ExportImportError("import checkpoint file cannot be inspected") from exc
    if kind is None:
        if missing:
            return False
        raise ExportImportError("import checkpoint file is missing") from None
    if kind != "file":
        raise ExportImportError("import checkpoint file is unsafe")
    return True


def _checkpoint_directory(path: Path, *, missing: bool = False) -> bool:
    try:
        kind = plain_kind(path)
    except OSError as exc:
        raise ExportImportError("import checkpoint directory cannot be inspected") from exc
    if kind is None:
        if missing:
            return False
        raise ExportImportError("import checkpoint directory is missing") from None
    if kind != "dir":
        raise ExportImportError("import checkpoint directory is unsafe")
    return True


def _read_checkpoint(checkpoint: Path) -> dict[str, Any]:
    checkpoint = Path(checkpoint)
    if not _checkpoint_directory(checkpoint, missing=True):
        raise ExportImportError(
            "import checkpoint does not exist", owner_text=Msg("cli.bundle.no_checkpoint", path=str(checkpoint))
        )
    try:
        _checkpoint_directory(checkpoint)
        checkpoint_file(checkpoint / "MANIFEST.json")
        data = decode_json_bytes(read_limited(checkpoint / "MANIFEST.json", label="import checkpoint manifest"))
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
        # The member name alone says where a file goes back to; the recorded absolute
        # "target" is informative only (it changes when the install moves).
        member = str(entry["member"])
        exists = entry.get("exists")
        if not isinstance(exists, bool):
            raise ExportImportError("import checkpoint exists flag is invalid")
        checksum = entry.get("sha256")
        backup = files_dir / member
        if exists:
            if not checkpoint_file(backup, missing=True):
                raise ExportImportError(f"import checkpoint backup is missing: {member}")
            content = read_limited(backup, label=f"checkpoint {member}")
            if (
                not isinstance(checksum, str)
                or not re.fullmatch(r"[0-9a-f]{64}", checksum)
                or checksum != sha256(content)
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
            exists = checkpoint_file(target, missing=True)
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
        atomic_write(target, content)
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
        backup = checkpoint / "files" / member
        if entry.get("exists"):
            content = read_limited(backup, label=f"checkpoint {member}")
            if entry.get("sha256") != sha256(content):
                raise ExportImportError(f"import checkpoint checksum mismatch: {member}")
        else:
            content = None
        restore_plan.append((member, target, content))

    write_import_transaction(checkpoint, status="prepared", operation_id=operation_id, phase="rollback")
    restored: list[str] = []
    try:
        for member, target, content in restore_plan:
            if content is not None:
                atomic_write(target, content)
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
            write_import_transaction(
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
        write_import_transaction(checkpoint, status="rolled_back", operation_id=operation_id, phase="rollback")
    except Exception as exc:
        raise ExportImportError("rollback applied but transaction marker update failed") from exc
    return {"ok": True, "checkpoint": str(checkpoint), "restored": restored}


def _owned_checkpoint(checkpoint: Path) -> bool:
    manifest = _read_checkpoint(checkpoint)
    allowed = {"MANIFEST.json", "TRANSACTION.json", "files"}
    if {entry.name for entry in checkpoint.iterdir()} != allowed:
        return False
    members = {entry["member"] for entry in manifest["targets"] if entry["exists"]}
    for entry in (checkpoint / "files").iterdir():
        if entry.name not in members:
            return False
        checkpoint_file(entry)
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
