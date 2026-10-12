"""The data lock, the check lock and the recovery that runs before anyone writes again.

``persistence_lock`` serializes every store write across threads and TOW processes, and the
first process to take it runs ``RECOVERY_STEPS``; ``check_run_lock`` keeps one applying check
at a time. Their lock files in the data folder keep their names: other TOW versions take the
same locks.
"""

from __future__ import annotations

import importlib
import os
import re
import stat
import threading
import time
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager, suppress
from pathlib import Path
from typing import BinaryIO

from tow.paths import config_path, data_dir, download_history_path, state_path
from tow.platform import locks as platform_locks

_WRITE_LOCK = threading.RLock()
_PROCESS_LOCK_STATE = threading.local()
_LOCK_FILE_NAME = ".tow-persistence.lock"


def init_lock_file(handle: BinaryIO) -> None:
    """Give a new lock file its single lock byte.

    Two processes can open a brand-new lock file at once; if the other one has
    already written and locked byte 0, our write hits the locked region
    (PermissionError on Windows). The byte exists either way, so ignore that.
    """
    handle.seek(0, os.SEEK_END)
    if handle.tell() == 0:
        try:
            handle.write(b"\0")
            handle.flush()
        except OSError:
            pass
    handle.seek(0)


# Every journal of an interrupted multi-file write, recovered by whichever process takes the
# data lock first (outermost level) - before ANY process writes again, and whatever that process
# imported: each step is imported here when it runs, never registered by an import elsewhere.
# (module, function, marker): with a marker the module is imported only when data/<marker> exists.
RECOVERY_STEPS: tuple[tuple[str, str, str | None], ...] = (
    # site settings: config + state + secrets of one web transaction
    ("tow.site_journal", "recover_site_journal", None),
    # a check's commit of state, history and secrets
    ("tow.check_transaction", "recover_locked", None),
    # an import of a portable bundle
    ("tow.bundle", "recover_interrupted_import", None),
    # a restore from a night copy (a check never needs the night-copy code otherwise)
    ("tow.snapshots", "recover_interrupted_restore", ".tow-night-restore.json"),
    # the temporary files of single-file writes that were killed before their replace
    ("tow.storage.locks", "remove_stale_temporaries", None),
)

# What tempfile adds between the prefix and the suffix atomic_write_bytes() gives it (eight
# of these characters today), then that suffix.
_TEMPORARY = re.compile(r"[a-z0-9_]+\.tmp")
# A temporary file this young may still be written (by a writer outside the data lock).
STALE_TEMPORARY_AGE_SEC = 5.0


def is_temporary_of(name: str, stores: Iterable[str]) -> bool:
    """``name`` is a temporary file atomic_write_bytes() made for one of ``stores`` (file
    names): ``.<store>.<random letters>.tmp``. Nothing else matches."""
    for store in stores:
        prefix = f".{store}."
        if name.startswith(prefix) and _TEMPORARY.fullmatch(name[len(prefix) :]):
            return True
    return False


def _store_paths() -> list[Path]:
    # Imported when it runs: the secrets are written through this module's lock.
    from tow.storage.secrets import encrypted_secrets_path, secret_undo_path

    paths = [state_path(), download_history_path(), encrypted_secrets_path(), secret_undo_path()]
    with suppress(OSError, RuntimeError):  # no config file yet: nothing of it to clean
        paths.append(config_path())
    return paths


def remove_stale_temporaries(*, now: float | None = None) -> None:
    """Remove what a writer killed between its temporary file and the replace left beside the
    stores (the state's is a full copy of it), never anything else: only plain files named
    like the temporary of one of the stores, at least STALE_TEMPORARY_AGE_SEC old. Writers
    hold the data lock, which the caller holds too; a file that cannot go now stays."""
    cutoff = (time.time() if now is None else now) - STALE_TEMPORARY_AGE_SEC
    folders: dict[Path, set[str]] = {}
    for path in _store_paths():
        folders.setdefault(path.parent, set()).add(path.name)
    for folder, names in folders.items():
        try:
            entries = list(os.scandir(folder))
        except OSError:
            continue
        for entry in entries:
            if not is_temporary_of(entry.name, names):
                continue
            try:
                info = entry.stat(follow_symlinks=False)
                if stat.S_ISREG(info.st_mode) and info.st_mtime < cutoff:
                    os.unlink(entry.path)
            except OSError:
                continue


def _recovery_steps() -> list[Callable[[], None]]:
    steps: list[Callable[[], None]] = []
    for module, name, marker in RECOVERY_STEPS:
        if marker is not None:
            try:
                (data_dir() / marker).lstat()
            except FileNotFoundError:
                continue
            except OSError:
                pass  # an unreadable marker is not absent; let its recovery hook refuse safely
        steps.append(getattr(importlib.import_module(module), name))
    return steps


def _run_recovery_hooks() -> None:
    if getattr(_PROCESS_LOCK_STATE, "recovering", False):
        return
    _PROCESS_LOCK_STATE.recovering = True
    try:
        for step in _recovery_steps():
            step()
    finally:
        _PROCESS_LOCK_STATE.recovering = False


_CHECK_RUN_LOCK = threading.Lock()
_CHECK_RUN_LOCK_NAME = ".tow-check-run.lock"


class CheckBusyError(RuntimeError):
    """Another applying check is running and the caller asked not to wait for it."""


@contextmanager
def check_run_lock(*, wait: bool = True) -> Iterator[None]:
    """One applying check at a time (scheduled, progress, web) across TOW processes.

    Separate from ``persistence_lock``: a check holds this one for its whole run, network
    included, but takes the persistence lock only to read and to commit - the web UI, the
    watchdog and the night copy are never held up by tracker or client network time.
    ``wait=False`` (a web request) raises ``CheckBusyError`` at once instead of waiting
    minutes for a running check.
    """
    if not _CHECK_RUN_LOCK.acquire(blocking=wait):
        raise CheckBusyError("a check is already running")
    try:
        lock_path = data_dir() / _CHECK_RUN_LOCK_NAME
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        handle = lock_path.open("a+b")
        try:
            init_lock_file(handle)
            if not platform_locks.lock(handle, wait=wait):
                raise CheckBusyError("a check is already running")
            try:
                yield
            finally:
                platform_locks.unlock(handle)
        finally:
            handle.close()
    finally:
        _CHECK_RUN_LOCK.release()


@contextmanager
def persistence_lock() -> Iterator[None]:
    """Serialize persistence across threads and TOW processes, reentrantly."""
    with _WRITE_LOCK:
        depth = int(getattr(_PROCESS_LOCK_STATE, "depth", 0))
        owner_pid = getattr(_PROCESS_LOCK_STATE, "owner_pid", None)
        current_pid = os.getpid()
        if depth and owner_pid == current_pid:
            _PROCESS_LOCK_STATE.depth = depth + 1
            try:
                yield
            finally:
                _PROCESS_LOCK_STATE.depth -= 1
            return
        if depth:
            inherited_handle = getattr(_PROCESS_LOCK_STATE, "handle", None)
            _PROCESS_LOCK_STATE.depth = 0
            _PROCESS_LOCK_STATE.owner_pid = None
            _PROCESS_LOCK_STATE.handle = None
            if inherited_handle is not None:
                inherited_handle.close()

        lock_path = data_dir() / _LOCK_FILE_NAME
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        handle = lock_path.open("a+b")
        try:
            init_lock_file(handle)
            platform_locks.lock(handle)
            _PROCESS_LOCK_STATE.depth = 1
            _PROCESS_LOCK_STATE.owner_pid = current_pid
            _PROCESS_LOCK_STATE.handle = handle
            try:
                _run_recovery_hooks()
                yield
            finally:
                _PROCESS_LOCK_STATE.depth = 0
                _PROCESS_LOCK_STATE.owner_pid = None
                _PROCESS_LOCK_STATE.handle = None
                platform_locks.unlock(handle)
        finally:
            handle.close()
