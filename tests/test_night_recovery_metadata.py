"""Recovery metadata cannot exhaust memory or let an unfinished restore disappear."""

from __future__ import annotations

import hashlib
import io
import json
import re
import stat
from pathlib import Path
from types import SimpleNamespace

import pytest

from tow import snapshots, store
from tow.i18n import t
from tow.paths import data_dir, state_path

MARKER_LIMIT = 64 * 1024
JOURNAL_LIMIT = 4 * snapshots.MAX_MANIFEST_BYTES


def _error(key):
    return re.escape(t(f"backup.snapshot.record_{key}", "ru"))


@pytest.fixture
def recovery():
    store.save_state({"topics": [{"id": "before"}]})
    safety = data_dir() / "before-restore-20261001-000000"
    safety.mkdir()
    content = state_path().read_bytes()
    (safety / "state.json").write_bytes(content)
    journal = safety / snapshots._JOURNAL
    journal.write_text(
        json.dumps(
            {
                "format": snapshots._JOURNAL_FORMAT,
                "status": "prepared",
                "entries": [{"name": "state.json", "existed": True, "sha256": hashlib.sha256(content).hexdigest()}],
            }
        ),
        encoding="utf-8",
    )
    marker = data_dir() / snapshots._MARKER
    marker.write_text(json.dumps({"safety": safety.name}), encoding="utf-8")
    return safety, marker, journal, content


def _inspect(surface, safety):
    if surface == "cleanup":
        return snapshots._marked_safety()
    if surface == "marker":
        return snapshots._marked_journal(data_dir() / snapshots._MARKER)
    return snapshots._read_journal(safety)


@pytest.mark.parametrize("surface", ["cleanup", "marker", "journal"])
def test_oversized_record_is_refused_before_read_or_parser(recovery, monkeypatch, surface):
    safety, marker, journal, before = recovery
    target, limit = (journal, JOURNAL_LIMIT) if surface == "journal" else (marker, MARKER_LIMIT)
    target.write_bytes(b"{}" + b" " * limit)
    real_open = Path.open
    reads = []

    class Watched:
        def __init__(self, handle):
            self.handle = handle

        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.handle.close()

        def fileno(self):
            return self.handle.fileno()

        def read(self, size=-1):
            reads.append(size)
            pytest.fail("oversized recovery metadata was read")

    def watched_open(path, *args, **kwargs):
        handle = real_open(path, *args, **kwargs)
        return Watched(handle) if path == target and args == ("rb",) else handle

    monkeypatch.setattr(Path, "open", watched_open)
    monkeypatch.setattr(snapshots, "decode_json_bytes", lambda *_a: pytest.fail("oversized metadata reached parser"))
    if surface == "cleanup":
        assert _inspect(surface, safety) == ""
    else:
        with pytest.raises(ValueError, match=_error("size")):
            _inspect(surface, safety)
    assert not reads
    assert state_path().read_bytes() == before
    assert target.stat().st_size == limit + 2


@pytest.mark.parametrize("surface", ["cleanup", "marker", "journal"])
@pytest.mark.parametrize(
    ("mode", "attributes"),
    [(stat.S_IFLNK, 0), (stat.S_IFIFO, 0), (stat.S_IFDIR, 0), (stat.S_IFREG, stat.FILE_ATTRIBUTE_REPARSE_POINT)],
)
def test_unsafe_record_types_are_refused_before_open(recovery, monkeypatch, surface, mode, attributes):
    safety, marker, journal, before = recovery
    target = journal if surface == "journal" else marker
    real_lstat, real_open = Path.lstat, Path.open
    opened = []

    def watched_lstat(path, *args, **kwargs):
        if path == target:
            return SimpleNamespace(st_mode=mode, st_file_attributes=attributes)
        return real_lstat(path, *args, **kwargs)

    def watched_open(path, *args, **kwargs):
        if path == target:
            opened.append(path)
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "lstat", watched_lstat)
    monkeypatch.setattr(Path, "open", watched_open)
    if surface == "cleanup":
        assert _inspect(surface, safety) == ""
    else:
        with pytest.raises(ValueError, match=_error("type")):
            _inspect(surface, safety)
    assert not opened
    assert state_path().read_bytes() == before


@pytest.mark.parametrize("surface", ["cleanup", "marker", "journal"])
def test_changed_open_record_is_refused_before_parsing(recovery, monkeypatch, surface):
    safety, marker, journal, before = recovery
    target = journal if surface == "journal" else marker
    original = target.read_bytes()
    real_open = Path.open

    class Changed:
        def __init__(self, handle):
            self.handle = handle
            self.content = io.BytesIO(original + b" ")

        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.handle.close()

        def fileno(self):
            return self.handle.fileno()

        def read(self, size=-1):
            assert 0 < size <= len(original) + 1
            return self.content.read(size)

    def watched_open(path, *args, **kwargs):
        handle = real_open(path, *args, **kwargs)
        return Changed(handle) if path == target and args == ("rb",) else handle

    monkeypatch.setattr(Path, "open", watched_open)
    monkeypatch.setattr(snapshots, "decode_json_bytes", lambda *_a: pytest.fail("changed metadata reached parser"))
    if surface == "cleanup":
        assert _inspect(surface, safety) == ""
    else:
        with pytest.raises(ValueError, match=_error("changed")):
            _inspect(surface, safety)
    assert state_path().read_bytes() == before


def test_broken_marker_is_not_skipped_by_recovery_discovery(recovery, monkeypatch):
    _, marker, _, before = recovery
    real_exists, real_lstat = Path.exists, Path.lstat
    monkeypatch.setattr(Path, "exists", lambda path, *a, **kw: False if path == marker else real_exists(path, *a, **kw))

    def broken(path, *args, **kwargs):
        return (
            SimpleNamespace(st_mode=stat.S_IFLNK, st_file_attributes=0)
            if path == marker
            else real_lstat(path, *args, **kwargs)
        )

    monkeypatch.setattr(Path, "lstat", broken)
    with pytest.raises(snapshots.SnapshotError, match="восстановление"), store.persistence_lock():
        pytest.fail("a broken recovery marker allowed the next writer")
    assert state_path().read_bytes() == before
    assert real_lstat(marker)


@pytest.mark.parametrize("which", ["marker", "journal"])
def test_oversized_recovery_record_keeps_all_data_and_blocks_the_next_writer(recovery, which):
    safety, marker, journal, before = recovery
    target, limit = (marker, MARKER_LIMIT) if which == "marker" else (journal, JOURNAL_LIMIT)
    target.write_bytes(b"{}" + b" " * limit)
    with pytest.raises(snapshots.SnapshotError), store.persistence_lock():
        pytest.fail("oversized recovery metadata allowed a writer")
    assert state_path().read_bytes() == before
    assert (safety / "state.json").read_bytes() == before
    assert target.stat().st_size == limit + 2
    assert marker.is_file()
    # A named in-progress safety copy is excluded from pruning without reading its journal.
    assert snapshots._prune_safety_copies(0) == ([], which == "marker")


def test_existing_journal_and_marker_are_read_without_rewriting_them(recovery):
    safety, marker, journal, _ = recovery
    before = {path: path.read_bytes() for path in (marker, journal)}
    assert snapshots._marked_safety() == safety.name
    assert snapshots._marked_journal(marker) == (safety, "prepared")
    assert snapshots._read_journal(safety)["entries"][0]["name"] == "state.json"
    assert {path: path.read_bytes() for path in before} == before


def test_missing_marker_does_not_block_a_fresh_install():
    assert snapshots._marked_safety() is None
    with store.persistence_lock():
        pass
    assert not (data_dir() / snapshots._MARKER).exists()


def test_oversized_journal_producer_preserves_previous_record(recovery):
    safety, _, journal, _ = recovery
    before = journal.read_bytes()
    value = {"format": snapshots._JOURNAL_FORMAT, "entries": [], "note": "x" * (JOURNAL_LIMIT + 1)}
    with pytest.raises(ValueError, match=_error("size")):
        snapshots._write_journal(safety, value, "committed")
    assert journal.read_bytes() == before


def test_oversized_preparation_never_publishes_marker_or_changes_live_files():
    store.save_state({"topics": [{"id": "before"}]})
    before = state_path().read_bytes()
    with pytest.raises(snapshots.SnapshotError):
        snapshots._begin_restore(
            [("state.json", state_path(), b"new")], snapshot="x" * (JOURNAL_LIMIT + 1), points_dir=""
        )
    assert state_path().read_bytes() == before
    assert not (data_dir() / snapshots._MARKER).exists()


def test_oversized_unmarked_journal_keeps_the_copy_and_reports_unknown_cleanup(recovery):
    safety, marker, journal, before = recovery
    marker.unlink()  # synthetic finished-copy inventory, not a real recovery marker
    journal.write_bytes(b"{}" + b" " * JOURNAL_LIMIT)
    assert snapshots._prune_safety_copies(0) == ([], True)
    assert (safety / "state.json").read_bytes() == before
    assert journal.stat().st_size == JOURNAL_LIMIT + 2


@pytest.mark.parametrize("surface", ["cleanup", "marker", "journal"])
def test_valid_record_at_its_exact_limit_remains_readable(recovery, surface):
    safety, marker, journal, _ = recovery
    target, limit = (journal, JOURNAL_LIMIT) if surface == "journal" else (marker, MARKER_LIMIT)
    original = target.read_bytes()
    target.write_bytes(original + b" " * (limit - len(original)))
    result = _inspect(surface, safety)
    if surface == "cleanup":
        assert result == safety.name
    elif surface == "marker":
        assert result == (safety, "prepared")
    else:
        assert result["status"] == "prepared"
    assert target.stat().st_size == limit


@pytest.mark.parametrize("surface", ["cleanup", "marker", "journal"])
def test_opened_nonregular_record_is_refused_before_reading(recovery, monkeypatch, surface):
    safety, _, _, before = recovery
    monkeypatch.setattr(snapshots.os, "fstat", lambda _fd: SimpleNamespace(st_mode=stat.S_IFIFO, st_size=0))
    if surface == "cleanup":
        assert _inspect(surface, safety) == ""
    else:
        with pytest.raises(ValueError, match=_error("type")):
            _inspect(surface, safety)
    assert state_path().read_bytes() == before


@pytest.mark.parametrize("status", ["prepared", "committed", "rolled_back"])
def test_journal_budget_covers_a_whole_maximum_supported_manifest(status):
    checksum = "0" * 64

    def name(number):
        return f"restore-points/20261001T000000Z-{number:08x}.towx"

    metadata = {"size": 0, "sha256": checksum}
    empty = {"format": snapshots.FORMAT, "signature": checksum, "files": {}}

    def encode(value):
        return json.dumps(value, separators=(",", ":")).encode("ascii")

    entry_size = len(encode({name(0): metadata})) - 1
    count = (snapshots.MAX_MANIFEST_BYTES - len(encode(empty))) // entry_size
    files = {name(number): metadata for number in range(count)}
    manifest = {**empty, "files": files}
    assert len(encode(manifest)) <= snapshots.MAX_MANIFEST_BYTES
    assert len(encode({**empty, "files": {**files, name(count): metadata}})) > snapshots.MAX_MANIFEST_BYTES
    journal = {
        "format": snapshots._JOURNAL_FORMAT,
        "snapshot": "fixture",
        "entries": [{"name": member, "existed": True, "sha256": checksum} for member in files],
    }
    raw = snapshots._journal_bytes(journal, status)
    assert snapshots.MAX_MANIFEST_BYTES < len(raw) <= JOURNAL_LIMIT
    assert json.loads(raw)["status"] == status


def test_final_phase_budget_is_checked_before_marker_publication(monkeypatch):
    store.save_state({"topics": [{"id": "before"}]})
    target = state_path()
    before = target.read_bytes()
    journal = {
        "format": snapshots._JOURNAL_FORMAT,
        "snapshot": "fixture",
        "entries": [{"name": "state.json", "existed": True, "sha256": hashlib.sha256(before).hexdigest()}],
    }
    limit = len(snapshots._journal_bytes(journal, "prepared"))
    monkeypatch.setattr(snapshots, "MAX_RESTORE_JOURNAL_BYTES", limit)
    with pytest.raises(snapshots.SnapshotError):
        snapshots._begin_restore([("state.json", target, b"new")], snapshot="fixture", points_dir="")
    assert target.read_bytes() == before
    assert not (data_dir() / snapshots._MARKER).exists()


def test_legacy_final_phase_budget_is_checked_before_rollback_writes(recovery, monkeypatch):
    safety, marker, journal, _ = recovery
    before = b'{"topics":[{"id":"current"}]}'
    state_path().write_bytes(before)
    limit = journal.stat().st_size
    monkeypatch.setattr(snapshots, "MAX_RESTORE_JOURNAL_BYTES", limit)
    with pytest.raises(snapshots.SnapshotError):
        snapshots._roll_back(safety)
    assert state_path().read_bytes() == before
    assert marker.is_file()


def test_restore_metadata_limits_do_not_cap_torrent_state(tmp_path, monkeypatch):
    from tow.config import load_config, save_config

    monkeypatch.setenv("TOW_HOME", str(tmp_path / "data"))
    cfg = load_config()
    cfg["backup_dir"] = str(tmp_path / "night")
    save_config(cfg)
    store.save_state({"topics": [], "fixture": "x" * (JOURNAL_LIMIT + 1)})
    before = state_path().read_bytes()
    copy = Path(snapshots.create_snapshot()["snapshot"])
    store.save_state({"topics": [{"id": "current"}]})
    assert snapshots.restore_snapshot(copy, apply=True)["applied"]
    assert state_path().read_bytes() == before


@pytest.mark.parametrize("marker_kind", ["MANIFEST.json", "RESTORE.json"])
def test_cleanup_proof_read_is_bounded_before_deleting_any_member(tmp_path, marker_kind):
    folder = tmp_path / "tow-20261001-000000"
    folder.mkdir()
    proof = folder / marker_kind
    limit = snapshots.MAX_MANIFEST_BYTES if marker_kind == "MANIFEST.json" else JOURNAL_LIMIT
    proof.write_bytes(b"{}" + b" " * limit)
    member = folder / "state.json"
    member.write_bytes(b"synthetic keep")
    assert snapshots._remove_copy(folder, tmp_path, marker_kind, {marker_kind, member.name}) is False
    assert member.read_bytes() == b"synthetic keep"
    assert proof.stat().st_size == limit + 2


@pytest.mark.parametrize("surface", ["cleanup", "marker", "journal"])
@pytest.mark.parametrize(
    "raw", [b"null", b"[]", b'{"x":NaN}', b"\xff", b'{"x":' + b"[" * 129 + b"0" + b"]" * 129 + b"}"]
)
def test_invalid_record_is_refused_and_not_rewritten(recovery, surface, raw):
    safety, marker, journal, before = recovery
    target = journal if surface == "journal" else marker
    target.write_bytes(raw)
    if surface == "cleanup":
        assert _inspect(surface, safety) == ""
    else:
        with pytest.raises((ValueError, TypeError), match=_error("invalid")):
            _inspect(surface, safety)
    assert target.read_bytes() == raw
    assert state_path().read_bytes() == before


@pytest.mark.parametrize("value", [float("nan"), float("inf"), [0] * 2])
def test_invalid_journal_producer_preserves_existing_record(recovery, value):
    safety, _, journal, _ = recovery
    before = journal.read_bytes()
    if isinstance(value, list):
        for _ in range(129):
            value = [value]
    with pytest.raises((ValueError, RecursionError), match=r"Out of range|nesting|recursion"):
        snapshots._write_journal(
            safety, {"format": snapshots._JOURNAL_FORMAT, "entries": [], "note": value}, "committed"
        )
    assert journal.read_bytes() == before


def test_permission_error_during_marker_discovery_never_allows_a_writer(recovery, monkeypatch):
    _, marker, _, before = recovery
    real_lstat = Path.lstat
    calls = []

    def held(path, *args, **kwargs):
        if path == marker:
            calls.append(path)
            raise PermissionError(13, "synthetic held marker")
        return real_lstat(path, *args, **kwargs)

    monkeypatch.setattr(Path, "lstat", held)
    monkeypatch.setattr(snapshots.time, "sleep", lambda _delay: None)
    with pytest.raises(snapshots.SnapshotError, match="synthetic held marker"), store.persistence_lock():
        pytest.fail("unreadable recovery marker allowed a writer")
    assert len(calls) >= snapshots._RECOVERY_ATTEMPTS + 1
    assert state_path().read_bytes() == before
    assert real_lstat(marker)


def test_real_dangling_marker_is_not_treated_as_absent(recovery, tmp_path):
    _, marker, _, before = recovery
    marker.unlink()
    try:
        marker.symlink_to(tmp_path / "missing-target")
    except OSError:
        pytest.skip("creating symlinks requires permission on this system")
    assert not marker.exists()
    assert marker.is_symlink()
    with pytest.raises(snapshots.SnapshotError), store.persistence_lock():
        pytest.fail("dangling marker allowed a writer")
    assert state_path().read_bytes() == before
    assert marker.is_symlink()
