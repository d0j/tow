"""Making a night copy: its partial folder, the space it needs, the read-back, the pruning of
older copies, and removing a copy.
"""

from __future__ import annotations

import contextlib
import hashlib
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

from tow import __version__
from tow.backup_actions import copy_revision
from tow.backup_retention import MIB, copy_time, retained_copies, retention_settings
from tow.config import load_config
from tow.i18n import t
from tow.log import locked_log_path, log_event, owner_language
from tow.paths import in_root
from tow.snapshots.errors import SnapshotError, reason
from tow.snapshots.location import PREFIX, backup_root, snapshot_folders, snapshot_path
from tow.snapshots.manifest import (
    FORMAT,
    MAX_MANIFEST_BYTES,
    MAX_MEMBER_SIZE,
    MAX_RESTORE_JOURNAL_BYTES,
    manifest_bytes,
    owned_manifest,
    signature_matches,
    signature_of,
    signing_key,
)
from tow.snapshots.members import (
    POINT_MEMBER,
    VALIDATED_MEMBERS,
    copy_members_in,
    fixed_members,
    live_members,
    ordinary_directory,
    ordinary_file,
    owned_tree,
    read_metadata_bytes,
)
from tow.snapshots.record import cleanup_inventory, cleanup_status_in_lock, record_failure, record_status
from tow.snapshots.verify import READ_CHUNK_BYTES, check_member, verified_member, verify_snapshot
from tow.store import persistence_lock
from tow.yaml_guard import MAX_INPUT_BYTES, YamlLimitError

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
            fields: dict[str, Any] = {
                "location": str(location),
                "location_in_root": None,
                "cleanup_inventory": None,
                "cleanup_names": None,
            }
            # A diagnostic failure cannot invalidate a verified copy. A missing binding
            # remains unknown, rather than binding an earlier result to a later copy.
            with contextlib.suppress(OSError, ValueError, UnicodeError):
                fields["location"] = str(location.resolve())
                fields["location_in_root"] = in_root(location)  # survives a move of the install
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
            # The verified copy proves its folder's volume (recorded once; a missing note goes).
            with contextlib.suppress(OSError, ValueError, UnicodeError, RecursionError, RuntimeError):
                from tow.locations import NIGHT, remember_volume

                remember_volume(NIGHT, location)
    except SnapshotError as exc:
        record_failure(str(exc))
        raise
    except Exception as exc:  # settings, folder access...: a failed copy too, never a silent one
        failure = SnapshotError(t("backup.snapshot.cannot_write", owner_language(), reason=reason(exc)))
        record_failure(str(failure))
        raise failure from exc
    return result


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
        removed = remove_copy(path, path.parent, "MANIFEST.json", {"MANIFEST.json", *manifest["files"]})
        # Rebind a known observation; explicit deletion never clears an earlier warning.
        if not removed or type(before["pending"]) is bool:
            with contextlib.suppress(OSError, ValueError, UnicodeError):
                record_status(
                    last_cleanup_pending=not removed or before["pending"],
                    location=str(path.parent.resolve()),
                    location_in_root=in_root(path.parent),
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
    from tow.locations import NIGHT, LocationError, refuse_unseen_volume

    cfg = load_config()
    root = backup_root(cfg)
    try:
        refuse_unseen_volume(NIGHT, root)  # its disk unmounted: no copy onto the disk below
    except LocationError as exc:
        raise SnapshotError(str(exc)) from exc
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
        if not remove_copy(target, root, "MANIFEST.json", expected):
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
            if not remove_copy(folder, root, proof, {proof, "MANIFEST.json", *copy_members_in(folder)}):
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
            if remove_copy(folder, root, "MANIFEST.json", expected):
                removed.append(folder.name)
            else:
                pending = True
    except OSError, ValueError, TypeError, KeyError, OverflowError, SnapshotError:
        return removed, True, False  # the new verified copy is usable even when cleanup cannot be checked
    return removed, pending, over_budget


def remove_copy(folder: Path, parent: Path, marker: str, expected: set[str]) -> bool:
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
