"""Night-copy creation must be bounded and keep old copies on every I/O refusal."""

from __future__ import annotations

import hashlib
import io
import stat
import threading
import tracemalloc
from pathlib import Path
from types import SimpleNamespace

import pytest

from tow import snapshots, store
from tow.config import load_config, save_config
from tow.paths import data_dir, state_path


@pytest.fixture
def night(tmp_path, monkeypatch):
    monkeypatch.setenv("TOW_HOME", str(tmp_path / "data"))
    cfg = load_config()
    cfg.update(backup_dir=str(tmp_path / "night"), backup_keep=1)
    save_config(cfg)
    store.save_state({"topics": [{"id": "synthetic"}], "mirrors": {}})
    store.save_secrets({"synthetic": "test-only"})
    old = Path(snapshots.create_snapshot()["snapshot"])
    return tmp_path / "night", old


def test_creation_never_reads_a_source_as_whole_bytes(night, monkeypatch):
    root, old = night
    source = data_dir() / "tow.jsonl"
    size = 3 * snapshots._READ_CHUNK_BYTES + 37
    source.write_bytes(b"x" * size)
    real_read = Path.read_bytes

    def no_whole_read(path):
        if path == source:
            pytest.fail("night creation still allocates an entire opaque file")
        return real_read(path)

    monkeypatch.setattr(Path, "read_bytes", no_whole_read)
    result = snapshots.create_snapshot()
    new = Path(result["snapshot"])
    assert snapshots.verify_snapshot(new)["files"]["tow.jsonl"]["size"] == size
    assert list(root.iterdir()) == [new]
    assert not old.exists()


@pytest.mark.parametrize("size", [0, 1, 1024 * 1024 - 1, 1024 * 1024, 1024 * 1024 + 1, 3 * 1024 * 1024 + 37])
def test_copy_hashes_exact_written_blocks_with_bounded_reads(tmp_path, monkeypatch, size):
    source = tmp_path / "source"
    destination = tmp_path / "prepared" / "copy"
    content = b"x" * size
    source.write_bytes(content)
    real_open = Path.open
    reads = []

    class Reader:
        def __init__(self, handle):
            self.handle = handle

        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.handle.close()

        def fileno(self):
            return self.handle.fileno()

        def read(self, length):
            assert 0 < length <= snapshots._READ_CHUNK_BYTES
            reads.append(length)
            return self.handle.read(length)

    def opened(path, *args, **kwargs):
        handle = real_open(path, *args, **kwargs)
        return Reader(handle) if path == source and args == ("rb",) else handle

    monkeypatch.setattr(Path, "open", opened)
    assert snapshots._copy_snapshot_member(source, destination, "tow.jsonl") == {
        "size": size,
        "sha256": hashlib.sha256(content).hexdigest(),
    }
    assert destination.read_bytes() == content
    assert len(reads) == (size + snapshots._READ_CHUNK_BYTES - 1) // snapshots._READ_CHUNK_BYTES + 1


def test_absent_source_creates_no_destination(tmp_path):
    destination = tmp_path / "not-created" / "copy"
    assert snapshots._copy_snapshot_member(tmp_path / "absent", destination, "tow.jsonl") is None
    assert not destination.parent.exists()


def test_exclusive_destination_never_overwrites_an_existing_file(tmp_path):
    source, destination = tmp_path / "source", tmp_path / "existing"
    source.write_bytes(b"synthetic source")
    destination.write_bytes(b"keep existing")
    with pytest.raises(FileExistsError):
        snapshots._copy_snapshot_member(source, destination, "tow.jsonl")
    assert destination.read_bytes() == b"keep existing"


@pytest.mark.parametrize(
    "kind", [stat.S_IFDIR, stat.S_IFLNK, stat.S_IFIFO, stat.S_IFSOCK, stat.S_IFCHR, stat.S_IFBLK, "reparse"]
)
def test_special_or_linked_source_is_rejected_before_open(tmp_path, monkeypatch, kind):
    source, destination = tmp_path / "source", tmp_path / "copy"
    source.write_bytes(b"synthetic")
    real_lstat, real_open = Path.lstat, Path.open
    info = SimpleNamespace(
        st_mode=stat.S_IFREG if kind == "reparse" else kind,
        st_file_attributes=stat.FILE_ATTRIBUTE_REPARSE_POINT if kind == "reparse" else 0,
    )
    monkeypatch.setattr(Path, "lstat", lambda path: info if path == source else real_lstat(path))

    def no_source_open(path, *args, **kwargs):
        assert path != source
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", no_source_open)
    with pytest.raises(snapshots.SnapshotError):
        snapshots._copy_snapshot_member(source, destination, "tow.jsonl")
    assert not destination.exists()


@pytest.mark.parametrize(
    ("mode", "size"),
    [(stat.S_IFIFO, 1), (stat.S_IFDIR, 1), (stat.S_IFREG, -1), (stat.S_IFREG, True), (stat.S_IFREG, 2**63)],
)
def test_opened_descriptor_is_checked_before_read_or_destination(tmp_path, monkeypatch, mode, size):
    source, destination = tmp_path / "source", tmp_path / "prepared" / "copy"
    source.write_bytes(b"synthetic")
    monkeypatch.setattr(snapshots.os, "fstat", lambda fd: SimpleNamespace(st_mode=mode, st_size=size))
    with pytest.raises(snapshots.SnapshotError):
        snapshots._copy_snapshot_member(source, destination, "tow.jsonl")
    assert not destination.parent.exists()


def _old_kept(night):
    root, old = night
    assert list(root.iterdir()) == [old]
    assert snapshots.verify_snapshot(old)["signed"]
    assert snapshots.status()["last_snapshot"] == old.name


@pytest.mark.parametrize("case", ["grow", "shrink", "mtime", "read-error", "disappear"])
def test_changed_or_failed_source_preserves_the_older_copy(night, monkeypatch, case):
    source = state_path()
    content = source.read_bytes()
    real_open = Path.open

    class Reader:
        def __init__(self, handle):
            self.handle = handle
            self.data = io.BytesIO(content + b"x" if case == "grow" else content[:-1] if case == "shrink" else content)

        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.handle.close()

        def fileno(self):
            return self.handle.fileno()

        def read(self, length):
            if case == "read-error":
                raise OSError(5, "synthetic read error")
            if case == "mtime":
                info = source.stat()
                snapshots.os.utime(source, ns=(info.st_atime_ns, info.st_mtime_ns + 2_000_000))
            return self.data.read(length)

    def opened(path, *args, **kwargs):
        if path == source and args == ("rb",):
            if case == "disappear":
                raise FileNotFoundError("synthetic disappearance after lstat")
            return Reader(real_open(path, *args, **kwargs))
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", opened)
    with pytest.raises(snapshots.SnapshotError):
        snapshots.create_snapshot()
    assert source.read_bytes() == content
    _old_kept(night)


@pytest.mark.parametrize("name", ["state.json", "MANIFEST.json"])
@pytest.mark.parametrize(
    "case", ["short", "zero", "none", "write-error", "flush-error", "sync-error", "close-error", "open-error"]
)
def test_destination_failure_never_publishes_or_prunes(night, monkeypatch, name, case):
    root, _old = night
    real_open, real_sync = Path.open, snapshots.os.fsync
    active = set()

    class Writer:
        def __init__(self, handle):
            self.handle = handle
            active.add(handle.fileno())

        def __enter__(self):
            return self

        def __exit__(self, *args):
            active.discard(self.handle.fileno())
            self.handle.close()
            if case == "close-error":
                raise OSError(5, "synthetic close error")

        def fileno(self):
            return self.handle.fileno()

        def write(self, content):
            if case == "write-error":
                raise OSError(28, "synthetic disk full")
            if case in {"zero", "none"}:
                return 0 if case == "zero" else None
            if case == "short":
                self.handle.write(content[:-1])
                return len(content) - 1
            return self.handle.write(content)

        def flush(self):
            if case == "flush-error":
                raise PermissionError(13, "synthetic held destination")
            self.handle.flush()

    def opened(path, *args, **kwargs):
        if path.name == name and path.is_relative_to(root) and args == ("xb",):
            if case == "open-error":
                raise PermissionError(13, "synthetic held destination")
            return Writer(real_open(path, *args, **kwargs))
        return real_open(path, *args, **kwargs)

    def sync(fd):
        if case == "sync-error" and fd in active:
            raise OSError(28, "synthetic sync disk full")
        real_sync(fd)

    monkeypatch.setattr(Path, "open", opened)
    monkeypatch.setattr(snapshots.os, "fsync", sync)
    with pytest.raises(snapshots.SnapshotError):
        snapshots.create_snapshot()
    _old_kept(night)


def test_copy_and_manifest_are_synced_before_publication(night, monkeypatch):
    real_open, real_sync, real_rename = Path.open, snapshots.os.fsync, snapshots._rename_with_retry
    paths, synced = {}, set()

    def opened(path, *args, **kwargs):
        handle = real_open(path, *args, **kwargs)
        if args == ("xb",):
            paths[handle.fileno()] = path.name
        return handle

    def sync(fd):
        if fd in paths:
            synced.add(paths[fd])
        real_sync(fd)

    def publish(source, target):
        members = {p.name for p in source.rglob("*") if p.is_file()}
        assert members <= synced
        assert "MANIFEST.json" in synced
        return real_rename(source, target)

    monkeypatch.setattr(Path, "open", opened)
    monkeypatch.setattr(snapshots.os, "fsync", sync)
    monkeypatch.setattr(snapshots, "_rename_with_retry", publish)
    assert snapshots.create_snapshot()["ok"]


@pytest.mark.parametrize("rotate", [False, True])
def test_log_append_and_rotation_wait_until_snapshot_read_finishes(night, monkeypatch, rotate):
    import tow.log
    from tow.log import log_event, log_path

    if rotate:
        monkeypatch.setattr(tow.log, "MAX_BYTES", 1)
    entered, attempted, finished = threading.Event(), threading.Event(), threading.Event()
    real_copy = snapshots._copy_snapshot_member
    failures = []

    def append():
        try:
            assert entered.wait(2)
            attempted.set()
            log_event("synthetic_concurrent_append")
            finished.set()
        except (AssertionError, OSError) as exc:
            failures.append(exc)

    def copied(source, destination, name):
        if name == "tow.jsonl":
            assert source == log_path()
            entered.set()
            assert attempted.wait(2)
            assert not finished.wait(0.05)
        return real_copy(source, destination, name)

    monkeypatch.setattr(snapshots, "_copy_snapshot_member", copied)
    worker = threading.Thread(target=append)
    worker.start()
    try:
        result = snapshots.create_snapshot()
    finally:
        entered.set()
        worker.join(timeout=2)
    assert not worker.is_alive()
    assert not failures
    assert finished.is_set()
    copied_log = (Path(result["snapshot"]) / "tow.jsonl").read_bytes()
    assert b"synthetic_concurrent_append" not in copied_log
    assert any(b"synthetic_concurrent_append" in path.read_bytes() for path in data_dir().glob("tow.jsonl*"))


def test_large_opaque_copy_has_no_whole_file_python_allocation(tmp_path):
    source, destination = tmp_path / "source", tmp_path / "prepared" / "copy"
    block = b"x" * snapshots._READ_CHUNK_BYTES
    with source.open("wb") as handle:
        for _ in range(16):
            handle.write(block)
    snapshots.owner_language()  # language/config initialization is not this copy's file buffer
    tracemalloc.start()
    try:
        result = snapshots._copy_snapshot_member(source, destination, "tow.jsonl")
        _current, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert result["size"] == 16 * snapshots._READ_CHUNK_BYTES
    assert peak < 4 * snapshots._READ_CHUNK_BYTES


def test_oversized_config_is_rejected_before_destination_creation(tmp_path, monkeypatch):
    source, destination = tmp_path / "source", tmp_path / "prepared" / "copy"
    source.write_bytes(b"synthetic oversized config")
    monkeypatch.setattr(snapshots, "MAX_INPUT_BYTES", 8)
    with pytest.raises(snapshots.SnapshotError):
        snapshots._copy_snapshot_member(source, destination, "config.yaml")
    assert not destination.parent.exists()


def test_corrupt_successful_write_is_caught_by_real_readback_before_retention(night, monkeypatch):
    root, old = night
    real_open = Path.open

    class CorruptWriter:
        def __init__(self, handle):
            self.handle = handle

        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.handle.close()

        def __getattr__(self, name):
            return getattr(self.handle, name)

        def write(self, content):
            self.handle.write(b"!" + content[1:])
            return len(content)

    def opened(path, *args, **kwargs):
        handle = real_open(path, *args, **kwargs)
        if path.name == "state.json" and path.is_relative_to(root) and args == ("xb",):
            return CorruptWriter(handle)
        return handle

    monkeypatch.setattr(Path, "open", opened)
    with pytest.raises(snapshots.SnapshotError):
        snapshots.create_snapshot()
    assert old in list(root.iterdir())
    assert snapshots.verify_snapshot(old)["signed"]
    assert snapshots.status()["last_snapshot"] == old.name
    assert not list(root.glob(".*.partial"))
