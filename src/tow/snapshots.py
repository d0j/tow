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
import json
import re
import shutil
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from tow import __version__
from tow.config import interval_sec_of, load_config
from tow.i18n import t
from tow.log import log_event, log_path, owner_language
from tow.paths import config_path, data_dir, download_history_path, state_path
from tow.store import (
    SecretStoreError,
    atomic_write_bytes,
    decrypt_secrets_bytes,
    derive_local_secret,
    encrypt_secrets_bytes,
    encrypted_secrets_path,
    load_secrets,
    master_fernet,
    persistence_lock,
    secret_undo_path,
)

FORMAT = "tow-snapshot-v2"  # a signed MANIFEST
UNSIGNED_FORMAT = "tow-snapshot-v1"  # copies made before 1.17: checked, never restored automatically
_SIGNATURE_PURPOSE = "night-copies"
_POINT_MEMBER = re.compile(r"restore-points/(?P<id>[A-Za-z0-9-]{1,64})\.towx")
KEEP_DEFAULT = 14
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
        value = json.loads(status_path().read_text(encoding="utf-8"))
    except OSError, UnicodeError, ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def _record(**fields: Any) -> None:
    from tow.store import atomic_write_text

    current = status()
    current.update(fields)
    # The status is informative; the copy itself already succeeded or failed.
    with contextlib.suppress(OSError):
        atomic_write_text(status_path(), json.dumps(current, ensure_ascii=False, indent=2) + "\n")


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
        manifest = json.loads((path / "MANIFEST.json").read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError, RecursionError) as exc:
        raise SnapshotError(t("backup.snapshot.manifest_unreadable", lang)) from exc
    if (
        not isinstance(manifest, dict)
        or manifest.get("format") not in (FORMAT, UNSIGNED_FORMAT)
        or not isinstance(manifest.get("files"), dict)
    ):
        raise SnapshotError(t("backup.snapshot.not_tow", lang))
    return manifest


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
    except SnapshotError as exc:
        record_failure(str(exc))
        raise
    _record(
        last_ok_at=round(time.time(), 3),
        last_snapshot=Path(result["snapshot"]).name,
        last_bytes=result["bytes"],
        last_missing=result["missing"],
        location=str(Path(result["snapshot"]).parent),
        last_error="",
    )
    return result


def list_snapshots(limit: int = 10) -> list[dict[str, Any]]:
    """Newest night copies first: name, time, size (no verification)."""
    try:
        root = backup_root()
        folders = _snapshots(root)
    except SnapshotError, OSError:
        return []
    rows = []
    for folder in reversed(folders[-limit:]):
        try:
            manifest = json.loads((folder / "MANIFEST.json").read_text(encoding="utf-8"))
            created = datetime.fromisoformat(str(manifest.get("created_at")))
            size = sum(int(item.get("size") or 0) for item in (manifest.get("files") or {}).values())
        except OSError, UnicodeError, ValueError, TypeError, AttributeError:
            continue
        rows.append(
            {"name": folder.name, "created_ts": created.timestamp(), "bytes": size, "version": manifest.get("version")}
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


def _create_snapshot(*, keep: int | None, how: str) -> dict[str, Any]:
    cfg = load_config()
    root = backup_root(cfg)
    keep = int(keep or cfg.get("backup_keep") or KEEP_DEFAULT)
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
    try:
        partial.mkdir(parents=True, exist_ok=False)
        with persistence_lock():  # a consistent cut: no check or edit writes meanwhile
            for name, source in _members():
                try:
                    content = source.read_bytes() if source.is_file() else None
                except FileNotFoundError:
                    content = None
                if content is None:
                    if name == "config.yaml" or name in previous_members:
                        raise SnapshotError(t("backup.snapshot.source_missing", owner_language(), name=name))
                    if name not in _OPTIONAL_MEMBERS:
                        missing.append(name)  # said in the result, the log and the MANIFEST
                    continue
                destination = partial / name
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(content)
                files[name] = {"sha256": hashlib.sha256(content).hexdigest(), "size": len(content)}
        manifest = {
            "format": FORMAT,
            "created_at": datetime.now(UTC).isoformat(),
            "version": __version__,
            "files": files,
            "missing": missing,
        }
        manifest["signature"] = _signature(manifest, key)
        (partial / "MANIFEST.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        _rename_with_retry(partial, target)
    except SnapshotError:
        shutil.rmtree(partial, ignore_errors=True)
        raise
    except OSError as exc:
        shutil.rmtree(partial, ignore_errors=True)
        raise SnapshotError(
            t("backup.snapshot.cannot_write", owner_language(), reason=exc.strerror or type(exc).__name__)
        ) from exc
    try:
        verify_snapshot(target)  # read every file back from the copies folder before deleting any older copy
    except SnapshotError as exc:
        raise SnapshotError(t("backup.snapshot.unverified", owner_language(), name=target.name, reason=exc)) from exc
    # Never the copy just verified, even when the clock went back and it does not sort last.
    others = [p.name for p in _snapshots(root) if p.name != target.name and _owned_manifest(p, key) is not None]
    pruned = others[: max(0, len(others) - (keep - 1))] if keep > 0 else []
    for name in pruned:
        shutil.rmtree(root / name, ignore_errors=True)
    size = sum(item["size"] for item in files.values())
    log_event(
        "backup_created",
        snapshot=target.name,
        files=len(files),
        bytes=size,
        pruned=len(pruned),
        missing=missing,
        how=how,
    )
    return {
        "ok": True,
        "snapshot": str(target),
        "files": len(files),
        "bytes": size,
        "pruned": pruned,
        "missing": missing,
    }


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


def _read_verified(path: Path) -> tuple[dict[str, Any], dict[str, bytes], bool]:
    """(manifest, contents by member name, signed): every file read once and checked.

    The bytes returned are the ones whose hash was checked, so a restore writes exactly
    what was verified (no second read the copies folder could change in between).
    A signed MANIFEST must carry this install's signature; an unsigned one (made before
    1.17) is still checked file by file, and the caller decides what it may be used for.
    """
    path = Path(path)
    lang = owner_language()
    manifest = _read_manifest(path)
    signed = manifest["format"] == FORMAT
    if signed and not _signature_matches(manifest, _signing_key()):
        raise SnapshotError(t("backup.snapshot.bad_signature", lang))
    contents: dict[str, bytes] = {}
    for name, meta in manifest["files"].items():
        _member_target(name)  # a strict name, and a target that stays where it belongs
        size = meta.get("size") if isinstance(meta, dict) else None
        if type(size) is not int or size < 0:
            raise SnapshotError(t("backup.snapshot.file_damaged", lang, name=name))
        try:
            source = path / name
            if source.stat().st_size != size:
                raise SnapshotError(t("backup.snapshot.file_damaged", lang, name=name))
            with source.open("rb") as handle:
                content = handle.read(size + 1)
        except OSError as exc:
            raise SnapshotError(t("backup.snapshot.file_missing", lang, name=name)) from exc
        if len(content) != size or hashlib.sha256(content).hexdigest() != meta.get("sha256"):
            raise SnapshotError(t("backup.snapshot.file_damaged", lang, name=name))
        contents[name] = content
    return manifest, contents, signed


def verify_snapshot(path: Path) -> dict[str, Any]:
    """The copy's MANIFEST when every file is intact (``signed`` says whether it may be restored)."""
    manifest, _contents, signed = _read_verified(path)
    return {**manifest, "signed": signed}


def _check_key_opens(contents: dict[str, bytes]) -> None:
    """Refuse before touching anything when this machine's key cannot read the copy's secrets."""
    for name in ("secrets.enc", "secrets-undo.enc"):
        if name in contents:
            try:
                envelope = json.loads(contents[name].decode("utf-8"))
                master_fernet().decrypt(str(envelope["token"]).encode("ascii"))
            except SecretStoreError as exc:
                raise SnapshotError(
                    t("backup.snapshot.cannot_check", owner_language(), name=name, error=str(exc))
                ) from exc
            except Exception as exc:  # InvalidToken, a damaged envelope
                raise SnapshotError(t("backup.snapshot.other_key", owner_language(), name=name)) from exc


def restore_snapshot(path: Path, *, apply: bool = False) -> dict[str, Any]:
    path = Path(path)
    manifest, contents, signed = _read_verified(path)
    _check_key_opens(contents)
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
    if not signed:
        raise SnapshotError(t("backup.snapshot.unsigned", owner_language()))
    with persistence_lock():
        before_interval = _interval()
        plan = _restore_plan(contents)  # everything merged and checked before the first write
        safety = _begin_restore(plan, snapshot=path.name)
        _apply_restore(plan, safety)
    result.update({"applied": True, "safety_copy": str(safety)})
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
            if target.is_file():
                saved = target.read_bytes()
                atomic_write_bytes(safety / name, saved)
                entries.append({"name": name, "existed": True, "sha256": hashlib.sha256(saved).hexdigest()})
            else:
                entries.append({"name": name, "existed": False})
        _write_journal(safety, {"format": _JOURNAL_FORMAT, "snapshot": snapshot, "entries": entries}, "prepared")
        atomic_write_bytes(data_dir() / _MARKER, json.dumps({"safety": safety.name}).encode("utf-8"))
    except OSError as exc:
        shutil.rmtree(safety, ignore_errors=True)
        raise SnapshotError(t("backup.snapshot.restore_failed", owner_language(), reason=_reason(exc))) from exc
    return safety


def _write_journal(safety: Path, journal: dict[str, Any], status: str) -> None:
    journal = {**journal, "status": status, "updated_at": datetime.now(UTC).isoformat()}
    atomic_write_bytes(safety / _JOURNAL, (json.dumps(journal, indent=2) + "\n").encode("utf-8"))


def _read_journal(safety: Path) -> dict[str, Any]:
    journal = json.loads((safety / _JOURNAL).read_text(encoding="utf-8"))
    if (
        not isinstance(journal, dict)
        or journal.get("format") != _JOURNAL_FORMAT
        or journal.get("status") not in {"prepared", "committed", "rolled_back"}
        or not isinstance(journal.get("entries"), list)
    ):
        raise ValueError("not a night-copy restore journal")
    return journal


def _finish(safety: Path, status: str) -> None:
    """Record the outcome first, then drop the marker: a marker left behind is harmless."""
    _write_journal(safety, _read_journal(safety), status)
    (data_dir() / _MARKER).unlink(missing_ok=True)
    _tidy_safety_copies()


def _marked_safety() -> str | None:
    """The before-restore folder the marker names (a restore in progress or to recover), if any."""
    try:
        name = json.loads((data_dir() / _MARKER).read_text(encoding="utf-8"))["safety"]
    except FileNotFoundError:
        return None
    except OSError, UnicodeError, ValueError, KeyError, TypeError:
        return ""  # unreadable: some folder may still be needed, so none is touched
    return str(name)


def _tidy_safety_copies(keep: int = SAFETY_KEEP) -> list[str]:
    """Finished before-restore copies: without the settings undo, the newest ``keep`` only.

    Never the one a marker names, never one whose journal still says ``prepared`` (a restore
    that is not finished needs every file). Best effort: a file held right now goes next time.
    Returns the names of the folders removed.
    """
    marked = _marked_safety()
    if marked == "":
        return []
    try:
        folders = sorted(
            p for p in data_dir().iterdir() if p.is_dir() and not p.is_symlink() and _SAFETY_NAME.fullmatch(p.name)
        )
    except OSError:
        return []
    finished = []
    for folder in folders:
        if folder.name == marked:
            continue
        with contextlib.suppress(OSError, UnicodeError, ValueError, KeyError, TypeError):
            if _read_journal(folder)["status"] == "prepared":
                continue
        finished.append(folder)
        with contextlib.suppress(OSError):
            (folder / _UNDO_IN_SAFETY).unlink(missing_ok=True)
    removed = finished[: max(0, len(finished) - keep)]
    for folder in removed:
        shutil.rmtree(folder, ignore_errors=True)
    return [folder.name for folder in removed]


def _apply_restore(plan: list[tuple[str, Path, bytes | None]], safety: Path) -> None:
    try:
        for name, target, content in plan:
            if content is None:
                target.unlink(missing_ok=True)
                continue
            atomic_write_bytes(target, content)
            if target.read_bytes() != content:
                raise SnapshotError(t("backup.snapshot.read_back_failed", owner_language(), name=name))
        _finish(safety, "committed")
    except Exception as exc:
        try:
            _roll_back(safety)
        except Exception as rollback_exc:
            log_event("backup_restore_failed", error=_reason(exc), rollback="failed", how="manual")
            raise SnapshotError(
                t("backup.snapshot.rollback_failed", owner_language(), path=str(safety))
            ) from rollback_exc
        log_event("backup_restore_failed", error=_reason(exc), rollback="done", how="manual")
        raise SnapshotError(t("backup.snapshot.restore_failed", owner_language(), reason=_reason(exc))) from exc


def _roll_back(safety: Path) -> None:
    """Put back every file the journal saved (config.yaml first: it says where restore points live)."""
    journal = _read_journal(safety)
    entries = sorted(journal["entries"], key=lambda entry: entry.get("name") != "config.yaml")
    for entry in entries:
        name = str(entry.get("name"))
        target = _member_target(name)
        if entry.get("existed"):
            content = (safety / name).read_bytes()
            if hashlib.sha256(content).hexdigest() != entry.get("sha256"):
                raise SnapshotError(t("backup.snapshot.file_damaged", owner_language(), name=name))
            atomic_write_bytes(target, content)
        else:
            target.unlink(missing_ok=True)
    _finish(safety, "rolled_back")


class _MarkerGone(Exception):
    """The marker disappeared while it was read: another process finished the recovery."""


def _marked_journal(marker: Path) -> tuple[Path, str]:
    """(before-restore folder, journal status) the marker points at."""
    try:
        raw = marker.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise _MarkerGone from exc
    name = str(json.loads(raw)["safety"])
    if _SAFETY_NAME.fullmatch(name) is None:
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
    if not marker.exists():
        return
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
        except (UnicodeError, ValueError, KeyError, TypeError) as exc:
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
    """The copy's config with this machine's access settings (the live ones).

    A live config.yaml that cannot be read does not stop the restore - replacing it is often
    the point - and then the restored config is local-only: network access is turned back on
    from this PC, as always.
    """
    try:
        restored = yaml.safe_load(snapshot_config.decode("utf-8")) or {}
    except (UnicodeError, yaml.YAMLError) as exc:
        raise SnapshotError(t("backup.snapshot.config_not_mapping", owner_language())) from exc
    if not isinstance(restored, dict):
        raise SnapshotError(t("backup.snapshot.config_not_mapping", owner_language()))
    try:
        current = yaml.safe_load(config_path().read_text(encoding="utf-8")) or {}
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
    return str(yaml.safe_dump(restored, allow_unicode=True, sort_keys=False, default_flow_style=False)).encode("utf-8")
