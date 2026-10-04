from __future__ import annotations

import json
import threading
import time
from types import SimpleNamespace

import pytest
from test_update_worker import _prepare_lease

from tow import update_worker, web_update
from tow.platform import locks


def _job(tmp_path, *, started_at=None):
    path = tmp_path / "job.json"
    job = {"id": "c" * 32, "status": "queued", "lease_version": 1}
    if started_at is not None:
        job["started_at"] = started_at
    lease = _prepare_lease(path, job["id"])
    path.write_text(json.dumps(job))
    return path, job, lease


def _fake_lease(monkeypatch, lock):
    elapsed = [0.0]
    sleeps = []
    unlocks = []

    def sleep(delay):
        sleeps.append(delay)
        elapsed[0] += delay

    module = SimpleNamespace(lock=lock, unlock=lambda handle: unlocks.append(handle.closed))
    spec = SimpleNamespace(loader=SimpleNamespace(exec_module=lambda module: None))
    monkeypatch.setattr(update_worker.importlib.util, "spec_from_file_location", lambda *args: spec)
    monkeypatch.setattr(update_worker.importlib.util, "module_from_spec", lambda spec: module)
    monkeypatch.setattr(
        update_worker, "time", SimpleNamespace(monotonic=lambda: elapsed[0], sleep=sleep, time=lambda: 100 + elapsed[0])
    )
    return elapsed, sleeps, unlocks


def test_transient_busy_lease_is_retried_without_a_blocking_os_lock(tmp_path, monkeypatch):
    path, job, _lease = _job(tmp_path)
    attempts = []

    def lock(handle, *, wait):
        assert not handle.closed
        attempts.append(wait)
        return len(attempts) == 3

    elapsed, sleeps, unlocks = _fake_lease(monkeypatch, lock)
    before = path.read_bytes()
    with update_worker.worker_lease(path, job) as acquired:
        assert acquired
        assert unlocks == []
    assert attempts == [False, False, False]
    assert 0 < elapsed[0] <= 1
    assert len(sleeps) == 2
    assert unlocks == [False]
    assert path.read_bytes() == before


def test_continuously_busy_lease_has_a_bounded_wait_and_never_unlocks_another_holder(tmp_path, monkeypatch):
    path, job, _lease = _job(tmp_path)
    attempts = []
    elapsed, sleeps, unlocks = _fake_lease(monkeypatch, lambda handle, *, wait: attempts.append(wait) or False)
    before = path.read_bytes()
    with update_worker.worker_lease(path, job) as acquired:
        assert not acquired
    assert elapsed[0] == pytest.approx(1)
    assert 2 <= len(attempts) <= 22
    assert all(wait is False for wait in attempts)
    assert all(0 < delay <= 0.05 for delay in sleeps)
    assert unlocks == []
    assert path.read_bytes() == before


def test_lease_io_error_is_not_retried_or_unlocked(tmp_path, monkeypatch):
    path, job, _lease = _job(tmp_path)

    def denied(handle, *, wait):
        raise PermissionError("synthetic denied lease")

    elapsed, sleeps, unlocks = _fake_lease(monkeypatch, denied)
    with pytest.raises(PermissionError), update_worker.worker_lease(path, job):
        pytest.fail("an unreadable lease cannot grant ownership")
    assert elapsed == [0]
    assert sleeps == []
    assert unlocks == []


def test_exception_in_owned_worker_still_releases_the_lease(tmp_path, monkeypatch):
    path, job, _lease = _job(tmp_path)
    _elapsed, sleeps, unlocks = _fake_lease(monkeypatch, lambda handle, *, wait: True)

    def fail_owned_worker():
        with update_worker.worker_lease(path, job) as acquired:
            assert acquired
            raise RuntimeError("synthetic worker failure")

    with pytest.raises(RuntimeError, match="synthetic worker failure"):
        fail_owned_worker()
    assert sleeps == []
    assert unlocks == [False]


def test_handoff_expiring_during_lease_retry_never_starts_the_updater(tmp_path, monkeypatch):
    path, job, _lease = _job(tmp_path, started_at=70.01)
    attempts = []
    _elapsed, sleeps, unlocks = _fake_lease(
        monkeypatch, lambda handle, *, wait: attempts.append(wait) or len(attempts) > 1
    )
    monkeypatch.setattr(update_worker, "_run", lambda *args: pytest.fail("expired handoff must not install"))
    assert update_worker.run(tmp_path, path, job["id"], "1.22.35", None) == 2
    assert len(sleeps) == 1
    assert unlocks == [False]
    result = json.loads(path.read_text())
    assert result["status"] == "failed"
    assert result["error"] == "releases.interrupted"


@pytest.mark.parametrize("replacement", [{"status": "ok"}, {"id": "d" * 32}, {"target": "1.22.21"}])
def test_lease_retry_does_not_install_or_overwrite_a_changed_job(tmp_path, monkeypatch, replacement):
    path, job, _lease = _job(tmp_path)
    attempts = []

    def lock(handle, *, wait):
        attempts.append(wait)
        if len(attempts) == 1:
            path.write_text(json.dumps({**job, **replacement}))
            return False
        return True

    _elapsed, sleeps, unlocks = _fake_lease(monkeypatch, lock)
    monkeypatch.setattr(update_worker, "_run", lambda *args: pytest.fail("changed handoff must not install"))
    assert update_worker.run(tmp_path, path, job["id"], "1.22.35", None) == 2
    assert len(sleeps) == 1
    assert unlocks == [False]
    assert json.loads(path.read_text()) == {**job, **replacement}


def test_real_status_reader_cannot_abort_the_first_worker_acquisition(tmp_path, monkeypatch):
    path, job, lease = _job(tmp_path)
    before = (path.read_bytes(), lease.read_bytes())
    reader_holds = threading.Event()
    reader_release = threading.Event()
    reader_done = threading.Event()
    results = {}
    real_lock = locks.lock

    def pause_reader(handle, **kwargs):
        acquired = real_lock(handle, **kwargs)
        if acquired:
            reader_holds.set()
            if not reader_release.wait(5):
                locks.unlock(handle)
                raise RuntimeError("synthetic reader must be released")
        return acquired

    def read_status():
        try:
            results["active"] = web_update._lease_active(job["id"])
        except (OSError, RuntimeError) as exc:
            results["error"] = exc
        finally:
            reader_done.set()

    def release_reader_on_retry(delay):
        reader_release.set()
        assert reader_done.wait(5)
        time.sleep(delay)

    monkeypatch.setattr(web_update, "_job_file", lambda: path)
    monkeypatch.setattr(locks, "lock", pause_reader)
    # The detached worker imports a separate copied locking module. Release the
    # real reader only after its first failed acquisition enters the retry path.
    monkeypatch.setattr(update_worker, "time", SimpleNamespace(monotonic=time.monotonic, sleep=release_reader_on_retry))
    reader = threading.Thread(target=read_status)
    reader.start()
    try:
        assert reader_holds.wait(5)
        with update_worker.worker_lease(path, job) as acquired:
            assert acquired
        assert reader_done.wait(5)
    finally:
        reader_release.set()
        reader.join(5)
    assert not reader.is_alive()
    assert results == {"active": False}
    assert (path.read_bytes(), lease.read_bytes()) == before


@pytest.mark.parametrize("started_at", [True, "100", 101, 70, 69])
def test_invalid_or_expired_handoff_claims_lease_only_to_publish_refusal(tmp_path, monkeypatch, started_at):
    path, job, _lease = _job(tmp_path, started_at=started_at)
    _elapsed, sleeps, unlocks = _fake_lease(monkeypatch, lambda handle, *, wait: True)
    monkeypatch.setattr(update_worker, "_run", lambda *args: pytest.fail("refused handoff must not install"))
    assert update_worker.run(tmp_path, path, job["id"], "1.22.35", None) == 2
    assert json.loads(path.read_text())["error"] == "releases.interrupted"
    assert sleeps == []
    assert unlocks == [False]


@pytest.mark.parametrize("started_at", [float("nan"), float("inf"), -float("inf")])
def test_nonfinite_record_is_preserved_without_acquiring_lease(tmp_path, monkeypatch, started_at):
    path, job, _lease = _job(tmp_path, started_at=started_at)
    before = path.read_bytes()
    _elapsed, sleeps, unlocks = _fake_lease(
        monkeypatch, lambda handle, *, wait: pytest.fail("unreadable record cannot grant ownership")
    )
    assert update_worker.run(tmp_path, path, job["id"], "1.22.35", None) == 2
    assert path.read_bytes() == before
    assert sleeps == []
    assert unlocks == []
