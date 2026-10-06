"""Detached update runner, copied outside app; standard library only, Python 3.11+.

The original updater performs installation, snapshot and rollback work (and refuses a
target that cannot read the current state). This runner records its phases.
"""

from __future__ import annotations

import contextlib
import importlib.util
import json
import math
import os
import re
import stat
import sys
import time
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

_LEASE_WAIT = 1.0
_LEASE_POLL = 0.05
_JOB_MAX_BYTES = 65536
_JSON_MAX_DEPTH = 128
_STATUSES = frozenset(
    {
        "queued",
        "preparing",
        "stopping",
        "backup",
        "installing",
        "checking",
        "rolling_back",
        "ok",
        "refused",
        "failed",
        "rolled_back",
        "recovered",
    }
)


def _finite_number(value: str) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("non-finite JSON number")
    return number


def _decode_object(raw: bytes) -> dict[str, Any]:
    # Keep in step with tow.store, without importing a replaceable Python 3.14 app.
    value = json.loads(raw.decode("utf-8"), parse_float=_finite_number, parse_constant=_finite_number)
    if not isinstance(value, dict):
        raise TypeError("record is not an object")
    stack = [(value, 0)]
    while stack:
        item, depth = stack.pop()
        if depth > _JSON_MAX_DEPTH:
            raise ValueError("record nesting exceeds limit")
        if isinstance(item, dict):
            stack.extend((child, depth + 1) for child in item.values())
        elif isinstance(item, list):
            stack.extend((child, depth + 1) for child in item)
    return value


def _check_job(job: dict[str, Any]) -> None:
    identifier = job.get("id")
    status = job.get("status")
    if not isinstance(identifier, str) or re.fullmatch(r"[A-Za-z0-9_-]{1,64}", identifier) is None:
        raise ValueError("invalid job identifier")
    if not isinstance(status, str) or status not in _STATUSES:
        raise ValueError("invalid job status")


def _read_job(path: Path) -> dict[str, Any]:
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT:
        raise ValueError("unsafe job record")
    with path.open("rb") as handle:
        raw = handle.read(_JOB_MAX_BYTES + 1)
    if len(raw) > _JOB_MAX_BYTES:
        raise ValueError("job record exceeds size limit")
    job = _decode_object(raw)
    _check_job(job)
    return job


def write_job(path: Path, job: dict[str, Any]) -> None:
    _check_job(job)
    raw = json.dumps(job, allow_nan=False).encode("utf-8")
    if len(raw) > _JOB_MAX_BYTES:
        raise ValueError("job record exceeds size limit")
    _decode_object(raw)
    temporary = path.with_name(f".{job['id']}-{uuid.uuid4().hex}.tmp")
    handle = temporary.open("xb")
    try:
        with handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        with contextlib.suppress(OSError):
            temporary.unlink()


def _handoff_expired(job: dict[str, Any]) -> bool:
    started = job.get("started_at")
    if started is None:
        return False  # compatible with pre-expiry reservations already on disk
    if type(started) not in {int, float}:
        return True
    try:
        return not math.isfinite(float(started)) or not 0 <= time.time() - started < 30
    except OverflowError:
        return True


def run(app: Path, job_path: Path, job_id: str, version: str, updater: Any) -> int:
    try:
        job = _read_job(job_path)
        if job.get("id") != job_id or job.get("status") != "queued":
            return 2
        if "target" in job and job["target"] != version:
            return refuse_handoff(job_path, job_id, "releases.job_unreadable")
        if _handoff_expired(job):
            return refuse_handoff(job_path, job_id, "releases.interrupted")
        with worker_lease(job_path, job) as acquired:
            if not acquired:
                return 2
            # A second worker may have completed this job before this one took the lease.
            latest = _read_job(job_path)
            if latest.get("id") != job_id or latest.get("status") != "queued" or latest != job:
                return 2
            if _handoff_expired(latest):
                return _record_refusal(job_path, latest, "releases.interrupted")
            return _run(app, job_path, job, version, updater)
    except (OSError, ValueError, TypeError, RecursionError, ImportError, SyntaxError):
        return refuse_handoff(job_path, job_id, "releases.job_unreadable")


@contextlib.contextmanager
def worker_lease(path: Path, job: dict[str, Any]) -> Iterator[bool]:
    if "lease_version" not in job:
        yield True  # compatible with pre-lease handoffs already on disk
        return
    if type(job["lease_version"]) is not int or job["lease_version"] != 1:
        raise ValueError("unsupported worker lease")
    if not isinstance(job.get("id"), str) or re.fullmatch(r"[a-f0-9]{32}", job["id"]) is None:
        raise ValueError("invalid worker lease identifier")
    folder = path.parent / job["id"]
    info = folder.lstat()
    if (
        not stat.S_ISDIR(info.st_mode)
        or getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
        or folder.resolve() != path.parent.resolve() / job["id"]
    ):
        raise ValueError("unsafe worker lease folder")
    for member in ("worker.lock", "locks.py"):
        info = (folder / member).lstat()
        if not stat.S_ISREG(info.st_mode) or getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT:
            raise ValueError("unsafe worker lease member")
    if (folder / "worker.lock").stat().st_size != 1:
        raise ValueError("invalid worker lease file")
    spec = importlib.util.spec_from_file_location("tow_detached_locks", folder / "locks.py")
    if spec is None or spec.loader is None:
        raise ImportError("worker lease module unavailable")
    locks = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(locks)
    with (folder / "worker.lock").open("r+b") as handle:
        deadline = time.monotonic() + _LEASE_WAIT
        acquired = locks.lock(handle, wait=False)
        while not acquired:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            # A status read briefly takes this same byte. Do not abandon the
            # handoff for that probe, or block indefinitely behind a real worker.
            time.sleep(min(_LEASE_POLL, remaining))
            acquired = locks.lock(handle, wait=False)
        try:
            yield acquired
        finally:
            if acquired:
                locks.unlock(handle)


def _run(app: Path, job_path: Path, job: dict[str, Any], version: str, updater: Any) -> int:
    job.update(pid=os.getpid(), status="preparing")
    write_job(job_path, job)
    seen: dict[str, Any] = {}
    rolling_back = False

    def phase(name: str) -> None:
        nonlocal rolling_back
        if name == "rolling_back":
            rolling_back = True
        job["status"] = name
        try:
            write_job(job_path, job)
        except OSError:
            if not rolling_back:
                raise
            print("rollback continues despite unavailable progress journal", flush=True)

    original_state = updater.Update.write_state

    def record(work: Any, **fields: Any) -> None:
        original_state(work, **fields)
        seen.update(fields)

    updater.Update.write_state = record
    for cls, method, label in (
        (updater.Update, "stop", "stopping"),
        (updater.Update, "take_snapshot", "backup"),
        (updater.System, "uv_sync", "installing"),
        (updater.Update, "start_and_check", "checking"),
        (updater.Update, "roll_back", "rolling_back"),
    ):
        original = getattr(cls, method)

        def wrapped(self: Any, *args: Any, _original: Any = original, _label: str = label, **kwargs: Any) -> Any:
            phase(_label)
            return _original(self, *args, **kwargs)

        setattr(cls, method, wrapped)
    try:
        code = int(updater.update(f"v{version}", system=updater.System(app)))
    except Exception as exc:  # noqa: BLE001 - record unexpected runner failures without claiming success
        print(f"update worker failed: {type(exc).__name__}", flush=True)
        code = 1
    result = "ok" if code == 0 else ("refused" if code == 2 else str(seen.get("status") or "failed"))
    if result not in {"ok", "refused", "failed", "rolled_back"} or (code != 0 and result == "ok"):
        result = "failed"
    job.update(status=result, finished_at=time.time(), error="" if code == 0 else "releases.update_failed")
    write_job(job_path, job)
    return code


def _utf8_output() -> None:
    # Isolated Python ignores PYTHONIOENCODING. Every worker mode writes the same
    # UTF-8 log, even when Windows initially gives a redirected stream an ANSI codec.
    for stream in (sys.stdout, sys.stderr):
        if stream is not None and hasattr(stream, "reconfigure"):
            # An unavailable diagnostic stream must not prevent recovery.
            with contextlib.suppress(AttributeError, OSError, ValueError):
                stream.reconfigure(encoding="utf-8", errors="replace")


def main() -> int:
    _utf8_output()
    args = sys.argv[1:]
    mode = args.pop(0) if args and args[0] in {"--handoff", "--after-parent", "--broker-child"} else ""
    try:
        parent = int(args.pop(0)) if mode in {"--after-parent", "--broker-child"} else 0
    except (ValueError, IndexError):
        return 2
    if len(args) != 4 or (mode in {"--after-parent", "--broker-child"} and not 0 < parent <= 0xFFFFFFFF):
        return 2
    _app, job_path, job_id, _version = args
    folder = Path(__file__).resolve().parent
    record = folder.parent / "job.json"
    if folder.name != job_id:
        return 2
    try:
        if os.path.normcase(os.path.abspath(job_path)) != os.path.normcase(str(record)):
            return 2
    except (OSError, ValueError, RuntimeError):
        return 2
    if mode == "--broker-child":
        try:
            job = _read_job(record)
        except (OSError, ValueError, TypeError, RecursionError):
            return refuse_handoff(record, job_id, "releases.job_unreadable")
        if job.get("id") != job_id or job.get("status") != "queued":
            return 2
        with (
            (folder / "update.log").open("a", encoding="utf-8") as output,
            contextlib.redirect_stdout(output),
            contextlib.redirect_stderr(output),
        ):
            return handoff(mode, parent, _app, record, folder.name, _version)
    return handoff(mode, parent, _app, record, folder.name, _version)


def handoff(mode: str, parent: int, app: str, job_path: Path, job_id: str, version: str) -> int:
    script = Path(__file__).with_name("update.py")
    spec = importlib.util.spec_from_file_location("tow_detached_updater", script)
    if spec is None or spec.loader is None:
        return 2
    updater = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = updater
    spec.loader.exec_module(updater)
    machine = updater.System(Path(app))
    if mode == "--handoff":
        independent = machine.updater_independent()
        python = str(Path(getattr(sys, "_base_executable", sys.executable)).resolve())
        child = [
            python,
            "-I",
            "-S",
            "-u",
            str(Path(__file__).resolve()),
            "--after-parent" if independent else "--broker-child",
            str(os.getpid()),
            app,
            str(job_path),
            job_id,
            version,
        ]
        try:
            if independent:
                machine.spawn_handoff(child, job_path.parent / job_id / "update.log")
            else:
                print("handoff: inherited job; requesting local Windows broker", flush=True)
                machine.spawn_broker(child)
        except OSError:
            return refuse_handoff(
                job_path, job_id, "releases.launch_failed" if independent else "releases.broker_failed"
            )
        return 0
    if mode in {"--after-parent", "--broker-child"}:
        if not machine.updater_independent():
            return refuse_handoff(job_path, job_id, "releases.inherited_job")
        if not machine.wait_process_exit(parent, 10.0):
            return refuse_handoff(job_path, job_id, "releases.parent_wait_failed")
    return run(Path(app), job_path, job_id, version, updater)


def refuse_handoff(path: Path, job_id: str, reason: str = "releases.launch_failed") -> int:
    try:
        job = _read_job(path)
        if job.get("id") == job_id and job.get("status") == "queued":
            with worker_lease(path, job) as acquired:
                if acquired:
                    return _record_refusal(path, job, reason)
    except (OSError, ValueError, TypeError, RecursionError, ImportError, SyntaxError):
        # A damaged record is evidence, not permission to reconstruct or overwrite it.
        print("handoff refused: releases.job_unreadable", flush=True)
    return 2


def _record_refusal(path: Path, job: dict[str, Any], reason: str) -> int:
    # Called only with the worker lease (or a compatible pre-lease reservation).
    # Taking ownership is not permission to replace a changed record.
    if _read_job(path) == job:
        print(f"handoff refused: {reason}", flush=True)
        job.update(status="failed", error=reason, finished_at=time.time())
        write_job(path, job)
    return 2


if __name__ == "__main__":
    sys.exit(main())
