"""An optional second destination for finished, signed nightly copies."""

from __future__ import annotations

import contextlib
import os
import uuid
from pathlib import Path
from typing import Any

from tow.backup_retention import retention_settings
from tow.config import load_config
from tow.locations import REPLICA, install_dir, resolve_checked
from tow.paths import data_dir
from tow.platform import locks
from tow.snapshots.create import (
    PARTIAL_PROOF,
    check_copy_space,
    copy_snapshot_member,
    partial_proof,
    prune_night_copies,
    remove_stale_partials,
    rename_with_retry,
    write_snapshot_manifest,
)
from tow.snapshots.errors import SnapshotError
from tow.snapshots.location import backup_root
from tow.snapshots.manifest import owned_manifest, signature_of, signing_key
from tow.snapshots.members import member_target, ordinary_directory, owned_tree, read_metadata_bytes
from tow.snapshots.record import record_status
from tow.snapshots.verify import verify_snapshot
from tow.storage.locks import init_lock_file, persistence_lock

_MARKER = ".tow-replica"


def replica_message(status: object) -> str:
    return {
        "verified": "settings.backups.replica_verified",
        "cleanup": "settings.backups.replica_cleanup",
        "unavailable": "settings.backups.replica_unavailable",
    }.get(str(status), "")


def _root(raw: str, cfg: dict[str, Any]) -> Path:
    parent = resolve_checked(raw, REPLICA)
    if not raw or not Path(raw).is_absolute():
        raise ValueError("an absolute destination is required")
    for directory in (parent, *parent.parents):
        ordinary_directory(directory)
    root = parent.resolve() / "TOW-nightly"
    primary = backup_root(cfg).resolve()
    if root.is_relative_to(install_dir()) or root.is_relative_to(primary) or primary.is_relative_to(root):
        raise ValueError("backup destinations overlap")
    return root


def _marker(root: Path, key: bytes) -> bytes:
    from tow.folders import volume_at

    binding = {"format": "tow-replica-v1", "path": os.path.normcase(str(root)), "volume": volume_at(str(root))}
    return signature_of(binding, key).encode("ascii")


def configure_replica(raw: str, cfg: dict[str, Any], *, enroll: bool) -> Path:
    """Only explicit local configuration can create and bind the dedicated destination."""
    root = _root(raw, cfg)
    probe = root.parent / f".tow-write-test-{uuid.uuid4().hex}"
    with probe.open("xb"):
        pass
    probe.unlink()
    if not enroll:
        return root
    root.mkdir(exist_ok=True)
    ordinary_directory(root)
    marker = root / _MARKER
    proof = _marker(root, signing_key())
    if marker.exists():
        if read_metadata_bytes(marker, limit=128) != proof:
            raise ValueError("destination belongs to another installation")
    else:
        if any(root.iterdir()):
            raise ValueError("destination contains unrecognised data")
        with marker.open("xb") as stream:
            stream.write(proof)
            stream.flush()
            os.fsync(stream.fileno())
    return root


def _replicate(source: Path, raw: str, cfg: dict[str, Any]) -> str:
    root = _root(raw, cfg)
    ordinary_directory(root)
    key = signing_key()
    if read_metadata_bytes(root / _MARKER, limit=128) != _marker(root, key):
        raise ValueError("configured disk is absent")
    manifest = owned_manifest(source, key)
    if manifest is None or not owned_tree(source, {"MANIFEST.json", *manifest["files"]}):
        raise ValueError("source ownership changed")
    members = []
    for name in manifest["files"]:
        member_target(name)
        members.append((name, source / name))
    target = root / source.name
    if target.exists() and owned_manifest(target, key) != manifest:
        # Independent retention: the primary may reuse a same-second name the replica still has.
        stem = source.name[: len("tow-YYYYMMDD-HHMMSS")]
        target = root / f"{stem}-{manifest['signature'][:6]}"
    pending = remove_stale_partials(root, key)
    if target.exists():
        if owned_manifest(target, key) != manifest:
            raise ValueError("a different snapshot already uses that name")
        verify_snapshot(target, payloads=False)
    else:
        check_copy_space(root, members)
        partial = root / f".{target.name}.partial"
        partial.mkdir()
        write_snapshot_manifest(partial / PARTIAL_PROOF, partial_proof(partial.name, key))
        try:
            for name, path in members:
                if copy_snapshot_member(path, partial / name, name) != manifest["files"][name]:
                    raise ValueError("source changed during replication")
            write_snapshot_manifest(partial / "MANIFEST.json", manifest)
            verify_snapshot(partial, payloads=False)
            (partial / PARTIAL_PROOF).unlink()
            rename_with_retry(partial, target)
        except OSError, ValueError, SnapshotError:
            remove_stale_partials(root, key)
            raise
    _, cleanup, over_budget = prune_night_copies(root, target, key, retention_settings(cfg))
    return "cleanup" if pending or cleanup or over_budget else "verified"


def replicate_if_enabled(result: dict[str, Any]) -> None:
    raw = ""
    try:
        cfg = load_config()
        raw = str(cfg.get(REPLICA.key) or "").strip()
        if not raw:
            return
        # Serialize replication and pruning across TOW processes without locking live data.
        with (data_dir() / ".tow-replica.lock").open("a+b") as handle:
            init_lock_file(handle)
            locks.lock(handle)
            try:
                status = _replicate(Path(result["snapshot"]), raw, cfg)
            finally:
                locks.unlock(handle)
    except OSError, ValueError, RuntimeError:
        status = "unavailable"
    result["replica"] = status
    with contextlib.suppress(OSError, ValueError, RuntimeError), persistence_lock():
        record_status(replica_state=status, replica_location=raw)
