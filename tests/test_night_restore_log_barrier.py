"""A night restore's safety copy, writes and rollback share one event-log barrier."""

from __future__ import annotations

import json
import multiprocessing
import os
import threading
import time
from contextlib import contextmanager
from pathlib import Path

import pytest
from helpers import reaped

from tow import log, snapshots, store
from tow.config import load_config, save_config
from tow.paths import data_dir, state_path
from tow.platform import locks


@pytest.fixture
def point(tmp_path, monkeypatch):
    monkeypatch.setenv("TOW_HOME", str(tmp_path / "data"))
    config = load_config()
    config["backup_dir"] = str(tmp_path / "night")
    save_config(config)
    store.save_state({"topics": [{"id": "before"}]})
    store.save_secrets({})
    assert log.log_event("synthetic_snapshot_event")
    point = Path(snapshots.create_snapshot()["snapshot"])
    store.save_state({"topics": [{"id": "current"}]})
    assert log.log_event("synthetic_before_restore")
    return point


def _append(home, max_bytes, requested, attempted, finished):
    os.environ["TOW_HOME"] = home
    log.MAX_BYTES = max_bytes
    if not requested.wait(10):
        raise AssertionError("restore never reached the controlled interleaving")
    attempted.set()
    assert log.log_event("synthetic_concurrent_restore_event")
    finished.set()


@contextmanager
def _writer(kind):
    if kind == "process":
        context = multiprocessing.get_context("spawn")
        requested, attempted, finished = (context.Event() for _ in range(3))
        worker = context.Process(target=_append, args=(str(data_dir()), log.MAX_BYTES, requested, attempted, finished))
    else:
        requested, attempted, finished = (threading.Event() for _ in range(3))
        worker = threading.Thread(target=_append, args=(str(data_dir()), log.MAX_BYTES, requested, attempted, finished))
    observed = []

    def must_wait():
        requested.set()
        assert attempted.wait(10)
        assert not finished.wait(0.05), "append/rotation escaped the restore barrier"
        observed.append(True)

    worker.start()
    try:
        yield must_wait
    finally:
        requested.set()
        if kind == "process":
            with reaped(worker):
                worker.join(timeout=10)
        else:
            worker.join(timeout=10)
    assert observed == [True]
    assert not worker.is_alive()
    if kind == "process":
        assert worker.exitcode == 0
    assert finished.is_set()
    assert any(b"synthetic_concurrent_restore_event" in path.read_bytes() for path in data_dir().glob("tow.jsonl*"))


def _assert_log_unlocked():
    with (data_dir() / ".tow-log.lock").open("a+b") as handle:
        store.init_lock_file(handle)
        deadline = time.monotonic() + 2
        # A just-released concurrent writer may own it briefly, but never this restore.
        while not (acquired := locks.lock(handle, wait=False)) and time.monotonic() < deadline:
            time.sleep(0.01)
        if acquired:
            locks.unlock(handle)
    assert acquired, "audit event or cleanup tried to re-enter the OS log lock"


@pytest.mark.parametrize("writer", ["thread", "process"])
@pytest.mark.parametrize("rotate", [False, True])
@pytest.mark.parametrize("stage", ["safety", "apply", "commit"])
def test_append_and_rotation_wait_for_the_entire_successful_restore(point, monkeypatch, writer, rotate, stage):
    before_log = log.log_path().read_bytes()
    if rotate:
        monkeypatch.setattr(log, "MAX_BYTES", 1)
    real_write, real_finish = snapshots.atomic_write_bytes, snapshots._finish
    with _writer(writer) as must_wait:

        def write(path, content):
            if stage == "safety" and path.name == "tow.jsonl" and path.parent.name.startswith("before-restore-"):
                assert content == before_log
                must_wait()
            if stage == "apply" and path == log.log_path():
                must_wait()
            return real_write(path, content)

        def finish(safety, status):
            if stage == "commit":
                must_wait()
            return real_finish(safety, status)

        monkeypatch.setattr(snapshots, "atomic_write_bytes", write)
        monkeypatch.setattr(snapshots, "_finish", finish)
        result = snapshots.restore_snapshot(point, apply=True)
    assert result["applied"]
    assert store.load_state()["topics"] == [{"id": "before"}]
    assert (Path(result["safety_copy"]) / "tow.jsonl").read_bytes() == before_log
    assert not (data_dir() / snapshots._MARKER).exists()


@pytest.mark.parametrize("writer", ["thread", "process"])
@pytest.mark.parametrize("rotate", [False, True])
@pytest.mark.parametrize("stage", ["write", "readback", "commit"])
def test_rollback_keeps_writer_waiting_and_logs_failure_after_unlock(point, monkeypatch, writer, rotate, stage):
    if rotate:
        monkeypatch.setattr(log, "MAX_BYTES", 1)
    before_state = state_path().read_bytes()
    real_write, real_finish, real_log = snapshots.atomic_write_bytes, snapshots._finish, snapshots.log_event
    failed, rollback_seen, audit = [], [], []
    with _writer(writer) as must_wait:

        def write(path, content):
            if path == log.log_path() and not failed and stage != "commit":
                failed.append(True)
                if stage == "write":
                    raise OSError(5, "synthetic I/O failure")
                return real_write(path, b"synthetic corrupt readback")
            if path == log.log_path() and failed and not rollback_seen:
                rollback_seen.append(True)
                must_wait()
            return real_write(path, content)

        def finish(safety, status):
            if status == "committed" and stage == "commit" and not failed:
                failed.append(True)
                raise OSError(5, "synthetic commit failure")
            return real_finish(safety, status)

        def event(kind, **fields):
            _assert_log_unlocked()
            audit.append((kind, fields))
            return real_log(kind, **fields)

        monkeypatch.setattr(snapshots, "atomic_write_bytes", write)
        monkeypatch.setattr(snapshots, "_finish", finish)
        monkeypatch.setattr(snapshots, "log_event", event)
        with pytest.raises(snapshots.SnapshotError):
            snapshots.restore_snapshot(point, apply=True)
    assert state_path().read_bytes() == before_state
    assert audit[-1][0] == "backup_restore_failed"
    assert audit[-1][1]["rollback"] == "done"
    assert not (data_dir() / snapshots._MARKER).exists()


@pytest.mark.parametrize("writer", ["thread", "process"])
@pytest.mark.parametrize("rotate", [False, True])
def test_crash_recovery_blocks_concurrent_writer_through_final_readback(point, monkeypatch, writer, rotate):
    real_write, real_log = snapshots.atomic_write_bytes, snapshots.log_event
    before_state = state_path().read_bytes()

    class Crash(BaseException):
        pass

    def crash(path, content):
        if path == log.log_path():
            raise Crash
        return real_write(path, content)

    monkeypatch.setattr(snapshots, "atomic_write_bytes", crash)
    with pytest.raises(Crash):
        snapshots.restore_snapshot(point, apply=True)
    assert (data_dir() / snapshots._MARKER).exists()
    if rotate:
        monkeypatch.setattr(log, "MAX_BYTES", 1)
    audits = []
    with _writer(writer) as must_wait:

        def write(path, content):
            if path == log.log_path():
                must_wait()
            return real_write(path, content)

        def event(kind, **fields):
            _assert_log_unlocked()
            audits.append(kind)
            return real_log(kind, **fields)

        monkeypatch.setattr(snapshots, "atomic_write_bytes", write)
        monkeypatch.setattr(snapshots, "log_event", event)
        with store.persistence_lock():
            pass
    assert state_path().read_bytes() == before_state
    assert "backup_restore_recovered" in audits
    assert not (data_dir() / snapshots._MARKER).exists()


def test_preview_never_takes_restore_log_barrier_or_prepares_safety_copy(point, monkeypatch):
    before = log.log_path().read_bytes()

    def refuse():
        raise AssertionError("preview entered the restore write barrier")

    monkeypatch.setattr(snapshots, "locked_log_path", refuse)
    assert not snapshots.restore_snapshot(point)["applied"]
    assert log.log_path().read_bytes() == before
    assert not list(data_dir().glob("before-restore-*"))


def test_failed_rollback_releases_log_lock_and_retains_recovery_marker(point, monkeypatch):
    real_write, real_log = snapshots.atomic_write_bytes, snapshots.log_event
    events = []

    def fail(path, content):
        if path == log.log_path():
            raise OSError(5, "synthetic repeated failure")
        return real_write(path, content)

    def event(kind, **fields):
        _assert_log_unlocked()
        events.append((kind, fields))
        return real_log(kind, **fields)

    monkeypatch.setattr(snapshots, "atomic_write_bytes", fail)
    monkeypatch.setattr(snapshots, "log_event", event)
    with pytest.raises(snapshots.SnapshotError) as failure:
        snapshots.restore_snapshot(point, apply=True)
    assert isinstance(failure.value.__cause__, OSError)
    assert (data_dir() / snapshots._MARKER).exists()
    assert events[-1][1]["rollback"] == "failed"
    _assert_log_unlocked()


def test_failed_safety_capture_releases_barrier_without_publishing_marker(point, monkeypatch):
    real_write = snapshots.atomic_write_bytes
    before = log.log_path().read_bytes()

    def fail(path, content):
        if path.parent.name.startswith("before-restore-") and path.name == "tow.jsonl":
            raise OSError(28, "synthetic full disk")
        return real_write(path, content)

    monkeypatch.setattr(snapshots, "atomic_write_bytes", fail)
    with pytest.raises(snapshots.SnapshotError):
        snapshots.restore_snapshot(point, apply=True)
    _assert_log_unlocked()
    assert log.log_path().read_bytes() == before
    assert not (data_dir() / snapshots._MARKER).exists()


@pytest.mark.parametrize("writer", ["thread", "process"])
@pytest.mark.parametrize("rotate", [False, True])
@pytest.mark.parametrize("interruption", ["crash", "failed_rollback"])
def test_first_log_writer_after_interruption_recovers_before_appending(
    point, monkeypatch, writer, rotate, interruption
):
    real_write = snapshots.atomic_write_bytes
    before_state = state_path().read_bytes()

    class Crash(BaseException):
        pass

    def fail(path, content):
        if path == log.log_path():
            if interruption == "crash":
                raise Crash
            raise OSError(5, "synthetic rollback failure")
        return real_write(path, content)

    monkeypatch.setattr(snapshots, "atomic_write_bytes", fail)
    expected = Crash if interruption == "crash" else snapshots.SnapshotError
    with pytest.raises(expected):
        snapshots.restore_snapshot(point, apply=True)
    monkeypatch.setattr(snapshots, "atomic_write_bytes", real_write)
    assert (data_dir() / snapshots._MARKER).exists()
    if rotate:
        monkeypatch.setattr(log, "MAX_BYTES", 1)
    if writer == "process":
        context = multiprocessing.get_context("spawn")
        requested, attempted, finished = (context.Event() for _ in range(3))
        worker = context.Process(target=_append, args=(str(data_dir()), log.MAX_BYTES, requested, attempted, finished))
    else:
        requested, attempted, finished = (threading.Event() for _ in range(3))
        worker = threading.Thread(target=_append, args=(str(data_dir()), log.MAX_BYTES, requested, attempted, finished))
    requested.set()
    worker.start()
    if writer == "process":
        with reaped(worker):
            worker.join(timeout=10)
        assert worker.exitcode == 0
    else:
        worker.join(timeout=10)
    assert not worker.is_alive()
    assert finished.is_set()
    # Inspect raw bytes BEFORE a store reader can inadvertently recover for the writer.
    assert not (data_dir() / snapshots._MARKER).exists()
    assert state_path().read_bytes() == before_state
    events = b"\n".join(path.read_bytes() for path in data_dir().glob("tow.jsonl*"))
    assert b"backup_restore_recovered" in events
    assert b"synthetic_concurrent_restore_event" in events
    with store.persistence_lock():
        pass
    assert b"synthetic_concurrent_restore_event" in b"\n".join(
        path.read_bytes() for path in data_dir().glob("tow.jsonl*")
    )


def test_blocked_recovery_does_not_claim_a_successful_append_or_recurse(monkeypatch):
    monkeypatch.setattr(snapshots, "_recovery_failure_logged", False)
    (data_dir() / snapshots._MARKER).write_bytes(b"{synthetic damaged marker")
    assert log.log_event("synthetic_must_not_append") is False
    assert (data_dir() / snapshots._MARKER).read_bytes() == b"{synthetic damaged marker"
    events = log.read_events()
    assert [event["kind"] for event in events] == ["backup_restore_recovery_failed"]
    assert log.log_event("synthetic_must_not_append") is False
    assert len(log.read_events()) == 1
    _assert_log_unlocked()


def test_log_reader_waits_until_replaced_bytes_are_verified(point, monkeypatch):
    requested, attempted, finished = threading.Event(), threading.Event(), threading.Event()
    real_write = snapshots.atomic_write_bytes
    observed = []

    def reader():
        assert requested.wait(10)
        attempted.set()
        observed.extend(log.read_events())
        finished.set()

    def write(path, content):
        result = real_write(path, content)
        if path == log.log_path():
            requested.set()
            assert attempted.wait(10)
            assert not finished.wait(0.05)
        return result

    monkeypatch.setattr(snapshots, "atomic_write_bytes", write)
    worker = threading.Thread(target=reader)
    worker.start()
    try:
        assert snapshots.restore_snapshot(point, apply=True)["applied"]
    finally:
        requested.set()
        worker.join(timeout=10)
    assert not worker.is_alive()
    assert finished.is_set()
    assert any(event["kind"] == "synthetic_snapshot_event" for event in observed)


def _exit_during_restore(home, point, stage):
    os.environ["TOW_HOME"] = home
    real_write = snapshots.atomic_write_bytes

    def write(path, content):
        result = real_write(path, content)
        if (
            (stage == "state" and path == state_path())
            or (stage == "log" and path == log.log_path())
            or (
                stage == "committed"
                and path.name == snapshots._JOURNAL
                and json.loads(content)["status"] == "committed"
            )
        ):
            os._exit(77)
        return result

    snapshots.atomic_write_bytes = write
    snapshots.restore_snapshot(Path(point), apply=True)
    raise AssertionError("restore did not reach the requested abrupt-exit boundary")


@pytest.mark.parametrize("stage", ["state", "log", "committed"])
def test_real_process_exit_releases_locks_and_first_event_observes_durable_outcome(point, stage):
    before_state = state_path().read_bytes()
    context = multiprocessing.get_context("spawn")
    worker = context.Process(target=_exit_during_restore, args=(str(data_dir()), str(point), stage))
    with reaped(worker):
        worker.start()
        worker.join(timeout=10)
    assert worker.exitcode == 77
    assert (data_dir() / snapshots._MARKER).exists()
    assert log.log_event("synthetic_after_abrupt_exit")
    assert not (data_dir() / snapshots._MARKER).exists()
    expected = (point / "state.json").read_bytes() if stage == "committed" else before_state
    assert state_path().read_bytes() == expected
    events = log.read_events()
    assert events[0]["kind"] == "synthetic_after_abrupt_exit"
    assert any(event["kind"] == "backup_restore_recovered" for event in events) == (stage != "committed")
