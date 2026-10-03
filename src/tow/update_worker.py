"""Detached update runner, copied outside app; standard library only, Python 3.11+.

The original updater performs installation, snapshot and rollback work. This runner
records phases and refuses a target that cannot understand the current state schema.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any


def write_job(path: Path, job: dict[str, Any]) -> None:
    temporary = path.with_name(f".{job['id']}.tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(job, handle)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def schema_preflight(work: Any, error: Any) -> None:
    if work.code.kind == "git":
        source = work.sys.git("show", f"{work.state['target']}:src/tow/store.py")
    else:
        source = (work.code.new / "src" / "tow" / "store.py").read_text(encoding="utf-8")
    match = re.search(r"^STATE_SCHEMA_VERSION\s*=\s*([0-9]+)\s*$", source, re.MULTILINE)
    if match is None:
        raise error("target state schema cannot be verified")
    state_path = work.root / "data" / "state.json"
    if state_path.exists():
        with state_path.open("rb") as handle:
            state = json.load(handle)
        schema = state.get("schema_version", 0) if isinstance(state, dict) else None
        if type(schema) is not int or schema < 0 or schema > int(match[1]):
            raise error("target version cannot read current data")


def run(app: Path, job_path: Path, job_id: str, version: str, updater: Any) -> int:
    job = json.loads(job_path.read_text(encoding="utf-8"))
    if job.get("id") != job_id or job.get("status") != "queued":
        return 2
    job.update(pid=os.getpid(), status="preparing")
    write_job(job_path, job)
    seen: dict[str, Any] = {}
    checked = False
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
            nonlocal checked
            if _label == "stopping" and not checked:
                schema_preflight(self, updater.UpdateError)
                checked = True
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


def main() -> int:
    app, job_path, job_id, version = sys.argv[1:]
    script = Path(__file__).with_name("update.py")
    spec = importlib.util.spec_from_file_location("tow_detached_updater", script)
    if spec is None or spec.loader is None:
        return 2
    updater = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = updater
    spec.loader.exec_module(updater)
    return run(Path(app), Path(job_path), job_id, version, updater)


if __name__ == "__main__":
    sys.exit(main())
