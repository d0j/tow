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

The package re-exports what the rest of TOW uses; inside it:

- ``errors``: ``SnapshotError`` and the reason of a failure;
- ``members``: what a copy holds, where each member goes, plain files and folders;
- ``manifest``: the MANIFEST, its limits and its signature;
- ``location``: the copies folder, its copies and one copy by name;
- ``record``: the record of the last copy and the cleanup it observed;
- ``verify``: reading a copy back, hashes first, then its stores;
- ``create``: making a copy, its partial folder, space, pruning and removal;
- ``restore``: the all-or-nothing restore, its safety copies and rollback,
  ``recover_interrupted_restore``, and the local password and access settings it keeps.
"""

from __future__ import annotations

from tow.snapshots.create import create_snapshot, delete_snapshot, snapshot_delete_view
from tow.snapshots.errors import SnapshotError
from tow.snapshots.location import backup_root, list_snapshots, snapshot_path
from tow.snapshots.manifest import (
    FORMAT,
    MAX_MANIFEST_BYTES,
    MAX_MEMBER_SIZE,
    MAX_RESTORE_JOURNAL_BYTES,
    UNSIGNED_FORMAT,
)
from tow.snapshots.record import STATUS_NAME, cleanup_status, record_failure, status, status_failed, status_path
from tow.snapshots.restore import MAX_RESTORE_MARKER_BYTES, SAFETY_KEEP, recover_interrupted_restore, restore_snapshot
from tow.snapshots.verify import check_snapshot, verify_snapshot

__all__ = [
    "FORMAT",
    "MAX_MANIFEST_BYTES",
    "MAX_MEMBER_SIZE",
    "MAX_RESTORE_JOURNAL_BYTES",
    "MAX_RESTORE_MARKER_BYTES",
    "SAFETY_KEEP",
    "STATUS_NAME",
    "UNSIGNED_FORMAT",
    "SnapshotError",
    "backup_root",
    "check_snapshot",
    "cleanup_status",
    "create_snapshot",
    "delete_snapshot",
    "list_snapshots",
    "record_failure",
    "recover_interrupted_restore",
    "restore_snapshot",
    "snapshot_delete_view",
    "snapshot_path",
    "status",
    "status_failed",
    "status_path",
    "verify_snapshot",
]
