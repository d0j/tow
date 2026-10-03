"""Launch the existing updater outside the replaceable app and read its durable result."""

from __future__ import annotations

import json
import math
import os
import re
import stat
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx

from tow import __version__, platform, releases
from tow.log import scrub_text
from tow.paths import repo_root, root, runtime_dir
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
    return stat.S_ISREG(info.st_mode) and not (
        getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
    )


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
    return folder / "job.json"


def _active(job: dict[str, Any]) -> bool:
    if job and (
        not isinstance(job.get("status"), str)
        or job["status"] not in _ACTIVE | _TERMINAL
        or not isinstance(job.get("id"), str)
        or re.fullmatch(r"[a-f0-9]{32}", job["id"]) is None
    ):
        raise WebUpdateError("releases.job_unreadable")
    if job.get("status") not in _ACTIVE:
        return False
    pid = job.get("pid")
    if isinstance(pid, int) and not isinstance(pid, bool) and pid > 0:
        return platform.current().process_alive(pid)
    started = job.get("started_at")
    return (
        job.get("status") == "queued"
        and isinstance(started, (float, int))
        and not isinstance(started, bool)
        and math.isfinite(started)
        and 0 <= time.time() - started < 30
    )


def status() -> dict[str, Any]:
    try:
        _runtime_app()
    except WebUpdateError as exc:
        return {"supported": False, "reason": str(exc), "current": __version__, "status": "idle", "previous": ""}
    job = _read(_job_file())
    updater = _read(root() / "update-state.json")
    job = _completed_job(job, updater)
    previous = updater.get("previous_version") if updater.get("status") == "ok" else ""
    parsed = releases.version_parts(previous)
    if parsed is None or parsed < MINIMUM_WEB_VERSION or previous == __version__:
        previous = ""
    result = {
        key: job.get(key)
        for key in ("id", "status", "target", "previous", "started_at", "finished_at", "error", "safety_point")
    }
    active = _active(job)
    for key in ("target", "previous", "error", "safety_point"):
        if key in job and not isinstance(job[key], str):
            raise WebUpdateError("releases.job_unreadable")
    if job.get("status") in _ACTIVE and not active:
        result.update(status="interrupted", error="releases.interrupted")
    if job.get("status") == "ok" and job.get("target") != __version__:
        result["status"] = "superseded"  # a later terminal update changed the installed version
    result.update(supported=True, current=__version__, active=active, rollback_version=previous)
    if not job:
        result["status"] = "idle"
    return result


def _completed_job(job: dict[str, Any], updater: dict[str, Any]) -> dict[str, Any]:
    """A finished terminal recovery can unblock a dead job; GET itself writes nothing."""
    active = _active(job)  # validate types before set membership, including damaged journals
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
    except ValueError, TypeError, KeyError, OverflowError:
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
    return scrub_text(content.decode("utf-8", "replace"))


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
            job = {
                "id": job_id,
                "status": "queued",
                "target": version,
                "previous": __version__,
                "started_at": time.time(),
                "safety_point": safety["id"],
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
    return {"ok": True, "id": job_id, "target": version, "status": "queued", "safety_point": safety["id"]}
