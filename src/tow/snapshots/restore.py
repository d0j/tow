"""Restoring a night copy, all or nothing, and recovering a restore a crash interrupted.

A restored config keeps this machine's network access and backup folders, and restored
secrets keep this install's current password.
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import re
import shutil
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from tow.backup_actions import KEPT_SETTINGS
from tow.config import interval_sec_of, load_config
from tow.i18n import t
from tow.log import locked_log_path, log_event, owner_language
from tow.paths import config_path, data_dir
from tow.snapshots.create import remove_copy
from tow.snapshots.errors import SnapshotError, reason
from tow.snapshots.manifest import MAX_RESTORE_JOURNAL_BYTES
from tow.snapshots.members import (
    NOT_RESTORED,
    POINT_MEMBER,
    copy_members_in,
    fixed_members,
    member_target,
    ordinary_directory,
    ordinary_file,
    owned_tree,
    parse_copy_config,
    points_dir_setting,
    read_metadata_bytes,
)
from tow.snapshots.verify import read_verified
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
from tow.yaml_guard import YamlLimitError
from tow.yaml_guard import dump as dump_yaml
from tow.yaml_guard import load as load_yaml
from tow.yaml_guard import read_text as read_yaml_text

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
                if _owned_safety(folder, journal) and remove_copy(folder, data_dir(), _JOURNAL, expected):
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
        if remove_copy(folder, data_dir(), _JOURNAL, expected):
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
