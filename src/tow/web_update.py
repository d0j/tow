"""Launch the existing updater outside the replaceable app and read its durable result."""

from __future__ import annotations

import contextlib
import json
import math
import os
import re
import sys
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from tow import __version__, platform, releases
from tow.diagnostic_json import check_epochs
from tow.log import log_event, scrub_text
from tow.paths import repo_root, root, runtime_dir
from tow.platform import locks
from tow.restore_points import CREATE_FAILED, MASTER_KEY, RestorePointError, create_restore_point
from tow.scrub import paths_out
from tow.store import StoreCorruptionError, atomic_write_bytes, decode_json_bytes, persistence_lock

MINIMUM_WEB_VERSION = (1, 22, 21)
# Older versions take a topic whose torrent client still checks its files for done
# (``verification_pending``, scripts/update.py RECEIPTS_SINCE).
RECEIPTS_SINCE = (1, 30, 5)
# Older versions read a prepared torrent's id only as ``content_token`` (scripts/update.py
# CONTENT_REF_SINCE).
CONTENT_REF_SINCE = (1, 32, 0)
# Folders of earlier web updates (their copied updater and update.log) kept besides the current
# job's; older ones are removed when the next update starts.
JOB_FOLDERS_KEPT = 3
# The longest an update keeps TOW running once its release is prepared, for the open pages to see
# the job (``start``'s ``grace``; update_worker.GRACE_MAX_SEC, which this module cannot import).
GRACE_MAX_SEC = 6.0
_JOB_ID = re.compile(r"[a-f0-9]{32}")
_ACTIVE = frozenset({"queued", "preparing", "stopping", "backup", "installing", "checking", "rolling_back"})
_TERMINAL = frozenset({"ok", "refused", "failed", "rolled_back", "recovered"})


# Why a restore point before an update failed, in fixed words (its details are in the Log).
_RESTORE_POINT_REASONS = {
    MASTER_KEY: "releases.backup_reason.master_key",
    CREATE_FAILED: "releases.backup_reason.write",
}
# Every refusal a WebUpdateError says, its one parameter and that parameter's words: the web
# answer is built from these constants alone, never from the exception (routes_health._said).
REFUSALS = (
    "releases.already_installed",
    "releases.backup_failed_because",
    "releases.busy",
    "releases.content_ref_pending",
    "releases.detached_unavailable",
    "releases.interrupted",
    "releases.job_unreadable",
    "releases.launch_failed",
    "releases.no_python",
    "releases.not_published",
    "releases.not_runtime",
    "releases.receipt_pending",
    "releases.unsupported_version",
)
REFUSAL_PARAM = "reason"
REFUSAL_REASONS = (*_RESTORE_POINT_REASONS.values(), "releases.backup_reason.other")


class WebUpdateError(RuntimeError):
    """A catalog key of ``REFUSALS`` (the message) and its `params`: ``reason``, a catalog key of
    ``REFUSAL_REASONS``, translated with it."""

    def __init__(self, key: str, **params: str) -> None:
        super().__init__(key)
        self.params = params


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


def _updater_record() -> dict[str, Any]:
    """The updater's record of its last run (``<TOW>/update-state.json``): information only.

    A damaged or half-written record (a power cut right after a terminal update) is no record:
    it never blocks Settings or a new update. Only the web job's own journal fails closed.
    """
    try:
        return _read(root() / "update-state.json")
    except WebUpdateError:
        return {}


def _runtime_app() -> Path:
    app = repo_root().resolve()
    if app != (root() / "app").resolve() or not (app / "scripts" / "update.py").is_file():
        raise WebUpdateError("releases.not_runtime")
    from tow.autostart import service_manager

    # systemd and launchd end every process of TOW's service with it: the updater too.
    if not platform.is_windows() and service_manager() is not None:
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


def _settled_job() -> tuple[dict[str, Any], bool, bool]:
    """job.json with ``_activity``. The worker writes its result before it lets go of its lease, so
    a job read as running whose lease is already free is read once more: a result written between
    the read and the probe (a refusal 2 s after the start) is that result, never "interrupted"."""
    path = _job_file()
    job = _read(path)
    active, unverified = _activity(job)
    if job.get("status") in _ACTIVE and not active:
        again = _read(path)
        if again != job:
            job = again
            active, unverified = _activity(job)
    return job, active, unverified


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
    job, active, unverified = _settled_job()
    updater = _updater_record()
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


def finished_job() -> dict[str, Any] | None:
    """The last web update's record once it ended (ok, refused, failed, rolled back or
    recovered), read only: no lease probe, recovery or write. None while it runs, without a
    job, and for a record that cannot be read (Settings says why)."""
    try:
        job = _read(_job_file())
    except WebUpdateError:
        return None
    status = job.get("status")
    if not isinstance(status, str) or status not in _TERMINAL:  # [] or {}: unreadable, not a TypeError
        return None
    if not isinstance(job.get("id"), str) or not _JOB_ID.fullmatch(job["id"]):
        return None
    if job.get("status") in {"failed", "refused", "rolled_back"}:
        job = _recover_completed(job, _updater_record(), False)
    return job


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
    # "recovered": a cut-off archive switch was undone by a later terminal run that then ended
    # (the release was installed already, or the run was refused): the previous version is back.
    expected = updater.get("target_version") if updater.get("status") == "ok" else updater.get("previous_version")
    if (
        not isinstance(updater.get("status"), str)
        or updater["status"] not in {"ok", "rolled_back", "recovered"}
        or expected != __version__
    ):
        return job
    return {**job, "status": "recovered", "error": "", "finished_at": finished.timestamp()}


def _prune_job_folders(current: str) -> None:
    """The folders of earlier web updates beyond the newest ``JOB_FOLDERS_KEPT``: never the
    ``current`` job's (job.json names it), never one whose worker still holds its lease, never
    anything but a plain folder named like a job; one that cannot be removed now stays."""
    import shutil

    parent = _job_file().parent
    folders = []
    with contextlib.suppress(OSError):
        for entry in parent.iterdir():
            with contextlib.suppress(OSError):
                if _JOB_ID.fullmatch(entry.name) and entry.name != current and platform.is_plain_dir(entry.lstat()):
                    folders.append((entry.stat().st_mtime, entry))
    folders.sort(reverse=True)
    for _mtime, folder in folders[JOB_FOLDERS_KEPT:]:
        try:
            # A worker older than the lease has none: only the current job can still run then.
            if (folder / "worker.lock").exists() and _lease_active(folder.name):
                continue
        except WebUpdateError:
            continue  # not as a job folder looks: left alone
        with contextlib.suppress(OSError):
            shutil.rmtree(folder)


def log_tail() -> str:
    job = _read(_job_file())
    _active(job)  # validate the stored identifier before using it in a filename
    if not job:
        return ""
    path = _job_file().parent / job["id"] / "update.log"
    if not path.exists():
        return ""
    return _tail(path, _job_file().parent)


_LOG_TAIL_BYTES = 16000
# An update started from a terminal ("Update TOW.cmd", update-tow, deploy.ps1) runs in its own
# process: scripts/update.py RUNS, OWNER_LOG, OWNER_RESULT. Its folder is named by its start in UTC.
_TERMINAL_RUNS = "update-run"
_TERMINAL_RUN = re.compile(r"(\d{8}-\d{6})-[0-9a-f]{8}")
_TERMINAL_RUNS_LOOKED_AT = 256  # update.py keeps 3 finished ones; never more names than this read


def _tail(path: Path, folder: Path) -> str:
    """The last bytes of the log ``path`` inside ``folder``, links cleaned and the install as <TOW>."""
    if not _safe_file(path) or not path.resolve().is_relative_to(folder.resolve()):
        raise WebUpdateError("releases.job_unreadable")
    try:
        with path.open("rb") as handle:
            handle.seek(max(0, path.stat().st_size - _LOG_TAIL_BYTES))
            content = handle.read(_LOG_TAIL_BYTES)
    except OSError as exc:
        raise WebUpdateError("releases.job_unreadable") from exc
    return _without_install_path(scrub_text(content.decode("utf-8", "replace")))


def terminal_run() -> dict[str, Any] | None:
    """The newest update started from a terminal: when it started (epoch seconds), its exit code
    (``scripts/update.py``: 0 done, 1 failed or rolled back, 2 refused; None while it runs or when
    it was cut off), whether it ran in its window instead (``in_job``) and the end of its log as the
    web update's is shown. Read-only, no lock; None when there is none or it cannot be read."""
    runs = runtime_dir() / _TERMINAL_RUNS
    try:
        if runs.is_symlink() or not runs.is_dir():
            return None
        names = []
        for count, entry in enumerate(runs.iterdir()):
            if count >= _TERMINAL_RUNS_LOOKED_AT:
                break
            if _TERMINAL_RUN.fullmatch(entry.name) and platform.is_plain_dir(entry.lstat()):
                names.append(entry.name)
    except OSError:
        return None
    if not names:
        return None
    folder = runs / max(names)
    try:
        started = datetime.strptime(folder.name[:15], "%Y%m%d-%H%M%S").replace(tzinfo=UTC).timestamp()
        text = _tail(folder / "update.log", runs) if (folder / "update.log").exists() else ""
    except ValueError, OverflowError, WebUpdateError:
        return None
    code, in_job = None, False
    result = folder / "result.json"
    with contextlib.suppress(OSError, ValueError, UnicodeError):
        if _safe_file(result):
            with result.open("rb") as handle:
                value = json.loads(handle.read(4097)[:4096].decode("utf-8"))
            if isinstance(value, dict) and type(value.get("code")) is int:
                code, in_job = value["code"], value.get("in_job") is True
    return {"started_at": started, "code": code, "in_job": in_job, "text": text}


def update_logs(*, local: bool = True) -> list[dict[str, Any]]:
    """The latest update of this page (``kind`` "web") and the latest one from a terminal
    ("terminal"), newest first, each with ``started_at`` and ``text`` (``log_tail``,
    ``terminal_run``). Read-only: polling it takes no lock.

    ``local``: read on this computer, which keeps the whole diagnostics. A network device gets
    the texts without a folder or file outside the install (an archive given with ``--source``
    names the account's folders); the install's own, said as ``<TOW>``, stay."""
    logs = _update_logs()
    if not local:
        for log in logs:
            log["text"] = paths_out(log["text"], kept=INSTALL_PLACEHOLDER)
    return logs


def _update_logs() -> list[dict[str, Any]]:
    job = _read(_job_file())
    _active(job)  # validate the stored identifier and dates before using them
    logs: list[dict[str, Any]] = []
    if job:
        started = job.get("started_at")
        when = started if isinstance(started, (int, float)) and not isinstance(started, bool) else None
        logs.append({"kind": "web", "started_at": when, "text": log_tail()})
    terminal = terminal_run()
    if terminal is not None:
        logs.append({"kind": "terminal", **terminal})
    return sorted(logs, key=lambda log: log.get("started_at") or 0.0, reverse=True)


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


def _receipt_pending() -> bool:
    """A torrent client still checks the files of a topic TOW handed to it."""
    from tow.records import topics_of
    from tow.store import load_state

    return any(topic.get("verification_pending") for topic in topics_of(load_state()))


def _content_ref_pending() -> bool:
    """A topic prepared when it was added waits for its first add, also one a live "Undo" would
    bring back: its prepared torrent's id is ``content_ref``, a name 1.31.2 and older never read."""
    from tow.records import topics_of
    from tow.store import load_state
    from tow.undo import can_undo

    state = load_state()
    waiting: list[Any] = list(topics_of(state))
    undo = state.get("undo")
    if isinstance(undo, dict) and isinstance(undo.get("item"), dict) and can_undo(state):
        waiting.append(undo["item"])
    return any(isinstance(topic.get("content_ref"), str) and topic["content_ref"] for topic in waiting)


def start(version: str, *, grace: float = 0.0) -> dict[str, Any]:
    """Reserve the job and hand it to a detached worker. ``grace``: seconds, at most
    ``GRACE_MAX_SEC``, that the worker keeps TOW running after it publishes "preparing" (the
    release's download and checks count), so pages open now see the job before TOW stops."""
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
        old, active, _unverified = _settled_job()
        old = _recover_completed(old, _updater_record(), active)
        if active:
            raise WebUpdateError("releases.busy")
        if old.get("status") in _ACTIVE:
            raise WebUpdateError("releases.interrupted")
        python = Path(getattr(sys, "_base_executable", sys.executable)).resolve()
        if not python.is_file() or python.is_relative_to(app / ".venv"):
            raise WebUpdateError("releases.no_python")
        if parsed < RECEIPTS_SINCE and _receipt_pending():
            raise WebUpdateError("releases.receipt_pending")
        if parsed < CONTENT_REF_SINCE and _content_ref_pending():
            raise WebUpdateError("releases.content_ref_pending")
        _prune_job_folders(str(old.get("id") or ""))
        # Archive creation is serialized with store writes and verified by export_bundle.
        try:
            safety = create_restore_point()
        except RestorePointError as exc:
            # The page names the kind of failure in fixed words; the restore point's own words (a
            # field name, a system error) go to the owner's Log only, never into an HTTP answer.
            log_event("settings_update_refused", target_version=version, reason=str(exc), how="manual")
            raise WebUpdateError(
                "releases.backup_failed_because",
                reason=_RESTORE_POINT_REASONS.get(exc.kind, "releases.backup_reason.other"),
            ) from exc
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
            if math.isfinite(grace) and grace > 0:
                job["grace"] = min(float(grace), GRACE_MAX_SEC)
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
