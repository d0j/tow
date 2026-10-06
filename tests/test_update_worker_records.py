"""The copied 3.11 worker must fail closed without the replaceable application."""

from __future__ import annotations

import contextlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_update_worker import _prepare_lease

from tow import update_worker
from tow.platform import locks

JOB_ID = "b" * 32
VERSION = "1.22.21"
JOB = {"id": JOB_ID, "status": "queued", "target": VERSION}
INVALID_RECORDS = [
    b"not-json",
    b"[]",
    b"null",
    b"true",
    b"{}",
    b"\xff",
    b'{"id":"' + JOB_ID.encode() + b'","status":"queued","extra":NaN}',
    b'{"id":"' + JOB_ID.encode() + b'","status":"queued","extra":Infinity}',
    b'{"id":"' + JOB_ID.encode() + b'","status":"queued","extra":1e9999}',
    json.dumps({**JOB, "extra": "x" * 65536}).encode(),
    b'{"id":"' + JOB_ID.encode() + b'","status":"queued","extra":' + b"[" * 2000 + b"0" + b"]" * 2000 + b"}",
    b'{"id":"' + JOB_ID.encode() + b'","status":"queued","extra":' + b"[" * 129 + b"0" + b"]" * 129 + b"}",
]


@pytest.mark.parametrize("content", INVALID_RECORDS, ids=[f"record-{i}" for i in range(len(INVALID_RECORDS))])
@pytest.mark.parametrize("entry", ["run", "refuse", "broker"])
def test_unreadable_records_never_install_rewrite_or_escape(tmp_path, monkeypatch, content, entry):
    path = tmp_path / "job.json"
    path.write_bytes(content)
    monkeypatch.setattr(update_worker, "_run", lambda *args: pytest.fail("unreadable job must not install"))
    if entry == "run":
        result = update_worker.run(tmp_path / "app", path, JOB_ID, VERSION, None)
    elif entry == "refuse":
        result = update_worker.refuse_handoff(path, JOB_ID)
    else:
        folder = tmp_path / JOB_ID
        folder.mkdir()
        monkeypatch.setattr(update_worker, "__file__", str(folder / "worker.py"))
        monkeypatch.setattr(update_worker, "handoff", lambda *args: pytest.fail("unreadable broker must not relay"))
        monkeypatch.setattr(
            update_worker.sys,
            "argv",
            ["worker.py", "--broker-child", "42", str(tmp_path / "app"), str(path), JOB_ID, VERSION],
        )
        result = update_worker.main()
        assert not (folder / "update.log").exists()
    assert result == 2
    assert path.read_bytes() == content


@pytest.mark.parametrize("entry", ["run", "refuse"])
@pytest.mark.parametrize("failure", ["missing", "denied"])
def test_io_failure_does_not_install_or_create_a_replacement(tmp_path, monkeypatch, entry, failure):
    path = tmp_path / "job.json"
    if failure == "denied":
        path.write_text(json.dumps(JOB))
        original = Path.open

        def denied(target, *args, **kwargs):
            if target == path:
                raise PermissionError("synthetic private detail")
            return original(target, *args, **kwargs)

        monkeypatch.setattr(Path, "open", denied)
    monkeypatch.setattr(update_worker, "_run", lambda *args: pytest.fail("unreadable job must not install"))
    if entry == "run":
        result = update_worker.run(tmp_path / "app", path, JOB_ID, VERSION, None)
    else:
        result = update_worker.refuse_handoff(path, JOB_ID)
    assert result == 2
    assert path.exists() is (failure == "denied")


@pytest.mark.parametrize(
    "value", [float("nan"), float("inf"), -float("inf"), "x" * 65536], ids=["nan", "inf", "negative-inf", "size"]
)
def test_invalid_writes_leave_the_previous_record_and_no_partial_file(tmp_path, value):
    path = tmp_path / "job.json"
    before = json.dumps(JOB).encode()
    path.write_bytes(before)
    members = set(tmp_path.iterdir())
    with pytest.raises(ValueError, match=r"(compliant|exceeds size limit)"):
        update_worker.write_job(path, {**JOB, "extra": value})
    assert path.read_bytes() == before
    assert set(tmp_path.iterdir()) == members


@pytest.mark.parametrize("identifier", ["../other", "a/b", "a\\b", "", "..", True, None])
def test_invalid_writer_identifier_never_selects_a_temporary_path(tmp_path, identifier):
    path = tmp_path / "job.json"
    before = json.dumps(JOB).encode()
    path.write_bytes(before)
    members = set(tmp_path.iterdir())
    with pytest.raises(ValueError, match="invalid job identifier"):
        update_worker.write_job(path, {**JOB, "id": identifier})
    assert path.read_bytes() == before
    assert set(tmp_path.iterdir()) == members


@pytest.mark.parametrize("entry", ["run", "refuse"])
def test_refusal_without_lease_cannot_replace_an_owned_queued_job(tmp_path, monkeypatch, entry):
    path = tmp_path / "job.json"
    lease = _prepare_lease(path, JOB_ID)
    before = json.dumps({**JOB, "lease_version": 1, "started_at": 0}).encode()
    path.write_bytes(before)
    monkeypatch.setattr(update_worker, "_run", lambda *args: pytest.fail("second worker must not install"))
    with lease.open("r+b") as handle:
        assert locks.lock(handle, wait=False)
        try:
            if entry == "run":
                result = update_worker.run(tmp_path / "app", path, JOB_ID, VERSION, None)
            else:
                result = update_worker.refuse_handoff(path, JOB_ID)
            assert result == 2
            assert path.read_bytes() == before
        finally:
            locks.unlock(handle)


def test_refusal_rechecks_the_unchanged_record_after_lease_acquisition(tmp_path, monkeypatch):
    path = tmp_path / "job.json"
    path.write_text(json.dumps({**JOB, "lease_version": 1}))
    replacement = json.dumps({**JOB, "lease_version": 1, "target": "1.22.22"}).encode()

    @contextlib.contextmanager
    def replace_then_acquire(*args):
        path.write_bytes(replacement)
        yield True

    monkeypatch.setattr(update_worker, "worker_lease", replace_then_acquire)
    assert update_worker.refuse_handoff(path, JOB_ID) == 2
    assert path.read_bytes() == replacement


def test_launch_target_cannot_disagree_with_the_reserved_job(tmp_path, monkeypatch):
    path = tmp_path / "job.json"
    path.write_text(json.dumps({**JOB, "target": "1.22.22"}))
    monkeypatch.setattr(update_worker, "_run", lambda *args: pytest.fail("different target must not install"))
    assert update_worker.run(tmp_path / "app", path, JOB_ID, VERSION, None) == 2
    result = json.loads(path.read_bytes())
    assert result["status"] == "failed"
    assert result["target"] == "1.22.22"
    assert result["error"] == "releases.job_unreadable"


def test_huge_handoff_timestamp_does_not_overflow_or_install(tmp_path, monkeypatch):
    path = tmp_path / "job.json"
    path.write_text(json.dumps({**JOB, "started_at": 10**1000}))
    monkeypatch.setattr(update_worker, "_run", lambda *args: pytest.fail("invalid date must not install"))
    assert update_worker.run(tmp_path / "app", path, JOB_ID, VERSION, None) == 2
    assert json.loads(path.read_bytes())["error"] == "releases.interrupted"


@pytest.mark.parametrize("method", ["fsync", "replace"])
def test_failed_atomic_publish_keeps_previous_record_and_removes_only_its_temp(tmp_path, monkeypatch, method):
    path = tmp_path / "job.json"
    before = json.dumps(JOB).encode()
    path.write_bytes(before)
    unrelated = tmp_path / f".{JOB_ID}.tmp"
    unrelated.write_bytes(b"unrelated pre-existing record")
    members = set(tmp_path.iterdir())

    def fail(*args):
        raise OSError("synthetic private detail")

    monkeypatch.setattr(update_worker.os, method, fail)
    with pytest.raises(OSError, match="synthetic private detail"):
        update_worker.write_job(path, {**JOB, "status": "preparing"})
    assert path.read_bytes() == before
    assert unrelated.read_bytes() == b"unrelated pre-existing record"
    assert set(tmp_path.iterdir()) == members


def test_exclusive_temp_collision_does_not_delete_someone_elses_file(tmp_path, monkeypatch):
    path = tmp_path / "job.json"
    before = json.dumps(JOB).encode()
    path.write_bytes(before)
    nonce = "f" * 32
    unrelated = tmp_path / f".{JOB_ID}-{nonce}.tmp"
    unrelated.write_bytes(b"occupied temporary name")
    monkeypatch.setattr(update_worker.uuid, "uuid4", lambda: SimpleNamespace(hex=nonce))
    with pytest.raises(FileExistsError):
        update_worker.write_job(path, {**JOB, "status": "preparing"})
    assert path.read_bytes() == before
    assert unrelated.read_bytes() == b"occupied temporary name"


@pytest.mark.parametrize("depth", [1, 127, 128, 129, 1000])
def test_detached_json_depth_contract_matches_the_live_store(depth):
    from tow.store import JSON_MAX_DEPTH, decode_json_bytes

    assert update_worker._JSON_MAX_DEPTH == JSON_MAX_DEPTH
    raw = b'{"value":' + b"[" * (depth - 1) + b"0" + b"]" * (depth - 1) + b"}"
    if depth <= 128:
        assert update_worker._decode_object(raw) == decode_json_bytes(raw)
    else:
        with pytest.raises((ValueError, RecursionError)):
            update_worker._decode_object(raw)
        with pytest.raises((ValueError, RecursionError)):
            decode_json_bytes(raw)


def test_read_is_bounded_before_decoding_not_a_stat_size_claim(tmp_path, monkeypatch):
    import io

    path = tmp_path / "job.json"
    path.write_text(json.dumps(JOB))
    original = Path.open
    requests = []

    class GrowingFile(io.BytesIO):
        def read(self, size=-1):
            requests.append(size)
            return super().read(size)

    def growing(target, *args, **kwargs):
        if target == path:
            return GrowingFile(json.dumps({**JOB, "extra": "x" * 100000}).encode())
        return original(target, *args, **kwargs)

    monkeypatch.setattr(Path, "open", growing)
    assert update_worker.run(tmp_path / "app", path, JOB_ID, VERSION, None) == 2
    assert requests
    assert set(requests) == {65537}


@pytest.mark.parametrize("content", [b"[]", b"broken", b'{"extra":NaN}'])
def test_record_damaged_during_lease_acquisition_is_preserved(tmp_path, monkeypatch, content):
    path = tmp_path / "job.json"
    path.write_text(json.dumps({**JOB, "lease_version": 1}))
    released = []

    @contextlib.contextmanager
    def damage_then_acquire(*args):
        path.write_bytes(content)
        try:
            yield True
        finally:
            released.append(True)

    monkeypatch.setattr(update_worker, "worker_lease", damage_then_acquire)
    monkeypatch.setattr(update_worker, "_run", lambda *args: pytest.fail("damaged handoff must not install"))
    assert update_worker.run(tmp_path / "app", path, JOB_ID, VERSION, None) == 2
    assert path.read_bytes() == content
    assert released == [True]


@pytest.mark.parametrize("entry", ["run", "refuse", "broker"])
@pytest.mark.parametrize("identifier", [None, True, [], "../foreign"])
def test_bad_record_identifier_is_not_a_filename_or_an_installation(tmp_path, monkeypatch, entry, identifier):
    test_unreadable_records_never_install_rewrite_or_escape(
        tmp_path, monkeypatch, json.dumps({**JOB, "id": identifier}).encode(), entry
    )


@pytest.mark.parametrize(
    "arguments", [[], ["--after-parent"], ["--after-parent", "bad"], ["--after-parent", "0"], ["one", "two"]]
)
def test_invalid_command_line_refuses_without_opening_a_job(monkeypatch, arguments):
    monkeypatch.setattr(update_worker.sys, "argv", ["worker.py", *arguments])
    monkeypatch.setattr(update_worker, "handoff", lambda *args: pytest.fail("invalid command must not relay"))
    assert update_worker.main() == 2


@pytest.mark.parametrize("mode", ["--handoff", "--after-parent", "--broker-child", ""])
@pytest.mark.parametrize("foreign", ["path", "id"])
def test_every_worker_entry_binds_the_journal_to_the_copied_script(tmp_path, monkeypatch, mode, foreign):
    folder = tmp_path / JOB_ID
    monkeypatch.setattr(update_worker, "__file__", str(folder / "worker.py"))
    record = tmp_path / "job.json" if foreign == "id" else tmp_path / "foreign.json"
    identifier = "c" * 32 if foreign == "id" else JOB_ID
    monkeypatch.setattr(
        update_worker.sys,
        "argv",
        [
            "worker.py",
            *([mode] if mode else []),
            *(["42"] if mode in {"--after-parent", "--broker-child"} else []),
            str(tmp_path / "app"),
            str(record),
            identifier,
            VERSION,
        ],
    )
    monkeypatch.setattr(update_worker, "handoff", lambda *args: pytest.fail("foreign journal must not relay"))
    assert update_worker.main() == 2
    assert not folder.exists()
    assert not record.exists()


@pytest.mark.parametrize("mode", ["--handoff", "--after-parent", ""])
@pytest.mark.parametrize("form", ["canonical", "normalised"])
def test_valid_entry_passes_only_the_derived_journal_to_the_relay(tmp_path, monkeypatch, mode, form):
    folder = tmp_path / JOB_ID
    record = tmp_path / "job.json"
    supplied = record if form == "canonical" else tmp_path / "absent" / ".." / "job.json"
    monkeypatch.setattr(update_worker, "__file__", str(folder / "worker.py"))
    calls = []
    monkeypatch.setattr(update_worker, "handoff", lambda *args: calls.append(args) or 0)
    monkeypatch.setattr(
        update_worker.sys,
        "argv",
        [
            "worker.py",
            *([mode] if mode else []),
            *(["42"] if mode == "--after-parent" else []),
            str(tmp_path / "app"),
            str(supplied),
            JOB_ID,
            VERSION,
        ],
    )
    assert update_worker.main() == 0
    assert calls == [
        (mode, 42 if mode == "--after-parent" else 0, str(tmp_path / "app"), record.resolve(), JOB_ID, VERSION)
    ]
    assert not record.exists()


def test_worker_does_not_resolve_or_inspect_a_foreign_command_line_path(tmp_path, monkeypatch):
    folder = tmp_path / JOB_ID
    foreign = tmp_path / "foreign" / "job.json"
    original = Path.resolve

    def resolve_only_script(path, *args, **kwargs):
        if path == foreign:
            pytest.fail("a supplied journal path must not trigger filesystem resolution")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", resolve_only_script)
    monkeypatch.setattr(update_worker, "__file__", str(folder / "worker.py"))
    monkeypatch.setattr(
        update_worker.sys,
        "argv",
        ["worker.py", "--handoff", str(tmp_path / "app"), str(foreign), JOB_ID, VERSION],
    )
    monkeypatch.setattr(update_worker, "handoff", lambda *args: pytest.fail("foreign path must not relay"))
    assert update_worker.main() == 2


def test_trusted_producer_canonicalizes_an_install_root_alias(tmp_path, monkeypatch):
    from tow import web_update

    real = tmp_path / "real"
    alias = tmp_path / "alias"
    real.mkdir()
    try:
        alias.symlink_to(real, target_is_directory=True)
    except OSError, NotImplementedError:
        pytest.skip("directory symlinks are unavailable on this runner")
    monkeypatch.setattr(web_update, "root", lambda: alias)
    monkeypatch.setattr(web_update, "runtime_dir", lambda: alias / "runtime")
    assert web_update._job_file() == real / "runtime" / "web-update" / "job.json"
    assert not (real / "runtime").exists()
