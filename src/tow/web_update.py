"""Launch the existing updater outside the replaceable app and read its durable result."""

from __future__ import annotations

import json
import math
import os
import re
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx

from tow import __version__, platform, releases
from tow.diagnostic_json import check_epochs
from tow.log import scrub_text
from tow.paths import repo_root, root, runtime_dir
from tow.platform import locks
from tow.restore_points import RestorePointError, create_restore_point
from tow.store import StoreCorruptionError, atomic_write_bytes, decode_json_bytes, persistence_lock

MINIMUM_WEB_VERSION = (1, 22, 21)
_ACTIVE = frozenset({"queued", "preparing", "stopping", "backup", "installing", "checking", "rolling_back"})
_TERMINAL = frozenset({"ok", "refused", "failed", "rolled_back", "recovered"})


class WebUpdateError(RuntimeError):
    pass


def _safe_file(path: Path) -> bool:
    try:
        info = path.lstat()
    except OSError:
        return False
    return platform.is_plain_file(info)


def _read(path: Path) -> dict[str, Any]:
    if not path.exists() and not path.is_symlink():
        return {}
    if not _safe_file(path):
        raise WebUpdateError("releases.job_unreadable")
    try:
        with path.open("rb") as handle:
            content = handle.read(65537)
        if len(content) > 65536:
            raise ValueError("oversized job")
        value = decode_json_bytes(content)
        if not isinstance(value, dict):
            raise TypeError("invalid job")
    except (OSError, ValueError, TypeError, RecursionError, StoreCorruptionError) as exc:
        raise WebUpdateError("releases.job_unreadable") from exc
    return value


def _runtime_app() -> Path:
    app = repo_root().resolve()
    if app != (root() / "app").resolve() or not (app / "scripts" / "update.py").is_file():
        raise WebUpdateError("releases.not_runtime")
    if platform.current().name != "windows" and os.environ.get("TOW_AUTOSTART") in {"systemd", "launchd"}:
        raise WebUpdateError("releases.detached_unavailable")
    return app


def _job_file() -> Path:
    folder = runtime_dir() / "web-update"
    if folder.is_symlink() or folder.resolve() != root().resolve() / "runtime" / "web-update":
        raise WebUpdateError("releases.job_unreadable")
    # The relay binds the journal to its resolved copied-script location. Normalize
    # our trusted install path here; never resolve a caller-selected path in the worker.
    return folder.resolve() / "job.json"


def _active(job: dict[str, Any]) -> bool:
    return _activity(job)[0]


def _activity(job: dict[str, Any]) -> tuple[bool, bool]:
    if job and (
        not isinstance(job.get("status"), str)
        or job["status"] not in _ACTIVE | _TERMINAL
        or not isinstance(job.get("id"), str)
        or re.fullmatch(r"[a-f0-9]{32}", job["id"]) is None
    ):
        raise WebUpdateError("releases.job_unreadable")
    if "lease_version" in job and (type(job["lease_version"]) is not int or job["lease_version"] != 1):
        raise WebUpdateError("releases.job_unreadable")
    try:
        check_epochs(job, ("started_at", "finished_at"))
    except ValueError as exc:
        raise WebUpdateError("releases.job_unreadable") from exc
    if job.get("status") not in _ACTIVE:
        return False, False
    if "lease_version" in job:
        if _lease_active(job["id"]):
            return True, False
    else:
        # Older workers do not hold a lease. Preserve their conservative reservation;
        # never silently turn an unverified old live worker into a dead one.
        pid = job.get("pid")
        if isinstance(pid, int) and not isinstance(pid, bool) and pid > 0:
            identity = _legacy_worker_active(pid, job["id"])
            return identity is not False, identity is None
    started = job.get("started_at")
    return (
        job.get("status") == "queued"
        and isinstance(started, (float, int))
        and not isinstance(started, bool)
        and math.isfinite(started)
        and 0 <= time.time() - started < 30,
        False,
    )


def _lease_active(job_id: str) -> bool:
    folder = _job_file().parent / job_id
    path = folder / "worker.lock"
    try:
        info = folder.lstat()
        if (
            not platform.is_plain_dir(info)
            or folder.resolve() != _job_file().parent.resolve() / job_id
            or not _safe_file(path)
            or path.stat().st_size != 1
        ):
            raise WebUpdateError("releases.job_unreadable")
        # Open an existing lease, never create/replace it during a status read.
        with path.open("r+b") as handle:
            if not locks.lock(handle, wait=False):
                return True
            locks.unlock(handle)
    except OSError as exc:
        raise WebUpdateError("releases.job_unreadable") from exc
    return False


def _legacy_worker_active(pid: int, job_id: str) -> bool | None:
    backend = platform.current()
    if not backend.process_alive(pid):
        return False
    command_reader = getattr(backend, "process_command", None)
    command = command_reader(pid) if command_reader else None
    if not isinstance(command, str) or not command:
        return None  # unknown identity is not permission to overwrite a possibly live job
    expected = str(_job_file().parent / job_id / "worker.py").replace("\\", "/")
    command = command.replace("\\", "/")
    if backend.name == "windows":
        expected, command = expected.casefold(), command.casefold()
    # Pre-lease workers already have a unique nonce in their absolute script path.
    # Match the entire script argument, not a basename, substring or arbitrary PID.
    if re.search(r"(?:^|[\s\"'])" + re.escape(expected) + r"(?=$|[\s\"'])", command) is not None:
        return True
    # ps can escape unusual filesystem characters. If the unique nonce is still
    # present but the complete argument cannot be verified, do not declare it dead.
    return None if job_id in command else False


def status() -> dict[str, Any]:
    try:
        _runtime_app()
    except WebUpdateError as exc:
        return {"supported": False, "reason": str(exc), "current": __version__, "status": "idle", "previous": ""}
    job = _read(_job_file())
    updater = _read(root() / "update-state.json")
    active, unverified = _activity(job)
    job = _recover_completed(job, updater, active)
    previous = updater.get("previous_version") if updater.get("status") == "ok" else ""
    parsed = releases.version_parts(previous)
    if parsed is None or parsed < MINIMUM_WEB_VERSION or previous == __version__:
        previous = ""
    result = {
        key: job.get(key)
        for key in ("id", "status", "target", "previous", "started_at", "finished_at", "error", "safety_point")
    }
    for key in ("target", "previous", "error", "safety_point"):
        if key in job and not isinstance(job[key], str):
            raise WebUpdateError("releases.job_unreadable")
    if "backup_cleanup_pending" in job and not isinstance(job["backup_cleanup_pending"], bool):
        raise WebUpdateError("releases.job_unreadable")
    result["backup_cleanup_pending"] = job.get("backup_cleanup_pending") is True
    if job.get("status") in _ACTIVE and not active:
        result.update(status="interrupted", error="releases.interrupted")
    elif unverified:
        result["error"] = "releases.worker_unverified"
    if job.get("status") == "ok" and job.get("target") != __version__:
        result["status"] = "superseded"  # a later terminal update changed the installed version
    result.update(supported=True, current=__version__, active=active, rollback_version=previous)
    if not job:
        result["status"] = "idle"
    return result


def _completed_job(job: dict[str, Any], updater: dict[str, Any]) -> dict[str, Any]:
    """A finished terminal recovery can unblock a dead job; GET itself writes nothing."""
    return _recover_completed(job, updater, _active(job))


def _recover_completed(job: dict[str, Any], updater: dict[str, Any], active: bool) -> dict[str, Any]:
    if active or job.get("status") not in _ACTIVE | {"failed", "refused", "rolled_back"}:
        return job
    started = job.get("started_at")
    if not isinstance(started, (int, float)) or isinstance(started, bool) or not math.isfinite(started):
        return job
    try:
        finished = datetime.fromisoformat(updater["finished_at"])
        last = job.get("finished_at", started)
        if type(last) not in {int, float} or not math.isfinite(last):
            return job
        if finished.tzinfo is None or finished.timestamp() < max(started, last):
            return job
    except OSError, ValueError, TypeError, KeyError, OverflowError:
        return job
    expected = updater.get("target_version") if updater.get("status") == "ok" else updater.get("previous_version")
    if (
        not isinstance(updater.get("status"), str)
        or updater["status"] not in {"ok", "rolled_back"}
        or expected != __version__
    ):
        return job
    return {**job, "status": "recovered", "error": "", "finished_at": finished.timestamp()}


def log_tail() -> str:
    job = _read(_job_file())
    _active(job)  # validate the stored identifier before using it in a filename
    if not job:
        return ""
    path = _job_file().parent / job["id"] / "update.log"
    if not path.exists():
        return ""
    if not _safe_file(path) or not path.resolve().is_relative_to(_job_file().parent.resolve()):
        raise WebUpdateError("releases.job_unreadable")
    try:
        with path.open("rb") as handle:
            handle.seek(max(0, path.stat().st_size - 16000))
            content = handle.read(16000)
    except OSError as exc:
        raise WebUpdateError("releases.job_unreadable") from exc
    return _without_install_path(scrub_text(content.decode("utf-8", "replace")))


INSTALL_PLACEHOLDER = "<TOW>"


def _without_install_path(text: str) -> str:
    """The install folder as <TOW>: the page is also read from other devices of the network,
    and the folder usually names the account (C:\\Users\\<name>\\TOW)."""
    install = root()
    forms = {str(install), str(install.resolve()), install.as_posix(), install.resolve().as_posix()}
    flags = re.IGNORECASE if platform.is_windows() else 0
    for form in sorted((form.rstrip("\\/") for form in forms if len(form) > 3), key=len, reverse=True):
        text = re.sub(re.escape(form), INSTALL_PLACEHOLDER, text, flags=flags)
    return text


def start(version: str) -> dict[str, Any]:
    app = _runtime_app()
    parsed = releases.version_parts(version)
    if parsed is None or parsed < MINIMUM_WEB_VERSION:
        raise WebUpdateError("releases.unsupported_version")
    version = version.removeprefix("v")
    if version == __version__:
        raise WebUpdateError("releases.already_installed")
    try:
        releases.published_version(version)
    except (httpx.HTTPError, OSError, ValueError, RuntimeError) as exc:
        raise WebUpdateError("releases.not_published") from exc
    with persistence_lock():
        path = _job_file()
        old = _read(path)
        old = _completed_job(old, _read(root() / "update-state.json"))
        if _active(old):
            raise WebUpdateError("releases.busy")
        if old.get("status") in _ACTIVE:
            raise WebUpdateError("releases.interrupted")
        python = Path(getattr(sys, "_base_executable", sys.executable)).resolve()
        if not python.is_file() or python.is_relative_to(app / ".venv"):
            raise WebUpdateError("releases.no_python")
        # Archive creation is serialized with store writes and verified by export_bundle.
        try:
            safety = create_restore_point()
        except RestorePointError as exc:
            raise WebUpdateError("releases.backup_failed") from exc
        job_id = uuid.uuid4().hex
        folder = path.parent / job_id
        try:
            folder.mkdir(parents=True, exist_ok=False)
            atomic_write_bytes(folder / "update.py", (app / "scripts" / "update.py").read_bytes())
            atomic_write_bytes(folder / "worker.py", Path(__file__).with_name("update_worker.py").read_bytes())
            atomic_write_bytes(folder / "locks.py", Path(locks.__file__).read_bytes())
            atomic_write_bytes(folder / "worker.lock", b"\0")
            job = {
                "id": job_id,
                "status": "queued",
                "target": version,
                "previous": __version__,
                "started_at": time.time(),
                "safety_point": safety["id"],
                "backup_cleanup_pending": bool(safety.get("cleanup_warning")),
                "lease_version": 1,
            }
            atomic_write_bytes(path, json.dumps(job).encode())
            platform.current().spawn_detached(
                [
                    str(python),
                    "-I",
                    "-S",
                    "-u",
                    str(folder / "worker.py"),
                    "--handoff",
                    str(app),
                    str(path),
                    job_id,
                    version,
                ],
                hidden=True,
                cwd=root(),
                log_path=folder / "update.log",
                env=dict(os.environ),
                # This is only the short-lived relay. The actual worker must verify that
                # it left every job, using the local Windows broker when breakaway cannot.
                require_breakaway=False,
            )
        except (OSError, RuntimeError) as exc:
            if path.exists() and _read(path).get("id") == job_id:
                job.update(status="failed", error="releases.launch_failed", finished_at=time.time())
                atomic_write_bytes(path, json.dumps(job).encode())
            raise WebUpdateError("releases.launch_failed") from exc
    return {
        "ok": True,
        "id": job_id,
        "target": version,
        "status": "queued",
        "safety_point": safety["id"],
        "backup_cleanup_pending": bool(safety.get("cleanup_warning")),
    }
