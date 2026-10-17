"""What the web's synchronization (``tow.web.sync``) reads, and how: complete snapshots of the
install's files, taken without a lock, a write, a quarantine or a folder made.

A file is read whole through one handle and stat'ed before and after; a reading that saw the
file change (another size, another file, another stamp) is taken again, a few times. The
answer tells a missing file (``missing``) from one that is there but could not be read now
(``error``: a sharing violation, a permission, a file that kept changing) - the first is a
fact about the data, the second only about this moment. The bytes come with their SHA-256: a
file's stamp (mtime, size, file id) only hints that it changed - a coarse clock or a same-size
rewrite in the same tick keeps it - the digest says whether it did.
"""

from __future__ import annotations

import hashlib
import os
import stat as stat_mode
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from tow import paths
from tow.storage.atomic import RegularReadError, read_regular_bytes

READ_ATTEMPTS = 3
RETRY_PAUSE_SEC = 0.01
# The tail of the event log read for its marker: enough to see an append, never the whole log.
LOG_TAIL_BYTES = 4096

type Stamp = tuple[int, int, int, int]


@dataclass(frozen=True)
class Reading:
    """One file as read: ``status`` "ok" (``content`` and its ``digest``), "missing" (no such
    file) or "error" (there, but not readable completely now: ``error`` says why)."""

    status: str
    content: bytes | None = None
    digest: str = ""
    stamp: Stamp | None = None
    error: str = ""


def _stamp(info: os.stat_result) -> Stamp:
    return (info.st_mtime_ns, info.st_size, info.st_ino, info.st_dev)


def read(path: Path, limit: int, *, attempts: int = READ_ATTEMPTS) -> Reading:
    """``path`` read whole and unchanged while it was read (see the module), or why not."""
    reason = "unstable"
    for attempt in range(max(1, attempts)):
        if attempt:
            time.sleep(RETRY_PAUSE_SEC)
        try:
            before = os.stat(path)
        except FileNotFoundError, NotADirectoryError:
            return Reading("missing")
        except OSError as exc:
            reason = type(exc).__name__
            continue
        if not stat_mode.S_ISREG(before.st_mode):
            return Reading("error", error="type")
        try:
            content = read_regular_bytes(path, limit, same_as=before)
            after = os.stat(path)
        except FileNotFoundError:
            reason = "replaced"
            continue  # replaced or removed meanwhile: the next look says which
        except RegularReadError as exc:
            if exc.kind != "changed":
                return Reading("error", error=exc.kind)
            reason = "changed"
            continue
        except OSError as exc:
            reason = type(exc).__name__
            continue
        if _stamp(after) != _stamp(before):
            reason = "changed"
            continue
        return Reading("ok", content, hashlib.sha256(content).hexdigest(), _stamp(before))
    return Reading("error", error=reason)


def stat_only(path: Path) -> Reading:
    """Whether ``path`` is there, by its stamp alone (a file whose content is not for this
    reader, such as a plaintext secrets file left from an old version)."""
    try:
        info = os.stat(path)
    except FileNotFoundError, NotADirectoryError:
        return Reading("missing")
    except OSError as exc:
        return Reading("error", error=type(exc).__name__)
    return Reading("ok", None, hashlib.sha256(repr(_stamp(info)).encode()).hexdigest(), _stamp(info))


def log_marker(path: Path) -> Reading:
    """The event log's marker: its stamp and the digest of its last LOG_TAIL_BYTES, read without
    the log's lock (an append in progress shows at the next look). Another digest: something was
    appended, the log was rotated or replaced."""
    reason = "unstable"
    for attempt in range(READ_ATTEMPTS):
        if attempt:
            time.sleep(RETRY_PAUSE_SEC)
        try:
            with path.open("rb") as handle:
                info = os.fstat(handle.fileno())
                handle.seek(max(0, info.st_size - LOG_TAIL_BYTES))
                tail = handle.read(LOG_TAIL_BYTES)
        except FileNotFoundError, NotADirectoryError:
            return Reading("missing")
        except OSError as exc:
            reason = type(exc).__name__
            continue
        marker = repr((info.st_size, info.st_ino, info.st_dev)).encode() + b"\0" + tail
        return Reading("ok", None, hashlib.sha256(marker).hexdigest(), _stamp(info))
    return Reading("error", error=reason)


def source_paths() -> dict[str, Path]:
    """The files synchronization watches, named without creating any folder."""
    from tow.log import log_path
    from tow.site_journal import journal_root
    from tow.storage.secrets import encrypted_secrets_path
    from tow.supervisor.layout import schedule_path, status_path

    with paths.read_only():
        return {
            "state": paths.state_path(),
            "history": paths.download_history_path(),
            "config": paths.config_path(),
            "secrets": encrypted_secrets_path(),
            "legacy_secrets": paths.secrets_path(),
            "status": status_path(),
            "schedule": schedule_path(),
            "log": log_path(),
            "journal": journal_root(),
        }


def quarantined(path: Path) -> bool:
    """A damaged copy of ``path`` was set aside (``tow.store``): a missing file is then not
    "empty" - the pages refuse it, and so does synchronization."""
    prefix = f"{path.name}.corrupt-"
    try:
        return any(entry.name.startswith(prefix) for entry in path.parent.iterdir())
    except OSError:
        return False


def transaction_pending(journal: Path) -> bool:
    """A store transaction is being written or was cut off (its journal is there): the stores
    may not belong together until it ends or is recovered."""
    try:
        return journal.exists() or journal.is_symlink()
    except OSError:
        return True


# Every journal or marker a writer leaves while the stores may not belong together, and that
# the recovery steps of the data lock (``tow.storage.locks.RECOVERY_STEPS``) act on: the site
# settings' (and every store) transaction, a check's commit, an import (a portable file or a
# restore point) and a night-copy restore. Their names are their modules' (tests/test_web_sync).
CHECK_JOURNAL = ".tow-check-transaction"
NIGHT_RESTORE_MARKER = ".tow-night-restore.json"
IMPORT_CHECKPOINTS = "import-checkpoints"
IMPORT_TRANSACTION = "TRANSACTION.json"
MAX_IMPORT_CHECKPOINTS = 256


def recovery_pending() -> str:
    """Which transaction is being written or waits for recovery ("" none): "site", "check",
    "night_restore" or "import". Looked at without a lock, a write or a folder made; a marker
    that cannot be looked at counts as there (the recovery refuses writes for it too)."""
    from tow.site_journal import journal_root

    with paths.read_only():
        data = paths.data_dir()
        markers = {"site": journal_root(), "check": data / CHECK_JOURNAL, "night_restore": data / NIGHT_RESTORE_MARKER}
    for name, marker in markers.items():
        if transaction_pending(marker):
            return name
    return "import" if _import_pending(data / IMPORT_CHECKPOINTS) else ""


def _import_pending(root: Path) -> bool:
    """An import's transaction still "prepared": the import runs, or a crash stopped it."""
    import json

    try:
        entries = [entry for entry in os.scandir(root) if entry.is_dir(follow_symlinks=False)]
    except FileNotFoundError, NotADirectoryError:
        return False
    except OSError:
        return True
    for entry in sorted(entries, key=lambda item: item.name, reverse=True)[:MAX_IMPORT_CHECKPOINTS]:
        reading = read(Path(entry.path) / IMPORT_TRANSACTION, 64 * 1024)
        if reading.status == "missing":
            continue
        try:
            status = json.loads(reading.content or b"").get("status") if reading.status == "ok" else None
        except ValueError, AttributeError, RecursionError:
            status = None
        if status not in {"committed", "rolled_back"}:
            return True
    return False


def supervisor_running() -> bool:
    """``tow run`` of this install holds its lock (probed without waiting, nothing created)."""
    from tow.supervisor.layout import running

    with paths.read_only():
        try:
            return running() is not None
        except OSError:
            return False


def secrets_from_bytes(content: bytes | None) -> dict[str, Any]:
    """The secrets of secrets.enc's bytes (None: no file, no secrets)."""
    from tow.storage.secrets import decrypt_secrets_bytes

    return {} if content is None else decrypt_secrets_bytes(content)
