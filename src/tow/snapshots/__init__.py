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

What is not in a module of its own yet is still here.
"""

from __future__ import annotations

import contextlib
import hashlib
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
from tow.backup_actions import KEPT_SETTINGS, copy_revision
from tow.backup_retention import MIB, copy_time, retained_copies, retention_settings
from tow.config import interval_sec_of, load_config
from tow.i18n import t
from tow.log import locked_log_path, log_event, owner_language
from tow.paths import config_path, data_dir
from tow.snapshots.errors import SnapshotError, reason
from tow.snapshots.location import PREFIX, backup_root, list_snapshots, snapshot_folders, snapshot_path
from tow.snapshots.manifest import (
    FORMAT,
    MAX_MANIFEST_BYTES,
    MAX_MEMBER_SIZE,
    MAX_RESTORE_JOURNAL_BYTES,
    UNSIGNED_FORMAT,
    manifest_bytes,
    owned_manifest,
    signature_matches,
    signature_of,
    signing_key,
)
from tow.snapshots.members import (
    NOT_RESTORED,
    POINT_MEMBER,
    VALIDATED_MEMBERS,
    copy_members_in,
    fixed_members,
    live_members,
    member_target,
    ordinary_directory,
    ordinary_file,
    owned_tree,
    parse_copy_config,
    points_dir_setting,
    read_metadata_bytes,
)
from tow.snapshots.record import (
    STATUS_NAME,
    cleanup_inventory,
    cleanup_status,
    cleanup_status_in_lock,
    record_failure,
    record_status,
    status,
    status_failed,
    status_path,
)
from tow.snapshots.verify import (
    READ_CHUNK_BYTES,
    check_member,
    check_snapshot,
    read_verified,
    verified_member,
    verify_snapshot,
)
from tow.store import (
    SecretStoreError,
    atomic_write_bytes,
    decode_json_bytes,
    decrypt_secrets_bytes,
    encrypt_secrets_bytes,
    encrypted_secrets_path,
    load_secrets,
    persistence_lock,
)
from tow.yaml_guard import MAX_INPUT_BYTES, YamlLimitError
from tow.yaml_guard import dump as dump_yaml
from tow.yaml_guard import load as load_yaml
from tow.yaml_guard import read_text as read_yaml_text

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


# Exists only while a settings change can still be undone: its absence is normal.
_OPTIONAL_MEMBERS = frozenset({"secrets-undo.enc"})
# A copy being written: ``.tow-<stamp>.partial`` with a signed proof that this install made it.
_PARTIAL_NAME = re.compile(r"\.tow-[0-9]{8}-[0-9]{6}(?:-[0-9a-f]{6})?\.partial")
_PARTIAL_PROOF = "PARTIAL.json"
_PARTIAL_FORMAT = "tow-snapshot-partial-v1"


def _previous_members(root: Path, key: bytes) -> set[str]:
    """Stores a signed earlier copy proves existed; their later disappearance is not normal."""
    present: set[str] = set()
    for folder in snapshot_folders(root):
        if manifest := owned_manifest(folder, key):
            present.update(
                name for name in manifest["files"] if name in {"state.json", "download_history.json", "secrets.enc"}
            )
    return present


def create_snapshot(*, how: str = "auto") -> dict[str, Any]:
    try:
        proven = _proven_sources()  # parsed before the data lock: a check or an edit need not wait
        with persistence_lock():  # creation, read-back and pruning cannot race another copy
            result = _create_snapshot(how=how, proven=proven)
            location = Path(result["snapshot"]).parent
            fields: dict[str, Any] = {"location": str(location), "cleanup_inventory": None, "cleanup_names": None}
            # A diagnostic failure cannot invalidate a verified copy. A missing binding
            # remains unknown, rather than binding an earlier result to a later copy.
            with contextlib.suppress(OSError, ValueError, UnicodeError):
                fields["location"] = str(location.resolve())
                fields.update(cleanup_inventory(location))
            record_status(
                last_ok_at=round(time.time(), 3),
                last_snapshot=Path(result["snapshot"]).name,
                last_bytes=result["bytes"],
                last_missing=result["missing"],
                last_cleanup_pending=bool(result.get("cleanup_warning")),
                last_error="",
                **fields,
            )
    except SnapshotError as exc:
        record_failure(str(exc))
        raise
    except Exception as exc:  # settings, folder access...: a failed copy too, never a silent one
        failure = SnapshotError(t("backup.snapshot.cannot_write", owner_language(), reason=reason(exc)))
        record_failure(str(failure))
        raise failure from exc
    return result


def snapshot_delete_view(name: str) -> dict[str, Any]:
    with persistence_lock():
        try:
            path = snapshot_path(name)
            manifest = owned_manifest(path, signing_key())
            fixed = {member for member, _target in fixed_members()}
            if manifest is None or any(
                member not in fixed and POINT_MEMBER.fullmatch(member) is None for member in manifest["files"]
            ):
                raise ValueError("copy ownership could not be verified")
            if not owned_tree(path, {"MANIFEST.json", *manifest["files"]}):
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
        manifest = owned_manifest(path, signing_key())
        if manifest is None:
            raise SnapshotError(t("web.backup.delete_failed", owner_language()))
        before = cleanup_status_in_lock(cfg=None)
        removed = _remove_copy(path, path.parent, "MANIFEST.json", {"MANIFEST.json", *manifest["files"]})
        # Rebind a known observation; explicit deletion never clears an earlier warning.
        if not removed or type(before["pending"]) is bool:
            with contextlib.suppress(OSError, ValueError, UnicodeError):
                record_status(
                    last_cleanup_pending=not removed or before["pending"],
                    location=str(path.parent.resolve()),
                    **cleanup_inventory(path.parent),
                )
        if not removed:
            raise SnapshotError(t("web.backup.delete_failed", owner_language()))
        return {"ok": True, "deleted": name}


def _copy_snapshot_member(source: Path, destination: Path, name: str) -> dict[str, Any] | None:
    """Copy/hash the same bounded blocks; a missing source is distinct from an unsafe one."""
    lang = owner_language()
    try:
        if not ordinary_file(source, missing=True):
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
                while block := reader.read(min(READ_CHUNK_BYTES, size - seen + 1)):
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
    content = manifest_bytes(manifest)
    with path.open("xb") as writer:
        if writer.write(content) != len(content):
            raise SnapshotError(t("backup.snapshot.write_incomplete", owner_language(), name="MANIFEST.json"))
        writer.flush()
        os.fsync(writer.fileno())


def _create_snapshot(*, how: str, proven: dict[str, str] | None = None) -> dict[str, Any]:
    """Called with the data lock held: the copy is a consistent cut, no check or edit writes meanwhile.

    ``proven``: the sha256 of live stores whose contents already passed the payload checks
    (``_proven_sources``); a copied store with another hash is checked here, from the copy.
    """
    cfg = load_config()
    root = backup_root(cfg)
    policy = retention_settings(cfg)
    stamp = datetime.now(UTC).astimezone().strftime("%Y%m%d-%H%M%S")
    target = root / f"{PREFIX}{stamp}"
    partial = root / f".{PREFIX}{stamp}.partial"
    while target.exists() or partial.exists():
        suffix = uuid.uuid4().hex[:6]
        target = root / f"{PREFIX}{stamp}-{suffix}"
        partial = root / f".{PREFIX}{stamp}-{suffix}.partial"
    files: dict[str, dict[str, Any]] = {}
    missing: list[str] = []
    key = signing_key()  # no key, no copy: an unsigned copy could never be restored
    stale_pending = _remove_stale_partials(root, key)
    previous_members = _previous_members(root, key)
    partial_created = False
    try:
        members = live_members()
        _check_copy_space(root, members)
        partial.mkdir(parents=True, exist_ok=False)
        partial_created = True
        _write_snapshot_manifest(partial / _PARTIAL_PROOF, _partial_proof(partial.name, key))
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
        for name in VALIDATED_MEMBERS:
            if name in files and files[name]["sha256"] != (proven or {}).get(name):
                content = verified_member(partial / name, name, files[name], collect=True)
                assert content is not None
                check_member(name, content)
                del content  # one store in memory at a time
        manifest = {
            "format": FORMAT,
            "created_at": datetime.now(UTC).isoformat(),
            "version": __version__,
            "files": files,
            "missing": missing,
        }
        manifest["signature"] = signature_of(manifest, key)
        _write_snapshot_manifest(partial / "MANIFEST.json", manifest)
        (partial / _PARTIAL_PROOF).unlink()  # from here the signed MANIFEST proves the folder is ours
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
        # Read every file back from the copies folder before deleting any older copy. Its hashes
        # are the ones of bytes whose contents passed the checks above: no store is parsed again.
        verify_snapshot(target, payloads=False)
    except SnapshotError as exc:
        # Never listed, restored or counted for retention: back to a partial folder, removed
        # now (or by the next copy when the folder cannot be read right now).
        _withdraw_unverified(target, partial, root, key, {"MANIFEST.json", *files})
        raise SnapshotError(t("backup.snapshot.unverified", owner_language(), name=target.name, reason=exc)) from exc
    # Never the copy just verified, even when the clock went back and it does not sort last.
    pruned, cleanup_pending, over_budget = _prune_night_copies(root, target, key, policy)
    cleanup_pending = cleanup_pending or stale_pending
    size = sum(item["size"] for item in files.values())
    log_event(
        "backup_created",
        snapshot=target.name,
        files=len(files),
        bytes=size,
        pruned=len(pruned),
        cleanup_pending=cleanup_pending,
        over_budget=over_budget,
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
    if over_budget:
        result["over_budget_mib"] = policy["max_mib"]
        result["budget_warning"] = t("backup.snapshot.over_budget", owner_language(), limit=policy["max_mib"])
    return result


def _withdraw_unverified(target: Path, partial: Path, root: Path, key: bytes, expected: set[str]) -> None:
    """A copy that failed its read-back stops being a copy. Renamed back to its partial name
    (the next copy removes a partial folder of ours), else removed, else at least without its
    signed MANIFEST: still named as a copy, it would be listed, offered for restore and counted
    as one of the copies kept, so an older good one could be pruned in its place."""
    try:
        _rename_with_retry(target, partial)
    except OSError:
        if not _remove_copy(target, root, "MANIFEST.json", expected):
            with contextlib.suppress(OSError):
                (target / "MANIFEST.json").unlink()
        return
    with contextlib.suppress(OSError):
        _remove_stale_partials(root, key)


def _partial_proof(folder: str, key: bytes) -> dict[str, Any]:
    proof: dict[str, Any] = {"format": _PARTIAL_FORMAT, "folder": folder}
    proof["signature"] = signature_of(proof, key)
    return proof


def _partial_owner_proof(folder: Path, key: bytes) -> str | None:
    """The member proving a partial folder is this install's: its signed proof, else a signed MANIFEST."""
    if ordinary_file(folder / _PARTIAL_PROOF, missing=True):
        try:
            proof = json.loads(read_metadata_bytes(folder / _PARTIAL_PROOF, limit=MAX_MANIFEST_BYTES))
        except UnicodeError, ValueError, RecursionError:
            return None
        if (
            isinstance(proof, dict)
            and proof.get("format") == _PARTIAL_FORMAT
            and proof.get("folder") == folder.name
            and signature_matches(proof, key)
        ):
            return _PARTIAL_PROOF
        return None
    return "MANIFEST.json" if owned_manifest(folder, key) is not None else None


def _remove_stale_partials(root: Path, key: bytes) -> bool:
    """Remove the partial folders a crash, a power loss or a killed copy left (data lock held).

    Each is as large as a whole copy. Only one this install signed is removed, never a
    foreign one or one of a version without the proof. True when one of ours stays.
    """
    pending = False
    try:
        candidates = [path for path in root.iterdir() if _PARTIAL_NAME.fullmatch(path.name)] if root.is_dir() else []
    except OSError:
        return True
    for folder in candidates:
        try:
            if not ordinary_directory(folder, missing=True) or (proof := _partial_owner_proof(folder, key)) is None:
                continue
            if not _remove_copy(folder, root, proof, {proof, "MANIFEST.json", *copy_members_in(folder)}):
                pending = True
        except OSError, ValueError:
            pending = True
    return pending


def _check_copy_space(root: Path, members: list[tuple[str, Path]]) -> None:
    """Keep room for a complete new copy; never erase the old one to make space."""
    size = 0
    for name, source in members:
        try:
            if ordinary_file(source, missing=True):
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


def _prune_night_copies(root: Path, target: Path, key: bytes, policy: dict[str, Any]) -> tuple[list[str], bool, bool]:
    """(removed, cleanup pending, over budget): a copy that cannot be read is left alone and
    makes cleanup pending, but the others are still pruned; the kept copies larger than the
    size budget (only the new copy and future-dated ones are never removed) is said apart,
    as no retry of the cleanup changes it."""
    removed: list[str] = []
    pending = False
    try:
        others = []
        entries = []
        fixed = {name for name, _path in fixed_members()}
        for folder in snapshot_folders(root):
            try:
                manifest = owned_manifest(folder, key)
                if manifest is None or any(
                    name not in fixed and POINT_MEMBER.fullmatch(name) is None for name in manifest["files"]
                ):
                    continue
                if owned_tree(folder, {"MANIFEST.json", *manifest["files"]}):
                    created = copy_time(str(manifest["created_at"]))
                    entries.append((folder.name, created, _copy_disk_bytes(folder)))
                    if folder != target:
                        others.append((folder, {"MANIFEST.json", *manifest["files"]}))
            except OSError, ValueError, TypeError, KeyError, OverflowError, SnapshotError:
                if folder == target:
                    raise
                pending = True  # one damaged copy: kept, and the others are still pruned
        anchor = next((when for name, when, _size in entries if name == target.name), None)
        if anchor is None:
            return [], True, False  # the verified new copy no longer proves ownership; retain older copies
        retained, over_budget = retained_copies(entries, newest=target.name, now=anchor, policy=policy)
        for folder, expected in others:
            if folder.name in retained:
                continue
            if _remove_copy(folder, root, "MANIFEST.json", expected):
                removed.append(folder.name)
            else:
                pending = True
    except OSError, ValueError, TypeError, KeyError, OverflowError, SnapshotError:
        return removed, True, False  # the new verified copy is usable even when cleanup cannot be checked
    return removed, pending, over_budget


def _remove_copy(folder: Path, parent: Path, marker: str, expected: set[str]) -> bool:
    """Remove an already-owned copy, retaining its proof until the final directory step."""
    try:
        if Path(marker).name != marker or marker not in expected:
            return False
        ordinary_directory(folder)
        if folder.resolve().parent != parent.resolve():
            return False
        proof = folder / marker
        ordinary_file(proof)
        limit = MAX_MANIFEST_BYTES if marker == "MANIFEST.json" else MAX_RESTORE_JOURNAL_BYTES
        content = read_metadata_bytes(proof, limit=limit)
        if not owned_tree(folder, expected):
            return False
        for child in sorted(folder.iterdir()):
            if child == proof:
                continue
            if child.name == "restore-points":
                ordinary_directory(child)
                shutil.rmtree(child)
            else:
                if child.name not in expected:
                    return False
                ordinary_file(child)
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
        ordinary_directory(folder)
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


def _proven_sources() -> dict[str, str]:
    """The sha256 of each live store whose bytes pass the payload checks, read without the data
    lock: parsing a large download history held every save up for most of a night copy. The
    copy made under the lock uses a proof only for bytes with the same hash; a store that
    changed in between, or failed here, is checked again from the copy."""
    proven: dict[str, str] = {}
    for name, source in fixed_members():
        if name not in VALIDATED_MEMBERS:
            continue
        try:
            limit = MAX_INPUT_BYTES if name == "config.yaml" else MAX_MEMBER_SIZE
            if not ordinary_file(source, missing=True) or source.stat().st_size > limit:
                continue
            content = source.read_bytes()
            check_member(name, content)
        except OSError, ValueError, SnapshotError:
            continue  # said by the check under the lock, with the copy's own error
        proven[name] = hashlib.sha256(content).hexdigest()
        del content
    return proven


def restore_snapshot(path: Path, *, apply: bool = False) -> dict[str, Any]:
    path = Path(path)
    manifest, contents, signed = read_verified(path, retain=apply, require_signed=apply)
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
        points_dir = points_dir_setting(contents.get("config.yaml"))
        plan = _restore_plan(contents, points_dir)  # everything merged and checked before the first write
        safety, cleanup_pending = _apply_restore(plan, snapshot=path.name, points_dir=points_dir)
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
_SAFETY_NAME = re.compile(r"before-restore-[0-9]{8}-[0-9]{6}(?:-[0-9a-f]{6})?")
SAFETY_KEEP = 3
_RECOVERY_ATTEMPTS = 5
# Secrets a finished before-restore copy must not keep: the settings undo of that moment.
_UNDO_IN_SAFETY = "secrets-undo.enc"
# A copy made when one of these stores did not exist yet: the restore removes the live one.
_REMOVED_WHEN_ABSENT = ("state.json", "download_history.json", "secrets-undo.enc")


def _restore_plan(contents: dict[str, bytes], points_dir: str) -> list[tuple[str, Path, bytes | None]]:
    """(member, live target, new bytes or None to remove) - config.yaml last, so that the
    targets of restore points are those of the config in force while the files are written."""
    plan: list[tuple[str, Path, bytes | None]] = []
    for name, content in contents.items():
        if name == NOT_RESTORED:
            continue
        if name == "config.yaml":
            content = _keep_local_access(content)
        elif name == "secrets.enc":
            content = _keep_local_password(content)
        plan.append((name, member_target(name, points_dir=points_dir), content))
    if "secrets.enc" not in contents and encrypted_secrets_path().is_file():
        plan.append(("secrets.enc", encrypted_secrets_path(), _keep_local_password(None)))
    plan.extend(
        (name, member_target(name), None)
        for name in _REMOVED_WHEN_ABSENT
        if name not in contents and member_target(name).is_file()
    )
    return sorted(plan, key=lambda step: step[0] == "config.yaml")


def _begin_restore(plan: list[tuple[str, Path, bytes | None]], *, snapshot: str, points_dir: str) -> Path:
    """The before-restore copy and its journal, then the marker; nothing live is touched yet."""
    stamp = datetime.now(UTC).astimezone().strftime("%Y%m%d-%H%M%S")
    safety = data_dir() / f"before-restore-{stamp}"
    if safety.exists():
        safety = data_dir() / f"before-restore-{stamp}-{uuid.uuid4().hex[:6]}"
    entries = []
    created = False
    try:
        safety.mkdir(parents=True, exist_ok=False)
        created = True
        for name, target, _content in plan:
            if ordinary_file(target, missing=True):
                saved = target.read_bytes()
                atomic_write_bytes(safety / name, saved)
                entries.append({"name": name, "existed": True, "sha256": hashlib.sha256(saved).hexdigest()})
            else:
                entries.append({"name": name, "existed": False})
        # The restore points folder as set (not resolved): rollback puts points back exactly
        # where they were written, even when the saved config.yaml cannot be read.
        journal = {"format": _JOURNAL_FORMAT, "snapshot": snapshot, "entries": entries, "points_dir": points_dir}
        # Every final status must remain writable before publishing a recovery marker.
        for status in ("prepared", "committed", "rolled_back"):
            _journal_bytes(journal, status)
        _write_journal(safety, journal, "prepared")
        _rollback_plan(safety)  # verify every just-written copy before publishing the marker
        marker = _restore_record_bytes({"safety": safety.name}, limit=MAX_RESTORE_MARKER_BYTES)
        atomic_write_bytes(data_dir() / _MARKER, marker)
    except (OSError, ValueError, TypeError, RecursionError, yaml.YAMLError, SnapshotError) as exc:
        # A failed exclusive mkdir may belong to another writer, and a marker published just
        # before an I/O error still needs the copies. Otherwise nothing refers to the folder.
        if created and _marked_safety() is None:
            shutil.rmtree(safety, ignore_errors=True)
        raise SnapshotError(t("backup.snapshot.restore_failed", owner_language(), reason=reason(exc))) from exc
    return safety


def _read_restore_record(path: Path, *, limit: int) -> dict[str, Any]:
    raw = read_metadata_bytes(path, limit=limit)
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
    ordinary_directory(safety)
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
        or not isinstance(journal.get("points_dir", ""), str)
    ):
        raise ValueError("not a night-copy restore journal")
    names: set[str] = set()
    fixed = dict(fixed_members())
    for entry in journal["entries"]:
        if not isinstance(entry, dict):
            raise TypeError("malformed night-copy restore entry")
        name = entry.get("name")
        if not isinstance(name, str) or (name not in fixed and POINT_MEMBER.fullmatch(name) is None):
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


def _rollback_target(name: str, config_bytes: bytes | None, points_dir: str | None) -> Path:
    fixed = dict(fixed_members())
    if name in fixed:
        return fixed[name]
    # Point targets are where the restore wrote them, not those of a partially restored config.
    from tow.locations import MANUAL, resolve_checked

    if points_dir is None:  # a journal of 1.23.11 or earlier: the saved config says where
        if config_bytes is None:
            return member_target(name)
        config = load_yaml(config_bytes)
        if not isinstance(config, dict):
            raise TypeError("night-copy restore config is malformed")
        points_dir = str(config.get("restore_points_dir") or "")
    match = POINT_MEMBER.fullmatch(name)
    if match is None or re.fullmatch(r"[0-9]{8}T[0-9]{6}Z-[0-9a-f]{8}", match["id"]) is None:
        raise ValueError("night-copy restore point is malformed")
    root = resolve_checked(points_dir, MANUAL).resolve()
    return root / f"{match['id']}.towx"


def _rollback_plan(safety: Path) -> list[tuple[str, Path, bytes | None]]:
    journal = _read_journal(safety)
    saved: dict[str, bytes | None] = {}
    for entry in journal["entries"]:
        name = entry["name"]
        backup = safety / name
        if backup.parent != safety:
            ordinary_directory(backup.parent, missing=not entry["existed"])
        if entry["existed"]:
            ordinary_file(backup)
            content = backup.read_bytes()
            if hashlib.sha256(content).hexdigest() != entry["sha256"]:
                raise SnapshotError(t("backup.snapshot.file_damaged", owner_language(), name=name))
            saved[name] = content
        else:
            if ordinary_file(backup, missing=True):
                raise ValueError("unexpected night-copy restore backup")
            saved[name] = None
    plan = []
    for name, content in saved.items():
        target = _rollback_target(name, saved.get("config.yaml"), journal.get("points_dir"))
        ordinary_file(target, missing=True)
        if target.parent.exists():
            ordinary_directory(target.parent)
        plan.append((name, target, content))
    return sorted(plan, key=lambda step: step[0] != "config.yaml")


def _owned_safety(safety: Path, journal: dict[str, Any]) -> bool:
    """Never prune an unrecognized file or descend into a link/junction."""
    expected = {_JOURNAL, *(entry["name"] for entry in journal["entries"] if entry["existed"])}
    return owned_tree(safety, expected)


# A file atomic_write_bytes was writing when the process died: ``.<name>.<random>.tmp``.
_WRITE_LEFTOVER = re.compile(r"\.(?P<name>.+)\.[A-Za-z0-9_]+\.tmp")


def _remove_write_leftovers(folder: Path) -> None:
    """The half-written files a crash left in a before-restore folder (of its stores, its
    journal or its restore points): with them it was never removed, as a folder with files of
    unknown names is never swept up."""
    known = {name for name, _path in fixed_members()} | {_JOURNAL}
    for parent, prefix in ((folder, ""), (folder / "restore-points", "restore-points/")):
        if not ordinary_directory(parent, missing=True):
            continue
        for path in parent.iterdir():
            match = _WRITE_LEFTOVER.fullmatch(path.name)
            if match is None:
                continue
            name = prefix + match["name"]
            if (name in known or POINT_MEMBER.fullmatch(name)) and ordinary_file(path):
                path.unlink()


def _remove_unjournaled(folder: Path) -> bool:
    """A before-restore folder without a journal holds only copies of stores; nothing refers to it."""
    try:
        if not ordinary_directory(folder, missing=True):
            return True
        if not owned_tree(folder, copy_members_in(folder)):
            return False
        shutil.rmtree(folder)
    except OSError, ValueError:
        return False
    return not folder.exists()


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


def _cleanup_safety_copies(keep: int = SAFETY_KEEP) -> tuple[list[str], bool]:
    """Finished before-restore copies: without the settings undo, the newest ``keep`` only.

    Never the one a marker names, never one whose journal still says ``prepared`` (a restore
    that is not finished needs every file). Best effort: a file held right now goes next time.
    Returns the names of the folders removed and whether cleanup is still pending.
    """
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
    removed = []
    pending = False
    for folder in folders:
        if folder.name == marked:
            continue
        try:
            _remove_write_leftovers(folder)
            journal = _read_journal(folder)
            if journal["status"] == "prepared":
                # Named by no marker, under the data lock: its restore stopped before the marker
                # was written, so before any live write, and nothing refers to it.
                expected = {_JOURNAL, *(entry["name"] for entry in journal["entries"] if entry["existed"])}
                if _owned_safety(folder, journal) and _remove_copy(folder, data_dir(), _JOURNAL, expected):
                    removed.append(folder.name)
                else:
                    pending = True
                continue
            if not _owned_safety(folder, journal):
                continue
        except FileNotFoundError:
            # No journal: its restore stopped before the marker (so before any live write).
            if not _remove_unjournaled(folder):
                pending = True
            continue
        except OSError, UnicodeError, ValueError, KeyError, TypeError, RecursionError:
            pending = True
            continue  # unreadable is not evidence that a restore has finished
        finished.append((folder, {_JOURNAL, *(entry["name"] for entry in journal["entries"] if entry["existed"])}))
        try:
            (folder / _UNDO_IN_SAFETY).unlink(missing_ok=True)
            if ordinary_file(folder / _UNDO_IN_SAFETY, missing=True):
                pending = True
        except OSError, ValueError:
            pending = True
    for folder, expected in finished[: max(0, len(finished) - keep)]:
        if _remove_copy(folder, data_dir(), _JOURNAL, expected):
            removed.append(folder.name)
        else:
            pending = True
    return removed, pending


def _apply_restore(plan: list[tuple[str, Path, bytes | None]], *, snapshot: str, points_dir: str) -> tuple[Path, bool]:
    """Write the plan under the data lock; any failure puts every saved file back."""
    safety = _begin_restore(plan, snapshot=snapshot, points_dir=points_dir)
    try:
        for name, target, content in plan:
            if content is None:
                target.unlink(missing_ok=True)
                continue
            atomic_write_bytes(target, content)
            if target.read_bytes() != content:
                raise SnapshotError(t("backup.snapshot.read_back_failed", owner_language(), name=name))
        _finish(safety, "committed")
    except Exception as failure:  # every transaction failure is rolled back before the lock is released
        try:
            _roll_back_files(safety)
        except Exception as rollback_failure:  # the marker stays: the next lock holder tries again
            log_event("backup_restore_failed", error=reason(failure), rollback="failed", how="manual")
            raise SnapshotError(
                t("backup.snapshot.rollback_failed", owner_language(), path=str(safety))
            ) from rollback_failure
        log_event("backup_restore_failed", error=reason(failure), rollback="done", how="manual")
        _cleanup_safety_copies()
        raise SnapshotError(t("backup.snapshot.restore_failed", owner_language(), reason=reason(failure))) from failure
    # Cleanup is outside the transaction: its failure must not undo a committed restore.
    return safety, _cleanup_safety_copies()[1]


def _roll_back(safety: Path) -> None:
    _roll_back_files(safety)
    _cleanup_safety_copies()


def _roll_back_files(safety: Path) -> None:
    """Put back every file the journal saved (config.yaml first: it says where restore points live)."""
    try:
        _journal_bytes(_read_journal(safety), "rolled_back")  # legacy final metadata fits before any live write
        plan = _rollback_plan(safety)
    except (OSError, ValueError, TypeError, RecursionError, yaml.YAMLError) as exc:
        raise SnapshotError(t("backup.snapshot.restore_failed", owner_language(), reason=reason(exc))) from exc
    # A restore of 1.23.11 or earlier also replaced the event log: no append while it goes back.
    legacy_log = any(name == "tow.jsonl" for name, _target, _content in plan)
    with locked_log_path() if legacy_log else contextlib.nullcontext():
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
        t("backup.snapshot.recovery_blocked", owner_language(), reason=reason(exc), marker=str(marker))
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


def _keep_local_access(snapshot_config: bytes) -> bytes:
    """The copy's config with this machine's access settings and backup folders (the live ones).

    A live config.yaml that cannot be read does not stop the restore - replacing it is often
    the point - and then the restored config is local-only: network access is turned back on
    from this PC, as always; its backup folders are then the copy's (where its restore points
    were just written).
    """
    restored = parse_copy_config(snapshot_config)
    try:
        current = load_yaml(read_yaml_text(config_path()))
    except OSError, UnicodeError, yaml.YAMLError:
        current = None
    if isinstance(current, dict):
        for key in KEPT_SETTINGS:
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
